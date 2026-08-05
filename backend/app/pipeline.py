"""The five stages, in order. The only module that knows the sequence.

    1. Cut-out          two segmentation APIs, auto-pick        ← the only AI
    2. Shadow extract   background plate + ratio map            deterministic
    3. Geometry         scale, centre, exact canvas             deterministic
    4. Background       hex fill, shadow, composite             deterministic
    5. Export           PNG / JPEG / TIFF / WebP                deterministic

Stage 3 runs **before** stage 4 deliberately: resizing after filling would resample the product
against the flat colour and bake edge halos in permanently.

Returns bytes rather than writing anything. Storage and job bookkeeping belong to the API layer,
which keeps this function testable end to end with no Redis, no MinIO and no network.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import numpy as np

from app.core import errors
from app.core.settings import Settings
from app.engines import autopick, registry
from app.engines.base import AlphaResult, BackgroundRemover
from app.imaging import color as C
from app.imaging import composite as X
from app.imaging import export as E
from app.imaging import geometry as G
from app.imaging import shadow as S
from app.models import (
    ImageResult,
    ImageState,
    JobConfig,
    Note,
    OutputFormat,
    ShadowMode,
)

# Decontamination needs only a *local* backdrop estimate, so it tolerates a less uniform
# background than shadow reconstruction does. Below this, the plate is untrustworthy enough that
# un-mixing against it would do more harm than the halo it removes.
_DECONTAM_MIN_UNIFORMITY = 0.25


async def process_image(
    image_bytes: bytes,
    source_name: str,
    config: JobConfig,
    settings: Settings,
    *,
    engines: list[BackgroundRemover] | None = None,
) -> tuple[ImageResult, dict[OutputFormat, bytes]]:
    """Run one image through all five stages.

    Never raises for a per-image problem: a failure becomes an `ImageResult` in the FAILED state
    carrying a typed `ErrorInfo`, so one bad file in a 200-image zip cannot take the job down.
    """
    started = time.perf_counter()
    result = ImageResult(source_name=source_name, state=ImageState.RUNNING)

    try:
        decoded = _decode(image_bytes, settings)
        result.source_size = decoded.source_size

        alpha, notes, candidates, cost, chosen = await _stage1_cutout(
            image_bytes, decoded, config, settings, engines
        )
        result.notes.extend(notes)
        result.candidates = candidates
        result.cost_usd = cost
        result.chosen_engine = chosen

        if alpha is None:
            result.state = ImageState.FAILED
            result.error = errors.VendorError(
                "Every segmentation engine returned an unusable mask for this image."
            ).to_info()
            return result, {}

        rgb, ratio, uniformity, stage2_notes = _stage2_shadow_and_edges(
            decoded, alpha, config, had_source_alpha=decoded.alpha is not None
        )
        result.notes.extend(stage2_notes)
        result.background_uniformity = uniformity

        placement = G.place_on_canvas(rgb, alpha, config.size, config.centring, shadow_ratio=ratio)
        result.notes.extend(placement.notes)
        result.centroid_offset_px = placement.centroid_offset_px

        final_rgb, final_alpha = _stage4_background(placement, config)
        outputs = _stage5_export(final_rgb, final_alpha, config)

        if config.psd.enabled:
            psd_bytes, psd_notes = await _stage5b_psd(placement, config, settings)
            outputs[OutputFormat.PSD] = psd_bytes
            result.notes.extend(psd_notes)

        result.state = ImageState.DONE
        return result, outputs

    except errors.PipelineError as exc:
        result.state = ImageState.FAILED
        result.error = exc.to_info()
        return result, {}
    except Exception:
        # Anything unclassified is our bug, not the user's file. Say so honestly rather than
        # blaming the input, and keep the detail out of the response.
        result.state = ImageState.FAILED
        result.error = errors.PipelineError("An unexpected error occurred processing this image.").to_info()
        return result, {}
    finally:
        result.duration_ms = int((time.perf_counter() - started) * 1000)


# ---------------------------------------------------------------------------
# Stage 0 — decode and normalise
# ---------------------------------------------------------------------------


def _decode(image_bytes: bytes, settings: Settings) -> E.DecodedImage:
    if len(image_bytes) > settings.max_image_bytes:
        raise errors.FileTooLarge(
            f"Image is larger than the {settings.max_image_bytes // 1_048_576} MB limit."
        )
    try:
        return E.decode(image_bytes, max_pixels=settings.max_image_pixels)
    except E.UnsupportedImageError as exc:
        raise errors.ImageDecodeFailed(str(exc)) from exc
    except Exception as exc:
        raise errors.ImageDecodeFailed("This file could not be decoded as an image.") from exc


# ---------------------------------------------------------------------------
# Stage 1 — cut-out
# ---------------------------------------------------------------------------


async def _stage1_cutout(
    image_bytes: bytes,
    decoded: E.DecodedImage,
    config: JobConfig,
    settings: Settings,
    engines: list[BackgroundRemover] | None,
):
    """Run the engine pool concurrently and auto-pick, or reuse a mask the source already had.

    An input PNG that arrives already cut out is used as-is: re-segmenting it would cost money and
    would almost certainly be worse than the mask someone already approved.
    """
    width, height = decoded.source_size

    if decoded.alpha is not None and float(decoded.alpha.min()) < 0.999:
        return decoded.alpha, [], [], 0.0, None

    pool = engines if engines is not None else registry.select_pool(settings, config.cutout)
    results = await _run_pool(pool, image_bytes, width, height)

    if not results:
        raise errors.VendorError("No segmentation engine was reachable for this image.")

    verdict = autopick.choose(results)
    cost = sum(r.cost_usd for r in results)

    if verdict.winner is None:
        return None, verdict.notes, verdict.candidates, cost, None

    return (
        verdict.winner.alpha,
        list(verdict.notes),
        verdict.candidates,
        cost,
        verdict.winner.engine,
    )


async def _run_pool(
    pool: list[BackgroundRemover], image_bytes: bytes, width: int, height: int
) -> list[AlphaResult]:
    """Call every engine concurrently; drop the ones that fail.

    Both engines are billed whether or not their mask wins, which is the accepted cost of the
    dual-engine design. Failures are dropped rather than raised so one vendor being down degrades
    to single-engine instead of failing the image.
    """
    gathered = await asyncio.gather(
        *(engine.alpha_for(image_bytes, width, height) for engine in pool),
        return_exceptions=True,
    )
    return [r for r in gathered if isinstance(r, AlphaResult)]


def cache_key(image_bytes: bytes, engine_id: str) -> str:
    """Cut-outs are deterministic per engine, so re-running while tuning colour costs nothing."""
    return hashlib.sha256(image_bytes).hexdigest() + ":" + engine_id


# ---------------------------------------------------------------------------
# Stage 2 — shadow extraction and edge decontamination
# ---------------------------------------------------------------------------


def _stage2_shadow_and_edges(
    decoded: E.DecodedImage,
    alpha: np.ndarray,
    config: JobConfig,
    *,
    had_source_alpha: bool,
):
    """Estimate the plate, recover the shadow, and un-mix the backdrop out of the edge.

    Both operations need the *original backdrop pixels*, which is why the pipeline keeps its own
    decoded original and takes only alpha from the vendor. An input that arrived already cut out
    has no backdrop to estimate, so both are skipped rather than run against transparent black.
    """
    rgb = decoded.rgb_linear
    notes: list[Note] = []

    if had_source_alpha:
        if config.background.shadow is ShadowMode.PRESERVE:
            notes.append(Note.SHADOW_GATE_FAILED)
        return rgb, None, None, notes

    plate = S.estimate_background_plate(rgb, alpha)

    if config.background.decontaminate_edges and plate.uniformity >= _DECONTAM_MIN_UNIFORMITY:
        rgb = X.decontaminate_edges(rgb, alpha, plate.plate)

    ratio = None
    if config.background.shadow is ShadowMode.PRESERVE:
        ratio = S.extract_shadow_ratio(rgb, alpha, plate)
        if ratio is None and not plate.gate_passed:
            notes.append(Note.SHADOW_GATE_FAILED)

    return rgb, ratio, round(plate.uniformity, 4), notes


# ---------------------------------------------------------------------------
# Stage 4 — background
# ---------------------------------------------------------------------------


def _stage4_background(placement: G.Placement, config: JobConfig):
    background_linear = (
        None
        if config.background.transparent
        else C.hex_to_srgb_linear(config.background.color or "#FFFFFF")
    )
    return X.flatten(
        placement.rgb,
        placement.alpha,
        background_linear=background_linear,
        shadow_ratio=placement.shadow_ratio,
    )


# ---------------------------------------------------------------------------
# Stage 5 — export
# ---------------------------------------------------------------------------


def _stage5_export(
    rgb_linear: np.ndarray, alpha: np.ndarray, config: JobConfig
) -> dict[OutputFormat, bytes]:
    """Encode every requested raster format. PSD is assembled separately by `app.psd`."""
    outputs: dict[OutputFormat, bytes] = {}
    keep_alpha = alpha if config.background.transparent else None

    for fmt in config.export.formats:
        if fmt is OutputFormat.PSD:
            continue
        outputs[fmt] = E.encode(
            rgb_linear,
            keep_alpha,
            fmt=fmt,
            profile=config.export.profile,
            jpeg_quality=config.export.jpeg_quality,
            webp_quality=config.export.webp_quality,
        )
    return outputs


# ---------------------------------------------------------------------------
# Stage 5b — layered PSD (requirement 5)
# ---------------------------------------------------------------------------


async def _stage5b_psd(
    placement: G.Placement, config: JobConfig, settings: Settings
) -> tuple[bytes, list[Note]]:
    """Assemble the layered PSD from the same pre-composite layers stage 4 flattens.

    Uses `placement.rgb`/`placement.alpha` (the product, straight alpha, not yet composited) and
    `placement.shadow_ratio` directly — these are exactly the per-layer arrays a PSD needs, and
    computing them twice would both waste work and risk the raster and PSD outputs drifting apart.
    """
    from app.psd.service import produce_psd

    background_linear = C.hex_to_srgb_linear(config.background.color or "#FFFFFF")
    return await produce_psd(
        product_rgb_linear=placement.rgb,
        product_alpha=placement.alpha,
        background_color_linear=background_linear,
        shadow_ratio=placement.shadow_ratio,
        spec=config.psd,
        settings=settings,
    )

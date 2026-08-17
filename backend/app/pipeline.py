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
import logging
import time
from dataclasses import dataclass

import numpy as np

from app.core import errors
from app.core.settings import Settings
from app.engines import autopick, registry
from app.engines.base import AlphaResult, BackgroundRemover
from app.engines.cache import CutoutCache, NullCutoutCache, hit_result
from app.imaging import color as C
from app.imaging import composite as X
from app.imaging import export as E
from app.imaging import formats as F
from app.imaging import geometry as G
from app.imaging import shadow as S
from app.models import (
    EngineCandidate,
    EngineId,
    ImageResult,
    ImageState,
    JobConfig,
    Note,
    OutputFormat,
    ShadowMode,
    SizeSpec,
    SourceFormat,
)

# Decontamination needs only a *local* backdrop estimate, so it tolerates a less uniform
# background than shadow reconstruction does. Below this, the plate is untrustworthy enough that
# un-mixing against it would do more harm than the halo it removes.
_DECONTAM_MIN_UNIFORMITY = 0.25

# A backdrop this non-uniform is not a studio sweep. On its own that is not an error — a preserved
# shadow is simply skipped — so it only becomes a warning when paired with a weak alpha score,
# which together is the signature of "we cut out the wrong object in a furnished room".
_SCENE_MAX_UNIFORMITY = 0.05
_SCENE_MAX_SCORE = 0.60

# Automatic subject detection degrades silently by design — whole-frame is the right answer for a
# packshot, so it cannot be a per-image warning. That makes it undiagnosable without a log line:
# "detection did nothing" and "detection was never attempted" look identical from the outside.
logger = logging.getLogger(__name__)


async def process_image(
    image_bytes: bytes,
    source_name: str,
    config: JobConfig,
    settings: Settings,
    *,
    engines: list[BackgroundRemover] | None = None,
    cache: CutoutCache | None = None,
) -> tuple[ImageResult, dict[OutputFormat, bytes]]:
    """Run one image through all five stages.

    Never raises for a per-image problem: a failure becomes an `ImageResult` in the FAILED state
    carrying a typed `ErrorInfo`, so one bad file in a 200-image zip cannot take the job down.

    `cache` is optional: with none supplied nothing is cached and no I/O happens here, which keeps
    this function testable with no storage backend. `app.jobs` supplies a real one.
    """
    started = time.perf_counter()
    result = ImageResult(source_name=source_name, state=ImageState.RUNNING)

    try:
        source_format = F.source_from_name(source_name)
        result.source_format = source_format

        decoded = _decode(image_bytes, settings, source_format)
        result.source_size = decoded.source_size
        size = _resolve_size(config.size, decoded.source_size)
        _check_output_size(size, settings)

        formats, format_notes = _resolve_formats(config, source_format)
        result.notes.extend(format_notes)

        # Layered-scene path: one PSD layer per object instead of a single cut-out. Diverges here
        # rather than inside stage 1 because it changes the *shape* of everything downstream —
        # N masks, no single subject to centre on, and no shadow reconstruction.
        if config.cutout.multi_object:
            return await _multi_object_image(
                result, image_bytes, decoded, config, settings, engines, cache, formats, size
            )

        alpha, notes, candidates, cost, chosen, cache_hit, subject_label = await _stage1_cutout(
            image_bytes, decoded, config, settings, engines, cache, source_format
        )
        result.cache_hit = cache_hit
        result.subject_label = subject_label
        result.notes.extend(notes)
        result.candidates = candidates
        result.cost_usd = cost
        result.chosen_engine = chosen

        if alpha is None:
            result.state = ImageState.FAILED
            result.error = errors.NoForegroundFound(_no_usable_mask_message(candidates)).to_info()
            return result, {}

        rgb, ratio, uniformity, stage2_notes = _stage2_shadow_and_edges(
            decoded, alpha, config, had_source_alpha=decoded.alpha is not None
        )
        result.notes.extend(stage2_notes)
        result.background_uniformity = uniformity

        placement = G.place_on_canvas(rgb, alpha, size, config.centring, shadow_ratio=ratio)
        result.notes.extend(placement.notes)
        result.centroid_offset_px = placement.centroid_offset_px

        if _looks_like_a_scene(result, config):
            result.notes.append(Note.BUSY_SCENE)

        final_rgb, final_alpha = _stage4_background(placement, config)
        outputs = _stage5_export(final_rgb, final_alpha, config, formats, source_format)
        result.preview_format = F.preview_format_for(formats)

        # `match_source` can put PSD in the delivered set without `psd.enabled` being ticked — a
        # PSD upload asking for its own format back is a legitimate way to request the layered
        # deliverable. Without this the format resolved to PSD and nothing wrote one.
        if config.psd.enabled or OutputFormat.PSD in formats:
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


def _looks_like_a_scene(result: ImageResult, config: JobConfig) -> bool:
    """Detect "this is a furnished room, not a packshot" from signals already computed.

    Two independent things have to be true, because either alone has honest explanations: a
    non-uniform backdrop is normal for a lifestyle shot the user deliberately chose, and a mediocre
    alpha score happens on genuinely hard subjects. Together they are the signature of the failure
    we actually care about — a confident, high-quality mask of the *wrong object*.

    Suppressed when a subject prompt succeeded: the user has already told us which object they meant
    and the crop enforced it, so the whole-frame backdrop being busy is expected, not a warning.
    """
    if Note.SUBJECT_LOCATED in result.notes:
        return False
    if result.background_uniformity is None:
        return False
    if result.background_uniformity > _SCENE_MAX_UNIFORMITY:
        return False

    scores = [c.score for c in result.candidates if c.rejected_reason is None]
    return bool(scores) and max(scores) < _SCENE_MAX_SCORE


def _resolve_size(size: SizeSpec, source_size: tuple[int, int]) -> SizeSpec:
    """Fill in the canvas dimensions when the job asked for the source's own.

    Resolved once, here, so every stage downstream sees a `SizeSpec` with concrete numbers and
    nothing else has to know the option exists — `place_on_canvas` still guarantees exact output
    dimensions, and `_check_output_size` still bounds the allocation.

    The source can exceed `SizeSpec`'s 20000-per-axis contract bound (a stitched panorama would),
    so the axes are clamped. That is a real ceiling rather than a silent shrink: the pixel-count
    guard below rejects anything genuinely too large, and clamping only bites past 20000 px, which
    no camera raw reaches.
    """
    if not size.match_source:
        return size
    width, height = source_size
    return size.model_copy(
        update={"width": min(max(width, 1), 20000), "height": min(max(height, 1), 20000)}
    )


def _check_output_size(size: SizeSpec, settings: Settings) -> None:
    """Reject a canvas whose *total* pixel count is beyond what this deployment will allocate.

    `SizeSpec` bounds each axis independently (1..20000), which leaves 20000x20000 = 400 MP valid.
    `place_on_canvas` allocates float32 colour, alpha and shadow buffers for that, so a ~60-byte
    config asks for roughly 8 GB — from an endpoint with no auth, and with `WORKER_CONCURRENCY`
    images in flight at once.

    The input side has had a decompression-bomb guard from day one (`max_image_pixels`); this is
    the same control on the output side, which was missing. It lives here rather than as a
    `SizeSpec` validator because it is a deployment limit, not a contract rule: a bigger machine
    can raise it without the frozen contract changing.
    """
    pixels = size.width * size.height
    if pixels > settings.max_output_pixels:
        raise errors.OutputTooLarge(
            f"Requested canvas is {pixels // 1_000_000} MP; this deployment allows up to "
            f"{settings.max_output_pixels // 1_000_000} MP."
        )


def _no_usable_mask_message(candidates: list[EngineCandidate]) -> str:
    """Say *why* every candidate was rejected, not just that they were.

    The reasons come from `autopick.measure` and are the only actionable thing in this failure —
    "mask is fragmented (largest blob 52%)" means the frame is a scene with no single subject, and
    the fix is the Subject field. A bare "unusable mask" sends the user to retry instead, which
    re-bills the vendor for a byte-identical rejection.

    The engine name is included because with a dual pool the two can be rejected for different
    reasons, and "photoroom said X, gemini said Y" is what makes that visible.
    """
    reasons = [
        f"{c.engine.value}: {c.rejected_reason}" for c in candidates if c.rejected_reason
    ]
    detail = f" ({'; '.join(reasons)})" if reasons else ""
    return (
        "No usable cut-out could be found in this image"
        f"{detail}. If the photograph is a scene rather than a single product, name the object "
        "you want in the Subject field."
    )


def _decode(
    image_bytes: bytes, settings: Settings, source: SourceFormat | None = None
) -> E.DecodedImage:
    # 0 disables the byte cap (the default). `max_image_pixels`, applied inside `E.decode` below,
    # is the guard that actually bounds what this allocates.
    if settings.max_image_bytes and len(image_bytes) > settings.max_image_bytes:
        raise errors.FileTooLarge(
            f"Image is larger than the {settings.max_image_bytes // 1_048_576} MB limit."
        )
    try:
        return E.decode(image_bytes, max_pixels=settings.max_image_pixels, source=source)
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
    cache: CutoutCache | None = None,
    source: SourceFormat | None = None,
):
    """Run the engine pool concurrently and auto-pick, or reuse a mask the source already had.

    An input PNG that arrives already cut out is used as-is: re-segmenting it would cost money and
    would almost certainly be worse than the mask someone already approved.
    """
    width, height = decoded.source_size

    if decoded.alpha is not None and float(decoded.alpha.min()) < 0.999:
        return decoded.alpha, [], [], 0.0, None, False, None

    pool = engines if engines is not None else registry.select_pool(settings, config.cutout)
    store = cache if (cache is not None and settings.cache_cutouts) else NullCutoutCache()

    # Vendors get the original bytes when they can read them AND those bytes are small enough to
    # send. Two independent questions that used to be one:
    #
    #   * **Can the vendor read this format?** `needs_reencode_for_vendor`. Photoroom returns a
    #     bare HTTP 400 for an EPS upload; PSD and camera raw are the same class of problem.
    #   * **Will the vendor accept this many bytes?** Nothing asked. BMP is a format Photoroom
    #     reads perfectly well, so a 130 MB uncompressed BMP was forwarded untouched and came back
    #     413 — and, before this, as "may succeed on retry".
    #
    # Re-encoding answers the size question at no cost to quality: the same 45 MP of pixels are
    # 130 MB as a BMP and 41 MB as a PNG. Downscaling is the last resort and says so.
    #
    # The cache key deliberately keeps using the ORIGINAL bytes: identity should be "this file",
    # not "this file as we happened to re-encode it today".
    vendor_bytes = image_bytes
    vendor_notes: list[Note] = []
    if F.needs_reencode_for_vendor(source) or len(image_bytes) > settings.vendor_max_upload_bytes:
        vendor_bytes, vendor_notes = _encode_for_vendor(decoded, settings)

    # Multi-object scenes: isolate the requested subject *before* segmenting, so the engine is
    # asked an answerable question. See app/engines/locate.py.
    roi, locate_notes, subject_label = await _locate_subject(
        vendor_bytes, decoded, config, settings
    )
    locate_notes = list(locate_notes) + vendor_notes
    if roi is not None:
        results, failures = await _run_pool_on_roi(
            pool, decoded, roi, width, height, store, settings
        )
    else:
        results, failures = await _run_pool(
            pool, image_bytes, width, height, store, vendor_bytes=vendor_bytes
        )

    if not results:
        # Surface the most informative failure rather than a generic one. Dropping exceptions is
        # right when *some* engine succeeded — one vendor being down should degrade, not fail — but
        # when they all failed, the reason is the only useful thing we have. A 402 reported as "no
        # engine was reachable" reads like a network fault and costs a debugging round.
        raise _most_informative(failures)

    verdict = autopick.choose(results)
    verdict.notes.extend(locate_notes)
    cost = sum(r.cost_usd for r in results)

    # Reported only when *every* candidate came from the cache; a partial hit still cost money, so
    # calling that a cache hit would misreport the ledger.
    cache_hit = all(r.cache_hit for r in results)

    if verdict.winner is None:
        return None, verdict.notes, verdict.candidates, cost, None, cache_hit, subject_label

    if autopick.needs_vision_tiebreak(verdict):
        verdict, tiebreak_cost = await _vision_tiebreak(decoded, results, verdict, settings)
        cost += tiebreak_cost

    # Gemini's mask is a rasterised polygon, so it carries no soft edge and leaves
    # `decontaminate_edges` nothing to correct. The operator chose that trade explicitly, but the
    # result must still say so — see Note.HARD_EDGED_MASK and app/engines/gemini_segment.py.
    if verdict.winner.engine is EngineId.GEMINI:
        verdict.notes.append(Note.HARD_EDGED_MASK)

    return (
        verdict.winner.alpha,
        list(verdict.notes),
        verdict.candidates,
        cost,
        verdict.winner.engine,
        cache_hit,
        subject_label,
    )


def _most_informative(failures: list[BaseException]) -> errors.PipelineError:
    """Pick the failure worth showing the user when every engine failed.

    Ordered by how actionable it is: an account or credential problem is something the user can fix
    right now, so it outranks a transient network error that merely says "try again".
    """
    ranked = (
        errors.VendorOutOfCredits,
        errors.VendorUnauthorized,
        errors.VendorRateLimited,
        errors.VendorTimeout,
    )
    for kind in ranked:
        for exc in failures:
            if isinstance(exc, kind):
                return exc
    for exc in failures:
        if isinstance(exc, errors.PipelineError):
            return exc
    return errors.VendorError("No segmentation engine was reachable for this image.")


async def _run_pool(
    pool: list[BackgroundRemover],
    image_bytes: bytes,
    width: int,
    height: int,
    cache: CutoutCache | None = None,
    vendor_bytes: bytes | None = None,
) -> tuple[list[AlphaResult], list[BaseException]]:
    """Call every engine concurrently; drop the ones that fail.

    Both engines are billed whether or not their mask wins, which is the accepted cost of the
    dual-engine design. Failures are dropped rather than raised so one vendor being down degrades
    to single-engine instead of failing the image.

    Each engine is checked against the cache independently. That matters for the asymmetric case:
    when a second key is added mid-session, the already-seen engine still serves from cache while
    only the new one is billed.

    `image_bytes` is the cache identity; `vendor_bytes` is what actually goes over the wire. They
    differ only for formats no vendor can read (EPS, PSD, camera raw) — see `_stage1_cutout`.
    """
    store = cache if cache is not None else NullCutoutCache()
    upload = vendor_bytes if vendor_bytes is not None else image_bytes

    async def one(engine: BackgroundRemover) -> AlphaResult:
        key = cache_key(image_bytes, engine.id.value)
        # Held across the vendor call so duplicate images coalesce onto one billed request rather
        # than all missing the cache together. See `CutoutCache.entry_lock`.
        async with store.entry_lock(key):
            cached = store.get(key)
            if cached is not None:
                return hit_result(cached, engine.id)

            result = await engine.alpha_for(upload, width, height)
            store.put(key, result.alpha)
            return result

    gathered = await asyncio.gather(
        *(one(engine) for engine in pool),
        return_exceptions=True,
    )
    return (
        [r for r in gathered if isinstance(r, AlphaResult)],
        [e for e in gathered if isinstance(e, BaseException)],
    )


def cache_key(image_bytes: bytes, engine_id: str, roi: G.BBox | None = None) -> str:
    """Cut-outs are deterministic per engine, so re-running while tuning colour costs nothing.

    The ROI is part of the key: the same photograph cropped to the coffee table and cropped to the
    sofa are two different segmentation problems with two different answers.
    """
    key = hashlib.sha256(image_bytes).hexdigest() + ":" + engine_id
    if roi is not None:
        key += f":{roi.x0},{roi.y0},{roi.x1},{roi.y1}"
    return key


async def _locate_subject(
    image_bytes: bytes,
    decoded: E.DecodedImage,
    config: JobConfig,
    settings: Settings,
) -> tuple[G.BBox | None, list[Note], str | None]:
    """Resolve `cutout.subject_prompt` to a region of interest.

    Returns ``(None, notes)`` when no prompt was given — the packshot path, unchanged — or when the
    subject could not be located, in which case `SUBJECT_NOT_FOUND` is emitted and the caller falls
    back to whole-frame segmentation. That fallback is deliberately *loud*: on a multi-object scene
    it will very likely cut out the wrong object, and silently delivering that is the failure this
    whole mechanism exists to prevent.
    """
    from app.engines.locate import GeminiLocator, SubjectNotLocated

    phrase = config.cutout.subject_prompt
    width, height = decoded.source_size
    locator = GeminiLocator(settings)

    if not phrase:
        # No named subject. Either segment whole-frame as before, or detect one automatically.
        if not settings.auto_subject_enabled:
            return None, [], None
        return await _auto_locate_subject(image_bytes, locator, config, width, height)

    try:
        located = await locator.locate(
            image_bytes, phrase, width, height, padding_pct=config.cutout.subject_padding_pct
        )
    except SubjectNotLocated:
        return None, [Note.SUBJECT_NOT_FOUND], None
    except Exception:
        # Broad on purpose: localisation is advisory, and must not add a way for an image to fail.
        return None, [Note.SUBJECT_NOT_FOUND], None

    # No label on this path: the user named the subject, so echoing their own words back as a
    # "detected" label would dress an instruction up as a finding.
    return located.box, [Note.SUBJECT_LOCATED], None


async def _auto_locate_subject(
    image_bytes: bytes,
    locator,
    config: JobConfig,
    width: int,
    height: int,
) -> tuple[G.BBox | None, list[Note], str | None]:
    """Detect a subject with no prompt: enumerate the objects, then pick the dominant one.

    Uses `GeminiLocator.detect`, which is `locate` with the phrase discovered instead of typed —
    same request, schema, parser and padding. That sharing is the point: measured against a real
    packshot, the named-subject path produced a correct tight crop while an enumerate-and-rank
    approach returned nothing at all, so the auto path runs on the machinery already known to
    work. Six calls at temperature 0 returned the identical box, so it is reproducible in practice
    even though the choice now sits with the model.

    The model's box is still checked by our own arithmetic before it is trusted.

    Every failure returns ``(None, [])`` and falls back to the unchanged whole-frame path. That is
    the *right* answer for a packshot, so a failure here is not an error and must not be reported
    as one: one product on a sweep needs no crop, and whole-frame segmentation already had it.
    """
    from app.engines.locate import SubjectNotLocated, box_is_plausible_subject

    try:
        located = await locator.detect(
            image_bytes, width, height, padding_pct=config.cutout.subject_padding_pct
        )
    except SubjectNotLocated as exc:
        # The ordinary "this is a packshot, there is nothing to crop to" answer as well as a
        # vendor failure. Logged rather than noted: whole-frame is the *correct* result for a
        # single product on a sweep, so surfacing it as a warning would cry wolf on most images.
        logger.info("auto subject detection found nothing: %s", exc)
        return None, [], None
    except Exception:
        # Broad on purpose, exactly as the prompted path is: automatic localisation is advisory
        # and must not add a way for an image to fail.
        logger.warning("auto subject detection failed", exc_info=True)
        return None, [], None

    # The model's box, checked by our own arithmetic before it is trusted. A box covering the
    # whole frame is the model boxing the scene rather than a product in it, and cropping to that
    # disables a whole-frame path that was already correct.
    if not box_is_plausible_subject(located.raw_box, width, height):
        logger.info("auto subject box rejected as implausible: %s", located.raw_box)
        return None, [], None

    return located.box, [Note.SUBJECT_AUTO_DETECTED], located.label or None


async def _run_pool_on_roi(
    pool: list[BackgroundRemover],
    decoded: E.DecodedImage,
    roi: G.BBox,
    width: int,
    height: int,
    store,
    settings: Settings,
) -> tuple[list[AlphaResult], list[BaseException]]:
    """Segment only inside `roi`, then place the mask back at full-frame coordinates.

    Cropping *and returning a cropped image* would be simpler, but it throws away the surrounding
    pixels that stage 2 needs for plate estimation and edge decontamination, and it would make the
    result's `source_size` disagree with the file the user uploaded. Instead the alpha comes back
    full-size with zeros outside the box, which is exactly what the geometry stage already knows how
    to centre — `alpha_bbox` finds the subject and nothing else.
    """
    crop_png, _downscaled = _encode_crop(decoded, roi, settings)
    # `crop_w/crop_h` stay the ROI's true dimensions even when the upload was downscaled: they are
    # what `_paste_alpha` places the mask back at, and `fit_alpha_to_source` has already resized
    # the vendor's mask to them.
    crop_w, crop_h = roi.width, roi.height

    async def one(engine: BackgroundRemover) -> AlphaResult:
        key = cache_key(crop_png, engine.id.value, roi)
        async with store.entry_lock(key):
            cached = store.get(key)
            if cached is not None:
                return hit_result(_paste_alpha(cached, roi, width, height), engine.id)

            result = await engine.alpha_for(crop_png, crop_w, crop_h)
            store.put(key, result.alpha)
            return AlphaResult(
                alpha=_paste_alpha(result.alpha, roi, width, height),
                engine=result.engine,
                latency_ms=result.latency_ms,
                cost_usd=result.cost_usd,
                cache_hit=result.cache_hit,
            )

    gathered = await asyncio.gather(*(one(e) for e in pool), return_exceptions=True)
    return (
        [r for r in gathered if isinstance(r, AlphaResult)],
        [e for e in gathered if isinstance(e, BaseException)],
    )


def _encode_under_budget(
    rgb_linear: np.ndarray, settings: Settings
) -> tuple[bytes, bool]:
    """PNG of these pixels, small enough for a vendor to accept. Returns (bytes, downscaled).

    Two steps, in this order, because only the second costs anything:

    1. **Re-encode.** Lossless and usually sufficient — 45 MP is 130 MB as an uncompressed BMP and
       41 MB as a PNG, the identical pixels either way.
    2. **Downscale**, halving the long edge until it fits or the floor is reached. This does cost
       mask precision, so the caller emits `Note.VENDOR_DOWNSCALED`.

    Downscaling is safe to do at all only because engines return **alpha, never colour**, and
    `base.fit_alpha_to_source` already resizes a vendor's mask back to the source dimensions —
    written for vendors that silently cap resolution themselves, which is the same situation
    arrived at deliberately. No output pixel is touched; the composite still uses the pipeline's
    own full-resolution original.
    """
    encoded = E.encode(rgb_linear, None, fmt=OutputFormat.PNG, embed_profile=False)
    if len(encoded) <= settings.vendor_max_upload_bytes:
        return encoded, False

    import cv2

    height, width = rgb_linear.shape[:2]
    long_edge = max(width, height)

    while long_edge > settings.vendor_min_long_edge_px:
        long_edge = max(settings.vendor_min_long_edge_px, long_edge // 2)
        scale = long_edge / max(width, height)
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
        small = np.stack(
            [
                cv2.resize(rgb_linear[..., c], (new_w, new_h), interpolation=cv2.INTER_AREA)
                for c in range(3)
            ],
            axis=-1,
        )
        encoded = E.encode(small, None, fmt=OutputFormat.PNG, embed_profile=False)
        if len(encoded) <= settings.vendor_max_upload_bytes:
            return encoded, True

    # Still too big at the floor. Send it and let the vendor's own 413 be the error, rather than
    # inventing one here — a mask from anything smaller would not be usable at full resolution
    # anyway, and the vendor's refusal is the more honest report.
    return encoded, True


def _encode_crop(decoded: E.DecodedImage, roi: G.BBox, settings: Settings) -> tuple[bytes, bool]:
    """PNG of just the ROI, re-encoded from the pipeline's own decoded pixels.

    Encoded from `rgb_linear` via the normal export path rather than from the original file bytes,
    so EXIF rotation and colour-profile normalisation that `decode` already applied are preserved —
    cropping the raw upload would reintroduce both.

    Budgeted like the full frame: an ROI covering most of a 45 MP source is just as capable of
    tripping a vendor's upload limit as the whole thing.
    """
    patch = decoded.rgb_linear[roi.y0 : roi.y1, roi.x0 : roi.x1]
    return _encode_under_budget(patch, settings)


async def _multi_object_image(
    result: ImageResult,
    image_bytes: bytes,
    decoded: E.DecodedImage,
    config: JobConfig,
    settings: Settings,
    engines: list[BackgroundRemover] | None,
    cache: CutoutCache | None,
    formats: list[OutputFormat],
    size: SizeSpec,
) -> tuple[ImageResult, dict[OutputFormat, bytes]]:
    """A scene delivered as one PSD layer per object.

    Three deliberate departures from the packshot path, each because the packshot assumption is
    simply false for a room:

    * **No centring.** Objects mean something only in their original relative positions, so the
      whole frame is fitted as a unit (`_fit_layers_to_canvas`) and every mask moves with it.
    * **No shadow reconstruction.** It needs a uniform backdrop to estimate a plate against, and a
      furnished room has none. The gate would refuse anyway; skipping is honest, not a shortcut.
    * **No background replacement.** The point is a layered file over the original scene, not a
      product on a flat colour.

    Raster formats still export, showing the composite, so a PNG request is not silently ignored.
    """
    pool = engines if engines is not None else registry.select_pool(settings, config.cutout)
    store = cache if (cache is not None and settings.cache_cutouts) else NullCutoutCache()

    layers, notes, cost = await _segment_objects(decoded, config, settings, pool, store)
    result.notes.extend(notes)
    result.cost_usd = cost

    if not layers:
        result.state = ImageState.FAILED
        result.error = errors.NoForegroundFound(
            "No separable objects were found in this image. Multi-object layering needs a scene "
            "with distinct items; for a single product, turn it off."
        ).to_info()
        return result, {}

    rgb_canvas, placed, _scale = _fit_layers_to_canvas(decoded, layers, config, size)
    result.layers = [l.name for l in placed]
    result.chosen_engine = pool[0].id if pool else None

    outputs: dict[OutputFormat, bytes] = {}

    # The composite is the fitted frame itself — every object is already in it.
    union = np.zeros(rgb_canvas.shape[:2], dtype=np.float32)
    for layer in placed:
        union = np.maximum(union, layer.alpha)

    for fmt in formats:
        if fmt is OutputFormat.PSD:
            continue
        keep = union if config.background.transparent else None
        outputs[fmt] = E.encode(
            rgb_canvas, keep, fmt=fmt, profile=config.export.profile,
            jpeg_quality=config.export.jpeg_quality,
            webp_quality=config.export.webp_quality,
        )

    # A layered-scene job usually asks for PSD alone, which no browser renders — so this path
    # needs the viewable preview even more than the packshot one does.
    preview = F.preview_format_for(formats)
    if preview is not None and preview not in outputs:
        keep = union if config.background.transparent else None
        outputs[preview] = E.encode(
            rgb_canvas, keep, fmt=preview, profile=config.export.profile
        )
    result.preview_format = preview

    if config.psd.enabled or OutputFormat.PSD in formats:
        from app.psd.service import produce_layered_psd

        psd_bytes, psd_notes = produce_layered_psd(
            frame_rgb_linear=rgb_canvas,
            objects=[(l.name, l.alpha) for l in placed],
            spec=config.psd,
        )
        outputs[OutputFormat.PSD] = psd_bytes
        result.notes.extend(psd_notes)

    result.state = ImageState.DONE
    return result, outputs


@dataclass
class ObjectLayer:
    """One enumerated object, as a canvas-aligned mask ready to become a PSD layer."""

    name: str
    alpha: np.ndarray
    """(H, W) float32 on the *output canvas*, not the source frame."""

    cost_usd: float = 0.0


async def _segment_objects(
    decoded: E.DecodedImage,
    config: JobConfig,
    settings: Settings,
    pool: list[BackgroundRemover],
    store: CutoutCache,
) -> tuple[list[ObjectLayer], list[Note], float]:
    """Enumerate the objects in a scene and cut each one out separately.

    The layered-PSD-of-a-room case. Division of labour is the same as the single-subject prompt
    path, just repeated: Gemini decides *what the objects are* (semantics), and the segmentation
    engine produces every mask at full precision from a crop around each one.

    Cost scales with the object count — one segmentation call each — which is why
    `locate.MAX_OBJECTS` caps it. Objects arrive largest-first, so the cap drops the least
    significant things.

    Degrades to `[]` on any failure, leaving the caller on the ordinary single-subject path.
    """
    from app.engines.locate import GeminiLocator, locate_many

    width, height = decoded.source_size
    # Budgeted too: the locator is a vendor call like any other, and a 45 MP frame is as likely to
    # be refused by Gemini as by a segmentation engine.
    frame_png, _frame_notes = _encode_for_vendor(decoded, settings)

    found = await locate_many(
        GeminiLocator(settings),
        frame_png,
        width,
        height,
        padding_pct=config.cutout.subject_padding_pct,
    )
    if not found:
        return [], [Note.SUBJECT_NOT_FOUND], 0.0

    layers: list[ObjectLayer] = []
    failures: list[BaseException] = []
    cost = 0.0
    for obj in found:
        try:
            results, roi_failures = await _run_pool_on_roi(
                pool, decoded, obj.box, width, height, store, settings
            )
        except Exception as exc:  # noqa: BLE001 - kept, but no longer discarded
            failures.append(exc)
            continue
        failures.extend(roi_failures)
        if not results:
            continue

        verdict = autopick.choose(results)
        cost += sum(r.cost_usd for r in results)
        if verdict.winner is None:
            # A rejected mask for one object is not a reason to lose the other eight.
            continue
        layers.append(ObjectLayer(name=obj.label, alpha=verdict.winner.alpha))

    if not layers:
        # The reason matters, and this used to throw it away. Every object failing because the
        # vendor rate-limited us was reported as "no separable objects were found in this image" —
        # blaming the photograph for a 429, and sending someone to look for objects that are
        # plainly there. `_most_informative` already exists for exactly this, in `_stage1_cutout`.
        #
        # Rate limiting is the *expected* failure here rather than a rare one: this mode fires one
        # segmentation call per object, so a nine-object room is a nine-call burst.
        if failures:
            raise _most_informative(failures)
        return [], [Note.SUBJECT_NOT_FOUND], cost

    return layers, [Note.MULTI_OBJECT], cost


def _fit_layers_to_canvas(
    decoded: E.DecodedImage, layers: list[ObjectLayer], config: JobConfig, size: SizeSpec
) -> tuple[np.ndarray, list[ObjectLayer], float]:
    """Scale the whole frame onto the canvas, moving every object mask identically.

    Deliberately *not* `place_on_canvas`. That centres one subject, which is right for a packshot
    and wrong here: a room's objects are meaningful only in their original relative positions, and
    centring the bed would slide every other layer out of register with it. So the frame is fitted
    as a unit and each mask rides along with it.
    """
    out_w, out_h = size.width, size.height
    src_h, src_w = decoded.rgb_linear.shape[:2]

    margin = 1.0 - (size.margin_pct / 100.0) * 2.0
    scale = min(out_w / src_w, out_h / src_h) * max(margin, 0.05)
    if scale > 1.0 and not size.allow_upscale:
        scale = 1.0

    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    x0 = (out_w - new_w) // 2
    y0 = (out_h - new_h) // 2

    def onto_canvas(field: np.ndarray) -> np.ndarray:
        full = np.zeros((out_h, out_w), dtype=np.float32)
        full[y0 : y0 + new_h, x0 : x0 + new_w] = G.resample_scalar(field, new_w, new_h)
        return full

    # Pad with the configured background colour, not zeros. Zeros are BLACK, so a frame smaller
    # than the canvas came out as a photograph floating in a black surround — measured composite
    # mean 32/255 on a 445x449 source fitted into 900x900.
    pad = C.hex_to_srgb_linear(config.background.color or "#FFFFFF")
    rgb_canvas = np.broadcast_to(pad, (out_h, out_w, 3)).astype(np.float32).copy()
    for c in range(3):
        rgb_canvas[y0 : y0 + new_h, x0 : x0 + new_w, c] = G.resample_scalar(
            decoded.rgb_linear[..., c], new_w, new_h
        )

    moved = [ObjectLayer(name=l.name, alpha=onto_canvas(l.alpha), cost_usd=l.cost_usd)
             for l in layers]
    return rgb_canvas, moved, scale


def _encode_for_vendor(
    decoded: E.DecodedImage, settings: Settings
) -> tuple[bytes, list[Note]]:
    """Full-frame PNG a segmentation vendor can both read and accept.

    Two reasons to reach here, and they are independent:

    * **Format.** EPS, PSD and camera raw all decode fine but are rejected by vendors — an EPS
      upload came back from Photoroom as a bare HTTP 400.
    * **Size.** A format the vendor reads happily can still be too many bytes. A 130 MB BMP was
      refused with a 413; the same pixels as PNG are 41 MB and are accepted.

    Re-encoding from `rgb_linear` (rather than the original bytes) keeps the EXIF rotation and
    profile normalisation `decode` already applied, exactly as `_encode_crop` does for the ROI path.

    Alpha is dropped deliberately: a PSD source can carry one, and handing a vendor a
    part-transparent image asks it to segment something already segmented. The pre-cut short
    circuit in `_stage1_cutout` has already claimed that case before this is reached.
    """
    encoded, downscaled = _encode_under_budget(decoded.rgb_linear, settings)
    return encoded, [Note.VENDOR_DOWNSCALED] if downscaled else []


def _paste_alpha(alpha_roi: np.ndarray, roi: G.BBox, width: int, height: int) -> np.ndarray:
    """Place an ROI-sized mask into a full-frame mask, zero everywhere else."""
    full = np.zeros((height, width), dtype=np.float32)
    a = np.asarray(alpha_roi, dtype=np.float32)
    if a.shape != (roi.height, roi.width):
        import cv2

        a = cv2.resize(a, (roi.width, roi.height), interpolation=cv2.INTER_AREA)
    full[roi.y0 : roi.y1, roi.x0 : roi.x1] = np.clip(a, 0.0, 1.0)
    return full


# Estimated per-call price of the vision tie-break, recorded into the ledger. An estimate, like the
# other vendor costs in this codebase — see app/engines/http.py.
_TIEBREAK_COST_USD = 0.0012


async def _vision_tiebreak(
    decoded: E.DecodedImage,
    results: list[AlphaResult],
    verdict: autopick.Verdict,
    settings: Settings,
) -> tuple[autopick.Verdict, float]:
    """Ask a vision model which of two indistinguishable masks is better.

    This is the *only* place a model exercises judgement in the pipeline, and it is reached only
    when the deterministic scorer in `autopick.py` genuinely cannot separate two candidates. It
    never touches colour, geometry or output — it returns one of two engine names.

    Every failure path returns the deterministic verdict unchanged. A tie-break that fails must
    never fail an image, because a correct-enough answer already exists; the measured value of the
    call is breaking ties, not producing the answer.
    """
    from app.engines.vision import GeminiTiebreak

    tiebreak = GeminiTiebreak(settings)
    if not tiebreak.available():
        return verdict, 0.0

    # GeminiTiebreak compares exactly two. Rank the un-rejected candidates the same way `choose`
    # did and take the top pair, so the model is shown the two that actually tied.
    rejected = {c.engine for c in verdict.candidates if c.rejected_reason is not None}
    by_score = sorted(
        (c for c in verdict.candidates if c.rejected_reason is None),
        key=lambda c: c.score,
        reverse=True,
    )
    if len(by_score) != 2:
        return verdict, 0.0

    alpha_for = {r.engine: r for r in results if r.engine not in rejected}
    try:
        pairs = [(c.engine, alpha_for[c.engine].alpha) for c in by_score]
    except KeyError:
        return verdict, 0.0

    try:
        winner_engine, _reason = await tiebreak.pick(decoded.rgb_linear, pairs)
    except Exception:
        # Deliberately broad: `TiebreakUnavailable` covers the expected paths (no key, bias
        # detected, timeout, bad response), but an advisory call must not introduce *any* new way
        # for an image to fail. The deterministic winner is already correct enough to ship.
        return verdict, _TIEBREAK_COST_USD

    winner = alpha_for.get(winner_engine)
    if winner is None:
        return verdict, _TIEBREAK_COST_USD

    notes = [n for n in verdict.notes if n is not Note.TIEBREAK_DETERMINISTIC]
    notes.append(Note.TIEBREAK_VISION)

    # ENGINE_FALLBACK_USED describes "the preferred engine lost", which the model may have just
    # changed. Recompute it rather than leaving a stale note from the deterministic pass.
    notes = [n for n in notes if n is not Note.ENGINE_FALLBACK_USED]
    if winner_engine is not results[0].engine:
        notes.append(Note.ENGINE_FALLBACK_USED)

    return autopick.Verdict(winner=winner, candidates=verdict.candidates, notes=notes), _TIEBREAK_COST_USD


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
        if ratio is None:
            # Emitted whenever preservation was asked for and did not happen, whatever refused it —
            # the uniformity gate, the coverage guard, or an all-flat map. Previously this was
            # conditioned on `not plate.gate_passed`, which meant the coverage guard could decline
            # silently. A shadow quietly not being preserved is exactly the kind of unexplained
            # difference that reads as a bug during a demo.
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


def _resolve_formats(
    config: JobConfig, source: SourceFormat | None
) -> tuple[list[OutputFormat], list[Note]]:
    """Which formats this image is delivered in, and any notes that decision produces.

    `export.formats` is the explicit request. `export.match_source` adds the source's own format
    on top, so "give me the file back as what I sent" and "always also give me a PNG" compose
    instead of fighting. The union is deduped in request order, explicit first.

    A source we cannot write (camera raw) resolves to 16-bit TIFF and reports
    `Note.FORMAT_SUBSTITUTED` — see `app/imaging/formats.py` for why no encoder can exist.
    """
    wanted: list[OutputFormat] = list(config.export.formats)
    notes: list[Note] = []

    if config.export.match_source and source is not None:
        matched, substituted = F.output_for(source)

        # JPEG, BMP and EPS carry no alpha. `JobConfig` already rejects them in `export.formats`
        # alongside `transparent`, but match_source adds a format *after* that validation — so a
        # transparent job on a .jpg upload used to append JPEG, hit the encoder's alpha guard, and
        # die as `internal_error`. Substituting a lossless alpha-capable format keeps the promise
        # ("same format back") as closely as transparency allows, and says so.
        if config.background.transparent and matched in F.NO_ALPHA_FORMATS:
            matched = F.TRANSPARENT_SUBSTITUTE
            substituted = True

        if matched not in wanted:
            wanted.append(matched)
        if substituted:
            notes.append(Note.FORMAT_SUBSTITUTED)

    if not wanted:
        # `export.formats` may now be empty, meaning "just give me back what I uploaded". That
        # relies on `match_source` resolving to something — and it cannot when the source format
        # is unknown (an extension we do not recognise, which still decoded fine). Without this
        # the image would produce only the browser preview, which is explicitly not a deliverable,
        # so the job would "succeed" and the download bundle would be empty for that file.
        wanted.append(F.PREVIEW_FORMAT)
        notes.append(Note.FORMAT_SUBSTITUTED)

    return wanted, notes


def _stage5_export(
    rgb_linear: np.ndarray,
    alpha: np.ndarray,
    config: JobConfig,
    formats: list[OutputFormat],
    source: SourceFormat | None,
) -> dict[OutputFormat, bytes]:
    """Encode every requested raster format. PSD is assembled separately by `app.psd`."""
    outputs: dict[OutputFormat, bytes] = {}
    keep_alpha = alpha if config.background.transparent else None

    # 16-bit only when the source actually carried more than 8 bits, i.e. camera raw. Writing a
    # 16-bit TIFF for an 8-bit JPEG source would double the file size to store nothing.
    deep = source is not None and source in F.DEEP_TIFF_SOURCES

    for fmt in formats:
        if fmt is OutputFormat.PSD:
            continue
        outputs[fmt] = E.encode(
            rgb_linear,
            keep_alpha,
            fmt=fmt,
            profile=config.export.profile,
            jpeg_quality=config.export.jpeg_quality,
            webp_quality=config.export.webp_quality,
            deep=deep,
        )

    # A job asking only for TIFF, EPS or PSD delivers correct files that no browser can paint, so
    # the results grid showed an empty card. Add a PNG purely to look at. Not a deliverable: the
    # caller records it as `preview_format` and it is kept out of the download bundle.
    preview = F.preview_format_for(formats)
    if preview is not None and preview not in outputs:
        outputs[preview] = E.encode(
            rgb_linear, keep_alpha, fmt=preview, profile=config.export.profile
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

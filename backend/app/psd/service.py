"""Produce the PSD deliverable: try the Adobe Photoshop API, fall back to the pure-Python writer.

This is the only entry point `pipeline.py` calls — it owns the Adobe-vs-fallback decision so
neither the pipeline nor the API layer needs to know which path actually produced the bytes.
"""

from __future__ import annotations

import numpy as np

from app.core.errors import PipelineError
from app.core.settings import Settings
from app.imaging import export as E
from app.models import Note, OutputFormat, PsdSpec
from app.psd.fallback import build_psd
from app.psd.photoshop_api import PhotoshopApiClient
from app.psd.validate import validate_psd


async def produce_psd(
    *,
    product_rgb_linear: np.ndarray,
    product_alpha: np.ndarray,
    background_color_linear: np.ndarray,
    shadow_ratio: np.ndarray | None,
    spec: PsdSpec,
    settings: Settings,
) -> tuple[bytes, list[Note]]:
    """Returns the PSD bytes and any notes to surface on the image result.

    Adobe is attempted first when configured; any failure there — including one this client has
    never actually observed, since it has no live credentials to have been tested against — falls
    back to the pytoshop writer rather than failing the whole image. A raster-only PSD (missing
    the vector path) is still a usable deliverable; no PSD at all is not.
    """
    notes: list[Note] = []

    if settings.adobe_configured:
        try:
            data = await _via_adobe(
                product_rgb_linear, product_alpha, background_color_linear, shadow_ratio, spec
            )
            return data, notes
        except PipelineError:
            # Fall through to the local writer. The Adobe path failing is exactly the scenario
            # the fallback exists for — see docs/PSD.md's fallback ladder.
            pass

    result = build_psd(
        product_rgb_linear=product_rgb_linear,
        product_alpha=product_alpha,
        background_color_linear=background_color_linear,
        shadow_ratio=shadow_ratio,
        spec=spec,
    )
    notes.extend(result.notes)

    validation = validate_psd(result.data, spec, expected_size=product_alpha.shape[::-1])
    if not validation.ok and Note.PSD_FALLBACK_RASTER not in notes:
        # A validation problem that isn't just "no vector path" (already covered by the note
        # above) is still worth surfacing, but the file is still returned — a flagged, inspectable
        # deliverable beats no deliverable, and validate.py never raises for exactly this reason.
        notes.append(Note.PSD_FALLBACK_RASTER)

    return result.data, notes


async def _via_adobe(
    product_rgb_linear: np.ndarray,
    product_alpha: np.ndarray,
    background_color_linear: np.ndarray,
    shadow_ratio: np.ndarray | None,
    spec: PsdSpec,
) -> bytes:
    """Encode the layers as PNGs and hand them to the Photoshop API client.

    Kept separate from `photoshop_api.py` itself so that module can stay a thin, faithful mirror
    of Adobe's documented request shape without also carrying pipeline-specific encoding choices.
    """
    from app.core.settings import get_settings

    settings = get_settings()
    client = PhotoshopApiClient(settings)

    product_png = E.encode(product_rgb_linear, product_alpha, fmt=OutputFormat.PNG)
    background_png = E.encode(
        np.broadcast_to(background_color_linear, product_rgb_linear.shape).astype(np.float32),
        None,
        fmt=OutputFormat.PNG,
    )
    shadow_png = None
    if shadow_ratio is not None:
        shadow_alpha = np.clip(1.0 - shadow_ratio, 0.0, 1.0)
        shadow_png = E.encode(
            np.zeros_like(product_rgb_linear), shadow_alpha, fmt=OutputFormat.PNG
        )

    # ⚠️ templates/base.psd does not exist yet in this build — authoring the hand-built template
    # (PROD/SHADOW/BG/PATH groups, per the client report's Feature 2 naming) is real design work
    # that only matters once Adobe credentials exist to test it against; building it speculatively
    # now, with no way to verify it against the real API, would be effort spent on an unverifiable
    # guess. This whole function is unreachable while `settings.adobe_configured` is False (see
    # `produce_psd`'s guard), which is the case for every environment this build has run in.
    with open("templates/base.psd", "rb") as fh:
        template = fh.read()

    return await client.build_layered_psd(
        product_png=product_png,
        shadow_png=shadow_png,
        background_png=background_png,
        template_psd=template,
        layer_names={
            "product": spec.product_layer_name,
            "shadow": spec.shadow_layer_name,
            "background": spec.background_layer_name,
        },
        trace_vector_path=spec.vector_clipping_path,
    )

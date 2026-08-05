"""The segmentation engine interface — stage 1, and the only AI in the default pipeline.

Adding an engine is one new module plus one line in `registry.py`. Nothing else changes.

The important design decision here: **an engine returns only alpha, never colour.**

Vendors hand back a cut-out PNG whose transparent region has been zeroed or decontaminated to
their taste. That is unusable for us, because two later stages need the *original* backdrop
pixels: the plate estimate that drives shadow reconstruction, and the edge decontamination that
un-mixes the real backdrop out of the edge. So the pipeline keeps its own decoded original and
takes only the mask from the vendor. It also means switching engines cannot change product
colour — only the mask quality moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import cv2
import numpy as np

from app.models import EngineId


@dataclass
class AlphaResult:
    """One engine's mask for one image."""

    alpha: np.ndarray
    """(H, W) float32 in [0, 1], matching the *source* image dimensions."""

    engine: EngineId
    latency_ms: int
    cost_usd: float
    cache_hit: bool = False


@runtime_checkable
class BackgroundRemover(Protocol):
    """Implemented by every engine. Keep it this small."""

    id: EngineId
    cost_per_image_usd: float

    def available(self) -> bool:
        """Whether this engine is usable — normally "is a key configured"."""
        ...

    async def alpha_for(self, image_bytes: bytes, width: int, height: int) -> AlphaResult:
        """Return a mask sized exactly (height, width).

        Implementations must raise a `app.core.errors.PipelineError` subclass on failure so the
        API can classify it, rather than leaking vendor exception types.
        """
        ...


def fit_alpha_to_source(alpha: np.ndarray, width: int, height: int) -> np.ndarray:
    """Resize a vendor mask to the source dimensions.

    Several APIs silently cap the long edge (commonly at 1500-2000px on lower tiers) and return a
    smaller mask than the image we sent. Pasting that over full-resolution pixels misaligns the
    edge by a few pixels, which looks exactly like a bad cut-out. Resizing here means every
    engine is comparable and the rest of the pipeline can assume alignment.

    Uses INTER_AREA when shrinking and INTER_LINEAR when growing: a mask is a coverage field, so
    smooth interpolation is right and Lanczos ringing — which would push alpha outside [0, 1] —
    is not.
    """
    a = np.asarray(alpha, dtype=np.float32)
    if a.ndim != 2:
        raise ValueError(f"alpha must be 2-D, got shape {a.shape}")
    if (a.shape[1], a.shape[0]) == (width, height):
        return a

    interp = cv2.INTER_AREA if (width < a.shape[1] or height < a.shape[0]) else cv2.INTER_LINEAR
    resized = cv2.resize(a, (width, height), interpolation=interp)
    return np.clip(resized, 0.0, 1.0).astype(np.float32)


def alpha_from_rgba_png(data: bytes, width: int, height: int) -> np.ndarray:
    """Extract the alpha channel from a vendor's cut-out PNG, sized to the source.

    A fully-opaque result means the vendor found nothing to remove; that is not an error here —
    the auto-pick reject rules in `autopick.py` catch it, so the decision stays in one place.
    """
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        arr = np.asarray(img)

    return fit_alpha_to_source(arr[..., 3].astype(np.float32) / 255.0, width, height)

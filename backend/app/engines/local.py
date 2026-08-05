"""Offline control engine. No network, no key, no cost.

Purpose, and its limits: this exists so the full pipeline — zip in, processed assets out — is
runnable and testable before any vendor credential arrives, and so the bake-off has a free
baseline to measure the paid engines against. A paid API that cannot beat this on the client's
categories is a finding worth having.

It is **not** production quality and must never be the default when a key is available. It keys
on backdrop colour, so it works on a uniform studio sweep and fails on anything else.

Method
------
Solve the two-colour linear mixture per pixel. For a pixel that is part product and part backdrop
the camera saw::

    O = a*F + b*B

where ``F`` is the product's colour, ``B`` the backdrop's, ``a`` the coverage we want, and ``b``
a free scale on the backdrop. Two unknowns, three equations (one per channel), so least squares
gives ``a`` directly.

Letting ``b`` float rather than fixing it at ``1 - a`` is the whole trick, and it buys two things:

* **Shadows are ignored for free.** A shadowed backdrop is ``O = k*B`` with ``k < 1``, which
  solves to ``a = 0, b = k``. So the shadow stays classified as background — which matters
  enormously, because cutting it out would destroy the very thing the shadow-preservation stage
  exists to recover.
* **``a`` is proportional to coverage.** An earlier version scored chromaticity distance from the
  backdrop, which *saturates*: a pixel only 10% covered already maxed it out, so edge pixels came
  through at near-full opacity carrying backdrop spill that decontamination then (correctly)
  declined to touch. Measured at 608 over-claimed pixels on a 600px frame, visible as a pink rim.

Two degenerate cases, both handled explicitly:

* **Near-black product.** ``a*F`` contributes nothing for any ``a``, so coverage is invisible in
  this parametrisation. `alpha_from_backdrop_loss` infers it from how much backdrop the pixel lost
  instead. That measure cannot distinguish a black product from its own shadow — a fundamental
  ambiguity of colour keying — so it is gated to the detector's footprint.
* **Product colour parallel to the backdrop's**, i.e. white on white. Not separable by colour at
  all; the mask degrades to the detector's hard edge.

Both are among the reasons this engine is not production quality.
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from app.engines.base import AlphaResult, fit_alpha_to_source
from app.imaging import export as E
from app.imaging.shadow import luminance
from app.models import EngineId

# Border ring sampled for the backdrop estimate, as a fraction of the shorter edge.
_BORDER_FRACTION = 0.04

# Chromaticity distance at which the first pass calls a pixel product. A *detector* threshold
# only — coverage comes from the mixture solve, not from this.
_CHROMA_DETECT = 0.03

# A cast shadow on a sweep rarely falls below ~35% of the bare backdrop. Anything darker than
# this is taken to be a dark product rather than shadow. This is the heuristic that most limits
# the control engine: a genuinely black product against a hard shadow will confuse it.
_DARK_PRODUCT_RATIO = 0.25

# Brighter than the backdrop means product or specular highlight, never shadow.
_BRIGHT_RATIO = 1.12

# Dilation applied to the component gate so the solved edge ramp is not clipped flat.
_GATE_DILATE_PX = 5

# Below this, the 2x2 mixture system is too ill-conditioned to invert.
_DEGENERATE_DET = 1e-6


class LocalEngine:
    """Offline mixture-solving segmenter. See the module docstring for method and caveats."""

    id = EngineId.LOCAL
    cost_per_image_usd = 0.0

    def available(self) -> bool:
        return True

    async def alpha_for(self, image_bytes: bytes, width: int, height: int) -> AlphaResult:
        started = time.perf_counter()
        decoded = E.decode(image_bytes)
        alpha = key_on_backdrop(decoded.rgb_linear)
        return AlphaResult(
            alpha=fit_alpha_to_source(alpha, width, height),
            engine=self.id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=0.0,
        )


def _chromaticity(rgb_linear: np.ndarray) -> np.ndarray:
    """Normalised (r, g) chromaticity — intensity-invariant, so shadows look like backdrop."""
    total = np.maximum(rgb_linear.sum(axis=-1, keepdims=True), 1e-6)
    return (rgb_linear[..., :2] / total).astype(np.float32)


def estimate_backdrop(rgb_linear: np.ndarray) -> np.ndarray:
    """Median colour of a border ring, as a linear-light triple.

    The median, not the mean: a product that runs off the edge of the frame skews a mean but
    barely moves a median.
    """
    h, w = rgb_linear.shape[:2]
    band = max(2, int(min(h, w) * _BORDER_FRACTION))

    ring = np.concatenate(
        [
            rgb_linear[:band].reshape(-1, 3),
            rgb_linear[-band:].reshape(-1, 3),
            rgb_linear[:, :band].reshape(-1, 3),
            rgb_linear[:, -band:].reshape(-1, 3),
        ]
    )
    return np.median(ring, axis=0).astype(np.float32)


def _rough_core(rgb_linear: np.ndarray, backdrop: np.ndarray) -> np.ndarray:
    """First pass: find confidently-product pixels, to estimate the product colour from.

    Uses chromaticity distance, which is a poor *coverage* estimate but a perfectly good
    *detector*. Only the interior matters here, so saturation is harmless.
    """
    chroma = _chromaticity(rgb_linear)
    backdrop_chroma = _chromaticity(backdrop.reshape(1, 1, 3))[0, 0]
    chroma_dist = np.linalg.norm(chroma - backdrop_chroma, axis=-1)

    lum = luminance(rgb_linear)
    backdrop_lum = max(float(luminance(backdrop.reshape(1, 1, 3))[0, 0]), 1e-6)
    ratio = lum / backdrop_lum

    detected = (
        (chroma_dist > _CHROMA_DETECT)
        | (ratio < _DARK_PRODUCT_RATIO)
        | (ratio > _BRIGHT_RATIO)
    ).astype(np.uint8)

    detected = cv2.morphologyEx(
        detected, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    )
    if not detected.any():
        return detected

    count, labels, stats, _ = cv2.connectedComponentsWithStats(detected, connectivity=8)
    if count > 1:
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        detected = (labels == largest).astype(np.uint8)

    # Erode so the colour estimate comes from the interior, never from mixed edge pixels.
    return cv2.erode(
        detected, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)), iterations=1
    )


def solve_mixture_alpha(
    rgb_linear: np.ndarray, product: np.ndarray, backdrop: np.ndarray
) -> np.ndarray:
    """Least-squares coverage for ``O = a*F + b*B``, solved per pixel.

    The 2x2 normal equations have a closed form, so this is a handful of dot products over the
    whole image rather than a loop.
    """
    o = np.asarray(rgb_linear, dtype=np.float32)
    f = np.asarray(product, dtype=np.float32).reshape(3)
    b = np.asarray(backdrop, dtype=np.float32).reshape(3)

    ff = float(f @ f)
    bb = float(b @ b)
    fb = float(f @ b)
    det = ff * bb - fb * fb

    if abs(det) < _DEGENERATE_DET or ff < _DEGENERATE_DET:
        # Either F is parallel to B (white product on white) or F is near black. A black product
        # is the interesting case: a*F contributes nothing for any a, so coverage is invisible in
        # this parametrisation. Signalled to the caller rather than guessed at here.
        return np.zeros(o.shape[:2], dtype=np.float32)

    fo = o @ f
    bo = o @ b
    alpha = (bb * fo - fb * bo) / det
    return np.clip(alpha, 0.0, 1.0).astype(np.float32)


def alpha_from_backdrop_loss(rgb_linear: np.ndarray, backdrop: np.ndarray) -> np.ndarray:
    """Coverage inferred from how much backdrop a pixel *lost*, for near-black products.

    A black product occluding the backdrop gives ``O = (1 - a) * B``, so projecting onto ``B``
    recovers ``a`` directly and keeps the soft edge that the eroded-core fallback would throw away.

    Ambiguous by construction: a shadow is also "backdrop, reduced", so this cannot tell a black
    product from its own shadow. It is therefore only ever applied inside the detector's footprint
    — which separates them by the much cruder `_DARK_PRODUCT_RATIO` test. This ambiguity is
    fundamental to colour keying, and is a large part of why the paid engines exist.
    """
    o = np.asarray(rgb_linear, dtype=np.float32)
    b = np.asarray(backdrop, dtype=np.float32).reshape(3)
    bb = float(b @ b)
    if bb < _DEGENERATE_DET:
        return np.zeros(o.shape[:2], dtype=np.float32)
    return np.clip(1.0 - (o @ b) / bb, 0.0, 1.0).astype(np.float32)


def key_on_backdrop(rgb_linear: np.ndarray) -> np.ndarray:
    """Produce soft alpha proportional to product coverage, treating shadow as background."""
    rgb = np.asarray(rgb_linear, dtype=np.float32)
    backdrop = estimate_backdrop(rgb)

    core = _rough_core(rgb, backdrop)
    if not core.any():
        return np.zeros(rgb.shape[:2], dtype=np.float32)

    # Median of the eroded interior: robust to a highlight or a logo on the product.
    product = np.median(rgb[core > 0], axis=0).astype(np.float32)

    alpha = solve_mixture_alpha(rgb, product, backdrop)

    if not (alpha > 0.5).any():
        # Degenerate mixture — most often a near-black product. Infer coverage from lost backdrop
        # instead, gated to the detector's footprint so cast shadow is not swept up with it.
        gate = cv2.dilate(
            core, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_GATE_DILATE_PX * 3,) * 2)
        ).astype(np.float32)
        alpha = alpha_from_backdrop_loss(rgb, backdrop) * gate

    return _clean_up(alpha)


def _clean_up(score: np.ndarray) -> np.ndarray:
    """Gate out stray blobs and fill interior holes, **preserving the score's own edge ramp**.

    An earlier version forced everything inside the 0.5-contour to 1.0 and then Gaussian-blurred,
    which manufactured a soft edge spreading *outward* over pure backdrop — measured at 478
    invented-coverage pixels on a 600px test frame. Those pixels are unfixable downstream: they
    were pure backdrop, so un-mixing them recovers backdrop, and the result is a pale rim on any
    saturated background colour.

    The chromaticity score already varies smoothly across an antialiased edge, because coverage
    does. That ramp approximates true coverage, so the right thing is to keep it and only use the
    component mask to *gate* strays.
    """
    core = (score > 0.5).astype(np.uint8)
    if not core.any():
        return np.zeros(score.shape, dtype=np.float32)

    core = cv2.morphologyEx(
        core, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    )
    if not core.any():
        return np.zeros(score.shape, dtype=np.float32)

    count, labels, stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
    if count > 1:
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        core = (labels == largest).astype(np.uint8)

    # Fill enclosed holes: flood from the border, invert. A bottle's label should not become a
    # transparent window.
    flood = core.copy()
    mask = np.zeros((core.shape[0] + 2, core.shape[1] + 2), np.uint8)
    cv2.floodFill(flood, mask, (0, 0), 1)
    filled = (core | (1 - flood)).astype(np.uint8)

    # Gate with a slightly dilated mask so the score's natural outward ramp survives, rather than
    # being clipped flat at the 0.5 contour.
    gate = cv2.dilate(
        filled, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_GATE_DILATE_PX, _GATE_DILATE_PX))
    )

    kept = score * gate.astype(np.float32)
    # Interior holes that were filled have no score of their own, so give them full opacity.
    kept = np.maximum(kept, (filled & (1 - core)).astype(np.float32))
    return np.clip(kept, 0.0, 1.0).astype(np.float32)

"""Shadow and reflection preservation, and the background-plate estimate it depends on.

Pure functions. Stage 2 of the pipeline. **No AI** — this is the physically correct model for a
shadow cast on a uniform studio backdrop, and it is exact where the model holds.

The idea
--------
Segmentation APIs treat the shadow as background and throw it away, so a cut-out composited onto
a new colour floats with no contact with its surface. Rather than generating a plausible fake
shadow, we recover the real one.

For a pixel of bare backdrop the camera saw ``O = P``, where ``P`` is the backdrop's own
brightness at that point. Where the product blocks some light, it saw ``O = P * r`` with
``r < 1``. That ``r`` is a property of *the scene's lighting*, not of the backdrop's colour — so
multiplying a different backdrop colour by the same ``r`` reproduces the same shadow on it.

``r > 1`` is the same algebra for a specular reflection bouncing extra light onto the surface.

Where it fails
--------------
The model needs the original backdrop to be estimable, which means roughly uniform. On a
lifestyle shot the "backdrop" is a whole scene and ``P`` is meaningless. That is what the
uniformity gate detects; when it trips, the shadow is dropped and `Note.SHADOW_GATE_FAILED` is
emitted so the UI can say so rather than shipping a smear.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from threadpoolctl import threadpool_limits

# Rec.709 luminance weights, correct for linear-light sRGB primaries.
_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# Pixels this far below full opacity are treated as "not product" when sampling the backdrop.
_BG_ALPHA_MAX = 0.02

# The backdrop must occupy at least this share of the frame to be estimable at all.
_MIN_BG_FRACTION = 0.05

# The bare backdrop is the *bright* part of the background region; anything below this percentile
# is presumed to be shadow and excluded from the plate fit.
_CLEAN_PERCENTILE = 70.0

# Relative residual at which uniformity scores zero. A studio sweep sits near 0.01; a furnished
# room is far above it.
_UNIFORMITY_TOLERANCE = 0.05

# Gate threshold. Provisional — tune against data/input/shadow/ once real photographs land, and
# record the chosen value here.
UNIFORMITY_GATE = 0.50

# Ratio values within this of 1.0 are snapped to exactly 1.0. Without a deadband, sensor noise in
# the clean backdrop would modulate the replacement colour and add visible grain to what is
# supposed to be a flat fill.
_RATIO_DEADBAND = 0.02

# Ceiling on reconstructed reflection gain, to stop a blown highlight becoming a bright artefact.
_MAX_RATIO = 3.0

_SMOOTH_SIGMA = 1.0


@dataclass
class PlateEstimate:
    """The original backdrop, reconstructed across the whole frame."""

    plate: np.ndarray
    """(H, W, 3) linear-light estimate of the bare backdrop behind everything."""

    uniformity: float
    """0-1. Measured, and reported on every result whether or not the gate passed."""

    bg_fraction: float
    """Share of the frame that was usable backdrop."""

    @property
    def gate_passed(self) -> bool:
        return self.uniformity >= UNIFORMITY_GATE and self.bg_fraction >= _MIN_BG_FRACTION


@dataclass
class ShadowExtraction:
    ratio: np.ndarray | None
    """(H, W) multiplicative shading map, or None when the gate failed."""

    plate: PlateEstimate


def luminance(rgb_linear: np.ndarray) -> np.ndarray:
    """Linear-light luminance, shape (H, W)."""
    return (np.asarray(rgb_linear, dtype=np.float32) @ _LUMA).astype(np.float32)


def _polynomial_basis(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Second-order surface basis: ``[1, x, y, x^2, xy, y^2]``.

    Quadratic, not linear, and the difference is not academic. Lens vignetting and softbox
    falloff are both quadratic in radius. Fitting a plane to a real studio sweep leaves up to
    ~2.8% residual at the corners (measured), which exceeds the ratio deadband — so the corners
    get misread as shadow and the "flat" replacement background comes out a shade dark. A
    quadratic surface drops that residual below the deadband and the fill stays exact.
    """
    return np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=-1)


def estimate_background_plate(rgb_linear: np.ndarray, alpha: np.ndarray) -> PlateEstimate:
    """Reconstruct the bare backdrop, and score how backdrop-like it actually was.

    A quadratic surface is fitted per channel rather than taking a single mean colour, because
    real studio backdrops fall off toward the edges by a few percent. See `_polynomial_basis`
    for why a plane is not enough.

    Only the bright subset of the background region is fitted, since the dark part is the very
    shadow we are trying to measure — fitting through it would flatten the thing we want.
    """
    rgb = np.asarray(rgb_linear, dtype=np.float32)
    a = np.asarray(alpha, dtype=np.float32)
    h, w = a.shape

    # Erode the background region so the product's soft edge does not pollute the estimate.
    bg = (a <= _BG_ALPHA_MAX).astype(np.uint8)
    if bg.any():
        bg = cv2.erode(bg, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)), iterations=1)
    bg_bool = bg > 0
    bg_fraction = float(bg_bool.mean())

    if bg_fraction < _MIN_BG_FRACTION:
        # Product fills the frame: nothing to measure. Fall back to a flat mid-grey plate so
        # decontamination has something defined, and score zero so the gate refuses.
        return PlateEstimate(
            plate=np.full_like(rgb, 0.5), uniformity=0.0, bg_fraction=bg_fraction
        )

    lum = luminance(rgb)
    cutoff = float(np.percentile(lum[bg_bool], _CLEAN_PERCENTILE))
    clean = bg_bool & (lum >= cutoff)
    if clean.sum() < 32:                       # too few samples to fit; widen to all background
        clean = bg_bool

    ys, xs = np.nonzero(clean)
    # Centred, normalised coordinates keep the least-squares system well conditioned at any size.
    sx = (xs / max(w - 1, 1) - 0.5).astype(np.float32)
    sy = (ys / max(h - 1, 1) - 0.5).astype(np.float32)
    basis = _polynomial_basis(sx, sy)

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    full_basis = _polynomial_basis(
        (xx / max(w - 1, 1) - 0.5).astype(np.float32),
        (yy / max(h - 1, 1) - 0.5).astype(np.float32),
    )

    plate = np.empty_like(rgb)
    residuals = np.empty((xs.size, 3), dtype=np.float32)

    # threadpool_limits, not an env var: `lstsq` and the matmuls below go through BLAS/LAPACK,
    # whose thread count some builds do not reliably take from OMP/OPENBLAS_NUM_THREADS at this
    # point in the process (see app/__init__.py for the full story — that env-var approach alone
    # left this pipeline non-deterministic in roughly 2 of 10 full-suite runs under concurrent
    # load). threadpool_limits reaches into the *already-loaded* library and is unaffected by
    # when it was loaded, so it is applied here, at the actual point of use, rather than trusted
    # to have been set correctly once at startup.
    with threadpool_limits(limits=1):
        for ch in range(3):
            coeffs, *_ = np.linalg.lstsq(basis, rgb[..., ch][clean], rcond=None)
            plate[..., ch] = full_basis @ coeffs.astype(np.float32)
            residuals[:, ch] = rgb[..., ch][clean] - (basis @ coeffs)

    plate = np.clip(plate, 1e-4, 1.0)

    # Uniformity: median absolute residual relative to the plate's own level. Robust to the
    # handful of stray bright pixels a real photograph always has.
    level = float(np.median(luminance(plate)[clean])) + 1e-6
    rel_mad = float(np.median(np.abs(residuals))) / level
    uniformity = float(np.clip(1.0 - rel_mad / _UNIFORMITY_TOLERANCE, 0.0, 1.0))

    return PlateEstimate(plate=plate, uniformity=uniformity, bg_fraction=bg_fraction)


def extract_shadow_ratio(
    rgb_linear: np.ndarray,
    alpha: np.ndarray,
    plate: PlateEstimate,
) -> np.ndarray | None:
    """Recover the multiplicative shading map, or ``None`` if the gate refuses.

    The ratio is taken on **luminance**, not per channel. Per-channel would carry the original
    backdrop's hue into the new one — a shadow lit by cool skylight would tint a warm brand
    colour blue. Luminance keeps the shadow neutral, which is predictable and is what a
    catalogue wants; the cost is that genuinely coloured bounce light is not reproduced.
    """
    if not plate.gate_passed:
        return None

    rgb = np.asarray(rgb_linear, dtype=np.float32)
    a = np.asarray(alpha, dtype=np.float32)

    obs = luminance(rgb)
    ref = np.maximum(luminance(plate.plate), 1e-4)
    ratio = np.clip(obs / ref, 0.0, _MAX_RATIO).astype(np.float32)

    # The ratio is only measurable where the backdrop is actually visible.
    known = (a <= _BG_ALPHA_MAX).astype(np.float32)

    # Smooth by *normalised convolution* rather than a plain blur. A plain blur would treat the
    # product's footprint as if it held valid data, dragging shadow values inward and — worse —
    # pulling the contact shadow at the product's base toward 1.0, which is exactly the region
    # that sells the effect. Weighting by the known mask uses only real samples.
    num = cv2.GaussianBlur(ratio * known, ksize=(0, 0), sigmaX=_SMOOTH_SIGMA)
    den = cv2.GaussianBlur(known, ksize=(0, 0), sigmaX=_SMOOTH_SIGMA)
    ratio = np.where(den > 1e-3, num / np.maximum(den, 1e-6), 1.0).astype(np.float32)

    # Deadband: keep clean backdrop exactly flat so the replacement colour stays a flat colour.
    ratio[np.abs(ratio - 1.0) <= _RATIO_DEADBAND] = 1.0

    # Under the product the backdrop is hidden, so pin it to a no-op. Done after smoothing so the
    # pin cannot bleed outward into the shadow we just measured.
    ratio[a > _BG_ALPHA_MAX] = 1.0

    if bool(np.all(ratio == 1.0)):
        return None                            # nothing was there; no point carrying a no-op map

    return ratio.astype(np.float32)


def extract(rgb_linear: np.ndarray, alpha: np.ndarray) -> ShadowExtraction:
    """Convenience wrapper: estimate the plate, then the ratio map."""
    plate = estimate_background_plate(rgb_linear, alpha)
    return ShadowExtraction(ratio=extract_shadow_ratio(rgb_linear, alpha, plate), plate=plate)

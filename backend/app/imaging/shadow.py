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

# Upper bound on the pixels fed to the plate fit. The surface has six free parameters, so the
# coefficients are pinned long before this; past it the solve only gets slower. Raising it does
# not improve the plate measurably, and lowering it below ~10k starts to show on noisy sweeps.
_MAX_PLATE_SAMPLES = 250_000

# Relative residual at which uniformity scores zero. A studio sweep sits near 0.01; a furnished
# room is far above it.
_UNIFORMITY_TOLERANCE = 0.05

# Gate threshold. Left at 0.50 deliberately, and the reasoning is worth recording because the
# obvious change here is the wrong one.
#
# Measured 6 Aug 2026:
#
#     synthetic studio sweep, real cast shadow   0.9590
#     man photographed against a panelled wall   0.6643   <- produced a real, reported bug
#     synthetic busy scene                       0.0000
#
# Raising the gate to ~0.85 would refuse the wall case, and was tried. It was reverted: there is no
# measurement of what a *real* studio photograph scores — `data/input/` holds no packshots — and real
# sensor noise, vignetting and sweep seams will all score below a clean synthetic fixture. Raising
# this on one data point risks silently disabling a working feature on exactly the images it is for.
#
# The wall case is caught by `_MAX_SHADOW_COVERAGE` instead, which measures the actual failure rather
# than a proxy for it. This gate keeps its original job: refusing a backdrop too non-uniform for the
# plate fit to mean anything at all.
UNIFORMITY_GATE = 0.50

# The guard that actually catches the reported bug: how much of the visible backdrop the ratio map is
# allowed to modulate.
#
# A man photographed against a near-white wall produced a *correct* silhouette from remove.bg, yet
# the delivered asset showed the original wall — so it read as "background removal is not working".
# It was working. The wall carried panel seams, a luminance gradient and a dark tree in one corner;
# uniformity scored 0.6643 and passed the gate, but the plate estimator fits a **quadratic surface**
# and cannot represent any of that structure. So the residual was not shadow, it was the wall — and
# `flatten` multiplied it back onto the replacement colour, faithfully repainting the backdrop.
#
# Uniformity scores the plate *fit*. This scores the *residual*, which is the thing that gets
# painted, and it separates the cases far more cleanly:
#
#     synthetic studio sweep, real cast shadow   modulates 11.3% of the backdrop
#     man photographed against a panelled wall   modulates 75.4% of the backdrop
#
# A cast shadow is *local* — it sits near the product's contact point. Scene texture mistaken for
# shadow is *global*. A map darkening most of the frame is asserting "the entire backdrop is in
# shadow", which is not a shadow but a copy of the backdrop.
#
# The failure is asymmetric, so this errs toward refusing: a *missed* shadow gives a flat background
# and says so via SHADOW_GATE_FAILED, which is mildly disappointing. A *false* shadow reproduces the
# whole original backdrop and looks exactly like the core feature is broken.
_MAX_SHADOW_COVERAGE = 0.35

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


def _evaluate_surface(coeffs: np.ndarray, h: int, w: int) -> np.ndarray:
    """Evaluate the fitted quadratic over a full ``(h, w, 3)`` grid.

    The basis is separable apart from the ``xy`` term, so this needs one full-size temporary
    rather than six. Building the ``(h, w, 6)`` basis and contracting it against the
    coefficients — the obvious way, and what this replaced — allocates 1.2 GB on a 50 MP frame
    and was the second-largest cost in stage 2 after the fit itself.
    """
    c = np.asarray(coeffs, dtype=np.float32)          # (6, 3), ordered as `_polynomial_basis`
    x = (np.arange(w, dtype=np.float32) / max(w - 1, 1) - 0.5).reshape(1, w)
    y = (np.arange(h, dtype=np.float32) / max(h - 1, 1) - 0.5).reshape(h, 1)
    xy = y * x                                        # the one unavoidable (h, w) temporary

    out = np.empty((h, w, 3), dtype=np.float32)
    for ch in range(3):
        c0, c1, c2, c3, c4, c5 = (float(v) for v in c[:, ch])
        row = c0 + c1 * x + c3 * (x * x)              # (1, w)
        col = c2 * y + c5 * (y * y)                   # (h, 1)
        out[..., ch] = row + col + c4 * xy
    return out


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

    # Fit on a bounded, evenly strided subset of the clean pixels rather than all of them. The
    # surface has six free parameters, so a quarter of a million well-spread samples pin it as
    # tightly as fifty million do, and the cost stops scaling with sensor size. Fitting the full
    # set was the single largest cost in the whole pipeline — a 50 MP frame spent 7.7 s inside
    # `lstsq` alone, on top of a 480 MB basis matrix. The stride is fixed and derived only from
    # the sample count, never randomised, so determinism is unaffected.
    flat_idx = np.nonzero(clean.ravel())[0]
    if flat_idx.size > _MAX_PLATE_SAMPLES:
        # linspace, not a `[::step]` slice. A slice with an integer step overshoots whenever the
        # count is not a clean multiple and the trailing truncation then keeps only a prefix —
        # which in raster order is the *top* of the frame, exactly the bias a vignette fit must
        # not have. linspace spreads the samples over the whole set at any ratio.
        picks = np.linspace(0, flat_idx.size - 1, _MAX_PLATE_SAMPLES).astype(np.int64)
        flat_idx = flat_idx[picks]

    # Centred, normalised coordinates keep the least-squares system well conditioned at any size.
    sx = ((flat_idx % w) / max(w - 1, 1) - 0.5).astype(np.float32)
    sy = ((flat_idx // w) / max(h - 1, 1) - 0.5).astype(np.float32)
    basis = _polynomial_basis(sx, sy)
    values = rgb.reshape(-1, 3)[flat_idx]

    # threadpool_limits, not an env var: `lstsq` and the matmuls below go through BLAS/LAPACK,
    # whose thread count some builds do not reliably take from OMP/OPENBLAS_NUM_THREADS at this
    # point in the process (see app/__init__.py for the full story — that env-var approach alone
    # left this pipeline non-deterministic in roughly 2 of 10 full-suite runs under concurrent
    # load). threadpool_limits reaches into the *already-loaded* library and is unaffected by
    # when it was loaded, so it is applied here, at the actual point of use, rather than trusted
    # to have been set correctly once at startup.
    with threadpool_limits(limits=1):
        # One solve for all three channels. `lstsq` accepts an (N, 3) right-hand side and the
        # expensive part is factorising `basis`, which is shared — three separate calls did that
        # work three times over.
        coeffs, *_ = np.linalg.lstsq(basis, values, rcond=None)
        fitted = basis @ coeffs

    residuals = values - fitted
    plate = np.clip(_evaluate_surface(coeffs, h, w), 1e-4, 1.0)

    # Uniformity: median absolute residual relative to the plate's own level. Robust to the
    # handful of stray bright pixels a real photograph always has. Taken at the fitted sample
    # points, which is the same population the residuals come from.
    level = float(np.median(luminance(np.clip(fitted, 1e-4, 1.0)))) + 1e-6
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

    # Coverage guard — see `_MAX_SHADOW_COVERAGE`. Checked on the *visible backdrop only*: the
    # product's own footprint was pinned to 1.0 above, so including it would dilute the measure by
    # however much of the frame the product happens to occupy, making the threshold depend on
    # framing rather than on whether this is a shadow.
    visible = a <= _BG_ALPHA_MAX
    if visible.any():
        coverage = float((np.abs(ratio - 1.0) > _RATIO_DEADBAND)[visible].mean())
        if coverage > _MAX_SHADOW_COVERAGE:
            return None

    return ratio.astype(np.float32)


def extract(rgb_linear: np.ndarray, alpha: np.ndarray) -> ShadowExtraction:
    """Convenience wrapper: estimate the plate, then the ratio map."""
    plate = estimate_background_plate(rgb_linear, alpha)
    return ShadowExtraction(ratio=extract_shadow_ratio(rgb_linear, alpha, plate), plate=plate)

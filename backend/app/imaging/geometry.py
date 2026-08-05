"""Geometry: measure the product, scale it, place it on an exact canvas.

Pure functions. Stage 3 of the pipeline, and it runs **before** the background fill — resizing
after filling would resample the product against the flat colour and bake edge halos in
permanently. See `docs/ARCHITECTURE.md`.

Everything here is deterministic measurement: contours and bounding boxes from the alpha
channel. There is no model involved, and there must not be — "centre the image" has an exact
answer, and the client report puts crop/alignment in its *"No - measured"* column.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from app.models import CentringMode, CentringSpec, FitMode, Note, SizeSpec

# Mild unsharp after a significant downscale. Downsampling always costs acuity; recovering a
# little is standard practice for packshots. Kept conservative on purpose — this touches product
# pixels, so it is deliberately too weak to invent detail.
_SHARPEN_BELOW_SCALE = 0.9
_SHARPEN_AMOUNT = 0.35
_SHARPEN_RADIUS = 1.0

# Below this, cv2's Lanczos aliases: INTER_LANCZOS4 has no prefilter, so a 5x reduction samples
# a sparse subset of source pixels. Step down with area-averaging first.
_AREA_STEP_THRESHOLD = 0.5


@dataclass(frozen=True)
class BBox:
    """Half-open box: x1 and y1 are exclusive."""

    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0

    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0


@dataclass
class Placement:
    """Output of the geometry stage: canvas-sized layers, ready for the background fill."""

    rgb: np.ndarray
    """(H, W, 3) linear-light, straight (un-premultiplied) alpha."""

    alpha: np.ndarray
    """(H, W) float32 in [0, 1]. Soft throughout — never thresholded."""

    shadow_ratio: np.ndarray | None
    """(H, W) float32 multiplicative map aligned to the canvas, or None."""

    scale: float
    centroid_offset_px: tuple[float, float]
    """Measured product centroid minus canvas centre, after placement."""

    notes: list[Note] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def alpha_bbox(alpha: np.ndarray, threshold: float = 0.05) -> BBox:
    """Tightest box containing all pixels with ``alpha > threshold``.

    `threshold` affects *measurement only*. The stored alpha is never thresholded — doing so
    would destroy soft edges irrecoverably.

    Returns an empty box for a fully transparent input; callers decide whether that is an error.
    """
    a = np.asarray(alpha, dtype=np.float32)
    if a.ndim != 2:
        raise ValueError(f"alpha must be 2-D, got shape {a.shape}")

    mask = a > threshold
    if not mask.any():
        return BBox(0, 0, 0, 0)

    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    return BBox(int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)


def alpha_centroid(alpha: np.ndarray) -> tuple[float, float] | None:
    """Alpha-weighted centre of mass, ``(cx, cy)`` in pixel coordinates.

    Differs from the bbox centre for asymmetric products — a mug with a handle sits off-centre
    by centroid but centred by bbox. We report both so the difference is visible rather than
    argued about.
    """
    a = np.asarray(alpha, dtype=np.float64)
    total = float(a.sum())
    if total <= 0.0:
        return None

    h, w = a.shape
    ys = np.arange(h, dtype=np.float64)
    xs = np.arange(w, dtype=np.float64)
    cy = float((a.sum(axis=1) * ys).sum() / total)
    cx = float((a.sum(axis=0) * xs).sum() / total)
    return cx + 0.5, cy + 0.5     # pixel centres, not corners


def shadow_bbox(shadow_ratio: np.ndarray, tolerance: float = 0.02) -> BBox:
    """Box containing pixels the shadow map actually darkens or brightens."""
    r = np.asarray(shadow_ratio, dtype=np.float32)
    mask = np.abs(r - 1.0) > tolerance
    if not mask.any():
        return BBox(0, 0, 0, 0)
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    return BBox(int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)


def union_bbox(a: BBox, b: BBox) -> BBox:
    if a.is_empty():
        return b
    if b.is_empty():
        return a
    return BBox(min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1))


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------


def resample_rgba(
    rgb_linear: np.ndarray,
    alpha: np.ndarray,
    out_w: int,
    out_h: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Resample colour and alpha together, correctly.

    Two things make this different from a naive ``cv2.resize`` on an RGBA array:

    1. **Premultiply first.** Resampling straight-alpha RGBA lets the colour of fully
       transparent pixels bleed into the edge — transparent black bleeding in reads as a dark
       rim. Premultiplied alpha is the only interpolation-safe representation.
    2. **Step down with area-averaging when the reduction is large.** ``INTER_LANCZOS4`` has no
       prefilter, so a 5x reduction point-samples and aliases. Halving with ``INTER_AREA``
       first, then a final Lanczos step, gives a clean, sharp result.

    Input is expected to be **linear light**; resampling gamma-encoded values darkens edges.
    """
    rgb = np.asarray(rgb_linear, dtype=np.float32)
    a = np.asarray(alpha, dtype=np.float32)
    if rgb.shape[:2] != a.shape:
        raise ValueError(f"rgb {rgb.shape[:2]} and alpha {a.shape} disagree")
    if out_w < 1 or out_h < 1:
        raise ValueError(f"target must be at least 1x1, got {out_w}x{out_h}")

    prem = rgb * a[..., None]
    stack = np.dstack([prem, a])          # (H, W, 4), premultiplied

    src_h, src_w = a.shape
    while (out_w / stack.shape[1]) < _AREA_STEP_THRESHOLD and stack.shape[1] > 2 * out_w:
        stack = cv2.resize(
            stack,
            (max(out_w, stack.shape[1] // 2), max(out_h, stack.shape[0] // 2)),
            interpolation=cv2.INTER_AREA,
        )

    if (stack.shape[1], stack.shape[0]) != (out_w, out_h):
        shrinking = out_w < stack.shape[1] or out_h < stack.shape[0]
        interp = cv2.INTER_LANCZOS4 if shrinking else cv2.INTER_CUBIC
        stack = cv2.resize(stack, (out_w, out_h), interpolation=interp)

    prem_out = np.clip(stack[..., :3], 0.0, None)
    a_out = np.clip(stack[..., 3], 0.0, 1.0)

    # Un-premultiply. Where alpha is ~0 the colour is meaningless, so leave it at zero rather
    # than dividing and amplifying resampling noise into visible speckle.
    safe = a_out > 1e-6
    rgb_out = np.zeros_like(prem_out)
    np.divide(prem_out, a_out[..., None], out=rgb_out, where=safe[..., None])
    rgb_out = np.clip(rgb_out, 0.0, 1.0)

    scale = (out_w / src_w + out_h / src_h) / 2.0
    if scale < _SHARPEN_BELOW_SCALE:
        rgb_out = _unsharp(rgb_out, a_out)

    return rgb_out.astype(np.float32), a_out.astype(np.float32)


def resample_scalar(field_map: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    """Resample a single-channel map (e.g. the shadow ratio) with the same policy."""
    m = np.asarray(field_map, dtype=np.float32)
    while (out_w / m.shape[1]) < _AREA_STEP_THRESHOLD and m.shape[1] > 2 * out_w:
        m = cv2.resize(
            m,
            (max(out_w, m.shape[1] // 2), max(out_h, m.shape[0] // 2)),
            interpolation=cv2.INTER_AREA,
        )
    if (m.shape[1], m.shape[0]) != (out_w, out_h):
        shrinking = out_w < m.shape[1] or out_h < m.shape[0]
        interp = cv2.INTER_LANCZOS4 if shrinking else cv2.INTER_CUBIC
        m = cv2.resize(m, (out_w, out_h), interpolation=interp)
    return m.astype(np.float32)


def _unsharp(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Mild unsharp mask, weighted by alpha so it does not chew the cut-out edge.

    Sharpening across an alpha boundary would carve a halo — exactly the artefact the rest of
    this module works to avoid — so the effect is faded out where alpha is not solid.
    """
    blurred = cv2.GaussianBlur(rgb, ksize=(0, 0), sigmaX=_SHARPEN_RADIUS)
    sharp = rgb + _SHARPEN_AMOUNT * (rgb - blurred)
    w = (alpha**2)[..., None]             # squared: only near-opaque interior gets the full effect
    return np.clip(rgb * (1.0 - w) + sharp * w, 0.0, 1.0).astype(np.float32)


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def place_on_canvas(
    rgb_linear: np.ndarray,
    alpha: np.ndarray,
    size: SizeSpec,
    centring: CentringSpec,
    shadow_ratio: np.ndarray | None = None,
) -> Placement:
    """Scale the product and centre it on an exact ``size.width`` x ``size.height`` canvas.

    The output is guaranteed to be exactly the requested dimensions — asserted before return,
    because "output 500x500 px" is a literal requirement.

    Placement offsets are integers. Sub-pixel positioning would mean a second resample and a
    second dose of softness for at most half a pixel of accuracy; instead the residual is
    measured and reported in `Placement.centroid_offset_px`, so the error is visible rather
    than hidden.
    """
    rgb = np.asarray(rgb_linear, dtype=np.float32)
    a = np.asarray(alpha, dtype=np.float32)
    notes: list[Note] = []

    out_w, out_h = size.width, size.height

    bounds = alpha_bbox(a, centring.alpha_threshold)
    if shadow_ratio is not None and centring.include_shadow_in_bounds:
        bounds = union_bbox(bounds, shadow_bbox(shadow_ratio))

    if bounds.is_empty():
        # Nothing to centre. Return an empty canvas rather than dividing by zero; the caller
        # surfaces this via the engine's reject rules, which will have flagged it already.
        return Placement(
            rgb=np.zeros((out_h, out_w, 3), dtype=np.float32),
            alpha=np.zeros((out_h, out_w), dtype=np.float32),
            shadow_ratio=None,
            scale=1.0,
            centroid_offset_px=(0.0, 0.0),
            notes=notes,
        )

    scale = _compute_scale(bounds, size, notes)
    scaled_w = max(1, int(round(a.shape[1] * scale)))
    scaled_h = max(1, int(round(a.shape[0] * scale)))

    rgb_s, a_s = resample_rgba(rgb, a, scaled_w, scaled_h)
    ratio_s = resample_scalar(shadow_ratio, scaled_w, scaled_h) if shadow_ratio is not None else None

    # Anchor: the point in the scaled image that should land on the canvas centre.
    scaled_bounds = alpha_bbox(a_s, centring.alpha_threshold)
    if shadow_ratio is not None and centring.include_shadow_in_bounds and ratio_s is not None:
        scaled_bounds = union_bbox(scaled_bounds, shadow_bbox(ratio_s))

    if centring.mode is CentringMode.CENTROID:
        anchor = alpha_centroid(a_s) or scaled_bounds.centre
    else:
        anchor = scaled_bounds.centre

    dx = int(round(out_w / 2.0 - anchor[0]))
    dy = int(round(out_h / 2.0 - anchor[1]))

    canvas_rgb = np.zeros((out_h, out_w, 3), dtype=np.float32)
    canvas_a = np.zeros((out_h, out_w), dtype=np.float32)
    canvas_ratio = np.ones((out_h, out_w), dtype=np.float32) if ratio_s is not None else None

    src, dst = _overlap_windows(scaled_w, scaled_h, out_w, out_h, dx, dy)
    if src is not None and dst is not None:
        sx0, sy0, sx1, sy1 = src
        dx0, dy0, dx1, dy1 = dst
        canvas_rgb[dy0:dy1, dx0:dx1] = rgb_s[sy0:sy1, sx0:sx1]
        canvas_a[dy0:dy1, dx0:dx1] = a_s[sy0:sy1, sx0:sx1]
        if canvas_ratio is not None and ratio_s is not None:
            canvas_ratio[dy0:dy1, dx0:dx1] = ratio_s[sy0:sy1, sx0:sx1]

        if size.fit is FitMode.COVER and _lost_product(a_s, src, centring.alpha_threshold):
            notes.append(Note.COVER_CROPPED)

    centre = alpha_centroid(canvas_a)
    offset = (
        (round(centre[0] - out_w / 2.0, 3), round(centre[1] - out_h / 2.0, 3))
        if centre is not None
        else (0.0, 0.0)
    )

    assert canvas_rgb.shape == (out_h, out_w, 3), "canvas dimensions must be exact"
    assert canvas_a.shape == (out_h, out_w), "canvas dimensions must be exact"

    return Placement(
        rgb=canvas_rgb,
        alpha=canvas_a,
        shadow_ratio=canvas_ratio,
        scale=scale,
        centroid_offset_px=offset,
        notes=notes,
    )


def _compute_scale(bounds: BBox, size: SizeSpec, notes: list[Note]) -> float:
    """Scale factor that fits `bounds` into the canvas, honouring margin and the upscale ban."""
    inset = 1.0 - 2.0 * (size.margin_pct / 100.0)

    if size.fit is FitMode.COVER:
        # COVER fills the canvas and crops; an inset margin would contradict that, so margin is
        # ignored here by design.
        scale = max(size.width / bounds.width, size.height / bounds.height)
    else:
        avail_w = size.width * inset
        avail_h = size.height * inset
        scale = min(avail_w / bounds.width, avail_h / bounds.height)

    if scale > 1.0 and not size.allow_upscale:
        # Lanczos-upscaling a master looks worse than padding it. When the caller has not opted
        # in, stay at 1:1 and say so.
        notes.append(Note.UPSCALE_SKIPPED)
        scale = 1.0

    return float(scale)


def _overlap_windows(
    src_w: int, src_h: int, dst_w: int, dst_h: int, dx: int, dy: int
) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]:
    """Intersect a source rect offset by (dx, dy) with the destination canvas.

    Handles the COVER case where the scaled product is larger than the canvas, and the case
    where it falls entirely outside.
    """
    dx0, dy0 = max(0, dx), max(0, dy)
    dx1, dy1 = min(dst_w, dx + src_w), min(dst_h, dy + src_h)
    if dx0 >= dx1 or dy0 >= dy1:
        return None, None

    sx0, sy0 = dx0 - dx, dy0 - dy
    return (sx0, sy0, sx0 + (dx1 - dx0), sy0 + (dy1 - dy0)), (dx0, dy0, dx1, dy1)


def _lost_product(alpha_scaled: np.ndarray, src_window: tuple[int, int, int, int], thr: float) -> bool:
    """Did cropping discard product pixels?"""
    sx0, sy0, sx1, sy1 = src_window
    total = float((alpha_scaled > thr).sum())
    if total <= 0:
        return False
    kept = float((alpha_scaled[sy0:sy1, sx0:sx1] > thr).sum())
    return kept < total - 0.5

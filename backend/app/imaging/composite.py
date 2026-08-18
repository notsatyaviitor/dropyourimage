"""Compositing and edge decontamination.

Pure functions. Stage 4 of the pipeline — it runs *after* geometry so the flat background colour
is never resampled.

Two things here are the difference between a professional cut-out and an obviously-automated
one:

1. **Compositing in linear light.** The alpha `over` operator is only physically meaningful on
   linear-light values. Applied to gamma-encoded numbers it under-weights the darker
   contributor, which reads as a thin dark rim around every edge.
2. **Edge decontamination.** A product photographed on white keeps white in its
   partially-transparent edge pixels. Composite that onto navy and the white comes with it as a
   halo. The fix is to un-mix it, which is algebra, not guesswork.
"""

from __future__ import annotations

import cv2
import numpy as np

# Below this alpha the un-mixing is too ill-conditioned to trust: dividing by a small alpha
# amplifies 8-bit quantisation (1/255) into visible speckle. The correction is faded in across
# this range rather than switching on abruptly.
#
# _FULL was originally 0.60, which was too cautious — it withheld correction over precisely the
# 0.25-0.60 band where backdrop spill is heaviest, leaving a measurable pale rim on saturated
# backgrounds. At 0.25 the worst-case amplification is 4x quantisation (~0.016), well below
# visibility, and the clamp below bounds the result regardless.
_DECONTAM_ALPHA_FLOOR = 0.08
_DECONTAM_ALPHA_FULL = 0.25

# Only pixels within this many pixels of the alpha edge are candidates. The interior of a solid
# product must never be touched, whatever the maths says.
_EDGE_BAND_PX = 3


def composite_over(
    fg_linear: np.ndarray,
    alpha: np.ndarray,
    bg_linear: np.ndarray,
) -> np.ndarray:
    """Porter-Duff `over` in linear light: ``fg*a + bg*(1-a)``.

    All three inputs must be linear-light. Passing gamma-encoded values produces a subtly dark
    edge on every cut-out — the single most common compositing bug.
    """
    fg = np.asarray(fg_linear, dtype=np.float32)
    bg = np.asarray(bg_linear, dtype=np.float32)
    a = np.asarray(alpha, dtype=np.float32)[..., None]

    if fg.shape != bg.shape:
        raise ValueError(f"foreground {fg.shape} and background {bg.shape} disagree")
    if a.shape[:2] != fg.shape[:2]:
        raise ValueError(f"alpha {a.shape[:2]} and colour {fg.shape[:2]} disagree")

    return (fg * a + bg * (1.0 - a)).astype(np.float32)


def solid_background(height: int, width: int, colour_linear: np.ndarray) -> np.ndarray:
    """A flat canvas of one linear-light colour, shape (H, W, 3)."""
    c = np.asarray(colour_linear, dtype=np.float32).reshape(3)
    return np.broadcast_to(c, (height, width, 3)).astype(np.float32).copy()


def apply_shadow_ratio(bg_linear: np.ndarray, ratio: np.ndarray) -> np.ndarray:
    """Modulate a background by a multiplicative shading map.

    This is what carries the photographed shadow onto the new colour. A ratio of 0.6 means "this
    pixel received 60% of the light the bare backdrop did", which is a property of the scene, not
    of the backdrop — so it transfers correctly to any replacement colour.

    Values above 1.0 are specular reflection. They are allowed through and then clipped at white:
    a blown highlight on the original cannot be reconstructed, and pretending otherwise would
    invent data. See docs/LIMITATIONS.md.
    """
    bg = np.asarray(bg_linear, dtype=np.float32)
    r = np.asarray(ratio, dtype=np.float32)
    if r.shape != bg.shape[:2]:
        raise ValueError(f"ratio {r.shape} and background {bg.shape[:2]} disagree")
    return np.clip(bg * r[..., None], 0.0, 1.0).astype(np.float32)


def edge_band_mask(alpha: np.ndarray, band_px: int = _EDGE_BAND_PX) -> np.ndarray:
    """Boolean mask of pixels near the alpha boundary.

    Anything not partially transparent and not adjacent to a partially transparent pixel is
    interior, and interior pixels are never modified by decontamination.
    """
    a = np.asarray(alpha, dtype=np.float32)
    partial = ((a > 0.001) & (a < 0.999)).astype(np.uint8)
    if not partial.any():
        return np.zeros(a.shape, dtype=bool)

    k = 2 * int(band_px) + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    grown = cv2.dilate(partial, kernel, iterations=1)
    return (grown > 0) & (a > 0.001)


def decontaminate_edges(
    rgb_linear: np.ndarray,
    alpha: np.ndarray,
    background_plate_linear: np.ndarray,
    band_px: int = _EDGE_BAND_PX,
) -> np.ndarray:
    """Remove residual original-background colour from partially transparent edge pixels.

    The observation model for a pixel the camera saw at the product's edge is::

        O = a*F + (1 - a)*B

    where ``O`` is what was photographed, ``B`` the original backdrop behind that pixel, ``F``
    the product's true colour and ``a`` its coverage. Vendors return ``O`` and ``a``; ``B`` is
    estimated from the plate. So ``F`` is recoverable::

        F = (O - (1 - a)*B) / a

    Without this step a product cut from a white studio background carries white in its edge
    and shows a bright halo once composited onto a saturated colour. With it, the edge is the
    product's own colour and composites correctly against anything.

    The correction is faded in between `_DECONTAM_ALPHA_FLOOR` and `_DECONTAM_ALPHA_FULL`
    because the division is ill-conditioned at low alpha, and it is confined to the edge band so
    a solid interior is never altered.
    """
    o = np.asarray(rgb_linear, dtype=np.float32)
    a = np.asarray(alpha, dtype=np.float32)
    b = np.asarray(background_plate_linear, dtype=np.float32)

    if o.shape[:2] != a.shape:
        raise ValueError(f"colour {o.shape[:2]} and alpha {a.shape} disagree")
    if b.ndim == 1:
        b = np.broadcast_to(b.reshape(3), o.shape).astype(np.float32)
    elif b.shape != o.shape:
        raise ValueError(f"plate {b.shape} and colour {o.shape} disagree")

    band = edge_band_mask(a, band_px)
    if not band.any():
        return o.copy()

    # Gather the band and work only on it. The blend below is the identity wherever the weight is
    # zero, and the weight is zero everywhere outside the band, so this is exact rather than an
    # approximation. It matters because the band is a thin rim — well under 1% of a packshot —
    # while the full-frame version paid for a divide, a clip and a lerp over every pixel of a
    # 50 MP master.
    out = o.copy()
    idx = np.nonzero(band.ravel())[0]
    o_b = o.reshape(-1, 3)[idx]
    a_b = a.reshape(-1)[idx][:, None]
    b_b = b.reshape(-1, 3)[idx]

    safe = np.maximum(a_b, _DECONTAM_ALPHA_FLOOR)
    unmixed = np.clip((o_b - (1.0 - a_b) * b_b) / safe, 0.0, 1.0)

    # Confidence ramp: 0 at the floor, 1 at full. Below the floor we keep the observation, which
    # is wrong but stable; guessing there produces speckle that looks worse than a faint halo.
    t = np.clip(
        (a_b - _DECONTAM_ALPHA_FLOOR) / (_DECONTAM_ALPHA_FULL - _DECONTAM_ALPHA_FLOOR), 0.0, 1.0
    )

    out.reshape(-1, 3)[idx] = (o_b * (1.0 - t) + unmixed * t).astype(np.float32)
    return out


def flatten(
    rgb_linear: np.ndarray,
    alpha: np.ndarray,
    *,
    background_linear: np.ndarray | None,
    shadow_ratio: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Produce the final layer: composite onto a colour, or pass transparency through.

    Returns ``(rgb_linear, alpha)``. When a background colour is supplied the returned alpha is
    fully opaque; when it is ``None`` the product's own alpha is preserved for PNG output.

    The shadow map is applied to the *background* before compositing, which is why a preserved
    shadow reads correctly on any replacement colour instead of being a grey smear baked in at
    cut-out time.
    """
    fg = np.asarray(rgb_linear, dtype=np.float32)
    a = np.asarray(alpha, dtype=np.float32)
    h, w = a.shape

    if background_linear is None:
        # Transparent output. A shadow has nothing to modulate, so it is dropped here rather
        # than silently multiplied into the product's colour.
        return fg.copy(), a.copy()

    bg = solid_background(h, w, background_linear)
    if shadow_ratio is not None:
        bg = apply_shadow_ratio(bg, shadow_ratio)

    out = composite_over(fg, a, bg)
    return out, np.ones((h, w), dtype=np.float32)

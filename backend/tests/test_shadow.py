"""Shadow preservation tests: plate estimation, the uniformity gate, ratio recovery."""

from __future__ import annotations

import numpy as np
import pytest

from app.imaging import color as C
from app.imaging import composite as X
from app.imaging import shadow as S


# ---------------------------------------------------------------------------
# Synthetic scenes
# ---------------------------------------------------------------------------


def studio_scene(size: int = 200, with_shadow: bool = True, falloff: float = 0.03):
    """A studio sweep with lighting falloff, a product, and optionally its shadow.

    Returns ``(observed, alpha, true_ratio)`` where `observed` is what the camera saw and
    `alpha` is what a segmentation API returns for the product.
    """
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)

    encoded = 0.97 - falloff * ((xx / size - 0.5) ** 2 + (yy / size - 0.5) ** 2)
    backdrop = np.dstack([C.srgb_decode(encoded.astype(np.float32))] * 3)

    if with_shadow:
        d = ((xx - size / 2) / (size * 0.23)) ** 2 + ((yy - size * 0.68) / (size * 0.09)) ** 2
        true_ratio = np.clip(1.0 - 0.45 * np.exp(-d * 1.6), 0.0, 1.0).astype(np.float32)
    else:
        true_ratio = np.ones((size, size), dtype=np.float32)

    dist = np.sqrt((xx - size / 2) ** 2 + (yy - size * 0.47) ** 2)
    alpha = np.clip(size * 0.2 - dist + 0.5, 0.0, 1.0).astype(np.float32)

    product = np.zeros((size, size, 3), dtype=np.float32)
    product[..., 0] = C.srgb_decode(np.float32(0.78))

    observed = X.composite_over(product, alpha, backdrop * true_ratio[..., None])
    return observed, alpha, true_ratio


def busy_scene(size: int = 200):
    """A lifestyle shot: textured, high-variance background. The gate must refuse this."""
    rng = np.random.default_rng(1234)          # seeded: tests must be deterministic
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)

    bg = 0.5 + 0.35 * np.sin(xx / 7.0) * np.cos(yy / 11.0)
    bg = bg + rng.normal(0, 0.08, size=(size, size))
    backdrop = np.dstack([C.srgb_decode(np.clip(bg, 0, 1).astype(np.float32))] * 3)

    dist = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
    alpha = np.clip(size * 0.18 - dist + 0.5, 0.0, 1.0).astype(np.float32)
    product = np.full((size, size, 3), 0.2, dtype=np.float32)

    return X.composite_over(product, alpha, backdrop), alpha


# ---------------------------------------------------------------------------
# Plate estimation
# ---------------------------------------------------------------------------


class TestPlateEstimation:
    def test_studio_sweep_scores_high_and_passes_the_gate(self):
        observed, alpha, _ = studio_scene()
        plate = S.estimate_background_plate(observed, alpha)
        assert plate.uniformity > 0.8, f"uniformity {plate.uniformity:.3f}"
        assert plate.gate_passed

    def test_plate_tracks_lighting_falloff_rather_than_calling_it_shadow(self):
        """A constant plate would misread a few percent of vignette as shadow and darken the
        corners of the replacement background. A fitted plane absorbs it."""
        observed, alpha, _ = studio_scene(falloff=0.06, with_shadow=False)
        plate = S.estimate_background_plate(observed, alpha)

        corner = float(S.luminance(plate.plate)[5, 5])
        centre = float(S.luminance(plate.plate)[100, 100])
        assert corner < centre, "the plate should slope with the real falloff"
        assert plate.uniformity > 0.8, "falloff must not be scored as non-uniformity"

    def test_busy_background_scores_low_and_fails_the_gate(self):
        observed, alpha = busy_scene()
        plate = S.estimate_background_plate(observed, alpha)
        assert plate.uniformity < S.UNIFORMITY_GATE, f"uniformity {plate.uniformity:.3f}"
        assert not plate.gate_passed

    def test_product_filling_the_frame_cannot_be_estimated(self):
        alpha = np.ones((60, 60), dtype=np.float32)
        rgb = np.full((60, 60, 3), 0.4, dtype=np.float32)
        plate = S.estimate_background_plate(rgb, alpha)
        assert plate.bg_fraction < 0.05
        assert not plate.gate_passed
        assert plate.plate.shape == rgb.shape, "a defined plate is still returned"

    def test_uniformity_is_always_reported_even_when_the_gate_fails(self):
        """It goes on every ImageResult so the reason is inspectable, not guessed at."""
        observed, alpha = busy_scene()
        plate = S.estimate_background_plate(observed, alpha)
        assert 0.0 <= plate.uniformity <= 1.0


# ---------------------------------------------------------------------------
# Ratio recovery
# ---------------------------------------------------------------------------


class TestShadowRatioRecovery:
    def test_recovers_the_ground_truth_shadow(self):
        """Regression guard on the figure quoted in docs/IMAGE_PIPELINE.md."""
        observed, alpha, true_ratio = studio_scene()
        ext = S.extract(observed, alpha)
        assert ext.ratio is not None

        region = (true_ratio < 0.95) & (alpha < 0.01)
        err = np.abs(ext.ratio[region] - true_ratio[region])
        assert err.mean() < 0.02, f"mean abs error {err.mean():.4f}"

    def test_clean_backdrop_is_left_perfectly_flat(self):
        """The deadband exists so sensor noise does not become visible grain on a flat fill."""
        observed, alpha, true_ratio = studio_scene()
        ext = S.extract(observed, alpha)
        assert ext.ratio is not None

        far_from_shadow = (true_ratio > 0.999) & (alpha < 0.001)
        assert far_from_shadow.any()
        assert np.all(ext.ratio[far_from_shadow] == 1.0), "clean backdrop must be exactly 1.0"

    def test_no_shadow_present_returns_none(self):
        observed, alpha, _ = studio_scene(with_shadow=False)
        assert S.extract(observed, alpha).ratio is None

    def test_gate_failure_returns_no_ratio(self):
        observed, alpha = busy_scene()
        ext = S.extract(observed, alpha)
        assert ext.ratio is None
        assert not ext.plate.gate_passed

    def test_ratio_is_bounded(self):
        observed, alpha, _ = studio_scene()
        ext = S.extract(observed, alpha)
        assert ext.ratio.min() >= 0.0
        assert ext.ratio.max() <= S._MAX_RATIO

    def test_area_under_the_product_is_neutral(self):
        """Hidden by the product anyway; forcing 1.0 keeps it from leaking at the edges."""
        observed, alpha, _ = studio_scene()
        ext = S.extract(observed, alpha)
        assert np.all(ext.ratio[alpha > 0.5] == 1.0)

    def test_shadow_survives_transfer_to_a_different_background_colour(self):
        """The point of the whole module: the shadow is scene lighting, not backdrop colour."""
        observed, alpha, _ = studio_scene()
        ext = S.extract(observed, alpha)

        for hexv in ["#1E3A8A", "#C81E1E", "#F5F5F5"]:
            bg = C.hex_to_srgb_linear(hexv)
            out, _ = X.flatten(observed, alpha, background_linear=bg, shadow_ratio=ext.ratio)
            out8 = C.to_uint8(C.srgb_encode(out))

            flat_corner = out8[2, 2]
            assert tuple(flat_corner) == C.hex_to_srgb8(hexv), f"{hexv}: flat area must be exact"

            shadowed = ext.ratio < 0.8
            assert shadowed.any()
            assert S.luminance(out)[shadowed].mean() < S.luminance(out)[2, 2], (
                f"{hexv}: shadow must darken the new background"
            )


class TestDeterminism:
    def test_extraction_is_reproducible(self):
        observed, alpha, _ = studio_scene()
        first = S.extract(observed, alpha)
        second = S.extract(observed, alpha)
        assert np.array_equal(first.ratio, second.ratio)
        assert first.plate.uniformity == second.plate.uniformity

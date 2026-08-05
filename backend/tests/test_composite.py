"""Compositing and decontamination tests."""

from __future__ import annotations

import numpy as np
import pytest

from app.imaging import color as C
from app.imaging import composite as X


class TestCompositeOverIsLinearLight:
    def test_fifty_percent_white_over_black_golden_value(self):
        """The golden-pixel test for linear-light compositing.

        Half-covered white over black is linear 0.5, which encodes to sRGB 188 — not 128.
        Getting 128 means the composite ran on gamma-encoded values, which is the single most
        common compositing bug and shows up as a dark rim on every cut-out.
        """
        fg = np.ones((1, 1, 3), dtype=np.float32)
        bg = np.zeros((1, 1, 3), dtype=np.float32)
        a = np.full((1, 1), 0.5, dtype=np.float32)

        out = X.composite_over(fg, a, bg)
        assert out[0, 0, 0] == pytest.approx(0.5)

        encoded = int(C.to_uint8(C.srgb_encode(out))[0, 0, 0])
        assert encoded == 188, f"expected 188 (linear), got {encoded}"
        assert encoded != 128, "128 means the composite happened in gamma space"

    def test_opaque_foreground_wins_completely(self):
        fg = np.full((4, 4, 3), 0.3, dtype=np.float32)
        bg = np.full((4, 4, 3), 0.9, dtype=np.float32)
        a = np.ones((4, 4), dtype=np.float32)
        assert np.allclose(X.composite_over(fg, a, bg), fg)

    def test_zero_alpha_leaves_background_untouched(self):
        fg = np.full((4, 4, 3), 0.3, dtype=np.float32)
        bg = np.full((4, 4, 3), 0.9, dtype=np.float32)
        a = np.zeros((4, 4), dtype=np.float32)
        assert np.allclose(X.composite_over(fg, a, bg), bg)

    def test_shape_mismatches_are_rejected(self):
        with pytest.raises(ValueError, match="disagree"):
            X.composite_over(
                np.zeros((4, 4, 3), np.float32),
                np.zeros((4, 4), np.float32),
                np.zeros((5, 5, 3), np.float32),
            )


class TestSolidBackground:
    def test_is_exactly_the_requested_colour(self):
        lin = C.hex_to_srgb_linear("#1E3A8A")
        bg = X.solid_background(10, 20, lin)
        assert bg.shape == (10, 20, 3)
        assert np.allclose(bg, lin.reshape(1, 1, 3))

    def test_round_trips_to_the_exact_hex(self):
        """The whole point of requirement 2."""
        for hexv in ["#F5F5F5", "#1E3A8A", "#C81E1E", "#000000", "#FFFFFF", "#808080"]:
            bg = X.solid_background(8, 8, C.hex_to_srgb_linear(hexv))
            out = C.to_uint8(C.srgb_encode(bg))
            assert tuple(out[0, 0]) == C.hex_to_srgb8(hexv), hexv
            assert len(np.unique(out.reshape(-1, 3), axis=0)) == 1, "fill must be perfectly flat"


class TestShadowRatioApplication:
    def test_multiplies_the_background(self):
        bg = np.full((4, 4, 3), 0.8, dtype=np.float32)
        ratio = np.full((4, 4), 0.5, dtype=np.float32)
        assert np.allclose(X.apply_shadow_ratio(bg, ratio), 0.4)

    def test_ratio_of_one_is_a_no_op(self):
        bg = np.full((4, 4, 3), 0.8, dtype=np.float32)
        assert np.allclose(X.apply_shadow_ratio(bg, np.ones((4, 4), np.float32)), bg)

    def test_reflection_gain_clips_at_white(self):
        """A blown highlight cannot be reconstructed; clipping is honest, invention is not."""
        bg = np.full((2, 2, 3), 0.9, dtype=np.float32)
        out = X.apply_shadow_ratio(bg, np.full((2, 2), 2.5, dtype=np.float32))
        assert out.max() <= 1.0

    def test_shape_mismatch_rejected(self):
        with pytest.raises(ValueError, match="disagree"):
            X.apply_shadow_ratio(np.zeros((4, 4, 3), np.float32), np.zeros((5, 5), np.float32))


class TestEdgeBandMask:
    def test_solid_interior_is_never_selected(self):
        a = np.zeros((40, 40), dtype=np.float32)
        a[10:30, 10:30] = 1.0
        a[9, 10:30] = 0.5                   # one partial row
        band = X.edge_band_mask(a, band_px=2)
        assert not band[20, 20], "deep interior must be excluded"
        assert band[10, 20], "pixels adjacent to the partial edge are included"

    def test_fully_opaque_image_has_no_band(self):
        assert not X.edge_band_mask(np.ones((10, 10), dtype=np.float32)).any()

    def test_fully_transparent_image_has_no_band(self):
        assert not X.edge_band_mask(np.zeros((10, 10), dtype=np.float32)).any()


class TestDecontamination:
    @staticmethod
    def _white_bg_scene():
        """A red product on a white backdrop with an antialiased edge — the halo scenario."""
        h = w = 80
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        dist = np.sqrt((xx - 40) ** 2 + (yy - 40) ** 2)
        alpha = np.clip(25 - dist + 0.5, 0.0, 1.0).astype(np.float32)

        plate = np.full((h, w, 3), C.srgb_decode(np.float32(0.97)), dtype=np.float32)
        product = np.zeros((h, w, 3), dtype=np.float32)
        product[..., 0] = C.srgb_decode(np.float32(0.78))

        observed = X.composite_over(product, alpha, plate)
        return observed, alpha, plate, product

    def test_recovers_the_products_true_edge_colour(self):
        observed, alpha, plate, product = self._white_bg_scene()
        cleaned = X.decontaminate_edges(observed, alpha, plate)

        band = (alpha > 0.5) & (alpha < 0.95)
        assert band.any()
        # The green channel is the tell: the product has none, the white backdrop has plenty.
        before = observed[..., 1][band].mean()
        after = cleaned[..., 1][band].mean()
        assert after < before, "decontamination must reduce backdrop spill"
        assert after < 0.5 * before, f"spill only fell from {before:.3f} to {after:.3f}"

    def test_solid_interior_is_left_bit_identical(self):
        """Decontamination must never touch pixels that were fully opaque."""
        observed, alpha, plate, _ = self._white_bg_scene()
        cleaned = X.decontaminate_edges(observed, alpha, plate)
        interior = alpha >= 0.999
        band = X.edge_band_mask(alpha)
        deep = interior & ~band
        assert deep.any()
        assert np.array_equal(cleaned[deep], observed[deep])

    def test_measured_halo_reduction_on_a_saturated_background(self):
        """Regression guard on the headline number quoted in docs/IMAGE_PIPELINE.md.

        Compositing the same cut-out onto navy, with and without decontamination. Navy's green
        channel is 58; white spill drags the edge band far above it.
        """
        observed, alpha, plate, _ = self._white_bg_scene()
        navy = C.hex_to_srgb_linear("#1E3A8A")
        band = (alpha > 0.25) & (alpha < 0.75)

        def edge_green(rgb):
            out, _ = X.flatten(rgb, alpha, background_linear=navy)
            return float(C.to_uint8(C.srgb_encode(out))[..., 1][band].mean())

        without = edge_green(observed)
        with_decontam = edge_green(X.decontaminate_edges(observed, alpha, plate))

        assert without - with_decontam > 30, (
            f"expected a large improvement, got {without:.1f} -> {with_decontam:.1f}"
        )

    def test_accepts_a_flat_plate_colour(self):
        observed, alpha, _, _ = self._white_bg_scene()
        flat = C.srgb_decode(np.float32([0.97, 0.97, 0.97]))
        assert X.decontaminate_edges(observed, alpha, flat).shape == observed.shape

    def test_no_partial_pixels_means_no_change(self):
        rgb = np.full((10, 10, 3), 0.4, dtype=np.float32)
        alpha = np.ones((10, 10), dtype=np.float32)
        plate = np.ones((10, 10, 3), dtype=np.float32)
        assert np.array_equal(X.decontaminate_edges(rgb, alpha, plate), rgb)


class TestFlatten:
    def test_transparent_output_preserves_alpha(self):
        rgb = np.full((6, 6, 3), 0.5, dtype=np.float32)
        alpha = np.full((6, 6), 0.4, dtype=np.float32)
        out_rgb, out_a = X.flatten(rgb, alpha, background_linear=None)
        assert np.array_equal(out_a, alpha)
        assert np.array_equal(out_rgb, rgb)

    def test_filled_output_is_opaque(self):
        rgb = np.full((6, 6, 3), 0.5, dtype=np.float32)
        alpha = np.full((6, 6), 0.4, dtype=np.float32)
        _, out_a = X.flatten(rgb, alpha, background_linear=C.hex_to_srgb_linear("#FFFFFF"))
        assert (out_a == 1.0).all()

    def test_shadow_is_dropped_for_transparent_output(self):
        """A shadow modulates a background. With no background it has nothing to act on, and
        multiplying it into the product's own colour would be wrong."""
        rgb = np.full((6, 6, 3), 0.5, dtype=np.float32)
        alpha = np.zeros((6, 6), dtype=np.float32)
        ratio = np.full((6, 6), 0.5, dtype=np.float32)
        out_rgb, _ = X.flatten(rgb, alpha, background_linear=None, shadow_ratio=ratio)
        assert np.array_equal(out_rgb, rgb)

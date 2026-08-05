"""Geometry tests: measurement, resampling quality, exact canvas, centring.

Synthetic fixtures only — no photographs, no keys. Exact expected values throughout.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.imaging import geometry as G
from app.models import CentringMode, CentringSpec, FitMode, Note, SizeSpec


# ---------------------------------------------------------------------------
# Fixtures: tiny synthetic products
# ---------------------------------------------------------------------------


def square(canvas: int = 100, size: int = 50, colour: float = 1.0):
    """Opaque square centred on a transparent canvas. RGB elsewhere is 0 (transparent black)."""
    rgb = np.zeros((canvas, canvas, 3), dtype=np.float32)
    a = np.zeros((canvas, canvas), dtype=np.float32)
    lo = (canvas - size) // 2
    hi = lo + size
    rgb[lo:hi, lo:hi] = colour
    a[lo:hi, lo:hi] = 1.0
    return rgb, a


def antialiased_disc(canvas: int = 200, radius: int = 70, colour: float = 1.0):
    """A disc with genuinely partial-coverage edge pixels.

    An axis-aligned square resized by an integer factor produces *no* partial pixels, so it
    cannot demonstrate anything about edge interpolation. A disc can.
    """
    yy, xx = np.mgrid[0:canvas, 0:canvas].astype(np.float32)
    dist = np.sqrt((xx - canvas / 2) ** 2 + (yy - canvas / 2) ** 2)
    a = np.clip(radius - dist + 0.5, 0.0, 1.0).astype(np.float32)
    rgb = np.full((canvas, canvas, 3), colour, dtype=np.float32)
    rgb[a <= 0] = 0.0                    # transparent region is black, the classic trap
    return rgb, a


def l_shape(canvas: int = 100):
    """Asymmetric mass: bbox centre and centroid genuinely differ."""
    rgb = np.zeros((canvas, canvas, 3), dtype=np.float32)
    a = np.zeros((canvas, canvas), dtype=np.float32)
    a[20:80, 20:35] = 1.0          # tall bar on the left
    a[65:80, 20:80] = 1.0          # foot along the bottom
    rgb[a > 0] = 1.0
    return rgb, a


DEFAULT_CENTRING = CentringSpec()


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


class TestAlphaBBox:
    def test_exact_bounds(self):
        _, a = square(100, 50)
        assert G.alpha_bbox(a) == G.BBox(25, 25, 75, 75)

    def test_half_open_so_width_is_intuitive(self):
        _, a = square(100, 50)
        b = G.alpha_bbox(a)
        assert (b.width, b.height) == (50, 50)

    def test_empty_alpha_gives_empty_box_not_an_error(self):
        a = np.zeros((10, 10), dtype=np.float32)
        assert G.alpha_bbox(a).is_empty()

    def test_threshold_is_measurement_only(self):
        """A soft halo below threshold is excluded from bounds but must remain in the data."""
        a = np.zeros((20, 20), dtype=np.float32)
        a[5:15, 5:15] = 1.0
        a[4, 5:15] = 0.02                      # faint fringe
        assert G.alpha_bbox(a, threshold=0.05).y0 == 5
        assert a[4, 7] == pytest.approx(0.02)   # untouched

    def test_rejects_non_2d(self):
        with pytest.raises(ValueError, match="2-D"):
            G.alpha_bbox(np.zeros((4, 4, 3), dtype=np.float32))


class TestCentroid:
    def test_symmetric_shape_centroid_is_the_centre(self):
        _, a = square(100, 50)
        cx, cy = G.alpha_centroid(a)
        assert cx == pytest.approx(50.0, abs=0.01)
        assert cy == pytest.approx(50.0, abs=0.01)

    def test_asymmetric_shape_differs_from_bbox_centre(self):
        _, a = l_shape()
        centroid = G.alpha_centroid(a)
        bbox_centre = G.alpha_bbox(a).centre
        assert abs(centroid[0] - bbox_centre[0]) > 3.0

    def test_empty_alpha_returns_none(self):
        assert G.alpha_centroid(np.zeros((8, 8), dtype=np.float32)) is None


# ---------------------------------------------------------------------------
# Resampling quality
# ---------------------------------------------------------------------------


class TestResampleRGBA:
    def test_premultiplied_resize_does_not_darken_edges(self):
        """The reason this module premultiplies.

        A white product on transparent *black* is the classic trap: interpolating straight alpha
        blends the invisible black into partially covered edge pixels, producing a dark rim that
        survives into the final composite. Premultiplied interpolation cannot do that.
        """
        rgb, a = antialiased_disc()
        out_rgb, out_a = G.resample_rgba(rgb, a, 57, 57)   # non-integer ratio

        band = (out_a > 0.2) & (out_a < 0.9)
        assert band.any(), "need partial-coverage pixels for this to mean anything"
        assert out_rgb[band].min() > 0.99, f"edge darkened to {out_rgb[band].min():.4f}"

    def test_naive_straight_alpha_resize_would_have_failed_that(self):
        """Control: the artefact is real, not hypothetical.

        Measured on this implementation: the naive path pulls mean edge colour from 1.0 down to
        ~0.87, worst pixel ~0.29. Quoted in docs/IMAGE_PIPELINE.md.
        """
        import cv2

        rgb, a = antialiased_disc()
        ours_rgb, ours_a = G.resample_rgba(rgb, a, 57, 57)
        naive = cv2.resize(np.dstack([rgb, a]), (57, 57), interpolation=cv2.INTER_LANCZOS4)

        band = (ours_a > 0.2) & (ours_a < 0.9)
        assert band.any()
        assert naive[..., :3][band].mean() < ours_rgb[band].mean() - 0.05, (
            "naive path should visibly darken the edge"
        )

    def test_premultiplied_path_keeps_alpha_in_gamut(self):
        """Lanczos overshoots. On straight RGBA that yields alpha outside [0,1] (measured 1.07)."""
        import cv2

        rgb, a = antialiased_disc()
        _, ours_a = G.resample_rgba(rgb, a, 57, 57)
        naive_a = cv2.resize(np.dstack([rgb, a]), (57, 57), interpolation=cv2.INTER_LANCZOS4)[..., 3]

        assert naive_a.max() > 1.0 or naive_a.min() < 0.0, "control: naive overshoots"
        assert ours_a.min() >= 0.0 and ours_a.max() <= 1.0

    def test_alpha_stays_in_range(self):
        rgb, a = square(64, 32)
        _, out_a = G.resample_rgba(rgb, a, 21, 21)
        assert out_a.min() >= 0.0 and out_a.max() <= 1.0

    def test_large_reduction_does_not_alias(self):
        """A fine stripe pattern reduced 8x must average, not point-sample."""
        rgb = np.zeros((256, 256, 3), dtype=np.float32)
        a = np.ones((256, 256), dtype=np.float32)
        rgb[:, ::2] = 1.0                     # 1px on / 1px off
        out_rgb, _ = G.resample_rgba(rgb, a, 32, 32)
        # Correct area-averaging converges on mid grey; point-sampling gives 0s and 1s.
        assert out_rgb.mean() == pytest.approx(0.5, abs=0.05)
        assert out_rgb.std() < 0.15, f"aliasing: std {out_rgb.std():.3f}"

    def test_output_dimensions_are_exact(self):
        rgb, a = square(100, 50)
        out_rgb, out_a = G.resample_rgba(rgb, a, 37, 53)
        assert out_rgb.shape == (53, 37, 3)
        assert out_a.shape == (53, 37)

    def test_rejects_mismatched_shapes(self):
        with pytest.raises(ValueError, match="disagree"):
            G.resample_rgba(
                np.zeros((10, 10, 3), dtype=np.float32), np.zeros((8, 8), dtype=np.float32), 5, 5
            )

    def test_rejects_degenerate_target(self):
        rgb, a = square(20, 10)
        with pytest.raises(ValueError, match="at least 1x1"):
            G.resample_rgba(rgb, a, 0, 5)


# ---------------------------------------------------------------------------
# Placement: the four requirements
# ---------------------------------------------------------------------------


class TestExactCanvas:
    @pytest.mark.parametrize(
        "w,h", [(500, 500), (1000, 1000), (37, 53), (1, 1), (2048, 512), (501, 499)]
    )
    @pytest.mark.parametrize("fit", list(FitMode))
    def test_output_is_exactly_the_requested_size(self, w, h, fit):
        """'output 500x500 px' is literal. Odd sizes and extreme ratios included."""
        rgb, a = square(100, 50)
        p = G.place_on_canvas(rgb, a, SizeSpec(width=w, height=h, fit=fit), DEFAULT_CENTRING)
        assert p.rgb.shape == (h, w, 3)
        assert p.alpha.shape == (h, w)

    def test_empty_alpha_still_yields_an_exact_canvas(self):
        rgb = np.zeros((50, 50, 3), dtype=np.float32)
        a = np.zeros((50, 50), dtype=np.float32)
        p = G.place_on_canvas(rgb, a, SizeSpec(width=500, height=500), DEFAULT_CENTRING)
        assert p.rgb.shape == (500, 500, 3)
        assert p.alpha.max() == 0.0


class TestCentring:
    def test_bbox_mode_centres_within_half_a_pixel(self):
        rgb, a = square(100, 50)
        p = G.place_on_canvas(rgb, a, SizeSpec(width=400, height=400), DEFAULT_CENTRING)
        b = G.alpha_bbox(p.alpha)
        cx, cy = b.centre
        assert cx == pytest.approx(200.0, abs=0.5)
        assert cy == pytest.approx(200.0, abs=0.5)

    def test_centroid_offset_is_reported_not_assumed(self):
        rgb, a = square(100, 50)
        p = G.place_on_canvas(rgb, a, SizeSpec(width=400, height=400), DEFAULT_CENTRING)
        assert p.centroid_offset_px is not None
        assert abs(p.centroid_offset_px[0]) <= 1.0
        assert abs(p.centroid_offset_px[1]) <= 1.0

    def test_centroid_mode_puts_centre_of_mass_on_the_canvas_centre(self):
        rgb, a = l_shape()
        p = G.place_on_canvas(
            rgb,
            a,
            SizeSpec(width=400, height=400, margin_pct=0),
            CentringSpec(mode=CentringMode.CENTROID),
        )
        assert abs(p.centroid_offset_px[0]) < 1.0
        assert abs(p.centroid_offset_px[1]) < 1.0

    def test_the_two_modes_disagree_on_an_asymmetric_product(self):
        """Not a bug — the reason the mode is configurable."""
        rgb, a = l_shape()
        size = SizeSpec(width=400, height=400, margin_pct=0)
        by_bbox = G.place_on_canvas(rgb, a, size, CentringSpec(mode=CentringMode.BBOX))
        by_centroid = G.place_on_canvas(rgb, a, size, CentringSpec(mode=CentringMode.CENTROID))
        assert abs(by_bbox.centroid_offset_px[0] - by_centroid.centroid_offset_px[0]) > 3.0


class TestMargin:
    @pytest.mark.parametrize("margin", [0.0, 5.0, 10.0, 25.0])
    def test_product_occupies_the_expected_fraction(self, margin):
        """Measured at the 50%-coverage contour, which is where the geometric edge actually is.

        Measuring at the default 0.05 threshold would include the soft interpolation ramp — for
        an upscaled hard-edged synthetic that is ~3px per side — and report a product wider than
        it is. Bounds *input* to scaling deliberately uses 0.05 so a real photo's visible fringe
        stays inside the margin; bounds *verification* uses 0.5.
        """
        rgb, a = square(100, 50)
        p = G.place_on_canvas(
            rgb,
            a,
            SizeSpec(width=400, height=400, margin_pct=margin, allow_upscale=True),
            DEFAULT_CENTRING,
        )
        b = G.alpha_bbox(p.alpha, threshold=0.5)
        expected = 400 * (1.0 - 2 * margin / 100.0)
        assert b.width == pytest.approx(expected, abs=2)

    def test_zero_margin_fills_the_canvas(self):
        rgb, a = square(100, 50)
        p = G.place_on_canvas(
            rgb, a, SizeSpec(width=200, height=200, margin_pct=0, allow_upscale=True), DEFAULT_CENTRING
        )
        assert G.alpha_bbox(p.alpha, threshold=0.5).width == pytest.approx(200, abs=2)


class TestUpscalePolicy:
    def test_small_master_is_padded_not_stretched_by_default(self):
        """A stretched master looks worse than a padded one, so this is the default."""
        rgb, a = square(50, 20)
        p = G.place_on_canvas(rgb, a, SizeSpec(width=1000, height=1000), DEFAULT_CENTRING)
        assert Note.UPSCALE_SKIPPED in p.notes
        assert p.scale == pytest.approx(1.0)
        assert G.alpha_bbox(p.alpha).width == pytest.approx(20, abs=1)

    def test_opting_in_allows_enlargement(self):
        rgb, a = square(50, 20)
        p = G.place_on_canvas(
            rgb, a, SizeSpec(width=1000, height=1000, allow_upscale=True), DEFAULT_CENTRING
        )
        assert Note.UPSCALE_SKIPPED not in p.notes
        assert p.scale > 1.0

    def test_downscale_never_emits_the_note(self):
        rgb, a = square(1000, 800)
        p = G.place_on_canvas(rgb, a, SizeSpec(width=200, height=200), DEFAULT_CENTRING)
        assert Note.UPSCALE_SKIPPED not in p.notes
        assert p.scale < 1.0


class TestFitModes:
    def test_contain_never_crops(self):
        rgb, a = square(100, 80)
        p = G.place_on_canvas(
            rgb, a, SizeSpec(width=200, height=100, fit=FitMode.CONTAIN), DEFAULT_CENTRING
        )
        assert Note.COVER_CROPPED not in p.notes

    def test_cover_crops_and_says_so(self):
        """COVER fills the canvas by removing product. Never silent."""
        rgb, a = square(100, 80)
        p = G.place_on_canvas(
            rgb,
            a,
            SizeSpec(width=400, height=100, fit=FitMode.COVER, allow_upscale=True),
            DEFAULT_CENTRING,
        )
        assert Note.COVER_CROPPED in p.notes

    def test_cover_leaves_no_empty_canvas(self):
        rgb, a = square(100, 80)
        p = G.place_on_canvas(
            rgb,
            a,
            SizeSpec(width=300, height=120, fit=FitMode.COVER, margin_pct=0, allow_upscale=True),
            DEFAULT_CENTRING,
        )
        # Integer placement can leave the outermost column at exactly half coverage; that is
        # inherent to not resampling a second time for sub-pixel positioning.
        assert p.alpha[:, 0].max() >= 0.5 and p.alpha[:, -1].max() >= 0.5


class TestShadowBounds:
    def test_shadow_excluded_from_bounds_by_default(self):
        rgb, a = square(100, 40)
        ratio = np.ones((100, 100), dtype=np.float32)
        ratio[70:85, 30:70] = 0.6                    # shadow below the product
        # allow_upscale so both cases are free to choose a scale; otherwise the upscale ban
        # clamps both to 1.0 and the comparison proves nothing.
        size = SizeSpec(width=300, height=300, allow_upscale=True)
        without = G.place_on_canvas(rgb, a, size, CentringSpec(), shadow_ratio=ratio)
        with_shadow = G.place_on_canvas(
            rgb, a, size, CentringSpec(include_shadow_in_bounds=True), shadow_ratio=ratio
        )
        # Including the shadow enlarges the measured bounds, so the product must scale smaller.
        assert with_shadow.scale < without.scale

    def test_shadow_map_is_carried_through_placement(self):
        rgb, a = square(100, 40)
        ratio = np.ones((100, 100), dtype=np.float32)
        ratio[70:85, 30:70] = 0.6
        p = G.place_on_canvas(
            rgb, a, SizeSpec(width=300, height=300), CentringSpec(), shadow_ratio=ratio
        )
        assert p.shadow_ratio is not None
        assert p.shadow_ratio.shape == (300, 300)
        assert p.shadow_ratio.min() < 0.99, "the shadow should still be present"

    def test_areas_outside_the_source_default_to_no_shadow(self):
        rgb, a = square(100, 40)
        ratio = np.ones((100, 100), dtype=np.float32)
        ratio[70:85, 30:70] = 0.6
        p = G.place_on_canvas(
            rgb, a, SizeSpec(width=400, height=400, margin_pct=25), CentringSpec(), shadow_ratio=ratio
        )
        assert p.shadow_ratio[0, 0] == pytest.approx(1.0), "padding must not be tinted"


class TestDeterminism:
    def test_identical_input_gives_identical_output(self):
        """Guards the deterministic path against a model or random seed creeping in."""
        rgb, a = square(120, 61)
        size = SizeSpec(width=433, height=289, margin_pct=7.5)
        first = G.place_on_canvas(rgb, a, size, DEFAULT_CENTRING)
        second = G.place_on_canvas(rgb, a, size, DEFAULT_CENTRING)
        assert np.array_equal(first.rgb, second.rgb)
        assert np.array_equal(first.alpha, second.alpha)
        assert first.centroid_offset_px == second.centroid_offset_px

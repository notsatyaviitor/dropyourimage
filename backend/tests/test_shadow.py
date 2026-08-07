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


def panelled_wall_scene(size: int = 300):
    """A subject against a mildly textured wall — the case that produced a real, reported bug.

    Reproduces the structure of the photograph that broke this: a near-white wall carrying panel
    seams and a luminance gradient. It is uniform *enough* to score well above the old 0.50 gate,
    but its structure is not representable by the plate estimator's quadratic surface, so the
    residual is scene texture rather than shadow.

    Returns ``(observed, alpha)``.
    """
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)

    # Bright wall with a gentle gradient — a quadratic *can* fit this part.
    wall = 0.90 - 0.07 * (yy / size)
    # Panel seams: narrow dark bands a quadratic cannot represent at all.
    seam = np.exp(-(((xx % (size / 3.0)) - 4.0) / 2.2) ** 2)
    wall = wall - 0.16 * seam
    # A dark object intruding into one corner, as the tree did in the real photograph.
    corner = np.exp(-(((xx - size * 0.93) / (size * 0.09)) ** 2 + ((yy - size * 0.06) / (size * 0.07)) ** 2))
    wall = wall - 0.55 * corner

    backdrop = np.dstack([C.srgb_decode(np.clip(wall, 0, 1).astype(np.float32))] * 3)

    dist = np.sqrt(((xx - size / 2) / 1.05) ** 2 + ((yy - size * 0.62) / 1.5) ** 2)
    alpha = np.clip(size * 0.30 - dist + 0.5, 0.0, 1.0).astype(np.float32)
    subject = np.full((size, size, 3), C.srgb_decode(np.float32(0.22)), dtype=np.float32)

    return X.composite_over(subject, alpha, backdrop), alpha


class TestShadowGateRegression:
    """A real photograph broke this, and the symptom was misleading.

    A man was photographed against a panelled white wall. remove.bg returned a *correct* silhouette,
    yet the delivered asset showed the original wall — so it read as "background removal is not
    working". It was working: the background was replaced, and then the shadow stage repainted the
    wall on top of it, because it had measured the wall's own structure as shadow.

    Two guards now stand between that and a delivered asset. Both are pinned here.
    """

    def test_a_real_cast_shadow_is_still_preserved(self):
        """The guards must not cost us the feature they protect."""
        observed, alpha, _ = studio_scene(size=300)
        plate = S.estimate_background_plate(observed, alpha)
        ratio = S.extract_shadow_ratio(observed, alpha, plate)

        assert plate.uniformity > S.UNIFORMITY_GATE, plate.uniformity
        assert ratio is not None, "a genuine cast shadow on a studio sweep must survive"
        assert float(ratio.min()) < 0.95, "and it must actually darken something"

    def test_a_textured_wall_is_refused(self):
        observed, alpha = panelled_wall_scene(size=300)
        plate = S.estimate_background_plate(observed, alpha)
        ratio = S.extract_shadow_ratio(observed, alpha, plate)

        assert ratio is None, (
            f"a wall with seams must not be treated as shadow (uniformity {plate.uniformity:.4f})"
        )

    def test_the_uniformity_gate_alone_would_have_let_it_through(self):
        """Pins that the coverage guard is the load-bearing one, not the uniformity gate.

        This fixture scores *high* uniformity — the wall is bright and mostly smooth, so the
        quadratic plate fits it well on average. The gate cannot see the seams. Only measuring the
        residual catches them. If someone ever "simplifies" this back to a uniformity threshold, this
        test says why that does not work.
        """
        observed, alpha = panelled_wall_scene(size=300)
        plate = S.estimate_background_plate(observed, alpha)
        assert plate.uniformity >= S.UNIFORMITY_GATE, (
            f"fixture no longer reproduces the bug — uniformity {plate.uniformity:.4f} is already "
            "below the gate, so it would be refused anyway and proves nothing about coverage"
        )

    def test_a_shadow_covering_most_of_the_backdrop_is_refused(self):
        """The coverage guard, isolated from the uniformity gate.

        A cast shadow is local; measured 11.3% of the backdrop on the studio fixture versus 75.4% on
        the real photograph. A map that darkens nearly everything is a copy of the backdrop.
        """
        size = 200
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        # A perfectly flat backdrop (uniformity will be high) dimmed almost everywhere, so only the
        # coverage guard can catch it.
        flat = np.full((size, size), C.srgb_decode(np.float32(0.95)), dtype=np.float32)
        backdrop = np.dstack([flat] * 3)
        wide = np.where(yy > size * 0.1, 0.6, 1.0).astype(np.float32)

        dist = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
        alpha = np.clip(size * 0.12 - dist + 0.5, 0.0, 1.0).astype(np.float32)
        product = np.zeros((size, size, 3), dtype=np.float32)
        observed = X.composite_over(product, alpha, backdrop * wide[..., None])

        plate = S.estimate_background_plate(observed, alpha)
        ratio = S.extract_shadow_ratio(observed, alpha, plate)
        assert ratio is None, "a map darkening ~90% of the backdrop is not a shadow"

    def test_the_thresholds_stay_where_the_measurements_put_them(self):
        """Both were tuned against real data; a silent change should fail loudly here."""
        assert S._MAX_SHADOW_COVERAGE == 0.35, "measured: studio 11.3% vs wall 75.4%"
        assert S.UNIFORMITY_GATE == 0.50, (
            "deliberately NOT raised — see the comment in shadow.py. There is no measurement of "
            "what a real studio photograph scores, so raising it on one data point risks disabling "
            "the feature on exactly the images it exists for."
        )

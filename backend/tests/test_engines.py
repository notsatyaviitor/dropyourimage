"""Engine layer tests: the control engine's mixture solve, and auto-pick selection.

No network. Vendor adapters are covered only for error classification — their wire behaviour needs
real keys and is marked `needs_keys`.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.settings import Settings
from app.engines import autopick, registry
from app.engines.base import AlphaResult, fit_alpha_to_source
from app.engines.local import LocalEngine, key_on_backdrop, solve_mixture_alpha
from app.imaging import color as C
from app.imaging import composite as X
from app.imaging import export as E
from app.models import CutoutSpec, EngineId, EngineStrategy, Note, OutputFormat
from tests.test_shadow import busy_scene, studio_scene


def result(alpha: np.ndarray, engine: EngineId = EngineId.LOCAL, cost: float = 0.0) -> AlphaResult:
    return AlphaResult(alpha=alpha, engine=engine, latency_ms=1, cost_usd=cost)


def disc_mask(size: int = 100, radius: int = 30, soft: bool = True) -> np.ndarray:
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    d = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
    if soft:
        return np.clip(radius - d + 0.5, 0.0, 1.0).astype(np.float32)
    return (d <= radius).astype(np.float32)


# ---------------------------------------------------------------------------
# The mixture solve
# ---------------------------------------------------------------------------


class TestMixtureSolve:
    def test_recovers_coverage_proportionally(self):
        """The property that chromaticity keying lacked: alpha tracks coverage, not just presence."""
        product = np.array([0.6, 0.05, 0.05], dtype=np.float32)
        backdrop = np.array([0.9, 0.9, 0.9], dtype=np.float32)

        for true_a in (0.0, 0.15, 0.35, 0.5, 0.75, 1.0):
            observed = (true_a * product + (1 - true_a) * backdrop).reshape(1, 1, 3)
            got = float(solve_mixture_alpha(observed, product, backdrop)[0, 0])
            assert got == pytest.approx(true_a, abs=0.01), f"coverage {true_a} -> {got}"

    def test_is_invariant_to_shadow_on_the_backdrop(self):
        """A shadowed backdrop is k*B, which must solve to alpha 0 — for any k.

        This is why the backdrop scale is left free instead of being fixed at 1-alpha. If it were
        fixed, shadow would read as product and be cut away.
        """
        product = np.array([0.6, 0.05, 0.05], dtype=np.float32)
        backdrop = np.array([0.9, 0.9, 0.9], dtype=np.float32)

        for k in (1.0, 0.8, 0.55, 0.3):
            shadowed = (k * backdrop).reshape(1, 1, 3)
            assert float(solve_mixture_alpha(shadowed, product, backdrop)[0, 0]) == pytest.approx(
                0.0, abs=0.01
            ), f"shadow at {k} leaked into alpha"

    def test_coverage_over_shadowed_backdrop_still_solves(self):
        product = np.array([0.6, 0.05, 0.05], dtype=np.float32)
        backdrop = np.array([0.9, 0.9, 0.9], dtype=np.float32)
        observed = (0.4 * product + 0.6 * 0.5 * backdrop).reshape(1, 1, 3)
        assert float(solve_mixture_alpha(observed, product, backdrop)[0, 0]) == pytest.approx(
            0.4, abs=0.02
        )

    def test_degenerate_when_product_matches_backdrop(self):
        """White on white is unsolvable; returning zeros lets the caller fall back."""
        c = np.array([0.9, 0.9, 0.9], dtype=np.float32)
        out = solve_mixture_alpha(np.full((4, 4, 3), 0.9, dtype=np.float32), c, c)
        assert out.shape == (4, 4)
        assert out.max() == 0.0


class TestLocalEngineOnSyntheticScenes:
    def test_finds_the_product_accurately(self):
        observed, true_alpha, _ = studio_scene(size=200)
        got = key_on_backdrop(observed)
        inter = ((got > 0.5) & (true_alpha > 0.5)).sum()
        union = ((got > 0.5) | (true_alpha > 0.5)).sum()
        assert inter / union > 0.95

    def test_does_not_claim_the_shadow(self):
        """If it ate the shadow, the preservation stage would have nothing left to recover."""
        observed, true_alpha, true_ratio = studio_scene(size=200)
        got = key_on_backdrop(observed)
        shadow = (true_ratio < 0.75) & (true_alpha < 0.01)
        assert shadow.any()
        assert got[shadow].max() < 0.1

    def test_produces_soft_alpha(self):
        observed, _, _ = studio_scene(size=200)
        got = key_on_backdrop(observed)
        assert ((got > 0.05) & (got < 0.95)).any(), "a binary mask scores badly in auto-pick"

    def test_alpha_tracks_true_coverage_closely(self):
        """Note: the synthetic scene *is* a two-colour mixture, which flatters this model.
        Real textured photographs will not be this close."""
        observed, true_alpha, _ = studio_scene(size=200)
        got = key_on_backdrop(observed)
        band = (true_alpha > 0.01) | (got > 0.01)
        assert np.abs(got - true_alpha)[band].mean() < 0.01

    def test_finds_a_dark_product_on_a_light_backdrop(self):
        size = 200
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        d = np.sqrt((xx - 100) ** 2 + (yy - 100) ** 2)
        alpha = np.clip(40 - d + 0.5, 0, 1).astype(np.float32)
        backdrop = np.full((size, size, 3), C.srgb_decode(np.float32(0.97)), dtype=np.float32)
        scene = X.composite_over(np.zeros((size, size, 3), dtype=np.float32), alpha, backdrop)

        got = key_on_backdrop(scene)
        inter = ((got > 0.5) & (alpha > 0.5)).sum()
        union = ((got > 0.5) | (alpha > 0.5)).sum()
        assert inter / union > 0.97

    def test_blank_frame_yields_an_empty_mask_not_a_crash(self):
        flat = np.full((60, 60, 3), 0.8, dtype=np.float32)
        assert key_on_backdrop(flat).max() == 0.0

    async def test_engine_interface(self):
        observed, _, _ = studio_scene(size=120)
        blob = E.encode(observed, None, fmt=OutputFormat.PNG)
        res = await LocalEngine().alpha_for(blob, 120, 120)
        assert res.engine is EngineId.LOCAL
        assert res.cost_usd == 0.0
        assert res.alpha.shape == (120, 120)

    async def test_mask_is_resized_to_the_source_dimensions(self):
        """Vendors cap resolution on lower tiers; a misaligned mask looks like a bad cut-out."""
        observed, _, _ = studio_scene(size=120)
        blob = E.encode(observed, None, fmt=OutputFormat.PNG)
        res = await LocalEngine().alpha_for(blob, 300, 200)
        assert res.alpha.shape == (200, 300)


class TestFitAlphaToSource:
    def test_passthrough_when_already_correct(self):
        a = disc_mask(50)
        assert fit_alpha_to_source(a, 50, 50) is a

    def test_stays_in_gamut_when_resized(self):
        out = fit_alpha_to_source(disc_mask(100), 37, 53)
        assert out.shape == (53, 37)
        assert out.min() >= 0.0 and out.max() <= 1.0

    def test_rejects_non_2d(self):
        with pytest.raises(ValueError, match="2-D"):
            fit_alpha_to_source(np.zeros((4, 4, 3), dtype=np.float32), 4, 4)


# ---------------------------------------------------------------------------
# Auto-pick
# ---------------------------------------------------------------------------


class TestRejectRules:
    def test_empty_mask_is_rejected(self):
        m = autopick.measure(result(np.zeros((100, 100), dtype=np.float32)))
        assert m.rejected_reason is not None
        assert "empty" in m.rejected_reason

    def test_full_frame_mask_is_rejected(self):
        m = autopick.measure(result(np.ones((100, 100), dtype=np.float32)))
        assert m.rejected_reason is not None
        assert "whole frame" in m.rejected_reason

    def test_mask_touching_all_four_edges_is_rejected_as_inverted(self):
        a = np.ones((100, 100), dtype=np.float32)
        a[40:60, 40:60] = 0.0                     # inverted: product transparent, backdrop opaque
        m = autopick.measure(result(a))
        assert m.rejected_reason is not None
        assert "inverted" in m.rejected_reason

    def test_confetti_mask_is_rejected(self):
        a = np.zeros((100, 100), dtype=np.float32)
        for i in range(6, 94, 12):
            for j in range(6, 94, 12):
                a[i : i + 4, j : j + 4] = 1.0
        m = autopick.measure(result(a))
        assert m.rejected_reason is not None
        assert "fragmented" in m.rejected_reason

    def test_a_good_mask_is_not_rejected(self):
        m = autopick.measure(result(disc_mask()))
        assert m.rejected_reason is None
        assert m.score > 0.0


class TestScoring:
    def test_soft_edges_score_above_hard_ones(self):
        """The reason engines returning true alpha beat binary-mask ones."""
        soft = autopick.measure(result(disc_mask(200, 60, soft=True)))
        hard = autopick.measure(result(disc_mask(200, 60, soft=False)))
        assert soft.score > hard.score

    def test_soft_alpha_ratio_is_reported(self):
        m = autopick.measure(result(disc_mask(200, 60, soft=True)))
        assert m.soft_alpha_ratio > 0.0

    def test_score_is_bounded(self):
        for mask in (disc_mask(), disc_mask(200, 80), disc_mask(50, 10)):
            assert 0.0 <= autopick.measure(result(mask)).score <= 1.0


def two_discs(size: int = 200, big: int = 34, small: int = 30) -> np.ndarray:
    """Two separated soft discs of similar size: a scene with more than one object.

    Sized so the larger holds ~56% of the mask, under `_MIN_SOLIDITY` (60%) — `measure` rejects it
    as fragmented. That is the shape of the real furnished-room mask this exists for, where the
    biggest of five fixtures held 52%.
    """
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    a = np.zeros((size, size), np.float32)
    for cx, cy, r in ((size * 0.30, size * 0.35, big), (size * 0.75, size * 0.75, small)):
        d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        a = np.maximum(a, np.clip(r - d + 0.5, 0.0, 1.0))
    return a.astype(np.float32)


class TestSceneRecovery:
    """A mask holding several objects delivers its largest rather than failing the image.

    Measured on a real customer file: a 45 MP CR3 of a bathroom, where Photoroom returned five
    correct fixtures (washbasin 52%, bath 23%, mirror 18%, flush plate, shelf) and auto-pick
    rejected the lot as fragmented. Failing was honest but useless — the order got nothing.
    """

    def test_a_two_object_mask_is_rejected_before_recovery(self):
        m = autopick.measure(result(two_discs()))
        assert m.reject is autopick._Reject.FRAGMENTED

    def test_choose_now_delivers_the_largest_object(self):
        v = autopick.choose([result(two_discs())])
        assert v.winner is not None
        assert Note.SCENE_LARGEST_OBJECT in v.notes

    def test_the_smaller_object_is_gone_and_the_larger_survives(self):
        kept = autopick.keep_largest_object(two_discs())
        assert kept is not None
        assert kept[150, 150] == 0.0, "the small disc should have been discarded"
        assert kept[70, 60] > 0.9, "the large disc should be untouched"

    def test_soft_edges_survive_isolation(self):
        """Invariant 2: labelling on the core rather than the support would hard-edge the keeper."""
        kept = autopick.keep_largest_object(two_discs())
        assert kept is not None
        soft = ((kept > 0.05) & (kept < 0.95)).sum()
        assert soft > 0, "the kept object lost its transition band"

    def test_a_single_object_mask_is_left_alone(self):
        assert autopick.keep_largest_object(disc_mask()) is None

    def test_an_empty_mask_is_not_rescued(self):
        """Only fragmentation is recoverable — isolating a blob of nothing invents an answer."""
        v = autopick.choose([result(np.zeros((50, 50), dtype=np.float32))])
        assert v.winner is None
        assert Note.SCENE_LARGEST_OBJECT not in v.notes

    def test_an_inverted_mask_is_not_rescued(self):
        v = autopick.choose([result(np.ones((50, 50), dtype=np.float32))])
        assert v.winner is None
        assert Note.SCENE_LARGEST_OBJECT not in v.notes

    def test_a_good_mask_never_reaches_recovery(self):
        v = autopick.choose([result(disc_mask())])
        assert Note.SCENE_LARGEST_OBJECT not in v.notes

    def test_recovery_is_deterministic(self):
        a = two_discs()
        first = autopick.keep_largest_object(a)
        second = autopick.keep_largest_object(a)
        assert np.array_equal(first, second)


class TestChoose:
    def test_no_candidates_yields_no_winner(self):
        v = autopick.choose([])
        assert v.winner is None

    def test_all_rejected_yields_no_winner_but_keeps_the_reasons(self):
        v = autopick.choose([result(np.zeros((50, 50), dtype=np.float32))])
        assert v.winner is None
        assert v.candidates[0].rejected_reason is not None

    def test_single_engine_is_flagged(self):
        v = autopick.choose([result(disc_mask())])
        assert Note.SINGLE_ENGINE_ONLY in v.notes

    def test_better_mask_wins_over_a_rejected_one(self):
        good = result(disc_mask(), EngineId.PHOTOROOM)
        bad = result(np.zeros((100, 100), dtype=np.float32), EngineId.REMOVEBG)
        v = autopick.choose([bad, good])
        assert v.winner is not None
        assert v.winner.engine is EngineId.PHOTOROOM

    def test_losing_the_preferred_engine_is_surfaced(self):
        """The signal that the primary engine is struggling on a category."""
        bad_first = result(np.zeros((100, 100), dtype=np.float32), EngineId.PHOTOROOM)
        good_second = result(disc_mask(), EngineId.REMOVEBG)
        v = autopick.choose([bad_first, good_second])
        assert Note.ENGINE_FALLBACK_USED in v.notes

    def test_every_candidate_is_reported_for_side_by_side_review(self):
        v = autopick.choose(
            [result(disc_mask(), EngineId.PHOTOROOM), result(disc_mask(), EngineId.REMOVEBG)]
        )
        assert len(v.candidates) == 2

    def test_identical_masks_go_to_the_tiebreak(self):
        mask = disc_mask()
        v = autopick.choose(
            [result(mask.copy(), EngineId.PHOTOROOM), result(mask.copy(), EngineId.REMOVEBG)]
        )
        assert Note.TIEBREAK_DETERMINISTIC in v.notes
        assert autopick.needs_vision_tiebreak(v)

    def test_clearly_different_masks_do_not_need_a_tiebreak(self):
        v = autopick.choose(
            [
                result(disc_mask(200, 60, soft=True), EngineId.PHOTOROOM),
                result(disc_mask(200, 60, soft=False), EngineId.REMOVEBG),
            ]
        )
        assert not autopick.needs_vision_tiebreak(v)

    def test_costs_are_recorded_per_candidate_even_for_the_loser(self):
        """Both engines are billed; the ledger must reflect that."""
        v = autopick.choose(
            [
                result(disc_mask(), EngineId.PHOTOROOM, cost=0.02),
                result(disc_mask(), EngineId.REMOVEBG, cost=0.20),
            ]
        )
        assert sum(c.cost_usd or 0 for c in v.candidates) == pytest.approx(0.22)


class TestRegistry:
    def test_falls_back_to_the_control_engine_with_no_keys(self):
        """What keeps procurement off the critical path."""
        pool = registry.select_pool(Settings(photoroom_api_key="", removebg_api_key="", fal_key=""), CutoutSpec())
        assert [e.id for e in pool] == [EngineId.LOCAL]

    def test_auto_uses_the_first_two_available_in_priority_order(self):
        settings = Settings(
            photoroom_api_key="k1",
            fal_key="k2",
            removebg_api_key="k3",
            engine_pool="photoroom,falai,removebg",
        )
        assert [e.id for e in registry.select_pool(settings, CutoutSpec())] == [
            EngineId.PHOTOROOM,
            EngineId.FALAI,
        ]

    def test_unconfigured_engines_are_skipped_not_fatal(self):
        settings = Settings(
            photoroom_api_key="", fal_key="k2", removebg_api_key="k3",
            engine_pool="photoroom,falai,removebg",
        )
        assert [e.id for e in registry.select_pool(settings, CutoutSpec())] == [
            EngineId.FALAI,
            EngineId.REMOVEBG,
        ]

    def test_single_strategy_honours_the_named_engine(self):
        settings = Settings(photoroom_api_key="k1", removebg_api_key="k2")
        pool = registry.select_pool(
            settings, CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.REMOVEBG)
        )
        assert [e.id for e in pool] == [EngineId.REMOVEBG]

    def test_engine_pool_rejects_unknown_names(self):
        with pytest.raises(ValueError, match="unknown engine"):
            Settings(engine_pool="photoroom,nonexistent")


class TestVendorAdapters:
    def test_unconfigured_engines_report_unavailable(self):
        from app.engines.http import PhotoroomEngine

        assert not PhotoroomEngine(Settings(photoroom_api_key="")).available()
        assert PhotoroomEngine(Settings(photoroom_api_key="k")).available()

    async def test_calling_an_unconfigured_engine_gives_a_typed_error(self):
        from app.core.errors import VendorUnauthorized
        from app.engines.http import PhotoroomEngine

        with pytest.raises(VendorUnauthorized):
            await PhotoroomEngine(Settings(photoroom_api_key="")).alpha_for(b"", 10, 10)

    async def test_the_error_message_never_contains_the_key(self):
        from app.core.errors import VendorUnauthorized
        from app.engines.http import PhotoroomEngine

        try:
            await PhotoroomEngine(Settings(photoroom_api_key="")).alpha_for(b"", 10, 10)
        except VendorUnauthorized as exc:
            assert "SECRET" not in exc.message

    def test_costs_match_the_client_report(self):
        from app.engines.http import FalAiEngine, PhotoroomEngine, RemoveBgEngine

        s = Settings()
        assert PhotoroomEngine(s).cost_per_image_usd == 0.02
        assert RemoveBgEngine(s).cost_per_image_usd == 0.20
        assert 0.03 <= FalAiEngine(s).cost_per_image_usd <= 0.05


class TestBusySceneHandling:
    def test_a_lifestyle_shot_is_either_rejected_or_flagged(self):
        """Either outcome is honest. Producing a confident bad mask is not."""
        observed, _ = busy_scene(size=200)
        got = key_on_backdrop(observed)
        verdict = autopick.choose([result(got)])
        if verdict.winner is not None:
            assert verdict.candidates[0].score is not None
        else:
            assert verdict.candidates[0].rejected_reason is not None


class TestEdgeQualityIsFairAcrossResolutions:
    """Regression guard for a real mis-ranking found on client PSDs, 17 Aug 2026.

    A 45 MP client packshot was delivered with a hard-edged Gemini cut-out because the auto-pick
    score ranked it above a real matte:

        gemini    score=0.7164   soft_alpha_ratio=0.0
        birefnet  score=0.4647   soft_alpha_ratio=0.00094

    Two independent defects, one test class:

    * A rasterised polygon has no transition band at all, yet scored ~0.50 on edge quality through
      the Gaussian's tail — half marks for discarding the edge, against invariant 2.
    * The 1.5px target was absolute. An engine that segments at 1024 and has its mask resized up
      to a 45 MP source shows a band 8x wider for the same quality, so the metric was ranking
      engines by output resolution. Measured widths: 1.95px at 900x1200 rising to 11.97px at
      5504x8256 — almost exactly the upscale factor each time.
    """

    @staticmethod
    def _disc(size: int, radius: int, band: float) -> np.ndarray:
        """A disc whose soft band is `band` pixels wide, at any canvas size."""
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        d = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
        return np.clip((radius - d) / max(band, 1e-6) + 0.5, 0.0, 1.0).astype(np.float32)

    def test_a_mask_with_no_soft_alpha_scores_zero_on_edge_quality(self):
        # Not "low" — zero. Soft alpha is unrecoverable once discarded, so there is nothing for
        # decontamination to fix and the halo against a saturated background is permanent.
        hard = (self._disc(400, 120, band=1.0) > 0.5).astype(np.float32)
        assert autopick._edge_quality(hard, (hard > 0.5).astype(np.uint8)) == 0.0

    def test_a_real_matte_beats_a_stencil(self):
        soft = self._disc(400, 120, band=2.0)
        hard = (soft > 0.5).astype(np.float32)
        v = autopick.choose(
            [result(soft, EngineId.PHOTOROOM), result(hard, EngineId.GEMINI)]
        )
        assert v.winner.engine is EngineId.PHOTOROOM
        soft_score = next(c.score for c in v.candidates if c.engine is EngineId.PHOTOROOM)
        hard_score = next(c.score for c in v.candidates if c.engine is EngineId.GEMINI)
        assert soft_score > hard_score

    def test_the_same_quality_of_edge_scores_alike_at_any_resolution(self):
        """The defect that let a hard mask win: quality must not track image size.

        The band is scaled with the canvas so every case is the *same* edge, just photographed
        larger — which is exactly what an engine's mask resized up to a big source looks like.
        """
        scores = []
        for size in (512, 1024, 3000, 6000):
            band = 1.5 * max(1.0, size / 1024.0)
            a = self._disc(size, size // 4, band=band)
            scores.append(autopick._edge_quality(a, (a > 0.5).astype(np.uint8)))
        assert min(scores) > 0.55, f"resolution changes the verdict: {scores}"
        assert max(scores) - min(scores) < 0.45, f"scores drift with size: {scores}"

    def test_a_small_image_is_not_asked_for_a_sub_pixel_edge(self):
        """The scale must never drop below 1.0.

        Scaling down for a small canvas made the target 0.29px on a 200px fixture, so a genuinely
        soft edge scored 0.05 and very nearly lost to a binary one. An edge cannot be finer than
        a pixel.
        """
        a = self._disc(200, 60, band=1.2)
        assert autopick._edge_quality(a, (a > 0.5).astype(np.uint8)) > 0.7

"""Engine layer tests: the control engine's mixture solve, and auto-pick selection.

No network. Vendor adapters are covered only for error classification — their wire behaviour needs
real keys and is marked `needs_keys`.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

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
        """What keeps procurement off the critical path.

        Every key AND the flagged engines' own gates explicitly forced off — a real backend/.env
        mid-experiment (GEMINI_EDIT_ENABLED=true, a real GEMINI_API_KEY) would otherwise leak
        through bare/partial Settings() and break hermeticity, since env vars outrank the class
        default in pydantic-settings' source order.
        """
        s = Settings(
            photoroom_api_key="",
            removebg_api_key="",
            fal_key="",
            huggingface_api_key="",
            gemini_edit_enabled=False,
        )
        pool = registry.select_pool(s, CutoutSpec())
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


class TestHuggingFaceEngine:
    """A normal commercial engine (see docs/ENGINES.md) — gated only by key presence, like
    Photoroom/remove.bg/fal.ai, not the extra locks GEMINI_EDIT needs."""

    def test_unconfigured_reports_unavailable(self):
        from app.engines.huggingface import HuggingFaceEngine

        assert not HuggingFaceEngine(Settings(huggingface_api_key="")).available()
        assert HuggingFaceEngine(Settings(huggingface_api_key="hf_x")).available()

    async def test_calling_an_unconfigured_engine_gives_a_typed_error(self):
        from app.core.errors import VendorUnauthorized
        from app.engines.huggingface import HuggingFaceEngine

        with pytest.raises(VendorUnauthorized):
            await HuggingFaceEngine(Settings(huggingface_api_key="")).alpha_for(b"", 10, 10)

    def test_in_the_default_engine_pool(self):
        """Unlike gemini_edit, this is a sanctioned commercial engine — listed by default like the
        other three, gated only by whether a key is actually set.

        Checks the field's declared default directly (not `Settings().engine_priority`) because a
        real `backend/.env` on the machine running these tests can override `ENGINE_POOL` — as it
        does here, since it's mid-experiment with GEMINI_EDIT_ENABLED. Bare `Settings()` reads that
        real file, not just the class default, so it is not hermetic for this assertion.
        """
        default_pool = Settings.model_fields["engine_pool"].default
        assert EngineId.HUGGINGFACE.value in default_pool.split(",")

    def test_registry_falls_back_to_local_when_no_huggingface_key(self):
        # gemini_edit_enabled forced off: an ambient backend/.env mid-experiment (this machine's
        # right now) sets GEMINI_EDIT_ENABLED=true + a real key, which would otherwise win the pool
        # instead of falling back to LOCAL — see _clean_state in test_api.py for the same issue.
        s = Settings(
            photoroom_api_key="",
            removebg_api_key="",
            fal_key="",
            huggingface_api_key="",
            gemini_edit_enabled=False,
        )
        assert [e.id for e in registry.select_pool(s, CutoutSpec())] == [EngineId.LOCAL]

    def test_registry_uses_it_when_it_is_the_only_key_set(self):
        # engine_pool forced explicitly too: an ambient backend/.env's ENGINE_POOL may not list
        # "huggingface" at all (it doesn't, on this machine mid-experiment), and select_pool only
        # ever considers engines present in the pool regardless of key availability.
        s = Settings(
            photoroom_api_key="",
            removebg_api_key="",
            fal_key="",
            huggingface_api_key="hf_x",
            gemini_edit_enabled=False,
            engine_pool="huggingface",
        )
        assert [e.id for e in registry.select_pool(s, CutoutSpec())] == [EngineId.HUGGINGFACE]

    def test_error_classification_matches_the_http_taxonomy(self):
        """Verified live: a real HfHubHTTPError carries `.response.status_code` — see
        docs/ENGINES.md for the actual call this was checked against."""
        from app.core.errors import VendorError, VendorRateLimited, VendorUnauthorized
        from app.engines.huggingface import _classify

        class _FakeResponse:
            def __init__(self, status_code):
                self.status_code = status_code

        class _FakeExc:
            def __init__(self, status_code):
                self.response = _FakeResponse(status_code)

        assert isinstance(_classify(_FakeExc(401)), VendorUnauthorized)
        assert isinstance(_classify(_FakeExc(429)), VendorRateLimited)
        assert isinstance(_classify(_FakeExc(500)), VendorError)
        assert isinstance(_classify(_FakeExc(400)), VendorError)

    def test_pick_alpha_extracts_the_single_segment(self):
        from app.engines.huggingface import _pick_alpha

        mask = Image.new("L", (10, 8), color=255)
        mask.putpixel((0, 0), 0)
        result = SimpleNamespace(label="foreground", score=0.98, mask=mask)

        alpha = _pick_alpha([result])
        assert alpha.shape == (8, 10)
        assert alpha.dtype == np.float32
        assert alpha[0, 0] == pytest.approx(0.0)
        assert alpha[4, 4] == pytest.approx(1.0)

    def test_pick_alpha_keeps_the_highest_score_when_multiple_segments_come_back(self):
        """Not independently verified against a real multi-segment response — see docs/ENGINES.md."""
        from app.engines.huggingface import _pick_alpha

        low = SimpleNamespace(label="a", score=0.2, mask=Image.new("L", (4, 4), color=0))
        high = SimpleNamespace(label="b", score=0.9, mask=Image.new("L", (4, 4), color=255))

        alpha = _pick_alpha([low, high])
        assert alpha.max() == pytest.approx(1.0)

    def test_pick_alpha_rejects_an_empty_result_list(self):
        from app.core.errors import VendorError
        from app.engines.huggingface import _pick_alpha

        with pytest.raises(VendorError):
            _pick_alpha([])


class TestGeminiEditEngine:
    """The prime-directive override — see docs/ENGINES.md. Every gate here must default closed."""

    def test_disabled_by_default(self):
        """Checks the field's declared default (not `GeminiEditEngine(Settings())`) — a real
        `backend/.env` can override GEMINI_EDIT_ENABLED, as it does on this machine mid-experiment,
        so bare `Settings()` is not hermetic for this assertion. See the huggingface test above."""
        assert Settings.model_fields["gemini_edit_enabled"].default is False

    def test_flag_alone_is_not_enough_without_a_key(self):
        from app.engines.gemini_edit import GeminiEditEngine

        s = Settings(gemini_edit_enabled=True, gemini_api_key="")
        assert not GeminiEditEngine(s).available()

    def test_key_alone_is_not_enough_without_the_flag(self):
        from app.engines.gemini_edit import GeminiEditEngine

        s = Settings(gemini_edit_enabled=False, gemini_api_key="k")
        assert not GeminiEditEngine(s).available()

    def test_available_only_with_both_flag_and_key(self):
        from app.engines.gemini_edit import GeminiEditEngine

        s = Settings(gemini_edit_enabled=True, gemini_api_key="k")
        assert GeminiEditEngine(s).available()

    async def test_calling_while_unavailable_gives_a_typed_error_not_a_network_call(self):
        from app.core.errors import VendorUnauthorized
        from app.engines.gemini_edit import GeminiEditEngine

        # Explicit kwargs, not bare Settings() — forces the unavailable state under test rather
        # than relying on ambient .env absence (see test_disabled_by_default above).
        s = Settings(gemini_edit_enabled=False, gemini_api_key="")
        with pytest.raises(VendorUnauthorized):
            await GeminiEditEngine(s).alpha_for(b"", 10, 10)

    def test_not_in_the_default_engine_pool(self):
        """Flipping the flag alone must not be enough to pull this into AUTO's pool.

        Checks the field's declared default directly, not `Settings().engine_priority` — see
        test_disabled_by_default above for why bare `Settings()` isn't hermetic here.
        """
        default_pool = Settings.model_fields["engine_pool"].default
        assert EngineId.GEMINI_EDIT.value not in default_pool.split(",")

    def test_registry_ignores_it_even_if_pooled_but_not_enabled(self):
        s = Settings(engine_pool="gemini_edit", gemini_edit_enabled=False, gemini_api_key="k")
        assert [e.id for e in registry.select_pool(s, CutoutSpec())] == [EngineId.LOCAL]

    def test_registry_uses_it_when_explicitly_pooled_and_enabled(self):
        s = Settings(engine_pool="gemini_edit", gemini_edit_enabled=True, gemini_api_key="k")
        pool = registry.select_pool(s, CutoutSpec())
        assert [e.id for e in pool] == [EngineId.GEMINI_EDIT]


class TestExtractImageBytes:
    """Pure parsing helper — no network. Tolerant by design, like vision.py's _parse_verdict."""

    def test_extracts_camel_case_inline_data(self):
        from app.engines.gemini_edit import _extract_image_bytes

        raw = b"not a real png, just bytes for the round-trip"
        import base64

        body = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "here you go"},
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": base64.b64encode(raw).decode("ascii"),
                                }
                            },
                        ]
                    }
                }
            ]
        }
        assert _extract_image_bytes(body) == raw

    def test_extracts_snake_case_inline_data(self):
        from app.engines.gemini_edit import _extract_image_bytes

        raw = b"other bytes"
        import base64

        body = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inline_data": {
                                    "mime_type": "image/png",
                                    "data": base64.b64encode(raw).decode("ascii"),
                                }
                            }
                        ]
                    }
                }
            ]
        }
        assert _extract_image_bytes(body) == raw

    def test_returns_none_when_no_image_part(self):
        from app.engines.gemini_edit import _extract_image_bytes

        body = {"candidates": [{"content": {"parts": [{"text": "I cannot edit this image."}]}}]}
        assert _extract_image_bytes(body) is None

    def test_returns_none_for_malformed_body(self):
        from app.engines.gemini_edit import _extract_image_bytes

        assert _extract_image_bytes({}) is None
        assert _extract_image_bytes({"candidates": []}) is None
        assert _extract_image_bytes("not even a dict") is None


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

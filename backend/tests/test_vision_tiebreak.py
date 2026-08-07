"""The vision tie-break: counterbalancing, and its wiring into the pipeline.

`vision.py` previously had no tests and `pipeline.py` never called it, so both the
position-bias guard and the fallback behaviour were unverified. These run offline against a mock
transport — the live model comparison that chose the default lives in docs/ENGINES.md.

The property that matters most here is **failing safe**: a tie-break is advisory, so every error
path must return the deterministic winner rather than failing the image.
"""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest

from app import pipeline
from app.core.settings import Settings
from app.engines import autopick
from app.engines.base import AlphaResult
from app.engines.vision import GeminiTiebreak, TiebreakUnavailable
from app.imaging import export as E
from app.models import EngineId, ImageState, JobConfig, Note, OutputFormat, SizeSpec
from tests.test_shadow import studio_scene


def scene():
    observed, alpha, _ = studio_scene(size=120)
    return observed, alpha.astype(np.float32)


def verdict_response(better: str, reason: str = "cleaner edge") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": json.dumps({"better": better, "reason": reason})}]}}
            ]
        },
    )


def settings(**kw) -> Settings:
    base = dict(
        _env_file=None,
        gemini_api_key="test-key",
        gemini_tiebreak_enabled=True,
        photoroom_api_key="",
        removebg_api_key="",
        fal_key="",
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def mock_gemini(monkeypatch):
    """Install a sequence of responses; the two counterbalanced calls consume them in order."""

    def install(*responses: httpx.Response):
        state = {"i": 0, "n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            state["n"] += 1
            r = responses[min(state["i"], len(responses) - 1)]
            state["i"] += 1
            return r

        real_init = httpx.AsyncClient.__init__

        def init(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            real_init(self, *a, **kw)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
        return state

    return install


class TestCounterbalancing:
    async def test_agreeing_orderings_return_a_winner(self, mock_gemini):
        """Forward says A, reverse says B — both mean 'the first candidate is better'."""
        state = mock_gemini(verdict_response("A"), verdict_response("B"))
        observed, alpha = scene()
        pairs = [(EngineId.PHOTOROOM, alpha), (EngineId.REMOVEBG, alpha * 0.5)]

        winner, reason = await GeminiTiebreak(settings()).pick(observed, pairs)

        assert winner is EngineId.PHOTOROOM
        assert reason == "cleaner edge"
        assert state["n"] == 2, "the pair must be judged in both orders"

    async def test_the_reverse_ordering_is_interpreted_swapped(self, mock_gemini):
        """Forward B and reverse A both mean 'the second candidate is better'."""
        mock_gemini(verdict_response("B"), verdict_response("A"))
        observed, alpha = scene()
        pairs = [(EngineId.PHOTOROOM, alpha), (EngineId.REMOVEBG, alpha * 0.5)]

        winner, _ = await GeminiTiebreak(settings()).pick(observed, pairs)
        assert winner is EngineId.REMOVEBG

    async def test_a_model_that_always_picks_A_is_rejected(self, mock_gemini):
        """The measured failure mode: 'A' in both orderings is position bias, not a verdict.

        gemini-3-flash-preview did exactly this in 4/4 live trials. Accepting it would launder a
        coin flip as judgement.
        """
        mock_gemini(verdict_response("A"), verdict_response("A"))
        observed, alpha = scene()
        pairs = [(EngineId.PHOTOROOM, alpha), (EngineId.REMOVEBG, alpha * 0.5)]

        with pytest.raises(TiebreakUnavailable, match="position bias"):
            await GeminiTiebreak(settings()).pick(observed, pairs)

    async def test_a_model_that_always_picks_B_is_also_rejected(self, mock_gemini):
        mock_gemini(verdict_response("B"), verdict_response("B"))
        observed, alpha = scene()
        pairs = [(EngineId.PHOTOROOM, alpha), (EngineId.REMOVEBG, alpha * 0.5)]

        with pytest.raises(TiebreakUnavailable, match="position bias"):
            await GeminiTiebreak(settings()).pick(observed, pairs)

    async def test_equivalent_in_either_direction_falls_back(self, mock_gemini):
        mock_gemini(verdict_response("A"), verdict_response("equivalent"))
        observed, alpha = scene()
        pairs = [(EngineId.PHOTOROOM, alpha), (EngineId.REMOVEBG, alpha * 0.5)]

        with pytest.raises(TiebreakUnavailable, match="no reliable difference"):
            await GeminiTiebreak(settings()).pick(observed, pairs)


class TestUnavailability:
    async def test_no_key_is_unavailable(self):
        assert GeminiTiebreak(settings(gemini_api_key="")).available() is False

    async def test_disabled_flag_is_unavailable(self):
        assert GeminiTiebreak(settings(gemini_tiebreak_enabled=False)).available() is False

    async def test_pick_without_a_key_raises_rather_than_calling_out(self, mock_gemini):
        state = mock_gemini(verdict_response("A"))
        observed, alpha = scene()
        with pytest.raises(TiebreakUnavailable):
            await GeminiTiebreak(settings(gemini_api_key="")).pick(
                observed, [(EngineId.PHOTOROOM, alpha), (EngineId.REMOVEBG, alpha)]
            )
        assert state["n"] == 0

    async def test_wrong_candidate_count_raises(self, mock_gemini):
        mock_gemini(verdict_response("A"))
        observed, alpha = scene()
        with pytest.raises(TiebreakUnavailable, match="expected 2"):
            await GeminiTiebreak(settings()).pick(observed, [(EngineId.PHOTOROOM, alpha)])

    async def test_an_http_error_falls_back(self, mock_gemini):
        mock_gemini(httpx.Response(503, text="unavailable"))
        observed, alpha = scene()
        with pytest.raises(TiebreakUnavailable):
            await GeminiTiebreak(settings()).pick(
                observed, [(EngineId.PHOTOROOM, alpha), (EngineId.REMOVEBG, alpha * 0.5)]
            )

    async def test_an_unparseable_body_falls_back(self, mock_gemini):
        mock_gemini(httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]}))
        observed, alpha = scene()
        with pytest.raises(TiebreakUnavailable):
            await GeminiTiebreak(settings()).pick(
                observed, [(EngineId.PHOTOROOM, alpha), (EngineId.REMOVEBG, alpha * 0.5)]
            )

    async def test_the_key_never_appears_in_an_error_message(self, mock_gemini):
        mock_gemini(httpx.Response(401, text="bad key: test-key"))
        observed, alpha = scene()
        try:
            await GeminiTiebreak(settings()).pick(
                observed, [(EngineId.PHOTOROOM, alpha), (EngineId.REMOVEBG, alpha * 0.5)]
            )
        except TiebreakUnavailable as exc:
            assert "test-key" not in str(exc)


# ---------------------------------------------------------------------------
# Pipeline wiring — this is what was missing entirely
# ---------------------------------------------------------------------------


class _FixedEngine:
    """Returns a preset mask, so autopick's scores can be forced into a tie."""

    cost_per_image_usd = 0.02

    def __init__(self, engine_id: EngineId, alpha: np.ndarray) -> None:
        self.id = engine_id
        self._alpha = alpha

    def available(self) -> bool:
        return True

    async def alpha_for(self, image_bytes: bytes, width: int, height: int) -> AlphaResult:
        return AlphaResult(alpha=self._alpha, engine=self.id, latency_ms=1, cost_usd=0.02)


class TestPipelineWiring:
    """A tie must actually reach the model, and every failure must fall back silently."""

    @staticmethod
    def _tied_engines():
        _, alpha = scene()
        # Identical masks guarantee identical scores, so choose() emits TIEBREAK_DETERMINISTIC.
        return [
            _FixedEngine(EngineId.PHOTOROOM, alpha.copy()),
            _FixedEngine(EngineId.REMOVEBG, alpha.copy()),
        ]

    @staticmethod
    def _png():
        observed, _ = scene()
        return E.encode(observed, None, fmt=OutputFormat.PNG)

    async def test_a_tie_is_flagged_deterministic_without_a_key(self):
        result, _ = await pipeline.process_image(
            self._png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            settings(gemini_api_key=""), engines=self._tied_engines(),
        )
        assert result.state is ImageState.DONE
        assert Note.TIEBREAK_DETERMINISTIC in result.notes
        assert Note.TIEBREAK_VISION not in result.notes

    async def test_an_agreeing_model_upgrades_the_note_to_vision(self, mock_gemini):
        mock_gemini(verdict_response("A"), verdict_response("B"))
        result, _ = await pipeline.process_image(
            self._png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            settings(), engines=self._tied_engines(),
        )
        assert result.state is ImageState.DONE
        assert Note.TIEBREAK_VISION in result.notes
        assert Note.TIEBREAK_DETERMINISTIC not in result.notes, (
            "the pick was judged, not measured — presenting it as measured would be a false claim"
        )

    async def test_a_biased_model_leaves_the_deterministic_note_intact(self, mock_gemini):
        mock_gemini(verdict_response("A"), verdict_response("A"))
        result, _ = await pipeline.process_image(
            self._png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            settings(), engines=self._tied_engines(),
        )
        assert result.state is ImageState.DONE
        assert Note.TIEBREAK_DETERMINISTIC in result.notes
        assert Note.TIEBREAK_VISION not in result.notes

    async def test_a_vision_failure_never_fails_the_image(self, mock_gemini):
        mock_gemini(httpx.Response(500, text="boom"))
        result, outputs = await pipeline.process_image(
            self._png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            settings(), engines=self._tied_engines(),
        )
        assert result.state is ImageState.DONE, "an advisory call must not fail an image"
        assert outputs
        assert Note.TIEBREAK_DETERMINISTIC in result.notes

    async def test_the_tiebreak_cost_is_recorded(self, mock_gemini):
        mock_gemini(verdict_response("A"), verdict_response("B"))
        result, _ = await pipeline.process_image(
            self._png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            settings(), engines=self._tied_engines(),
        )
        # Two engines at $0.02 plus the tie-break estimate.
        assert result.cost_usd == pytest.approx(0.04 + pipeline._TIEBREAK_COST_USD)

    async def test_no_tie_means_no_vision_call_at_all(self, mock_gemini):
        """The model must not be consulted when measurement already separated the candidates."""
        state = mock_gemini(verdict_response("A"), verdict_response("B"))
        _, alpha = scene()
        engines = [
            _FixedEngine(EngineId.PHOTOROOM, alpha.copy()),
            _FixedEngine(EngineId.REMOVEBG, np.zeros_like(alpha)),   # rejected outright
        ]
        result, _ = await pipeline.process_image(
            self._png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            settings(), engines=engines,
        )
        assert result.state is ImageState.DONE
        assert state["n"] == 0, "no tie, so no money spent on judgement"

    async def test_single_engine_never_triggers_a_vision_call(self, mock_gemini):
        state = mock_gemini(verdict_response("A"), verdict_response("B"))
        _, alpha = scene()
        result, _ = await pipeline.process_image(
            self._png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            settings(), engines=[_FixedEngine(EngineId.REMOVEBG, alpha)],
        )
        assert Note.SINGLE_ENGINE_ONLY in result.notes
        assert state["n"] == 0


class TestNeedsVisionTiebreak:
    def test_true_only_when_the_deterministic_note_is_present(self):
        v = autopick.Verdict(winner=None, candidates=[], notes=[Note.TIEBREAK_DETERMINISTIC])
        assert autopick.needs_vision_tiebreak(v) is True
        assert autopick.needs_vision_tiebreak(
            autopick.Verdict(winner=None, candidates=[], notes=[Note.SINGLE_ENGINE_ONLY])
        ) is False

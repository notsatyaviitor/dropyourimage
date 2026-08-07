"""Subject localisation: pick one object out of a multi-object scene.

Why this exists at all: background removal answers foreground-vs-background and has no notion of
*which* foreground. On a furnished-room photograph remove.bg returned a high-quality mask of the
sofa when the user wanted the coffee table — not a bad cut-out, a cut-out of the wrong object, which
is worse because nothing downstream can tell.

Offline against a mock transport. The live model verification is recorded in app/engines/locate.py.
"""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest

from app import pipeline
from app.core import errors
from app.core.settings import Settings
from app.engines.base import AlphaResult
from app.engines.cache import StorageCutoutCache
from app.engines.local import LocalEngine
from app.engines.locate import (
    GeminiLocator,
    SubjectNotLocated,
    _parse,
    _to_pixels,
    pad_and_clamp,
)
from app.imaging import export as E
from app.imaging.geometry import BBox
from app.models import (
    CutoutSpec,
    EngineId,
    ImageResult,
    ImageState,
    JobConfig,
    Note,
    OutputFormat,
    SizeSpec,
)
from app.storage.memory import MemoryStorage
from tests.test_shadow import busy_scene, studio_scene


def settings(**kw) -> Settings:
    base = dict(
        _env_file=None,
        gemini_api_key="test-key",
        photoroom_api_key="",
        removebg_api_key="",
        fal_key="",
    )
    base.update(kw)
    return Settings(**base)


def box_response(found: bool, box: list[int] | None = None, label: str = "x") -> httpx.Response:
    payload: dict = {"found": found, "label": label}
    if box is not None:
        payload["box_2d"] = box
    return httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]},
    )


@pytest.fixture
def mock_gemini(monkeypatch):
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


def scene_png(size: int = 200) -> bytes:
    observed, _, _ = studio_scene(size=size)
    return E.encode(observed, None, fmt=OutputFormat.PNG)


# ---------------------------------------------------------------------------
# Coordinate conversion — the highest-risk pure function here
# ---------------------------------------------------------------------------


class TestBoxConversion:
    def test_gemini_boxes_are_y_first(self):
        """[ymin, xmin, ymax, xmax], normalised 0-1000.

        Getting this backwards yields a plausible box in the wrong place, which is exactly the
        failure that is hard to spot by eye — so it is pinned.
        """
        box = _to_pixels([100, 200, 300, 800], 1000, 500)
        assert (box.x0, box.y0, box.x1, box.y1) == (200, 50, 800, 150)

    def test_reversed_min_max_is_sorted_not_trusted(self):
        """A negative-width box would crop to nothing."""
        box = _to_pixels([300, 800, 100, 200], 1000, 500)
        assert box.x0 < box.x1 and box.y0 < box.y1

    def test_out_of_range_values_are_clamped(self):
        box = _to_pixels([-50, -50, 2000, 2000], 400, 300)
        assert (box.x0, box.y0, box.x1, box.y1) == (0, 0, 400, 300)

    def test_the_real_measured_box_maps_correctly(self):
        """From the live run in locate.py's docstring: a 940x564 room photo."""
        box = _to_pixels([677, 441, 900, 697], 940, 564)
        assert 400 < box.x0 < 430 and 640 < box.x1 < 670
        assert 370 < box.y0 < 390 and 500 < box.y1 < 515


class TestPadding:
    def test_padding_is_relative_to_the_box_not_the_image(self):
        """A small object should get proportionally the same context as a large one."""
        small = pad_and_clamp(BBox(100, 100, 200, 200), 1000, 1000, 10.0)
        assert (small.x0, small.y0, small.x1, small.y1) == (90, 90, 210, 210)

    def test_padding_clamps_at_the_image_edge(self):
        box = pad_and_clamp(BBox(0, 0, 100, 100), 100, 100, 50.0)
        assert (box.x0, box.y0, box.x1, box.y1) == (0, 0, 100, 100)

    def test_zero_padding_is_a_no_op(self):
        box = pad_and_clamp(BBox(10, 20, 30, 40), 100, 100, 0.0)
        assert (box.x0, box.y0, box.x1, box.y1) == (10, 20, 30, 40)


class TestParse:
    def test_found_false_is_honoured(self):
        assert _parse(json.loads(box_response(False).content)) == (False, None, "x")

    def test_a_missing_box_is_none_not_a_crash(self):
        found, box, _ = _parse(json.loads(box_response(True).content))
        assert found is True and box is None

    def test_garbage_returns_none(self):
        assert _parse({"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}) is None
        assert _parse({}) is None
        assert _parse("nonsense") is None


# ---------------------------------------------------------------------------
# The locator
# ---------------------------------------------------------------------------


class TestGeminiLocator:
    async def test_returns_a_padded_box(self, mock_gemini):
        mock_gemini(box_response(True, [100, 200, 300, 800], "coffee table"))
        got = await GeminiLocator(settings()).locate(b"x", "coffee table", 1000, 500, padding_pct=10.0)

        assert got.raw_box == BBox(200, 50, 800, 150)
        assert got.box.x0 < got.raw_box.x0 and got.box.x1 > got.raw_box.x1
        assert got.label == "coffee table"

    async def test_not_found_raises_rather_than_inventing_a_box(self, mock_gemini):
        mock_gemini(box_response(False))
        with pytest.raises(SubjectNotLocated, match="not found"):
            await GeminiLocator(settings()).locate(b"x", "unicorn", 100, 100)

    async def test_no_key_raises_without_calling_out(self, mock_gemini):
        state = mock_gemini(box_response(True, [0, 0, 500, 500]))
        with pytest.raises(SubjectNotLocated):
            await GeminiLocator(settings(gemini_api_key="")).locate(b"x", "table", 100, 100)
        assert state["n"] == 0

    async def test_an_empty_phrase_raises(self, mock_gemini):
        with pytest.raises(SubjectNotLocated, match="empty"):
            await GeminiLocator(settings()).locate(b"x", "   ", 100, 100)

    async def test_http_failure_raises_the_typed_error(self, mock_gemini):
        mock_gemini(httpx.Response(500, text="boom"))
        with pytest.raises(SubjectNotLocated):
            await GeminiLocator(settings()).locate(b"x", "table", 100, 100)

    async def test_a_degenerate_box_is_rejected(self, mock_gemini):
        mock_gemini(box_response(True, [500, 500, 500, 500]))
        with pytest.raises(SubjectNotLocated, match="degenerate"):
            await GeminiLocator(settings()).locate(b"x", "table", 100, 100)

    async def test_the_key_never_leaks_into_the_error(self, mock_gemini):
        mock_gemini(httpx.Response(401, text="bad key test-key"))
        try:
            await GeminiLocator(settings()).locate(b"x", "table", 100, 100)
        except SubjectNotLocated as exc:
            assert "test-key" not in str(exc)


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------


class TestSubjectPromptInThePipeline:
    async def test_no_prompt_leaves_the_packshot_path_untouched(self, mock_gemini):
        state = mock_gemini(box_response(True, [0, 0, 500, 500]))
        result, outputs = await pipeline.process_image(
            scene_png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            settings(), engines=[LocalEngine()],
        )
        assert result.state is ImageState.DONE and outputs
        assert state["n"] == 0, "no prompt must not cost a locator call"
        assert Note.SUBJECT_LOCATED not in result.notes

    async def test_a_prompt_crops_before_segmenting_and_says_so(self, mock_gemini):
        mock_gemini(box_response(True, [250, 250, 750, 750]))
        cfg = JobConfig(
            cutout=CutoutSpec(subject_prompt="coffee table"),
            size=SizeSpec(width=200, height=200),
        )
        result, outputs = await pipeline.process_image(
            scene_png(), "a.png", cfg, settings(), engines=[LocalEngine()]
        )
        assert result.state is ImageState.DONE and outputs
        assert Note.SUBJECT_LOCATED in result.notes
        assert Note.SUBJECT_NOT_FOUND not in result.notes

    async def test_a_prompt_that_matches_nothing_warns_loudly(self, mock_gemini):
        """Falling back to whole-frame is right; doing it silently is not."""
        mock_gemini(box_response(False))
        cfg = JobConfig(
            cutout=CutoutSpec(subject_prompt="unicorn"), size=SizeSpec(width=200, height=200)
        )
        result, outputs = await pipeline.process_image(
            scene_png(), "a.png", cfg, settings(), engines=[LocalEngine()]
        )
        assert result.state is ImageState.DONE, "fallback must still deliver"
        assert outputs
        assert Note.SUBJECT_NOT_FOUND in result.notes
        assert Note.SUBJECT_LOCATED not in result.notes

    async def test_a_locator_outage_falls_back_rather_than_failing(self, mock_gemini):
        mock_gemini(httpx.Response(503, text="down"))
        cfg = JobConfig(
            cutout=CutoutSpec(subject_prompt="table"), size=SizeSpec(width=200, height=200)
        )
        result, _ = await pipeline.process_image(
            scene_png(), "a.png", cfg, settings(), engines=[LocalEngine()]
        )
        assert result.state is ImageState.DONE
        assert Note.SUBJECT_NOT_FOUND in result.notes

    async def test_the_mask_is_confined_to_the_located_region(self, mock_gemini):
        """Alpha must be zero outside the box — that is what makes centring pick the subject."""
        mock_gemini(box_response(True, [400, 400, 600, 600]))
        cfg = JobConfig(
            cutout=CutoutSpec(subject_prompt="thing", subject_padding_pct=0.0),
            size=SizeSpec(width=200, height=200),
        )

        captured = {}

        class Spy:
            id = EngineId.LOCAL
            cost_per_image_usd = 0.0

            def available(self):
                return True

            async def alpha_for(self, image_bytes, width, height):
                captured["size"] = (width, height)
                return AlphaResult(
                    alpha=np.ones((height, width), np.float32), engine=EngineId.LOCAL,
                    latency_ms=1, cost_usd=0.0,
                )

        result, _ = await pipeline.process_image(scene_png(200), "a.png", cfg, settings(), engines=[Spy()])
        assert result.state is ImageState.DONE
        # The box spans 40%..60% of a 200px image, so 40px on each axis — not the full frame.
        assert captured["size"] == (40, 40), "the engine must see only the ROI"

    async def test_prompt_and_roi_are_part_of_the_cache_key(self):
        """The same photo cropped to the table and to the sofa are different problems."""
        a = pipeline.cache_key(b"img", "removebg", BBox(0, 0, 10, 10))
        b = pipeline.cache_key(b"img", "removebg", BBox(50, 50, 90, 90))
        whole = pipeline.cache_key(b"img", "removebg")
        assert len({a, b, whole}) == 3

    async def test_a_blank_prompt_is_treated_as_no_prompt(self, mock_gemini):
        """An empty UI box means "packshot", not "find nothing"."""
        state = mock_gemini(box_response(True, [0, 0, 500, 500]))
        cfg = JobConfig(cutout=CutoutSpec(subject_prompt="   "), size=SizeSpec(width=200, height=200))
        assert cfg.cutout.subject_prompt is None
        result, _ = await pipeline.process_image(
            scene_png(), "a.png", cfg, settings(), engines=[LocalEngine()]
        )
        assert state["n"] == 0
        assert Note.SUBJECT_NOT_FOUND not in result.notes


# ---------------------------------------------------------------------------
# Busy-scene warning
# ---------------------------------------------------------------------------


class TestBusySceneWarning:
    def _result(self, uniformity, score, notes=()):
        from app.models import EngineCandidate

        r = ImageResult(source_name="x", state=ImageState.RUNNING)
        r.background_uniformity = uniformity
        r.candidates = [EngineCandidate(engine=EngineId.REMOVEBG, score=score)]
        r.notes = list(notes)
        return r

    def test_fires_on_a_busy_backdrop_with_a_weak_score(self):
        """The signature of the real failure: the exact numbers from the live room photo."""
        assert pipeline._looks_like_a_scene(self._result(0.0, 0.448), JobConfig()) is True

    def test_does_not_fire_on_a_studio_backdrop(self):
        assert pipeline._looks_like_a_scene(self._result(0.96, 0.448), JobConfig()) is False

    def test_does_not_fire_on_a_busy_backdrop_with_a_strong_score(self):
        """A lifestyle shot the user chose deliberately, cut out well, is not a problem."""
        assert pipeline._looks_like_a_scene(self._result(0.0, 0.92), JobConfig()) is False

    def test_is_suppressed_when_a_subject_prompt_succeeded(self):
        """The user already said which object they meant, and the crop enforced it."""
        r = self._result(0.0, 0.448, notes=[Note.SUBJECT_LOCATED])
        assert pipeline._looks_like_a_scene(r, JobConfig()) is False

    def test_absent_uniformity_does_not_fire(self):
        assert pipeline._looks_like_a_scene(self._result(None, 0.1), JobConfig()) is False

    async def test_an_unusable_mask_on_a_busy_scene_fails_rather_than_delivering(self):
        """End to end on the synthetic busy scene, no mocks.

        The offline control engine cannot key a busy scene, `autopick` rejects every candidate, and
        the image fails with the reasons attached. That is the desired outcome: better a failure the
        UI can explain than a confident asset of the wrong thing. `BUSY_SCENE` is for the harder
        case where a *commercial* engine returns a plausible-but-wrong mask that passes the reject
        rules — which is what happened on the real room photograph.
        """
        observed = busy_scene(size=160)[0]
        png = E.encode(observed, None, fmt=OutputFormat.PNG)
        result, outputs = await pipeline.process_image(
            png, "busy.png", JobConfig(size=SizeSpec(width=200, height=200)),
            settings(gemini_api_key=""), engines=[LocalEngine()],
        )
        assert result.state is ImageState.FAILED
        assert outputs == {}
        assert result.error is not None
        assert result.candidates, "the rejected candidate must still be reported"


# ---------------------------------------------------------------------------
# Error propagation — found by a real 402
# ---------------------------------------------------------------------------


class TestPoolErrorPropagation:
    """A 402 was once reported as "no segmentation engine was reachable", which reads like a
    network fault and cost a debugging round. When every engine fails, the reason must survive."""

    async def test_out_of_credits_survives_to_the_result(self):
        class Broke:
            id = EngineId.REMOVEBG
            cost_per_image_usd = 0.2

            def available(self):
                return True

            async def alpha_for(self, *a):
                raise errors.VendorOutOfCredits("removebg is out of credits — top it up.")

        result, outputs = await pipeline.process_image(
            scene_png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            settings(), engines=[Broke()],
        )
        assert result.state is ImageState.FAILED
        assert result.error.code.value == "vendor_out_of_credits"
        assert "credits" in result.error.message
        assert outputs == {}

    def test_actionable_errors_outrank_transient_ones(self):
        chosen = pipeline._most_informative(
            [errors.VendorTimeout("slow"), errors.VendorOutOfCredits("empty")]
        )
        assert isinstance(chosen, errors.VendorOutOfCredits)

    def test_a_bad_key_outranks_a_rate_limit(self):
        chosen = pipeline._most_informative(
            [errors.VendorRateLimited("429"), errors.VendorUnauthorized("401")]
        )
        assert isinstance(chosen, errors.VendorUnauthorized)

    def test_an_empty_failure_list_still_yields_a_typed_error(self):
        assert isinstance(pipeline._most_informative([]), errors.PipelineError)

    def test_one_engine_failing_does_not_fail_the_image(self):
        """The original behaviour must survive: a dead vendor degrades, it does not fail."""

        class Broke:
            id = EngineId.REMOVEBG
            cost_per_image_usd = 0.2

            def available(self):
                return True

            async def alpha_for(self, *a):
                raise errors.VendorOutOfCredits("empty")

        import asyncio

        result, outputs = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            pipeline.process_image(
                scene_png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
                settings(), engines=[Broke(), LocalEngine()],
            )
        )
        assert result.state is ImageState.DONE
        assert outputs

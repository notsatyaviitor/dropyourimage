"""Automatic subject detection.

The model finds the product and returns one box (`GeminiLocator.detect`); our own arithmetic then
checks the box is a plausible subject rather than the whole scene. These tests pin the wiring and
the deterministic guard, with no key and no network.

What is deliberately NOT tested: whether Gemini locates a real photograph correctly. That needs a
key and real images, and belongs to the `needs_keys` set.
"""

from __future__ import annotations

import pytest

from app.core.settings import Settings
from app.engines.locate import Located, box_is_plausible_subject
from app.imaging.geometry import BBox
from app.models import Note

W = H = 1000


def obj(label: str, x0: int, y0: int, x1: int, y1: int) -> Located:
    box = BBox(x0, y0, x1, y1)
    # raw_box is what pick_dominant scores — padding must not change which object wins, or the
    # padding percentage would quietly become a selection knob.
    return Located(box=box, raw_box=box, label=label)


class TestWiring:
    def test_enabled_by_default(self):
        # Read off the field, not an instance: Settings() merges backend/.env, so an instance
        # asserts whatever the developer running the suite happens to have configured.
        assert Settings.model_fields["auto_subject_enabled"].default is True

    def test_notes_exist_and_are_stable(self):
        # These strings reach the browser and get rendered as warnings; renaming one silently
        # turns a visible caveat into an unknown note the UI drops.
        assert Note.SUBJECT_AUTO_DETECTED.value == "subject_auto_detected"
        # SUBJECT_AUTO_AMBIGUOUS was removed with the enumerate-and-rank path it belonged to.
        # The model now returns one box, so there is no runner-up to be ambiguous against, and a
        # note that can never be emitted is worse than no note.
        assert not hasattr(Note, "SUBJECT_AUTO_AMBIGUOUS")

    @pytest.mark.asyncio
    async def test_disabled_setting_restores_the_untouched_whole_frame_path(self):
        from app.imaging import export as E
        from app.pipeline import _locate_subject
        from app.models import JobConfig

        import io

        import numpy as np
        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray(np.full((40, 40, 3), 220, np.uint8)).save(buf, "PNG")
        raw = buf.getvalue()

        roi, notes, label = await _locate_subject(
            raw,
            E.decode(raw),
            JobConfig(),
            Settings(auto_subject_enabled=False, s3_endpoint_url="memory", gemini_api_key=""),
        )
        assert (roi, notes, label) == (None, [], None)

    @pytest.mark.asyncio
    async def test_no_gemini_key_degrades_to_whole_frame_without_failing(self):
        # A packshot needs no crop, so "could not detect" is the correct outcome here and must
        # never surface as an error on the image.
        from app.imaging import export as E
        from app.pipeline import _locate_subject
        from app.models import JobConfig

        import io

        import numpy as np
        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray(np.full((40, 40, 3), 220, np.uint8)).save(buf, "PNG")
        raw = buf.getvalue()

        roi, notes, label = await _locate_subject(
            raw,
            E.decode(raw),
            JobConfig(),
            Settings(auto_subject_enabled=True, s3_endpoint_url="memory", gemini_api_key=""),
        )
        assert roi is None and label is None
        assert Note.SUBJECT_NOT_FOUND not in notes, "a packshot must not be reported as a failure"


# ---------------------------------------------------------------------------
# Pipeline integration — the behaviour change itself
# ---------------------------------------------------------------------------


class TestPlausibilityGuard:
    """Our arithmetic over the model's box. The model proposes, this disposes."""

    W = H = 1000

    def test_a_normal_product_box_is_accepted(self):
        assert box_is_plausible_subject(BBox(350, 250, 650, 750), self.W, self.H) is True

    def test_a_box_covering_the_whole_frame_is_rejected(self):
        # The model boxed the scene rather than a product in it. Cropping to that is a no-op at
        # best; at worst it disables a whole-frame path that was already correct.
        assert box_is_plausible_subject(BBox(0, 0, self.W, self.H), self.W, self.H) is False

    def test_a_speck_is_rejected(self):
        # A fitting, a printed label, a highlight — not what the order was for.
        assert box_is_plausible_subject(BBox(500, 500, 510, 510), self.W, self.H) is False

    def test_an_empty_box_is_rejected(self):
        assert box_is_plausible_subject(BBox(400, 400, 400, 400), self.W, self.H) is False

    def test_degenerate_frame_dimensions_do_not_raise(self):
        assert box_is_plausible_subject(BBox(0, 0, 10, 10), 0, 0) is False


class TestAutoDetectionInThePipeline:
    """With no prompt, the pipeline asks the model to find the product and crops to it."""

    @staticmethod
    def _detect_response(found=True, box=None, label="bottle"):
        import httpx

        payload = {"found": found, "label": label}
        if box is not None:
            payload["box_2d"] = box
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": __import__("json").dumps(payload)}]}}]}
        )

    @pytest.fixture
    def mock_gemini(self, monkeypatch):
        import httpx

        def install(*responses):
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

    def _settings(self, **kw):
        base = dict(
            _env_file=None,
            gemini_api_key="test-key",
            auto_subject_enabled=True,
            photoroom_api_key="",
            removebg_api_key="",
            fal_key="",
            s3_endpoint_url="memory",
        )
        base.update(kw)
        return Settings(**base)

    @pytest.mark.asyncio
    async def test_no_prompt_now_detects_crops_and_labels_the_result(self, mock_gemini):
        from app import pipeline
        from app.engines.local import LocalEngine
        from app.models import ImageState, JobConfig, SizeSpec
        from tests.test_subject_prompt import scene_png

        # box_2d is [ymin, xmin, ymax, xmax] normalised 0-1000 — Gemini's ordering. Getting it
        # backwards yields a plausible-looking box in the wrong place.
        state = mock_gemini(self._detect_response(True, [250, 250, 750, 750], "bottle"))
        result, outputs = await pipeline.process_image(
            scene_png(),
            "a.png",
            JobConfig(size=SizeSpec(width=200, height=200)),
            self._settings(),
            engines=[LocalEngine()],
        )
        assert result.state is ImageState.DONE and outputs
        assert state["n"] >= 1, "detection should have called the model"
        assert Note.SUBJECT_AUTO_DETECTED in result.notes
        assert result.subject_label == "bottle"

    @pytest.mark.asyncio
    async def test_found_false_is_the_packshot_answer_not_an_error(self, mock_gemini):
        # One product on a sweep needs no crop. "No product to isolate" must degrade quietly to
        # whole-frame, never surface as a failure on the image.
        from app import pipeline
        from app.engines.local import LocalEngine
        from app.models import ImageState, JobConfig, SizeSpec
        from tests.test_subject_prompt import scene_png

        mock_gemini(self._detect_response(found=False))
        result, outputs = await pipeline.process_image(
            scene_png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            self._settings(), engines=[LocalEngine()],
        )
        assert result.state is ImageState.DONE and outputs
        assert result.subject_label is None
        assert Note.SUBJECT_AUTO_DETECTED not in result.notes

    @pytest.mark.asyncio
    async def test_a_whole_frame_box_is_rejected_rather_than_cropped_to(self, mock_gemini):
        from app import pipeline
        from app.engines.local import LocalEngine
        from app.models import ImageState, JobConfig, SizeSpec
        from tests.test_subject_prompt import scene_png

        mock_gemini(self._detect_response(True, [0, 0, 1000, 1000], "room"))
        result, outputs = await pipeline.process_image(
            scene_png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            self._settings(), engines=[LocalEngine()],
        )
        assert result.state is ImageState.DONE and outputs
        assert Note.SUBJECT_AUTO_DETECTED not in result.notes
        assert result.subject_label is None

    @pytest.mark.asyncio
    async def test_a_vendor_failure_still_delivers_the_image(self, mock_gemini):
        import httpx

        from app import pipeline
        from app.engines.local import LocalEngine
        from app.models import ImageState, JobConfig, SizeSpec
        from tests.test_subject_prompt import scene_png

        mock_gemini(httpx.Response(503, text="upstream unavailable"))
        result, outputs = await pipeline.process_image(
            scene_png(), "a.png", JobConfig(size=SizeSpec(width=200, height=200)),
            self._settings(), engines=[LocalEngine()],
        )
        assert result.state is ImageState.DONE and outputs
        assert result.subject_label is None
        assert Note.SUBJECT_AUTO_DETECTED not in result.notes

    @pytest.mark.asyncio
    async def test_a_named_subject_still_wins_and_is_not_relabelled(self, mock_gemini):
        # subject_prompt is the exact path. It must not be second-guessed by detection, and the
        # user's own words must not be echoed back as if they were a finding.
        from app import pipeline
        from app.engines.local import LocalEngine
        from app.models import CutoutSpec, ImageState, JobConfig, SizeSpec
        from tests.test_subject_prompt import box_response, scene_png

        mock_gemini(box_response(True, [250, 250, 750, 750], label="coffee table"))
        result, outputs = await pipeline.process_image(
            scene_png(),
            "a.png",
            JobConfig(
                cutout=CutoutSpec(subject_prompt="coffee table"),
                size=SizeSpec(width=200, height=200),
            ),
            self._settings(),
            engines=[LocalEngine()],
        )
        assert result.state is ImageState.DONE and outputs
        assert Note.SUBJECT_LOCATED in result.notes
        assert Note.SUBJECT_AUTO_DETECTED not in result.notes
        assert result.subject_label is None


class TestUsesTheProductPrompt:
    """Regression guard for a real bug found on client images, 14-17 Aug 2026.

    Automatic detection first reused `_ENUMERATE_PROMPT`, which asks for "furniture and
    significant fixtures" with examples like "bed" and "pillow". On a studio packshot the model
    correctly answered "there is no furniture here", so detection silently did nothing on every
    product image — the exact case the feature exists for. Two real client PSDs came back with
    `subject_label: None` and the display stand still attached.

    The fix was to stop inventing a second path and reuse `locate`'s, which was already measured
    to work when the subject was typed by hand.
    """

    def test_the_auto_prompt_is_built_from_the_named_subject_prompt(self):
        from app.engines.locate import _AUTO_PROMPT, _ENUMERATE_PROMPT, _PROMPT

        assert _AUTO_PROMPT != _ENUMERATE_PROMPT
        # The clause that makes the named path work: a product sits ON its stand, so without
        # "nothing else" the box swallows the stand and the segmenter has no reason to drop it.
        assert "nothing else" in _PROMPT
        assert "nothing else" in _AUTO_PROMPT
        assert "{phrase}" not in _AUTO_PROMPT, "the auto prompt takes no phrase"

    def test_the_auto_prompt_excludes_the_things_that_hold_a_product_up(self):
        from app.engines.locate import _AUTO_PROMPT

        lowered = _AUTO_PROMPT.lower()
        for word in ("stand", "riser", "plinth", "clamp", "pole", "prop", "pedestal"):
            assert word in lowered, f"the auto prompt must exclude {word!r}"
        for word in ("shadow", "reflection", "backdrop"):
            assert word in lowered, f"the auto prompt must exclude {word!r}"

    @pytest.mark.asyncio
    async def test_auto_detection_calls_detect_not_the_enumerator(self, monkeypatch):
        """The wiring itself. Enumerating was the bug; this pins that it is not used here."""
        from app import pipeline
        from app.engines.locate import Located
        from app.imaging import export as E
        from app.imaging.geometry import BBox
        from app.models import JobConfig

        import io

        import numpy as np
        from PIL import Image

        called = {"detect": 0, "enumerate": 0}

        async def fake_detect(self, image_bytes, width, height, **kw):
            called["detect"] += 1
            box = BBox(10, 10, 30, 30)
            return Located(box=box, raw_box=box, label="bottle")

        async def fake_many(*a, **kw):
            called["enumerate"] += 1
            return []

        monkeypatch.setattr("app.engines.locate.GeminiLocator.detect", fake_detect)
        monkeypatch.setattr("app.engines.locate.locate_many", fake_many)

        buf = io.BytesIO()
        Image.fromarray(np.full((40, 40, 3), 220, np.uint8)).save(buf, "PNG")
        raw = buf.getvalue()

        roi, notes, label = await pipeline._locate_subject(
            raw,
            E.decode(raw),
            JobConfig(),
            Settings(
                _env_file=None, auto_subject_enabled=True,
                gemini_api_key="k", s3_endpoint_url="memory",
            ),
        )
        assert called == {"detect": 1, "enumerate": 0}
        assert label == "bottle"
        assert Note.SUBJECT_AUTO_DETECTED in notes

"""The Gemini segmentation adapter, against a mock transport.

No API key and no network: `httpx.MockTransport` intercepts the request so the *exact bytes we
would have sent* can be asserted. The live counterpart is `scripts/check_engine.py gemini`.

Two things here are worth more than the rest, because they are what a live probe found and no
amount of reading the docs would have:

* **Polygon vertices are y-first.** The published docs say ``[x, y]``. They are not. A transposed
  mask still looks like a mask, so only an asymmetric fixture catches it.
* **The same model returns three different vertex shapes across calls.** Nested pairs, one flat
  run wrapped in a list, and a bare flat run. All three must produce the same mask.
"""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest

from app.core import errors
from app.core.settings import Settings
from app.engines.gemini_segment import GeminiSegmentEngine
from app.engines.registry import select_pool
from app.models import CutoutSpec, EngineId, EngineStrategy, Note


def settings(**kw) -> Settings:
    base = dict(
        _env_file=None,
        gemini_api_key="test-key-not-real",
        photoroom_api_key="",
        removebg_api_key="",
        fal_key="",
        vendor_max_retries=3,
    )
    base.update(kw)
    return Settings(**base)


def reply(entries: object) -> httpx.Response:
    """A generateContent response whose single text part carries `entries` as JSON."""
    body = {"candidates": [{"content": {"parts": [{"text": json.dumps(entries)}]}}]}
    return httpx.Response(200, json=body)


#: A square occupying the middle of the frame, as nested y-first pairs.
SQUARE_PAIRS = [[250, 250], [250, 750], [750, 750], [750, 250]]

#: Deliberately asymmetric: wide in x, short in y. If the adapter reads these as (x, y) the mask
#: comes back transposed — tall and narrow — which is the whole point of the fixture.
WIDE_PAIRS = [[100, 200], [100, 900], [400, 900], [400, 200]]


def entry(mask: object, label: str = "product") -> dict:
    return {"box_2d": [250, 250, 750, 750], "mask": mask, "label": label}


class Recorder:
    """Captures the outgoing request so the wire format can be asserted."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    @property
    def payload(self) -> dict:
        return json.loads(self.requests[0].content)


@pytest.fixture
def patch_transport(monkeypatch):
    """Route the adapter's httpx.AsyncClient through a MockTransport."""

    def install(recorder: Recorder):
        real_init = httpx.AsyncClient.__init__

        def init(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(recorder)
            real_init(self, *a, **kw)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
        return recorder

    return install


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


class TestWireFormat:
    async def test_posts_to_the_configured_model_endpoint(self, patch_transport):
        rec = patch_transport(Recorder(reply([entry(SQUARE_PAIRS)])))
        await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

        req = rec.requests[0]
        assert req.method == "POST"
        assert req.url.path == "/v1beta/models/gemini-3.5-flash:generateContent"

    async def test_the_model_id_comes_from_gemini_segment_model_not_gemini_model(
        self, patch_transport
    ):
        """The two settings are separate so tuning the tie-break cannot retune segmentation."""
        rec = patch_transport(Recorder(reply([entry(SQUARE_PAIRS)])))
        s = settings(gemini_model="gemini-3.6-flash", gemini_segment_model="gemini-3.5-flash")
        await GeminiSegmentEngine(s).alpha_for(b"png", 100, 100)

        assert "gemini-3.5-flash" in rec.requests[0].url.path

    async def test_thinking_is_minimal(self, patch_transport):
        """Load-bearing, not a tuning knob: it is the difference between ~2s and minutes."""
        rec = patch_transport(Recorder(reply([entry(SQUARE_PAIRS)])))
        await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

        cfg = rec.payload["generationConfig"]
        assert cfg["thinkingConfig"]["thinkingLevel"] == "MINIMAL"
        assert cfg["temperature"] == 0.0
        assert cfg["responseMimeType"] == "application/json"

    async def test_uses_the_segment_timeout_not_the_advisory_one(self):
        s = settings(gemini_timeout_seconds=15.0, gemini_segment_timeout_seconds=60.0)
        assert GeminiSegmentEngine(s)._timeout_seconds == 60.0


class TestKeySafety:
    async def test_key_is_not_in_the_body(self, patch_transport):
        rec = patch_transport(Recorder(reply([entry(SQUARE_PAIRS)])))
        await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

        assert "test-key-not-real" not in rec.requests[0].content.decode("utf-8")

    async def test_no_key_means_no_request_is_made(self, patch_transport):
        rec = patch_transport(Recorder(reply([entry(SQUARE_PAIRS)])))
        with pytest.raises(errors.VendorUnauthorized):
            await GeminiSegmentEngine(settings(gemini_api_key="")).alpha_for(b"png", 100, 100)

        assert rec.requests == []

    async def test_key_never_leaks_into_an_error_message(self, patch_transport):
        patch_transport(Recorder(httpx.Response(500)))
        with pytest.raises(errors.PipelineError) as exc:
            await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

        assert "test-key-not-real" not in str(exc.value)


# ---------------------------------------------------------------------------
# Polygon parsing — the part a live probe found and the docs got wrong
# ---------------------------------------------------------------------------


class TestPolygonShapes:
    @pytest.mark.parametrize(
        "mask",
        [
            pytest.param(SQUARE_PAIRS, id="nested-pairs"),
            pytest.param([[250, 250, 250, 750, 750, 750, 750, 250]], id="wrapped-flat-run"),
            pytest.param([250, 250, 250, 750, 750, 750, 750, 250], id="bare-flat-run"),
        ],
    )
    async def test_all_three_observed_shapes_give_the_same_mask(self, patch_transport, mask):
        patch_transport(Recorder(reply([entry(mask)])))
        alpha = await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

        assert alpha.alpha.shape == (100, 100)
        # The square spans 250-750 of 1000, i.e. the middle half of each axis.
        assert alpha.alpha[50, 50] == pytest.approx(1.0)
        assert alpha.alpha[5, 5] == pytest.approx(0.0)
        assert 0.20 < float(alpha.alpha.mean()) < 0.30

    async def test_vertices_are_y_first_not_x_first(self, patch_transport):
        """The docs say [x, y]. Measured: [y, x]. A transposed mask still looks like a mask."""
        patch_transport(Recorder(reply([entry(WIDE_PAIRS)])))
        alpha = (await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)).alpha

        rows = np.flatnonzero(alpha.any(axis=1))
        cols = np.flatnonzero(alpha.any(axis=0))
        height = rows[-1] - rows[0]
        width = cols[-1] - cols[0]
        # y spans 100-400, x spans 200-900 -> the shape must be wider than it is tall.
        assert width > height, "vertices were read x-first; the mask came back transposed"

    async def test_multiple_entries_are_unioned(self, patch_transport):
        """'Remove the background' means keep all the foreground, not just the first polygon."""
        second = [[100, 100], [100, 200], [200, 200], [200, 100]]
        patch_transport(Recorder(reply([entry(SQUARE_PAIRS), entry(second, "handle")])))
        alpha = (await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)).alpha

        assert alpha[50, 50] == pytest.approx(1.0)   # the main square
        assert alpha[15, 15] == pytest.approx(1.0)   # the second polygon

    async def test_mask_is_hard_edged_and_that_is_recorded_honestly(self, patch_transport):
        """Not a defect to fix here — the engine's documented trade. Assert it so it stays visible."""
        patch_transport(Recorder(reply([entry(SQUARE_PAIRS)])))
        alpha = (await GeminiSegmentEngine(settings()).alpha_for(b"png", 200, 200)).alpha

        assert sorted(np.unique(alpha).tolist()) == [0.0, 1.0]
        assert int(np.count_nonzero((alpha > 0.03) & (alpha < 0.97))) == 0


class TestUnusableResponses:
    async def test_an_rle_mask_string_raises_and_names_the_setting(self, patch_transport):
        """gemini-3.6-flash returns COCO RLE. Guessing at it would be worse than failing."""
        patch_transport(Recorder(reply([entry("i0n1:;c222N4K0000001O0001N2O001O1N101N101N2M2N2N2M2M3")])))
        with pytest.raises(errors.VendorError) as exc:
            await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

        assert "GEMINI_SEGMENT_MODEL" in str(exc.value)

    async def test_no_entries_is_no_foreground_and_is_not_retried(self, patch_transport):
        rec = patch_transport(Recorder(reply([])))
        with pytest.raises(errors.NoForegroundFound) as exc:
            await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

        assert exc.value.retryable is False
        assert len(rec.requests) == 1

    async def test_malformed_inner_json_degrades_to_no_foreground(self, patch_transport):
        body = {"candidates": [{"content": {"parts": [{"text": "[{\"mask\": [1,2,"}]}}]}
        patch_transport(Recorder(httpx.Response(200, json=body)))
        with pytest.raises(errors.NoForegroundFound):
            await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

    async def test_a_degenerate_polygon_does_not_silently_return_an_empty_mask(
        self, patch_transport
    ):
        patch_transport(Recorder(reply([entry([[10, 10], [20, 20]])])))
        with pytest.raises(errors.VendorError):
            await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)


class TestErrorClassification:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (429, errors.VendorRateLimited),
            (402, errors.VendorOutOfCredits),
            (401, errors.VendorUnauthorized),
            (403, errors.VendorUnauthorized),
            (500, errors.VendorError),
            (503, errors.VendorError),
        ],
    )
    async def test_status_maps_to_the_shared_taxonomy(self, patch_transport, status, expected):
        patch_transport(Recorder(httpx.Response(status)))
        with pytest.raises(expected):
            await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

    async def test_a_401_is_not_retried(self, patch_transport):
        rec = patch_transport(Recorder(httpx.Response(401)))
        with pytest.raises(errors.VendorUnauthorized):
            await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

        assert len(rec.requests) == 1

    async def test_a_429_is_retried_and_can_succeed(self, patch_transport):
        rec = patch_transport(
            Recorder(httpx.Response(429), reply([entry(SQUARE_PAIRS)]))
        )
        result = await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

        assert len(rec.requests) == 2
        assert result.alpha[50, 50] == pytest.approx(1.0)


class TestResultMetadata:
    async def test_records_engine_cost_and_latency(self, patch_transport):
        patch_transport(Recorder(reply([entry(SQUARE_PAIRS)])))
        result = await GeminiSegmentEngine(settings()).alpha_for(b"png", 100, 100)

        assert result.engine is EngineId.GEMINI
        assert result.cost_usd == pytest.approx(0.0065)
        assert result.latency_ms >= 0
        assert result.cache_hit is False
        assert result.alpha.dtype == np.float32


# ---------------------------------------------------------------------------
# Selection — reachable by name, never auto-picked
# ---------------------------------------------------------------------------


class TestSelection:
    def test_single_strategy_reaches_gemini(self):
        pool = select_pool(
            settings(), CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.GEMINI)
        )
        assert [e.id for e in pool] == [EngineId.GEMINI]

    def test_auto_never_selects_gemini_even_with_a_key(self):
        """The measured reason: a 0.7246 score passes every reject rule, so it could win on score
        while shipping the worse edge. Exclusion lives in ENGINE_POOL, not in select_pool."""
        s = settings(photoroom_api_key="p", fal_key="f")
        pool = select_pool(s, CutoutSpec(strategy=EngineStrategy.AUTO))

        assert EngineId.GEMINI not in [e.id for e in pool]

    def test_gemini_is_not_in_the_default_engine_pool(self):
        assert "gemini" not in Settings(_env_file=None).engine_pool

    def test_a_gemini_only_deployment_still_falls_back_to_local_for_auto(self):
        """A key but no pool entry must not leave AUTO with nothing to run."""
        pool = select_pool(settings(), CutoutSpec(strategy=EngineStrategy.AUTO))
        assert [e.id for e in pool] == [EngineId.LOCAL]


class TestPipelineWiring:
    async def test_choosing_gemini_emits_the_hard_edged_note(self, patch_transport):
        from app import pipeline
        from app.models import JobConfig
        from tests.test_shadow import studio_scene

        patch_transport(Recorder(reply([entry(SQUARE_PAIRS)])))
        observed, _, _ = studio_scene(size=100)

        config = JobConfig(cutout=CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.GEMINI))
        result, _ = await pipeline.process_image(
            _png_of(observed), "a.png", config, settings()
        )

        assert Note.HARD_EDGED_MASK in result.notes
        assert result.chosen_engine is EngineId.GEMINI

    async def test_photoroom_does_not_emit_the_hard_edged_note(self, patch_transport):
        from app import pipeline
        from tests.test_shadow import studio_scene
        from app.models import JobConfig
        from tests.test_removebg import cutout_png

        patch_transport(Recorder(httpx.Response(200, content=cutout_png(100))))
        observed, _, _ = studio_scene(size=100)

        config = JobConfig(
            cutout=CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.PHOTOROOM)
        )
        result, _ = await pipeline.process_image(
            _png_of(observed), "a.png", config, settings(photoroom_api_key="p")
        )

        assert Note.HARD_EDGED_MASK not in result.notes


def _png_of(observed: np.ndarray) -> bytes:
    import io

    from PIL import Image

    srgb8 = np.clip(observed ** (1 / 2.2) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(srgb8).save(buf, "PNG")
    return buf.getvalue()

"""The remove.bg adapter, against a mock transport.

These run with no API key and no network: `httpx.MockTransport` intercepts the request so we can
assert on the *exact bytes we would have sent*. That is the point — the adapter was written from
documentation and the wire parameters are the part most likely to be wrong. A live check against a
real key is `scripts/check_removebg.py`, which costs a credit; this costs nothing and runs in CI.
"""

from __future__ import annotations

import io

import httpx
import numpy as np
import pytest
from PIL import Image

from app.core import errors
from app.core.settings import Settings
from app.engines import http as H
from app.engines.registry import select_pool
from app.models import CutoutSpec, EngineId, EngineStrategy


def cutout_png(size: int = 64, *, soft: bool = True) -> bytes:
    """An RGBA cut-out of the shape remove.bg returns: product opaque, backdrop transparent."""
    rgba = np.zeros((size, size, 4), np.uint8)
    rgba[16:48, 16:48, :3] = (200, 30, 30)
    rgba[16:48, 16:48, 3] = 255
    if soft:
        rgba[16:48, 48:52, :3] = (200, 30, 30)
        rgba[16:48, 48:52, 3] = 128          # a partial-coverage edge band
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def settings(**kw) -> Settings:
    base = dict(
        _env_file=None,
        removebg_api_key="test-key-not-real",
        photoroom_api_key="",
        fal_key="",
        vendor_max_retries=3,
    )
    base.update(kw)
    return Settings(**base)


class Recorder:
    """Captures the outgoing request so the wire format can be asserted."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    @property
    def body(self) -> str:
        return self.requests[0].content.decode("latin-1")


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
    async def test_posts_to_the_documented_endpoint_with_the_documented_header(self, patch_transport):
        rec = patch_transport(Recorder(httpx.Response(200, content=cutout_png())))
        await H.RemoveBgEngine(settings()).alpha_for(cutout_png(), 64, 64)

        req = rec.requests[0]
        assert str(req.url) == "https://api.remove.bg/v1.0/removebg"
        assert req.method == "POST"
        assert req.headers["X-Api-Key"] == "test-key-not-real"

    async def test_sends_the_image_under_image_file(self, patch_transport):
        rec = patch_transport(Recorder(httpx.Response(200, content=cutout_png())))
        await H.RemoveBgEngine(settings()).alpha_for(cutout_png(), 64, 64)
        assert 'name="image_file"' in rec.body

    @pytest.mark.parametrize(
        "field,value",
        [("type", "product"), ("semitransparency", "true"), ("format", "png")],
    )
    async def test_sends_each_documented_parameter(self, patch_transport, field, value):
        """`size=full` was the previous value: a deprecated alias capped at 25 MP.

        `type=product` tells the vendor this is a packshot rather than making it guess, and
        `semitransparency=true` is stated rather than inherited so a change in the vendor's default
        cannot silently harden our edges — soft alpha is invariant 2 here.
        """
        rec = patch_transport(Recorder(httpx.Response(200, content=cutout_png())))
        await H.RemoveBgEngine(settings()).alpha_for(cutout_png(), 64, 64)

        body = rec.body
        assert f'name="{field}"' in body
        marker = body.split(f'name="{field}"', 1)[1]
        assert value in marker[: len(value) + 8]

class TestSizeSelection:
    """`preview` is free and, below 0.25 MP, identical to `auto`. Above it, `auto` is required.

    Measured live: a 268x142 subject crop requested at `preview` came back 268x142 — the same mask
    `auto` would have produced, for a free call instead of a paid credit. Asking for `auto` there
    buys nothing and spends money, which matters because a subject-prompt workflow crops small and
    gets iterated on repeatedly.
    """

    @staticmethod
    def _png(w: int, h: int) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "PNG")
        return buf.getvalue()

    @pytest.mark.parametrize(
        "w,h,expected",
        [
            (268, 142, "preview"),      # the real measured ROI crop — 0.038 MP
            (500, 500, "preview"),      # exactly 0.25 MP, at the ceiling
            (501, 500, "auto"),         # one pixel over
            (940, 564, "auto"),         # the full room photo — 0.53 MP
            (4000, 3000, "auto"),
        ],
    )
    def test_size_is_chosen_by_pixel_count(self, w, h, expected):
        assert H.size_for(self._png(w, h)) == expected

    def test_undecodable_bytes_default_to_paid_full_resolution(self):
        """Paying is the safe error; the alternative silently degrades every mask."""
        assert H.size_for(b"not an image") == "auto"

    async def test_a_small_image_actually_sends_preview(self, patch_transport):
        rec = patch_transport(Recorder(httpx.Response(200, content=cutout_png())))
        await H.RemoveBgEngine(settings()).alpha_for(self._png(200, 200), 200, 200)
        body = rec.body
        marker = body.split('name="size"', 1)[1]
        assert "preview" in marker[:20]

    async def test_a_large_image_actually_sends_auto(self, patch_transport):
        rec = patch_transport(Recorder(httpx.Response(200, content=cutout_png())))
        await H.RemoveBgEngine(settings()).alpha_for(self._png(1200, 900), 1200, 900)
        body = rec.body
        marker = body.split('name="size"', 1)[1]
        assert "auto" in marker[:20]


class TestKeySafety:
    async def test_the_key_is_never_placed_in_the_url_or_body(self, patch_transport):
        rec = patch_transport(Recorder(httpx.Response(200, content=cutout_png())))
        await H.RemoveBgEngine(settings()).alpha_for(cutout_png(), 64, 64)
        assert "test-key-not-real" not in str(rec.requests[0].url)
        assert "test-key-not-real" not in rec.body


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------


class TestResponseHandling:
    async def test_keeps_only_alpha_and_sizes_it_to_the_source(self, patch_transport):
        """Vendors cap resolution on lower tiers; the mask must be resized to the source."""
        patch_transport(Recorder(httpx.Response(200, content=cutout_png(size=32))))
        result = await H.RemoveBgEngine(settings()).alpha_for(cutout_png(size=200), 200, 200)

        assert result.alpha.shape == (200, 200)
        assert result.alpha.dtype == np.float32
        assert 0.0 <= float(result.alpha.min()) and float(result.alpha.max()) <= 1.0

    async def test_soft_alpha_survives_and_is_not_thresholded(self, patch_transport):
        patch_transport(Recorder(httpx.Response(200, content=cutout_png(soft=True))))
        result = await H.RemoveBgEngine(settings()).alpha_for(cutout_png(), 64, 64)

        a = result.alpha
        assert ((a > 0.01) & (a < 0.99)).any(), "partial coverage must be preserved (invariant 2)"

    async def test_records_engine_cost_and_latency(self, patch_transport):
        patch_transport(Recorder(httpx.Response(200, content=cutout_png())))
        result = await H.RemoveBgEngine(settings()).alpha_for(cutout_png(), 64, 64)

        assert result.engine is EngineId.REMOVEBG
        assert result.cost_usd == 0.20
        assert result.cache_hit is False
        assert result.latency_ms >= 0

    async def test_a_missing_key_fails_before_any_request_is_made(self, patch_transport):
        rec = patch_transport(Recorder(httpx.Response(200, content=cutout_png())))
        with pytest.raises(errors.VendorUnauthorized):
            await H.RemoveBgEngine(settings(removebg_api_key="")).alpha_for(cutout_png(), 64, 64)
        assert rec.requests == [], "no credit may be spent when no key is configured"


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    @pytest.mark.parametrize(
        "status,exc",
        [
            (401, errors.VendorUnauthorized),
            # 402 is its own error: the key is valid, the account is empty. Top up vs fix the key
            # are different actions, and conflating them cost a real debugging round.
            (402, errors.VendorOutOfCredits),
            (403, errors.VendorUnauthorized),
            (400, errors.VendorError),
            (500, errors.VendorError),
        ],
    )
    async def test_status_maps_to_the_taxonomy(self, patch_transport, status, exc):
        patch_transport(Recorder(httpx.Response(status, json={"errors": [{"title": "nope"}]})))
        with pytest.raises(exc):
            await H.RemoveBgEngine(settings(vendor_max_retries=1)).alpha_for(cutout_png(), 64, 64)

    async def test_a_401_is_not_retried(self, patch_transport):
        """Retrying a rejected key just burns the demo's time budget."""
        rec = patch_transport(Recorder(httpx.Response(401, json={})))
        with pytest.raises(errors.VendorUnauthorized):
            await H.RemoveBgEngine(settings(vendor_max_retries=3)).alpha_for(cutout_png(), 64, 64)
        assert len(rec.requests) == 1

    async def test_a_429_is_retried_and_can_succeed(self, patch_transport):
        rec = patch_transport(
            Recorder(
                httpx.Response(429, headers={"Retry-After": "0"}, json={}),
                httpx.Response(200, content=cutout_png()),
            )
        )
        result = await H.RemoveBgEngine(settings()).alpha_for(cutout_png(), 64, 64)
        assert result.alpha.shape == (64, 64)
        assert len(rec.requests) == 2

    async def test_unknown_foreground_is_its_own_error(self, patch_transport):
        """remove.bg's real 400 body for a crop with no figure/ground separation.

        Seen live on a 402x81 strip of a TV cabinet. Reported as itself because the user action is
        specific — widen the padding or rename the subject — and it must be non-retryable, since the
        same bytes fail identically every time.
        """
        rec = patch_transport(
            Recorder(
                httpx.Response(
                    400,
                    json={"errors": [{"title": "Could not identify foreground in image.",
                                      "code": "unknown_foreground"}]},
                )
            )
        )
        with pytest.raises(errors.NoForegroundFound) as exc:
            await H.RemoveBgEngine(settings(vendor_max_retries=3)).alpha_for(cutout_png(), 64, 64)

        assert exc.value.retryable is False
        assert len(rec.requests) == 1, "a deterministic 400 must not be retried"
        assert "padding" in exc.value.message or "subject" in exc.value.message

    async def test_an_unrecognised_400_stays_a_generic_vendor_error(self, patch_transport):
        patch_transport(Recorder(httpx.Response(400, json={"errors": [{"code": "something_new"}]})))
        with pytest.raises(errors.VendorError):
            await H.RemoveBgEngine(settings(vendor_max_retries=1)).alpha_for(cutout_png(), 64, 64)

    async def test_an_unparseable_400_body_does_not_misclassify(self, patch_transport):
        """A body we cannot read must never become a *wrong* specific error."""
        patch_transport(Recorder(httpx.Response(400, text="<html>gateway</html>")))
        with pytest.raises(errors.VendorError):
            await H.RemoveBgEngine(settings(vendor_max_retries=1)).alpha_for(cutout_png(), 64, 64)

    def test_vendor_code_extraction_is_tolerant(self):
        assert H._vendor_error_code(httpx.Response(400, json={"errors": [{"code": "x"}]})) == "x"
        assert H._vendor_error_code(httpx.Response(400, text="not json")) == ""
        assert H._vendor_error_code(httpx.Response(400, json={"errors": []})) == ""
        assert H._vendor_error_code(httpx.Response(400, json=["a"])) == ""

    async def test_error_messages_never_leak_the_key(self, patch_transport):
        patch_transport(Recorder(httpx.Response(401, json={})))
        try:
            await H.RemoveBgEngine(settings(vendor_max_retries=1)).alpha_for(cutout_png(), 64, 64)
        except errors.PipelineError as exc:
            assert "test-key-not-real" not in exc.message
            assert "test-key-not-real" not in exc.to_info().message


class TestRetryAfter:
    """remove.bg's published limit scales down with megapixels, so it tells us how long to wait."""

    def test_parses_a_delta_in_seconds(self):
        r = httpx.Response(429, headers={"Retry-After": "7"})
        assert H._retry_after_seconds(r) == 7.0

    def test_parses_an_http_date(self):
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone

        when = datetime.now(timezone.utc) + timedelta(seconds=20)
        r = httpx.Response(429, headers={"Retry-After": format_datetime(when)})
        got = H._retry_after_seconds(r)
        assert got is not None and 10 <= got <= 25

    def test_absent_header_returns_none_so_backoff_is_used(self):
        assert H._retry_after_seconds(httpx.Response(429)) is None

    def test_a_malformed_header_is_ignored_rather_than_crashing(self):
        assert H._retry_after_seconds(httpx.Response(429, headers={"Retry-After": "soon"})) is None

    def test_a_past_date_clamps_to_zero(self):
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone

        when = datetime.now(timezone.utc) - timedelta(seconds=60)
        r = httpx.Response(429, headers={"Retry-After": format_datetime(when)})
        assert H._retry_after_seconds(r) == 0.0

    async def test_the_adapter_sleeps_for_the_hinted_duration(self, patch_transport, monkeypatch):
        """The header must actually reach asyncio.sleep, not merely be parsed and discarded."""
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        patch_transport(
            Recorder(
                httpx.Response(429, headers={"Retry-After": "7"}, json={}),
                httpx.Response(200, content=cutout_png()),
            )
        )
        await H.RemoveBgEngine(settings()).alpha_for(cutout_png(), 64, 64)

        assert slept == [7.0], "the vendor's own Retry-After must win over our backoff"

    async def test_without_the_header_it_falls_back_to_exponential_backoff(
        self, patch_transport, monkeypatch
    ):
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        patch_transport(
            Recorder(
                httpx.Response(429, json={}),
                httpx.Response(200, content=cutout_png()),
            )
        )
        await H.RemoveBgEngine(settings()).alpha_for(cutout_png(), 64, 64)

        assert slept == [0.5], "first backoff step, since the vendor gave no hint"

    async def test_an_absurd_retry_after_is_capped(self, patch_transport, monkeypatch):
        """A vendor may ask for minutes. A demo cannot wait; fail one image, not the batch."""
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        patch_transport(
            Recorder(
                httpx.Response(429, headers={"Retry-After": "600"}, json={}),
                httpx.Response(200, content=cutout_png()),
            )
        )
        await H.RemoveBgEngine(settings()).alpha_for(cutout_png(), 64, 64)

        assert slept == [H._MAX_RETRY_AFTER_S]
        assert H._MAX_RETRY_AFTER_S <= 30.0


# ---------------------------------------------------------------------------
# Pool selection with only this key
# ---------------------------------------------------------------------------


class TestPoolWithOnlyRemoveBg:
    def test_a_removebg_only_key_is_selected_automatically(self):
        s = settings(engine_pool="removebg,photoroom,falai")
        pool = select_pool(s, CutoutSpec())
        assert [e.id for e in pool] == [EngineId.REMOVEBG]

    def test_it_is_still_selected_when_listed_last(self):
        """Priority order only matters when several keys are present."""
        s = settings(engine_pool="photoroom,falai,removebg")
        pool = select_pool(s, CutoutSpec())
        assert [e.id for e in pool] == [EngineId.REMOVEBG]

    def test_the_local_engine_is_not_used_once_a_real_key_exists(self):
        pool = select_pool(settings(), CutoutSpec())
        assert EngineId.LOCAL not in [e.id for e in pool]

    def test_single_strategy_can_pin_it_explicitly(self):
        pool = select_pool(
            settings(), CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.REMOVEBG)
        )
        assert [e.id for e in pool] == [EngineId.REMOVEBG]

    def test_it_pairs_with_a_second_key_for_auto_pick(self):
        s = settings(photoroom_api_key="k2", engine_pool="removebg,photoroom,falai")
        pool = select_pool(s, CutoutSpec())
        assert [e.id for e in pool] == [EngineId.REMOVEBG, EngineId.PHOTOROOM]

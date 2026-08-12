"""Bulk upload: many images across several archives, and the guard rails that make it safe.

Everything here runs in memory mode with no vendor keys, like the rest of the suite. The image
counts are small — a real 400-image run is minutes of wall clock and is not something a unit test
should do — so each test drives the *mechanism* at a size where it is observable, with the
thresholds lowered rather than the behaviour faked.
"""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import numpy as np
import pytest

from app.api.deps import reset_memory_backends
from app.core.settings import Settings
from app.main import app
from app.models import ImageState, JobState, Note

from tests.test_api import tiny_png, zip_of


@pytest.fixture(autouse=True)
def _clean_state():
    reset_memory_backends()
    yield
    reset_memory_backends()


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def batch(names: list[str]) -> bytes:
    return zip_of({n: tiny_png() for n in names})


async def upload(
    client: httpx.AsyncClient,
    zip_bytes: bytes,
    *,
    config: dict | None = None,
    job_id: str | None = None,
    start: bool = True,
) -> httpx.Response:
    data: dict[str, str] = {"config": json.dumps(config or {})}
    if job_id is not None:
        data["job_id"] = job_id
    data["start"] = "true" if start else "false"
    return await client.post(
        "/jobs",
        files={"file": ("batch.zip", zip_bytes, "application/zip")},
        data=data,
    )


class TestChunkedUpload:
    """A large selection arrives as several archives against one job id.

    The browser cannot build one archive for 400 photos — it holds every file's bytes, then the
    assembled zip, then the File, roughly three times the total, before sending anything. So the
    upload is split and the job is assembled server-side.
    """

    async def test_three_batches_become_one_job(self, client):
        first = await upload(client, batch(["a.png", "b.png"]), start=False)
        assert first.status_code == 202
        body = first.json()
        job_id = body["job_id"]
        assert body["accepting_uploads"] is True
        assert body["batches"] == 1

        await upload(client, batch(["c.png", "d.png"]), job_id=job_id, start=False)
        third = await upload(client, batch(["e.png"]), job_id=job_id, start=False)
        assert third.json()["batches"] == 3

        started = await client.post(f"/jobs/{job_id}/start")
        assert started.status_code == 202

        status = (await client.get(f"/jobs/{job_id}")).json()
        assert status["state"] == "completed"
        assert status["total"] == 5
        assert status["completed"] == 5
        assert {i["source_name"] for i in status["images"]} == {
            "a.png", "b.png", "c.png", "d.png", "e.png"
        }

    async def test_a_single_batch_upload_is_unchanged(self, client):
        """The additive half of the contract change: one POST still creates, runs and returns."""
        r = await upload(client, batch(["only.png"]))
        assert r.status_code == 202
        body = r.json()
        assert body["accepting_uploads"] is False
        assert body["total"] == 1
        assert (await client.get(f"/jobs/{body['job_id']}")).json()["state"] == "completed"

    async def test_later_batches_cannot_change_the_config(self, client):
        """One job is one JobConfig.

        Letting batch 2 alter the background colour would deliver an order whose images disagree
        with each other, with no record of which setting produced which file.
        """
        first = await upload(
            client, batch(["a.png"]), config={"background": {"color": "#FF0000"}}, start=False
        )
        job_id = first.json()["job_id"]
        await upload(
            client,
            batch(["b.png"]),
            config={"background": {"color": "#00FF00"}},
            job_id=job_id,
            start=False,
        )
        await client.post(f"/jobs/{job_id}/start")

        status = (await client.get(f"/jobs/{job_id}")).json()
        assert status["completed"] == 2
        # Both images came from the first batch's config; the hash is that config's, not the
        # second one's.
        assert status["config_hash"] == first.json()["config_hash"]

    async def test_a_started_job_refuses_more_images(self, client):
        started = await upload(client, batch(["a.png"]))
        job_id = started.json()["job_id"]

        late = await upload(client, batch(["b.png"]), job_id=job_id, start=False)
        assert late.status_code == 409

    async def test_starting_an_empty_job_is_rejected(self, client):
        """`/start` with no batches would otherwise produce a job that fails for no visible reason."""
        r = await client.post("/jobs/does-not-exist/start")
        assert r.status_code == 404

    async def test_unknown_job_id_is_a_404_not_a_new_job(self, client):
        r = await upload(client, batch(["a.png"]), job_id="deadbeef", start=False)
        assert r.status_code == 404


class TestJobLevelCaps:
    """Per-archive guards bound one upload; without these, N uploads multiply through them."""

    async def test_the_accumulated_image_cap_is_enforced_across_batches(self, client):
        from app.api import routes

        app.dependency_overrides[routes._settings_dep] = lambda: Settings(
            max_job_images=3, inline_job_max_images=50
        )
        try:
            first = await upload(client, batch(["a.png", "b.png"]), start=False)
            job_id = first.json()["job_id"]

            over = await upload(client, batch(["c.png", "d.png"]), job_id=job_id, start=False)
            assert over.status_code == 413
            assert "limit is 3" in over.json()["detail"]
        finally:
            app.dependency_overrides.clear()

    async def test_the_cap_counts_the_job_not_the_archive(self, client):
        """Two archives of two, against a cap of three, must fail — the archives each pass."""
        from app.api import routes

        app.dependency_overrides[routes._settings_dep] = lambda: Settings(
            max_job_images=3, inline_job_max_images=50
        )
        try:
            first = await upload(client, batch(["a.png", "b.png"]), start=False)
            assert first.status_code == 202, "one archive of two is under the cap"
            second = await upload(
                client, batch(["c.png", "d.png"]), job_id=first.json()["job_id"], start=False
            )
            assert second.status_code == 413, "four across two archives is over it"
        finally:
            app.dependency_overrides.clear()


class TestInlineGate:
    """Memory mode runs the job inside the POST handler, which does not scale to hundreds."""

    async def test_bulk_without_a_worker_is_refused_with_the_fix_named(self, client):
        from app.api import routes

        app.dependency_overrides[routes._settings_dep] = lambda: Settings(
            inline_job_max_images=2
        )
        try:
            r = await upload(client, batch(["a.png", "b.png", "c.png"]))
            assert r.status_code == 503
            detail = r.json()["detail"]
            assert "rq worker" in detail.lower()
            assert "3 images" in detail
        finally:
            app.dependency_overrides.clear()

    async def test_a_small_job_still_runs_with_no_infrastructure(self, client):
        """The half of the trade that keeps pytest and a keyless first run infra-free."""
        r = await upload(client, batch(["a.png"]))
        assert r.status_code == 202
        assert (await client.get(f"/jobs/{r.json()['job_id']}")).json()["state"] == "completed"


class TestCancel:
    async def test_cancelling_a_finished_job_keeps_its_results(self, client):
        """A racing click on a job that just finished must not destroy what it produced."""
        r = await upload(client, batch(["a.png"]))
        job_id = r.json()["job_id"]

        cancelled = await client.post(f"/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200

        status = (await client.get(f"/jobs/{job_id}")).json()
        assert status["state"] == "completed"
        assert status["completed"] == 1
        assert status["bundle_url"]

    async def test_cancel_stops_the_run_and_names_what_was_skipped(self, client):
        """The operator's next question is "which ones do I still need?" — so skipped images
        carry their real filenames, not a count."""
        from app.api.deps import get_jobstore, get_storage
        from app.core.settings import get_settings
        from app import jobs

        storage, store = get_storage(), get_jobstore()
        settings = get_settings()

        r = await upload(client, batch(["a.png", "b.png", "c.png"]), start=False)
        job_id = r.json()["job_id"]

        # Cancel before the worker starts: every image should be skipped, by name.
        store.request_cancel(job_id)
        status = await jobs.run_job_async(job_id, storage, store, settings)

        assert status.state is JobState.CANCELLED
        assert status.completed == 0
        skipped = {i.source_name for i in status.images if i.state is ImageState.SKIPPED}
        assert skipped == {"a.png", "b.png", "c.png"}
        assert all(
            i.error.code.value == "job_cancelled"
            for i in status.images
            if i.state is ImageState.SKIPPED
        )

    async def test_every_image_is_accounted_for_after_a_stop(self, client):
        """completed + failed must always equal total, or an image looks lost rather than skipped."""
        from app.api.deps import get_jobstore, get_storage
        from app.core.settings import get_settings
        from app import jobs

        storage, store = get_storage(), get_jobstore()
        r = await upload(client, batch(["a.png", "b.png"]), start=False)
        job_id = r.json()["job_id"]
        store.request_cancel(job_id)

        status = await jobs.run_job_async(job_id, storage, store, get_settings())
        assert status.completed + status.failed == status.total


class TestSpendCeiling:
    async def test_the_job_stops_at_the_ceiling_and_keeps_what_it_paid_for(self, client):
        """A 400-image dual-engine run is real money against a ~$1,300/month POC budget."""
        from app.api.deps import get_jobstore, get_storage
        from app import jobs
        from app.engines.base import AlphaResult
        from app.models import EngineId
        import numpy as np

        class _Billing:
            id = EngineId.LOCAL
            cost_per_image_usd = 1.0

            def available(self):
                return True

            async def alpha_for(self, data, width, height):
                alpha = np.zeros((height, width), dtype=np.float32)
                alpha[height // 4 : 3 * height // 4, width // 4 : 3 * width // 4] = 1.0
                return AlphaResult(
                    alpha=alpha, engine=EngineId.LOCAL, latency_ms=1, cost_usd=1.0
                )

        import app.engines.registry as registry

        storage, store = get_storage(), get_jobstore()
        settings = Settings(max_job_cost_usd=2.0, inline_job_max_images=50)

        # Distinct pixels per image, deliberately. Identical bytes would all hit the cut-out cache
        # (keyed on sha256 of the image), so only the first would be billed and the ceiling would
        # never be approached — the test would pass for the wrong reason.
        data = zip_of({f"{i}.png": tiny_png((10 * i, 30, 200 - 10 * i)) for i in range(6)})
        r = await upload(client, data, start=False)
        job_id = r.json()["job_id"]

        original = registry.select_pool
        registry.select_pool = lambda s, spec: [_Billing()]
        try:
            status = await jobs.run_job_async(job_id, storage, store, settings)
        finally:
            registry.select_pool = original

        assert status.cost_usd >= 2.0, "it stops at the ceiling, not before reaching it"
        assert status.cost_usd < 6.0, "and does not run the whole batch"
        assert status.completed > 0, "images finished before the ceiling are kept"
        assert status.state is JobState.COMPLETED, "partial results are still a completed job"

        stopped = [
            i for i in status.images if i.error and i.error.code.value == "budget_exceeded"
        ]
        assert stopped, "the remainder says why it did not run"
        assert status.completed + status.failed == status.total


class TestEstimate:
    """Under `conftest.py` there are no vendor keys, so `select_pool` legitimately returns the
    free local control engine and every real price is 0. Tests that care about the arithmetic
    substitute a priced pool rather than asserting against zero, which would pass for any bug."""

    @pytest.fixture
    def priced_pool(self):
        from app.engines import registry
        from app.engines.http import FalAiEngine, PhotoroomEngine

        original = registry.select_pool
        registry.select_pool = lambda s, spec: [PhotoroomEngine(s), FalAiEngine(s)]
        yield 0.02 + 0.04
        registry.select_pool = original

    async def test_the_estimate_uses_the_real_per_engine_prices(self, client, priced_pool):
        r = await client.post("/jobs/estimate", data={"config": json.dumps({}), "images": "400"})
        assert r.status_code == 200
        body = r.json()

        assert body["images"] == 400
        assert body["engines"] == ["photoroom", "falai"]
        assert body["cost_per_image_usd"] == pytest.approx(priced_pool)
        assert body["estimated_cost_usd"] == pytest.approx(priced_pool * 400)
        assert body["ceiling_usd"] > 0

    async def test_the_dual_engine_pool_bills_both(self, client, priced_pool):
        """Both engines are billed whether or not their mask wins — that is the accepted cost of
        the dual-engine design, and an estimate that hid it would understate by half."""
        r = await client.post("/jobs/estimate", data={"config": json.dumps({}), "images": "1"})
        assert r.json()["cost_per_image_usd"] == pytest.approx(0.06)

    async def test_multi_object_is_flagged_as_a_floor_not_a_total(self, client):
        """Cost is per object there, and the object count is not knowable in advance."""
        r = await client.post(
            "/jobs/estimate",
            data={
                "config": json.dumps({"cutout": {"multi_object": True}}),
                "images": "10",
            },
        )
        assert r.json()["per_object_pricing"] is True

    async def test_a_batch_over_the_ceiling_says_so_before_it_runs(self, client, priced_pool):
        from app.api import routes

        app.dependency_overrides[routes._settings_dep] = lambda: Settings(max_job_cost_usd=5.0)
        try:
            over = await client.post(
                "/jobs/estimate", data={"config": json.dumps({}), "images": "400"}
            )
            assert over.json()["estimated_cost_usd"] == pytest.approx(24.0)
            assert over.json()["exceeds_ceiling"] is True

            under = await client.post(
                "/jobs/estimate", data={"config": json.dumps({}), "images": "10"}
            )
            assert under.json()["exceeds_ceiling"] is False
        finally:
            app.dependency_overrides.clear()


class TestPollingPayload:
    async def test_include_images_false_drops_the_records_but_not_the_counts(self, client):
        r = await upload(client, batch(["a.png", "b.png"]))
        job_id = r.json()["job_id"]

        lean = (await client.get(f"/jobs/{job_id}?include_images=false")).json()
        assert lean["images"] == []
        assert lean["images_total"] == 2
        assert lean["completed"] == 2
        assert lean["state"] == "completed"

    async def test_the_lean_response_is_materially_smaller(self, client):
        """The point of the flag: a 1.2s poll must not move the whole record every time."""
        r = await upload(client, batch([f"{i}.png" for i in range(8)]))
        job_id = r.json()["job_id"]

        full = await client.get(f"/jobs/{job_id}")
        lean = await client.get(f"/jobs/{job_id}?include_images=false")
        assert len(lean.content) * 4 < len(full.content)

    async def test_images_paginate(self, client):
        r = await upload(client, batch([f"{i:02d}.png" for i in range(7)]))
        job_id = r.json()["job_id"]

        page = (await client.get(f"/jobs/{job_id}/images?offset=2&limit=3")).json()
        assert page["total"] == 7
        assert page["offset"] == 2
        assert len(page["images"]) == 3

        rest = (await client.get(f"/jobs/{job_id}/images?offset=6&limit=3")).json()
        assert len(rest["images"]) == 1

    async def test_paged_assets_carry_usable_urls(self, client):
        r = await upload(client, batch(["a.png"]))
        job_id = r.json()["job_id"]

        page = (await client.get(f"/jobs/{job_id}/images?offset=0&limit=1")).json()
        url = page["images"][0]["outputs"][0]["url"]
        assert url
        assert (await client.get(url)).status_code == 200


class TestSignedUrlFreshness:
    """A 400-image job runs far longer than SIGNED_URL_TTL_SECONDS.

    A URL frozen into the record when the image finished is already dead by the time the results
    page renders. Assets carry their storage key and every read path re-signs from it.
    """

    async def test_urls_are_minted_on_read_not_on_write(self, client):
        from app.api.deps import get_jobstore

        r = await upload(client, batch(["a.png"]))
        job_id = r.json()["job_id"]

        stored = get_jobstore().load(job_id)
        assert stored.images[0].outputs[0].key, "the key is what survives in storage"
        assert stored.images[0].outputs[0].url == "", "no URL is frozen at write time"

        served = (await client.get(f"/jobs/{job_id}")).json()
        assert served["images"][0]["outputs"][0]["url"], "and one is minted for the response"

    async def test_the_bundle_url_is_re_signed_too(self, client):
        from app.api.deps import get_jobstore

        r = await upload(client, batch(["a.png"]))
        job_id = r.json()["job_id"]

        assert get_jobstore().load(job_id).bundle_key
        served = (await client.get(f"/jobs/{job_id}")).json()
        assert (await client.get(served["bundle_url"])).status_code == 200


class TestMemoryBounding:
    """`plan_entries` must not retain entry bytes — that is what makes 400 images possible."""

    def test_planning_reads_only_a_sniff_of_each_entry(self):
        from app.ingest import zip_reader

        settings = Settings()
        payload = tiny_png()
        data = zip_of({f"{i}.png": payload for i in range(30)})

        reads: list[int] = []
        real_read = zipfile.ZipExtFile.read

        def counting_read(self, n=-1):
            out = real_read(self, n)
            reads.append(len(out))
            return out

        zipfile.ZipExtFile.read = counting_read
        try:
            with zip_reader.open_archive(data, settings) as archive:
                planned, rejections = zip_reader.plan_entries(archive, settings)
        finally:
            zipfile.ZipExtFile.read = real_read

        assert len(planned) == 30
        assert not rejections
        assert max(reads) <= zip_reader._SNIFF_BYTES, (
            "planning read a whole entry; at 400 images that is the memory blow-up this avoids"
        )

    def test_a_forged_size_is_still_neutralised_on_the_streaming_path(self):
        """The declared-size check moved from `read_images` into `read_entry`. It must still bite.

        Pins the *outcome*, not the mechanism, for the reason
        `test_zip_ingest.py::test_a_forged_central_directory_size_is_neutralised` documents:
        forging the size also breaks the entry's CRC-32, so Python's own `zipfile` may raise
        `BadZipFile` before our length check runs. Either way the entry must never come back as
        usable bytes — that is the property worth pinning, and it is the one the streaming path
        newly has to uphold on its own.
        """
        from app.ingest import zip_reader

        settings = Settings()
        data = bytearray(zip_of({"a.png": tiny_png() + b"\x00" * 1000}))

        # Shrink the central directory's uncompressed-size field, so the archive claims to be far
        # smaller than it is — the pre-check that sums `file_size` would pass it clean.
        idx = data.index(b"PK\x01\x02")
        data[idx + 24 : idx + 28] = (1).to_bytes(4, "little")

        with zip_reader.open_archive(bytes(data), settings) as archive:
            planned, _ = zip_reader.plan_entries(archive, settings)
            for item in planned:
                with pytest.raises(Exception) as caught:
                    zip_reader.read_entry(archive, item)
                assert not isinstance(caught.value, AssertionError)

    def test_a_forged_size_becomes_a_failed_image_not_a_dead_job(self):
        """One poisoned entry must not take the other 399 down with it."""
        from app.core import errors
        from app.ingest import zip_reader

        settings = Settings()
        data = bytearray(zip_of({"a.png": tiny_png() + b"\x00" * 1000, "b.png": tiny_png()}))
        idx = data.index(b"PK\x01\x02")
        data[idx + 24 : idx + 28] = (1).to_bytes(4, "little")

        entries, rejections = zip_reader.read_images(bytes(data), settings)
        names = {e.name for e in entries} | {r.name for r in rejections}
        assert "a.png" not in {e.name for e in entries}, "the tampered entry is never usable"
        assert names == {"a.png", "b.png"}, "and its neighbour survives"

    def test_plan_and_read_agree_with_read_images(self):
        """Both paths screen identically — a second copy of a security screen drifts."""
        from app.ingest import zip_reader

        settings = Settings()
        data = zip_of(
            {
                "good.png": tiny_png(),
                "notes.txt": b"this is not an image at all, not even slightly",
                "also-good.png": tiny_png((10, 200, 10)),
            }
        )

        entries, rejections = zip_reader.read_images(data, settings)
        with zip_reader.open_archive(data, settings) as archive:
            planned, planned_rejections = zip_reader.plan_entries(archive, settings)

        assert [e.name for e in entries] == [p.name for p in planned]
        assert [r.name for r in rejections] == [r.name for r in planned_rejections]
        assert [r.reason for r in rejections] == [r.reason for r in planned_rejections]


class TestLargePsdSources:
    """The client's real PSD set is 132 files of 130 MB each — 5504x8256 RGB, written
    uncompressed, which is why every one is byte-identical in size.

    Every single one was rejected, and reported as `unsupported_file` / "Not a supported image
    file" — for a format that is supported, listed in `SourceFormat`, and sniffs correctly. The
    format was never the problem; the 50 MB per-image byte cap was, and the taxonomy hid it.
    """

    def _psd_header(self, extra: int = 0) -> bytes:
        """A byte string that sniffs as PSD.

        This is the real 26-byte header from the client's own file, not an invented one, and the
        difference is load-bearing: `8BPS` plus zero padding is reported by libmagic as
        `application/octet-stream`, because it validates the channel count, dimensions and depth
        that follow the magic. A stub would have "failed" this test for a reason unrelated to the
        bug and sent the diagnosis off after a sniff problem that does not exist.

            8BPS  ver 1  6x reserved  3 channels  8256 high  5504 wide  8-bit  mode 3 (RGB)
        """
        header = bytes.fromhex(
            "38 42 50 53 00 01 00 00 00 00 00 00 00 03 00 00 20 40 00 00 15 80 00 08 00 03".replace(" ", "")
        )
        return header + b"\x00" * extra

    def test_a_psd_sniffs_as_an_image(self):
        """The half that always worked, pinned so a sniff regression is not misread as this bug."""
        from app.ingest.zip_reader import _sniff

        assert _sniff(self._psd_header()) == "image/vnd.adobe.photoshop"

    def test_a_130mb_psd_is_accepted_at_the_current_cap(self):
        """The regression. At MAX_IMAGE_BYTES=50 MB this was rejected; the real files are 130 MB.

        Originally asserted a minimum cap large enough for the set. That was chasing a number —
        50 MB rejected all 132, then 200 MB rejected the 260 MB one. The guarantee is now simply
        that no byte cap applies by default, which is strictly stronger and cannot go stale as
        soon as someone exports a larger file.
        """
        from app.ingest import zip_reader

        settings = Settings()
        assert settings.max_image_bytes == 0 or settings.max_image_bytes >= 272_682_484, (
            "the client's PSDs run to 260 MB; any cap below that rejects real source material"
        )

        # Sized just past the real file, stored (not deflated) so the entry is genuinely that big.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("product.psd", self._psd_header(extra=1024))

        with zip_reader.open_archive(buf.getvalue(), settings) as archive:
            planned, rejections = zip_reader.plan_entries(archive, settings)

        assert [p.name for p in planned] == ["product.psd"]
        assert rejections == []

    def test_an_oversize_file_reports_size_not_format(self):
        """The reporting bug, pinned independently of the cap's value."""
        from app.ingest import zip_reader
        from app.models import ErrorCode

        settings = Settings(max_image_bytes=1024)
        data = zip_of({"product.psd": self._psd_header(extra=4096)})
        _entries, rejections = zip_reader.read_images(data, settings)

        assert len(rejections) == 1
        assert rejections[0].code is ErrorCode.FILE_TOO_LARGE, (
            "a supported format that is merely too big must not be reported as a format problem"
        )
        assert "per-image limit" in rejections[0].reason

    async def test_the_reason_survives_all_the_way_into_the_job_record(self, client):
        """End to end: the reason must reach the response, not be flattened by `jobs.py`.

        This is where it was lost. `jobs.py` stamped every rejection `unsupported_file`, so the
        specific reason existed in `zip_reader`, was passed to the caller, and was overwritten one
        line later — while the message field the UI could have shown carried the generic copy.
        """
        from app.api import routes

        app.dependency_overrides[routes._settings_dep] = lambda: Settings(max_image_bytes=2048)
        try:
            data = zip_of({"big.psd": self._psd_header(extra=8192), "ok.png": tiny_png()})
            r = await upload(client, data)
            job_id = r.json()["job_id"]

            images = {i["source_name"]: i for i in (await client.get(f"/jobs/{job_id}")).json()["images"]}

            big = images["big.psd"]
            assert big["state"] == "skipped"
            assert big["error"]["code"] == "file_too_large", "not 'unsupported_file'"
            assert "per-image limit" in big["error"]["message"], (
                "the specific reason must reach the browser; the UI renders it under the "
                "category copy"
            )
            assert images["ok.png"]["state"] == "done", "one bad file never blocks the batch"
        finally:
            app.dependency_overrides.clear()


class TestVendorPayloadSize:
    """"The vendor can read this format" and "the vendor will accept this many bytes" are
    different questions, and only the first was being asked.

    BMP is in `VENDOR_SAFE_SOURCES` — Photoroom reads BMP perfectly well — so a 130 MB
    uncompressed BMP was forwarded untouched and refused with a bare 413. Measured: the identical
    45 MP of pixels are 130 MB as BMP and 41 MB as PNG, so re-encoding fixes it losslessly.
    """

    def _big_rgb(self, w=1400, h=1400):
        """Incompressible noise, so PNG cannot shrink it away and the budget actually bites."""
        rng = np.random.default_rng(0)
        return (rng.random((h, w, 3), dtype=np.float32) * 0.4 + 0.3).astype(np.float32)

    def test_a_payload_under_budget_is_left_alone(self):
        from app import pipeline

        settings = Settings(vendor_max_upload_bytes=50_000_000)
        data, downscaled = pipeline._encode_under_budget(self._big_rgb(), settings)
        assert not downscaled
        assert len(data) <= settings.vendor_max_upload_bytes

    def test_an_oversize_payload_is_downscaled_until_it_fits(self):
        from app import pipeline

        settings = Settings(vendor_max_upload_bytes=200_000, vendor_min_long_edge_px=128)
        data, downscaled = pipeline._encode_under_budget(self._big_rgb(), settings)
        assert downscaled, "it must reduce rather than send something the vendor will refuse"
        assert len(data) <= settings.vendor_max_upload_bytes

    def test_downscaling_is_reported(self):
        """Silently shipping a lower-precision mask is exactly what the notes exist to prevent."""
        from app import pipeline
        from app.imaging import export as E

        settings = Settings(vendor_max_upload_bytes=200_000, vendor_min_long_edge_px=128)
        decoded = E.DecodedImage(
            rgb_linear=self._big_rgb(), alpha=None, source_size=(1400, 1400)
        )
        _data, notes = pipeline._encode_for_vendor(decoded, settings)
        assert notes == [Note.VENDOR_DOWNSCALED]

    def test_no_downscale_means_no_note(self):
        from app import pipeline
        from app.imaging import export as E

        settings = Settings(vendor_max_upload_bytes=50_000_000)
        decoded = E.DecodedImage(
            rgb_linear=self._big_rgb(), alpha=None, source_size=(1400, 1400)
        )
        _data, notes = pipeline._encode_for_vendor(decoded, settings)
        assert notes == []

    def test_the_mask_still_comes_back_at_source_resolution(self):
        """Downscaling the UPLOAD must not change the mask's shape.

        `base.fit_alpha_to_source` already resizes a vendor mask back to the source dimensions —
        written for vendors that silently cap resolution, which is the same situation reached
        deliberately here. Without that, a downscaled upload would misalign every edge.
        """
        from app.engines.base import alpha_from_rgba_png
        from PIL import Image

        small = Image.new("RGBA", (256, 384), (0, 0, 0, 255))
        buf = io.BytesIO()
        small.save(buf, format="PNG")

        alpha = alpha_from_rgba_png(buf.getvalue(), width=1024, height=1536)
        assert alpha.shape == (1536, 1024)

    async def test_a_413_is_not_reported_as_retryable(self, client):
        """The misleading half: "may succeed on retry" for bytes that never change."""
        import httpx as _httpx

        from app.core import errors
        from app.engines.http import PhotoroomEngine

        engine = PhotoroomEngine(Settings(photoroom_api_key="k"))
        response = _httpx.Response(413, request=_httpx.Request("POST", "https://x/y"))

        with pytest.raises(errors.VendorPayloadTooLarge) as caught:
            engine._classify(response)

        assert caught.value.retryable is False
        assert caught.value.code.value == "vendor_payload_too_large"

    def test_bmp_is_still_a_format_vendors_can_read(self):
        """The format table was never wrong — only incomplete. Don't "fix" it by removing BMP."""
        from app.imaging import formats as F
        from app.models import SourceFormat

        assert not F.needs_reencode_for_vendor(SourceFormat.BMP)
        assert SourceFormat.BMP in F.VENDOR_SAFE_SOURCES


class TestNoPerImageByteLimit:
    """`max_image_bytes = 0` means no per-image byte limit, and that is the default.

    A byte count measures the container, not the threat. Every value tried was wrong for someone:
    50 MB rejected an entire 132-file PSD set (130 MB each), 200 MB then rejected a 260 MB one —
    and each rejection cost a debugging round to trace back to a config number, because a
    perfectly supported format was refused on size. `max_image_pixels` is the guard that bounds
    what decode actually allocates, and it is unchanged.
    """

    def test_unlimited_is_the_default(self):
        assert Settings().max_image_bytes == 0

    def test_a_huge_entry_is_accepted_when_the_cap_is_off(self):
        from app.ingest import zip_reader

        settings = Settings(max_image_bytes=0)
        data = zip_of({"huge.png": tiny_png() + b"\x00" * 200_000})
        entries, rejections = zip_reader.read_images(data, settings)

        assert [e.name for e in entries] == ["huge.png"]
        assert rejections == []

    def test_a_cap_still_works_when_one_is_set(self):
        """Switchable off, not deleted — a deployment that wants a ceiling can still have one."""
        from app.ingest import zip_reader
        from app.models import ErrorCode

        settings = Settings(max_image_bytes=1024)
        data = zip_of({"huge.png": tiny_png() + b"\x00" * 200_000})
        entries, rejections = zip_reader.read_images(data, settings)

        assert entries == []
        assert rejections[0].code is ErrorCode.FILE_TOO_LARGE

    def test_the_pixel_guard_survives_the_byte_cap_being_off(self):
        """The half that must NOT be switchable. This is what a decompression bomb attacks."""
        from app.core import errors
        from app import pipeline

        settings = Settings(max_image_bytes=0, max_image_pixels=64)
        big = zip_of({"x.png": tiny_png(size=60)})  # 3600 px, well over the 64 px cap
        from app.ingest import zip_reader

        entries, _ = zip_reader.read_images(big, settings)
        with pytest.raises(errors.ImageDecodeFailed):
            pipeline._decode(entries[0].data, settings)

    async def test_no_size_rejection_reaches_the_job_record(self, client):
        from app.api import routes

        app.dependency_overrides[routes._settings_dep] = lambda: Settings(max_image_bytes=0)
        try:
            data = zip_of({"big.png": tiny_png() + b"\x00" * 300_000})
            r = await upload(client, data)
            images = (await client.get(f"/jobs/{r.json()['job_id']}")).json()["images"]
            assert [i["state"] for i in images] == ["done"]
        finally:
            app.dependency_overrides.clear()

    def test_archive_caps_also_honour_zero(self):
        """Otherwise an unlimited per-image size is silently re-capped by the archive carrying it."""
        from app.ingest import zip_reader

        settings = Settings(max_image_bytes=0, max_zip_bytes=0, max_uncompressed_bytes=0)
        data = zip_of({"a.png": tiny_png(), "b.png": tiny_png() + b"\x00" * 100_000})
        entries, rejections = zip_reader.read_images(data, settings)

        assert len(entries) == 2
        assert rejections == []


def _real_psd(size: int = 160) -> bytes:
    """A genuine PSD, written by the same fallback writer the pipeline uses.

    Not `E.encode(..., fmt=PSD)` — that deliberately raises, because PSD is assembled by
    `app.psd`, not the raster encoder.

    The shape matters for the same reason `tiny_png`'s does: a flat fill gives the local control
    engine's backdrop keyer nothing to separate, so it returns an empty mask, autopick rejects it,
    and the image fails for a reason that has nothing to do with what is under test. A soft-edged
    disc of saturated colour on a light backdrop is segmentable offline.
    """
    from app.models import PsdSpec
    from app.psd import fallback

    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    dist = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
    alpha = np.clip(size * 0.3 - dist + 0.5, 0.0, 1.0).astype(np.float32)

    product = np.broadcast_to(
        np.array([0.55, 0.02, 0.02], dtype=np.float32), (size, size, 3)
    ).astype(np.float32)

    return fallback.build_psd(
        product_rgb_linear=product,
        product_alpha=alpha,
        background_color_linear=np.array([0.9, 0.9, 0.9], dtype=np.float32),
        shadow_ratio=None,
        spec=PsdSpec(enabled=True),
    ).data


def _stored_zip(name: str, data: bytes) -> bytes:
    """Zip with STORED entries, exactly as the browser's own writer does (`frontend/src/lib/zip.ts`).

    Not incidental: a synthetic flat-colour PSD DEFLATEs past the 200:1 compression-ratio guard
    and is rejected as hostile. Real photographic PSDs compress about 2.2:1 — measured on the
    client's own 130 MB and 260 MB files — so the guard is right and the fixture was the
    unrealistic part.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(name, data)
    return buf.getvalue()


class TestSourcePreview:
    """The "before" panel showed the user's raw upload, which a browser cannot decode for
    EPS/PSD/TIFF/raw — so every PSD and EPS rendered an empty box beside a correct "after".

    Same blind spot `preview_format` fixes for delivered outputs, never applied to the input side.
    """

    def test_a_renderable_source_gets_no_thumbnail(self):
        """PNG/JPEG/BMP/WebP use the user's own local File — free, instant, full resolution."""
        from app.imaging import formats as F
        from app.models import SourceFormat

        for fmt in (SourceFormat.PNG, SourceFormat.JPEG, SourceFormat.BMP, SourceFormat.WEBP):
            assert F.is_browser_renderable_source(fmt), fmt

    def test_formats_a_browser_cannot_paint_are_flagged(self):
        from app.imaging import formats as F
        from app.models import SourceFormat

        for fmt in (
            SourceFormat.PSD, SourceFormat.EPS, SourceFormat.TIFF,
            SourceFormat.CR2, SourceFormat.NEF, SourceFormat.DNG,
        ):
            assert not F.is_browser_renderable_source(fmt), fmt

    def test_the_thumbnail_is_small_and_decodable(self):
        from app.imaging import export as E
        from app.models import OutputFormat, SourceFormat

        # A TIFF source: decodable here, blank in a browser.
        tiff = E.encode(
            np.full((900, 1400, 3), 0.5, dtype=np.float32), None, fmt=OutputFormat.TIFF
        )
        thumb = E.source_thumbnail(tiff, max_pixels=80_000_000, source=SourceFormat.TIFF)

        w, h = E.dimensions_of(thumb)
        assert max(w, h) == E.SOURCE_THUMBNAIL_LONG_EDGE
        assert (w, h) == (768, 494), "aspect ratio must be preserved"
        assert len(thumb) < len(tiff), "a thumbnail that is not smaller has no reason to exist"

    async def test_a_psd_upload_gets_a_before_thumbnail(self, client):
        r = await upload(client, _stored_zip("product.psd", _real_psd()))
        job_id = r.json()["job_id"]

        image = (await client.get(f"/jobs/{job_id}")).json()["images"][0]
        assert image["source_format"] == "psd"
        assert image["source_preview_url"], "a PSD has no in-browser preview without one"

        served = await client.get(image["source_preview_url"])
        assert served.status_code == 200
        assert served.headers["content-type"] == "image/png"

    async def test_a_png_upload_gets_none(self, client):
        r = await upload(client, batch(["a.png"]))
        image = (await client.get(f"/jobs/{r.json()['job_id']}")).json()["images"][0]
        assert image["source_preview_url"] is None, (
            "the client already holds the file; an extra encode and round trip buys nothing"
        )

    async def test_the_thumbnail_is_not_in_the_download_bundle(self, client):
        """It is ours, not theirs — like `preview_format`, it must not ship as a deliverable."""
        r = await upload(client, _stored_zip("product.psd", _real_psd()))
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()

        bundle = await client.get(status["bundle_url"])
        names = zipfile.ZipFile(io.BytesIO(bundle.content)).namelist()
        assert not any("/src/" in n for n in names)
        assert len(names) == len(
            [o for o in status["images"][0]["outputs"]
             if o["format"] != status["images"][0]["preview_format"]]
        )

    async def test_a_thumbnail_failure_never_fails_the_image(self, client, monkeypatch):
        """A missing "before" picture is cosmetic; the processed asset is the deliverable."""
        from app.imaging import export as E

        def boom(*a, **k):
            raise RuntimeError("thumbnail encoder exploded")

        monkeypatch.setattr(E, "source_thumbnail", boom)

        r = await upload(client, _stored_zip("product.psd", _real_psd()))
        image = (await client.get(f"/jobs/{r.json()['job_id']}")).json()["images"][0]

        assert image["state"] == "done"
        assert image["source_preview_url"] is None


class TestSourceFormatOnly:
    """"Give me back what I uploaded, in its own format, and nothing else."

    `match_source` always delivered the source format, but `export.formats` could not be empty —
    so every image also picked up whatever was listed there. With the default `['png']` that
    doubled a 132-image PSD order into 264 files, half of them unasked for.
    """

    def _mixed(self) -> bytes:
        """One archive holding a PNG, a JPEG, a TIFF and a BMP."""
        from app.imaging import export as E
        from app.models import OutputFormat

        png = tiny_png()
        decoded = E.decode(png)
        return zip_of(
            {
                "a.png": png,
                "b.jpg": E.encode(decoded.rgb_linear, None, fmt=OutputFormat.JPEG),
                "c.tiff": E.encode(decoded.rgb_linear, None, fmt=OutputFormat.TIFF),
                "d.bmp": E.encode(decoded.rgb_linear, None, fmt=OutputFormat.BMP),
            }
        )

    async def test_empty_formats_delivers_only_the_uploaded_format(self, client):
        r = await upload(
            client,
            self._mixed(),
            config={"export": {"formats": [], "match_source": True}},
        )
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()
        assert status["completed"] == 4

        delivered = {
            i["source_name"]: [
                o["format"] for o in i["outputs"] if o["format"] != i["preview_format"]
            ]
            for i in status["images"]
        }
        assert delivered == {
            "a.png": ["png"],
            "b.jpg": ["jpeg"],
            "c.tiff": ["tiff"],
            "d.bmp": ["bmp"],
        }, "one deliverable each, matching what arrived"

    async def test_the_bundle_holds_one_file_per_upload(self, client):
        """The point of the whole change: no unrequested second file per image."""
        r = await upload(
            client,
            self._mixed(),
            config={"export": {"formats": [], "match_source": True}},
        )
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()
        bundle = await client.get(status["bundle_url"])
        names = sorted(zipfile.ZipFile(io.BytesIO(bundle.content)).namelist())

        assert names == ["a.png", "b.jpeg", "c.tiff", "d.bmp"]

    async def test_the_default_still_adds_png(self, client):
        """Unchanged for anyone who has not asked for the new behaviour."""
        r = await upload(client, self._mixed())
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()
        by_name = {i["source_name"]: i for i in status["images"]}

        tiff = [o["format"] for o in by_name["c.tiff"]["outputs"]]
        assert "png" in tiff and "tiff" in tiff

    async def test_a_raw_upload_gets_its_original_back_in_the_bundle(self, client, monkeypatch):
        """Camera raw has no encoder anywhere, so `match_source` can only ever substitute TIFF.

        The original is shipped alongside so the delivered folder does contain the `.nef` the
        order was placed with. It is the untouched source and carries no cut-out — that is what
        `format_substituted` on the result says.
        """
        self._stub_libraw(monkeypatch)
        # Sniffs as binary rather than text, which ingest rejects. Content is irrelevant:
        # libraw is stubbed, and the point is that these exact bytes come back.
        raw_bytes = bytes(range(256)) * 8

        r = await upload(
            client,
            zip_of({"shot.nef": raw_bytes}),
            config={"export": {"formats": [], "match_source": True}},
        )
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()
        assert status["images"][0]["state"] == "done"

        bundle = await client.get(status["bundle_url"])
        zf = zipfile.ZipFile(io.BytesIO(bundle.content))
        names = sorted(zf.namelist())

        assert names == ["shot.nef", "shot.tiff"], "the substitute AND the original"
        assert zf.read("shot.nef") == raw_bytes, "byte-identical to what was uploaded"

    async def test_the_original_is_left_out_when_same_format_was_not_asked_for(
        self, client, monkeypatch
    ):
        """A PNG-only order must not collect a 130 MB raw it never requested."""
        self._stub_libraw(monkeypatch)
        r = await upload(
            client,
            zip_of({"shot.nef": bytes(range(256)) * 8}),
            config={"export": {"formats": ["png"], "match_source": False}},
        )
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()
        bundle = await client.get(status["bundle_url"])

        assert zipfile.ZipFile(io.BytesIO(bundle.content)).namelist() == ["shot.png"]

    async def test_the_bundle_is_uploaded_streamed_not_read_into_memory(self, client, monkeypatch):
        """`_write_bundle` assembles through a temp file so a huge bundle is never a `bytes`
        object; uploading it with `put(scratch.read())` threw that away at the last step.

        Measured on this project's own assets: 200 images at source resolution is ~45 GB of TIFFs
        plus returned originals — an OOM on a job that had already succeeded and been paid for.
        """
        from app.api.deps import get_storage

        storage = get_storage()
        used = {"stream": 0, "bytes": 0}
        real_stream, real_put = storage.put_stream, storage.put

        def spy_stream(key, fileobj, content_type, content_disposition=None):
            if key.endswith("outputs.zip"):
                used["stream"] += 1
            return real_stream(key, fileobj, content_type, content_disposition)

        def spy_put(key, data, content_type):
            if key.endswith("outputs.zip"):
                used["bytes"] += 1
            return real_put(key, data, content_type)

        monkeypatch.setattr(storage, "put_stream", spy_stream)
        monkeypatch.setattr(storage, "put", spy_put)

        r = await upload(client, batch(["a.png", "b.png"]))
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()

        assert status["bundle_url"], "the bundle must still be produced"
        assert used["stream"] == 1, "bundle must go through put_stream"
        assert used["bytes"] == 0, "bundle must never be read into a bytes object"

    async def test_the_bundle_downloads_under_an_ai_prefixed_source_name(self, client):
        """`outputs.zip` was indistinguishable in a downloads folder the moment a second order
        landed. Client feedback: name it after the upload, prefixed `AI_`.

        It has to be `Content-Disposition` on the stored object, not the anchor's `download`
        attribute: the link points straight at cloud storage, and `download` is ignored
        cross-origin.
        """
        r = await upload(client, zip_of({"product-42.png": tiny_png()}))
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()

        bundle = await client.get(status["bundle_url"])
        assert bundle.status_code == 200
        assert 'filename="AI_product-42.zip"' in bundle.headers["content-disposition"]

    async def test_a_writable_source_does_not_get_a_duplicate_original(self, client):
        """PNG round-trips, so its 'original' would just be a second copy of the same format."""
        r = await upload(
            client, zip_of({"a.png": tiny_png()}),
            config={"export": {"formats": [], "match_source": True}},
        )
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()
        bundle = await client.get(status["bundle_url"])

        assert zipfile.ZipFile(io.BytesIO(bundle.content)).namelist() == ["a.png"]

    @staticmethod
    def _stub_libraw(monkeypatch):
        """No camera files exist in the repo and none may be added — see tests/test_formats.py."""
        import sys
        import types

        class FakeRaw:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def postprocess(self, **kw):
                # Needs a real figure/ground split: the offline control engine keys on the
                # backdrop, so a flat frame fails as no_foreground_found and never reaches
                # the bundle at all.
                frame = np.full((48, 48, 3), 62000, np.uint16)
                frame[14:34, 14:34, :] = 9000
                return frame

        module = types.ModuleType("rawpy")
        module.imread = lambda _fh: FakeRaw()
        module.ColorSpace = types.SimpleNamespace(sRGB="sRGB")
        monkeypatch.setitem(sys.modules, "rawpy", module)

    async def test_asking_for_nothing_at_all_is_rejected(self, client):
        """Empty formats AND match_source off would deliver zero files — a 422, not a silent no-op."""
        r = await upload(
            client, self._mixed(), config={"export": {"formats": [], "match_source": False}}
        )
        assert r.status_code == 422
        assert "nothing would be delivered" in json.dumps(r.json())

    async def test_a_tiff_only_job_still_gets_a_viewable_preview(self, client):
        """TIFF is not browser-renderable, so the results grid still needs something to paint —
        and that preview must stay out of the download."""
        r = await upload(
            client,
            zip_of({"c.tiff": self._one("tiff")}),
            config={"export": {"formats": [], "match_source": True}},
        )
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()
        image = status["images"][0]

        assert image["preview_format"] == "png", "otherwise the card is blank"
        assert [o["format"] for o in image["outputs"] if o["format"] != "png"] == ["tiff"]

        bundle = await client.get(status["bundle_url"])
        assert zipfile.ZipFile(io.BytesIO(bundle.content)).namelist() == ["c.tiff"]

    def _one(self, fmt_name: str) -> bytes:
        from app.imaging import export as E
        from app.models import OutputFormat

        decoded = E.decode(tiny_png())
        return E.encode(decoded.rgb_linear, None, fmt=OutputFormat(fmt_name))

    async def test_an_unknown_extension_still_delivers_something(self, client):
        """`match_source` cannot resolve a format it does not recognise. With empty `formats` that
        would leave only the preview — not a deliverable — so the bundle would be empty for that
        file while the job reported success."""
        r = await upload(
            client,
            zip_of({"mystery": tiny_png()}),
            config={"export": {"formats": [], "match_source": True}},
        )
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()
        image = status["images"][0]

        assert image["state"] == "done"
        assert image["source_format"] is None
        assert "format_substituted" in image["notes"], "the swap is reported, not silent"

        bundle = await client.get(status["bundle_url"])
        assert zipfile.ZipFile(io.BytesIO(bundle.content)).namelist() == ["mystery.png"]


class TestByteAwareBatching:
    """Batching by file COUNT is wrong when sources are large.

    50 files per batch is 10 MB of JPEGs or 6.5 GB of the client's PSDs. The count says nothing
    about the cost, so `planUploadBatches` budgets bytes. Mirrored here because the backend's
    per-archive caps are what the client is budgeting against — if they drift, the browser
    discovers it as a 413 halfway through an upload.
    """

    def test_a_batch_of_130mb_sources_fits_the_archive_cap(self):
        settings = Settings()
        psd = 136_341_242
        # The browser targets 256 MB per batch; the server must accept at least that.
        assert settings.max_zip_bytes >= 256 * 1024 * 1024
        assert settings.max_uncompressed_bytes >= 256 * 1024 * 1024
        # And at minimum one whole file must fit in an archive, or nothing can ever be uploaded.
        assert settings.max_zip_bytes >= psd

    def test_the_full_client_set_fits_the_job_cap(self):
        """132 x 130 MB = 16.8 GB in one order. The previous 4 GB cap refused it at ~file 30."""
        settings = Settings()
        assert settings.max_job_upload_bytes >= 132 * 136_341_242
        assert settings.max_job_images >= 132


class TestThrottle:
    """Per-call backoff does not scale: one worker learning of a 429 teaches the others nothing."""

    async def test_a_429_halves_that_vendor_only(self):
        from app.engines.throttle import JobThrottle, report_rate_limited, slot, use

        job_throttle = JobThrottle(ceiling=8)
        with use(job_throttle):
            async with slot("photoroom"):
                pass
            await report_rate_limited("photoroom", 0.0)

            assert job_throttle.limits()["photoroom"] == 4
            async with slot("falai"):
                pass
            assert job_throttle.limits()["falai"] == 8, (
                "Photoroom's quota says nothing about fal.ai's; slowing it would drop throughput "
                "on a healthy vendor exactly when the job needs it to take up the slack"
            )

    async def test_repeated_429s_never_fall_below_the_floor(self):
        from app.engines.throttle import JobThrottle, report_rate_limited, use

        job_throttle = JobThrottle(ceiling=8, floor=1)
        with use(job_throttle):
            for _ in range(10):
                await report_rate_limited("photoroom", 0.0)
        assert job_throttle.limits()["photoroom"] == 1

    async def test_an_absurd_retry_after_is_capped(self):
        """Photoroom's free tier was measured answering "available in 8705 seconds" — 2.4 hours.

        Honouring that literally is a job that looks hung for the rest of the working day.
        """
        import time

        from app.engines.throttle import _MAX_PAUSE_SECONDS, JobThrottle, report_rate_limited, use

        job_throttle = JobThrottle(ceiling=4)
        with use(job_throttle):
            await report_rate_limited("photoroom", 8705.0)

        governor = job_throttle._for("photoroom")
        assert governor._resume_at - time.monotonic() <= _MAX_PAUSE_SECONDS + 1

    async def test_no_throttle_installed_is_a_no_op(self):
        """Unit tests and direct process_image calls must behave exactly as they did before."""
        from app.engines.throttle import current, report_rate_limited, report_success, slot

        assert current() is None
        async with slot("photoroom"):
            pass
        await report_rate_limited("photoroom", 1.0)
        await report_success("photoroom")

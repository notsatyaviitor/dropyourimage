"""Server-side demo samples.

The client's PSDs are ~130 MB each, so four pre-loaded into a browser is 520 MB fetched, held and
posted straight back. These live on the server instead and the browser sends only a flag.

The property worth protecting is that a sample job is **not a special case**: it becomes the same
archive an upload produces and goes through the same `_accept_batch`, the same caps and the same
pipeline. A demo that takes a different code path is not a demo of the product.
"""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from app.api.deps import reset_memory_backends
from app.api import routes
from app.core.settings import Settings
from app.main import app
from app import samples

from tests.test_api import tiny_png


@pytest.fixture(autouse=True)
def _clean_state():
    reset_memory_backends()
    yield
    reset_memory_backends()
    app.dependency_overrides.clear()


@pytest.fixture
def sample_dir(tmp_path):
    (tmp_path / "b-second.png").write_bytes(tiny_png())
    (tmp_path / "a-first.png").write_bytes(tiny_png())
    (tmp_path / "notes.txt").write_text("not an image")
    return tmp_path


@pytest.fixture
def client_with_samples(sample_dir):
    app.dependency_overrides[routes._settings_dep] = lambda: Settings(
        _env_file=None, samples_dir=str(sample_dir)
    )
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestListing:
    def test_it_lists_only_decodable_files(self, sample_dir):
        """A stray .txt offered as a sample would fail at ingest — a poor start to a demo."""
        found = samples.list_samples(Settings(_env_file=None, samples_dir=str(sample_dir)))
        assert [s.name for s in found] == ["a-first.png", "b-second.png"]

    def test_it_is_sorted_so_a_demo_is_reproducible(self, sample_dir):
        found = samples.list_samples(Settings(_env_file=None, samples_dir=str(sample_dir)))
        assert [s.name for s in found] == sorted(s.name for s in found)

    def test_an_unset_directory_yields_nothing_rather_than_raising(self):
        """The UI hides the option on an empty list. A demo convenience must never break a boot."""
        assert samples.list_samples(Settings(_env_file=None, samples_dir="")) == []

    def test_a_missing_directory_yields_nothing(self, tmp_path):
        missing = str(tmp_path / "nope")
        assert samples.list_samples(Settings(_env_file=None, samples_dir=missing)) == []

    def test_it_is_capped_so_a_full_test_set_cannot_become_one_job(self, tmp_path):
        """SAMPLES_DIR pointed at the client's 132-PSD folder would otherwise be a 17 GB job."""
        for i in range(samples.MAX_SAMPLES + 5):
            (tmp_path / f"img-{i:03d}.png").write_bytes(tiny_png())
        found = samples.list_samples(Settings(_env_file=None, samples_dir=str(tmp_path)))
        assert len(found) == samples.MAX_SAMPLES


class TestNameSelection:
    """`sample_names` selects from the server's listing; it is never a path.

    This endpoint has no authentication in front of it in the application itself, so joining a
    caller's string onto SAMPLES_DIR would be arbitrary file read on the host.
    """

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../../etc/passwd",
            "/etc/shadow",
            "..\\..\\windows\\system32\\config\\sam",
            "./a-first.png",
            "subdir/a-first.png",
        ],
    )
    def test_a_path_cannot_be_smuggled_through_a_name(self, sample_dir, hostile):
        settings = Settings(_env_file=None, samples_dir=str(sample_dir))
        assert samples.resolve(settings, [hostile]) == []

    def test_names_select_by_exact_basename(self, sample_dir):
        settings = Settings(_env_file=None, samples_dir=str(sample_dir))
        chosen = samples.resolve(settings, ["b-second.png"])
        assert [s.name for s in chosen] == ["b-second.png"]

    def test_none_means_every_sample(self, sample_dir):
        settings = Settings(_env_file=None, samples_dir=str(sample_dir))
        assert len(samples.resolve(settings, None)) == 2

    def test_an_unknown_name_is_dropped_rather_than_raising(self, sample_dir):
        """A stale name from a client that has not refreshed should narrow the set, not 500."""
        settings = Settings(_env_file=None, samples_dir=str(sample_dir))
        chosen = samples.resolve(settings, ["a-first.png", "deleted-since.png"])
        assert [s.name for s in chosen] == ["a-first.png"]

    def test_a_filtered_archive_holds_only_what_was_asked_for(self, sample_dir):
        settings = Settings(_env_file=None, samples_dir=str(sample_dir))
        data = samples.build_archive(settings, ["b-second.png"])
        assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["b-second.png"]


class TestArchive:
    def test_it_packs_the_same_shape_an_upload_produces(self, sample_dir):
        data = samples.build_archive(Settings(_env_file=None, samples_dir=str(sample_dir)))
        assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["a-first.png", "b-second.png"]

    def test_it_raises_when_no_directory_is_configured(self):
        with pytest.raises(FileNotFoundError):
            samples.build_archive(Settings(_env_file=None, samples_dir=""))


class TestEndpoint:
    async def test_samples_are_published_for_the_ui(self, client_with_samples):
        async with client_with_samples as c:
            body = (await c.get("/samples")).json()

        assert [f["name"] for f in body["files"]] == ["a-first.png", "b-second.png"]
        assert body["total_bytes"] > 0

    async def test_it_reports_none_when_unconfigured(self):
        app.dependency_overrides[routes._settings_dep] = lambda: Settings(_env_file=None)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body = (await c.get("/samples")).json()

        assert body["files"] == []

    async def test_a_job_can_be_created_from_samples_with_no_upload(self, client_with_samples):
        """The point of the whole feature: no file part in the request at all."""
        async with client_with_samples as c:
            r = await c.post(
                "/jobs", data={"config": json.dumps({}), "use_samples": "true"}
            )
            assert r.status_code == 202
            status = (await c.get(f"/jobs/{r.json()['job_id']}")).json()

        assert status["total"] == 2
        assert {i["source_name"] for i in status["images"]} == {"a-first.png", "b-second.png"}

    async def test_samples_run_the_real_pipeline(self, client_with_samples):
        """Not placeholders — they produce genuine outputs, which is why this is honest to demo."""
        async with client_with_samples as c:
            r = await c.post("/jobs", data={"config": json.dumps({}), "use_samples": "true"})
            status = (await c.get(f"/jobs/{r.json()['job_id']}")).json()

        assert status["state"] == "completed"
        assert all(i["state"] == "done" for i in status["images"])
        assert all(i["outputs"] for i in status["images"])

    async def test_asking_for_samples_when_there_are_none_is_a_clear_error(self):
        app.dependency_overrides[routes._settings_dep] = lambda: Settings(_env_file=None)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/jobs", data={"config": json.dumps({}), "use_samples": "true"})

        assert r.status_code == 409
        assert "SAMPLES_DIR" in r.json()["detail"]

    async def test_dismissed_samples_are_left_out_of_the_job(self, client_with_samples):
        """The row's × removes that file from the order, not just from the list."""
        async with client_with_samples as c:
            r = await c.post(
                "/jobs",
                data={
                    "config": json.dumps({}),
                    "use_samples": "true",
                    "sample_names": ["a-first.png"],
                },
            )
            status = (await c.get(f"/jobs/{r.json()['job_id']}")).json()

        assert status["total"] == 1
        assert [i["source_name"] for i in status["images"]] == ["a-first.png"]

    async def test_dismissing_every_sample_is_refused_rather_than_running_empty(
        self, client_with_samples
    ):
        """An empty job would 'complete' having produced nothing, which reads as a silent failure."""
        async with client_with_samples as c:
            r = await c.post(
                "/jobs",
                data={"config": json.dumps({}), "use_samples": "true", "sample_names": ["gone.png"]},
            )

        assert r.status_code == 409

    async def test_a_job_with_neither_file_nor_samples_is_rejected(self, client_with_samples):
        async with client_with_samples as c:
            r = await c.post("/jobs", data={"config": json.dumps({})})

        assert r.status_code == 422
        assert "use_samples" in json.dumps(r.json())

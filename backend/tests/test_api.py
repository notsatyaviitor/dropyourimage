"""API tests: the full HTTP round trip, in memory mode (no Redis, no MinIO).

Memory mode runs jobs inline (see routes.py docstring), so `POST /jobs` blocks until processing
finishes and there is no polling race to manage in these tests.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import numpy as np
import pytest

from app.api.deps import reset_memory_backends
from app.main import app
from app.models import ImageState, JobState, OutputFormat


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


def tiny_png(colour: tuple[int, int, int] = (200, 30, 30), size: int = 60) -> bytes:
    """A product-on-backdrop photo the local control engine can actually segment.

    Not a flat single-colour image: `key_on_backdrop` looks for a difference between a border
    ring and an interior region, so a uniform fill gives it nothing to find and correctly returns
    an empty mask — which is what an earlier version of this fixture produced.
    """
    from app.imaging import color as C
    from app.imaging import export as E

    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    dist = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
    alpha = np.clip(size * 0.28 - dist + 0.5, 0.0, 1.0).astype(np.float32)

    backdrop = np.full((size, size, 3), C.srgb_decode(np.float32(0.95)), dtype=np.float32)
    product = np.broadcast_to(
        C.SRGB.decode(np.array(colour, dtype=np.float32) / 255.0), (size, size, 3)
    ).astype(np.float32)

    from app.imaging import composite as X

    composited = X.composite_over(product, alpha, backdrop)
    return E.encode(composited, None, fmt=OutputFormat.PNG)


def zip_of(images: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in images.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def post_job(client: httpx.AsyncClient, zip_bytes: bytes, config: dict) -> httpx.Response:
    import json

    return await client.post(
        "/jobs",
        files={"file": ("batch.zip", zip_bytes, "application/zip")},
        data={"config": json.dumps(config)},
    )


class TestHealth:
    async def test_health_reports_configuration_without_secrets(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "config" in body
        assert "api_key" not in str(body).lower()


class TestJobLifecycle:
    async def test_full_round_trip(self, client):
        zdata = zip_of({"a.png": tiny_png(), "b.png": tiny_png((30, 30, 200))})
        r = await post_job(client, zdata, {"background": {"color": "#1E3A8A"}})

        assert r.status_code == 202
        created = r.json()
        assert created["state"] == "queued"
        assert created["total"] == 2

        status = (await client.get(f"/jobs/{created['job_id']}")).json()
        assert status["state"] == "completed"
        assert status["completed"] == 2
        assert status["failed"] == 0
        assert len(status["images"]) == 2
        assert status["bundle_url"]

    async def test_output_asset_is_downloadable_and_correct(self, client):
        from PIL import Image

        from app.imaging import color as C

        r = await post_job(client, zip_of({"a.png": tiny_png()}), {"background": {"color": "#F5F5F5"}})
        job_id = r.json()["job_id"]
        status = (await client.get(f"/jobs/{job_id}")).json()

        outputs = status["images"][0]["outputs"]
        assert outputs
        asset = next(o for o in outputs if o["format"] == "png")

        img_bytes = (await client.get(asset["url"])).content
        arr = np.asarray(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
        assert tuple(arr[0, 0]) == C.hex_to_srgb8("#F5F5F5")
        assert arr.shape[:2] == (asset["height"], asset["width"])

    async def test_unknown_job_id_is_404(self, client):
        r = await client.get("/jobs/does-not-exist")
        assert r.status_code == 404

    async def test_invalid_config_is_422_with_details(self, client):
        r = await post_job(client, zip_of({"a.png": tiny_png()}), {"size": {"width": -5}})
        assert r.status_code == 422
        assert r.json()["detail"]

    async def test_transparent_plus_jpeg_is_rejected_by_the_contract(self, client):
        """The cross-field rule in JobConfig must be enforced at the HTTP boundary too."""
        r = await post_job(
            client,
            zip_of({"a.png": tiny_png()}),
            {"background": {"transparent": True}, "export": {"formats": ["jpeg"]}},
        )
        assert r.status_code == 422

    async def test_rejected_zip_entries_are_reported_not_silently_dropped(self, client):
        zdata = zip_of({"a.png": tiny_png(), "not-an-image.txt": b"hello"})
        r = await post_job(client, zdata, {})
        status = (await client.get(f"/jobs/{r.json()['job_id']}")).json()

        assert status["total"] == 2
        names_by_state = {i["source_name"]: i["state"] for i in status["images"]}
        assert names_by_state["a.png"] == "done"
        assert names_by_state["not-an-image.txt"] == "skipped"

    async def test_a_bad_archive_fails_the_whole_job_cleanly(self, client):
        r = await client.post(
            "/jobs",
            files={"file": ("bad.zip", b"not a zip file", "application/zip")},
            data={"config": "{}"},
        )
        job_id = r.json()["job_id"]
        status = (await client.get(f"/jobs/{job_id}")).json()
        assert status["state"] == "failed"
        assert status["error"] is not None

    async def test_default_config_is_accepted(self, client):
        """An empty config object must fall back to JobConfig defaults, not 422."""
        r = await post_job(client, zip_of({"a.png": tiny_png()}), {})
        assert r.status_code == 202


class TestUploadLimits:
    async def test_oversized_upload_is_rejected_before_full_processing(self, client):
        """`_read_capped` bounds the actual multipart bytes, not the declared Content-Length.

        The zip entry must be genuinely incompressible for this to test anything real: a small
        block repeated many times (an earlier version of this test used `tiny_png() * 50`)
        compresses down to a few hundred bytes under DEFLATE and never approaches the cap at all.
        `os.urandom` cannot be compressed away like that.
        """
        import os

        from app.core.settings import get_settings

        os.environ["MAX_ZIP_BYTES"] = "1000"
        get_settings.cache_clear()
        try:
            zdata = zip_of({"a.png": os.urandom(15_000)})
            assert len(zdata) > 1000, "fixture must exceed the cap to test anything"
            r = await post_job(client, zdata, {})
            assert r.status_code == 413
        finally:
            os.environ.pop("MAX_ZIP_BYTES", None)
            get_settings.cache_clear()


class TestObjectPassthrough:
    async def test_unknown_object_key_is_404(self, client):
        r = await client.get("/objects/jobs/nope/out/x.png")
        assert r.status_code == 404

    async def test_it_serves_objects_in_memory_mode(self, client):
        """The results grid and download links depend on this route when there is no S3/GCS."""
        from app.api.deps import get_storage

        get_storage().put("jobs/j1/out/a.png", b"PNGBYTES", "image/png")
        r = await client.get("/objects/jobs/j1/out/a.png")

        assert r.status_code == 200
        assert r.content == b"PNGBYTES"

    async def test_it_is_disabled_when_real_storage_is_configured(self, client):
        """Verified against a live GCS bucket before this gate existed: the route returned HTTP
        200 with the object body for any key, no signature and no expiry, defeating the
        signed-URL model entirely. Keys cannot be the control — a job id is in every client URL
        and every other key is derived from it by a documented rule."""
        from app.api import routes
        from app.api.deps import get_storage
        from app.core.settings import Settings
        from app.main import app

        get_storage().put("jobs/j2/out/a.png", b"PNGBYTES", "image/png")
        # Same in-memory bytes; only the *declared* backend changes, so this asserts the gate and
        # not merely that a different store is empty.
        app.dependency_overrides[routes._settings_dep] = lambda: Settings(
            _env_file=None, s3_endpoint_url="https://s3.example", gcp_bucket_name=""
        )
        try:
            r = await client.get("/objects/jobs/j2/out/a.png")
        finally:
            app.dependency_overrides.pop(routes._settings_dep, None)

        assert r.status_code == 404
        assert b"PNGBYTES" not in r.content
        assert "disabled" not in r.text.lower(), "must not confirm the key exists"


class TestPresetHash:
    async def test_identical_configs_share_a_config_hash(self, client):
        r1 = await post_job(client, zip_of({"a.png": tiny_png()}), {"background": {"color": "#FFFFFF"}})
        r2 = await post_job(client, zip_of({"a.png": tiny_png()}), {"background": {"color": "#FFFFFF"}})
        assert r1.json()["config_hash"] == r2.json()["config_hash"]

    async def test_different_configs_have_different_hashes(self, client):
        r1 = await post_job(client, zip_of({"a.png": tiny_png()}), {"background": {"color": "#FFFFFF"}})
        r2 = await post_job(client, zip_of({"a.png": tiny_png()}), {"background": {"color": "#000000"}})
        assert r1.json()["config_hash"] != r2.json()["config_hash"]

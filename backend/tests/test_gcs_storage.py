"""GCS backend: selection precedence and the Protocol contract.

The google-cloud-storage client is stubbed throughout. These tests must pass on a clean checkout
with no credentials and no network — the same rule as the vendor adapters — so nothing here
reaches a real bucket. What is covered is the wiring that is easy to get silently wrong:
which backend gets picked, and whether GCS honours the same Protocol as S3.
"""

from __future__ import annotations

import sys
import types

import pytest

from app.api import deps
from app.core.settings import Settings
from app.storage.base import StorageBackend


class _FakeBlob:
    def __init__(self, store: dict, key: str) -> None:
        self._store = store
        self._key = key

    def upload_from_string(self, data: bytes, content_type: str) -> None:
        self._store[self._key] = (data, content_type)

    def download_as_bytes(self) -> bytes:
        if self._key not in self._store:
            raise _NotFound(self._key)
        return self._store[self._key][0]

    def exists(self) -> bool:
        return self._key in self._store

    def generate_signed_url(self, *, version: str, expiration, method: str) -> str:
        return f"https://storage.example/{self._key}?v={version}&m={method}&e={int(expiration.total_seconds())}"


class _NotFound(Exception):
    pass


class _FakeBucket:
    def __init__(self, store: dict, present: bool) -> None:
        self._store = store
        self._present = present

    def exists(self) -> bool:
        return self._present

    def blob(self, key: str) -> _FakeBlob:
        return _FakeBlob(self._store, key)


@pytest.fixture
def gcs(monkeypatch):
    """Install a fake `google.cloud.storage` / `google.cloud.exceptions` before import.

    `state` is mutated by a test *before* it constructs the storage, so one fixture covers the
    healthy path and both startup failures.
    """
    store: dict = {}
    state = {"bucket_present": True, "can_sign": True, "key_file": None, "project": None}

    class _Client:
        def __init__(self, project=None):
            state["project"] = project
            # ADC on a default compute service account has no signer — the case that otherwise
            # breaks at download time rather than at startup.
            self._credentials = types.SimpleNamespace(
                signer_email="svc@example.iam.gserviceaccount.com" if state["can_sign"] else None
            )

        @staticmethod
        def from_service_account_json(path, project=None):
            state["key_file"] = path
            return _Client(project)

        def bucket(self, name):
            return _FakeBucket(store, state["bucket_present"])

    storage_mod = types.ModuleType("google.cloud.storage")
    storage_mod.Client = _Client

    exc_mod = types.ModuleType("google.cloud.exceptions")
    exc_mod.NotFound = _NotFound

    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.exceptions", exc_mod)
    return store, state


def settings_for_gcs(**over) -> Settings:
    """Defaults to no key file, i.e. the Application Default Credentials path.

    Deliberate: a key file must point at a file that really exists (`GcsStorage` checks, because a
    relative path silently differs between uvicorn and the worker), so defaulting to one would
    make every test carry a tmp_path it does not care about.
    """
    base = dict(
        _env_file=None,
        s3_endpoint_url="http://not-memory:9000",
        gcp_bucket_name="drop-your-image",
        gcp_project_id="proj",
        gcp_key_file="",
    )
    base.update(over)
    return Settings(**base)


class TestBackendSelection:
    """Which store gets used. Wrong answers here are silent: the job succeeds and writes the
    assets somewhere the reader never looks."""

    def test_memory_wins_over_a_configured_gcs_bucket(self):
        """The escape hatch must beat a fully configured cloud .env — this is what keeps the
        test suite off a real bucket."""
        s = Settings(_env_file=None, s3_endpoint_url="memory", gcp_bucket_name="drop-your-image")
        assert s.storage_backend_name == "memory"
        assert deps.get_storage(s) is deps._memory_storage

    def test_gcs_is_selected_when_the_bucket_is_named(self):
        assert settings_for_gcs().storage_backend_name == "gcs"

    def test_s3_stays_the_default_when_no_gcp_bucket_is_set(self):
        s = Settings(_env_file=None, s3_endpoint_url="http://minio:9000", gcp_bucket_name="")
        assert s.storage_backend_name == "s3"

    def test_a_project_id_alone_does_not_redirect_storage(self):
        """A half-filled GCP block must not silently move a working S3 deployment's assets."""
        s = Settings(_env_file=None, s3_endpoint_url="http://minio:9000", gcp_project_id="proj")
        assert s.storage_backend_name == "s3"

    def test_health_reports_the_backend_without_leaking_anything(self):
        report = settings_for_gcs(gcp_key_file="/creds/sa.json").redacted()
        assert report["storage"] == "gcs"
        blob = repr(report)
        for secret in ("drop-your-image", "/creds/sa.json", "proj"):
            assert secret not in blob, "bucket names and key paths are not health-check output"


class TestProtocolContract:
    def test_it_satisfies_the_storage_backend_protocol(self, gcs):
        from app.storage.gcs import GcsStorage

        assert isinstance(GcsStorage(settings_for_gcs()), StorageBackend)

    def test_put_then_get_round_trips(self, gcs):
        from app.storage.gcs import GcsStorage

        store, _ = gcs
        s = GcsStorage(settings_for_gcs())
        s.put("jobs/a/out/x.png", b"PNGDATA", "image/png")

        assert s.get("jobs/a/out/x.png") == b"PNGDATA"
        assert store["jobs/a/out/x.png"][1] == "image/png", "content type must survive"

    def test_a_missing_object_raises_keyerror_like_s3(self, gcs):
        """`jobs.py` and the bundle writer catch KeyError; a vendor exception would escape."""
        from app.storage.gcs import GcsStorage

        with pytest.raises(KeyError):
            GcsStorage(settings_for_gcs()).get("jobs/a/nope.png")

    def test_exists_is_false_for_an_absent_key(self, gcs):
        from app.storage.gcs import GcsStorage

        assert GcsStorage(settings_for_gcs()).exists("jobs/a/nope.png") is False

    def test_signed_urls_are_v4_and_carry_the_ttl(self, gcs):
        from app.storage.gcs import GcsStorage

        url = GcsStorage(settings_for_gcs()).signed_url("jobs/a/out/x.png", 900)

        assert url.startswith("https://"), "must be absolute; the frontend does not prefix these"
        assert "v=v4" in url and "e=900" in url


class TestStartupChecks:
    """Both of these fail at *download* time if not caught here — the worst moment to find out."""

    def test_a_missing_bucket_is_a_clear_startup_error(self, gcs):
        from app.storage.gcs import GcsStorage

        _, state = gcs
        state["bucket_present"] = False
        with pytest.raises(RuntimeError, match="does not exist"):
            GcsStorage(settings_for_gcs()).ensure_bucket()

    def test_credentials_that_cannot_sign_are_rejected_up_front(self, gcs):
        """ADC on a default compute service account has no private key, so every download URL
        would raise — long after startup, on a page a client is looking at."""
        from app.storage.gcs import GcsStorage

        _, state = gcs
        state["can_sign"] = False
        with pytest.raises(RuntimeError, match="cannot sign"):
            GcsStorage(settings_for_gcs()).ensure_bucket()

    def test_a_healthy_bucket_passes(self, gcs):
        from app.storage.gcs import GcsStorage

        GcsStorage(settings_for_gcs()).ensure_bucket()

    def test_the_key_file_is_used_when_given(self, gcs, tmp_path):
        from app.storage.gcs import GcsStorage

        _, state = gcs
        real = tmp_path / "sa.json"
        real.write_text("{}")
        GcsStorage(settings_for_gcs(gcp_key_file=str(real)))
        assert state["key_file"] == str(real)

    def test_a_missing_key_file_names_the_directory_it_searched(self, gcs):
        """A relative path resolves against the process's cwd, and uvicorn and the RQ worker do
        not share one — so the API can start while the worker dies on identical config. The bare
        FileNotFoundError names only the filename, which reads like a bad key."""
        from app.storage.gcs import GcsStorage

        with pytest.raises(RuntimeError) as exc:
            GcsStorage(settings_for_gcs(gcp_key_file="sa-does-not-exist.json"))

        message = str(exc.value)
        assert "absolute path" in message
        assert "working directory" in message

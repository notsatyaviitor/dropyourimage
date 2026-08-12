"""Dependency wiring: which storage and job store backend the API uses.

Swaps to in-memory automatically under pytest, so route tests need no Redis or MinIO — the same
reasoning as the imaging core being pure functions: the fewer things a test needs running, the
more of the system it can actually cover.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.jobstore import JobStore, MemoryJobStore, RedisJobStore
from app.core.settings import Settings, get_settings
from app.storage.base import StorageBackend
from app.storage.memory import MemoryStorage

_memory_storage = MemoryStorage()
_memory_jobstore = MemoryJobStore()


def get_storage(settings: Settings | None = None) -> StorageBackend:
    """Pick the object-storage backend, in this order:

    1. **Memory**, when `S3_ENDPOINT_URL` is empty or ``"memory"``. Checked first because it is an
       explicit escape hatch, not a fallback: `tests/conftest.py` sets it to guarantee the suite
       never touches a real bucket, and it must win even on a machine whose `.env` is fully
       configured for a cloud deployment.
    2. **GCS**, when `GCP_BUCKET_NAME` is set.
    3. **S3/MinIO** otherwise, which stays the default so nothing existing changes.

    GCS is opt-in rather than auto-detected: a half-filled GCP block in a `.env` should not
    silently redirect a working S3 deployment's assets to a bucket nobody configured properly.
    """
    settings = settings or get_settings()
    if settings.s3_endpoint_url in ("", "memory"):
        return _memory_storage
    if settings.gcp_bucket_name:
        return _cached_gcs_storage(settings.gcp_bucket_name, settings.gcp_key_file)
    return _cached_s3_storage(settings.s3_endpoint_url, settings.s3_bucket)


@lru_cache
def _cached_s3_storage(endpoint: str, bucket: str) -> StorageBackend:
    from app.storage.s3 import S3Storage

    storage = S3Storage(get_settings())
    storage.ensure_bucket()
    return storage


@lru_cache
def _cached_gcs_storage(bucket: str, key_file: str) -> StorageBackend:
    """Cached on the settings that identify the client, like `_cached_s3_storage`.

    One client per process: it holds a connection pool and, with a key file, parses and keeps the
    private key. Rebuilding it per request would re-read that file on every call.
    """
    from app.storage.gcs import GcsStorage

    storage = GcsStorage(get_settings())
    storage.ensure_bucket()
    return storage


def get_jobstore(settings: Settings | None = None) -> JobStore:
    settings = settings or get_settings()
    if settings.redis_url in ("", "memory"):
        return _memory_jobstore
    return _cached_redis_jobstore(settings.redis_url)


@lru_cache
def _cached_redis_jobstore(url: str) -> JobStore:
    return RedisJobStore(url)


def reset_memory_backends() -> None:
    """Test-only: clear state between test modules that don't get a fresh process."""
    _memory_storage.clear()
    _memory_jobstore.clear()

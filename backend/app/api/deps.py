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
    settings = settings or get_settings()
    if settings.s3_endpoint_url in ("", "memory"):
        return _memory_storage
    return _cached_s3_storage(settings.s3_endpoint_url, settings.s3_bucket)


@lru_cache
def _cached_s3_storage(endpoint: str, bucket: str) -> StorageBackend:
    from app.storage.s3 import S3Storage

    storage = S3Storage(get_settings())
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

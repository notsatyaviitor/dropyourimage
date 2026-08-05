"""Job records. Redis-backed in normal use, in-memory for tests.

A `JobStatus` is stored as one JSON blob per job. No relational schema, deliberately: for a demo
the only query is "give me this job", and a Postgres migration path is not on the critical path.
The trade is that per-image updates rewrite the whole record — fine at 200 images, not at 200,000.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models import JobStatus

_TTL_SECONDS = 60 * 60 * 24


def _key(job_id: str) -> str:
    return f"dyi:job:{job_id}"


@runtime_checkable
class JobStore(Protocol):
    def save(self, status: JobStatus) -> None: ...
    def load(self, job_id: str) -> JobStatus | None: ...


class RedisJobStore:
    def __init__(self, url: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(url, decode_responses=True)

    def save(self, status: JobStatus) -> None:
        # Expiring records keeps a demo machine from accumulating history forever. Outputs in
        # object storage outlive this, which is intentional — the assets are the deliverable.
        self._redis.set(_key(status.job_id), status.model_dump_json(), ex=_TTL_SECONDS)

    def load(self, job_id: str) -> JobStatus | None:
        raw = self._redis.get(_key(job_id))
        return JobStatus.model_validate_json(raw) if raw else None


class MemoryJobStore:
    def __init__(self) -> None:
        self._records: dict[str, str] = {}

    def save(self, status: JobStatus) -> None:
        # Serialised rather than held as an object, so tests catch anything unserialisable — the
        # same failure the Redis path would hit in production.
        self._records[status.job_id] = status.model_dump_json()

    def load(self, job_id: str) -> JobStatus | None:
        raw = self._records.get(job_id)
        return JobStatus.model_validate_json(raw) if raw else None

    def clear(self) -> None:
        self._records.clear()

"""Job records. Redis-backed in normal use, in-memory for tests.

A `JobStatus` is stored as one JSON blob per job. No relational schema, deliberately: for a demo
the only query is "give me this job", and a Postgres migration path is not on the critical path.
The trade is that per-image updates rewrite the whole record — which is why `app/jobs.py`
coalesces those writes rather than issuing one per image.

## Why cancellation is its own key

`request_cancel` does **not** set a field on `JobStatus`, and that is deliberate rather than
untidy. The API process and the RQ worker are different processes holding the same record: the
worker keeps `status` in memory for the whole run and writes it back wholesale, so a flag the API
set on its own copy would be silently overwritten by the worker's very next save. The race is not
unlikely, it is the normal case — the worker saves every few seconds.

A separate key has no such interaction. The API only ever writes it, the worker only ever reads
it, and neither can clobber the other.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models import JobStatus

_TTL_SECONDS = 60 * 60 * 24


def _key(job_id: str) -> str:
    return f"dyi:job:{job_id}"


def _cancel_key(job_id: str) -> str:
    return f"dyi:cancel:{job_id}"


@runtime_checkable
class JobStore(Protocol):
    def save(self, status: JobStatus) -> None: ...
    def load(self, job_id: str) -> JobStatus | None: ...

    def request_cancel(self, job_id: str) -> None:
        """Ask the worker to stop after its in-flight images. Idempotent."""
        ...

    def is_cancel_requested(self, job_id: str) -> bool:
        """Read the flag. Called by the worker between images, so it must stay cheap."""
        ...


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

    def request_cancel(self, job_id: str) -> None:
        self._redis.set(_cancel_key(job_id), "1", ex=_TTL_SECONDS)

    def is_cancel_requested(self, job_id: str) -> bool:
        return bool(self._redis.exists(_cancel_key(job_id)))


class MemoryJobStore:
    def __init__(self) -> None:
        self._records: dict[str, str] = {}
        self._cancelled: set[str] = set()

    def save(self, status: JobStatus) -> None:
        # Serialised rather than held as an object, so tests catch anything unserialisable — the
        # same failure the Redis path would hit in production.
        self._records[status.job_id] = status.model_dump_json()

    def load(self, job_id: str) -> JobStatus | None:
        raw = self._records.get(job_id)
        return JobStatus.model_validate_json(raw) if raw else None

    def request_cancel(self, job_id: str) -> None:
        self._cancelled.add(job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        return job_id in self._cancelled

    def clear(self) -> None:
        self._records.clear()
        self._cancelled.clear()

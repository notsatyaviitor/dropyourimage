"""RQ queue setup for real deployment (Redis available).

Only used when `Settings.redis_url` points at a real Redis instance. In memory-mode (no Redis
configured — the default for local dev and for tests) the API runs jobs inline instead; see
`app/api/routes.py`. That split is deliberate: it keeps `pytest` and a keyless first run free
of infrastructure, while `docker compose up -d && rq worker ... dyi` gives the real async path.
"""

from __future__ import annotations

from functools import lru_cache

from redis import Redis
from rq import Queue

from app.core.settings import Settings, get_settings


@lru_cache
def get_queue(settings: Settings | None = None) -> Queue:
    settings = settings or get_settings()
    connection = Redis.from_url(settings.redis_url)
    return Queue(name=settings.queue_name, connection=connection)


def enqueue_job(job_id: str, settings: Settings | None = None) -> None:
    from app.jobs import run_job

    get_queue(settings or get_settings()).enqueue(run_job, job_id, job_timeout="30m")

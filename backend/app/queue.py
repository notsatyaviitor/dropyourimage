"""RQ queue setup for real deployment (Redis available).

Only used when `Settings.redis_url` points at a real Redis instance. In memory-mode (no Redis
configured — the default for local dev and for tests) the API runs jobs inline instead; see
`app/api/routes.py`. That split is deliberate: it keeps `pytest` and a keyless first run free
of infrastructure, while `docker compose up -d && rq worker ... dyi` gives the real async path.
"""

from __future__ import annotations

import time
from functools import lru_cache

from redis import Redis
from rq import Queue

from app.core.settings import Settings, get_settings

# Bounded so a Redis that is configured but not listening fails fast. Without this, `Redis.from_url`
# inherits the OS connect timeout and every request that touches the queue hangs for it.
_CONNECT_TIMEOUT_SECONDS = 1.0


@lru_cache
def get_queue(settings: Settings | None = None) -> Queue:
    settings = settings or get_settings()
    connection = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=_CONNECT_TIMEOUT_SECONDS,
    )
    return Queue(name=settings.queue_name, connection=connection)


# Reachability is cached briefly: a healthy ping is sub-millisecond, but an unreachable Redis costs
# the connect timeout, and without a cache every job POST and every /health would pay it.
_AVAILABILITY_TTL_SECONDS = 5.0
_availability: tuple[float, bool] | None = None


def queue_available(settings: Settings | None = None) -> bool:
    """Whether a worker queue is actually **reachable**, not merely configured.

    `REDIS_URL` has a non-empty default (`redis://localhost:6379`), so "configured" is the state a
    developer is in by default — before `docker compose up`, and any time Redis is down. Treating
    that as "a queue exists" made every job a 500 from a failed enqueue, and made `/health` report
    `bulk_enabled: true` on a machine where bulk could not run at all.

    The distinction matters in both directions: a small job should quietly fall back to running
    inline rather than failing, and a bulk job should be refused with the reason named rather than
    a stack trace.
    """
    global _availability
    settings = settings or get_settings()

    if settings.redis_url in ("", "memory"):
        return False

    now = time.monotonic()
    if _availability is not None and now - _availability[0] < _AVAILABILITY_TTL_SECONDS:
        return _availability[1]

    try:
        get_queue(settings).connection.ping()
        reachable = True
    except Exception:
        reachable = False

    _availability = (now, reachable)
    return reachable


def reset_availability_cache() -> None:
    """Test-only: forget the cached probe so a test can flip reachability."""
    global _availability
    _availability = None


# Wall-clock budget per image, used to size the RQ timeout. Generous on purpose: it has to cover
# a throttled vendor's backoff, not just a healthy call, and the cost of overestimating is a job
# that is allowed to finish while the cost of underestimating is one killed at 90%.
#
# 60, not the 20 this started at. Measured on the client's own PSDs — 5504x8256, 45.4 MP — where
# decode plus the imaging pipeline is ~45s per image before a vendor is even involved. At 20s
# their 132-image order would have been budgeted 44 minutes against a real ~1.7 hours, so RQ
# would have killed it around the two-thirds mark, having paid for every image processed and
# never reaching `_finalise` to bundle any of them.
_SECONDS_PER_IMAGE = 60

# Floor, for a job whose image count is unknown or tiny.
_MIN_TIMEOUT_SECONDS = 1800


def enqueue_job(job_id: str, settings: Settings | None = None, *, image_count: int = 0) -> None:
    """Enqueue a job with a timeout that fits its size.

    The timeout was a fixed 30 minutes, which is ample for the twenty-image case it was written
    for and kills a 400-image job partway through — RQ stops the worker mid-run, so the images
    already processed are paid for and the job never reaches `_finalise` to bundle them.
    """
    from app.jobs import run_job

    timeout = max(_MIN_TIMEOUT_SECONDS, image_count * _SECONDS_PER_IMAGE)
    get_queue(settings or get_settings()).enqueue(run_job, job_id, job_timeout=timeout)

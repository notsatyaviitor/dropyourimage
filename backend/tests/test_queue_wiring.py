"""The real-queue path, exercised the way the app calls it.

The rest of the suite runs with `REDIS_URL=memory`, which `queue_available` short-circuits before
it touches `app/queue.py` at all. That left the entire RQ path — the one every real deployment
uses — with no coverage, and it was broken: `get_queue` was `@lru_cache`d on a `Settings` object,
which pydantic makes unhashable, so every call raised `TypeError` before reaching Redis.

`queue_available` catches broad `Exception` as "Redis is unreachable", so the TypeError read as a
down queue on a perfectly healthy Redis. Consequences, all silent:

* `/health` reported `bulk_enabled: false`
* every job fell back to running inline in the request handler
* a running `rq worker` never received anything
* any order over `INLINE_JOB_MAX_IMAGES` was refused with "bulk requires a worker"

These tests need no Redis: they stub the connection. What they pin is the *wiring* — that the
functions can be called with a `Settings` instance without raising, which is the part that broke.
"""

from __future__ import annotations

import pytest

from app import queue as q
from app.core.settings import Settings


@pytest.fixture
def real_redis_settings() -> Settings:
    """A configuration that does NOT short-circuit — i.e. one that reaches `get_queue`."""
    return Settings(_env_file=None, redis_url="redis://localhost:6379", queue_name="dyi")


@pytest.fixture(autouse=True)
def _clear_caches():
    q.reset_availability_cache()
    q._cached_queue.cache_clear()
    yield
    q.reset_availability_cache()
    q._cached_queue.cache_clear()


class TestGetQueue:
    def test_it_accepts_a_settings_object_without_raising(self, real_redis_settings):
        """The exact call `queue_available` and `enqueue_job` make.

        This is the regression: `Settings` is unhashable, so an `@lru_cache` keyed on it raised
        `TypeError` here and the whole async path silently died.
        """
        queue = q.get_queue(real_redis_settings)
        assert queue.name == "dyi"

    def test_it_honours_the_configured_queue_name(self):
        """`rq worker` listens on a named queue; a mismatch means the worker sits idle."""
        other = Settings(_env_file=None, redis_url="redis://localhost:6379", queue_name="other")
        assert q.get_queue(other).name == "other"

    def test_the_connection_is_reused_per_configuration(self, real_redis_settings):
        """Cached on primitives rather than rebuilt per call — a connection pool per request
        would exhaust file descriptors under load."""
        assert q.get_queue(real_redis_settings) is q.get_queue(real_redis_settings)

    def test_a_different_configuration_gets_its_own_queue(self, real_redis_settings):
        other = Settings(_env_file=None, redis_url="redis://localhost:6379", queue_name="other")
        assert q.get_queue(real_redis_settings) is not q.get_queue(other)


class TestQueueAvailability:
    def test_a_reachable_redis_reports_available(self, real_redis_settings, monkeypatch):
        monkeypatch.setattr(q, "get_queue", lambda s=None: _FakeQueue(ping_ok=True))
        assert q.queue_available(real_redis_settings) is True

    def test_an_unreachable_redis_reports_unavailable(self, real_redis_settings, monkeypatch):
        monkeypatch.setattr(q, "get_queue", lambda s=None: _FakeQueue(ping_ok=False))
        assert q.queue_available(real_redis_settings) is False

    def test_memory_mode_short_circuits_without_touching_redis(self):
        """Why the rest of the suite never exercised any of the above."""
        memory = Settings(_env_file=None, redis_url="memory")
        assert q.queue_available(memory) is False

    def test_health_reports_bulk_enabled_when_the_queue_is_reachable(
        self, real_redis_settings, monkeypatch
    ):
        """The user-visible symptom of the bug: bulk refused on a healthy Redis."""
        monkeypatch.setattr(q, "get_queue", lambda s=None: _FakeQueue(ping_ok=True))
        assert q.queue_available(real_redis_settings) is True


class _FakeQueue:
    def __init__(self, *, ping_ok: bool) -> None:
        self.connection = _FakeConnection(ping_ok)
        self.name = "dyi"


class _FakeConnection:
    def __init__(self, ping_ok: bool) -> None:
        self._ok = ping_ok

    def ping(self) -> bool:
        if not self._ok:
            raise ConnectionError("redis is down")
        return True

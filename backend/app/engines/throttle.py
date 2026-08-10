"""Job-wide adaptive rate governor for vendor calls.

## Why per-call backoff is not enough

`_HttpEngine._call_with_retries` already backs off on a 429, honouring the vendor's own
`Retry-After`. That is correct and it is not sufficient at scale, because it is *per call*: with
`WORKER_CONCURRENCY` images in flight and two engines per image, one worker discovering that the
vendor is throttling teaches the other fifteen nothing. They keep firing into a vendor that has
already said stop, each collecting its own 429, each burning a retry from its own budget. The
observed failure mode is a batch that spends its entire retry allowance in the first few seconds
of a throttle and then fails a hundred images that would have succeeded thirty seconds later.

This governor is the shared piece of knowledge: **one 429 anywhere pauses everyone, and shrinks
how many are allowed to run at once.**

## Shape: additive increase, multiplicative decrease

The same control law TCP uses, for the same reason — it finds a sustainable rate without being
told what the limit is, which matters here because vendor limits are undocumented, tier-dependent
and change without notice. Photoroom's free tier was measured refusing even a single call with
`"Expected available in 8705 seconds"` (see docs/LIMITATIONS.md), so a fixed concurrency number
tuned against one account is worthless against another.

* **429 → halve** the concurrency limit and pause every worker for the vendor's `Retry-After`.
* **Sustained success → +1**, slowly, up to the original ceiling.

## Why a contextvar rather than a parameter

The throttle has to be visible at the vendor call site in `app/engines/http.py`, which sits five
frames below `app/jobs.py` behind `pipeline.process_image` and the engine `Protocol`. Threading it
through would mean putting a rate-limiting argument into `BackgroundRemover.alpha_for` — the
interface `backend/CLAUDE.md` says to keep as small as it is — and into the pure pipeline
signature, for a concern neither of them has.

`contextvars` propagate into tasks at creation, so `asyncio.gather` in `jobs.py` hands every image
the job's own governor with no plumbing. Note this is deliberately *not* in `app/imaging/`, which
stays free of globals; `app/engines/` is the I/O layer, where ambient request context belongs.

With no throttle installed — a unit test, or `pipeline.process_image` called directly — every
method here is a no-op and behaviour is exactly what it was before.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

# Longest pause a single Retry-After can impose. The vendor is trusted about *whether* it is
# throttling and only partly about *for how long*: an 8705-second hint is honest and also longer
# than any demo, so past this the job should fail visibly with `vendor_rate_limited` rather than
# appear to hang for two and a half hours.
_MAX_PAUSE_SECONDS = 120.0

# Used when a 429 arrives with no Retry-After at all.
_DEFAULT_PAUSE_SECONDS = 5.0

# Consecutive successes at the current limit before widening by one. Deliberately unhurried —
# recovering fast just re-triggers the throttle and wastes another round of retry budget.
_SUCCESSES_BEFORE_WIDENING = 12

# Granularity of the waiting loop. A slot is held across a whole vendor call (measured in
# seconds), so polling this often is free relative to the work, and it avoids the notify/timeout
# races that a Condition would need handling for.
_POLL_SECONDS = 0.05


class _VendorGovernor:
    """The control law for **one** vendor. See `JobThrottle` for why they are kept apart."""

    def __init__(self, ceiling: int, *, floor: int = 1) -> None:
        self._ceiling = max(1, ceiling)
        self._floor = max(1, min(floor, self._ceiling))
        self._limit = self._ceiling
        self._active = 0
        self._resume_at = 0.0
        self._successes = 0
        self._lock = asyncio.Lock()
        self.rate_limit_events = 0
        """Count of throttle trips, for the job to report. Never used for control decisions."""

    @property
    def limit(self) -> int:
        """Current concurrency ceiling. Exposed for tests and progress reporting."""
        return self._limit

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one of the currently-permitted call slots for the duration of a vendor call."""
        await self._enter()
        try:
            yield
        finally:
            async with self._lock:
                self._active -= 1

    async def _enter(self) -> None:
        while True:
            async with self._lock:
                wait = self._resume_at - time.monotonic()
                if wait <= 0 and self._active < self._limit:
                    self._active += 1
                    return
            await asyncio.sleep(_POLL_SECONDS if wait <= 0 else min(wait, _POLL_SECONDS * 20))

    async def on_rate_limited(self, retry_after: float | None) -> None:
        """A vendor said stop. Halve the limit and pause everyone.

        The pause applies to calls not yet started; a call already in flight is left alone, since
        cancelling it would waste a request the vendor has already accepted.
        """
        pause = _DEFAULT_PAUSE_SECONDS if retry_after is None else retry_after
        pause = max(0.0, min(pause, _MAX_PAUSE_SECONDS))

        async with self._lock:
            self._limit = max(self._floor, self._limit // 2)
            self._resume_at = max(self._resume_at, time.monotonic() + pause)
            self._successes = 0
            self.rate_limit_events += 1

    async def on_success(self) -> None:
        """A call came back clean. Widen by one after enough of them in a row."""
        async with self._lock:
            if self._limit >= self._ceiling:
                return
            self._successes += 1
            if self._successes >= _SUCCESSES_BEFORE_WIDENING:
                self._limit += 1
                self._successes = 0


class JobThrottle:
    """One governor per vendor, for one job.

    **Per vendor, not per job**, and that distinction is the whole reason this class exists rather
    than a bare `_VendorGovernor`. The default pool runs Photoroom and fal.ai concurrently on every
    image, and Gemini serves the locator and the tie-break on top. These are unrelated accounts
    with unrelated quotas — Photoroom exhausting its free tier says nothing whatsoever about
    fal.ai's. A single shared governor would take the correct observation "Photoroom is throttling"
    and draw the false conclusion "slow everything down", halving throughput on a vendor that was
    perfectly healthy, right when the job most needs it to take up the slack.

    Governors are created on demand, so an engine that is never called never constrains anything.
    """

    def __init__(self, ceiling: int, *, floor: int = 1) -> None:
        self._ceiling = ceiling
        self._floor = floor
        self._governors: dict[str, _VendorGovernor] = {}

    def _for(self, vendor: str) -> _VendorGovernor:
        governor = self._governors.get(vendor)
        if governor is None:
            governor = _VendorGovernor(self._ceiling, floor=self._floor)
            self._governors[vendor] = governor
        return governor

    def rate_limit_events(self) -> int:
        """Total throttle trips across every vendor. Reported, never used for control."""
        return sum(g.rate_limit_events for g in self._governors.values())

    def limits(self) -> dict[str, int]:
        """Current per-vendor concurrency ceilings. For tests and progress reporting."""
        return {vendor: g.limit for vendor, g in self._governors.items()}


_current: contextvars.ContextVar[JobThrottle | None] = contextvars.ContextVar(
    "dyi_vendor_throttle", default=None
)


def current() -> JobThrottle | None:
    """The throttle for the job running in this context, or None when nothing installed one."""
    return _current.get()


@contextmanager
def use(job_throttle: JobThrottle | None) -> Iterator[None]:
    """Install a throttle for everything started inside this block."""
    token = _current.set(job_throttle)
    try:
        yield
    finally:
        _current.reset(token)


@asynccontextmanager
async def slot(vendor: str) -> AsyncIterator[None]:
    """Hold a call slot for `vendor` if a throttle is installed; a no-op otherwise."""
    job_throttle = current()
    if job_throttle is None:
        yield
        return
    async with job_throttle._for(vendor).slot():
        yield


async def report_rate_limited(vendor: str, retry_after: float | None) -> None:
    job_throttle = current()
    if job_throttle is not None:
        await job_throttle._for(vendor).on_rate_limited(retry_after)


async def report_success(vendor: str) -> None:
    job_throttle = current()
    if job_throttle is not None:
        await job_throttle._for(vendor).on_success()

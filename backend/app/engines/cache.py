"""Cut-out caching — stage 1's cost control.

Segmentation is deterministic per engine: the same bytes sent to the same vendor come back with
the same mask. So the *only* reason to call a paid API twice for one image is that we threw the
first answer away. Tuning a background colour, a canvas size or a centring mode re-runs stages
2-5, and none of them can change the mask — so a re-run must cost nothing.

Without this, a demo session that tries six background colours on a 50-image zip bills 300
segmentation calls instead of 50. At remove.bg's ~$0.20 that is $60 versus $10, for identical
output.

Two deliberate choices:

* **Alpha is stored as lossless float32**, not as an 8-bit PNG. Quantising to 256 levels on every
  cache round trip would erode exactly the soft edges that `backend/CLAUDE.md` invariant 2 exists
  to protect, and it would erode them a little more each time. A cached mask must be
  *bit-identical* to the one the vendor returned, or the determinism tests are lying.
* **It reuses `StorageBackend`** rather than introducing a second persistence mechanism. That
  backend is already a Protocol with `put`/`get`/`exists`, already works in both memory and S3
  modes, and already has no auth surface of its own. Cache entries are namespaced under
  `cutouts/` instead of `jobs/`, so they deliberately outlive the job that produced them — a
  per-job cache would miss the re-run case, which is the whole point.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
from typing import AsyncContextManager, Protocol, runtime_checkable

import numpy as np

from app.engines.base import AlphaResult
from app.models import EngineId

CACHE_PREFIX = "cutouts"

# Bumped if the stored representation changes. An old entry then simply misses rather than being
# misread as the new format — a silently misinterpreted mask would be far worse than a re-billed
# API call.
CACHE_FORMAT_VERSION = "v1"


@runtime_checkable
class CutoutCache(Protocol):
    """Keyed on ``sha256(image_bytes) + engine_id`` — see `app.pipeline.cache_key`."""

    def get(self, key: str) -> np.ndarray | None:
        """The cached mask, or None on a miss. Must never raise for an absent key."""
        ...

    def put(self, key: str, alpha: np.ndarray) -> None:
        """Store a mask. Failures must be swallowed — see `StorageCutoutCache.put`."""
        ...

    def entry_lock(self, key: str) -> AsyncContextManager[None]:
        """Serialise concurrent work on one key — see `StorageCutoutCache.entry_lock`."""
        ...


class NullCutoutCache:
    """Caches nothing. The default, so `pipeline.process_image` needs no storage to be testable."""

    def get(self, key: str) -> np.ndarray | None:
        return None

    def put(self, key: str, alpha: np.ndarray) -> None:
        return None

    def entry_lock(self, key: str) -> AsyncContextManager[None]:
        # No cache to populate, so serialising identical images would only cost latency.
        return contextlib.nullcontext()


class StorageCutoutCache:
    """Persists masks through any `StorageBackend`.

    Every method is failure-tolerant by design. A cache is an optimisation: if it is unreachable,
    the correct behaviour is to pay for the API call again, not to fail the image. So a broken
    cache costs money and never correctness.
    """

    def __init__(self, storage, *, prefix: str = CACHE_PREFIX) -> None:
        self._storage = storage
        self._prefix = prefix
        self._locks: dict[str, asyncio.Lock] = {}

    def _key(self, key: str) -> str:
        return f"{self._prefix}/{CACHE_FORMAT_VERSION}/{key}.npy"

    def entry_lock(self, key: str) -> AsyncContextManager[None]:
        """Serialise concurrent lookups of the *same* key, so only one vendor call is made.

        Without this, duplicate images in one zip are a cache miss for every copy: the job fans out
        with `WORKER_CONCURRENCY` images in flight, they all check the cache before any of them has
        written, and every copy is billed. Observed live — two byte-identical images in a two-image
        zip cost two remove.bg credits instead of one.

        Locking is per key, so distinct images still run fully in parallel; only genuine duplicates
        wait, and they wait exactly long enough to turn into a cache hit.
        """
        # Created lazily and kept for the cache's lifetime. Bounded by the number of distinct
        # images in a job, which the zip-entry cap already limits.
        return self._locks.setdefault(key, asyncio.Lock())

    def get(self, key: str) -> np.ndarray | None:
        try:
            raw = self._storage.get(self._key(key))
        except Exception:
            # KeyError on a miss; anything else means the backend is unhealthy. Both are a miss.
            return None

        try:
            alpha = np.load(io.BytesIO(raw), allow_pickle=False)
        except Exception:
            return None

        # A corrupt or wrong-shaped entry must not reach the pipeline, which assumes 2-D float32
        # in [0, 1]. Treat anything unexpected as a miss rather than trusting stored bytes.
        if alpha.ndim != 2 or alpha.dtype != np.float32:
            return None
        return alpha

    def put(self, key: str, alpha: np.ndarray) -> None:
        a = np.asarray(alpha, dtype=np.float32)
        if a.ndim != 2:
            return
        buf = io.BytesIO()
        # allow_pickle stays off in both directions: these bytes round-trip through object
        # storage, and a pickle payload there would be arbitrary code execution on read.
        np.save(buf, a, allow_pickle=False)
        try:
            self._storage.put(self._key(key), buf.getvalue(), "application/octet-stream")
        except Exception:
            return


def hit_result(alpha: np.ndarray, engine: EngineId) -> AlphaResult:
    """Build the `AlphaResult` for a cache hit.

    ``cost_usd`` is zero and ``latency_ms`` reflects the cache read, not the original call. Both
    matter: `jobs.py` sums `cost_usd` into the job's spend, so reporting the original price would
    overstate the ledger by exactly the amount this module exists to save.
    """
    return AlphaResult(alpha=alpha, engine=engine, latency_ms=0, cost_usd=0.0, cache_hit=True)

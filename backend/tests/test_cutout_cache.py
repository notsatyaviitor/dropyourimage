"""Cut-out caching, and the output-size guard.

Both exist for the same reason: an unmetered cost. The cache stops a re-run re-billing the
segmentation vendor; the size guard stops a tiny request body allocating gigabytes.

The cache tests use a counting engine rather than a real vendor, because the property under test
is "how many times was the engine called", which no amount of pixel comparison can show.
"""

from __future__ import annotations

import asyncio
import io

import numpy as np
import pytest
from PIL import Image

from app import pipeline
from app.core import errors
from app.core.settings import Settings
from app.engines.base import AlphaResult
from app.engines.cache import (
    NullCutoutCache,
    StorageCutoutCache,
    hit_result,
)
from app.engines.local import LocalEngine
from app.imaging import export as E
from app.models import (
    BackgroundSpec,
    ErrorCode,
    ImageState,
    JobConfig,
    OutputFormat,
    SizeSpec,
)
from app.storage.memory import MemoryStorage
from tests.test_shadow import studio_scene


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        photoroom_api_key="",
        removebg_api_key="",
        fal_key="",
        gemini_api_key="",
        adobe_client_id="",
    )


def studio_png(size: int = 120) -> bytes:
    observed, _, _ = studio_scene(size=size)
    return E.encode(observed, None, fmt=OutputFormat.PNG)


class CountingEngine:
    """A real engine wrapped in a call counter — the cache is invisible in pixels alone."""

    def __init__(self) -> None:
        self.inner = LocalEngine()
        self.id = self.inner.id
        self.cost_per_image_usd = 0.25          # pretend it is billed, so cost is observable
        self.calls = 0

    def available(self) -> bool:
        return True

    async def alpha_for(self, image_bytes: bytes, width: int, height: int) -> AlphaResult:
        self.calls += 1
        r = await self.inner.alpha_for(image_bytes, width, height)
        return AlphaResult(
            alpha=r.alpha, engine=r.engine, latency_ms=r.latency_ms, cost_usd=0.25
        )


# ---------------------------------------------------------------------------
# The cache store itself
# ---------------------------------------------------------------------------


class TestStorageCutoutCache:
    def test_round_trip_is_bit_identical(self):
        """Soft alpha must survive caching exactly — invariant 2 erodes if it does not."""
        cache = StorageCutoutCache(MemoryStorage())
        alpha = np.linspace(0, 1, 64 * 64, dtype=np.float32).reshape(64, 64)

        cache.put("k", alpha)
        got = cache.get("k")

        assert got is not None
        assert got.dtype == np.float32
        assert np.array_equal(got, alpha), "a cached mask must be bit-identical, not merely close"

    def test_an_8_bit_round_trip_would_have_failed_that(self):
        """Pins why the store is .npy and not a PNG: 8 bits loses the gradient."""
        alpha = np.linspace(0, 1, 64 * 64, dtype=np.float32).reshape(64, 64)
        as_png = np.asarray(
            Image.fromarray((alpha * 255).round().astype(np.uint8))
        ).astype(np.float32) / 255.0
        assert not np.array_equal(as_png, alpha)
        assert np.abs(as_png - alpha).max() > 1e-4

    def test_a_miss_returns_none_rather_than_raising(self):
        assert StorageCutoutCache(MemoryStorage()).get("never-stored") is None

    def test_corrupt_entry_is_treated_as_a_miss(self):
        """A bad stored blob must not reach the pipeline, which assumes 2-D float32."""
        storage = MemoryStorage()
        cache = StorageCutoutCache(storage)
        storage.put(cache._key("k"), b"not an npy file at all", "application/octet-stream")
        assert cache.get("k") is None

    def test_wrong_shape_is_treated_as_a_miss(self):
        cache = StorageCutoutCache(MemoryStorage())
        cache.put("k", np.zeros((4, 4), np.float32))
        # Overwrite with a 3-D array through the raw backend.
        buf = io.BytesIO()
        np.save(buf, np.zeros((4, 4, 3), np.float32), allow_pickle=False)
        cache._storage.put(cache._key("k"), buf.getvalue(), "application/octet-stream")
        assert cache.get("k") is None

    def test_a_broken_backend_degrades_to_a_miss_not_an_error(self):
        """A cache is an optimisation. If it breaks, pay for the API call — never fail the image."""

        class Broken:
            def put(self, *a, **k):
                raise RuntimeError("storage down")

            def get(self, *a, **k):
                raise RuntimeError("storage down")

        cache = StorageCutoutCache(Broken())
        cache.put("k", np.zeros((4, 4), np.float32))     # must not raise
        assert cache.get("k") is None

    def test_null_cache_never_stores(self):
        c = NullCutoutCache()
        c.put("k", np.zeros((4, 4), np.float32))
        assert c.get("k") is None

    def test_hit_result_reports_zero_cost(self):
        """cost_usd feeds the job ledger; a hit that reported the original price would inflate it."""
        r = hit_result(np.zeros((4, 4), np.float32), LocalEngine().id)
        assert r.cache_hit is True
        assert r.cost_usd == 0.0

    def test_keys_are_namespaced_and_versioned(self):
        key = StorageCutoutCache(MemoryStorage())._key("abc:photoroom")
        assert key.startswith("cutouts/v1/")
        assert "jobs/" not in key, "cache must outlive the job that produced it"


# ---------------------------------------------------------------------------
# The cache in the pipeline
# ---------------------------------------------------------------------------


class TestCacheInThePipeline:
    async def test_second_run_does_not_call_the_engine(self, settings):
        """The actual requirement: tuning colour must cost nothing."""
        cache = StorageCutoutCache(MemoryStorage())
        engine = CountingEngine()
        blob = studio_png()

        cfg1 = JobConfig(background=BackgroundSpec(color="#1E3A8A"), size=SizeSpec(width=200, height=200))
        cfg2 = JobConfig(background=BackgroundSpec(color="#C81E1E"), size=SizeSpec(width=400, height=300))

        r1, _ = await pipeline.process_image(blob, "a.png", cfg1, settings, engines=[engine], cache=cache)
        r2, _ = await pipeline.process_image(blob, "a.png", cfg2, settings, engines=[engine], cache=cache)

        assert engine.calls == 1, "the second run must be served from cache"
        assert r1.cache_hit is False and r1.cost_usd == 0.25
        assert r2.cache_hit is True and r2.cost_usd == 0.0

    async def test_cached_run_is_byte_identical_to_the_uncached_one(self, settings):
        """A cache that changes output pixels is a bug, not an optimisation."""
        blob = studio_png()
        cfg = JobConfig(background=BackgroundSpec(color="#1E3A8A"), size=SizeSpec(width=250, height=250))

        _, uncached = await pipeline.process_image(
            blob, "a.png", cfg, settings, engines=[CountingEngine()], cache=NullCutoutCache()
        )

        cache = StorageCutoutCache(MemoryStorage())
        engine = CountingEngine()
        await pipeline.process_image(blob, "a.png", cfg, settings, engines=[engine], cache=cache)
        _, from_cache = await pipeline.process_image(
            blob, "a.png", cfg, settings, engines=[engine], cache=cache
        )

        assert from_cache[OutputFormat.PNG] == uncached[OutputFormat.PNG]

    async def test_a_different_image_is_not_a_hit(self, settings):
        cache = StorageCutoutCache(MemoryStorage())
        engine = CountingEngine()
        cfg = JobConfig(size=SizeSpec(width=200, height=200))

        await pipeline.process_image(studio_png(100), "a.png", cfg, settings, engines=[engine], cache=cache)
        await pipeline.process_image(studio_png(140), "b.png", cfg, settings, engines=[engine], cache=cache)

        assert engine.calls == 2, "the key includes the image digest"

    async def test_cache_cutouts_false_disables_it(self):
        """The setting was previously never read; this pins that it now is."""
        off = Settings(_env_file=None, photoroom_api_key="", removebg_api_key="", fal_key="",
                       cache_cutouts=False)
        cache = StorageCutoutCache(MemoryStorage())
        engine = CountingEngine()
        blob = studio_png()
        cfg = JobConfig(size=SizeSpec(width=200, height=200))

        await pipeline.process_image(blob, "a.png", cfg, off, engines=[engine], cache=cache)
        await pipeline.process_image(blob, "a.png", cfg, off, engines=[engine], cache=cache)

        assert engine.calls == 2

    async def test_duplicate_images_in_one_job_are_billed_once(self, settings):
        """Regression: found live, with real money.

        Two byte-identical images in one zip cost two remove.bg credits, because `jobs.py` fans out
        concurrently and every copy checked the cache before any had written. `entry_lock` coalesces
        them onto one call.
        """
        cache = StorageCutoutCache(MemoryStorage())
        engine = CountingEngine()
        blob = studio_png()
        cfg = JobConfig(size=SizeSpec(width=200, height=200))

        results = await asyncio.gather(
            *(
                pipeline.process_image(
                    blob, f"copy-{i}.png", cfg, settings, engines=[engine], cache=cache
                )
                for i in range(6)
            )
        )

        assert engine.calls == 1, f"6 identical images billed {engine.calls} times"
        hits = [r.cache_hit for r, _ in results]
        assert sum(hits) == 5, f"exactly one should pay, five should hit: {hits}"
        assert sum(r.cost_usd for r, _ in results) == pytest.approx(0.25)

        # And every copy must still produce identical bytes.
        pngs = {out[OutputFormat.PNG] for _, out in results}
        assert len(pngs) == 1

    async def test_distinct_images_still_run_concurrently(self, settings):
        """The lock is per key, so it must not serialise different images."""
        cache = StorageCutoutCache(MemoryStorage())
        engine = CountingEngine()
        cfg = JobConfig(size=SizeSpec(width=200, height=200))

        await asyncio.gather(
            *(
                pipeline.process_image(
                    studio_png(100 + i * 20), f"i-{i}.png", cfg, settings,
                    engines=[engine], cache=cache,
                )
                for i in range(4)
            )
        )
        assert engine.calls == 4, "distinct images must each be segmented"

    async def test_no_cache_supplied_still_works(self, settings):
        """process_image must stay usable with no storage at all."""
        engine = CountingEngine()
        blob = studio_png()
        cfg = JobConfig(size=SizeSpec(width=200, height=200))

        r, _ = await pipeline.process_image(blob, "a.png", cfg, settings, engines=[engine])
        assert r.state is ImageState.DONE
        assert r.cache_hit is False


# ---------------------------------------------------------------------------
# Output size guard
# ---------------------------------------------------------------------------


class TestOutputSizeGuard:
    async def test_an_oversized_canvas_is_rejected_by_code_not_by_an_oom(self, settings):
        """SizeSpec allows 20000x20000 = 400 MP, roughly 8 GB of float32 buffers.

        The input side has always had `max_image_pixels`; this is the same control on the output,
        which was missing. It must fail as a typed error, not as a killed process.
        """
        cfg = JobConfig(size=SizeSpec(width=20000, height=20000))
        result, outputs = await pipeline.process_image(
            studio_png(), "a.png", cfg, settings, engines=[LocalEngine()]
        )

        assert result.state is ImageState.FAILED
        assert result.error is not None
        assert result.error.code is ErrorCode.OUTPUT_TOO_LARGE
        assert outputs == {}

    async def test_the_message_names_the_limit_without_leaking_config(self, settings):
        cfg = JobConfig(size=SizeSpec(width=20000, height=20000))
        result, _ = await pipeline.process_image(
            studio_png(), "a.png", cfg, settings, engines=[LocalEngine()]
        )
        msg = result.error.message
        assert "400 MP" in msg and "80 MP" in msg
        for leak in ("/", "api_key", "redis", "minio", "secret"):
            assert leak not in msg.lower()

    async def test_an_ordinary_canvas_is_unaffected(self, settings):
        cfg = JobConfig(size=SizeSpec(width=500, height=500))
        result, outputs = await pipeline.process_image(
            studio_png(), "a.png", cfg, settings, engines=[LocalEngine()]
        )
        assert result.state is ImageState.DONE
        assert outputs

    async def test_the_limit_is_configurable_per_deployment(self):
        """It is a deployment limit, not a contract rule — a bigger machine can raise it."""
        generous = Settings(_env_file=None, photoroom_api_key="", removebg_api_key="", fal_key="",
                            max_output_pixels=400_000_000)
        # 4000x4000 = 16 MP: over a tightened limit, under the generous one.
        tight = Settings(_env_file=None, photoroom_api_key="", removebg_api_key="", fal_key="",
                         max_output_pixels=1_000_000)
        cfg = JobConfig(size=SizeSpec(width=4000, height=4000))

        r_tight, _ = await pipeline.process_image(
            studio_png(), "a.png", cfg, tight, engines=[LocalEngine()]
        )
        assert r_tight.error.code is ErrorCode.OUTPUT_TOO_LARGE

        r_ok, outputs = await pipeline.process_image(
            studio_png(), "a.png", cfg, generous, engines=[LocalEngine()]
        )
        assert r_ok.state is ImageState.DONE and outputs

    def test_the_guard_raises_the_typed_error_directly(self, settings):
        with pytest.raises(errors.OutputTooLarge):
            pipeline._check_output_size(JobConfig(size=SizeSpec(width=20000, height=20000)), settings)

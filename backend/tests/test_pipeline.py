"""End-to-end pipeline tests: all five stages, offline.

Uses the local control engine, so these run with no API keys and no network. That is the point —
the imaging core cannot be blocked by procurement.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from app import pipeline
from app.core.settings import Settings
from app.engines.base import AlphaResult
from app.engines.local import LocalEngine
from app.imaging import color as C
from app.imaging import export as E
from app.models import (
    BackgroundSpec,
    CentringSpec,
    EngineId,
    ErrorCode,
    ExportSpec,
    ImageState,
    JobConfig,
    Note,
    OutputFormat,
    ShadowMode,
    SizeSpec,
)
from tests.test_shadow import busy_scene, studio_scene


@pytest.fixture
def settings() -> Settings:
    """Settings with no vendor keys — forces the offline control engine."""
    return Settings(
        photoroom_api_key="",
        removebg_api_key="",
        fal_key="",
        gemini_api_key="",
        adobe_client_id="",
    )


@pytest.fixture
def engines():
    return [LocalEngine()]


def studio_png(size: int = 200, with_shadow: bool = True) -> bytes:
    observed, _, _ = studio_scene(size=size, with_shadow=with_shadow)
    return E.encode(observed, None, fmt=OutputFormat.PNG)


def pixels(blob: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(blob)).convert("RGB"))


# ---------------------------------------------------------------------------
# The four core requirements, end to end
# ---------------------------------------------------------------------------


class TestTheFourRequirements:
    async def test_all_four_together(self, settings, engines):
        """Background removed, exact hex background, exact 500x500, product centred."""
        config = JobConfig(
            background=BackgroundSpec(color="#1E3A8A"),
            size=SizeSpec(width=500, height=500, margin_pct=5),
            export=ExportSpec(formats=[OutputFormat.PNG]),
        )
        result, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )

        assert result.state is ImageState.DONE, result.error
        arr = pixels(outputs[OutputFormat.PNG])

        # 3. exact size
        assert arr.shape == (500, 500, 3)
        # 2. exact background colour, in the corner where nothing else is
        assert tuple(arr[3, 3]) == C.hex_to_srgb8("#1E3A8A")
        # 4. centred, measured not assumed
        assert abs(result.centroid_offset_px[0]) < 3.0
        assert abs(result.centroid_offset_px[1]) < 3.0
        # 1. background actually removed - the original white sweep is gone
        assert not (arr > 230).all(axis=-1).any(), "no white backdrop should survive"

    @pytest.mark.parametrize("hexv", ["#FFFFFF", "#000000", "#F5F5F5", "#1E3A8A", "#C81E1E"])
    async def test_background_hex_is_exact_for_any_colour(self, settings, engines, hexv):
        config = JobConfig(
            background=BackgroundSpec(color=hexv),
            size=SizeSpec(width=300, height=300, margin_pct=10),
        )
        result, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        assert result.state is ImageState.DONE
        assert tuple(pixels(outputs[OutputFormat.PNG])[2, 2]) == C.hex_to_srgb8(hexv)

    @pytest.mark.parametrize("w,h", [(500, 500), (1000, 1000), (1200, 628), (256, 256)])
    async def test_output_size_is_exact(self, settings, engines, w, h):
        config = JobConfig(size=SizeSpec(width=w, height=h))
        _, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        assert pixels(outputs[OutputFormat.PNG]).shape == (h, w, 3)

    async def test_transparent_output_keeps_alpha(self, settings, engines):
        config = JobConfig(
            background=BackgroundSpec(transparent=True),
            export=ExportSpec(formats=[OutputFormat.PNG]),
        )
        result, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        assert result.state is ImageState.DONE
        img = Image.open(io.BytesIO(outputs[OutputFormat.PNG]))
        assert img.mode == "RGBA"
        assert np.asarray(img)[..., 3].min() == 0, "backdrop must be fully transparent"


# ---------------------------------------------------------------------------
# Shadow preservation
# ---------------------------------------------------------------------------


class TestShadowPreservation:
    async def test_shadow_survives_onto_the_new_background(self, settings, engines):
        config = JobConfig(
            background=BackgroundSpec(color="#F5F5F5", shadow=ShadowMode.PRESERVE),
            size=SizeSpec(width=400, height=400, margin_pct=2),
        )
        result, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        assert result.state is ImageState.DONE
        assert Note.SHADOW_GATE_FAILED not in result.notes

        arr = pixels(outputs[OutputFormat.PNG]).astype(np.int32)
        light = C.hex_to_srgb8("#F5F5F5")[0]
        # Some background pixels must be darker than the fill: that is the shadow.
        darker = (arr[..., 0] < light - 8) & (arr[..., 1] < light - 8) & (arr[..., 2] < light - 8)
        assert darker.sum() > 200, "no shadow was carried onto the new background"

    async def test_removing_the_shadow_leaves_a_perfectly_flat_background(self, settings, engines):
        config = JobConfig(
            background=BackgroundSpec(color="#F5F5F5", shadow=ShadowMode.REMOVE),
            size=SizeSpec(width=400, height=400, margin_pct=2),
        )
        _, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        arr = pixels(outputs[OutputFormat.PNG])
        want = C.hex_to_srgb8("#F5F5F5")
        corners = [arr[2, 2], arr[2, -3], arr[-3, 2], arr[-3, -3]]
        for c in corners:
            assert tuple(c) == want

    async def test_busy_background_trips_the_gate_and_says_so(self, settings, engines):
        """A lifestyle shot must fail visibly, not produce a smear."""
        observed, _ = busy_scene(size=200)
        blob = E.encode(observed, None, fmt=OutputFormat.PNG)

        config = JobConfig(background=BackgroundSpec(color="#F5F5F5", shadow=ShadowMode.PRESERVE))
        result, outputs = await pipeline.process_image(
            blob, "lifestyle.png", config, settings, engines=engines
        )

        if result.state is ImageState.DONE:
            assert Note.SHADOW_GATE_FAILED in result.notes
            assert result.background_uniformity is not None
            assert result.background_uniformity < 0.5
        else:
            # The control engine may legitimately reject a busy scene outright; either outcome is
            # an honest failure rather than a bad asset.
            assert result.error is not None

    async def test_uniformity_is_always_reported(self, settings, engines):
        config = JobConfig()
        result, _ = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        assert result.background_uniformity is not None


# ---------------------------------------------------------------------------
# Formats and profiles
# ---------------------------------------------------------------------------


class TestFormats:
    async def test_multiple_formats_in_one_run(self, settings, engines):
        config = JobConfig(
            background=BackgroundSpec(color="#FFFFFF"),
            export=ExportSpec(
                formats=[OutputFormat.PNG, OutputFormat.JPEG, OutputFormat.TIFF, OutputFormat.WEBP]
            ),
        )
        result, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        assert result.state is ImageState.DONE
        assert set(outputs) == {
            OutputFormat.PNG,
            OutputFormat.JPEG,
            OutputFormat.TIFF,
            OutputFormat.WEBP,
        }
        for blob in outputs.values():
            assert len(blob) > 0

    async def test_adobe_rgb_export_embeds_a_profile(self, settings, engines):
        from app.models import ColorProfile

        config = JobConfig(
            export=ExportSpec(formats=[OutputFormat.PNG], profile=ColorProfile.ADOBE_RGB)
        )
        _, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        img = Image.open(io.BytesIO(outputs[OutputFormat.PNG]))
        assert img.info.get("icc_profile"), "Adobe RGB without a profile is unreadable"


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


class TestFailureHandling:
    async def test_a_bad_file_fails_without_raising(self, settings, engines):
        """One corrupt file in a 200-image zip must not take the job down."""
        result, outputs = await pipeline.process_image(
            b"not an image at all", "broken.png", JobConfig(), settings, engines=engines
        )
        assert result.state is ImageState.FAILED
        assert result.error is not None
        assert outputs == {}

    async def test_oversized_file_is_rejected_by_code_not_by_crashing(self, engines):
        small_limit = Settings(max_image_bytes=100)
        result, _ = await pipeline.process_image(
            studio_png(), "big.png", JobConfig(), small_limit, engines=engines
        )
        assert result.state is ImageState.FAILED
        assert result.error is not None
        assert "limit" in result.error.message.lower()

    async def test_duration_is_recorded_even_on_failure(self, settings, engines):
        result, _ = await pipeline.process_image(
            b"garbage", "broken.png", JobConfig(), settings, engines=engines
        )
        assert result.duration_ms is not None

    async def test_error_messages_never_leak_configuration(self, engines):
        leaky = Settings(photoroom_api_key="SECRET-KEY-12345")
        result, _ = await pipeline.process_image(
            b"garbage", "broken.png", JobConfig(), leaky, engines=engines
        )
        assert result.error is not None
        assert "SECRET" not in result.error.message


class TestNoUsableMask:
    """Every engine answered, but auto-pick rejected every mask.

    Measured on a real customer file: a 45 MP Canon CR3 of a bathroom interior. Photoroom returned
    a mask and billed $0.02; auto-pick rejected it as "fragmented (largest blob 52%)" because a
    furnished room has no single subject. That is a property of the photograph, so it must not be
    reported as a vendor fault the user should retry — retrying re-sends byte-identical pixels,
    gets the byte-identical rejection, and bills again. Same reasoning as `NoForegroundFound` and
    `VendorPayloadTooLarge` in app/core/errors.py, which were split out for exactly this.
    """

    class FragmentedEngine:
        """Returns speckle: real coverage, but no dominant blob. Rejected by solidity."""

        id = EngineId.LOCAL
        cost_per_image_usd = 0.02

        def available(self) -> bool:
            return True

        async def alpha_for(self, image_bytes: bytes, width: int, height: int):
            alpha = np.zeros((height, width), dtype=np.float32)
            alpha[::4, ::4] = 1.0
            return AlphaResult(alpha=alpha, engine=self.id, latency_ms=1, cost_usd=0.02)

    @pytest.fixture
    def fragmented(self):
        return [self.FragmentedEngine()]

    async def test_it_is_not_reported_as_a_retryable_vendor_error(self, settings, fragmented):
        result, outputs = await pipeline.process_image(
            studio_png(), "scene.png", JobConfig(), settings, engines=fragmented
        )
        assert result.state is ImageState.FAILED
        assert outputs == {}
        assert result.error is not None
        assert result.error.code is ErrorCode.NO_FOREGROUND_FOUND
        assert result.error.retryable is False, "the same bytes fail identically every time"

    async def test_the_message_carries_the_reason_and_the_way_out(self, settings, fragmented):
        result, _ = await pipeline.process_image(
            studio_png(), "scene.png", JobConfig(), settings, engines=fragmented
        )
        assert result.error is not None
        # The rejection reason from autopick, not just "unusable" — it is the only actionable
        # thing in this failure.
        assert "fragmented" in result.error.message
        assert "Subject" in result.error.message

    async def test_the_spend_is_still_recorded(self, settings, fragmented):
        """The vendor answered and billed. A failed image that cost money must say so."""
        result, _ = await pipeline.process_image(
            studio_png(), "scene.png", JobConfig(), settings, engines=fragmented
        )
        assert result.cost_usd == pytest.approx(0.02)
        assert result.candidates[0].rejected_reason is not None


class TestPreCutInput:
    async def test_an_already_cut_out_png_is_reused_not_re_segmented(self, settings, engines):
        """Re-segmenting an approved cut-out would cost money and probably be worse."""
        rgb = np.full((80, 80, 3), 0.5, dtype=np.float32)
        alpha = np.zeros((80, 80), dtype=np.float32)
        alpha[20:60, 20:60] = 1.0
        blob = E.encode(rgb, alpha, fmt=OutputFormat.PNG)

        result, outputs = await pipeline.process_image(
            blob, "cut.png", JobConfig(background=BackgroundSpec(color="#C81E1E")), settings,
            engines=engines,
        )
        assert result.state is ImageState.DONE
        assert result.chosen_engine is None, "no engine should have run"
        assert result.cost_usd == 0.0
        assert tuple(pixels(outputs[OutputFormat.PNG])[2, 2]) == C.hex_to_srgb8("#C81E1E")

    async def test_shadow_is_skipped_when_there_is_no_backdrop_to_measure(self, settings, engines):
        rgb = np.full((80, 80, 3), 0.5, dtype=np.float32)
        alpha = np.zeros((80, 80), dtype=np.float32)
        alpha[20:60, 20:60] = 1.0
        blob = E.encode(rgb, alpha, fmt=OutputFormat.PNG)

        result, _ = await pipeline.process_image(
            blob,
            "cut.png",
            JobConfig(background=BackgroundSpec(color="#FFFFFF", shadow=ShadowMode.PRESERVE)),
            settings,
            engines=engines,
        )
        assert Note.SHADOW_GATE_FAILED in result.notes


class TestDeterminism:
    async def test_the_same_input_produces_byte_identical_output(self, settings, engines):
        """Guards the deterministic stages against a model or random seed creeping in.

        If this fails, something non-reproducible entered stages 2-5. Do not loosen the
        comparison — find it.
        """
        config = JobConfig(
            background=BackgroundSpec(color="#1E3A8A"),
            size=SizeSpec(width=333, height=444, margin_pct=7.5),
            centring=CentringSpec(),
        )
        blob = studio_png()

        _, first = await pipeline.process_image(blob, "a.png", config, settings, engines=engines)
        _, second = await pipeline.process_image(blob, "a.png", config, settings, engines=engines)

        assert first[OutputFormat.PNG] == second[OutputFormat.PNG]

    async def test_stays_deterministic_across_a_real_time_gap(self, settings, engines):
        """Reproduces the actual bug that once broke determinism — a wall-clock gap, not load.

        Two rapid successive calls (the test above) rarely straddle a one-second boundary, so it
        did not catch this. `ImageCms.createProfile("sRGB")` embeds littleCMS's creation
        timestamp, which changes every second; the export stage called it fresh on every PNG
        write, so two runs of the identical job landing in different seconds produced genuinely
        different output bytes despite every pixel being identical. Fixed by caching
        `icc.srgb_profile()` — see `app/imaging/icc.py`. This test pins the regression by forcing
        the exact gap that exposed it, rather than hoping a fast test happens to hit it.
        """
        import time

        config = JobConfig(
            background=BackgroundSpec(color="#1E3A8A"),
            size=SizeSpec(width=200, height=200),
        )
        blob = studio_png(size=100)

        _, first = await pipeline.process_image(blob, "a.png", config, settings, engines=engines)
        time.sleep(1.1)
        _, second = await pipeline.process_image(blob, "a.png", config, settings, engines=engines)

        assert first[OutputFormat.PNG] == second[OutputFormat.PNG]

    async def test_stays_deterministic_under_concurrent_cpu_load(self, settings, engines):
        """Reproduces the thread-contention condition that once broke determinism.

        A quiet, single-test run never triggered it — only running the full suite did, because
        that is when other tests' CPU work created real scheduling contention. This test
        generates that contention itself (several CPU-bound threads doing matrix multiplies
        concurrently with the pipeline), so the regression does not depend on however loaded the
        machine happens to be when the suite runs.
        """
        import threading

        config = JobConfig(
            background=BackgroundSpec(color="#1E3A8A"),
            size=SizeSpec(width=333, height=444, margin_pct=7.5),
        )
        blob = studio_png()

        stop = threading.Event()

        def hammer() -> None:
            rng = np.random.default_rng()
            while not stop.is_set():
                a, b = rng.random((300, 300)), rng.random((300, 300))
                _ = a @ b

        threads = [threading.Thread(target=hammer, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            runs = [
                (await pipeline.process_image(blob, "a.png", config, settings, engines=engines))[1][
                    OutputFormat.PNG
                ]
                for _ in range(6)
            ]
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=2)

        assert len(set(runs)) == 1, "output differed across runs under CPU contention"


class TestResultMetadata:
    async def test_records_what_actually_happened(self, settings, engines):
        result, _ = await pipeline.process_image(
            studio_png(), "shot.png", JobConfig(), settings, engines=engines
        )
        assert result.source_size == (200, 200)
        assert result.chosen_engine is not None
        assert result.candidates, "candidates must be reported so the UI can explain the choice"
        assert result.pipeline_version
        assert result.duration_ms is not None

    async def test_single_engine_pool_is_flagged(self, settings, engines):
        """One engine is a valid configuration, but the result must say it was not a comparison."""
        result, _ = await pipeline.process_image(
            studio_png(), "shot.png", JobConfig(), settings, engines=engines
        )
        assert Note.SINGLE_ENGINE_ONLY in result.notes

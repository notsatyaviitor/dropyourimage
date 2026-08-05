"""PSD wired through the full pipeline — requirement 5, end to end, offline (local engine, no
API keys). Requires pytoshop; skips cleanly if absent, same as test_psd_fallback.py.
"""

from __future__ import annotations

import pytest

pytoshop = pytest.importorskip("pytoshop", reason="PSD fallback writer needs pytoshop")

from app import pipeline
from app.core.settings import Settings
from app.engines.local import LocalEngine
from app.imaging import export as E
from app.models import (
    BackgroundSpec,
    ExportSpec,
    ImageState,
    JobConfig,
    Note,
    OutputFormat,
    PsdSpec,
    SizeSpec,
)
from app.psd.validate import validate_psd
from tests.test_shadow import studio_scene


@pytest.fixture
def settings() -> Settings:
    """No vendor keys, no Adobe credentials — matches this build exactly."""
    return Settings(photoroom_api_key="", removebg_api_key="", fal_key="", adobe_client_id="")


@pytest.fixture
def engines():
    return [LocalEngine()]


def studio_png(size: int = 150) -> bytes:
    observed, _, _ = studio_scene(size=size)
    return E.encode(observed, None, fmt=OutputFormat.PNG)


class TestPsdThroughThePipeline:
    async def test_psd_output_is_produced_and_valid(self, settings, engines):
        config = JobConfig(
            background=BackgroundSpec(color="#1E3A8A"),
            size=SizeSpec(width=200, height=200, margin_pct=5),
            psd=PsdSpec(enabled=True),
            export=ExportSpec(formats=[OutputFormat.PNG, OutputFormat.PSD]),
        )
        result, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )

        assert result.state is ImageState.DONE, result.error
        assert OutputFormat.PSD in outputs
        assert outputs[OutputFormat.PSD][:4] == b"8BPS"

        v = validate_psd(outputs[OutputFormat.PSD], config.psd, expected_size=(200, 200))
        assert v.ok, v.problems
        assert set(v.layer_names) >= {"PROD", "BG"}

    async def test_psd_disabled_by_default_produces_no_psd_output(self, settings, engines):
        result, outputs = await pipeline.process_image(
            studio_png(), "shot.png", JobConfig(), settings, engines=engines
        )
        assert result.state is ImageState.DONE
        assert OutputFormat.PSD not in outputs

    async def test_shadow_layer_present_when_shadow_is_preserved(self, settings, engines):
        config = JobConfig(
            background=BackgroundSpec(color="#F5F5F5", shadow="preserve"),
            size=SizeSpec(width=200, height=200, margin_pct=3),
            psd=PsdSpec(enabled=True),
            export=ExportSpec(formats=[OutputFormat.PSD]),
        )
        result, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        assert result.state is ImageState.DONE
        v = validate_psd(outputs[OutputFormat.PSD], config.psd)
        assert v.ok, v.problems
        assert "SHADOW" in v.layer_names

    async def test_no_adobe_credentials_uses_the_fallback_writer_without_failing(
        self, settings, engines
    ):
        """The whole point of the fallback ladder: the image still succeeds."""
        assert not settings.adobe_configured
        config = JobConfig(
            psd=PsdSpec(enabled=True),
            export=ExportSpec(formats=[OutputFormat.PSD]),
        )
        result, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        assert result.state is ImageState.DONE
        assert OutputFormat.PSD in outputs

    async def test_custom_layer_and_path_names_flow_through(self, settings, engines):
        config = JobConfig(
            psd=PsdSpec(
                enabled=True,
                product_layer_name="ITEM",
                background_layer_name="CANVAS",
                path_name="OUTLINE",
            ),
            export=ExportSpec(formats=[OutputFormat.PSD]),
        )
        result, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        assert result.state is ImageState.DONE
        v = validate_psd(outputs[OutputFormat.PSD], config.psd)
        assert v.ok, v.problems
        assert "ITEM" in v.layer_names
        assert "CANVAS" in v.layer_names

    async def test_disabling_the_vector_path_flags_the_note(self, settings, engines):
        config = JobConfig(
            psd=PsdSpec(enabled=True, vector_clipping_path=False),
            export=ExportSpec(formats=[OutputFormat.PSD]),
        )
        result, outputs = await pipeline.process_image(
            studio_png(), "shot.png", config, settings, engines=engines
        )
        assert result.state is ImageState.DONE
        assert Note.PSD_FALLBACK_RASTER in result.notes

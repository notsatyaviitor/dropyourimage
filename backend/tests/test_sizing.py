"""Startup memory sizing check.

Exists because nothing bounded `WORKER_CONCURRENCY` against real RAM. The default of 8, at the
measured 4.4 GB for one 50.6 MP image, implies ~35 GB in flight — a deploy that looks healthy,
accepts an order, and dies to the OOM killer after paying the vendor for every image already
processed.

`available_memory_mb` is stubbed throughout so the result does not depend on the machine running
the suite.
"""

from __future__ import annotations

import pytest

from app.core import sizing
from app.core.settings import Settings


def settings_with(**over) -> Settings:
    base = dict(_env_file=None, max_image_pixels=80_000_000, worker_concurrency=8)
    base.update(over)
    return Settings(**base)


@pytest.fixture
def ram(monkeypatch):
    """Pin total system RAM in GB."""
    def set_gb(gb: float | None):
        monkeypatch.setattr(sizing, "available_memory_mb", lambda: None if gb is None else gb * 1024)
    return set_gb


class TestEstimate:
    def test_it_scales_with_megapixels_not_file_size(self):
        """The whole point: a 51 MB DNG used 4.4 GB because it is 50.6 MP, not because it is 51 MB."""
        small = sizing.estimated_peak_mb(settings_with(max_image_pixels=12_000_000))
        large = sizing.estimated_peak_mb(settings_with(max_image_pixels=80_000_000))
        assert large > small
        assert large / small == pytest.approx(80 / 12, rel=0.01)

    def test_the_default_ceiling_lands_near_the_measured_figure(self):
        """80 MP should estimate ~7 GB, from 4.4 GB measured at 50.6 MP."""
        mb = sizing.estimated_peak_mb(settings_with(max_image_pixels=80_000_000))
        assert 6_000 < mb < 8_000


class TestHeadroomCheck:
    def test_the_default_concurrency_is_refused_on_a_small_box(self, ram):
        ram(16)
        problem = sizing.check_memory_headroom(settings_with(worker_concurrency=8))
        assert problem is not None
        assert "WORKER_CONCURRENCY=8" in problem

    def test_it_names_a_concurrency_that_would_fit(self, ram):
        """A refusal that does not say what to do instead just gets worked around."""
        ram(16)
        problem = sizing.check_memory_headroom(settings_with(worker_concurrency=8))
        assert "Set WORKER_CONCURRENCY=1" in problem or "Set WORKER_CONCURRENCY=2" in problem

    def test_a_sound_configuration_passes_silently(self, ram):
        ram(64)
        assert sizing.check_memory_headroom(settings_with(worker_concurrency=4)) is None

    def test_lowering_max_image_pixels_is_a_valid_way_out(self, ram):
        """A shop that never handles 80 MP files should not need a bigger machine."""
        ram(16)
        assert sizing.check_memory_headroom(
            settings_with(worker_concurrency=4, max_image_pixels=12_000_000)
        ) is None

    def test_it_is_skipped_when_ram_cannot_be_determined(self, ram):
        """Guessing would be worse than not checking."""
        ram(None)
        assert sizing.check_memory_headroom(settings_with(worker_concurrency=64)) is None


class TestEnforcement:
    def test_it_raises_at_startup_by_default(self, ram):
        ram(8)
        with pytest.raises(RuntimeError, match="Insufficient memory"):
            sizing.enforce_memory_headroom(settings_with(worker_concurrency=8))

    def test_it_can_be_downgraded_to_a_warning(self, ram, caplog):
        """For a box whose real limit this cannot see — a cgroup, or small images only."""
        ram(8)
        sizing.enforce_memory_headroom(
            settings_with(worker_concurrency=8, enforce_memory_headroom=False)
        )
        assert "Memory headroom" in caplog.text

    def test_a_sound_configuration_does_not_raise(self, ram):
        ram(64)
        sizing.enforce_memory_headroom(settings_with(worker_concurrency=2))

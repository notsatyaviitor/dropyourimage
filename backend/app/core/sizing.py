"""Startup sizing check: does this machine have the RAM its concurrency implies?

The pipeline holds several float32 buffers per image — colour, alpha, canvas, shadow ratio — so
memory scales with **megapixels, not file size**. Measured 12 Aug 2026 on a real 50.6 MP DNG
through the full Photoroom path: peak RSS 4,435 MB for a single image. `WORKER_CONCURRENCY`
images run at once, so the default of 8 implies roughly 35 GB in flight at that resolution.

Nothing checked this. A deploy with the default on a 16 GB box looked healthy, accepted an order,
and died to the OOM killer partway through — after paying the vendor for every image already
processed and never reaching `_finalise` to write the bundle. This turns that into a refusal at
startup with the arithmetic shown, which is the same trade the GCS bucket and signing checks make:
fail where it is cheap to diagnose, not where it is expensive.

The estimate is deliberately crude. It cannot see a cgroup limit, and it assumes every image is at
`MAX_IMAGE_PIXELS`; a batch of 12 MP JPEGs will use a fraction of it. That is why it is tunable
(`MEMORY_MB_PER_MEGAPIXEL`) and can be downgraded to a warning (`ENFORCE_MEMORY_HEADROOM=false`)
rather than being something to work around by guessing.
"""

from __future__ import annotations

import logging
import os

from app.core.settings import Settings

logger = logging.getLogger(__name__)

# Left for the OS, the Python runtime, the HTTP layer and one archive being read. Not generous:
# `MAX_ZIP_BYTES` alone allows a 1 GB archive resident while its entries are planned.
_RESERVED_MB = 2048


def available_memory_mb() -> float | None:
    """Total system RAM in MB, or None where it cannot be determined.

    `os.sysconf` is Linux/macOS only. Returning None disables the check rather than guessing —
    a sizing guard that invents its own numbers is worse than no guard.
    """
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        return None


def estimated_peak_mb(settings: Settings) -> float:
    """Worst-case resident memory for one image at `max_image_pixels`."""
    megapixels = settings.max_image_pixels / 1_000_000
    return megapixels * settings.memory_mb_per_megapixel


def check_memory_headroom(settings: Settings) -> str | None:
    """Return a human-readable problem, or None when the sizing is sound.

    Pure: it reports, and the caller decides whether that is fatal. Keeps this testable without
    starting an app or monkeypatching a logger.
    """
    total = available_memory_mb()
    if total is None:
        return None

    per_image = estimated_peak_mb(settings)
    concurrency = max(1, settings.worker_concurrency)
    needed = per_image * concurrency
    usable = total - _RESERVED_MB

    if needed <= usable:
        return None

    safe = max(1, int(usable // per_image))
    return (
        f"WORKER_CONCURRENCY={concurrency} needs about {needed / 1024:.1f} GB "
        f"({per_image / 1024:.1f} GB per image at {settings.max_image_pixels // 1_000_000} MP), "
        f"but this machine has {total / 1024:.1f} GB with ~{usable / 1024:.1f} GB usable. "
        f"Set WORKER_CONCURRENCY={safe} or lower, reduce MAX_IMAGE_PIXELS, or move to a larger "
        f"machine. Measured basis: 4.4 GB peak for one 50.6 MP image. "
        f"Set ENFORCE_MEMORY_HEADROOM=false to downgrade this to a warning."
    )


def enforce_memory_headroom(settings: Settings) -> None:
    """Raise or warn at startup, per `ENFORCE_MEMORY_HEADROOM`."""
    problem = check_memory_headroom(settings)
    if problem is None:
        return
    if settings.enforce_memory_headroom:
        raise RuntimeError(f"Insufficient memory for this configuration. {problem}")
    logger.warning("Memory headroom check failed (not enforced): %s", problem)

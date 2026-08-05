#!/usr/bin/env python
"""Generate demo outputs from a synthetic studio shot. No API keys, no network.

    cd backend && source .venv/bin/activate && python scripts/demo.py

Writes to ../data/output/. Point it at a real photograph instead once images land:

    python scripts/demo.py ../data/input/shadow/some-shot.jpg
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline                                            # noqa: E402
from app.core.settings import Settings                              # noqa: E402
from app.engines.local import LocalEngine                           # noqa: E402
from app.imaging import export as E                                 # noqa: E402
from app.models import (                                            # noqa: E402
    BackgroundSpec,
    CentringSpec,
    ExportSpec,
    JobConfig,
    OutputFormat,
    ShadowMode,
    SizeSpec,
)

OUT = Path(__file__).resolve().parents[2] / "data" / "output"

# One config per demo tab combination worth showing side by side.
CASES = {
    "navy-500": JobConfig(
        background=BackgroundSpec(color="#1E3A8A"),
        size=SizeSpec(width=500, height=500, margin_pct=5),
    ),
    "white-500": JobConfig(
        background=BackgroundSpec(color="#FFFFFF"),
        size=SizeSpec(width=500, height=500, margin_pct=5),
    ),
    "lightgrey-shadow-kept": JobConfig(
        background=BackgroundSpec(color="#F5F5F5", shadow=ShadowMode.PRESERVE),
        size=SizeSpec(width=500, height=500, margin_pct=4),
    ),
    "lightgrey-shadow-removed": JobConfig(
        background=BackgroundSpec(color="#F5F5F5", shadow=ShadowMode.REMOVE),
        size=SizeSpec(width=500, height=500, margin_pct=4),
    ),
    "halo-demo-no-decontam": JobConfig(
        background=BackgroundSpec(color="#1E3A8A", decontaminate_edges=False),
        size=SizeSpec(width=500, height=500, margin_pct=5),
    ),
    "transparent": JobConfig(
        background=BackgroundSpec(transparent=True),
        size=SizeSpec(width=500, height=500, margin_pct=5),
        export=ExportSpec(formats=[OutputFormat.PNG]),
    ),
    "banner-1200x628-centroid": JobConfig(
        background=BackgroundSpec(color="#0F766E"),
        size=SizeSpec(width=1200, height=628, margin_pct=8),
        centring=CentringSpec(mode=CentringSpec.model_fields["mode"].default),
    ),
}


def source_bytes(argv: list[str]) -> tuple[bytes, str]:
    if len(argv) > 1:
        path = Path(argv[1])
        return path.read_bytes(), path.name

    # Synthetic studio shot: white sweep with falloff, red product, real cast shadow.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.test_shadow import studio_scene

    observed, _, _ = studio_scene(size=600)
    return E.encode(observed, None, fmt=OutputFormat.PNG), "synthetic-studio.png"


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    data, name = source_bytes(sys.argv)
    (OUT / f"00-source-{name}").write_bytes(data)

    settings = Settings(photoroom_api_key="", removebg_api_key="", fal_key="")
    engines = [LocalEngine()]

    print(f"source: {name} ({len(data) / 1024:.0f} KB)\n")
    failures = 0

    for label, config in CASES.items():
        result, outputs = await pipeline.process_image(
            data, name, config, settings, engines=engines
        )
        if result.state.value != "done":
            print(f"  {label:28s} FAILED  {result.error.message if result.error else ''}")
            failures += 1
            continue

        for fmt, blob in outputs.items():
            (OUT / f"{label}.{fmt.value}").write_bytes(blob)

        notes = ",".join(n.value for n in result.notes) or "-"
        offset = result.centroid_offset_px or (0.0, 0.0)
        print(
            f"  {label:28s} ok  {result.duration_ms:4d}ms  "
            f"centroid_offset=({offset[0]:+.1f},{offset[1]:+.1f})  "
            f"uniformity={result.background_uniformity}  notes={notes}"
        )

    print(f"\nwrote {len(list(OUT.glob('*')))} files to {OUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python
"""Verify a real segmentation key end to end, before trusting it in a demo.

    python scripts/check_engine.py                 # every engine that has a key
    python scripts/check_engine.py photoroom       # just one
    python scripts/check_engine.py --dry-run       # configuration only, spend nothing
    python scripts/check_engine.py --full          # also run all five stages and write an asset
    python scripts/check_engine.py --image PATH    # use a real photograph
    python scripts/check_engine.py --subject "coffee table"   # multi-object scene

Why this exists: the adapters in `app/engines/http.py` were written from vendor documentation, and
documentation drifts. Checking remove.bg this way found two real defects — a deprecated `size=full`
alias capped at 25 MP, and a missing `type=product` hint — neither of which any unit test could catch,
because both were *accepted* by the vendor and simply produced worse results.

**Each engine checked spends one API call.** `--dry-run` spends nothing. Nothing here prints a key,
and no argument accepts one: credentials come from backend/.env only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from app.core import errors  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.engines import gemini_segment as GS  # noqa: E402
from app.engines import http as H  # noqa: E402
from app.engines.base import BackgroundRemover  # noqa: E402
from app.engines.registry import build_all, select_pool  # noqa: E402
from app.imaging import export as E  # noqa: E402
from app.models import CutoutSpec, EngineId, OutputFormat  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "data" / "output"

# Engines that talk to a paid vendor. `local` is excluded: it needs no key and no verification.
# remove.bg stays listed so an existing key can still be verified after its retirement, even
# though it is no longer in ENGINE_POOL. `gemini` is here for the opposite reason: it is never
# auto-picked, so a live check is the only way its wire format gets exercised.
VENDOR_ENGINES = (EngineId.PHOTOROOM, EngineId.REMOVEBG, EngineId.FALAI, EngineId.GEMINI)


def source_bytes(path: str | None) -> tuple[bytes, str]:
    if path:
        p = Path(path)
        return p.read_bytes(), p.name
    from tests.test_shadow import studio_scene

    observed, _, _ = studio_scene(size=200)
    return E.encode(observed, None, fmt=OutputFormat.PNG), "synthetic-studio.png"


def report_config(settings: Settings, wanted: list[EngineId]) -> list[EngineId]:
    print("=== configuration ===")
    print(f"  ENGINE_POOL    : {settings.engine_pool}")
    print(f"  cache_cutouts  : {settings.cache_cutouts}")
    print(f"  vendor timeout : {settings.vendor_timeout_seconds}s, retries {settings.vendor_max_retries}")

    have = [e for e in wanted if settings.key_for(e)]
    missing = [e for e in wanted if not settings.key_for(e)]
    print(f"  keys present   : {[e.value for e in have] or 'NONE'}")
    if missing:
        print(f"  no key         : {[e.value for e in missing]}")

    pool = [e.id.value for e in select_pool(settings, CutoutSpec())]
    print(f"  AUTO pool      : {pool}")

    # Keyed on `have`, not on the pool: asking for one specific engine that has no key must still
    # explain how to add *that* key, even when a different engine's key means the pool is non-empty.
    if not have:
        print("\n  No key for the requested engine(s). Add one without touching shell history:")
        for e in missing:
            print(f"      python scripts/set_env_key.py {_env_var(e)}")
        return []

    if len(pool) == 1:
        print("  note: one engine only, so auto-pick emits SINGLE_ENGINE_ONLY. Expected, not an error.")
        print("        add a second key to exercise the dual-engine comparison.")
    else:
        print(f"  -> dual-engine auto-pick is live across {pool}")
    return have


def _env_var(engine: EngineId) -> str:
    return {
        EngineId.PHOTOROOM: "PHOTOROOM_API_KEY",
        EngineId.REMOVEBG: "REMOVEBG_API_KEY",
        EngineId.FALAI: "FAL_KEY",
    }[engine]


def wire_summary(engine: EngineId, data: bytes) -> list[tuple[str, str]]:
    """What this build will actually send — printed so a drifted field is visible before spending."""
    if engine is EngineId.REMOVEBG:
        return [
            ("POST", H.REMOVEBG_URL),
            ("header", f"{H.REMOVEBG_KEY_HEADER}: <redacted>"),
            ("size", f"{H.size_for(data)}  (chosen by pixel count)"),
            ("type", H.REMOVEBG_TYPE),
            ("semitransparency", H.REMOVEBG_SEMITRANSPARENCY),
        ]
    if engine is EngineId.PHOTOROOM:
        return [
            ("POST", H.PHOTOROOM_URL),
            ("header", f"{H.PHOTOROOM_KEY_HEADER}: <redacted>"),
            ("file field", H.PHOTOROOM_FILE_FIELD),
        ]
    if engine is EngineId.GEMINI:
        from app.core.settings import get_settings

        model = get_settings().gemini_segment_model
        return [
            ("POST", GS._ENDPOINT.format(model=model)),
            ("auth", "?key=<redacted> (query param)"),
            ("model", f"{model}  (GEMINI_SEGMENT_MODEL, NOT GEMINI_MODEL)"),
            ("thinkingLevel", "MINIMAL  (load-bearing: ~2s with, minutes without)"),
            ("expects", "polygon mask; a COCO RLE string raises rather than guessing"),
            ("note", "mask is HARD-EDGED — 0 soft alpha pixels by construction"),
        ]
    return [("POST", H.FALAI_URL), ("header", "Authorization: Key <redacted>"), ("body", "image_url as a data URI")]


async def check_one(engine_obj: BackgroundRemover, data: bytes, name: str) -> np.ndarray | None:
    engine = engine_obj.id
    width, height = E.dimensions_of(data)

    print(f"\n=== {engine.value} — live call ({name}, {width}x{height}, {len(data) / 1024:.0f} KB) ===")
    for k, v in wire_summary(engine, data):
        print(f"  {k:18s} {v}")
    print("  spending 1 call...")

    try:
        result = await engine_obj.alpha_for(data, width, height)
    except errors.VendorOutOfCredits as exc:
        print(f"  FAIL  {exc.message}")
        return None
    except errors.VendorUnauthorized as exc:
        print(f"  FAIL  {exc.message}")
        return None
    except errors.NoForegroundFound as exc:
        print(f"  FAIL  {exc.message}")
        return None
    except errors.PipelineError as exc:
        print(f"  FAIL  {type(exc).__name__}: {exc.message}")
        print("  A 400 usually means a wire field was rejected — compare the values above against")
        print("  the vendor's current docs and correct the constants in app/engines/http.py.")
        return None

    a = result.alpha
    soft = int(((a > 0.01) & (a < 0.99)).sum())
    exact = a.shape == (height, width)
    print(f"  OK    {result.latency_ms} ms, ledger cost ${result.cost_usd}")
    print(f"        mask shape {a.shape} vs source ({height}, {width}): {'PASS' if exact else 'FAIL'}")
    print(f"        alpha range [{a.min():.3f}, {a.max():.3f}], {len(np.unique(a))} distinct values")
    # Gemini returns a polygon, so a hard edge is its documented behaviour rather than a defect —
    # reporting it as FAIL next to "verified" would read as a broken adapter. Still printed, because
    # it is the one number that decides whether this engine suits the image.
    if soft:
        verdict = "PASS, edges are not binary"
    elif engine is EngineId.GEMINI:
        verdict = "EXPECTED for gemini — polygon mask, no soft alpha (see docs/LIMITATIONS.md)"
    else:
        verdict = "FAIL, mask is hard-edged"
    print(f"        soft pixels {soft} — {verdict}")
    print(f"        foreground coverage {float((a > 0.5).mean()) * 100:.1f}%")

    _warn_on_watermark(a)

    if a.max() < 0.01:
        print("        WARNING: mask is empty — nothing detected as foreground.")
    elif a.min() > 0.99:
        print("        WARNING: mask is fully opaque — the vendor found nothing to remove, and")
        print("                 auto-pick will reject this candidate.")
    return a


#: Alpha above this in a region with no subject is the vendor drawing on the mask.
_STRAY_ALPHA = 0.01
#: Fraction of a subject-free band that must be non-zero before it looks like a pattern rather
#: than ordinary mask noise.
_STRAY_FRACTION = 0.02


def _warn_on_watermark(a: np.ndarray) -> None:
    """Catch a free-tier vendor stamping its logo into the ALPHA channel.

    Found live on Photoroom: its trial plan tiles "Photoroom" across the returned cut-out at up to
    31% opacity, and because the watermark is in the alpha rather than only the colour, taking just
    the alpha does not escape it. Every delivered composite carried ghost lettering.

    Nothing already in this script could see it. Alpha range, distinct-value count, soft-pixel
    count and coverage are all *satisfied* by a watermark — it looks exactly like legitimate soft
    edge detail. The question none of them asked is the one that matters: **is the mask actually
    zero where there is no subject?**

    Uses the top and bottom eighths, which are background in any sane packshot or room shot. A
    genuine subject touching both would make this warn spuriously, so it warns rather than fails.
    """
    band = max(1, a.shape[0] // 8)
    edges = np.concatenate([a[:band].ravel(), a[-band:].ravel()])
    stray = float((edges > _STRAY_ALPHA).mean())
    if stray <= _STRAY_FRACTION:
        return

    print(f"        WARNING: {stray * 100:.1f}% of the top/bottom bands are non-zero, peaking at")
    print(f"                 alpha {edges.max():.2f}, in a region that should hold no subject.")
    print("                 That is the signature of a WATERMARK baked into the alpha channel —")
    print("                 check whether this key is on a free or trial plan. Every delivered")
    print("                 asset inherits it; taking only the alpha does not avoid it.")


async def full_pipeline(settings: Settings, engine_obj: BackgroundRemover, data: bytes, name: str, subject: str | None) -> None:
    from app import pipeline
    from app.engines.cache import StorageCutoutCache
    from app.imaging import color as C
    from app.models import BackgroundSpec, JobConfig, SizeSpec
    from app.storage.memory import MemoryStorage

    print(f"\n=== {engine_obj.id.value} — full pipeline ===")
    cache = StorageCutoutCache(MemoryStorage())
    config = JobConfig(
        cutout=CutoutSpec(subject_prompt=subject),
        background=BackgroundSpec(color="#F5F5F5"),
        size=SizeSpec(width=500, height=500, margin_pct=5, allow_upscale=True),
    )

    result, outputs = await pipeline.process_image(
        data, name, config, settings, engines=[engine_obj], cache=cache
    )
    if result.state.value != "done":
        print(f"  FAIL  {result.error.code.value}: {result.error.message}" if result.error else "  FAIL")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{engine_obj.id.value}-live-500.png"
    path.write_bytes(outputs[OutputFormat.PNG])

    from PIL import Image

    arr = np.asarray(Image.open(path).convert("RGB"))
    corner = tuple(int(v) for v in arr[3, 3])
    want = C.hex_to_srgb8("#F5F5F5")
    print(f"  {result.duration_ms} ms, cost ${result.cost_usd}")
    print(f"  size {arr.shape[1]}x{arr.shape[0]} {'PASS' if arr.shape[:2] == (500, 500) else 'FAIL'}")
    print(f"  background {corner} vs #F5F5F5 {want} {'PASS' if corner == want else 'FAIL'}")
    print(f"  centroid offset {result.centroid_offset_px} px")
    print(f"  notes {[n.value for n in result.notes] or '-'}")
    print(f"  wrote {path}")

    r2, _ = await pipeline.process_image(data, name, config, settings, engines=[engine_obj], cache=cache)
    ok = r2.cache_hit and r2.cost_usd == 0
    print(f"  re-run cache_hit={r2.cache_hit} cost=${r2.cost_usd} {'PASS — tuning is free' if ok else 'FAIL'}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("engines", nargs="*", choices=[e.value for e in VENDOR_ENGINES],
                    help="which engines to check (default: all that have a key)")
    ap.add_argument("--dry-run", action="store_true", help="check config only, spend nothing")
    ap.add_argument("--full", action="store_true", help="also run all five stages")
    ap.add_argument("--image", help="path to a real photograph")
    ap.add_argument("--subject", help="subject prompt, for a multi-object scene")
    args = ap.parse_args()

    wanted = [EngineId(e) for e in args.engines] if args.engines else list(VENDOR_ENGINES)

    settings = Settings()
    have = report_config(settings, wanted)
    if not have:
        return 1
    if args.dry_run:
        print("\ndry run: configuration looks usable, nothing spent.")
        return 0

    data, name = source_bytes(args.image)
    if not args.image:
        print("\nnote: using the synthetic studio fixture. It is a clean two-colour composite, so it")
        print("      cannot compare mask quality between engines — pass --image for that.")

    registry = build_all(settings)
    failures = 0
    for engine_id in have:
        alpha = await check_one(registry[engine_id], data, name)
        if alpha is None:
            failures += 1
            continue
        if args.full:
            await full_pipeline(settings, registry[engine_id], data, name, args.subject)

    print(f"\n{len(have) - failures} of {len(have)} engine(s) verified.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

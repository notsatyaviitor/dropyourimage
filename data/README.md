# Test data

Real product photographs for eyeball QA and the Day 5 review sweep.

**Nothing in `input/`, `expected/` or `output/` is committed.** These are client images; they stay local. Only
this README and the `.gitkeep` files are tracked. See the `data/` rules in `.gitignore`.

## Two kinds of test data, kept apart deliberately

| | `data/` | `backend/tests/fixtures/` |
|---|---|---|
| Contents | Real photographs | Synthetic images generated in code |
| Committed | No | Yes — they are tiny |
| Used by | Manual QA, the Day 5 sweep, tuning | Automated assertions in `pytest` |
| Judged by | Human eye | Exact pixel values |

Automated tests must never depend on `data/`. A test that needs a real photograph is not a test — it is a
review. Keeping the split means `pytest` passes on a clean checkout with no images and no API keys.

## Layout

```
data/
├── input/
│   ├── opaque/        plain product shots, no transparency, no shadow — the happy path
│   ├── shadow/        uniform background WITH a visible drop shadow  ← the shadow-preservation feature
│   ├── reflection/    product on a glossy surface with a visible reflection
│   └── edge-cases/    images that SHOULD fail gracefully (see below)
├── expected/          hand-approved reference outputs, for spotting regressions by eye
└── output/            pipeline results; disposable, wiped freely
```

## What to put in each folder

**`opaque/`** — 10–15 straightforward packshots on white or light grey. If these are not flawless, nothing else
matters.

**`shadow/`** — the important set. Uniform studio background with a real drop shadow. This is what exercises
the ratio-map reconstruction in [../docs/IMAGE_PIPELINE.md](../docs/IMAGE_PIPELINE.md). Aim for 8–10, mixing
soft and hard shadows.

**`reflection/`** — product on gloss or acrylic. The same ratio model handles these, but blown specular
highlights clip; this folder is how we find out where.

**`edge-cases/`** — images that are *supposed* to hit a guard rail, so that behaviour is verified rather than
discovered during the demo:

- A lifestyle or in-situ shot → must trip the background-uniformity gate and fall back to "shadow removed"
  with a visible note, not produce garbage.
- A photo smaller than the requested canvas → must pad or route to the upscaler, never Lanczos-stretch.
- CMYK, 16-bit, and EXIF-rotated files → must normalise on ingest.
- Out-of-scope categories (glass, sheer fabric, fine hair) → **keep these here and out of the demo set.** They
  are documented as unsupported in [../docs/LIMITATIONS.md](../docs/LIMITATIONS.md); the folder exists so we
  know how bad they look rather than guessing.

## Conventions

- Keep original filenames from the client where possible — traceability during review beats tidy naming.
- Prefix a file with `_` to have QA scripts skip it (`_wip_shot3.jpg`).
- Zip a folder to feed it to the API: `cd data/input/shadow && zip -r ../../output/shadow.zip .`

## Day 0 checklist

- [ ] ~20 real product images obtained
- [ ] **At least 8 with a visible drop shadow on a uniform background** — without these, the shadow feature
      cannot be demoed or tuned at all
- [ ] 2–3 lifestyle shots for `edge-cases/`, to prove the uniformity gate fires
- [ ] Confirm the client is content for these images to sit on dev machines

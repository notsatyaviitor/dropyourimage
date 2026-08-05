# Testing

296 backend tests, all offline — no API keys, no Redis, no MinIO. Frontend verified end-to-end
with a live browser driver (see below); not part of the automated suite.

```bash
cd backend && source .venv/bin/activate && pytest -q
```

## What's asserted vs what's eyeballed

| Layer | How it's checked |
|---|---|
| Colour, geometry, compositing, shadow | Exact numeric assertions against synthetic fixtures — golden-pixel values, measured error rates, exact-hex byte matches |
| Zip ingest security | Constructed exploits (real zip-slip paths, real zip bombs, a real forged central-directory record) against real guards |
| API layer | Full HTTP round trip via `httpx.ASGITransport`, memory mode, real multipart uploads |
| PSD | Structural validation (`psd-tools` read-back) **and** rendered-composite pixel checks — the latter caught two real bugs structural checks alone missed |
| Determinism | Byte-identical output across repeated runs, including under deliberately generated concurrent CPU load |
| Frontend | Driven end to end with Playwright against a live backend: all 5 tabs, upload → process → results, zero console errors, zero failed requests — see `frontend/README.md` |

**No ground-truth quality benchmark exists.** There is no labelled mask set and no
SAD/MSE/ΔE harness against real product photography. The demo can show cut-out quality
convincingly; it cannot support a numeric quality claim or answer "which vendor API should we
buy" — see `docs/LIMITATIONS.md`. Building that harness is a real 3–4 day follow-on, not
attempted here.

## Regression tests worth reading before touching their area

These exist because a real, non-obvious bug was found and fixed, and the test is what would
catch it coming back:

- `test_pipeline.py::TestDeterminism::test_stays_deterministic_across_a_real_time_gap` — the ICC
  sRGB timestamp bug. A two-rapid-calls test would not have caught this; this one forces the
  actual >1s gap that exposed it.
- `test_pipeline.py::TestDeterminism::test_stays_deterministic_under_concurrent_cpu_load` —
  generates real thread contention itself rather than hoping the machine happens to be loaded.
- `test_geometry.py::TestResampleRGBA` — the premultiplied-vs-naive resize comparison, with the
  naive path kept in the test as a control to prove the artefact is real, not hypothetical.
- `test_psd_fallback.py::TestComposite` — renders the actual PSD layer stack and checks pixel
  values; caught a layer-ordering bug and a double-shadow-application bug that every structural
  check (layer names, count, ICC profile) missed completely.
- `test_export.py::TestIccProfileWriter` — validates the synthesised Adobe RGB profile against an
  independent library (littleCMS via `ImageCms`), not just against its own encoder.

## Stability

The full suite has been run repeatedly (20+ consecutive runs during the determinism
investigation) with zero flakes after the fixes above landed. If a test becomes flaky again,
suspect a library embedding wall-clock time or an uncontrolled thread count before suspecting the
test itself — both of the bugs above looked exactly like "the algorithm is wrong" before turning
out to be neither.

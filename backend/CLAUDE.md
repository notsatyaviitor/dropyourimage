# Backend — working rules

Python 3.10 · FastAPI · RQ/Redis · numpy/OpenCV/Pillow. Read the root `CLAUDE.md` first; the prime directive
there governs everything here.

## Environment

```bash
cd backend && source .venv/bin/activate     # ALWAYS. Never use system python3
pytest -q                                   # must pass with no API keys, no images
```

If a dependency is added, put it in `requirements.txt` with a pinned version and a one-line comment saying why.

## Module boundaries

```
app/core/        settings (pydantic-settings), errors, jobstore — no domain logic
app/models.py    the frozen contract: JobConfig, JobStatus, ImageResult
app/imaging/     PURE FUNCTIONS ONLY. No I/O, no network, no globals, no config reads
app/engines/     segmentation adapters behind one Protocol + a registry, plus the cut-out cache
app/storage/     object storage behind a Protocol (MinIO/S3 now, anything later)
app/ingest/      safe zip extraction
app/psd/         Photoshop API client, fallback writer, psd-tools validator
app/pipeline.py  orchestrates the five stages in order — the ONLY place that knows the sequence
app/jobs.py      per-job orchestration: unpack, fan out over images, bundle the results
app/api/         FastAPI routes; thin, no imaging logic
```

There is no `app/core/logging.py`; the app uses stdlib logging directly and `LOG_LEVEL` is not yet
wired to anything.

**`app/imaging/` is pure.** Every function takes arrays plus a config object and returns arrays. No file
reads, no HTTP, no clock, no randomness. This is what makes the core testable offline with exact assertions,
and it is the most important boundary in the codebase — do not put a vendor call or a file read in there.

Use `typing.Protocol` for `BackgroundRemover`, `StorageBackend` and `CutoutCache`. Adding an engine should be one
new file in `app/engines/` plus one registry line — nothing else changes. (PSD has no `PsdBuilder` Protocol: it
has a single entry point, `app/psd/service.py::produce_psd`, which picks the Adobe path or the fallback.)

## Imaging invariants — each of these is a bug if broken

1. **Composite in linear light.** Decode sRGB → linear → composite → re-encode. Compositing in gamma space
   produces dark fringes on cut-out edges.
2. **Never threshold alpha to binary.** Keep soft alpha end to end, float32 or 16-bit. Thresholding destroys
   edge quality permanently and cannot be recovered later.
3. **Geometry runs BEFORE the background fill.** Resizing after filling resamples the product against the flat
   colour and bakes halos in. The order in `pipeline.py` is deliberate; do not "tidy" it.
4. **The hex is an sRGB triple.** When the output profile is Adobe RGB (1998), it must be *converted*, not
   copied — otherwise the delivered background is not the colour that was requested.
5. **Never Lanczos-upscale past 100%.** Pad, or route to the AI upscaler. A stretched master looks worse than a
   padded one.
6. **Decontaminate the edge band** before compositing onto a saturated colour, or white studio backgrounds
   leave a white halo.
7. **Output dimensions are exact.** If the config says 500×500, assert 500×500 before returning.

## Determinism

Stages 2–5 must be byte-reproducible: same input twice → identical output bytes. There is a test for this. It
exists to catch a model or a random seed being introduced into the deterministic path. Do not skip it, and do
not "fix" it by loosening the comparison.

Two real, non-obvious sources of non-determinism have already been found and fixed here — both are guarded by
tests that force the actual failure condition (a real time gap / real concurrent load), not just two rapid
calls, because rapid calls rarely trigger either:

1. **`ImageCms.createProfile("sRGB")` embeds a creation timestamp.** littleCMS sets it to current system time,
   so two calls a second apart return different bytes. `app/imaging/icc.py:srgb_profile()` is `@lru_cache`d
   for exactly this reason — never remove the cache or call `ImageCms.createProfile` fresh elsewhere.
2. **BLAS (`numpy.linalg.lstsq` in `shadow.py`) is not reliably pinned to one thread by env vars alone** —
   some builds don't take `OPENBLAS_NUM_THREADS` from the environment at the point our process sets it.
   `threadpoolctl.threadpool_limits(1)` is applied both process-wide (`app/__init__.py`) and, critically, right
   at the `lstsq` call site in `shadow.py` — the call-site wrap is the one that actually matters, since it runs
   after numpy is certainly already loaded.

If a new non-determinism shows up, suspect a library that embeds wall-clock time or thread-count into its
output before suspecting the pipeline's own maths — both bugs here looked like "the algorithm is wrong" before
turning out to be neither.

## Security musts

- **Zip ingest is the attack surface.** Reject path-traversal entries (zip-slip), cap entry count and total
  uncompressed size (zip bombs), sniff MIME per entry rather than trusting extensions.
- Decompression-bomb guard on individual images; cap max dimension and file size.
- API keys from env only. Never returned in a response, never logged, never reachable from the browser.
- Signed short-lived download URLs; no public bucket listing.
- Per-vendor timeouts, bounded retries with backoff, circuit breaker. A 50-image zip will hit rate limits.

## Cost discipline

Cut-outs are cached on `sha256(image_bytes) + engine_id` — `app/engines/cache.py`, keyed by
`pipeline.cache_key()`. Segmentation is deterministic per engine, so re-running while tuning colour or geometry
costs nothing. Two rules for that module: alpha is stored as **lossless float32**, never an 8-bit PNG (8 bits
erodes soft edges a little more on every round trip, which is invariant 2), and a broken cache must degrade to a
miss rather than fail the image — a cache is an optimisation, so it may cost money but never correctness.

Per-job spend accumulates into `JobStatus.cost_usd`. There is no separate cost-ledger store yet.

## Style

Match the surrounding code. Type hints on public functions. Docstrings only where the *why* is non-obvious —
the imaging maths needs them, CRUD does not. Errors raise typed exceptions from `app/core/errors.py` so the API
layer can map them to a taxonomy the UI can display.

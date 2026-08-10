# Architecture

## The one decision that explains everything else

Only **stage 1 (segmentation)** is an AI API call. Stages 2–5 — shadow reconstruction, geometry,
background fill, export — are deterministic code. See the root `CLAUDE.md` for the full
reasoning; every module in this codebase is organised around that split.

## Services

```
backend/     Python · FastAPI · RQ (or inline in memory mode) · own .venv
frontend/    TypeScript · React · Vite · talks to the backend only over /api
```

Fully independent — no shared build, no cross-imports. The only coupling is
`docs/API_CONTRACT.md` (mirrored in `backend/app/models.py` and `frontend/src/api/types.ts`).

## The five pipeline stages, in order

```
1. Cut-out          two segmentation APIs, auto-pick             ← the only AI
2. Shadow extract    background plate + ratio map                deterministic
3. Geometry          scale, centre, exact canvas                 deterministic
4. Background        hex fill, shadow, composite                 deterministic
5. Export            PNG / JPEG / TIFF / WebP, and PSD           deterministic
```

Implemented in `backend/app/pipeline.py`. Stage 3 runs **before** stage 4 deliberately — resizing
after filling would resample the product against the flat colour and bake edge halos in
permanently.

Stage 5's PSD path is a peer output alongside the raster formats, not a re-encoding of them — it
is built from the same pre-composite layers (`app/imaging/geometry.py`'s `Placement`) that stage
4 flattens, so the raster and PSD outputs cannot drift apart. See `docs/PSD.md`.

## Backend module map

```
app/core/        settings (env-driven), typed error taxonomy, job store abstraction
app/models.py    the frozen contract
app/imaging/     PURE FUNCTIONS. color, geometry, composite, shadow, export, icc — no I/O, no network
app/engines/     segmentation adapters (base/local/http), auto-pick scoring, vision tie-break,
                 registry, per-vendor adaptive throttle
app/ingest/      safe zip extraction — the untrusted-input boundary. Two ways in (whole-archive
                 and streaming), one set of guards
app/psd/         fallback writer, vector path encoder, validator, Adobe API client (unverified)
app/storage/     object storage behind a Protocol — memory (tests/dev) or S3/MinIO
app/pipeline.py  the five stages, composed
app/jobs.py      unpack a zip, run every image through the pipeline, assemble the download bundle
app/queue.py     RQ wiring for real deployment
app/api/         FastAPI routes
```

`app/imaging/` is the most important boundary in the codebase: every function there takes arrays
and a config object and returns arrays, nothing else. That purity is what makes 296 tests run in
about 5 seconds with no API keys, no Redis, and no MinIO.

## Two execution modes

| | Memory mode | Real deployment |
|---|---|---|
| Trigger | `REDIS_URL`/`S3_ENDPOINT_URL` unset or `memory` (the default, and always true under `pytest`) | Real Redis + MinIO/S3 configured |
| `POST /jobs` | Runs the job inline, synchronously, in the request handler | Enqueues via RQ; a separate `rq worker` process runs it |
| Storage | In-process dict | S3-compatible object storage, signed URLs |
| Bulk | Refused above `INLINE_JOB_MAX_IMAGES` (25) — a blocking POST cannot span a job that takes minutes | Required for 200–400 image orders |

`JobCreated.state` is always `"queued"` in the response regardless of mode — it describes request
acceptance, not a live snapshot. Poll `GET /jobs/{id}` for the real state in both modes.

## Bulk: what changes at 400 images

The original shape — one archive, everything in memory, a status write per image — was right for
the twenty-image case and fails in four independent ways at batch scale. Each is fixed at its own
layer; they only make sense together.

```
browser     one zip per ~50 files, sent sequentially, buffers released between batches
            (peak was ~3x the whole selection: files + zip + File)
routes      POST /jobs takes an optional job_id and start flag; job-level caps on top of
            the per-archive ones
zip_reader  plan_entries (<=4 KB per entry) + read_entry, so entry bytes are never all
            resident — measured 1 MB vs 317 MB on 60 photos
jobs.py     one archive open at a time; coalesced status writes; a stop signal for cancel
            and the spend ceiling; bundle assembled through a temp file
engines     per-vendor adaptive throttle, so one 429 slows the whole job on that vendor
```

Two of these are worth knowing about before touching them:

**`OutputAsset.url` is minted on read, not on write.** The signed-URL TTL is 15 minutes; a
400-image job is not. Assets carry a storage `key` and every read path re-signs from it. Do not
reintroduce a URL stored at write time.

**The throttle is a contextvar, and per vendor.** It has to be visible at the vendor call site five
frames below `jobs.py`, behind the engine Protocol — threading it through would put a
rate-limiting parameter into `BackgroundRemover.alpha_for`. It is keyed per vendor because
Photoroom exhausting its quota says nothing about fal.ai's: one shared governor would halve
throughput on a healthy vendor exactly when the job needs it to take up the slack. See
`app/engines/throttle.py`.

## Frontend

Five tabs configure one `JobConfig`; one upload runs the whole pipeline once. Brand tokens
(`frontend/src/theme/tokens.ts`) are pulled from the live dropyourimage.com CSS via `curl`, not
eyeballed — see that file's header for the exact source and a correction worth knowing about (the
accent is green, `#2cbc63`, not blue as an early visual pass assumed).

## Determinism

Stages 2–5 are byte-reproducible: identical input bytes in, identical output bytes out, always.
This is tested directly (`tests/test_pipeline.py::TestDeterminism`), including under deliberate
concurrent CPU load, because two real non-determinism bugs were found and fixed while building
this — an ICC profile embedding a wall-clock timestamp, and BLAS thread-count pinning that needed
to be applied at the actual call site, not just once at process start. See `backend/CLAUDE.md`'s
Determinism section for the full story before touching anything in this area.

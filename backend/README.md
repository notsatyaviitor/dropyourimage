# Backend — DropYourImage POC

Python 3.10+ · FastAPI · numpy/OpenCV/Pillow · RQ/Redis · MinIO/S3.

Five pipeline stages, of which **only stage 1 (segmentation) is an AI API call**. Stages 2–5 —
shadow reconstruction, geometry, background fill, export — are deterministic imaging code. See the
root [`CLAUDE.md`](../CLAUDE.md) for why that split is non-negotiable, and
[`CLAUDE.md`](CLAUDE.md) here for the module boundaries and the seven imaging invariants.

## Setup

```bash
cd backend && ./scripts/setup.sh      # creates .venv, installs requirements.txt, verifies imports
source .venv/bin/activate             # ALWAYS. Never use the system python3 — it is apt-managed
pytest                                # 652 passed, 0 skipped — no API keys, no images needed
```

`scripts/setup.sh` is idempotent; re-run it after a `requirements.txt` change.

Two things it will warn about rather than fix:

- **`libmagic1`** — `python-magic` is only a binding. Without the system library, zip-entry MIME
  sniffing fails, and that is a security control (see [`../docs/SECURITY.md`](../docs/SECURITY.md)).
  Install with `sudo apt-get install -y libmagic1`.
- **`pytoshop`** is now **in `requirements.txt`** (pinned `1.2.1`). It was previously left out
  because it builds a Cython extension and is unmaintained — but Adobe is enterprise-quoted, so the
  pytoshop writer is the *only* path that ships a PSD, and leaving it out meant layered PSD simply
  did not work. It builds from sdist and pulls in `cython`; if that build fails on your box, PSD
  jobs raise a typed `psd_unavailable` error and every other output format still works.

## What works with no credentials

Everything except real segmentation quality and layered PSD via Adobe. With no keys set, stage 1
falls back to the `local` control engine — classical colour keying, zero cost, no network. It is
**not** production quality and must never be the default once a key exists, but it means the full
zip-in / asset-out path works before procurement completes.

```bash
python scripts/demo.py                # writes 7 configs to ../data/output/, no keys, no images
```

Add a vendor key without it landing in your shell history:

```bash
python scripts/set_env_key.py PHOTOROOM_API_KEY
```

Then verify it before trusting it in a demo — the adapters were written from vendor documentation,
and documentation drifts:

```bash
python scripts/check_engine.py --dry-run     # config only, spends nothing
python scripts/check_engine.py photoroom     # one live call
python scripts/check_engine.py --full        # also runs all five stages
```

This is how two real remove.bg defects were found: a deprecated `size=full` alias capped at 25 MP,
and a missing `type=product` hint. Both were *accepted* by the vendor and merely produced worse
results, so no unit test could have caught them.

With one key, auto-pick emits `SINGLE_ENGINE_ONLY` — that is correct, not a bug. Dual-engine
auto-pick needs two; `ENGINE_POOL` order decides which pair runs.

## Running the API

**There are two execution modes, and picking the wrong one looks like a broken app.**

`REDIS_URL` decides. If it points at a real Redis, `POST /jobs` enqueues to RQ and returns
immediately — so **without a worker running, the job never progresses and clients poll forever.**

```bash
# Memory mode — no Redis, no MinIO, job runs inline inside the request. Best for a quick demo.
REDIS_URL=memory S3_ENDPOINT_URL=memory uvicorn app.main:app --reload

# Real-queue mode — needs BOTH halves. The queue name must match QUEUE_NAME (default: dyi).
docker compose up -d          # from the repo root: Redis + MinIO
rq worker dyi                 # in a second terminal
uvicorn app.main:app --reload
```

`GET /health` returns a redacted config snapshot — which engines have keys, whether Adobe is
configured — and never a secret value.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | multipart: `file` (a zip) + `config` (JobConfig JSON) → `JobCreated`, HTTP 202 |
| `GET` | `/jobs/{job_id}` | poll for `JobStatus` |
| `GET` | `/objects/{key}` | memory-mode asset passthrough (S3 mode uses presigned URLs) |
| `GET` | `/health` | redacted config |

The contract is frozen: [`../docs/API_CONTRACT.md`](../docs/API_CONTRACT.md) is the readable map,
`app/models.py` is the source of truth, and `frontend/src/api/types.ts` mirrors it by hand. Change
all three in one commit or the UI drifts silently.

## Cost control

Cut-outs are cached on `sha256(image_bytes) + engine_id` (`app/engines/cache.py`), so re-running a
zip to try a different background colour, canvas size or centring mode costs **nothing** — stages
2–5 cannot change the mask. Identical images inside one zip dedupe too. Disable with
`CACHE_CUTOUTS=false`.

Per-job spend accumulates into `JobStatus.cost_usd`.

## Limits

`MAX_IMAGE_PIXELS` guards the input decode against decompression bombs; `MAX_OUTPUT_PIXELS` is its
output-side counterpart, because `SizeSpec` allows each axis up to 20000 — 400 MP, roughly 8 GB of
float32 buffers, from a request body of a few dozen bytes. Both are deployment limits, tunable per
machine. See [`../docs/SECURITY.md`](../docs/SECURITY.md) for the full guard table.

## Layout

```
app/core/        settings, typed errors, job store
app/models.py    the frozen contract
app/imaging/     PURE functions: colour, geometry, composite, shadow, export, icc
app/engines/     segmentation adapters + registry + autopick + cut-out cache
app/storage/     object storage behind a Protocol
app/ingest/      safe zip extraction
app/psd/         Adobe client, pure-Python fallback, psd-tools validator
app/pipeline.py  the five stages in order — the only module that knows the sequence
app/jobs.py      per-job orchestration
app/api/         thin FastAPI routes
```

`app/imaging/` is pure — arrays in, arrays out, no I/O, no clock, no randomness. It is the most
important boundary in the codebase and what makes the core testable offline with exact assertions.

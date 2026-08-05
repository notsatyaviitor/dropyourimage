# Setup

## Requirements

| Tool | Version here | Notes |
|---|---|---|
| Python | 3.10.12 | 3.10+ required |
| Node | 20.20.0 | frontend only |
| Docker | 29.6.1 | Redis + MinIO for local dev |

## Backend — Python venv

All Python libraries live in a project-local virtual environment. Never install into the system interpreter;
this machine's `python3` is Ubuntu's and is managed by apt.

```bash
scripts/setup.sh
source .venv/bin/activate
```

`scripts/setup.sh` is idempotent — safe to re-run after a dependency change.

Manually, if you prefer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r backend/requirements.txt
```

Confirm the environment:

```bash
python -c "import numpy, cv2, PIL, fastapi; print('ok')"
pytest backend/tests -q
```

### Dependencies and why each is here

| Package | Purpose |
|---|---|
| `numpy` | All pixel maths. Every imaging stage operates on float32 arrays |
| `opencv-python-headless` | Contours for centring, morphology for the edge band, resampling. Headless — no GUI libs needed on a server |
| `Pillow` | Image decode/encode, EXIF, ICC profile read/write |
| `colour-science` | CIEDE2000 and colourspace conversions done properly rather than by hand |
| `psd-tools` | **Reading** PSDs, for the output validator. Not a writer |
| `pytoshop` | PSD *writing* — the fallback path only, if Adobe access does not land |
| `fastapi`, `uvicorn` | API |
| `rq`, `redis` | Job queue |
| `httpx` | Async HTTP to the segmentation vendors |
| `pydantic`, `pydantic-settings` | The contract in `app/models.py`, and env-var config |
| `python-magic` | MIME sniffing on zip entries — we do not trust file extensions |
| `pytest`, `pytest-asyncio` | Tests |

## Local services

```bash
docker compose up -d       # Redis + MinIO
```

Redis backs the RQ queue. MinIO stands in for S3-compatible object storage so the storage layer is written
against the real API from Day 1.

## Environment variables

Copy `.env.example` to `.env` and fill it in. **`.env` is gitignored and must never be committed.**

```bash
cp .env.example .env
```

Keys are read server-side only, via `pydantic-settings`. They are never sent to the browser and never appear
in an API response — see [SECURITY.md](SECURITY.md).

### What works without credentials

The imaging core — stages 2 through 5 — needs **no API keys at all**. With none configured, the pipeline runs
with the `local` engine (a bundled offline segmenter used as a test control), so the full path from zip upload
to downloaded asset is exercisable before any procurement completes.

| Capability | Needs a key? |
|---|---|
| Hex background, resize, centring, shadow recomposition, PNG/JPEG/TIFF/WebP export | No |
| Commercial-quality background removal | Yes — `PHOTOROOM_API_KEY` and/or `REMOVEBG_API_KEY` / `FAL_KEY` |
| Auto-pick vision tie-break | Yes — `GEMINI_API_KEY` (falls back to the deterministic score without it) |
| Layered PSD with vector clipping path | Yes — Adobe Firefly Services credentials |

## Running it

```bash
source .venv/bin/activate
docker compose up -d
uvicorn app.main:app --reload --app-dir backend    # API on :8000
rq worker --url redis://localhost:6379 dyi         # worker, separate terminal
cd frontend && npm install && npm run dev          # UI on :5173
```

## Troubleshooting

**`ImportError: libGL.so.1`** — you have `opencv-python` rather than `opencv-python-headless`. Reinstall from
`requirements.txt`.

**`pip install` fails on network** — this environment may be sandboxed. Re-run `scripts/setup.sh` outside the
sandbox, or pre-seed a wheel cache.

**Worker picks up nothing** — the queue name is `dyi` on both sides. `rq worker` without it listens to
`default` and will sit idle.

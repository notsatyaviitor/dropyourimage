"""Session-wide test configuration.

Three independent things are pinned here, all because `conftest.py` is guaranteed to run before
pytest imports any test module — no other hook in this codebase can make that guarantee.

1. **Memory mode.** Forces the API layer to use in-memory storage and job store rather than real
   Redis/MinIO, regardless of what `backend/.env` points at for local development.
   `pydantic-settings` prioritises real environment variables over the `.env` file, so setting
   these here overrides it cleanly without touching the developer's own `.env`. Must happen
   before the first call to `get_settings()` (it is `@lru_cache`d).

2. **No vendor keys, ever.** The root rule is that `pytest` passes on a clean checkout with no API
   keys — but tests read the developer's `.env`, so on a machine that *has* keys the suite was
   silently doing something else entirely. Two real incidents:

   * A stray `ENGINE_POOL` value left over from an experiment branch failed `Settings()`
     construction and errored 60+ tests that have nothing to do with engine selection.
   * Once a real `REMOVEBG_API_KEY` was configured, `tests/test_api.py` began making **live, paid**
     segmentation calls, and started failing when that account ran out of credits.

   The second is the serious one: a test suite must never spend money or depend on a network. These
   are set to empty rather than `setdefault` — unlike memory mode, a developer must not be able to
   opt in by exporting a key, because the cost is silent. Tests that need a key construct
   `Settings(...)` explicitly with a fake one.

3. **Single-threaded numerics — see `app/__init__.py` for the full rationale.** That module pins
   the same thread env vars, but several test files do `import numpy as np` textually before any
   `from app... import`, which imports NumPy's BLAS backend before `app/__init__.py` has run.
   BLAS libraries commonly read their thread-count env var once, at load time, so setting it after
   import can be too late. `conftest.py` has no such ordering risk.
"""

import os

for _var in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_var, "1")

os.environ.setdefault("S3_ENDPOINT_URL", "memory")
os.environ.setdefault("REDIS_URL", "memory")

# Deliberately unconditional. See point 2 above.
for _key in (
    "PHOTOROOM_API_KEY",
    "REMOVEBG_API_KEY",
    "FAL_KEY",
    "GEMINI_API_KEY",
    "ADOBE_CLIENT_ID",
    "ADOBE_CLIENT_SECRET",
    "ADOBE_ORG_ID",
    # Same reasoning as the vendor keys, for the same reason it is unconditional: a developer's
    # .env now names a real GCS bucket, and a test that reached it would write objects into
    # production storage and bill for them. Memory mode above already wins, so this is the second
    # lock on the same door — deliberately, because the cost of it failing is silent.
    "GCP_BUCKET_NAME",
    "GCP_KEY_FILE",
    "GCP_PROJECT_ID",
):
    os.environ[_key] = ""

# Also pinned: a local ENGINE_POOL naming an engine this branch does not have is a config error in
# someone's .env, not a reason for the imaging tests to fail.
os.environ["ENGINE_POOL"] = "photoroom,falai,removebg"

try:
    import cv2 as _cv2

    _cv2.setNumThreads(1)
except ImportError:
    pass

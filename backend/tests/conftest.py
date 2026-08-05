"""Session-wide test configuration.

Two independent things are pinned here, both because `conftest.py` is guaranteed to run before
pytest imports any test module — no other hook in this codebase can make that guarantee.

1. **Memory mode.** Forces the API layer to use in-memory storage and job store rather than real
   Redis/MinIO, regardless of what `backend/.env` points at for local development.
   `pydantic-settings` prioritises real environment variables over the `.env` file, so setting
   these here overrides it cleanly without touching the developer's own `.env`. Must happen
   before the first call to `get_settings()` (it is `@lru_cache`d).

2. **Single-threaded numerics — see `app/__init__.py` for the full rationale.** That module pins
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

try:
    import cv2 as _cv2

    _cv2.setNumThreads(1)
except ImportError:
    pass

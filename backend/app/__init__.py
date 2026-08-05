"""Package init. Runs before any submodule, which is why the determinism pinning lives here.

Pin numerical libraries to a single thread before they are used anywhere.

Why: this pipeline's stages 2-5 are documented and tested as deterministic — same input bytes in,
same output bytes out, every time. That guarantee is what the cut-out cache
(`sha256(image)+engine`) and the "re-running while tuning colour costs nothing" claim both rest
on. Multi-threaded BLAS (used by `numpy.linalg.lstsq` in the shadow-plate fit) and OpenCV's own
threaded reductions are not guaranteed bit-reproducible across runs: floating-point addition is
not associative, so a different thread-scheduling order can sum the same values in a different
sequence and round to a different bit.

This was not hypothetical. Measured directly: an otherwise-identical PNG output differed by one
byte under full-test-suite CPU load, never under a quiet single-file run — the classic signature
of a multi-threaded reduction. A first attempt at fixing it set `OMP_NUM_THREADS`-style env vars
before importing numpy/cv2, which helped but did not fully eliminate the failure (2/10 full-suite
runs still diverged under a deliberate concurrent-load stress test) — some BLAS builds establish
their thread pool from more than just the env var read at import time, so the env var is not a
reliable enough guarantee on its own.

`threadpoolctl` is the real fix, and it is applied in two places, not one:

* **Here**, once, process-wide, for defence in depth against any incidental BLAS use elsewhere.
* **At the actual point of use** — the `with threadpool_limits(limits=1):` around the
  `numpy.linalg.lstsq` call in `app/imaging/shadow.py` — which is the guarantee that actually
  matters. `threadpool_limits` reaches into the BLAS library that is loaded *at the moment it is
  called*; called here, at process start, it can run before numpy (and the OpenBLAS it bundles)
  has even been imported anywhere, in which case there is nothing yet to limit and this call is a
  silent no-op. That gap is exactly what re-applying it at the call site in `shadow.py` closes:
  by the time `np.linalg.lstsq` runs, numpy is certainly already imported, so the library is
  there to be limited. Confirmed by measurement — the process-wide call alone still left the
  pipeline non-deterministic in roughly 2 of 10 full-suite runs under concurrent CPU load; adding
  the call-site wrap in `shadow.py` was what actually made
  `tests/test_pipeline.py::TestDeterminism::test_stays_deterministic_under_concurrent_cpu_load`
  pass reliably.

`cv2.setNumThreads` does not have this problem — it is a plain runtime toggle with no
load-order sensitivity, so pinning it once here is sufficient on its own.
"""

from threadpoolctl import threadpool_limits

# Persists for the process — not a context manager. Defence in depth; see the docstring above for
# why the call in app/imaging/shadow.py is the one that actually matters.
threadpool_limits(limits=1)

try:
    import cv2 as _cv2

    _cv2.setNumThreads(1)
except ImportError:
    pass

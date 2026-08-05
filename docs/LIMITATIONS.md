# Limitations — read before demoing

What this POC does not do, stated plainly rather than discovered live in front of a client.

## No measured quality claim

There is no ground-truth mask benchmark and no SAD/MSE/ΔE harness against real product
photography. The demo shows quality convincingly (measured IoU ~1.0 on synthetic test scenes for
the local engine, halo reduction measured on synthetic edges) but **cannot support a numeric
accuracy claim on real client photography**, and cannot answer "which vendor API should we buy."
That needs a 3–4 day metrics harness against real, labelled images — a real follow-on, not built
here.

## Out of scope, and kept out of any demo image set

Glass and clear plastic, sheer fabric (tulle/lace/mesh), dedicated hair/fur handling. Soft alpha
is preserved throughout so these degrade gracefully rather than being actively broken, but they
are not supported and will look poor if shown. `data/input/edge-cases/` exists specifically to
know how bad, rather than guess.

## The local control engine is not production quality

`app/engines/local.py` exists so the pipeline is runnable before any vendor key arrives. It works
well on synthetic two-colour scenes (measured IoU ~1.0) but has known, explicit failure modes:
white-on-white (unsolvable by colour alone) and a heuristic split between "black product" and
"cast shadow" that a genuinely hard case could confuse. It must never be the default once a real
segmentation key is configured.

## Vendor integrations are unverified against live traffic

- **Photoroom / remove.bg / fal.ai** (`app/engines/http.py`): endpoint shapes are the documented
  forms, not confirmed against live responses. Isolated in module constants for a one-line fix.
- **Gemini vision tie-break**: live-tested and working (see `docs/ENGINES.md`), but depends on
  `gemini-3-flash-preview` — a preview model, since the client report's named
  `gemini-3-flash` does not exist in the API. A real dependency risk, not hidden.
- **Adobe Photoshop API** (`app/psd/photoshop_api.py`): **entirely unverified**. No credentials
  were provided. The request shapes are transcribed from Adobe's docs, not tested. See
  `docs/PSD.md` for exactly what needs confirming before this touches a real account.

## PSD

- The vector clipping path has been verified by an independent decoder and by `psd-tools` opening
  the file without error — **not** by an actual open in Adobe Photoshop, which this environment
  does not have. Do this before shipping to a client.
- External contour only (no holes — a mug handle's gap isn't a separate subpath).
- Straight-line Bezier segments, not curve-fitted — faceted rather than smooth in the Paths panel.
- The shadow layer's darkening is proportionally correct but not byte-exact against the raster
  output, due to gamma-space vs linear-light compositing — see `docs/PSD.md` for the measured
  detail.

## No production infrastructure

No auth, no multi-tenancy, no autoscaling, no production TLS termination, CORS wide open. This is
explicitly a demo target — see `docs/SECURITY.md` for what would need to change before it touches
anything beyond a demo.

## Determinism has edge cases that were found and fixed, but the class of bug could recur

Any library that embeds wall-clock time or spins up uncontrolled thread pools is a risk to the
byte-reproducibility guarantee the cut-out cache depends on. Two real instances were found and
fixed (an ICC profile timestamp, BLAS thread pinning) — see `backend/CLAUDE.md`'s Determinism
section before adding a new dependency that touches image encoding or heavy numeric computation.

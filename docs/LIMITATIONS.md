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

## Input formats — what round-trips and what cannot

Fourteen formats decode. **Eight can be written back; six cannot, permanently.**

| | Formats | Same format out? |
|---|---|---|
| Round-trips | PNG, JPG/JPEG, BMP, TIF/TIFF, WebP, EPS, PSD | ✅ verified live over HTTP |
| Cannot | CRW, CR2, CR3, DNG, NEF, RAW | ❌ delivers 16-bit TIFF + `format_substituted` |

**Camera raw is decode-only and always will be.** These are undemosaiced sensor readout plus
proprietary maker notes, not image containers. No encoder exists in any library — Canon and Nikon
publish none, and libraw is explicitly a decoder. More basically, the format cannot express what
this pipeline produces: a CR2 means "this is what the sensor recorded", and there is no valid CR2
meaning "that photo, composited onto `#F5F5F5`". Two workarounds were considered and rejected in
`app/imaging/formats.py`: renaming the output `.cr2` (Photoshop rejects it as corrupt) and
rewriting the raw's embedded JPEG preview (thumbnailers would look right while any real editor
opened the *original* — the same failure mode as the blank-PSD-preview bug).

**DNG is the one genuine exception, and is deliberately not implemented.** It is an open,
TIFF-based Adobe spec, so a Linear DNG could be written. It is grouped with the other raws because
it could not be verified here — no Adobe software — and shipping an unverifiable writer is the
mistake `docs/PSD.md` already records. Revisit when someone can open the output in Photoshop.

**⚠️ Raw decoding has never been run against a real camera file.** There are no raw files in the
repo and none may be added (`data/` is gitignored, and the suite must run with no images). The
tests cover routing and substitution with a stubbed libraw: they prove a `.cr2` goes to libraw and
comes back as 16-bit TIFF with a note, **not** that libraw renders a real Canon file correctly.
Colour and exposure from a genuine raw are unverified. Test with real files before demoing raw.

EPS needs Ghostscript at the system level (present here). EPS output is a PostScript wrapper around
a raster, not vector artwork — an EPS in gives a valid EPS out, but nothing is vectorised.

JPEG, BMP and EPS carry no alpha, so all three are rejected with `background.transparent`.

**TIFF, EPS and PSD cannot be shown in a browser.** No major browser renders them in an `<img>`, so
a job delivering only those used to produce a blank results card — correct files, broken-looking
UI. The backend now adds a PNG purely for display (`ImageResult.preview_format`), excluded from the
download bundle. Worth knowing when demoing: the thumbnail for a TIFF job is a PNG rendition, not
the delivered file, and the card says so.

## Layer-per-object PSD (`cutout.multi_object`)

Verified live on a bedroom interior: 9 objects enumerated (bed, ceiling fan, bedside table, 4
pillows, 2 potted plants), 9 named PSD layers each with its own saved path (2000-2008), 2999
designating the bed, composite intact.

Four things to know before demoing it:

- **Cost is per object.** That bedroom was **$0.18** — nine segmentation calls — against $0.02 for
  an ordinary cut-out. A 50-image batch of rooms is not a $1 job.
- **It will hit vendor rate limits.** One call per object means a nine-object room is a nine-call
  burst. On 7 Aug 2026 this exhausted the Photoroom free tier mid-session: `"Request was throttled.
  Expected available in 8705 seconds"` — 2.4 hours, with even a single call refused. The image now
  fails with `vendor_rate_limited` rather than the misleading "no separable objects were found"
  it reported before. Budget quota per object, not per image.
- **The object list is a model's judgement, not a measurement.** Gemini decides what counts as a
  thing. It listed four separate pillows and two plants; a different run may split or merge
  differently. Nothing downstream can tell a wrong list from a right one.
- **No shadow layer and no background replacement.** Both need a uniform backdrop; a room has
  none. The frame is the bottom layer instead.
- **Files are large.** Raw compression plus one full-canvas layer per object: the 900x900 bedroom
  PSD is **34.8 MB**. Ten layers at full canvas size, uncompressed.

Mask quality per object is bounded by the segmentation engine working on a crop, and on a busy
scene that is materially worse than on a packshot — the same `alpha_suspect`/`busy_scene` caveats
apply, per layer, without being individually reported.

## Vendor integrations — what is live-verified and what is not

- **⚠️ Photoroom is WATERMARKING every mask — the current key is on a free tier.** Measured 7 Aug
  2026: the raw alpha carries "Photoroom" tiled across the whole frame at up to 31% opacity,
  including regions with no subject. Because the watermark is in the **alpha**, taking only the
  alpha does not escape it, and every delivered composite inherits ghost lettering.
  `scripts/check_engine.py` now warns on it (it asks whether the mask is actually zero where there
  is no subject — the question none of the earlier assertions asked). **A paid plan is required
  before anything is shown to a client.** Nothing in the code can work around this.
- **Photoroom** (`app/engines/http.py`): **live-verified 7 Aug 2026** and now the default engine.
  793–861 ms, mask at source resolution, 204 distinct alpha values with 3737 soft edge pixels, full
  five-stage pipeline to an exact 500×500 canvas and a byte-exact `#F5F5F5` background. The check
  found **no** wire-format defects. Re-confirm with `scripts/check_engine.py photoroom`; one call.
- **fal.ai** (`app/engines/http.py`): wire format **confirmed against vendor docs**, but **no key
  exists**, so it has never been exercised against a live response.
- **remove.bg**: **retired 7 Aug 2026** on accuracy, and at ~$0.20/image it was 10× Photoroom. The
  adapter and its tests remain in the tree and still pass; it is simply out of `ENGINE_POOL` and
  out of the UI. It was live-verified on 6 Aug 2026 before retirement.
- **Gemini as a segmentation engine** (`app/engines/gemini_segment.py`): **live-verified and
  shipped, with a known and accepted quality trade.** It returns the mask as a *polygon*, so the
  cut-out edge is hard: **2 distinct alpha values and 0 soft edge pixels**, against 27 for ground
  truth and 204 for Photoroom. It also over-covers by ~54% and describes a circle with 13–24
  vertices.
  - **Do not demo it on soft-edged subjects**, and do not pair it with a saturated background
    colour — edge decontamination has no partial-coverage band to correct, so a halo can show.
  - It is **never auto-picked**. Its mask still scores 0.7246 and passes every reject rule, so in an
    AUTO pair it could beat Photoroom (0.9659) on score while looking worse. Selectable only.
  - Every such result carries `hard_edged_mask`. Never present a Gemini cut-out as equivalent to a
    Photoroom one.
  - Evidence base is **thin**: the numbers above come from the synthetic studio fixture plus one
    live run per resolution. There is no real-photograph comparison, for the same reason recorded
    at the top of this document — there is no ground-truth mask set.
- **Gemini image-edit for background removal**: measured and rejected, unchanged. Those models
  return RGB/JPEG with no transparency at all, so an alpha-extraction engine cannot work against
  them. The unmerged implementation sits on `experiment/gemini-edit-engine`.
  - ⚠️ **A previous claim in this file that native Gemini segmentation takes 182–245 s has been
    retracted** — re-measured at 1.7–8.8 s once thinking is set to `MINIMAL`. See the retraction
    box in `ENGINES.md`. Do not quote the old figure.
- **Gemini vision tie-break**: live-tested, and the result is worse than this document previously
  claimed. On 4 counterbalanced trials the best model (`gemini-3.5-flash`, now the default) scored
  3/4, and `gemini-3-flash-preview` scored 2/4 *only by always picking the first image shown* —
  position bias, not judgement. Comparisons are now counterbalanced and a winner is accepted only
  when both orderings agree, so the failure mode degrades to the deterministic score. **Do not
  present a `tiebreak_vision` outcome as a quality measurement**: it is a model's opinion, and it is
  the only non-deterministic step in the pipeline.
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

## Bulk orders (200–400 images) — what works and what to expect

Supported, and verified end to end at 240 images (memory-bounded ingest, correct accounting,
exact output dimensions). Five things to know before demoing one:

- **It needs a worker.** Memory mode runs jobs inside the HTTP request handler, so anything over
  `INLINE_JOB_MAX_IMAGES` (25) is refused with `bulk_requires_worker`. `docker compose up -d` plus
  an `rq worker` is not optional for a bulk demo — start it before the meeting, not during it.
- **It will hit vendor rate limits, and that is the expected path, not the exception.** 400 images
  is 800 calls on the dual-engine pool. The job now throttles itself per vendor and reports
  `rate_limit_events` so the slowdown is visible and attributed, but a throttled batch is a *slow*
  batch — budget wall-clock accordingly. Photoroom's free tier was measured refusing everything for
  8705 seconds once exhausted.
- **Cost is real.** ~$24 for 400 images on Photoroom + fal.ai. The pre-flight estimate and the $25
  `MAX_JOB_COST_USD` ceiling exist because that is a meaningful fraction of the ~$1,300 POC month.
  Do not run a full 400-image batch casually to check something.
- **The spend ceiling is approximate on the multi-object path only.** It reserves one image's pool
  cost before starting each image, which binds correctly for ordinary packshots. `multi_object`
  bills per *object* and the object count is unknown until Gemini has enumerated the scene, so a
  scene-heavy job can overshoot. `CostEstimate.per_object_pricing` flags this rather than hiding it.
- **The download bundle is one zip of everything.** 400 images across two formats is a
  multi-gigabyte download. It is assembled through a temp file rather than in memory, so the worker
  survives it, but the *browser* still has to download it in one piece. There is no per-image or
  chunked download path; individual assets are downloadable one at a time from the results grid.

Not addressed, and worth naming: there is no resume. If a bulk job dies partway — worker killed,
Redis restarted — the finished images are still in storage but the job record is what it is, and
re-running means re-uploading. The cut-out cache means re-running is cheap in vendor spend (same
bytes, same engine → cache hit), not that it is free in time.

### Large PSD sources change the arithmetic

The client's own test set is **132 PSDs of 130.0 MB each** — 5504×8256 RGB written uncompressed,
which is why every file is byte-identical in size. Measured on that set:

| | Measured | Consequence |
|---|---|---|
| Decode + pipeline | **~45 s/image** at 45.4 MP | a 132-image order is ~1.7 hours, before any vendor call |
| One order | 16.8 GB uploaded | needs MinIO/S3; memory-mode storage cannot hold it |
| Per batch | 130 MB/file → 2 files per 256 MB batch | ~66 upload requests for one order, not 3 |

`MAX_IMAGE_BYTES` is `0` (no per-image byte limit) precisely because chasing a number failed
twice here — 50 MB rejected the whole set, 200 MB then rejected a 260 MB file. `MAX_IMAGE_PIXELS`
(80 MP) remains the real guard. Two settings *are* still sized against these numbers and are wrong
if your sources are much larger: `MAX_JOB_UPLOAD_BYTES` (24 GB) and `queue._SECONDS_PER_IMAGE`
(60 s, which sets the RQ timeout).

**`WORKER_CONCURRENCY` is the one to watch.** At 45 MP, `rgb_linear` alone is 545 MB of float32
per image, before alpha and the placement buffers. Eight concurrent images is several gigabytes of
resident numpy. The ingest path is memory-bounded (one archive, one entry at a time), but the
*pipeline* is not bounded by image size — so on a machine with limited RAM, lower
`WORKER_CONCURRENCY` for PSD-heavy batches. This is a real ceiling and it has not been tuned
automatically.

## No production infrastructure

No auth, no multi-tenancy, no autoscaling, no production TLS termination, CORS wide open. This is
explicitly a demo target — see `docs/SECURITY.md` for what would need to change before it touches
anything beyond a demo.

## Determinism has edge cases that were found and fixed, but the class of bug could recur

Any library that embeds wall-clock time or spins up uncontrolled thread pools is a risk to the
byte-reproducibility guarantee the cut-out cache depends on. Two real instances were found and
fixed (an ICC profile timestamp, BLAS thread pinning) — see `backend/CLAUDE.md`'s Determinism
section before adding a new dependency that touches image encoding or heavy numeric computation.

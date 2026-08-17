# API contract

**Frozen.** The source of truth is `backend/app/models.py`; this document is a readable map of it,
not a second definition. `frontend/src/api/types.ts` mirrors it by hand — when the contract
changes, update both plus this doc in the same change.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | Upload a zip + `JobConfig` JSON, get a `JobCreated` |
| `POST` | `/jobs/{job_id}/start` | Run a job whose batches were uploaded with `start=false` |
| `POST` | `/jobs/{job_id}/cancel` | Ask a running job to stop after its in-flight images |
| `POST` | `/jobs/estimate` | Estimated vendor spend for a batch of a given size |
| `GET` | `/jobs/{job_id}` | Poll for `JobStatus` |
| `GET` | `/jobs/{job_id}/images` | One page of `ImageResult`s |
| `GET` | `/objects/{key}` | Memory-mode asset passthrough (S3 mode uses presigned URLs directly) |
| `GET` | `/health` | Redacted config snapshot — no secrets, ever |

`POST /jobs` takes `multipart/form-data`: a `file` field (the zip) and a `config` field (JobConfig
as a JSON string). Invalid config is a `422` with FastAPI's validation error list.

## Bulk: 200–400 images in one order

A single archive does not scale to a large order, and **the binding constraint is the browser, not
the network**: zipping client-side holds every file's bytes, then the assembled zip, then the
`File` — roughly three times the total — before a byte is sent. At 400 photos the tab dies first.

So `POST /jobs` takes two optional extra form fields, and today's single-shot call is unchanged:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `job_id` | `str \| null` | `null` | Append this archive to an existing job instead of creating one |
| `start` | `bool` | `true` | Run the job once this archive is stored |

```
POST /jobs        config + batch 1, start=false   -> {job_id, batches: 1, accepting_uploads: true}
POST /jobs        job_id + batch 2, start=false   -> {batches: 2, accepting_uploads: true}
...
POST /jobs/{id}/start                             -> {accepting_uploads: false}
```

Rules that matter:

- **`start=true` (the default) means one POST still creates, runs and returns**, exactly as before.
- The config is stored with the **first** batch and later batches cannot change it. One job is one
  `JobConfig`; letting batch 5 alter the background colour would deliver an order whose images
  disagree with each other, with no record of which setting produced which file.
- Every batch is screened by the full set of archive guards independently. Being the second batch
  of an accepted job confers no trust.
- Appending to a job that has already started is a **409**.
- The last batch does not start the job — `/start` does. Starting on the final upload would race a
  batch that failed and is being retried.

**Job-level caps** sit on top of the per-archive ones, because N uploads against one job id would
otherwise multiply straight through them: `MAX_JOB_IMAGES` (500) and `MAX_JOB_UPLOAD_BYTES` (4 GB),
both **413** with `batch_too_large`.

**Memory mode refuses bulk.** With no Redis configured, jobs run inline inside the POST handler —
fine for a handful of images, a guaranteed timeout for hundreds. Above `INLINE_JOB_MAX_IMAGES` (25)
the request is a **503** with `bulk_requires_worker`, naming the fix. Start Redis and an
`rq worker`; see `docs/SETUP.md`.

`GET /health` publishes the real limits under `config.limits`, so a client sizes its batches from
the server rather than a hardcoded copy that drifts:

```json
{"max_batch_images": 200, "max_batch_bytes": 524288000, "max_job_images": 500,
 "inline_max_images": 25, "bulk_enabled": false, "max_job_cost_usd": 25.0}
```

### Cost guard rails

`POST /jobs/estimate` (`config` + `images`) returns a `CostEstimate`. **Every figure is an
estimate** — per-engine list prices, not invoices, and the real total moves with cache hits and
retries. It exists so a 400-image click is an informed one, and is never a price to a customer.
`per_object_pricing` marks the multi-object case, where cost is per *object* and the object count
is unknowable in advance, so the figure is a floor rather than a total.

`MAX_JOB_COST_USD` (default $25) stops a job that reaches it; whatever finished is kept and
downloadable, and the remainder is `SKIPPED` with `budget_exceeded`. Each image reserves its
projected cost before starting, so the ceiling binds on *spent + in flight* — comparing spend
alone let a whole concurrency window overshoot it.

`POST /jobs/{id}/cancel` sets `JobState.CANCELLED`'s trigger. It returns the current status and
does **not** flip the state itself: the worker owns that transition, and claiming it up front would
report a stop that has not happened while images are still billing. Poll for the real state.
Images already finished stay downloadable — a cancel is "stop spending", not "discard what I paid
for" — and each unprocessed image is recorded **by name** with `job_cancelled`, because the next
question is "which ones do I still need?". Idempotent, and accepted on a terminal job.

### Polling a large job

`GET /jobs/{id}?include_images=false` omits the per-image records. They are the entire payload —
measured **231× smaller** on a 180-image job — and the progress UI reads none of them. Counts stay
accurate either way; `images_total` reports how many records exist.

`GET /jobs/{id}/images?offset=&limit=` (limit 1–200, default 50) returns an `ImagePage` with fresh
signed URLs for that page only.

## `JobConfig` — one field group per UI tab

```
JobConfig
├── cutout      CutoutSpec       Tab 1 "Clipping"           — the only AI stage
├── background  BackgroundSpec   Tab 2 "Background Colour"
├── size        SizeSpec         Tab 3 "Size"
├── centring    CentringSpec     Tab 4 "Centring"
├── psd         PsdSpec          Tab 5 "PSD"
└── export      ExportSpec       formats + colour profile (lives in the Size tab in the UI)
```

Cross-field rules enforced by the model itself (not by the UI, though the UI also fixes these up
before submitting so a user never sees the rejection):

- `background.transparent=true` conflicts with `'jpeg'` in `export.formats` — JPEG has no alpha.
- `psd.enabled` must agree with whether `'psd'` is present in `export.formats`.

### `cutout.subject_prompt` — for scenes with more than one object

| Field | Type | Default | Meaning |
|---|---|---|---|
| `subject_prompt` | `str \| null` | `null` | Which object to extract, in plain words (`"coffee table"`). Max 120 chars; blank/whitespace normalises to `null`. |
| `subject_padding_pct` | `float` | `6.0` | Context kept around the located object before segmenting, as a percentage of the box's own size. 0–50. |

`null` means whole-frame segmentation, which is correct for a packshot and costs no extra call. Set
it only when the photograph contains several objects: background removal separates foreground from
background and **cannot tell which foreground you meant**, so on a furnished-room photo it may return
a clean, confident cut-out of the wrong object.

When set, the response carries `subject_located`, or `subject_not_found` if the object could not be
found — in which case the whole frame was segmented instead and the result is probably not what was
asked for. That note must be surfaced prominently, not as a quiet badge.

`JobConfig.config_hash()` is a stable hash of every field that affects output pixels
(`preset_name` excluded). Used for the cut-out cache key and stamped on results for provenance
comparison.

## `JobStatus` — what you get back from polling

```
JobStatus
├── state          queued | running | completed | failed | cancelled
├── total / completed / failed
├── images[]        one ImageResult per file — EMPTY when polled with include_images=false
├── images_total     how many records exist, regardless of how many `images` carries
├── cost_usd         running vendor spend for the whole job
├── bundle_url       signed zip of every output, once state is COMPLETED
├── bundle_key       storage key behind it; the URL is re-signed on every read
├── batches          upload archives accepted (1 for an ordinary single-zip job)
├── upload_bytes     accumulated archive size, against MAX_JOB_UPLOAD_BYTES
├── accepting_uploads  true between the first batch and /start — the job is a draft
├── rate_limit_events  times a vendor throttled us; explains a slow batch honestly
└── error            only set if the JOB itself failed (bad archive) — not per-image failures
```

A cancelled or budget-stopped job that produced **any** output reports `completed`, not
`cancelled`: the assets exist and are downloadable, and reporting the whole job as cancelled would
imply nothing came back. The per-image `SKIPPED` records carry the reason. `cancelled` is reserved
for a job that stopped before anything finished.

`state == "completed"` means every image reached a terminal state, **not** that every image
succeeded — check `images[].state` for per-image outcomes. A job only fails outright when nothing
in the archive could be processed at all (e.g. not a valid zip).

## `ImageResult` — per image

```
ImageResult
├── state              pending | running | done | failed | skipped
├── outputs[]           one OutputAsset per requested format (empty unless state == done)
├── notes[]             non-fatal deviations from the request — see below
├── error               typed ErrorInfo, only if state == failed
├── chosen_engine       which segmentation engine won, if auto-pick ran
├── candidates[]        every engine tried, with scores/rejection reasons — for side-by-side review
├── centroid_offset_px  measured, not assumed — how far off canvas-centre the product landed
├── background_uniformity   drives the shadow-preservation gate; reported even when it fails
└── source_preview_url  small PNG of the file AS UPLOADED, for the "before" panel — see below
```

### `source_preview_url` — the input-side counterpart of `preview_format`

`preview_format` exists because no browser renders TIFF, EPS or PSD, so a job delivering only
those painted an empty results card. The **before/after row has the same problem from the other
end**: it shows the user's *source* file, handed straight to an `<img>` that cannot decode it. Every
PSD and EPS upload rendered a blank "before" beside a correct "after".

So when the source format is one a browser cannot paint, the backend stores a small PNG of the
decoded source (768 px long edge) and names it here. It is `null` for `png`, `jpeg`, `bmp` and
`webp` — the client already holds those files locally, which is free, instant and full resolution.

Like `preview_format`, it is **not a deliverable**: it lives outside `outputs`, is excluded from the
download bundle, and is re-signed on read like every other asset.

### Notes — every one means "we did something other than literally what you asked"

The frontend renders every one of these; never swallow one silently (see `frontend/CLAUDE.md`).

| Note | Meaning |
|---|---|
| `shadow_gate_failed` | Background too non-uniform to preserve the shadow; removed instead |
| `upscale_skipped` | Source smaller than requested canvas; padded, not stretched |
| `upscaled` | Routed through an AI upscaler — pixels beyond the source are synthesised |
| `engine_fallback_used` | The preferred engine's cut-out lost the auto-pick comparison |
| `single_engine_only` | Only one engine was available — no comparison was made |
| `tiebreak_deterministic` | Candidates scored equal; picked by measurement, no vision call |
| `tiebreak_vision` | Candidates scored equal; a vision model judged between them — a judgement, not a measurement |
| `alpha_suspect` | Cut-out passed automatic checks but scored low — worth a manual look |
| `psd_fallback_raster` | PSD has no vector clipping path — layers and mask only |
| `cover_cropped` | COVER fit mode removed product pixels to fill the canvas |
| `subject_located` | A subject prompt isolated one object; only that region was segmented |
| `subject_not_found` | Subject prompt matched nothing — whole frame segmented, very likely the wrong object |
| `busy_scene` | Backdrop is not a uniform studio sweep; the cut-out may be of the wrong object |
| `scene_largest_object` | The mask held several separate objects, so only the largest was kept rather than failing the image. **A guess** — largest is not the same as wanted. Deterministic (connected components), free, and skipped entirely when `cutout.subject_prompt` is set |
| `hard_edged_mask` | Cut out by an engine that returns a boundary, not a coverage field — no soft alpha, so edge decontamination had nothing to correct. Emitted for `gemini` |
| `vendor_downscaled` | Too large to send the vendor at full resolution, so the mask was computed on a reduced copy and scaled back up. **Only mask precision is affected** — engines return alpha, never colour, and the composite uses the full-resolution original — but edges are softer. Only emitted when a lossless re-encode was not enough on its own |
| `multi_object` | The scene was split into one PSD layer per object; `ImageResult.layers` names them |
| `format_substituted` | `match_source` was asked for but the source format cannot be written; delivered as 16-bit TIFF. Emitted for camera raw |

### `cutout.multi_object` — a scene as one layer per object

Off by default. On, the image is treated as a **scene** rather than a packshot: a model enumerates
the objects, each is segmented separately, and the PSD carries one named layer and one saved path
per object. `ImageResult.layers` lists them largest-first; the result carries `multi_object`.

Three stages are skipped, because none of them are meaningful for a room — **centring**,
**shadow reconstruction**, and **background replacement**. The frame becomes the bottom layer.

**Cost is per object, not per image.** Nine objects is nine segmentation calls. `MAX_OBJECTS` caps
it at 12; objects arrive largest-first so the cap drops the least significant ones.

PSD allocates saved paths across resource IDs 2000–2997, so N objects occupy 2000..2000+N−1.
Resource 2999 designates the single document clipping path, which names the largest object.

### `preview_format` — why an output you did not ask for may appear

`CompleteStep` puts an output URL straight into an `<img>`, and **no browser renders TIFF, EPS or
PSD** (Chrome, Firefox and Edge render none; Safari manages TIFF alone). A job delivering only
those produced perfectly good files and a blank results card.

So when none of the delivered formats is viewable, the backend adds one PNG purely for display and
names it in `ImageResult.preview_format`. It is a real entry in `outputs`, but it is **not a
deliverable**: it is excluded from the download bundle, and clients should exclude it from download
links. `preview_format` is `null` whenever a requested format is already viewable — the common
case, which pays for no extra encode.

Viewable formats: `png` · `jpeg` · `webp` · `bmp`.

### `size.match_source` — deliver at the source's own resolution

`false` by default, so existing jobs are unchanged. When `true`, `width`/`height` are ignored and
each image is delivered at its own pixel dimensions; the canvas is still exact, only the numbers
come from the photograph instead of the config.

This is the answer to "the output is less sharp than what I uploaded". That is a canvas choice
rather than a resampling fault — resizing already steps down with `INTER_AREA` before a final
Lanczos pass — and a 50.6 MP raw asked for at the 500×500 default keeps **0.49%** of its pixels,
which no filter can recover. Leave it off when an order needs one fixed size for a marketplace.

Still bounded by `MAX_OUTPUT_PIXELS`, so an enormous source is refused with `output_too_large`
rather than silently shrunk, and each axis is clamped to the 20000 contract bound.

### Formats

**`SourceFormat` — what can be read (14).**
`png` · `jpeg` · `bmp` · `tiff` · `webp` · `eps` · `psd` · `crw` · `cr2` · `cr3` · `dng` · `nef` · `raw`
(`.jpg`/`.jpeg` both map to `jpeg`; `.tif`/`.tiff` both to `tiff`.)

**`OutputFormat` — what can be written (7).**
`png` · `jpeg` · `tiff` · `webp` · `bmp` · `eps` · `psd`

**`export.match_source`** (default `true`) additionally delivers each image in the format it
arrived as, unioned with `export.formats`. A PNG in gives a PNG out; a TIFF a TIFF.

**Camera raw is the exception, and the download bundle compensates.** CRW/CR2/CR3/DNG/NEF/RAW have
no encoder in any library — Nikon and Canon publish no writer spec, and a Linear DNG written here
was verified to round-trip *wrong* through libraw — so `match_source` can only substitute 16-bit
TIFF for them (`format_substituted`). When `match_source` is on, the bundle therefore also carries
the **untouched original upload** (`shot.nef` beside `shot.tiff`), byte-identical to what was sent.
It is the source, not a processed asset: it cannot carry the cut-out, because no format in that
family can express one. Copied straight from the stored upload archive, so nothing is duplicated in
object storage.

The delivered set is the **union** of the two fields, which gives three modes:

| `formats` | `match_source` | Delivered, for a mixed batch of 7 |
|---|---|---|
| `[]` | `true` | **one file per upload, each in its own format** — 7 files |
| `["png"]` (default) | `true` | the source format *and* a PNG — 13 files |
| `["png"]` | `false` | one fixed format for everything — 7 PNGs |

`formats` may be **empty only when `match_source` is true**. That combination is how you ask for
"give me back exactly what I uploaded and nothing else"; it was previously impossible, because
`formats` required at least one entry, so every image collected an extra file nobody asked for —
a PNG beside every PSD, turning a 132-image order into 264 files. Empty *and* `match_source:false`
is a `422`, since it asks for no output at all.

Two things still hold in the empty case: a job delivering only non-viewable formats still gets a
`preview_format` so the results grid has something to paint (excluded from the bundle as always),
and a source whose extension is unrecognised falls back to PNG with `format_substituted` rather
than delivering nothing.

**Camera raw cannot be written, ever.** `crw`/`cr2`/`cr3`/`dng`/`nef`/`raw` are undemosaiced sensor
data plus proprietary maker notes; no encoder exists in any library because the format cannot
express a processed RGB image. Those deliver **16-bit TIFF** and carry `format_substituted`.
`ImageResult.source_format` reports what arrived, so a client can show both halves of the swap.

`jpeg`, `bmp` and `eps` carry no alpha channel. Requesting any of them with
`background.transparent` is rejected — the same rule JPEG has always had.

### `EngineId`

`photoroom` · `falai` · `gemini` · `local` · `removebg`

`photoroom` is the default. `gemini` is selectable but **never auto-picked** — it is absent from
`ENGINE_POOL` because its hard-edged mask still passes every auto-pick reject rule, so it could win
a comparison on score while shipping a worse edge. Reach it with
`{"strategy": "single", "engine": "gemini"}`; results carry `hard_edged_mask`.

`removebg` was **retired on 7 Aug 2026** (accuracy, and 10× the cost of Photoroom). It stays in the
enum so previously-saved configs still validate, and the adapter still works if a key and pool entry
are restored — but it is not offered in the UI and nothing selects it by default.

### Error taxonomy

Every failure carries a typed `ErrorCode` — `vendor_rate_limited`, `vendor_timeout`,
`vendor_unauthorized`, `vendor_out_of_credits`, `vendor_payload_too_large`,
`no_foreground_found`, `vendor_error`,
`unsupported_file`,
`file_too_large`, `output_too_large`,
`malicious_archive`, `image_decode_failed`, `psd_unavailable`,
`batch_too_large`, `bulk_requires_worker`, `budget_exceeded`, `job_cancelled`,
`internal_error` — plus a
`retryable` flag. The point of
this list existing at all: "the vendor throttled us" must never render the same as "your file is
broken" or "we have a bug."

The four bulk codes keep that separation at batch scale. `batch_too_large` is a legitimate upload
with too much in it, not a hostile archive — a user shown `malicious_archive` for a 600-image order
learns the wrong thing. `bulk_requires_worker` is a deployment gap with a named fix, not a fault in
the request. `budget_exceeded` and `job_cancelled` are deliberate stops, and the images behind them
are still there.

`vendor_payload_too_large` (HTTP 413 from a vendor) exists mainly to be **non-retryable**. Falling
through to the generic `vendor_error` marked it retryable, so the UI offered "may succeed on
retry" for a request that re-sends byte-identical content — burning the retry budget to collect
the same refusal. Same reasoning as `no_foreground_found`.

Reaching it at all now means `VENDOR_MAX_UPLOAD_BYTES` is too generous for that vendor, which is a
configuration answer rather than a retry: the pipeline sizes the payload before sending, because
"can the vendor read this format" and "will the vendor accept this many bytes" are different
questions. BMP is a format Photoroom reads happily, and a 130 MB uncompressed BMP was still
refused; the identical pixels as PNG are 41 MB and are accepted.

### `OutputAsset.url` is minted on read

Assets carry a storage `key`, and `url` is signed from it when the record is **read** — not when
the asset was written. `SIGNED_URL_TTL_SECONDS` is 15 minutes and a 400-image job is not, so a URL
frozen at write time was already dead by the time the results page rendered. Every read path
(`GET /jobs/{id}`, `GET /jobs/{id}/images`, `cancel`) re-signs, so a URL is always fresh from the
response that carried it and the TTL stays short. Never cache one past the response it came in.

## Versioning

`pipeline_version` is stamped on every `ImageResult` and `JobStatus`. Bump
`app/models.py::PIPELINE_VERSION` whenever a change alters output pixels, so two assets can be
compared for provenance.

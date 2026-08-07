# API contract

**Frozen.** The source of truth is `backend/app/models.py`; this document is a readable map of it,
not a second definition. `frontend/src/api/types.ts` mirrors it by hand — when the contract
changes, update both plus this doc in the same change.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | Upload a zip + `JobConfig` JSON, get a `JobCreated` |
| `GET` | `/jobs/{job_id}` | Poll for `JobStatus` |
| `GET` | `/objects/{key}` | Memory-mode asset passthrough (S3 mode uses presigned URLs directly) |
| `GET` | `/health` | Redacted config snapshot — no secrets, ever |

`POST /jobs` takes `multipart/form-data`: a `file` field (the zip) and a `config` field (JobConfig
as a JSON string). Invalid config is a `422` with FastAPI's validation error list.

## `JobConfig` — one field group per UI tab

```
JobConfig
├── cutout      CutoutSpec       Tab 1 "Clipping"           — the only AI stage
├── background  BackgroundSpec   Tab 2 "Background Services"
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
├── images[]        one ImageResult per file in the archive
├── cost_usd         running vendor spend for the whole job
├── bundle_url       signed zip of every output, once state is COMPLETED
└── error            only set if the JOB itself failed (bad archive) — not per-image failures
```

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
└── background_uniformity   drives the shadow-preservation gate; reported even when it fails
```

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
| `hard_edged_mask` | Cut out by an engine that returns a boundary, not a coverage field — no soft alpha, so edge decontamination had nothing to correct. Emitted for `gemini` |
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

### Formats

**`SourceFormat` — what can be read (14).**
`png` · `jpeg` · `bmp` · `tiff` · `webp` · `eps` · `psd` · `crw` · `cr2` · `cr3` · `dng` · `nef` · `raw`
(`.jpg`/`.jpeg` both map to `jpeg`; `.tif`/`.tiff` both to `tiff`.)

**`OutputFormat` — what can be written (7).**
`png` · `jpeg` · `tiff` · `webp` · `bmp` · `eps` · `psd`

**`export.match_source`** (default `true`) additionally delivers each image in the format it
arrived as, unioned with `export.formats`. A PNG in gives a PNG out; a TIFF a TIFF. Set it `false`
for one fixed format across a mixed batch.

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
`vendor_unauthorized`, `vendor_out_of_credits`, `no_foreground_found`, `vendor_error`,
`unsupported_file`,
`file_too_large`, `output_too_large`,
`malicious_archive`, `image_decode_failed`, `psd_unavailable`, `internal_error` — plus a
`retryable` flag. The point of
this list existing at all: "the vendor throttled us" must never render the same as "your file is
broken" or "we have a bug."

## Versioning

`pipeline_version` is stamped on every `ImageResult` and `JobStatus`. Bump
`app/models.py::PIPELINE_VERSION` whenever a change alters output pixels, so two assets can be
compared for provenance.

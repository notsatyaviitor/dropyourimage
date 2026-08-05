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
| `alpha_suspect` | Cut-out passed automatic checks but scored low — worth a manual look |
| `psd_fallback_raster` | PSD has no vector clipping path — layers and mask only |
| `cover_cropped` | COVER fit mode removed product pixels to fill the canvas |

### Error taxonomy

Every failure carries a typed `ErrorCode` — `vendor_rate_limited`, `vendor_timeout`,
`vendor_unauthorized`, `vendor_error`, `unsupported_file`, `file_too_large`, `malicious_archive`,
`image_decode_failed`, `psd_unavailable`, `internal_error` — plus a `retryable` flag. The point of
this list existing at all: "the vendor throttled us" must never render the same as "your file is
broken" or "we have a bug."

## Versioning

`pipeline_version` is stamped on every `ImageResult` and `JobStatus`. Bump
`app/models.py::PIPELINE_VERSION` whenever a change alters output pixels, so two assets can be
compared for provenance.

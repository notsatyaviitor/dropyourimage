/**
 * Mirrors backend/app/models.py — the frozen contract. Kept in sync by hand.
 *
 * When the contract changes, update this file and docs/API_CONTRACT.md in the same change; see
 * both CLAUDE.md files for why this pairing is not optional.
 */

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

// 'removebg' stays in the union because EngineId is part of the frozen contract and the backend
// adapter still exists — but it is retired, absent from ENGINE_POOL, and not offered in the UI.
// 'birefnet' is self-hosted rather than a metered API, and is only usable when the server has
// torch and the weights on disk — the UI offers it regardless, and the backend surfaces a typed
// error if it was chosen on a box that cannot run it.
export type EngineId = 'photoroom' | 'removebg' | 'falai' | 'gemini' | 'local' | 'birefnet'
export type EngineStrategy = 'auto' | 'single'
export type FitMode = 'contain' | 'pad' | 'cover'
export type CentringMode = 'bbox' | 'centroid'
export type ShadowMode = 'preserve' | 'remove'
export type OutputFormat = 'png' | 'jpeg' | 'tiff' | 'webp' | 'bmp' | 'eps' | 'psd'

/**
 * Formats the backend can READ. Strictly larger than OutputFormat — camera raw decodes but
 * cannot be written back, so those deliver 16-bit TIFF with a `format_substituted` note.
 */
export type SourceFormat =
  | 'png' | 'jpeg' | 'bmp' | 'tiff' | 'webp' | 'eps' | 'psd'
  | 'crw' | 'cr2' | 'cr3' | 'dng' | 'nef' | 'raw'

/** Extensions accepted by the upload dropzone, mirroring formats.EXTENSION_TO_SOURCE. */
export const ACCEPTED_EXTENSIONS = [
  '.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp', '.eps', '.psd',
  '.crw', '.cr2', '.cr3', '.dng', '.nef', '.raw',
] as const

/**
 * Formats a browser will paint inside an <img>. TIFF, EPS and PSD are NOT among them — Chrome,
 * Firefox and Edge render none of the three, so putting one in an <img> yields a blank card.
 * Mirrors formats.BROWSER_RENDERABLE on the backend.
 */
export const BROWSER_RENDERABLE: OutputFormat[] = ['png', 'jpeg', 'webp', 'bmp']

/** Sources that cannot be written back, so `match_source` substitutes 16-bit TIFF. */
export const RAW_EXTENSIONS = ['.crw', '.cr2', '.cr3', '.dng', '.nef', '.raw'] as const

/**
 * Extensions a browser can paint from a local `File`. The INPUT-side mirror of
 * BROWSER_RENDERABLE, and the reason the before/after row was blank for every PSD and EPS: the
 * user's source file was being handed straight to an `<img>` that cannot decode it.
 *
 * For anything not in this list the backend supplies `source_preview_url` instead.
 */
export const RENDERABLE_SOURCE_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.webp', '.bmp'] as const

export function isBrowserRenderableFile(name: string): boolean {
  const dot = name.lastIndexOf('.')
  if (dot < 0) return false
  const ext = name.slice(dot).toLowerCase()
  return (RENDERABLE_SOURCE_EXTENSIONS as readonly string[]).includes(ext)
}

export type ColorProfile = 'srgb' | 'adobe_rgb'
export type JobState = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
export type ImageState = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export type ErrorCode =
  | 'vendor_rate_limited'
  | 'vendor_error'
  | 'vendor_timeout'
  | 'vendor_unauthorized'
  | 'vendor_out_of_credits'
  | 'vendor_payload_too_large'
  | 'no_foreground_found'
  | 'unsupported_file'
  | 'file_too_large'
  | 'output_too_large'
  | 'malicious_archive'
  | 'image_decode_failed'
  | 'psd_unavailable'
  | 'batch_too_large'
  | 'bulk_requires_worker'
  | 'budget_exceeded'
  | 'job_cancelled'
  | 'internal_error'

/**
 * Non-fatal deviations from the request. Every one means "we did something other than what you
 * literally asked for" — frontend/CLAUDE.md requires all of these be rendered, never swallowed.
 */
export type Note =
  | 'shadow_gate_failed'
  | 'upscale_skipped'
  | 'upscaled'
  | 'engine_fallback_used'
  | 'single_engine_only'
  | 'tiebreak_deterministic'
  | 'tiebreak_vision'
  | 'alpha_suspect'
  | 'subject_located'
  | 'subject_not_found'
  | 'busy_scene'
  | 'scene_largest_object'
  | 'hard_edged_mask'
  | 'vendor_downscaled'
  | 'format_substituted'
  | 'multi_object'
  | 'psd_fallback_raster'
  | 'cover_cropped'

/** Human-readable copy for each note, shown as a badge on the affected image. */
export const NOTE_LABELS: Record<Note, string> = {
  shadow_gate_failed:
    'Background was not uniform enough to preserve the shadow — shadow was removed instead.',
  upscale_skipped: 'Source is smaller than the requested size — padded rather than stretched.',
  upscaled: 'Enlarged with an AI upscaler — pixels beyond the source are synthesised.',
  engine_fallback_used: 'The preferred engine’s cut-out lost; the other candidate was used.',
  single_engine_only: 'Only one engine was available — no comparison was made.',
  tiebreak_deterministic: 'Candidates scored equal; picked by measurement, not a vision call.',
  subject_located:
    'A subject prompt isolated one object before cutting out; only that region was segmented.',
  subject_not_found:
    'The subject prompt matched nothing, so the whole frame was segmented — on a multi-object scene this very likely cut out the wrong object.',
  busy_scene:
    'The background is not a uniform studio backdrop. This pipeline is built for packshots; the cut-out may be of the wrong object. Try a subject prompt.',
  scene_largest_object:
    'Several separate objects were found and only the largest was kept. Largest is not the same as wanted — name the object in the Subject field to choose it properly.',
  tiebreak_vision:
    'Candidates scored equal; a vision model judged which was better. A judgement, not a measurement — and the only step that can differ between runs.',
  multi_object:
    'This scene was split into one PSD layer per object. A model decided what counts as an object, and each was cut out separately — so this cost one segmentation call per layer. Centring, shadow reconstruction and background replacement are all skipped in this mode.',
  format_substituted:
    'The uploaded format could not be handed back. Camera raw is decode-only (no encoder for it exists anywhere), so it becomes 16-bit TIFF; and JPEG, BMP and EPS carry no alpha, so a transparent result becomes PNG rather than silently losing the transparency.',
  hard_edged_mask:
    'Gemini returns the mask as a polygon, so this cut-out has a hard edge with no soft alpha. Edge decontamination had nothing to work on, so expect a halo against saturated background colours. Photoroom preserves the soft edge.',
  alpha_suspect: 'Cut-out passed automatic checks but scored low — worth a manual look.',
  vendor_downscaled:
    'This image was too large to send the segmentation service at full resolution, so the mask was computed on a reduced copy and scaled back up. Only mask precision is affected — the product pixels are the full-resolution original — but edges will be softer than usual.',
  psd_fallback_raster: 'PSD written without a vector clipping path (layers and mask only).',
  cover_cropped: 'Cover fit removed some product pixels to fill the canvas.',
}

// ---------------------------------------------------------------------------
// Job configuration — one field group per tab
// ---------------------------------------------------------------------------

export interface CutoutSpec {
  strategy: EngineStrategy
  /** Split a scene into one PSD layer per object. Costs one segmentation call PER object. */
  multi_object: boolean
  engine?: EngineId | null
  keep_losing_candidate: boolean
  /** Which object to extract, in plain words. Null = segment the whole frame (correct for a packshot). */
  subject_prompt?: string | null
  subject_padding_pct: number
}

export interface BackgroundSpec {
  transparent: boolean
  color?: string | null
  shadow: ShadowMode
  decontaminate_edges: boolean
}

/** One server-side demo image — see `GET /samples`. */
export interface SampleFile {
  name: string
  size_bytes: number
}

/**
 * `GET /samples`. Empty when the server has none configured, which is the signal to hide the
 * option rather than show an error — a demo convenience must degrade to absent.
 */
export interface SampleList {
  files: SampleFile[]
  total_bytes: number
}

export interface SizeSpec {
  width: number
  height: number
  /** Deliver at the source's own pixel dimensions, ignoring width/height. */
  match_source: boolean
  fit: FitMode
  margin_pct: number
  allow_upscale: boolean
}

export interface CentringSpec {
  mode: CentringMode
  include_shadow_in_bounds: boolean
  alpha_threshold: number
}

export interface PsdSpec {
  enabled: boolean
  vector_clipping_path: boolean
  path_name: string
  product_layer_name: string
  shadow_layer_name: string
  background_layer_name: string
  profile: ColorProfile
}

export interface ExportSpec {
  formats: OutputFormat[]
  match_source: boolean
  profile: ColorProfile
  jpeg_quality: number
  webp_quality: number
}

export interface JobConfig {
  cutout: CutoutSpec
  background: BackgroundSpec
  size: SizeSpec
  centring: CentringSpec
  psd: PsdSpec
  export: ExportSpec
  preset_name?: string | null
}

/** Matches every backend default exactly — see JobConfig's field defaults in models.py. */
export const DEFAULT_JOB_CONFIG: JobConfig = {
  cutout: {
    strategy: 'auto',
    multi_object: false,
    engine: null,
    keep_losing_candidate: true,
    subject_prompt: null,
    subject_padding_pct: 6,
  },
  background: { transparent: false, color: '#FFFFFF', shadow: 'preserve', decontaminate_edges: true },
  size: {
    width: 500,
    height: 500,
    match_source: false,
    fit: 'contain',
    margin_pct: 5,
    allow_upscale: false,
  },
  centring: { mode: 'bbox', include_shadow_in_bounds: false, alpha_threshold: 0.05 },
  psd: {
    enabled: false,
    vector_clipping_path: true,
    path_name: 'PATH',
    product_layer_name: 'PROD',
    shadow_layer_name: 'SHADOW',
    background_layer_name: 'BG',
    profile: 'adobe_rgb',
  },
  export: { formats: ['png'], match_source: true, profile: 'srgb', jpeg_quality: 92, webp_quality: 90 },
  preset_name: null,
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

export interface ErrorInfo {
  code: ErrorCode
  message: string
  retryable: boolean
}

export interface EngineCandidate {
  engine: EngineId
  score: number | null
  soft_alpha_ratio: number | null
  rejected_reason: string | null
  latency_ms: number | null
  cost_usd: number | null
  preview_url: string | null
}

export interface OutputAsset {
  format: OutputFormat
  /**
   * Signed URL, minted when this record was READ rather than when the asset was written — a
   * 400-image job runs far longer than the 15-minute TTL, so a URL frozen at write time would be
   * dead before the results page rendered. Valid for the TTL from the response that carried it.
   */
  url: string
  /** Storage key the URL is signed from. Never fetched directly; it exists so the URL can be re-minted. */
  key: string
  width: number
  height: number
  bytes: number
  profile: ColorProfile
}

export interface ImageResult {
  source_name: string
  state: ImageState
  outputs: OutputAsset[]
  notes: Note[]
  error: ErrorInfo | null
  chosen_engine: EngineId | null
  candidates: EngineCandidate[]
  centroid_offset_px: [number, number] | null
  background_uniformity: number | null
  source_size: [number, number] | null
  /**
   * What the subject was taken to be when it was not named in the config. Set alongside the
   * `subject_auto_detected` note; null on the whole-frame path. Show it — "cropped to the bottle"
   * is a guess a human can check, "a subject was detected" is not.
   */
  subject_label: string | null
  /** What arrived. Paired with `format_substituted` it names both halves of the swap. */
  source_format: SourceFormat | null
  /**
   * Small PNG of the file AS UPLOADED, for the "before" panel. Set only when the source is a
   * format no browser paints (EPS, PSD, TIFF, camera raw); null for PNG/JPEG/BMP/WebP, where the
   * client shows the user's own File instead — free, instant and full resolution.
   */
  source_preview_url: string | null
  source_preview_key: string | null
  /**
   * An extra viewable asset the backend added only so the UI has something to show, set when no
   * requested format renders in a browser. Present in `outputs` but NOT a deliverable: exclude it
   * from download links and from the bundle.
   */
  preview_format: OutputFormat | null
  /** Named object layers in the PSD, largest first. Only populated for multi_object jobs. */
  layers: string[]
  cost_usd: number
  duration_ms: number | null
  cache_hit: boolean
  pipeline_version: string
}

export interface JobStatus {
  job_id: string
  state: JobState
  config_hash: string
  pipeline_version: string
  total: number
  completed: number
  failed: number
  created_at: string
  started_at: string | null
  finished_at: string | null
  /** Empty when polled with `include_images=false`. Use `images_total` for the count either way. */
  images: ImageResult[]
  error: ErrorInfo | null
  cost_usd: number
  bundle_url: string | null
  bundle_key: string | null

  // --- bulk ---------------------------------------------------------------
  /** Upload archives accepted for this job. A large selection is split across several. */
  batches: number
  upload_bytes: number
  /** True between the first batch and `POST /jobs/{id}/start`. */
  accepting_uploads: boolean
  /**
   * Times a vendor throttled us. Surfaced so a slow batch reads as "the vendor is rate-limiting"
   * rather than "the pipeline stalled" — see frontend/CLAUDE.md on distinguishing error causes.
   */
  rate_limit_events: number
  /** How many ImageResults exist, independent of how many `images` actually carries. */
  images_total: number
}

export interface ImagePage {
  job_id: string
  offset: number
  limit: number
  total: number
  images: ImageResult[]
}

export interface JobCreated {
  job_id: string
  state: 'queued'
  total: number
  config_hash: string
  created_at: string
  batches: number
  /** True when this call sent `start=false`; finish with `POST /jobs/{id}/start` or it never runs. */
  accepting_uploads: boolean
}

/**
 * `POST /jobs/estimate`. **Every figure is an estimate** — list prices per engine, not invoices,
 * and the real total moves with cache hits and retries. It exists so a 400-image click is an
 * informed one. Never relabel any of it as a price or a quote (frontend/CLAUDE.md).
 */
export interface CostEstimate {
  images: number
  engines: EngineId[]
  cost_per_image_usd: number
  estimated_cost_usd: number
  /** Multi-object bills per object, and the count is unknowable in advance — so this is a floor. */
  per_object_pricing: boolean
  ceiling_usd: number
  exceeds_ceiling: boolean
}

/** Server-published upload limits, from `GET /health`. Sized from these, never from a guess. */
export interface ServerLimits {
  max_batch_images: number
  max_batch_bytes: number
  max_job_images: number
  max_job_upload_bytes: number
  /** Per-image byte cap. Checked in the picker so an over-size file never costs a round trip. */
  max_image_bytes: number
  inline_max_images: number
  /** False when no queue is REACHABLE (not merely configured) — bulk will be refused. */
  bulk_enabled: boolean
  max_job_cost_usd: number
}

export const FALLBACK_LIMITS: ServerLimits = {
  max_batch_images: 200,
  max_batch_bytes: 1_073_741_824,
  max_job_images: 500,
  max_job_upload_bytes: 25_769_803_776,
  max_image_bytes: 209_715_200,
  inline_max_images: 25,
  bulk_enabled: false,
  max_job_cost_usd: 25,
}

/** Not sent over the wire — computed client-side the same way JobStatus.progress_pct is on the backend. */
export function progressPct(status: JobStatus): number {
  if (status.total === 0) return 0
  return Math.round((10 * (status.completed + status.failed) * 100) / status.total) / 10
}

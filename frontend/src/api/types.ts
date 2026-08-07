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
export type EngineId = 'photoroom' | 'removebg' | 'falai' | 'gemini' | 'local'
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

export type ColorProfile = 'srgb' | 'adobe_rgb'
export type JobState = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
export type ImageState = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export type ErrorCode =
  | 'vendor_rate_limited'
  | 'vendor_error'
  | 'vendor_timeout'
  | 'vendor_unauthorized'
  | 'vendor_out_of_credits'
  | 'no_foreground_found'
  | 'unsupported_file'
  | 'file_too_large'
  | 'output_too_large'
  | 'malicious_archive'
  | 'image_decode_failed'
  | 'psd_unavailable'
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
  | 'hard_edged_mask'
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
  tiebreak_vision:
    'Candidates scored equal; a vision model judged which was better. A judgement, not a measurement — and the only step that can differ between runs.',
  multi_object:
    'This scene was split into one PSD layer per object. A model decided what counts as an object, and each was cut out separately — so this cost one segmentation call per layer. Centring, shadow reconstruction and background replacement are all skipped in this mode.',
  format_substituted:
    'The uploaded format could not be handed back. Camera raw is decode-only (no encoder for it exists anywhere), so it becomes 16-bit TIFF; and JPEG, BMP and EPS carry no alpha, so a transparent result becomes PNG rather than silently losing the transparency.',
  hard_edged_mask:
    'Gemini returns the mask as a polygon, so this cut-out has a hard edge with no soft alpha. Edge decontamination had nothing to work on, so expect a halo against saturated background colours. Photoroom preserves the soft edge.',
  alpha_suspect: 'Cut-out passed automatic checks but scored low — worth a manual look.',
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

export interface SizeSpec {
  width: number
  height: number
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
  size: { width: 500, height: 500, fit: 'contain', margin_pct: 5, allow_upscale: false },
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
  url: string
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
  /** What arrived. Paired with `format_substituted` it names both halves of the swap. */
  source_format: SourceFormat | null
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
  images: ImageResult[]
  error: ErrorInfo | null
  cost_usd: number
  bundle_url: string | null
}

export interface JobCreated {
  job_id: string
  state: 'queued'
  total: number
  config_hash: string
  created_at: string
}

/** Not sent over the wire — computed client-side the same way JobStatus.progress_pct is on the backend. */
export function progressPct(status: JobStatus): number {
  if (status.total === 0) return 0
  return Math.round((10 * (status.completed + status.failed) * 100) / status.total) / 10
}

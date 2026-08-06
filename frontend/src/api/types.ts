/**
 * Mirrors backend/app/models.py — the frozen contract. Kept in sync by hand.
 *
 * When the contract changes, update this file and docs/API_CONTRACT.md in the same change; see
 * both CLAUDE.md files for why this pairing is not optional.
 */

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

// 'huggingface' is a normal commercial engine (background removal via HF's Inference Providers,
// routed through fal-ai) — gated only by HUGGINGFACE_API_KEY, same as photoroom/removebg/falai.
// 'gemini_edit' is disabled server-side by default (GEMINI_EDIT_ENABLED=false) — see
// docs/ENGINES.md. Listed here only so `chosen_engine`/`candidates[].engine` type correctly if
// someone deliberately turns it on; the UI adds no dedicated affordance for it.
export type EngineId = 'photoroom' | 'removebg' | 'falai' | 'local' | 'huggingface' | 'gemini_edit'
export type EngineStrategy = 'auto' | 'single'
export type FitMode = 'contain' | 'pad' | 'cover'
export type CentringMode = 'bbox' | 'centroid'
export type ShadowMode = 'preserve' | 'remove'
export type OutputFormat = 'png' | 'jpeg' | 'tiff' | 'webp' | 'psd'
export type ColorProfile = 'srgb' | 'adobe_rgb'
export type JobState = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
export type ImageState = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export type ErrorCode =
  | 'vendor_rate_limited'
  | 'vendor_error'
  | 'vendor_timeout'
  | 'vendor_unauthorized'
  | 'unsupported_file'
  | 'file_too_large'
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
  | 'alpha_suspect'
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
  alpha_suspect: 'Cut-out passed automatic checks but scored low — worth a manual look.',
  psd_fallback_raster: 'PSD written without a vector clipping path (layers and mask only).',
  cover_cropped: 'Cover fit removed some product pixels to fill the canvas.',
}

// ---------------------------------------------------------------------------
// Job configuration — one field group per tab
// ---------------------------------------------------------------------------

export interface CutoutSpec {
  strategy: EngineStrategy
  engine?: EngineId | null
  keep_losing_candidate: boolean
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
  cutout: { strategy: 'auto', engine: null, keep_losing_candidate: true },
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
  export: { formats: ['png'], profile: 'srgb', jpeg_quality: 92, webp_quality: 90 },
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

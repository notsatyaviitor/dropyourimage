import type {
  CostEstimate,
  ImagePage,
  JobConfig,
  JobCreated,
  JobStatus,
  ServerLimits,
} from './types'
import { FALLBACK_LIMITS } from './types'
import type { SampleList } from './types'
import { wrapImagesInZip } from '@/lib/zip'

/**
 * All requests go through /api, proxied to the backend by Vite in dev (vite.config.ts) and by
 * whatever reverse proxy fronts this in a real deployment. Never call a vendor (Photoroom,
 * Gemini, Adobe) directly from here — see frontend/CLAUDE.md. This file is the only place that
 * talks to the network.
 */
const API_BASE = '/api'

export class ApiError extends Error {
  status: number
  detail?: unknown

  constructor(message: string, status: number, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/**
 * Backend asset URLs come in two shapes, and both must work unmodified from the browser:
 *
 * - **Memory mode** (`MemoryStorage`, used until MinIO/Redis are wired up — see backend
 *   docs/SETUP.md): relative paths like `/objects/jobs/<id>/out/x.png`, served by our own API.
 *   These need the `/api` prefix so Vite's dev proxy (or a real reverse proxy) forwards them to
 *   the backend rather than the frontend's own dev server.
 * - **Real deployment** (`S3Storage`): already-absolute presigned `https://...` URLs pointing
 *   straight at S3/MinIO. These must NOT be rewritten — prefixing `/api` onto an absolute URL
 *   would just break it.
 */
export function resolveAssetUrl(url: string): string {
  if (/^https?:\/\//.test(url)) return url
  return `${API_BASE}${url.startsWith('/') ? '' : '/'}${url}`
}

/**
 * Sample images the server can process without an upload.
 *
 * Returns an empty list when the server has none configured, which is how the UI knows to hide the
 * option. Failure is silent for the same reason: a demo convenience must never block the page that
 * real work happens on.
 */
export async function getSamples(): Promise<SampleList> {
  try {
    const r = await fetch(`${API_BASE}/samples`)
    if (!r.ok) return { files: [], total_bytes: 0 }
    return (await r.json()) as SampleList
  } catch {
    return { files: [], total_bytes: 0 }
  }
}

/**
 * Start a job from the server's sample images.
 *
 * No file is sent. The client's PSDs are ~130 MB each, so four of them fetched into a tab and
 * posted back is half a gigabyte moved to demonstrate something the server already has on disk.
 * The server builds the same archive an upload would produce, so the job is processed identically.
 */
export async function createJobFromSamples(
  config: JobConfig,
  names?: string[],
): Promise<JobCreated> {
  const body = new FormData()
  body.set('config', JSON.stringify(config))
  body.set('use_samples', 'true')
  // Repeated field, which is how FastAPI reads a list from a form. Omitted entirely when the whole
  // set is wanted, so the common case sends nothing extra. Names are matched against the server's
  // own listing — they are never treated as paths.
  for (const name of names ?? []) body.append('sample_names', name)
  return post<JobCreated>('/jobs', body)
}

export async function createJob(file: File, config: JobConfig): Promise<JobCreated> {
  const body = new FormData()
  body.set('file', file)
  body.set('config', JSON.stringify(config))

  return post<JobCreated>('/jobs', body)
}

/**
 * Batch size, in **bytes** — not in files.
 *
 * A count is the wrong unit and the client's own test set proves it: 132 PSDs of 130 MB each. At
 * 50 files per batch that is a 6.5 GB archive built in the tab, which does not survive; at the
 * same 50 for 200 KB JPEGs it is 10 MB and wastefully chatty. The count says nothing about the
 * cost, so the cost is what gets measured.
 *
 * 256 MB targets a peak near 500 MB (`wrapImagesInZip` holds the batch plus the zip parts) and
 * lands on 2 PSDs or ~1200 ordinary JPEGs per request — both reasonable.
 */
export const UPLOAD_BATCH_BYTES = 256 * 1024 * 1024

/**
 * Secondary cap, so a batch of thousands of tiny files does not exceed the server's per-archive
 * entry limit while sitting well under the byte budget.
 */
export const UPLOAD_BATCH_SIZE = 50

/**
 * Split a selection into batches that fit both budgets.
 *
 * A single file larger than the whole byte budget still gets its own batch rather than being
 * dropped — the server's `max_image_bytes` is what decides whether it is acceptable, and it can
 * say so with a real reason. Silently discarding it here would be the worst of both.
 */
export function planUploadBatches(
  files: File[],
  byteBudget = UPLOAD_BATCH_BYTES,
  countBudget = UPLOAD_BATCH_SIZE,
): File[][] {
  // A server limit of 0 means "no limit", and callers pass `Math.min(serverLimit, ours)` — which
  // yields 0 and would put every file in its own batch. Our own budget is a browser-memory
  // heuristic and applies regardless of what the server permits, so fall back to it.
  if (byteBudget <= 0) byteBudget = UPLOAD_BATCH_BYTES
  if (countBudget <= 0) countBudget = UPLOAD_BATCH_SIZE

  const batches: File[][] = []
  let current: File[] = []
  let bytes = 0

  for (const file of files) {
    const wouldExceed = current.length > 0 && (bytes + file.size > byteBudget || current.length >= countBudget)
    if (wouldExceed) {
      batches.push(current)
      current = []
      bytes = 0
    }
    current.push(file)
    bytes += file.size
  }

  if (current.length > 0) batches.push(current)
  return batches
}

export interface UploadProgress {
  batch: number
  batches: number
  imagesSent: number
  imagesTotal: number
}

/**
 * Upload a whole selection as one job, split across as many archives as it takes.
 *
 * Batches are sent **sequentially and one at a time**, which is the point: a parallel upload would
 * hold every batch's bytes simultaneously and put the memory problem straight back. Each iteration
 * builds one zip, sends it, and drops its reference before the next begins.
 *
 * The last batch does not start the job — `POST /jobs/{id}/start` does, once every batch has
 * landed. Starting on the final upload instead would race: a batch that failed and is being
 * retried would arrive after processing had begun and be refused with a 409.
 */
export async function createJobInBatches(
  files: File[],
  config: JobConfig,
  options: {
    batchBytes?: number
    batchSize?: number
    onProgress?: (p: UploadProgress) => void
    signal?: AbortSignal
  } = {},
): Promise<JobCreated> {
  const batches = planUploadBatches(files, options.batchBytes, options.batchSize)
  let jobId: string | null = null
  let sent = 0

  for (let i = 0; i < batches.length; i++) {
    if (options.signal?.aborted) throw new ApiError('Upload cancelled.', 0)

    const zip = await wrapImagesInZip(batches[i])

    const body = new FormData()
    body.set('file', zip)
    body.set('config', JSON.stringify(config))
    body.set('start', 'false')
    if (jobId) body.set('job_id', jobId)

    const created = await post<JobCreated>('/jobs', body, options.signal)
    jobId = created.job_id
    sent += batches[i].length

    options.onProgress?.({
      batch: i + 1,
      batches: batches.length,
      imagesSent: sent,
      imagesTotal: files.length,
    })
  }

  return startJob(jobId!)
}

export async function startJob(jobId: string): Promise<JobCreated> {
  return post<JobCreated>(`/jobs/${encodeURIComponent(jobId)}/start`, undefined)
}

export async function cancelJob(jobId: string): Promise<JobStatus> {
  return post<JobStatus>(`/jobs/${encodeURIComponent(jobId)}/cancel`, undefined)
}

export async function estimateJob(config: JobConfig, images: number): Promise<CostEstimate> {
  const body = new FormData()
  body.set('config', JSON.stringify(config))
  body.set('images', String(images))
  return post<CostEstimate>('/jobs/estimate', body)
}

/**
 * Poll a job.
 *
 * `includeImages` defaults to **false**, which is the opposite of the endpoint's own default and
 * deliberate: the per-image records are the bulk of the payload and the progress UI reads none of
 * them. On a 400-image job polled every 1.2s that is megabytes per second of data nothing renders.
 * The results grid fetches what it needs from `getJobImages` once the job is terminal.
 */
export async function getJob(jobId: string, includeImages = false): Promise<JobStatus> {
  const query = includeImages ? '' : '?include_images=false'
  return get<JobStatus>(`/jobs/${encodeURIComponent(jobId)}${query}`)
}

export async function getJobImages(
  jobId: string,
  offset: number,
  limit: number,
): Promise<ImagePage> {
  return get<ImagePage>(
    `/jobs/${encodeURIComponent(jobId)}/images?offset=${offset}&limit=${limit}`,
  )
}

/**
 * The server's own upload limits, so the UI sizes batches and warns about bulk from the real
 * configuration rather than a hardcoded copy that drifts out of step with it.
 *
 * Falls back to conservative defaults rather than throwing: `/health` being unreachable should
 * degrade the upload page, not break it.
 */
export async function getLimits(): Promise<ServerLimits> {
  try {
    const res = await fetch(`${API_BASE}/health`)
    if (!res.ok) return FALLBACK_LIMITS
    const body = await res.json()
    return { ...FALLBACK_LIMITS, ...(body?.config?.limits ?? {}) }
  } catch {
    return FALLBACK_LIMITS
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) {
    const detail = await safeJson(res)
    throw new ApiError(summariseError(res.status, detail), res.status, detail)
  }
  return res.json()
}

async function post<T>(path: string, body?: FormData, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: 'POST', body, signal })
  if (!res.ok) {
    const detail = await safeJson(res)
    throw new ApiError(summariseError(res.status, detail), res.status, detail)
  }
  return res.json()
}

async function safeJson(res: Response): Promise<unknown> {
  try {
    return await res.json()
  } catch {
    return null
  }
}

/**
 * FastAPI's 422 validation errors arrive as a list of {loc, msg, type} objects (see
 * routes.py's ValidationError handling) — this renders the first few in one readable line rather
 * than dumping raw JSON at the user.
 */
function summariseError(status: number, detail: unknown): string {
  if (status === 422 && Array.isArray(detail)) {
    const messages = detail
      .slice(0, 3)
      .map((e) => {
        const loc = Array.isArray(e?.loc) ? e.loc.filter((p: unknown) => p !== 'body').join('.') : ''
        return loc ? `${loc}: ${e?.msg}` : String(e?.msg ?? e)
      })
      .join('; ')
    return messages || 'The request was invalid.'
  }
  if (status === 413) return 'The upload is larger than the server allows.'
  if (status === 404) return 'Job not found.'
  // 503 is the bulk gate: the server has no worker and the batch is too big to run inline. Its
  // detail names the fix (start Redis and an rq worker), so pass it through rather than flattening
  // it into a generic failure — the whole point of the error taxonomy is that causes stay distinct.
  if (status === 503 && typeof detail === 'object' && detail && 'detail' in (detail as object)) {
    return String((detail as Record<string, unknown>).detail)
  }
  if (status === 409) return 'This job has already started and cannot accept more images.'
  if (typeof detail === 'object' && detail && 'detail' in (detail as Record<string, unknown>)) {
    return String((detail as Record<string, unknown>).detail)
  }
  return `Request failed (HTTP ${status}).`
}

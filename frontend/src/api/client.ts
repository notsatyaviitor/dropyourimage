import type { JobConfig, JobCreated, JobStatus } from './types'

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

export async function createJob(file: File, config: JobConfig): Promise<JobCreated> {
  const body = new FormData()
  body.set('file', file)
  body.set('config', JSON.stringify(config))

  const res = await fetch(`${API_BASE}/jobs`, { method: 'POST', body })
  if (!res.ok) {
    const detail = await safeJson(res)
    throw new ApiError(summariseError(res.status, detail), res.status, detail)
  }
  return res.json()
}

export async function getJob(jobId: string): Promise<JobStatus> {
  const res = await fetch(`${API_BASE}/jobs/${encodeURIComponent(jobId)}`)
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
  if (typeof detail === 'object' && detail && 'detail' in (detail as Record<string, unknown>)) {
    return String((detail as Record<string, unknown>).detail)
  }
  return `Request failed (HTTP ${status}).`
}

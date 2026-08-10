import { useCallback, useEffect, useRef, useState } from 'react'
import { getJob } from './client'
import type { JobStatus } from './types'

/**
 * Poll cadence. A small job finishes in seconds and wants a responsive bar; a 400-image job runs
 * for many minutes and does not need 500 requests to tell the user it is 43% done. So the interval
 * starts fast and eases off as the job proves it is long-running.
 *
 * The ceiling matters more than it looks: at 1.2s flat, a twenty-minute batch is a thousand polls,
 * and every one of them wakes React and re-renders the overlay.
 */
const POLL_MIN_MS = 1200
const POLL_MAX_MS = 5000
const POLL_GROWTH = 1.25

const TERMINAL_STATES = new Set(['completed', 'failed', 'cancelled'])

/**
 * Polls GET /jobs/{id} until the job reaches a terminal state.
 *
 * Works unmodified in both backend execution modes (see backend/app/api/routes.py): in memory
 * mode the job is already terminal by the time this first fires, so it polls once and stops; with
 * a real Redis/RQ worker it polls repeatedly while the job actually runs. The frontend does not
 * need to know which mode the backend is in.
 *
 * **The polled response carries no per-image records.** `getJob` defaults to
 * `include_images=false`, because the progress UI reads only the counts and the records are the
 * entire payload — a 400-image job would move megabytes per poll for data nothing renders. The
 * results grid fetches pages of records itself once the job is terminal, and the final poll here
 * requests them so a small job still has everything it needs in one shot.
 */
export function useJobPolling(jobId: string | null) {
  const [status, setStatus] = useState<JobStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  /** Force an immediate re-poll — used after a cancel so the UI does not wait out the interval. */
  const refresh = useCallback(async () => {
    if (!jobId) return
    try {
      setStatus(await getJob(jobId, true))
    } catch {
      /* the polling loop will retry and surface anything persistent */
    }
  }, [jobId])

  useEffect(() => {
    setStatus(null)
    setError(null)
    if (!jobId) return

    let cancelled = false
    let interval = POLL_MIN_MS

    async function tick() {
      try {
        const s = await getJob(jobId!)
        if (cancelled) return

        if (TERMINAL_STATES.has(s.state)) {
          // One full fetch at the end, so a job small enough to render in one page needs no
          // follow-up request. CompleteStep pages the rest for larger ones.
          setStatus(await getJob(jobId!, true))
          return
        }

        setStatus(s)
        interval = Math.min(POLL_MAX_MS, interval * POLL_GROWTH)
        timer.current = setTimeout(tick, interval)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to fetch job status.')
      }
    }

    tick()

    return () => {
      cancelled = true
      if (timer.current) clearTimeout(timer.current)
    }
  }, [jobId])

  return { status, error, refresh }
}

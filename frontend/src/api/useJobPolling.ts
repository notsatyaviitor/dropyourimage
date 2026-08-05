import { useEffect, useRef, useState } from 'react'
import { getJob } from './client'
import type { JobStatus } from './types'

const POLL_INTERVAL_MS = 1200
const TERMINAL_STATES = new Set(['completed', 'failed', 'cancelled'])

/**
 * Polls GET /jobs/{id} until the job reaches a terminal state.
 *
 * Works unmodified in both backend execution modes (see backend/app/api/routes.py): in memory
 * mode the job is already terminal by the time this first fires, so it polls once and stops; with
 * a real Redis/RQ worker it polls repeatedly while the job actually runs. The frontend does not
 * need to know which mode the backend is in.
 */
export function useJobPolling(jobId: string | null) {
  const [status, setStatus] = useState<JobStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    setStatus(null)
    setError(null)
    if (!jobId) return

    let cancelled = false

    async function tick() {
      try {
        const s = await getJob(jobId!)
        if (cancelled) return
        setStatus(s)
        if (!TERMINAL_STATES.has(s.state)) {
          timer.current = setTimeout(tick, POLL_INTERVAL_MS)
        }
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

  return { status, error }
}

/**
 * Processing overlay — the prototype's design, driven by the real job.
 *
 * The prototype animated four stage rows on a 1100ms timer regardless of what the backend was
 * doing, and always finished at 100%. That is a screensaver: it reports progress it cannot know and
 * cannot fail.
 *
 * The API does not stream per-stage progress — `JobStatus` reports completed/failed counts per image
 * (see docs/API_CONTRACT.md). So progress here is the honest quantity: **images finished out of
 * total**, from the polled status. The four stage rows are kept, because they explain what the
 * pipeline does to a stakeholder, but they are labelled as the fixed pipeline order rather than as
 * live per-stage telemetry — captioned so nobody reads them as measured timings.
 *
 * It can also end in failure, which the prototype had no state for.
 */

import type { JobStatus } from '@/api/types'

const STAGES = [
  { name: 'Removing backgrounds', desc: 'AI segmentation & mask generation' },
  { name: 'Reconstructing shadow', desc: 'Backdrop plate & ratio map' },
  { name: 'Resizing & centring', desc: 'Exact canvas, measured placement' },
  { name: 'Compositing & exporting', desc: 'Background fill & file packaging' },
]

const RING_CIRCUMFERENCE = 314.16

export function ProcessingOverlay({ status, jobId }: { status: JobStatus | null; jobId: string | null }) {
  if (!jobId) return null

  const total = status?.total ?? 0
  const done = (status?.completed ?? 0) + (status?.failed ?? 0)
  const pct = total > 0 ? Math.round((done / total) * 100) : 0
  const terminal = status ? ['completed', 'failed', 'cancelled'].includes(status.state) : false

  // With no per-stage telemetry, the only defensible mapping is "how far through the batch are we".
  // Stage rows are marked done proportionally so the panel is informative without inventing timings.
  const stagesDone = total > 0 ? Math.floor((done / total) * STAGES.length) : 0

  const caption = !status
    ? 'Submitting batch…'
    : status.state === 'queued'
      ? 'Queued — waiting for a worker'
      : status.state === 'failed'
        ? (status.error?.message ?? 'The job failed.')
        : terminal
          ? 'All images processed — packaging output files…'
          : `Processing image ${Math.min(done + 1, total)} of ${total}…`

  return (
    <div className={`proc-overlay${jobId ? ' open' : ''}`}>
      <div className="proc-modal">
        <div className="proc-modal-header">
          <div className="proc-live-indicator">
            <span className="proc-live-pulse" />
            Processing engine
          </div>
          <div className="proc-modal-batch">
            Batch · <span>{total || '…'}</span> images
          </div>
        </div>

        <div className="proc-modal-body">
          <div className="proc-ring-col">
            <div className="proc-ring-wrap">
              <svg className="proc-ring-svg" viewBox="0 0 120 120">
                <circle className="proc-ring-track" cx="60" cy="60" r="50" />
                <circle
                  className="proc-ring-fill"
                  cx="60"
                  cy="60"
                  r="50"
                  style={{ strokeDashoffset: RING_CIRCUMFERENCE - (pct / 100) * RING_CIRCUMFERENCE }}
                />
              </svg>
              <div className="proc-ring-center">
                <div className="proc-pct-line">
                  <span>{pct}</span>
                  <span className="proc-pct-sym">%</span>
                </div>
                <div className="proc-pct-sub">
                  {total > 0 ? `${done} of ${total}` : 'complete'}
                </div>
              </div>
            </div>
            {status && status.failed > 0 && (
              <div className="proc-failed-note">
                {status.failed} of {total} failed — details on the results page
              </div>
            )}
          </div>

          <div className="proc-steps-col">
            {STAGES.map((s, i) => {
              const state = i < stagesDone ? 'done' : i === stagesDone && !terminal ? 'running' : ''
              return (
                <div className={`psr${state === 'running' ? ' active-step' : ''}`} key={s.name}>
                  <div className={`psr-icon ${state}`}>
                    <svg viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="2">
                      <circle cx="9" cy="9" r="7" />
                    </svg>
                  </div>
                  <div className="psr-info">
                    <div className="psr-name">{s.name}</div>
                    <div className="psr-desc">{s.desc}</div>
                  </div>
                  <div className={`psr-badge ${state}`}>
                    {state === 'done' ? 'Done' : state === 'running' ? 'Running' : 'Queued'}
                  </div>
                </div>
              )
            })}
            <p className="proc-stage-caption">
              These are the pipeline&rsquo;s fixed stages, shown for context. The API reports progress
              per image rather than per stage, so the percentage above counts finished images — it is
              not a per-stage timing.
            </p>
          </div>
        </div>

        <div className="proc-modal-footer">
          <div className="proc-prog-track">
            <div
              className={`proc-prog-fill${status?.state === 'failed' ? ' failed' : ''}`}
              style={{ width: `${pct}%` }}
            />
          </div>
          <div className="proc-prog-text">{caption}</div>
        </div>
      </div>
    </div>
  )
}

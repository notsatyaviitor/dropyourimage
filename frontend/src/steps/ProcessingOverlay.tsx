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

import type { UploadProgress } from '@/api/client'
import type { JobStatus } from '@/api/types'

const STAGES = [
  { name: 'Removing backgrounds', desc: 'AI segmentation & mask generation' },
  { name: 'Reconstructing shadow', desc: 'Backdrop plate & ratio map' },
  { name: 'Resizing & centring', desc: 'Exact canvas, measured placement' },
  { name: 'Compositing & exporting', desc: 'Background fill & file packaging' },
]

const RING_CIRCUMFERENCE = 314.16

export function ProcessingOverlay({
  status,
  jobId,
  upload,
  uploadTotal = 0,
  usingSamples = false,
  cancelling,
  onCancel,
}: {
  status: JobStatus | null
  jobId: string | null
  upload?: UploadProgress | null
  /** How many files the user picked. Known before the first batch lands, unlike `upload`. */
  uploadTotal?: number
  /** Sample jobs upload nothing — the server already holds the files. */
  usingSamples?: boolean
  cancelling?: boolean
  onCancel?: () => void
}) {
  // Mounting is App's decision (it knows when the whole flow is busy); this renders whenever it
  // is mounted. Gating on `jobId` here was what kept the upload phase invisible.
  const total = status?.total ?? 0
  const done = (status?.completed ?? 0) + (status?.failed ?? 0)

  // Two distinct phases, and the boundary is "does a job exist yet". Keying this off `upload`
  // instead left a window — between the click and the first batch landing — where the panel
  // claimed the pipeline was already segmenting and offered to cancel a job that did not exist.
  const uploading = !jobId
  const sent = upload?.imagesSent ?? 0
  const batchTotal = upload?.imagesTotal ?? uploadTotal
  const pct = uploading
    ? Math.round((sent / Math.max(1, batchTotal)) * 100)
    : total > 0
      ? Math.round((done / total) * 100)
      : 0
  const terminal = status ? ['completed', 'failed', 'cancelled'].includes(status.state) : false
  const throttled = (status?.rate_limit_events ?? 0) > 0

  // With no per-stage telemetry, the only defensible mapping is "how far through the batch are we".
  // Stage rows are marked done proportionally so the panel is informative without inventing timings.
  const stagesDone = total > 0 && !uploading ? Math.floor((done / total) * STAGES.length) : 0

  /**
   * Upload is a real phase of its own, so it gets its own row rather than borrowing stage 1's —
   * and it **stays on the list once it finishes**, marked Done.
   *
   * It used to be dropped from the array the moment a job id existed, so the row vanished rather
   * than completing. A step that disappears reads as a step that was skipped or failed, and it
   * also made the list jump by one line at exactly the moment the user was watching it.
   */
  const uploadRow = {
    name: usingSamples ? 'Preparing sample files' : 'Uploading your files',
    desc: usingSamples
      ? 'Held on the server — nothing to upload'
      : uploading
        ? upload
          ? `Batch ${upload.batch} of ${upload.batches} · ${sent} of ${batchTotal} sent`
          : 'Packaging the first batch'
        : `${total || batchTotal} file${(total || batchTotal) === 1 ? '' : 's'} received`,
    // Samples are ready the moment the job exists, so that row is never in a running state.
    state: uploading && !usingSamples ? 'running' : 'done',
  }

  const rows: { name: string; desc: string; state: string }[] = [
    uploadRow,
    ...STAGES.map((s, i) => ({
      ...s,
      state: uploading
        ? ''
        : i < stagesDone
          ? 'done'
          : i === stagesDone && !terminal
            ? 'running'
            : '',
    })),
  ]

  const caption = uploading
    ? usingSamples
      ? 'Starting the job from the server’s sample files…'
      : upload
      ? `Uploading batch ${upload.batch} of ${upload.batches} — ${sent} of ${batchTotal} images sent…`
      : `Packaging ${batchTotal || 'your'} image${batchTotal === 1 ? '' : 's'} for upload…`
    : !status
      ? 'Submitting batch…'
      : status.state === 'queued'
        ? 'Queued — waiting for a worker'
        : status.state === 'failed'
          ? (status.error?.message ?? 'The job failed.')
          : status.state === 'cancelled'
            ? 'Cancelled — images already finished are still available'
            : terminal
              ? 'All images processed — packaging output files…'
              : `Processing image ${Math.min(done + 1, total)} of ${total}…`

  return (
    <div className="proc-overlay open">
      <div className="proc-modal">
        <div className="proc-modal-header">
          <div className="proc-live-indicator">
            <span className="proc-live-pulse" />
            Processing engine
          </div>
          <div className="proc-modal-batch">
            Batch · <span>{total || batchTotal || '…'}</span> images
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
                  {uploading
                    ? batchTotal > 0
                      ? `${sent} of ${batchTotal} sent`
                      : 'uploading'
                    : total > 0
                      ? `${done} of ${total}`
                      : 'complete'}
                </div>
              </div>
            </div>
            {status && status.failed > 0 && (
              <div className="proc-failed-note">
                {status.failed} of {total} failed — details on the results page
              </div>
            )}
            {throttled && !terminal && (
              // A batch this size will hit vendor rate limits, and a silent slowdown reads as a
              // stall. Naming the cause is the same principle as the error taxonomy: "the vendor
              // throttled us" must never look like "the pipeline broke".
              <div className="proc-throttle-note">
                A segmentation vendor is rate-limiting us ({status!.rate_limit_events}×). The batch
                has slowed down automatically and is still running.
              </div>
            )}
          </div>

          <div className="proc-steps-col">
            {rows.map(({ name, desc, state }) => (
                <div className={`psr${state === 'running' ? ' active-step' : ''}`} key={name}>
                  <div className={`psr-icon ${state}`}>
                    <svg viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="2">
                      <circle cx="9" cy="9" r="7" />
                    </svg>
                  </div>
                  <div className="psr-info">
                    <div className="psr-name">{name}</div>
                    <div className="psr-desc">{desc}</div>
                  </div>
                  <div className={`psr-badge ${state}`}>
                    {state === 'done' ? 'Done' : state === 'running' ? 'Running' : 'Queued'}
                  </div>
                </div>
            ))}
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
          <div className="proc-prog-row">
            <div className="proc-prog-text">{caption}</div>
            {onCancel && !uploading && !terminal && (
              // A 400-image run costs real money and takes a long time. Before this there was no
              // way to stop one that was clearly going wrong except closing the tab, which stops
              // nothing — the worker keeps going and keeps billing.
              <button
                type="button"
                className="proc-cancel-btn"
                onClick={onCancel}
                disabled={cancelling}
                title="Stop after the images already in flight. Finished images stay downloadable."
              >
                {cancelling ? 'Cancelling…' : 'Cancel job'}
              </button>
            )}
          </div>
          {cancelling && (
            <div className="proc-cancel-note">
              Stopping after the images already in flight — those are paid for, so they are
              finished rather than discarded.
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

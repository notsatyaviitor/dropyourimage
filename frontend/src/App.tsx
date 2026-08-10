/**
 * The order wizard, ported from the design prototype at repo root (poc.html/css/js).
 *
 * The prototype was a clickable mockup: no network calls, four `setTimeout` stages standing in for
 * processing, and "after" images that were unrelated stock photos. This drives the same design from
 * the real API — `POST /jobs` with the configured `JobConfig`, then polling `GET /jobs/{id}` until
 * the job is terminal.
 *
 * There is still **one job per order**, as before: the five stages are one pipeline, not five tools
 * (see frontend/CLAUDE.md). The wizard steps are a submission flow, not five separate jobs.
 */

import { useCallback, useEffect, useState } from 'react'
import { DEFAULT_JOB_CONFIG, FALLBACK_LIMITS, type JobConfig, type ServerLimits } from '@/api/types'
import {
  ApiError,
  UPLOAD_BATCH_BYTES,
  UPLOAD_BATCH_SIZE,
  cancelJob,
  createJobInBatches,
  getLimits,
  type UploadProgress,
} from '@/api/client'
import { useJobPolling } from '@/api/useJobPolling'
import { Sidebar } from '@/shell/Sidebar'
import { WizardHeader } from '@/shell/WizardHeader'
import type { StepIndex } from '@/shell/steps'
import { SpecificationStep } from '@/steps/SpecificationStep'
import { UploadMethodStep } from '@/steps/UploadMethodStep'
import { UploadStep, type PickedFile } from '@/steps/UploadStep'
import { ProcessingOverlay } from '@/steps/ProcessingOverlay'
import { CompleteStep } from '@/steps/CompleteStep'

/**
 * Set true once ADOBE_CLIENT_ID/SECRET/ORG_ID are configured on the backend — see docs/PSD.md.
 * `GET /health` does report `adobe_psd` in its redacted config, so this could be read live; until
 * then the PSD row states plainly that the fallback writer is in use rather than implying Adobe.
 */
const ADOBE_CONFIGURED = false

const TERMINAL = new Set(['completed', 'failed', 'cancelled'])

function App() {
  const [step, setStep] = useState<StepIndex>(1)
  const [maxReached, setMaxReached] = useState<StepIndex>(1)
  const [config, setConfig] = useState<JobConfig>(DEFAULT_JOB_CONFIG)
  const [picked, setPicked] = useState<PickedFile[]>([])
  const [jobId, setJobId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [upload, setUpload] = useState<UploadProgress | null>(null)
  const [limits, setLimits] = useState<ServerLimits>(FALLBACK_LIMITS)
  const [cancelling, setCancelling] = useState(false)

  const { status, error: pollError, refresh } = useJobPolling(jobId)

  // Read the server's real caps once, so the upload page warns from configuration rather than a
  // hardcoded copy of it. Failure is silent by design — getLimits falls back rather than throwing.
  useEffect(() => {
    let live = true
    getLimits().then((l) => live && setLimits(l))
    return () => {
      live = false
    }
  }, [])

  const goTo = useCallback((n: StepIndex) => {
    setStep(n)
    setMaxReached((m) => (n > m ? n : m))
  }, [])

  /** Advance to results only once the job is terminal, so step 4 never renders an empty page. */
  useEffect(() => {
    if (status && TERMINAL.has(status.state)) {
      setStep(4)
      setMaxReached(4)
    }
  }, [status])

  /**
   * Submit the whole selection as one job.
   *
   * Uploaded in batches (see `createJobInBatches`) because the browser, not the server, is the
   * binding constraint: zipping 400 photos in one go holds roughly three copies of the batch in
   * the tab and kills it before anything is sent. The job is still ONE job with one `JobConfig` —
   * the five tabs are one pipeline, not five tools (frontend/CLAUDE.md).
   */
  async function handleStart() {
    if (picked.length === 0) return
    setSubmitting(true)
    setSubmitError(null)
    setUpload(null)
    try {
      const created = await createJobInBatches(
        picked.map((p) => p.file),
        config,
        {
          // Both budgets come from the server where it publishes one, so a deployment that
          // tightens its archive limits tightens the client automatically rather than having the
          // client discover it as a 413 halfway through an upload.
          batchBytes: Math.min(limits.max_batch_bytes, UPLOAD_BATCH_BYTES),
          batchSize: Math.min(limits.max_batch_images, UPLOAD_BATCH_SIZE),
          onProgress: setUpload,
        },
      )
      setJobId(created.job_id)
    } catch (e) {
      setSubmitError(e instanceof ApiError ? e.message : 'Could not start the job.')
    } finally {
      setSubmitting(false)
      setUpload(null)
    }
  }

  /**
   * Ask the worker to stop. The state does not flip here — the backend owns that transition and
   * claiming it early would report a stop that has not happened while images are still billing.
   * A refresh is forced so the user sees movement rather than waiting out the poll interval.
   */
  async function handleCancel() {
    if (!jobId || cancelling) return
    setCancelling(true)
    try {
      await cancelJob(jobId)
      await refresh()
    } catch (e) {
      setSubmitError(e instanceof ApiError ? e.message : 'Could not cancel the job.')
    } finally {
      setCancelling(false)
    }
  }

  function handleNewOrder() {
    // No object URLs to revoke here any more: thumbnails are owned by the components that show
    // them (`useObjectUrl`), so unmounting the list releases them. Holding one per picked file was
    // the thing that made a 400-file selection unsurvivable.
    setPicked([])
    setJobId(null)
    setSubmitError(null)
    setCancelling(false)
    setStep(1)
    setMaxReached(1)
  }

  /** Back to step 3 to re-run: drop the old job so the overlay does not immediately reopen. */
  function handleBackToUpload() {
    setJobId(null)
    setSubmitError(null)
    setCancelling(false)
    setStep(3)
  }

  /**
   * The overlay covers **the whole busy window**, not just the job.
   *
   * It used to mount on `jobId !== null`, which is only true *after* every batch has been
   * uploaded — so the longest, least explicable part of a large order (zipping and sending
   * gigabytes) showed nothing at all but a static "Ready" beside each file. On the client's
   * 130 MB sources that is minutes of apparent nothing.
   */
  const processing = submitting || (jobId !== null && (!status || !TERMINAL.has(status.state)))

  return (
    <div className="app-layout">
      <Sidebar onCreateOrder={handleNewOrder} />

      <div className="main-wrapper">
        <WizardHeader step={step} maxReached={maxReached} onGoTo={goTo} />

        <div className="wizard-content">
          {step === 1 && (
            <SpecificationStep
              config={config}
              adobeConfigured={ADOBE_CONFIGURED}
              onChange={setConfig}
              onNext={() => goTo(2)}
            />
          )}

          {step === 2 && <UploadMethodStep onBack={() => goTo(1)} onNext={() => goTo(3)} />}

          {step === 3 && (
            <UploadStep
              files={picked}
              config={config}
              limits={limits}
              submitting={submitting}
              upload={upload}
              submitError={submitError ?? pollError}
              onFilesChange={setPicked}
              onBack={() => goTo(2)}
              onStart={handleStart}
            />
          )}

          {step === 4 && (
            <CompleteStep
              status={status}
              config={config}
              picked={picked}
              jobId={jobId}
              onBack={handleBackToUpload}
              onNewOrder={handleNewOrder}
            />
          )}
        </div>
      </div>

      {processing && (
        <ProcessingOverlay
          status={status}
          jobId={jobId}
          upload={upload}
          uploadTotal={picked.length}
          cancelling={cancelling}
          onCancel={handleCancel}
        />
      )}
    </div>
  )
}

export default App

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
import { DEFAULT_JOB_CONFIG, type JobConfig } from '@/api/types'
import { ApiError, createJob } from '@/api/client'
import { useJobPolling } from '@/api/useJobPolling'
import { wrapImagesInZip } from '@/lib/zip'
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

  const { status, error: pollError } = useJobPolling(jobId)

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

  async function handleStart() {
    if (picked.length === 0) return
    setSubmitting(true)
    setSubmitError(null)
    try {
      // The contract is frozen to a zip upload; a batch is packaged client-side rather than
      // loosening it. See lib/zip.ts.
      const zip = await wrapImagesInZip(picked.map((p) => p.file))
      const created = await createJob(zip, config)
      setJobId(created.job_id)
    } catch (e) {
      setSubmitError(e instanceof ApiError ? e.message : 'Could not start the job.')
    } finally {
      setSubmitting(false)
    }
  }

  function handleNewOrder() {
    picked.forEach((p) => URL.revokeObjectURL(p.previewUrl))
    setPicked([])
    setJobId(null)
    setSubmitError(null)
    setStep(1)
    setMaxReached(1)
  }

  /** Back to step 3 to re-run: drop the old job so the overlay does not immediately reopen. */
  function handleBackToUpload() {
    setJobId(null)
    setSubmitError(null)
    setStep(3)
  }

  const processing = jobId !== null && (!status || !TERMINAL.has(status.state))

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
              submitting={submitting}
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
              onBack={handleBackToUpload}
              onNewOrder={handleNewOrder}
            />
          )}
        </div>
      </div>

      {processing && <ProcessingOverlay status={status} jobId={jobId} />}
    </div>
  )
}

export default App

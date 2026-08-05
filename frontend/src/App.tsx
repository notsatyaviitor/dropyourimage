import { useCallback, useState } from 'react'
import { DEFAULT_JOB_CONFIG, type BackgroundSpec, type JobConfig, type PsdSpec } from '@/api/types'
import { createJob, ApiError } from '@/api/client'
import { useJobPolling } from '@/api/useJobPolling'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { AlertTriangle } from '@/components/ui/icons'
import { ClippingTab } from '@/tabs/ClippingTab'
import { BackgroundTab } from '@/tabs/BackgroundTab'
import { SizeTab } from '@/tabs/SizeTab'
import { CentringTab } from '@/tabs/CentringTab'
import { PsdTab } from '@/tabs/PsdTab'
import { UploadPanel } from '@/upload/UploadPanel'
import { ResultsGrid } from '@/results/ResultsGrid'

// Set true once ADOBE_CLIENT_ID/SECRET/ORG_ID are configured on the backend — see docs/PSD.md.
// Not read from the API today because there is no unauthenticated way to ask; /health's redacted
// config does carry `adobe_psd`, so this could be wired up live as a follow-up.
const ADOBE_CONFIGURED = false

function App() {
  const [config, setConfig] = useState<JobConfig>(DEFAULT_JOB_CONFIG)
  const [jobId, setJobId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  const { status, error: pollError } = useJobPolling(jobId)

  // background.transparent and 'jpeg' in export.formats conflict per the contract (JobConfig's
  // cross-field validator) — fixed up here rather than left for the backend to reject, so the
  // user sees the consequence immediately instead of a 422 after upload.
  const handleBackgroundChange = useCallback((next: BackgroundSpec) => {
    setConfig((c) => {
      const formats = next.transparent ? c.export.formats.filter((f) => f !== 'jpeg') : c.export.formats
      return { ...c, background: next, export: { ...c.export, formats } }
    })
  }, [])

  // psd.enabled must agree with whether 'psd' is present in export.formats — same reasoning.
  const handlePsdChange = useCallback((next: PsdSpec) => {
    setConfig((c) => {
      const formats = next.enabled
        ? c.export.formats.includes('psd')
          ? c.export.formats
          : [...c.export.formats, 'psd' as const]
        : c.export.formats.filter((f) => f !== 'psd')
      return { ...c, psd: next, export: { ...c.export, formats } }
    })
  }, [])

  async function handleSubmit(file: File) {
    setSubmitting(true)
    setSubmitError(null)
    try {
      const created = await createJob(file, config)
      setJobId(created.job_id)
    } catch (e) {
      setSubmitError(e instanceof ApiError ? e.message : 'Could not start the job.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="min-h-screen bg-page-bg">
      {/* Header background #152339 matches --awb_header_bg_color on the live site exactly. */}
      <header className="bg-navy px-4 py-5 text-white sm:px-8">
        <div className="mx-auto flex max-w-(--container-brand) items-center justify-between">
          <div>
            <h1 className="font-heading text-xl font-bold text-white">DropYourImage</h1>
            <p className="text-xs text-white/70">Image Processing POC — Clipping, Background Services &amp; PSD delivery</p>
          </div>
        </div>
      </header>

      <main className="mx-auto flex max-w-(--container-brand) flex-col gap-6 px-4 py-8 sm:px-8">
        <Tabs defaultValue="clipping">
          <TabsList>
            <TabsTrigger value="clipping">Clipping</TabsTrigger>
            <TabsTrigger value="background">Background Services</TabsTrigger>
            <TabsTrigger value="size">Size</TabsTrigger>
            <TabsTrigger value="centring">Centring</TabsTrigger>
            <TabsTrigger value="psd">PSD</TabsTrigger>
          </TabsList>

          <TabsContent value="clipping">
            <ClippingTab value={config.cutout} onChange={(v) => setConfig((c) => ({ ...c, cutout: v }))} />
          </TabsContent>
          <TabsContent value="background">
            <BackgroundTab value={config.background} onChange={handleBackgroundChange} />
          </TabsContent>
          <TabsContent value="size">
            <SizeTab
              size={config.size}
              exportSpec={config.export}
              transparent={config.background.transparent}
              onSizeChange={(v) => setConfig((c) => ({ ...c, size: v }))}
              onExportChange={(v) => setConfig((c) => ({ ...c, export: v }))}
            />
          </TabsContent>
          <TabsContent value="centring">
            <CentringTab value={config.centring} onChange={(v) => setConfig((c) => ({ ...c, centring: v }))} />
          </TabsContent>
          <TabsContent value="psd">
            <PsdTab value={config.psd} adobeConfigured={ADOBE_CONFIGURED} onChange={handlePsdChange} />
          </TabsContent>
        </Tabs>

        <UploadPanel onSubmit={handleSubmit} submitting={submitting} />

        {submitError && (
          <div className="flex items-start gap-2 rounded-form border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{submitError}</span>
          </div>
        )}

        {pollError && (
          <div className="flex items-start gap-2 rounded-form border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{pollError}</span>
          </div>
        )}

        {status && <ResultsGrid status={status} />}
      </main>
    </div>
  )
}

export default App

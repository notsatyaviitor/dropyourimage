import type { JobStatus } from '@/api/types'
import { progressPct } from '@/api/types'
import { resolveAssetUrl } from '@/api/client'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { Button } from '@/components/ui/button'
import { Download, AlertTriangle } from '@/components/ui/icons'
import { ImageCard } from './ImageCard'

export function ResultsGrid({ status }: { status: JobStatus }) {
  const pct = progressPct(status)
  const isTerminal = status.state === 'completed' || status.state === 'failed'

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-4">
        <div>
          <CardTitle>Results</CardTitle>
          <p className="text-xs text-body/60">
            job {status.job_id.slice(0, 8)} · {status.completed}/{status.total} done
            {status.failed > 0 && `, ${status.failed} failed`} · ${status.cost_usd.toFixed(4)}
          </p>
        </div>
        {status.bundle_url && (
          <Button asChild variant="secondary" size="sm">
            <a href={resolveAssetUrl(status.bundle_url)} download>
              <Download className="h-3.5 w-3.5" /> Download all
            </a>
          </Button>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!isTerminal && <Progress value={pct} />}

        {status.state === 'failed' && status.error && (
          <div className="flex items-start gap-2 rounded-form border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{status.error.message}</span>
          </div>
        )}

        {status.images.length > 0 && (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
            {status.images.map((img) => (
              <ImageCard key={img.source_name} image={img} />
            ))}
          </div>
        )}

        {status.images.length === 0 && status.state === 'queued' && (
          <p className="text-sm text-body/60">Waiting to start&hellip;</p>
        )}
      </CardContent>
    </Card>
  )
}

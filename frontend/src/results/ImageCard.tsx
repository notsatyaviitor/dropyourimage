import { useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import type { ImageResult } from '@/api/types'
import { NOTE_LABELS } from '@/api/types'
import { resolveAssetUrl } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { AlertTriangle, Download } from '@/components/ui/icons'
import { formatBytes } from '@/lib/utils'

const STATE_TONE = {
  done: 'success',
  running: 'info',
  pending: 'neutral',
  failed: 'danger',
  skipped: 'warning',
} as const

/** Error codes the UI must never present as a generic failure — see frontend/CLAUDE.md. */
const ERROR_COPY: Record<string, string> = {
  vendor_rate_limited: 'The segmentation service is rate-limiting requests',
  vendor_timeout: 'The segmentation service did not respond in time',
  vendor_unauthorized: 'A vendor API key is missing or was rejected',
  vendor_error: 'The segmentation service returned an error',
  unsupported_file: 'Not a supported image file',
  file_too_large: 'File exceeds the size limit',
  malicious_archive: 'Rejected by the archive safety checks',
  image_decode_failed: 'Could not decode this image',
  psd_unavailable: 'Layered PSD is not available on this server',
  internal_error: 'An internal error occurred',
}

export function ImageCard({ image }: { image: ImageResult }) {
  const primary = image.outputs[0]
  const [open, setOpen] = useState(false)

  return (
    <div className="flex flex-col overflow-hidden rounded-form border border-navy/10 bg-white">
      <Dialog.Root open={open} onOpenChange={setOpen}>
        <Dialog.Trigger asChild>
          <button
            type="button"
            disabled={!primary}
            className="checkerboard flex aspect-square w-full items-center justify-center overflow-hidden disabled:cursor-default"
          >
            {primary ? (
              <img
                src={resolveAssetUrl(primary.url)}
                alt={image.source_name}
                className="h-full w-full object-contain"
              />
            ) : (
              <span className="text-xs text-body/50">No output</span>
            )}
          </button>
        </Dialog.Trigger>
        {primary && (
          <Dialog.Portal>
            <Dialog.Overlay className="fixed inset-0 bg-black/60" />
            <Dialog.Content className="fixed left-1/2 top-1/2 max-h-[90vh] max-w-[90vw] -translate-x-1/2 -translate-y-1/2 overflow-auto rounded-form bg-white p-2 shadow-xl">
              <Dialog.Title className="sr-only">{image.source_name} — pixel-peep view</Dialog.Title>
              <div className="checkerboard">
                <img
                  src={resolveAssetUrl(primary.url)}
                  alt={image.source_name}
                  className="max-h-[85vh] max-w-full"
                  style={{ imageRendering: 'pixelated' }}
                />
              </div>
            </Dialog.Content>
          </Dialog.Portal>
        )}
      </Dialog.Root>

      <div className="flex flex-col gap-2 p-3">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-sm font-medium text-navy" title={image.source_name}>
            {image.source_name}
          </span>
          <Badge tone={STATE_TONE[image.state]}>{image.state}</Badge>
        </div>

        {image.outputs.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {image.outputs.map((o) => (
              <a
                key={o.format}
                href={resolveAssetUrl(o.url)}
                download
                title={`Download ${o.format.toUpperCase()} (${formatBytes(o.bytes)}) — a browser cannot preview a PSD's layers; open it in Photoshop, GIMP, or Affinity Photo`}
                className="inline-flex items-center gap-1 rounded border border-navy/15 px-2 py-1 text-xs text-navy transition-colors hover:bg-navy/5"
              >
                <Download className="h-3 w-3" />
                {o.format.toUpperCase()}
                <span className="text-body/50">{formatBytes(o.bytes)}</span>
              </a>
            ))}
          </div>
        )}

        {image.error && (
          <div className="flex items-start gap-1.5 rounded bg-red-50 px-2 py-1.5 text-xs text-red-700">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              {ERROR_COPY[image.error.code] ?? image.error.code}
              {image.error.retryable && ' — may succeed on retry'}
            </span>
          </div>
        )}

        {image.notes.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {image.notes.map((n) => (
              <Badge key={n} tone="warning" title={NOTE_LABELS[n]}>
                {n.replace(/_/g, ' ')}
              </Badge>
            ))}
          </div>
        )}

        {image.chosen_engine && (
          <div className="text-xs text-body/70">
            Engine: <span className="font-medium text-navy">{image.chosen_engine}</span>
            {image.candidates.length > 1 && (
              <CandidateDetails engine={image.chosen_engine} candidates={image.candidates} />
            )}
          </div>
        )}

        {image.centroid_offset_px && (
          <div className="text-xs text-body/60">
            centroid offset: {image.centroid_offset_px[0].toFixed(1)}px,{' '}
            {image.centroid_offset_px[1].toFixed(1)}px
          </div>
        )}
      </div>
    </div>
  )
}

function CandidateDetails({
  engine,
  candidates,
}: {
  engine: string
  candidates: ImageResult['candidates']
}) {
  return (
    <details className="mt-1">
      <summary className="cursor-pointer text-navy/60 hover:text-navy">
        compare {candidates.length} candidates
      </summary>
      <ul className="mt-1 flex flex-col gap-1">
        {candidates.map((c) => (
          <li key={c.engine} className="flex items-center justify-between gap-2">
            <span className={c.engine === engine ? 'font-medium text-navy' : 'text-body/70'}>
              {c.engine}
              {c.engine === engine && ' (chosen)'}
            </span>
            <span className="text-body/60">
              {c.rejected_reason
                ? `rejected: ${c.rejected_reason}`
                : c.score != null
                  ? `score ${c.score.toFixed(2)}`
                  : '—'}
              {c.cost_usd != null && ` · $${c.cost_usd.toFixed(4)}`}
            </span>
          </li>
        ))}
      </ul>
    </details>
  )
}

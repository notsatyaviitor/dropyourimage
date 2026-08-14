/**
 * Step 3 — pick files and start the real job.
 *
 * Differences from the design prototype, all in the same direction:
 *
 * - It starts **empty**. The prototype pre-loaded four Unsplash stock photos as if they were the
 *   user's files, and paired each with an unrelated stock photo as its "after". Nothing was
 *   processed, so a stakeholder was shown a result the pipeline never produced.
 * - "Start Upload" performs the actual `POST /jobs` instead of a 1.6s `setTimeout` status flip.
 * - **The Details panel shows no monetary figures at all.** It began by dropping the prototype's
 *   invented `€ 9.65` / VAT / total, keeping a labelled per-image API cost in their place. That
 *   went too on client feedback: our vendor cost read as a price to the customer, and a number on
 *   screen becomes a quote whether or not the caption says otherwise — including the paragraph
 *   that existed to explain it wasn't one.
 *
 *   `POST /jobs/estimate` is still called, because two of the things it returns are not pricing:
 *   whether a batch is large enough to need explicit confirmation, and whether it will hit the
 *   server's per-job ceiling and stop partway. See `BatchPanel`.
 *
 * ## Surviving a 400-file selection
 *
 * Two things here scale with the file count and had to stop doing so. Both are marked at their
 * site: thumbnails are created only for rows on screen (`useObjectUrl`), and the list is paged
 * rather than rendered whole. Between them, picking 400 files costs the same as picking 50.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { UPLOAD_BATCH_BYTES, UPLOAD_BATCH_SIZE, estimateJob, planUploadBatches } from '@/api/client'
import type { UploadProgress } from '@/api/client'
import type { CostEstimate, JobConfig, SampleFile, SampleList, ServerLimits } from '@/api/types'
import { ACCEPTED_EXTENSIONS, isBrowserRenderableFile } from '@/api/types'
import { useObjectUrl } from '@/lib/objectUrl'
import { formatBytes } from '@/lib/utils'

export interface PickedFile {
  id: string
  file: File
}

/** Rows rendered at once. See `useObjectUrl` for why an unbounded list is not an option. */
const PAGE_SIZE = 50

export function UploadStep({
  files,
  config,
  limits,
  samples,
  submitting,
  onDismissSample,
  upload,
  submitError,
  onFilesChange,
  onBack,
  onStart,
}: {
  files: PickedFile[]
  config: JobConfig
  limits: ServerLimits
  samples: SampleList
  submitting: boolean
  onDismissSample: (name: string) => void
  upload: UploadProgress | null
  submitError: string | null
  onFilesChange: (next: PickedFile[]) => void
  onBack: () => void
  onStart: () => void
}) {
  const [dragOver, setDragOver] = useState(false)
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(0)
  const [estimate, setEstimate] = useState<CostEstimate | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const addFiles = useCallback(
    (incoming: File[]) => {
      const named = new Set(ACCEPTED_EXTENSIONS.map((e) => e.toLowerCase()))
      const images = incoming.filter(
        (f) =>
          f.type.startsWith('image/') ||
          named.has(`.${f.name.split('.').pop()?.toLowerCase() ?? ''}`),
      )
      if (images.length === 0) return
      onFilesChange([
        ...files,
        ...images.map((file) => ({
          id: `${file.name}-${file.size}-${file.lastModified}-${files.length}`,
          file,
        })),
      ])
    },
    [files, onFilesChange],
  )

  const removeFile = (id: string) => onFilesChange(files.filter((f) => f.id !== id))

  const visible = useMemo(
    () =>
      query
        ? files.filter((f) => f.file.name.toLowerCase().includes(query.toLowerCase()))
        : files,
    [files, query],
  )
  const pages = Math.max(1, Math.ceil(visible.length / PAGE_SIZE))
  const current = Math.min(page, pages - 1)
  const window_ = visible.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE)
  const totalBytes = useMemo(() => files.reduce((n, f) => n + f.file.size, 0), [files])
  const batches = useMemo(
    () =>
      planUploadBatches(
        files.map((f) => f.file),
        Math.min(limits.max_batch_bytes, UPLOAD_BATCH_BYTES),
        Math.min(limits.max_batch_images, UPLOAD_BATCH_SIZE),
      ),
    [files, limits],
  )
  // 0 means the server enforces no per-image byte limit (the default), so there is nothing to
  // warn about — and warning anyway would be worse than useless, since it names a limit that does
  // not exist and tells the user to flatten files that would have processed fine.
  const oversized = useMemo(
    () =>
      limits.max_image_bytes > 0
        ? files.filter((f) => f.file.size > limits.max_image_bytes)
        : [],
    [files, limits],
  )

  useEffect(() => setPage(0), [query, files.length])

  // A big selection needs an explicit confirmation, so re-picking invalidates one already given.
  useEffect(() => setConfirmed(false), [files.length])

  // The estimate is server-side because engine selection is: which engines a config resolves to
  // depends on which keys are configured, which the browser must never know.
  useEffect(() => {
    if (files.length === 0) {
      setEstimate(null)
      return
    }
    let live = true
    estimateJob(config, files.length)
      .then((e) => live && setEstimate(e))
      .catch(() => live && setEstimate(null))
    return () => {
      live = false
    }
  }, [files.length, config])

  /**
   * Per-row upload state.
   *
   * Every row said "Ready" for the entire upload, including while its own bytes were on the wire —
   * so on a large order the only sign anything was happening was the button caption. Batches are
   * sent in list order, so a file's position against `imagesSent` is exactly its state: sent,
   * in the batch currently going out, or still waiting.
   */
  const indexById = useMemo(() => new Map(files.map((f, i) => [f.id, i])), [files])
  const rowStatus = (f: PickedFile): RowStatus => {
    if (!submitting) return 'ready'
    const i = indexById.get(f.id) ?? 0
    const sent = upload?.imagesSent ?? 0
    if (i < sent) return 'uploaded'
    // Everything from `sent` up to the end of the batch in flight is being sent right now. Without
    // the batch size to hand, treating the next slice as "uploading" is both simple and honest.
    return i < sent + (upload ? Math.ceil(files.length / Math.max(1, upload.batches)) : files.length)
      ? 'uploading'
      : 'waiting'
  }

  /**
   * Nothing picked and the server offers samples: the order runs on those instead.
   *
   * Derived from the file list rather than a toggle, so there is exactly one rule and no third
   * state — adding a real file makes it an ordinary upload again, and removing them all brings
   * the samples back. Mirrors `usingSamples` in App.tsx, which decides the actual request.
   *
   * The rows below are **not** placeholders. The server holds these files and processes them
   * through the same pipeline as an upload; the browser simply never carries the bytes, which for
   * four ~130 MB PSDs is the difference between a working demo and a hung tab.
   */
  const usingSamples = files.length === 0 && samples.files.length > 0

  /** How many files this order will actually process — picked files, or the samples standing in. */
  const queued = usingSamples ? samples.files.length : files.length

  const overJobCap = limits.max_job_images > 0 && files.length > limits.max_job_images
  const overJobBytes = limits.max_job_upload_bytes > 0 && totalBytes > limits.max_job_upload_bytes
  const needsWorker = !limits.bulk_enabled && files.length > limits.inline_max_images
  const needsConfirm = !!estimate && estimate.estimated_cost_usd > 0 && files.length > 25
  // Over-size files are a warning, not a block: the rest of the order is still processable and
  // the server reports each skipped file by name with its real reason.
  const blocked = overJobCap || overJobBytes || needsWorker || (needsConfirm && !confirmed)

  return (
    <div className="wizard-page active">
      <div className="page-title-row">
        <div className="ptrow-left">
          <button type="button" className="btn-back" onClick={onBack}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <polyline points="10,3 5,8 10,13" />
            </svg>
            Back
          </button>
          <h1 className="page-title">Upload your images</h1>
        </div>
        <button
          type="button"
          className="btn-next"
          onClick={onStart}
          disabled={(files.length === 0 && !usingSamples) || submitting || blocked}
          title={
            files.length === 0 && !usingSamples ? 'Add at least one file first' : undefined
          }
        >
          {submitting
            ? upload
              ? `Uploading ${upload.imagesSent}/${upload.imagesTotal}…`
              : 'Starting…'
            : /* Counts the samples when they are what will run. Reading `files.length` alone gave
                 "Process  images" — no number, and a double space — for a sample order, because
                 nothing is picked in that case. */
              `Process ${queued} file${queued === 1 ? '' : 's'}`}
        </button>
      </div>

      <div className="upload-layout">
        <div>
          <div
            className={`upload-dropzone${dragOver ? ' drag-over' : ''}`}
            onClick={() => inputRef.current?.click()}
            onDragOver={(e) => {
              e.preventDefault()
              setDragOver(true)
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault()
              setDragOver(false)
              addFiles(Array.from(e.dataTransfer.files))
            }}
          >
            <svg className="dz-cloud-icon" width="58" height="58" viewBox="0 0 58 58" fill="none">
              <circle cx="29" cy="29" r="28" fill="#1C2B3C" />
              <path d="M29 38V22M22 29l7-7 7 7" stroke="white" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <div className="dz-title">Drag and drop your files here</div>
            <div className="dz-sub">Or click within this area to select your photos</div>
            <div className="dz-formats">
              PNG · JPG · BMP · TIFF · EPS · PSD · and camera raw (CR2, CR3, NEF, CRW, DNG, RAW)
            </div>
            <div className="dz-bulk">Up to {limits.max_job_images} images per order</div>
            <input
              ref={inputRef}
              type="file"
              // `image/*` alone would hide every raw file in the picker: the OS reports CR2/CR3/
              // NEF/CRW as octet-stream or video, and EPS as application/postscript. Listing the
              // extensions explicitly alongside it is what actually makes them selectable.
              accept={`image/*,${ACCEPTED_EXTENSIONS.join(',')}`}
              multiple
              style={{ display: 'none' }}
              onChange={(e) => {
                addFiles(Array.from(e.target.files ?? []))
                e.target.value = ''
              }}
            />
          </div>

          {overJobCap && (
            <div className="upload-error">
              {files.length} images is over this server&rsquo;s limit of {limits.max_job_images} per
              order. Remove some, or split them into separate orders.
            </div>
          )}

          {oversized.length > 0 && (
            // Caught here rather than after upload. These files WILL be rejected server-side, and
            // learning that from a per-image result after sending gigabytes is the worst possible
            // moment — especially for PSD, where the format is supported and only the size is not.
            <div className="upload-error">
              {oversized.length} file{oversized.length === 1 ? '' : 's'} exceed the{' '}
              {formatBytes(limits.max_image_bytes)} per-image limit and would be skipped:{' '}
              {oversized.slice(0, 3).map((f) => f.file.name).join(', ')}
              {oversized.length > 3 && ` and ${oversized.length - 3} more`}. Flatten them, or raise{' '}
              <code>MAX_IMAGE_BYTES</code> on the server.
            </div>
          )}

          {limits.max_job_upload_bytes > 0 && totalBytes > limits.max_job_upload_bytes && (
            <div className="upload-error">
              This order is {formatBytes(totalBytes)}, over the{' '}
              {formatBytes(limits.max_job_upload_bytes)} total-upload limit. Split it into separate
              orders.
            </div>
          )}

          {needsWorker && (
            // Not a silent failure at submit time: this server runs jobs inside the request
            // handler, and hundreds of images would time out with a half-finished job behind it.
            <div className="upload-error">
              This server has no background worker configured, so it can process at most{' '}
              {limits.inline_max_images} images at a time. Start Redis and an <code>rq worker</code>{' '}
              (see docs/SETUP.md), or upload fewer images.
            </div>
          )}

          <div className="upload-files-box">
            <div className="upload-files-head">
              <span className="upload-files-title">
                {usingSamples ? 'Sample files' : 'Your files'}
                {(usingSamples ? samples.files.length : files.length) > 0 &&
                  ` · ${usingSamples ? samples.files.length : files.length}`}
              </span>
              <input
                type="text"
                className="search-input"
                placeholder="To search…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>

            {usingSamples ? (
              <>
                <p className="upload-sample-note">
                  Ready to process without uploading — these live on the server. Remove any you do
                  not want, or add your own files to replace them entirely.
                </p>
                <div className="file-list">
                  {samples.files.map((s) => (
                    <SampleRow
                      key={s.name}
                      sample={s}
                      disabled={submitting}
                      onRemove={() => onDismissSample(s.name)}
                    />
                  ))}
                </div>
              </>
            ) : files.length === 0 ? (
              <div className="upload-empty">No files added yet</div>
            ) : visible.length === 0 ? (
              <div className="upload-empty">No file matches &ldquo;{query}&rdquo;</div>
            ) : (
              <>
                <div className="file-list">
                  {window_.map((f) => (
                    <FileRow
                      key={f.id}
                      file={f}
                      status={rowStatus(f)}
                      disabled={submitting}
                      onRemove={() => removeFile(f.id)}
                    />
                  ))}
                </div>

                {pages > 1 && (
                  <div className="file-pager">
                    <button
                      type="button"
                      onClick={() => setPage((p) => Math.max(0, p - 1))}
                      disabled={current === 0}
                    >
                      Previous
                    </button>
                    <span>
                      {current * PAGE_SIZE + 1}&ndash;
                      {Math.min((current + 1) * PAGE_SIZE, visible.length)} of {visible.length}
                    </span>
                    <button
                      type="button"
                      onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
                      disabled={current >= pages - 1}
                    >
                      Next
                    </button>
                  </div>
                )}
              </>
            )}
          </div>

          {submitError && <div className="upload-error">{submitError}</div>}
        </div>

        <div className="details-panel">
          <div className="details-title">Details</div>
          <Detail k="Specification" v="POC Configuration" />
          <Detail
            k="Images"
            v={usingSamples ? String(samples.files.length) : files.length > 0 ? String(files.length) : '—'}
          />
          {/* "Total upload" would read 520 MB for samples the browser never sends. Naming the
              source instead is both true and the more useful fact on this panel. */}
          <Detail
            k={usingSamples ? 'Source' : 'Total upload'}
            v={
              usingSamples
                ? `server samples · ${formatBytes(samples.total_bytes)}`
                : files.length > 0
                  ? formatBytes(totalBytes)
                  : '—'
            }
          />
          {batches.length > 1 && (
            // Shown because with large sources it is not derivable from the file count: a 130 MB
            // PSD makes a batch on its own, so "132 files" can mean 66 uploads.
            <Detail
              k="Upload batches"
              v={`${batches.length} × up to ${formatBytes(UPLOAD_BATCH_BYTES)}`}
            />
          )}
          <div className="detail-divider" />
          <Detail k="Background removal" v={config.cutout.strategy === 'auto' ? 'Auto (dual engine)' : (config.cutout.engine ?? '—')} />
          <Detail k="Subject" v={config.cutout.subject_prompt ?? 'whole frame'} />
          <Detail
            k="Background"
            v={config.background.transparent ? 'transparent' : (config.background.color ?? '—')}
          />
          <Detail k="Shadow" v={config.background.shadow} />
          <Detail
            k="Output size"
            v={
              config.size.match_source
                ? 'original resolution of each image'
                : `${config.size.width} × ${config.size.height} px`
            }
          />
          <Detail k="Centring" v={config.centring.mode === 'bbox' ? 'bounding box' : 'centre of mass'} />
          <Detail k="Formats" v={config.export.formats.map((f) => f.toUpperCase()).join(', ')} />
          <Detail k="Profile" v={config.export.profile === 'srgb' ? 'sRGB' : 'Adobe RGB'} />
          <div className="detail-divider" />
          <Detail k="Upload method" v="Manual" />

          {/*
            No monetary figures on this page. Client feedback: our per-image API cost read as a
            price, and a number on the screen the customer sees becomes a quote whether or not it
            is labelled one — including the paragraph that used to explain it wasn't.

            The estimate is still fetched, because it carries two things that are not pricing: the
            batch-size confirmation, and whether the job will hit the server's per-job ceiling and
            stop partway. Both are operational warnings and both stay.
          */}
          {estimate && estimate.estimated_cost_usd > 0 && (
            <BatchPanel
              estimate={estimate}
              needsConfirm={needsConfirm}
              confirmed={confirmed}
              onConfirm={setConfirmed}
              images={files.length}
            />
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * One row of the file list.
 *
 * Its own component specifically so `useObjectUrl` is scoped to it — the thumbnail exists only
 * while this row is mounted, which is what keeps a 400-file selection from pinning 400 decoded
 * bitmaps. `loading="lazy"` is belt and braces for the rows within a page that are scrolled past.
 */
type RowStatus = 'ready' | 'waiting' | 'uploading' | 'uploaded'

const ROW_LABEL: Record<RowStatus, string> = {
  ready: 'Ready',
  waiting: 'Queued',
  uploading: 'Uploading',
  uploaded: 'Uploaded',
}

/**
 * A server-side sample, rendered to match `FileRow` exactly.
 *
 * Deliberately the same markup — `.file-item`, thumb, info, status, remove — because the two
 * lists sit in the same box and any difference reads as a rendering bug. An earlier version
 * hand-rolled its own div classes and stacked vertically while real files laid out in a row.
 *
 * No object URL and no thumbnail: the browser does not have these bytes, which is the whole
 * point. The format badge is what a PSD or EPS would show anyway.
 */
function SampleRow({
  sample,
  disabled,
  onRemove,
}: {
  sample: SampleFile
  disabled: boolean
  onRemove: () => void
}) {
  const ext = sample.name.split('.').pop()?.toUpperCase() ?? '?'

  return (
    <div className="file-item">
      <div className="file-thumb file-thumb-empty" title={`${ext} — held on the server`}>
        <span className="file-thumb-ext">{ext}</span>
      </div>
      <div className="file-info">
        {/* `title` because the name truncates: two exports sharing a long prefix are otherwise
            indistinguishable in the list. Same on FileRow. */}
        <div className="file-name" title={sample.name}>{sample.name}</div>
        <div className="file-size">{formatBytes(sample.size_bytes)}</div>
      </div>
      <div className="file-status file-status-ready">Ready</div>
      <button
        type="button"
        className="file-remove"
        onClick={onRemove}
        title="Remove this sample"
        disabled={disabled}
      >
        ×
      </button>
    </div>
  )
}

function FileRow({
  file,
  status,
  disabled,
  onRemove,
}: {
  file: PickedFile
  status: RowStatus
  disabled: boolean
  onRemove: () => void
}) {
  // Nothing has been uploaded yet, so there is no backend thumbnail to fall back on here — but a
  // PSD or EPS in an `<img>` is a blank box either way. Naming the format is more useful than an
  // empty square, and it also avoids allocating an object URL that can never be painted.
  const renderable = isBrowserRenderableFile(file.file.name)
  const url = useObjectUrl(renderable ? file.file : undefined)
  const ext = file.file.name.split('.').pop()?.toUpperCase() ?? '?'

  return (
    <div className="file-item">
      {url ? (
        <img className="file-thumb" src={url} alt="" loading="lazy" />
      ) : (
        <div className="file-thumb file-thumb-empty" title={`${ext} — no in-browser preview`}>
          {!renderable && <span className="file-thumb-ext">{ext}</span>}
        </div>
      )}
      <div className="file-info">
        <div className="file-name" title={file.file.name}>{file.file.name}</div>
        <div className="file-size">{formatBytes(file.file.size)}</div>
      </div>
      <div className={`file-status file-status-${status}`}>
        {status === 'uploading' && <span className="file-status-spinner" />}
        {ROW_LABEL[status]}
      </div>
      <button
        type="button"
        className="file-remove"
        onClick={onRemove}
        title="Remove"
        disabled={disabled}
      >
        ×
      </button>
    </div>
  )
}

/**
 * Batch-size guard rail. **Carries no monetary figures** — see the note at its call site.
 *
 * What survives from the cost panel it replaces is the part that is not pricing: a large batch
 * asks for an explicit acknowledgement before it starts, and a batch big enough to hit the
 * server's per-job ceiling says so, because that job will stop partway and return only what
 * finished. Both were bundled with the price display and would have been lost with it.
 */
function BatchPanel({
  estimate,
  needsConfirm,
  confirmed,
  onConfirm,
  images,
}: {
  estimate: CostEstimate
  needsConfirm: boolean
  confirmed: boolean
  onConfirm: (v: boolean) => void
  images: number
}) {
  if (!needsConfirm && !estimate.exceeds_ceiling && !estimate.per_object_pricing) return null

  return (
    <div className={`cost-panel${estimate.exceeds_ceiling ? ' over' : ''}`}>
      {estimate.per_object_pricing && (
        <p className="cost-note">
          Multi-object layering runs <strong>one segmentation pass per object</strong>, and the
          object count is not known until each scene is analysed — so a nine-object room is nine
          passes and takes proportionally longer.
        </p>
      )}

      {estimate.exceeds_ceiling && (
        <p className="cost-note over">
          This order is large enough to reach the server&rsquo;s per-job limit. If it does, the job
          stops there and returns whatever finished first.
        </p>
      )}

      {needsConfirm && (
        <label className="cost-confirm">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => onConfirm(e.target.checked)}
          />
          <span>
            I understand this will process {images} images and cannot be undone once it starts.
          </span>
        </label>
      )}
    </div>
  )
}

function Detail({ k, v }: { k: string; v: string }) {
  return (
    <div className="detail-row">
      <span className="detail-key">{k}</span>
      <span className="detail-val">{v}</span>
    </div>
  )
}

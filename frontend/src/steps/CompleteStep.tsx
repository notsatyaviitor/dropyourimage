/**
 * Step 4 — results, from the real job.
 *
 * The prototype's version of this page was the least honest part of it: the "after" image was an
 * unrelated stock photo (or, for a user's own upload, the untouched original), the background colour
 * was a CSS `background-color` on the wrapping div rather than in the pixels, every card was stamped
 * `AUTO-PASS`, and "Download all files" was an `alert()`.
 *
 * This version keeps the layout and replaces all of that with what the API actually returned:
 *
 * - "Before" is the file the user picked; "After" is the delivered asset fetched from the backend.
 *   The background colour is in the pixels, so no CSS tinting is needed — and because it is not
 *   faked, a transparent output correctly shows a checkerboard instead.
 * - Per-image state, typed errors, and every `Note` are rendered. Notes that mean "this may be the
 *   wrong object" get a banner rather than a chip, because a chip was demonstrably easy to miss.
 * - The engine that won, the measured centroid offset, and cache/cost are shown, so a reviewer can
 *   explain *why* an output looks the way it does.
 * - Download links point at real signed asset URLs, and the batch download is the real bundle zip.
 */

import { useEffect, useMemo, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import type { ImageResult, JobConfig, JobStatus, Note } from '@/api/types'
import { BROWSER_RENDERABLE, NOTE_LABELS, isBrowserRenderableFile } from '@/api/types'
import { getJobImages, resolveAssetUrl } from '@/api/client'
import { useObjectUrl } from '@/lib/objectUrl'
import { formatBytes } from '@/lib/utils'
import type { PickedFile } from './UploadStep'

/**
 * Result cards rendered at once.
 *
 * Each card carries two `<img>` — the source the user picked and the delivered asset — so 400
 * results is 800 full-resolution decodes if the grid is rendered whole. That is the same
 * arithmetic that forced pagination on the upload list (see `lib/objectUrl.ts`), arriving from
 * the other end of the pipeline.
 *
 * Pages are fetched from `GET /jobs/{id}/images` rather than sliced from the polled status, so
 * the poll itself never has to carry all 400 records either.
 */
const RESULTS_PER_PAGE = 24

/** Error copy — the API's taxonomy exists so these never collapse into one toast. */
const ERROR_COPY: Record<string, string> = {
  vendor_rate_limited: 'The segmentation service is rate-limiting requests',
  vendor_timeout: 'The segmentation service did not respond in time',
  vendor_unauthorized: 'A vendor API key is missing or was rejected',
  vendor_out_of_credits: 'The engine account is out of credits — top it up to continue',
  vendor_payload_too_large: 'The image was too large for the segmentation service to accept',
  // Deliberately does NOT say "try a different subject": this fires most often when no subject was
  // named at all, and copy presuming one sends the reader looking for a setting they never set.
  // The API's `message` carries the specific remedy — name the object, or widen the padding.
  no_foreground_found: 'No subject could be found to cut out',
  vendor_error: 'The segmentation service returned an error',
  unsupported_file: 'Not a supported image file',
  file_too_large: 'File exceeds the size limit',
  output_too_large: 'Requested canvas is too large for this deployment — reduce width or height',
  malicious_archive: 'Rejected by the archive safety checks',
  image_decode_failed: 'Could not decode this image',
  psd_unavailable: 'Layered PSD is not available on this server',
  internal_error: 'An internal error occurred',
}

/** Notes meaning "this asset may be of the wrong thing". Banner, not chip — chips get scrolled past. */
const CRITICAL_NOTES: Partial<Record<Note, string>> = {
  subject_not_found:
    'The subject you named was not found, so the whole frame was cut out — very likely the wrong object.',
  busy_scene:
    'This looks like a scene rather than a packshot. The cut-out may be of the wrong object — name it in the Subject field.',
  scene_largest_object:
    'Several objects were found here and only the largest was kept — this may not be the one you wanted. Name it in the Subject field to pick it properly.',
  alpha_suspect: 'The cut-out scored low on automatic checks. Look closely before using it.',
  hard_edged_mask:
    'Cut out by Gemini, which returns a polygon — this edge is hard, with no soft alpha. Expect a halo against saturated background colours. Re-run with Photoroom for a soft edge.',
}

const OFFSET_WARN_PX = 20

/**
 * The output to put in an `<img>`, or undefined if none can be shown.
 *
 * **Never index `outputs[0]` for display.** No browser renders TIFF, EPS or PSD, so a job whose
 * first output is one of those paints an empty box while the file itself is perfectly good. That
 * bug was fixed once in the result card and immediately recurred in the before/after row, which
 * had its own copy of `outputs[0]` — hence one shared helper rather than two call sites.
 *
 * The backend guarantees at least one viewable entry (see `ImageResult.preview_format`), so
 * undefined here means the image genuinely produced nothing.
 */
function viewableOutput(result: ImageResult) {
  return result.outputs.find((o) => BROWSER_RENDERABLE.includes(o.format))
}

/** What the user actually asked for — the preview is ours, not theirs, so never offer it. */
function deliverablesOf(result: ImageResult) {
  return result.outputs.filter((o) => o.format !== result.preview_format)
}

export function CompleteStep({
  status,
  config,
  picked,
  jobId,
  onBack,
  onNewOrder,
}: {
  status: JobStatus | null
  config: JobConfig
  picked: PickedFile[]
  jobId: string | null
  onBack: () => void
  onNewOrder: () => void
}) {
  const [zoom, setZoom] = useState<{ url: string; caption: string } | null>(null)
  const [page, setPage] = useState(0)
  const [fetched, setFetched] = useState<ImageResult[] | null>(null)

  const total = status?.images_total ?? status?.images.length ?? 0
  const pages = Math.max(1, Math.ceil(total / RESULTS_PER_PAGE))

  /**
   * The records for the current page.
   *
   * The final poll fetches the whole record, which is everything a job small enough to fit on one
   * page needs — so the common case costs no extra request. Anything larger pages from the API.
   */
  useEffect(() => {
    if (!jobId || total <= RESULTS_PER_PAGE) {
      setFetched(null)
      return
    }
    let live = true
    getJobImages(jobId, page * RESULTS_PER_PAGE, RESULTS_PER_PAGE)
      .then((p) => live && setFetched(p.images))
      .catch(() => live && setFetched(null))
    return () => {
      live = false
    }
  }, [jobId, page, total])

  const allImages = status?.images ?? []
  const images = fetched ?? allImages
  const done = images.filter((i) => i.state === 'done')
  const failed = images.filter((i) => i.state !== 'done')
  const transparent = config.background.transparent

  // Counts come from the JOB, never from the page — `status.completed`/`failed` are authoritative
  // and a page of 24 says nothing about the other 376.
  const doneTotal = status?.completed ?? done.length
  const failedTotal = status?.failed ?? failed.length

  /** Match a result back to the file the user picked, so "before" is the genuine source. */
  const pickedByName = useMemo(
    () => new Map(picked.map((p) => [p.file.name, p.file])),
    [picked],
  )

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
          <h1 className="page-title">{status?.state === 'failed' ? 'Order failed' : 'Order complete'}</h1>
        </div>
        <button type="button" className="btn-next" onClick={onNewOrder}>
          New order
        </button>
      </div>

      <div className="complete-wrap">
        {status?.state === 'failed' && total === 0 ? (
          <div className="res-error">
            <AlertIcon />
            <span>{status.error?.message ?? 'Nothing in this batch could be processed.'}</span>
          </div>
        ) : (
          <>
            <div className="complete-badge">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="#2c9424" strokeWidth="2.2" strokeLinecap="round">
                <polyline points="2,7 6,11 12,3" />
              </svg>
              {failedTotal === 0 ? 'PROCESSING COMPLETE' : `${doneTotal} OF ${total} SUCCEEDED`}
            </div>

            <h2 className="complete-title">
              {doneTotal > 0 ? 'Your images are ready' : 'No images could be processed'}
            </h2>
            <p className="complete-sub">
              {doneTotal > 0
                ? `Backgrounds removed, ${transparent ? 'transparency preserved' : `background set to ${config.background.color}`}, resized to ${config.size.width} × ${config.size.height} and centred.`
                : 'Every image in this batch failed — see the reasons below.'}
            </p>

            {/* Summary — only quantities the API actually reported. */}
            <div className="proc-summary">
              <SummaryItem label="In batch" val={String(total)} sub="images total" />
              <SummaryItem
                label="Succeeded"
                val={String(doneTotal)}
                sub={failedTotal > 0 ? `${failedTotal} failed` : 'all images'}
                cls={failedTotal > 0 ? '' : 'val-green'}
              />
              <SummaryItem
                label="Output size"
                val={`${config.size.width}×${config.size.height}`}
                sub={config.export.formats.map((f) => f.toUpperCase()).join(' · ')}
                cls="val-text"
              />
              <SummaryItem
                label="Vendor spend"
                val={status ? `$${status.cost_usd.toFixed(4)}` : '—'}
                sub={images.some((i) => i.cache_hit) ? 'some served from cache' : 'this job'}
                cls="val-text"
              />
            </div>

            {status && status.rate_limit_events > 0 && (
              // Vendor throttling is expected at this batch size and is not our bug. Saying so
              // keeps a slow run from being reported as a broken one.
              <div className="res-note-banner">
                A segmentation vendor rate-limited this job {status.rate_limit_events} time
                {status.rate_limit_events === 1 ? '' : 's'}. The batch slowed itself down and
                carried on; no image was lost to it.
              </div>
            )}

            {status?.state === 'cancelled' && (
              <div className="res-note-banner">
                This job was cancelled. Images that had already finished are below and remain
                downloadable.
              </div>
            )}

            <div className="batch-section">
              <div className="batch-section-hdr">
                <div>
                  <div className="batch-title">
                    Processed batch · <span>{total}</span> images
                  </div>
                  <div className="batch-meta">
                    Pipeline <strong>{status?.pipeline_version}</strong> · config{' '}
                    <strong>{status?.config_hash}</strong>
                  </div>
                </div>
                <button type="button" className="btn-rerun" onClick={onBack}>
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.8">
                    <path d="M11.5 7A4.5 4.5 0 1 1 7 2.5" strokeLinecap="round" />
                    <polyline points="11.5,2.5 11.5,7 7,7" />
                  </svg>
                  Process again
                </button>
              </div>

              {images.length === 0 ? (
                <div className="res-empty">No results returned.</div>
              ) : (
                <>
                  <div className="result-grid">
                    {images.map((r, i) => (
                      <ResultCard
                        key={`${r.source_name}-${i}`}
                        result={r}
                        transparent={transparent}
                        onZoom={setZoom}
                      />
                    ))}
                  </div>

                  {pages > 1 && (
                    <Pager
                      page={page}
                      pages={pages}
                      from={page * RESULTS_PER_PAGE + 1}
                      to={Math.min((page + 1) * RESULTS_PER_PAGE, total)}
                      total={total}
                      onChange={setPage}
                    />
                  )}
                </>
              )}
            </div>

            {done.length > 0 && (
              <div className="ba-section">
                <div className="ba-section-title">
                  Before &amp; after — side by side
                  {pages > 1 && <span className="ba-section-note"> (this page)</span>}
                </div>
                <div className="ba-row">
                  {done.map((r, i) => (
                    <BeforeAfterCard
                      key={`${r.source_name}-${i}`}
                      result={r}
                      source={pickedByName.get(r.source_name)}
                      transparent={transparent}
                      onZoom={setZoom}
                    />
                  ))}
                </div>
              </div>
            )}

            <div className="complete-actions">
              {status?.bundle_url ? (
                <a className="btn-download" href={resolveAssetUrl(status.bundle_url)} download>
                  Download all files ({doneTotal})
                </a>
              ) : (
                <button type="button" className="btn-download" disabled title="No successful outputs to bundle">
                  Download all files
                </button>
              )}
              <button type="button" className="btn-new-order" onClick={onNewOrder}>
                Create new order
              </button>
            </div>
          </>
        )}
      </div>

      {/* Radix rather than a hand-rolled overlay: it brings Escape-to-close, focus trapping and
          scroll lock, which a plain click-outside div does not. Pixel-peep is how a reviewer judges
          edge quality, so it needs to be properly operable — see frontend/CLAUDE.md. */}
      <Dialog.Root open={zoom !== null} onOpenChange={(open) => !open && setZoom(null)}>
        <Dialog.Portal>
          <Dialog.Overlay className="zoom-overlay" />
          <Dialog.Content className="zoom-content">
            <Dialog.Title className="zoom-caption">
              {zoom?.caption} — pixels shown unsmoothed
            </Dialog.Title>
            <div className="zoom-box">{zoom && <img src={zoom.url} alt="" />}</div>
            <Dialog.Close className="zoom-close" aria-label="Close">
              ×
            </Dialog.Close>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  )
}

/**
 * One before/after pair.
 *
 * Its own component so `useObjectUrl` scopes the source thumbnail to it: the "before" image is
 * the user's original file, which for a 400-image order is gigabytes of `File` data that would
 * otherwise be pinned and decoded all at once. Only the cards on the current page hold one.
 *
 * `source` is undefined when the results page is reached without the original files in hand — a
 * reload, or a job started in another session. That is said plainly rather than shown as a blank
 * box, which reads as a broken image.
 */
function BeforeAfterCard({
  result,
  source,
  transparent,
  onZoom,
}: {
  result: ImageResult
  source: File | undefined
  transparent: boolean
  onZoom: (z: { url: string; caption: string }) => void
}) {
  // Only make an object URL for a file the browser can actually decode. A PSD or EPS handed to an
  // `<img>` renders nothing — which is exactly why every "before" panel was blank for this
  // client's material — so those use the backend's thumbnail instead.
  const renderableLocally = !!source && isBrowserRenderableFile(source.name)
  const localUrl = useObjectUrl(renderableLocally ? source : undefined)
  const before = localUrl ?? (result.source_preview_url ? resolveAssetUrl(result.source_preview_url) : undefined)
  // Not outputs[0]: that is the delivered format, which for a TIFF/EPS/PSD job renders as an
  // empty box. See viewableOutput.
  const after = viewableOutput(result)

  return (
    <div className="ba-card">
      <div className="ba-card-label-row">
        <div className="ba-label before">Before</div>
        <div className="ba-label after">After</div>
      </div>
      <div className="ba-imgs">
        <div className="ba-img-wrap before">
          {before ? (
            <img className="ba-img" src={before} alt="" loading="lazy" />
          ) : (
            // Distinguish the two reasons rather than showing one blank box for both, the same
            // way the "after" side names "no viewable output".
            <span className="res-empty">
              {source ? `no preview for ${result.source_format?.toUpperCase() ?? 'this format'}` : 'source not retained'}
            </span>
          )}
        </div>
        <div className={`ba-img-wrap after${transparent ? ' checkerboard-bg' : ''}`}>
          {after ? (
            <img
              className="ba-img"
              src={resolveAssetUrl(after.url)}
              alt=""
              loading="lazy"
              onClick={() =>
                onZoom({
                  url: resolveAssetUrl(after.url),
                  caption: `${result.source_name} — ${after.width}×${after.height} ${after.format.toUpperCase()}`,
                })
              }
            />
          ) : (
            // Rendering nothing here is what made this read as a bug rather than a missing file.
            // Say which it is.
            <span className="res-empty">no viewable output</span>
          )}
        </div>
      </div>
      <div className="ba-card-name">{result.source_name}</div>
    </div>
  )
}

function Pager({
  page,
  pages,
  from,
  to,
  total,
  onChange,
}: {
  page: number
  pages: number
  from: number
  to: number
  total: number
  onChange: (p: number) => void
}) {
  return (
    <div className="result-pager">
      <button type="button" onClick={() => onChange(Math.max(0, page - 1))} disabled={page === 0}>
        Previous
      </button>
      <span>
        {from}&ndash;{to} of {total}
      </span>
      <button
        type="button"
        onClick={() => onChange(Math.min(pages - 1, page + 1))}
        disabled={page >= pages - 1}
      >
        Next
      </button>
    </div>
  )
}

function ResultCard({
  result,
  transparent,
  onZoom,
}: {
  result: ImageResult
  transparent: boolean
  onZoom: (z: { url: string; caption: string }) => void
}) {
  const deliverables = deliverablesOf(result)
  const viewable = viewableOutput(result)

  // Dimensions and profile come from a real deliverable when there is one — the preview shares
  // them, but reporting the thing the user asked for is the honest choice.
  const primary = deliverables[0] ?? viewable
  const previewOnly = viewable != null && viewable.format === result.preview_format

  const critical = result.notes.filter((n) => n in CRITICAL_NOTES)
  const other = result.notes.filter((n) => !(n in CRITICAL_NOTES))
  const offset = result.centroid_offset_px
  const offCentre = offset != null && Math.max(Math.abs(offset[0]), Math.abs(offset[1])) > OFFSET_WARN_PX
  const ok = result.state === 'done'

  return (
    <div className="result-card">
      <div className={`result-card-status-badge ${ok ? 'badge-auto' : 'badge-review'}`}>
        {ok ? 'PROCESSED' : result.state.toUpperCase()}
      </div>

      <div className={`result-card-img-wrap${transparent ? ' checkerboard-bg' : ''}`}>
        {viewable ? (
          <img
            className="result-card-img"
            src={resolveAssetUrl(viewable.url)}
            alt={result.source_name}
            onClick={() =>
              onZoom({
                url: resolveAssetUrl(viewable.url),
                caption:
                  `${result.source_name} — ${viewable.width}×${viewable.height} ` +
                  (previewOnly
                    ? `preview of the ${deliverables.map((d) => d.format.toUpperCase()).join('/')}`
                    : viewable.format.toUpperCase()),
              })
            }
          />
        ) : (
          <span className="res-empty">No output</span>
        )}
      </div>
      {previewOnly && (
        <p className="res-preview-note">
          On-screen preview. {deliverables.map((d) => d.format.toUpperCase()).join(' and ')}{' '}
          {deliverables.length > 1 ? 'do' : 'does'} not render in a browser — download to view.
        </p>
      )}

      <div className="result-card-footer">
        <span className="result-card-id" title={result.source_name}>
          {result.source_name}
        </span>
      </div>

      {/*
        Everything below the filename shares one padded, gap-spaced column.
        These blocks were direct children of `.result-card`, which has no padding of its own — so
        the chips, download links and metadata all sat flush against the card's left edge while the
        filename above them was inset by 16px. Every row was individually spaced with its own
        `margin-top`, which is why they also read as one dense paragraph rather than distinct
        groups. One wrapper fixes both: the padding aligns them with the filename, and a flex `gap`
        replaces six competing margins.
      */}
      <div className="result-card-body">
      {result.error && (
        <div className="res-error">
          <AlertIcon />
          <span>
            {ERROR_COPY[result.error.code] ?? result.error.code}
            {result.error.retryable && ' — may succeed on retry'}
            {/*
              The specific reason, which the API has always sent and this used to discard. The
              code's copy is a category ("File exceeds the size limit"); `message` is the fact
              ("This file is 130 MB; the per-image limit is 50 MB"). Showing only the category
              cost a real debugging session on a rejected PSD — the answer was on the screen the
              whole time, one field away.
            */}
            {result.error.message && (
              <span className="res-error-detail">{result.error.message}</span>
            )}
          </span>
        </div>
      )}

      {critical.map((n) => (
        <div className="res-banner" key={n}>
          <AlertIcon />
          <span>{CRITICAL_NOTES[n]}</span>
        </div>
      ))}

      {other.length > 0 && (
        <div className="res-notes">
          {other.map((n) => (
            <span className="res-note-chip" key={n} title={NOTE_LABELS[n]}>
              {n.replace(/_/g, ' ')}
            </span>
          ))}
        </div>
      )}

      {result.outputs.length > 0 && (
        <div className="res-fmt-links">
          {/* `deliverables`, not `outputs` — the preview is not something the user requested, and
              offering it here would put an unasked-for PNG next to the real formats. */}
          {deliverables.map((o) => (
            <a key={o.format} className="res-fmt-link" href={resolveAssetUrl(o.url)} download>
              {o.format.toUpperCase()}
              <span className="res-fmt-bytes">{formatBytes(o.bytes)}</span>
            </a>
          ))}
        </div>
      )}

      {ok && (
        <div className="res-meta">
          {primary && (
            <div className="res-meta-row">
              <strong>{primary.width} × {primary.height}</strong> px · {primary.profile === 'srgb' ? 'sRGB' : 'Adobe RGB'}
            </div>
          )}
          {result.layers.length > 0 && (
            <div className="res-meta-row">
              {/* The whole point of the mode — name the layers a retoucher will open. */}
              {result.layers.length} PSD layers: <strong>{result.layers.join(', ')}</strong>
            </div>
          )}
          {result.source_format && (
            <div className="res-meta-row">
              {/* Both halves of the round trip, so "did I get my format back" is answerable at a
                  glance rather than by reading the note text. */}
              uploaded <strong>{result.source_format.toUpperCase()}</strong>
              {' → '}
              <strong>{result.outputs.map((o) => o.format.toUpperCase()).join(', ') || '—'}</strong>
            </div>
          )}
          {result.chosen_engine && (
            <div className="res-meta-row">
              engine <strong>{result.chosen_engine}</strong>
              {result.cache_hit && ' · from cache'}
              {!result.cache_hit && result.cost_usd > 0 && ` · $${result.cost_usd.toFixed(4)}`}
            </div>
          )}
          {offset && (
            <div className={`res-meta-row${offCentre ? ' res-meta-warn' : ''}`}>
              centroid offset {offset[0].toFixed(1)}px, {offset[1].toFixed(1)}px
              {offCentre && ' — far off centre, mask may cover more than one object'}
            </div>
          )}
          {result.candidates.length > 1 && (
            <details>
              <summary>compare {result.candidates.length} candidates</summary>
              {result.candidates.map((c) => (
                <div key={c.engine}>
                  {c.engine}
                  {c.engine === result.chosen_engine && ' (chosen)'} —{' '}
                  {c.rejected_reason
                    ? `rejected: ${c.rejected_reason}`
                    : c.score != null
                      ? `score ${c.score.toFixed(2)}`
                      : '—'}
                </div>
              ))}
            </details>
          )}
        </div>
      )}
      </div>
    </div>
  )
}

function SummaryItem({ label, val, sub, cls = '' }: { label: string; val: string; sub: string; cls?: string }) {
  return (
    <div className="proc-summary-item">
      <span className="proc-summary-label">{label}</span>
      <span className={`proc-summary-val ${cls}`}>{val}</span>
      <span className="proc-summary-sub">{sub}</span>
    </div>
  )
}

function AlertIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <path d="M8 1.8L14.8 13.5H1.2z" />
      <line x1="8" y1="6" x2="8" y2="9.6" />
      <circle cx="8" cy="11.6" r=".7" fill="currentColor" stroke="none" />
    </svg>
  )
}

/**
 * Step 3 — pick files and start the real job.
 *
 * Differences from the design prototype, all in the same direction:
 *
 * - It starts **empty**. The prototype pre-loaded four Unsplash stock photos as if they were the
 *   user's files, and paired each with an unrelated stock photo as its "after". Nothing was
 *   processed, so a stakeholder was shown a result the pipeline never produced.
 * - "Start Upload" performs the actual `POST /jobs` instead of a 1.6s `setTimeout` status flip.
 * - The Details panel drops the invented `€ 9.65` / VAT / total figures. There is no pricing model
 *   in this POC, and a number on a screen becomes a quote.
 */

import { useCallback, useRef, useState } from 'react'
import type { JobConfig } from '@/api/types'
import { ACCEPTED_EXTENSIONS } from '@/api/types'
import { formatBytes } from '@/lib/utils'

export interface PickedFile {
  id: string
  file: File
  previewUrl: string
}

export function UploadStep({
  files,
  config,
  submitting,
  submitError,
  onFilesChange,
  onBack,
  onStart,
}: {
  files: PickedFile[]
  config: JobConfig
  submitting: boolean
  submitError: string | null
  onFilesChange: (next: PickedFile[]) => void
  onBack: () => void
  onStart: () => void
}) {
  const [dragOver, setDragOver] = useState(false)
  const [query, setQuery] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const addFiles = useCallback(
    (incoming: File[]) => {
      const images = incoming.filter((f) => f.type.startsWith('image/'))
      if (images.length === 0) return
      onFilesChange([
        ...files,
        ...images.map((file) => ({
          id: `${file.name}-${file.size}-${Math.random().toString(36).slice(2)}`,
          file,
          previewUrl: URL.createObjectURL(file),
        })),
      ])
    },
    [files, onFilesChange],
  )

  const removeFile = (id: string) => {
    const gone = files.find((f) => f.id === id)
    if (gone) URL.revokeObjectURL(gone.previewUrl)
    onFilesChange(files.filter((f) => f.id !== id))
  }

  const visible = files.filter((f) => f.file.name.toLowerCase().includes(query.toLowerCase()))
  const totalBytes = files.reduce((n, f) => n + f.file.size, 0)

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
          disabled={files.length === 0 || submitting}
          title={files.length === 0 ? 'Add at least one image first' : undefined}
        >
          {submitting ? 'Starting…' : 'Process images'}
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

          <div className="upload-files-box">
            <div className="upload-files-head">
              <span className="upload-files-title">Your files</span>
              <input
                type="text"
                className="search-input"
                placeholder="To search…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>

            {files.length === 0 ? (
              <div className="upload-empty">No files added yet</div>
            ) : (
              <div className="file-list">
                {visible.map((f) => (
                  <div className="file-item" key={f.id}>
                    <img className="file-thumb" src={f.previewUrl} alt="" />
                    <div className="file-info">
                      <div className="file-name">{f.file.name}</div>
                      <div className="file-size">{formatBytes(f.file.size)}</div>
                    </div>
                    <div className="file-status">Ready</div>
                    <button
                      type="button"
                      className="file-remove"
                      onClick={() => removeFile(f.id)}
                      title="Remove"
                      disabled={submitting}
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {submitError && <div className="upload-error">{submitError}</div>}
        </div>

        <div className="details-panel">
          <div className="details-title">Details</div>
          <Detail k="Specification" v="POC Configuration" />
          <Detail k="Images" v={files.length > 0 ? String(files.length) : '—'} />
          <Detail k="Total upload" v={files.length > 0 ? formatBytes(totalBytes) : '—'} />
          <div className="detail-divider" />
          <Detail k="Clipping" v={config.cutout.strategy === 'auto' ? 'Auto (dual engine)' : (config.cutout.engine ?? '—')} />
          <Detail k="Subject" v={config.cutout.subject_prompt ?? 'whole frame'} />
          <Detail
            k="Background"
            v={config.background.transparent ? 'transparent' : (config.background.color ?? '—')}
          />
          <Detail k="Shadow" v={config.background.shadow} />
          <Detail k="Output size" v={`${config.size.width} × ${config.size.height} px`} />
          <Detail k="Centring" v={config.centring.mode === 'bbox' ? 'bounding box' : 'centre of mass'} />
          <Detail k="Formats" v={config.export.formats.map((f) => f.toUpperCase()).join(', ')} />
          <Detail k="Profile" v={config.export.profile === 'srgb' ? 'sRGB' : 'Adobe RGB'} />
          <div className="detail-divider" />
          <Detail k="Upload method" v="Manual" />
          <p className="details-note">
            No pricing is shown: this POC has no billing model, and a figure on screen becomes a quote.
          </p>
        </div>
      </div>
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

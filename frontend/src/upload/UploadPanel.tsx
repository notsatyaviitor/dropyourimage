import { useCallback, useRef, useState } from 'react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { UploadCloud, XCircle, Loader } from '@/components/ui/icons'
import { wrapImageInZip } from '@/lib/zip'
import { formatBytes } from '@/lib/utils'

// Matches what the backend's zip_reader actually sniffs and accepts as an image (see
// backend/app/ingest/zip_reader.py::_sniff_by_signature) — deliberately excludes GIF and SVG,
// which the backend explicitly rejects as unsupported input, and PSD, which is an output format
// only, never an input.
const SINGLE_IMAGE_EXTENSIONS = /\.(png|jpe?g|webp|tiff?|bmp)$/i

export function UploadPanel({
  onSubmit,
  submitting,
}: {
  onSubmit: (file: File) => void
  submitting: boolean
}) {
  // `file` is what actually gets uploaded (always a zip, once picked); `displayName`/`displaySize`
  // are what the user is shown, which for a single-image pick is the ORIGINAL file, not the
  // synthetic wrapper zip — showing "single-image.zip" instead would be a confusing lie about
  // what the user actually selected.
  const [file, setFile] = useState<File | null>(null)
  const [displayName, setDisplayName] = useState<string>('')
  const [displaySize, setDisplaySize] = useState<number>(0)
  const [wrapping, setWrapping] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  const pick = useCallback(async (f: File | undefined) => {
    if (!f) return

    if (/\.zip$/i.test(f.name)) {
      setFile(f)
      setDisplayName(f.name)
      setDisplaySize(f.size)
      return
    }

    if (!SINGLE_IMAGE_EXTENSIONS.test(f.name)) return

    setWrapping(true)
    try {
      const zipped = await wrapImageInZip(f)
      setFile(zipped)
      setDisplayName(f.name)
      setDisplaySize(f.size)
    } finally {
      setWrapping(false)
    }
  }, [])

  return (
    <Card>
      <CardHeader>
        <CardTitle>Upload</CardTitle>
        <CardDescription>
          A .zip of product photos, or a single image. All five tabs above apply to every image in
          the batch in one run.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div
          onDragOver={(e) => {
            e.preventDefault()
            setDragOver(true)
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => {
            e.preventDefault()
            setDragOver(false)
            void pick(e.dataTransfer.files[0])
          }}
          onClick={() => inputRef.current?.click()}
          className={
            'flex cursor-pointer flex-col items-center justify-center gap-2 rounded-form border-2 border-dashed px-6 py-10 text-center transition-colors ' +
            (dragOver ? 'border-accent bg-accent/5' : 'border-navy/20 hover:border-navy/40')
          }
        >
          <UploadCloud className="h-8 w-8 text-navy/50" />
          {wrapping ? (
            <p className="flex items-center gap-2 text-sm text-body/70">
              <Loader className="h-4 w-4" /> Preparing image&hellip;
            </p>
          ) : file ? (
            <div className="flex items-center gap-2 text-sm text-navy">
              <span className="font-medium">{displayName}</span>
              <span className="text-body/60">{formatBytes(displaySize)}</span>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  setFile(null)
                  setDisplayName('')
                  setDisplaySize(0)
                }}
                aria-label="Remove file"
                className="text-navy/40 hover:text-red-600"
              >
                <XCircle className="h-4 w-4" />
              </button>
            </div>
          ) : (
            <p className="text-sm text-body/70">
              Drag a .zip or a single image here, or click to choose one
            </p>
          )}
          <input
            ref={inputRef}
            type="file"
            accept=".zip,application/zip,.png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp,image/png,image/jpeg,image/webp,image/tiff,image/bmp"
            className="hidden"
            onChange={(e) => {
              void pick(e.target.files?.[0])
              e.target.value = '' // allow re-picking the same file after removing it
            }}
          />
        </div>

        <Button
          disabled={!file || submitting || wrapping}
          onClick={() => file && onSubmit(file)}
          className="self-start"
        >
          {submitting ? (
            <>
              <Loader className="h-4 w-4" /> Processing&hellip;
            </>
          ) : (
            'Process batch'
          )}
        </Button>
      </CardContent>
    </Card>
  )
}

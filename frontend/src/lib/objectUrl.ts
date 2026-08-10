import { useEffect, useState } from 'react'

/**
 * An object URL that lives exactly as long as the component showing it.
 *
 * ## Why this is a hook rather than a field on the picked file
 *
 * The upload list used to call `URL.createObjectURL` for every file the moment it was picked and
 * hold the URL on the record. At twenty files that is invisible. At 400 it is the single thing
 * most likely to kill the tab, for two compounding reasons:
 *
 * 1. **Every URL pins its blob.** 400 photos at 5 MB is 2 GB of `File` data the browser cannot
 *    release, on top of the copies `wrapImagesInZip` is about to make.
 * 2. **Every `<img>` decodes.** A 4000×3000 JPEG is ~48 MB decoded in RGBA regardless of the
 *    60-pixel box it is drawn in. 400 of those is not a number a browser survives.
 *
 * Tying the URL to the mounted component means only the rows actually on screen hold either. A
 * paginated list of 50 holds 50, and the rest cost nothing until scrolled to.
 *
 * Pass `undefined` for "nothing to show" — the hook returns undefined and allocates nothing.
 */
export function useObjectUrl(file: File | Blob | undefined): string | undefined {
  const [url, setUrl] = useState<string | undefined>(undefined)

  useEffect(() => {
    if (!file) {
      setUrl(undefined)
      return
    }
    const created = URL.createObjectURL(file)
    setUrl(created)
    return () => {
      URL.revokeObjectURL(created)
    }
  }, [file])

  return url
}

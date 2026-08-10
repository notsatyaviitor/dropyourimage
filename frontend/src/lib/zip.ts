/**
 * Minimal ZIP writer, so images can be dropped straight into the upload dropzone without the user
 * having to archive them first.
 *
 * The backend's `POST /jobs` contract is frozen to a zip upload (see docs/API_CONTRACT.md) — this
 * exists instead of loosening that contract, or pulling in a zip library for one small, fully
 * spec-known case. Entries are STORED (uncompressed): product photos are already compressed
 * formats, so DEFLATE would buy almost nothing and would need a compressor in the browser for
 * bytes that are about to be decompressed and re-processed anyway.
 *
 * Verified directly, not assumed: the encoded output is fed through Python's own `zipfile` and the
 * backend's `zip_reader.read_images`, checking that every entry's bytes are readable — not merely
 * that the directory listing parses. See the note on `buildZip` for why that distinction matters.
 */

// ZIP local/central header signatures, per the PKZIP APPNOTE format.
const LOCAL_FILE_SIGNATURE = 0x04034b50
const CENTRAL_DIR_SIGNATURE = 0x02014b50
const END_OF_CENTRAL_DIR_SIGNATURE = 0x06054b50

const VERSION_NEEDED = 20 // 2.0 — the baseline that supports STORED entries

let crcTable: Uint32Array | null = null

function getCrcTable(): Uint32Array {
  if (crcTable) return crcTable
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    }
    table[n] = c >>> 0
  }
  crcTable = table
  return table
}

function crc32(data: Uint8Array): number {
  const table = getCrcTable()
  let crc = 0xffffffff
  for (let i = 0; i < data.length; i++) {
    crc = table[(crc ^ data[i]) & 0xff] ^ (crc >>> 8)
  }
  return (crc ^ 0xffffffff) >>> 0
}

/** DOS date/time fields are required by the format but not meaningful here; zero is valid. */
const DOS_TIME = 0
const DOS_DATE = 0

/**
 * Build a zip containing any number of STORED entries.
 *
 * **The offset bookkeeping is the part that breaks.** Each central-directory record must point at
 * *its own* local header, so `offset` is accumulated as entries are laid down. An earlier
 * single-entry version of this file conflated "where the local header starts" with "where the
 * central directory starts" — the result parsed fine as a directory listing (names and sizes were
 * readable) and failed only when something tried to read an entry's bytes, with "Bad magic number
 * for file header". With one entry that bug is invisible half the time because the correct answer is
 * 0; with several it corrupts every entry after the first. Verified against Python's own `zipfile`
 * and the backend's `zip_reader.read_images`, not assumed.
 *
 * Duplicate names are disambiguated, because the backend keys per-image results by entry name and
 * two files called `image.png` would otherwise collide silently.
 */
export function buildZip(entries: { name: string; data: Uint8Array }[]): Uint8Array {
  const parts = buildZipParts(entries)
  const total = parts.reduce((n, p) => n + p.length, 0)
  const out = new Uint8Array(total)
  let pos = 0
  for (const chunk of parts) {
    out.set(chunk, pos)
    pos += chunk.length
  }
  return out
}

function localHeader(nameBytes: Uint8Array, crc: number, size: number): Uint8Array {
  const local = new Uint8Array(30 + nameBytes.length)
  const lv = new DataView(local.buffer)
  lv.setUint32(0, LOCAL_FILE_SIGNATURE, true)
  lv.setUint16(4, VERSION_NEEDED, true)
  lv.setUint16(6, 0, true)
  lv.setUint16(8, 0, true) // stored
  lv.setUint16(10, DOS_TIME, true)
  lv.setUint16(12, DOS_DATE, true)
  lv.setUint32(14, crc, true)
  lv.setUint32(18, size, true)
  lv.setUint32(22, size, true)
  lv.setUint16(26, nameBytes.length, true)
  lv.setUint16(28, 0, true)
  local.set(nameBytes, 30)
  return local
}

function centralRecord(
  nameBytes: Uint8Array,
  crc: number,
  size: number,
  offset: number,
): Uint8Array {
  const central = new Uint8Array(46 + nameBytes.length)
  const cv = new DataView(central.buffer)
  cv.setUint32(0, CENTRAL_DIR_SIGNATURE, true)
  cv.setUint16(4, VERSION_NEEDED, true)
  cv.setUint16(6, VERSION_NEEDED, true)
  cv.setUint16(8, 0, true)
  cv.setUint16(10, 0, true)
  cv.setUint16(12, DOS_TIME, true)
  cv.setUint16(14, DOS_DATE, true)
  cv.setUint32(16, crc, true)
  cv.setUint32(20, size, true)
  cv.setUint32(24, size, true)
  cv.setUint16(28, nameBytes.length, true)
  cv.setUint16(30, 0, true)
  cv.setUint16(32, 0, true)
  cv.setUint16(34, 0, true)
  cv.setUint16(36, 0, true)
  cv.setUint32(38, 0, true)
  cv.setUint32(42, offset, true) // this entry's own local header
  central.set(nameBytes, 46)
  return central
}

function endRecord(count: number, centralSize: number, centralDirStart: number): Uint8Array {
  const end = new Uint8Array(22)
  const ev = new DataView(end.buffer)
  ev.setUint32(0, END_OF_CENTRAL_DIR_SIGNATURE, true)
  ev.setUint16(4, 0, true)
  ev.setUint16(6, 0, true)
  ev.setUint16(8, count, true)
  ev.setUint16(10, count, true)
  ev.setUint32(12, centralSize, true)
  ev.setUint32(16, centralDirStart, true)
  ev.setUint16(20, 0, true)
  return end
}

function dedupeName(name: string, n: number): string {
  const dot = name.lastIndexOf('.')
  return dot > 0 ? `${name.slice(0, dot)}-${n}${name.slice(dot)}` : `${name}-${n}`
}

/**
 * The zip's parts, ready to hand to a `Blob` without ever concatenating them.
 *
 * `buildZip` returns one contiguous `Uint8Array`, which means allocating a second copy of the
 * entire batch just to join pieces that a `Blob` is perfectly happy to accept as a list. On a
 * 60 KB thumbnail that is free. On the client's real PSDs — 130 MB **each** — it is the
 * difference between a batch costing 2x its own size in the tab and costing 3x.
 *
 * Same bytes, same layout, same offset bookkeeping; only the final join is skipped.
 */
function buildZipParts(entries: { name: string; data: Uint8Array }[]): Uint8Array[] {
  const encoder = new TextEncoder()
  const seen = new Map<string, number>()

  const prepared = entries.map(({ name, data }) => {
    const count = seen.get(name) ?? 0
    seen.set(name, count + 1)
    const unique = count === 0 ? name : dedupeName(name, count)
    return { nameBytes: encoder.encode(unique), data, crc: crc32(data) }
  })

  const locals: Uint8Array[] = []
  const centrals: Uint8Array[] = []
  let offset = 0

  for (const { nameBytes, data, crc } of prepared) {
    const size = data.length
    locals.push(localHeader(nameBytes, crc, size), data)
    centrals.push(centralRecord(nameBytes, crc, size, offset))
    offset += 30 + nameBytes.length + size
  }

  const centralSize = centrals.reduce((n, c) => n + c.length, 0)
  return [...locals, ...centrals, endRecord(prepared.length, centralSize, offset)]
}

/**
 * Wrap a batch of image Files into one zip File for `POST /jobs`.
 *
 * Files are read **one at a time**, not via `Promise.all`. Reading them in parallel materialises
 * every `ArrayBuffer` simultaneously — for a batch of the client's 130 MB PSDs that is the whole
 * batch resident before a single byte is written into the zip, on top of the zip itself.
 * Sequential reads cost a little latency and bound the peak to roughly one batch.
 */
export async function wrapImagesInZip(files: File[]): Promise<File> {
  const entries: { name: string; data: Uint8Array }[] = []
  for (const f of files) {
    entries.push({ name: f.name, data: new Uint8Array(await f.arrayBuffer()) })
  }
  return new File(buildZipParts(entries) as BlobPart[], 'batch.zip', { type: 'application/zip' })
}


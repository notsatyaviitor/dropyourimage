/**
 * Minimal single-entry ZIP writer, so a single image can be dropped straight into the upload
 * dropzone without the user having to zip it themselves first.
 *
 * The backend's `POST /jobs` contract is frozen to a zip upload (see docs/API_CONTRACT.md) — this
 * exists instead of loosening that contract, or pulling in a zip library for one small, fully
 * spec-known case. Entries are STORED (uncompressed): a single product photo is small, and
 * skipping DEFLATE avoids needing a compressor in the browser for a file that's about to be
 * decompressed and re-processed anyway.
 *
 * Verified directly, not assumed: the encoded output was fed through the backend's own
 * `zip_reader.read_images` (Python's `zipfile` under the hood) and confirmed to open with the
 * image intact — see frontend/README.md.
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
 * Build a zip containing exactly one STORED entry.
 *
 * @param filename entry name inside the archive — kept as the original file's name so the
 *   backend's per-image result still shows the name the user recognises.
 * @param data the file's raw bytes.
 */
export function buildSingleEntryZip(filename: string, data: Uint8Array): Uint8Array {
  const nameBytes = new TextEncoder().encode(filename)
  const crc = crc32(data)
  const size = data.length

  const local = new Uint8Array(30 + nameBytes.length)
  {
    const v = new DataView(local.buffer)
    v.setUint32(0, LOCAL_FILE_SIGNATURE, true)
    v.setUint16(4, VERSION_NEEDED, true)
    v.setUint16(6, 0, true) // flags
    v.setUint16(8, 0, true) // compression: 0 = stored
    v.setUint16(10, DOS_TIME, true)
    v.setUint16(12, DOS_DATE, true)
    v.setUint32(14, crc, true)
    v.setUint32(18, size, true) // compressed size == uncompressed for STORED
    v.setUint32(22, size, true)
    v.setUint16(26, nameBytes.length, true)
    v.setUint16(28, 0, true) // extra field length
    local.set(nameBytes, 30)
  }

  // The archive holds exactly one entry, so its local header always starts at byte 0 — this is
  // NOT the same value as where the central directory starts (that's centralDirStart, below).
  // Conflating the two under one name was the exact bug this comment now guards against: it
  // produced a central directory that pointed at itself instead of at the local header, which
  // parsed fine as a directory listing (names/sizes were readable) but failed the moment
  // anything tried to actually read the file's bytes ("Bad magic number for file header").
  // Caught by feeding the encoder's real output through Python's own zipfile module.
  const localHeaderOffset = 0
  const centralDirStart = local.length + size

  const central = new Uint8Array(46 + nameBytes.length)
  {
    const v = new DataView(central.buffer)
    v.setUint32(0, CENTRAL_DIR_SIGNATURE, true)
    v.setUint16(4, VERSION_NEEDED, true) // version made by
    v.setUint16(6, VERSION_NEEDED, true) // version needed
    v.setUint16(8, 0, true) // flags
    v.setUint16(10, 0, true) // compression: stored
    v.setUint16(12, DOS_TIME, true)
    v.setUint16(14, DOS_DATE, true)
    v.setUint32(16, crc, true)
    v.setUint32(20, size, true)
    v.setUint32(24, size, true)
    v.setUint16(28, nameBytes.length, true)
    v.setUint16(30, 0, true) // extra field length
    v.setUint16(32, 0, true) // comment length
    v.setUint16(34, 0, true) // disk number start
    v.setUint16(36, 0, true) // internal file attributes
    v.setUint32(38, 0, true) // external file attributes
    v.setUint32(42, localHeaderOffset, true) // relative offset of local header
    central.set(nameBytes, 46)
  }

  const end = new Uint8Array(22)
  {
    const v = new DataView(end.buffer)
    v.setUint32(0, END_OF_CENTRAL_DIR_SIGNATURE, true)
    v.setUint16(4, 0, true) // disk number
    v.setUint16(6, 0, true) // disk with central dir
    v.setUint16(8, 1, true) // entries on this disk
    v.setUint16(10, 1, true) // total entries
    v.setUint32(12, central.length, true) // central dir size
    v.setUint32(16, centralDirStart, true) // central dir offset
    v.setUint16(20, 0, true) // comment length
  }

  const out = new Uint8Array(local.length + size + central.length + end.length)
  let offset = 0
  out.set(local, offset)
  offset += local.length
  out.set(data, offset)
  offset += size
  out.set(central, offset)
  offset += central.length
  out.set(end, offset)
  return out
}

/** Wrap a single image File into a zip File, ready to hand to the existing upload flow. */
export async function wrapImageInZip(file: File): Promise<File> {
  const bytes = new Uint8Array(await file.arrayBuffer())
  const zipBytes = buildSingleEntryZip(file.name, bytes)
  // `Uint8Array` is generic over its buffer type in newer TS DOM lib definitions
  // (ArrayBufferLike, which also covers SharedArrayBuffer, and whose `.slice()` return type
  // stays that same union) while File's BlobPart wants a concrete ArrayBuffer. Type-system
  // nuance only: every byte array in this module is built fresh via `new Uint8Array(number)`,
  // never from a shared buffer, so this is never actually a SharedArrayBuffer at runtime.
  const buffer = zipBytes.buffer.slice(zipBytes.byteOffset, zipBytes.byteOffset + zipBytes.byteLength)
  return new File([buffer as ArrayBuffer], 'single-image.zip', { type: 'application/zip' })
}

"""Safe extraction of an uploaded zip.

This is the untrusted-input boundary of the whole system, so the guards here are security
controls rather than tuning knobs — see docs/SECURITY.md. Nothing is written to disk: entries are
read into memory one at a time, bounded, so a hostile archive cannot escape a directory it never
touches.

Guards, and what each stops:

* **Path traversal (zip-slip)** — an entry named ``../../etc/cron.d/x``. We never write files, but
  entry names still reach logs, the results grid and output filenames, so they are validated and
  flattened to a basename regardless.
* **Entry count cap** — an archive with a million tiny files, to exhaust the queue.
* **Total uncompressed cap** — the classic zip bomb: a few KB expanding to gigabytes.
* **Per-entry compression-ratio cap** — one entry that is mostly zeroes, which slips under a total
  cap while still exhausting memory when read.
* **MIME sniffing** — a ``.jpg`` that is really a script. Extensions are never trusted.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Iterator

from app.core import errors
from app.core.settings import Settings

# Content types we will attempt to decode. Sniffed from bytes, never from the filename.
_ALLOWED_MIME_PREFIXES = ("image/",)

# Sniffed types that are images but that we do not process, listed explicitly so the rejection
# message can be specific rather than "unsupported".
_KNOWN_UNSUPPORTED = {
    "image/svg+xml": "SVG is a vector format with no pixels to segment",
    "image/gif": "GIF is not used for product photography",
}

# A single entry expanding by more than this is treated as hostile regardless of the total cap.
_MAX_ENTRY_COMPRESSION_RATIO = 200

# Noise every macOS zip contains. Skipped quietly — flagging them as errors would fill the results
# grid with rubbish on every upload from a Mac.
_IGNORED_PREFIXES = ("__MACOSX/", "._")
_IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


@dataclass
class ZipEntry:
    name: str
    """Flattened basename, safe to display and to use in an output filename."""

    data: bytes


@dataclass
class ZipRejection:
    name: str
    reason: str


def read_images(data: bytes, settings: Settings) -> tuple[list[ZipEntry], list[ZipRejection]]:
    """Extract image entries from a zip, with everything bounded.

    Returns the usable entries plus a list of what was rejected and why — the rejections are shown
    in the UI rather than dropped, because "17 of 20 processed" with no explanation is
    indistinguishable from a bug.

    Raises `MaliciousArchive` or `FileTooLarge` only for problems with the archive *itself*. A bad
    individual entry becomes a rejection, so one poisoned file cannot block a legitimate batch.
    """
    if len(data) > settings.max_zip_bytes:
        raise errors.FileTooLarge(
            f"The archive is larger than the {settings.max_zip_bytes // 1_048_576} MB limit."
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise errors.MaliciousArchive("The uploaded file is not a valid zip archive.") from exc

    infos = [i for i in archive.infolist() if not _is_ignorable(i)]

    if len(infos) > settings.max_zip_entries:
        raise errors.MaliciousArchive(
            f"The archive contains more than {settings.max_zip_entries} files."
        )

    declared_total = sum(i.file_size for i in infos)
    if declared_total > settings.max_uncompressed_bytes:
        raise errors.MaliciousArchive(
            "The archive expands to more than the permitted uncompressed size."
        )

    entries: list[ZipEntry] = []
    rejections: list[ZipRejection] = []
    consumed = 0

    # Sorted for determinism: the same archive must produce the same order every run, or the
    # results grid reshuffles between demo runs for no reason.
    for info in sorted(infos, key=lambda i: i.filename):
        display = _safe_basename(info.filename)

        problem = _screen(info, settings)
        if problem:
            rejections.append(ZipRejection(display, problem))
            continue

        try:
            with archive.open(info) as handle:
                # Read one byte past the declared size: a mismatch means the central directory
                # lied about file_size, which is how a bomb evades the pre-check above.
                payload = handle.read(info.file_size + 1)
        except Exception:
            rejections.append(ZipRejection(display, "The file could not be read from the archive."))
            continue

        if len(payload) > info.file_size:
            raise errors.MaliciousArchive(
                "An entry is larger than the archive's own index claims."
            )

        consumed += len(payload)
        if consumed > settings.max_uncompressed_bytes:
            raise errors.MaliciousArchive(
                "The archive expands to more than the permitted uncompressed size."
            )

        mime = _sniff(payload)
        if mime in _KNOWN_UNSUPPORTED:
            rejections.append(ZipRejection(display, _KNOWN_UNSUPPORTED[mime]))
            continue
        if not mime.startswith(_ALLOWED_MIME_PREFIXES):
            rejections.append(
                ZipRejection(display, f"Not an image file (detected {mime}).")
            )
            continue

        entries.append(ZipEntry(name=display, data=payload))

    if not entries and not rejections:
        raise errors.MaliciousArchive("The archive contains no files.")

    return entries, rejections


def _is_ignorable(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    if info.is_dir():
        return True
    if any(name.startswith(p) or f"/{p}" in name for p in _IGNORED_PREFIXES):
        return True
    return _safe_basename(name) in _IGNORED_NAMES


def _screen(info: zipfile.ZipInfo, settings: Settings) -> str | None:
    """Cheap checks against the archive index, before reading any bytes."""
    if _is_unsafe_path(info.filename):
        # Not reported as a per-file rejection with the raw path, since echoing an attacker's
        # string into the UI is its own small problem.
        return "The file path in the archive is not permitted."

    if info.file_size > settings.max_image_bytes:
        return f"Larger than the {settings.max_image_bytes // 1_048_576} MB per-image limit."

    if info.compress_size > 0:
        ratio = info.file_size / info.compress_size
        if ratio > _MAX_ENTRY_COMPRESSION_RATIO:
            return "The file's compression ratio is implausible."

    return None


def _is_unsafe_path(name: str) -> bool:
    """Absolute paths, parent traversal, drive letters and NUL bytes."""
    if not name or "\x00" in name:
        return True
    normalised = name.replace("\\", "/")
    if normalised.startswith("/"):
        return True
    if len(normalised) > 1 and normalised[1] == ":":
        return True
    return any(part == ".." for part in normalised.split("/"))


def _safe_basename(name: str) -> str:
    """Last path component, with separators stripped. Never empty."""
    flattened = name.replace("\\", "/").rstrip("/")
    base = flattened.rsplit("/", 1)[-1]
    return base or "unnamed"


def _sniff(payload: bytes) -> str:
    """Detect content type from bytes.

    Prefers libmagic; falls back to signature checks so the security control still functions when
    libmagic is missing rather than silently passing everything through.
    """
    try:
        import magic

        detected = magic.from_buffer(payload[:4096], mime=True)
        if detected:
            return str(detected)
    except Exception:
        pass

    return _sniff_by_signature(payload)


def _sniff_by_signature(payload: bytes) -> str:
    head = payload[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "image/tiff"
    if head.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head.startswith(b"8BPS"):
        return "image/vnd.adobe.photoshop"
    if head.lstrip()[:5].lower() in (b"<?xml", b"<svg"):
        return "image/svg+xml"
    return "application/octet-stream"


def iter_names(data: bytes) -> Iterator[str]:
    """Entry names without extracting anything — used for a fast pre-upload count."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if not _is_ignorable(info):
                yield _safe_basename(info.filename)

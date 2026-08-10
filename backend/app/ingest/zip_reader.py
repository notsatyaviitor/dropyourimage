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

## Two ways in, one set of guards

`read_images` returns every entry's bytes at once. That is fine for the handful of images an
inline job or a test uses, and wrong for a bulk job: 400 entries under the 2 GB uncompressed cap
means 2 GB resident before a single image is processed, on top of `WORKER_CONCURRENCY` images
already in flight.

So bulk uses `plan_entries` + `read_entry` instead — plan once to learn *what is in the archive*
(reading at most `_SNIFF_BYTES` per entry, never a whole one), then read each entry's bytes only
when its turn comes and drop them straight after. Resident cost goes from "every image" to "one".

**Both paths screen identically.** `read_images` is implemented on top of the same two primitives
rather than keeping its own copy of the checks — a second copy of a security screen is how one of
them silently falls behind the other.
"""

from __future__ import annotations

import io
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from app.core import errors
from app.core.settings import Settings
from app.models import ErrorCode

# Content types we will attempt to decode. Sniffed from bytes, never from the filename.
_ALLOWED_MIME_PREFIXES = ("image/",)

# Exact types that are legitimate input but do not sniff as `image/*`.
#
# Camera raw is the awkward case. Some raws are TIFF containers and sniff as `image/tiff`, but
# CR3 is ISO-BMFF and libmagic commonly reports it as `video/quicktime` or a generic binary, while
# CRW and some .raw dumps have no signature libmagic knows at all. EPS is PostScript, so it is
# `application/postscript` by definition and never `image/*`.
#
# **This does not re-trust the extension.** An entry only gets through this path when its sniffed
# type is in the narrow list below AND its extension names a raw/EPS format we can actually decode
# — the extension narrows an already-allowed content type, it never promotes a disallowed one. The
# decoder is still the final arbiter, and a mislabelled file fails there as `image_decode_failed`.
_EXTENSION_GATED_MIMES = frozenset(
    {
        "application/postscript",   # EPS
        "application/octet-stream", # CRW, .raw, some CR3
        "video/quicktime",          # CR3 (ISO-BMFF, shares a container family with MOV)
        "video/mp4",                # CR3 under a different libmagic build
        "application/x-empty",      # zero-length; rejected by the decoder with a clear message
    }
)

# Sniffed types that are images but that we do not process, listed explicitly so the rejection
# message can be specific rather than "unsupported".
_KNOWN_UNSUPPORTED = {
    "image/svg+xml": "SVG is a vector format with no pixels to segment",
    "image/gif": "GIF is not used for product photography",
}

# A single entry expanding by more than this is treated as hostile regardless of the total cap.
_MAX_ENTRY_COMPRESSION_RATIO = 200

# How much of an entry the planning pass reads. libmagic is given 4096 bytes and the signature
# fallback looks at 16, so this is everything the sniff can use — and it is the reason planning a
# 400-image archive costs kilobytes instead of gigabytes.
_SNIFF_BYTES = 4096

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

    code: ErrorCode = ErrorCode.UNSUPPORTED_FILE
    """Why, in the taxonomy — not just in prose.

    Every rejection used to be stamped `unsupported_file` by the caller, which made a 130 MB PSD
    (over the per-image byte cap, a perfectly supported format) indistinguishable from a `.txt`
    renamed to `.png`. The UI renders copy keyed on the code, so both printed "Not a supported
    image file" and the real reason — carried right here in `reason` — was thrown away one line
    from the screen. Diagnosing it needed the server logs for something the user was already
    looking at.
    """


@dataclass
class PlannedEntry:
    """An entry that passed screening, described without holding its bytes.

    `path` is the archive's own entry name and is how the bytes are read back later; `name` is the
    flattened basename used for display and output filenames. They are kept apart because two
    entries in different folders can flatten to the same basename, and reading back by basename
    would then fetch the wrong file.
    """

    name: str
    path: str
    size: int
    """Declared uncompressed size, from the index. Verified against reality in `read_entry`."""


@contextmanager
def open_archive(data: bytes, settings: Settings) -> Iterator[zipfile.ZipFile]:
    """Open an archive with the whole-archive guards applied.

    Held open across a batch so the central directory is parsed once rather than per entry. The
    compressed bytes stay resident for that window, which `max_zip_bytes` bounds — the thing bulk
    processing must avoid is the *decompressed* set, which is a different order of magnitude.
    """
    # 0 disables, so an unlimited per-image size is not silently re-capped by the archive that
    # carries it — an image cap of 'none' means nothing if a 1 GB archive limit still refuses it.
    if settings.max_zip_bytes and len(data) > settings.max_zip_bytes:
        raise errors.FileTooLarge(
            f"The archive is larger than the {settings.max_zip_bytes // 1_048_576} MB limit."
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise errors.MaliciousArchive("The uploaded file is not a valid zip archive.") from exc

    try:
        yield archive
    finally:
        archive.close()


def plan_entries(
    archive: zipfile.ZipFile, settings: Settings
) -> tuple[list[PlannedEntry], list[ZipRejection]]:
    """Screen every entry and report what is usable, **without reading any entry in full**.

    This is the pass that makes a 400-image job possible: it answers "how many images are there
    and what are they called" — which the job needs upfront to report an honest `total` — at a
    cost of at most `_SNIFF_BYTES` per entry rather than the whole thing.

    The bomb check that needs the full bytes (declared size versus actual) deliberately lives in
    `read_entry` instead. That is not a weakening: it runs at the moment the bytes actually enter
    memory, which is the moment the guard is protecting.
    """
    infos = [i for i in archive.infolist() if not _is_ignorable(i)]

    if len(infos) > settings.max_zip_entries:
        raise errors.MaliciousArchive(
            f"The archive contains more than {settings.max_zip_entries} files."
        )

    declared_total = sum(i.file_size for i in infos)
    if settings.max_uncompressed_bytes and declared_total > settings.max_uncompressed_bytes:
        raise errors.MaliciousArchive(
            "The archive expands to more than the permitted uncompressed size."
        )

    planned: list[PlannedEntry] = []
    rejections: list[ZipRejection] = []

    # Sorted for determinism: the same archive must produce the same order every run, or the
    # results grid reshuffles between demo runs for no reason.
    for info in sorted(infos, key=lambda i: i.filename):
        display = _safe_basename(info.filename)

        problem = _screen(info, settings)
        if problem:
            reason, code = problem
            rejections.append(ZipRejection(display, reason, code))
            continue

        try:
            with archive.open(info) as handle:
                head = handle.read(_SNIFF_BYTES)
        except Exception:
            rejections.append(
                ZipRejection(
                    display,
                    "The file could not be read from the archive.",
                    ErrorCode.MALICIOUS_ARCHIVE,
                )
            )
            continue

        mime = _sniff(head)
        if mime in _KNOWN_UNSUPPORTED:
            rejections.append(ZipRejection(display, _KNOWN_UNSUPPORTED[mime]))
            continue
        if not mime.startswith(_ALLOWED_MIME_PREFIXES) and not _extension_gated(display, mime):
            rejections.append(ZipRejection(display, f"Not an image file (detected {mime})."))
            continue

        planned.append(PlannedEntry(name=display, path=info.filename, size=info.file_size))

    if not planned and not rejections:
        raise errors.MaliciousArchive("The archive contains no files.")

    return planned, rejections


def read_entry(archive: zipfile.ZipFile, planned: PlannedEntry) -> bytes:
    """Read one planned entry's bytes, with the declared-size bomb check applied.

    Reading one byte past the declared size is how a lying central directory is caught: a bomb
    evades the index-based pre-check in `plan_entries` precisely by understating `file_size`, so
    the only place the lie is detectable is here, against the real decompressed stream.
    """
    info = archive.getinfo(planned.path)
    with archive.open(info) as handle:
        payload = handle.read(info.file_size + 1)

    if len(payload) > info.file_size:
        raise errors.MaliciousArchive("An entry is larger than the archive's own index claims.")

    return payload


def read_images(data: bytes, settings: Settings) -> tuple[list[ZipEntry], list[ZipRejection]]:
    """Extract image entries from a zip, with everything bounded.

    Returns the usable entries plus a list of what was rejected and why — the rejections are shown
    in the UI rather than dropped, because "17 of 20 processed" with no explanation is
    indistinguishable from a bug.

    Raises `MaliciousArchive` or `FileTooLarge` only for problems with the archive *itself*. A bad
    individual entry becomes a rejection, so one poisoned file cannot block a legitimate batch.

    **Holds every entry in memory at once**, so it is for inline jobs and tests. Bulk goes through
    `plan_entries` + `read_entry` — see the module docstring.
    """
    entries: list[ZipEntry] = []
    consumed = 0

    with open_archive(data, settings) as archive:
        planned, rejections = plan_entries(archive, settings)

        for item in planned:
            try:
                payload = read_entry(archive, item)
            except errors.MaliciousArchive:
                raise
            except Exception:
                rejections.append(
                    ZipRejection(
                        item.name,
                        "The file could not be read from the archive.",
                        ErrorCode.MALICIOUS_ARCHIVE,
                    )
                )
                continue

            # Retained here because this path accumulates the whole archive in memory by
            # definition. The streaming path drops each entry after use, so it has no equivalent
            # total to keep — its bound is one entry, structurally.
            consumed += len(payload)
            if settings.max_uncompressed_bytes and consumed > settings.max_uncompressed_bytes:
                raise errors.MaliciousArchive(
                    "The archive expands to more than the permitted uncompressed size."
                )

            entries.append(ZipEntry(name=item.name, data=payload))

    return entries, rejections


def _extension_gated(name: str, mime: str) -> bool:
    """Whether a non-`image/*` entry is a raw or EPS file we can decode.

    Both halves must agree: the sniffed type has to be one of the few that camera raw and EPS
    legitimately produce, *and* the extension has to name a format in the decoder's table. Either
    alone is not enough, so this cannot be used to smuggle an arbitrary binary through — the worst
    a mislabelled file achieves is reaching the decoder and failing there.
    """
    if mime not in _EXTENSION_GATED_MIMES:
        return False

    from app.imaging import formats as F

    source = F.source_from_name(name)
    return source is not None and (F.is_raw(source) or source.value == "eps")


def _is_ignorable(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    if info.is_dir():
        return True
    if any(name.startswith(p) or f"/{p}" in name for p in _IGNORED_PREFIXES):
        return True
    return _safe_basename(name) in _IGNORED_NAMES


def _screen(
    info: zipfile.ZipInfo, settings: Settings
) -> tuple[str, ErrorCode] | None:
    """Cheap checks against the archive index, before reading any bytes.

    Returns the reason **and its code**. The two travel together from here to the browser: the
    code chooses the copy and drives anything that keys on the taxonomy, the reason carries the
    specifics. Collapsing them was what made "your 130 MB PSD is over the size limit" arrive as
    "not a supported image file".
    """
    if _is_unsafe_path(info.filename):
        # Not reported as a per-file rejection with the raw path, since echoing an attacker's
        # string into the UI is its own small problem.
        return "The file path in the archive is not permitted.", ErrorCode.MALICIOUS_ARCHIVE

    # 0 means no per-image byte limit — the default. See `Settings.max_image_bytes` for why a byte
    # count is the wrong shape for this guard; `max_image_pixels` does the real work at decode.
    if settings.max_image_bytes and info.file_size > settings.max_image_bytes:
        return (
            f"This file is {info.file_size / 1_048_576:.0f} MB; the per-image limit is "
            f"{settings.max_image_bytes // 1_048_576} MB.",
            ErrorCode.FILE_TOO_LARGE,
        )

    if info.compress_size > 0:
        ratio = info.file_size / info.compress_size
        if ratio > _MAX_ENTRY_COMPRESSION_RATIO:
            return (
                "The file's compression ratio is implausible.",
                ErrorCode.MALICIOUS_ARCHIVE,
            )

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

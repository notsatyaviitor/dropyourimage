"""Object storage behind a Protocol, so the backend never hard-codes S3.

Two implementations: `s3.S3Storage` (MinIO locally, S3 in any real deployment) and
`memory.MemoryStorage` for tests. Written against the real S3 API from day one rather than the
local filesystem, so there is no port to do later.
"""

from __future__ import annotations

from typing import BinaryIO, Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None:
        """Store bytes. Overwrites silently — keys include a job id, so collisions are our bug."""
        ...

    def get(self, key: str) -> bytes:
        """Retrieve bytes. Raises `KeyError` if absent."""
        ...

    def put_stream(
        self,
        key: str,
        fileobj: BinaryIO,
        content_type: str,
        content_disposition: str | None = None,
    ) -> None:
        """Store from an open file object, without loading it into memory.

        `content_disposition` names the file the browser saves. It has to be stored on the object
        rather than set by the client: the download link points straight at cloud storage, and
        HTML's `download` attribute is **ignored for cross-origin URLs**, so the browser falls back
        to the storage key — which is why every order arrived as `outputs.zip`.

        Exists for the download bundle. `_write_bundle` assembles the zip through a temp file
        precisely so a multi-gigabyte bundle is never a `bytes` object — and then handed it to
        `put`, which took bytes, so the whole thing was read straight back in. Measured against
        this session's real assets: a 200-image order at source resolution is ~45 GB of TIFFs plus
        originals, i.e. a guaranteed OOM at the final step of an otherwise finished job.

        Implementations must stream. `fileobj` is positioned at the start and is not closed here.
        """
        ...

    def signed_url(self, key: str, ttl_seconds: int) -> str:
        """A time-limited read URL.

        Short-lived and unguessable, because output assets are the only thing this POC exposes
        publicly and it has no auth. See docs/SECURITY.md.
        """
        ...

    def exists(self, key: str) -> bool:
        ...


# Must cover every `OutputFormat`. `tests/test_export.py` asserts that, because the failure mode
# of a gap here is silent and format-specific rather than an error anywhere.
#
# BMP was missing, and BMP is the case where it actually breaks something. It is in
# `formats.BROWSER_RENDERABLE`, so a BMP-only job correctly gets NO extra PNG preview — the
# results grid puts the BMP itself into an `<img>`. Served as `application/octet-stream` the
# browser treats that as a download and paints nothing, so the job produced a perfectly good file
# and a blank card, with no preview to fall back on. Exactly the bug `preview_format` exists to
# prevent, arriving through the one door it does not cover.
CONTENT_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "tif": "image/tiff",
    "webp": "image/webp",
    "eps": "application/postscript",
    "psd": "image/vnd.adobe.photoshop",
    "zip": "application/zip",
}


def content_type_for(filename: str) -> str:
    return CONTENT_TYPES.get(filename.rsplit(".", 1)[-1].lower(), "application/octet-stream")


def job_key(job_id: str, *parts: str) -> str:
    """Namespace every object under its job, so cleanup is a prefix delete."""
    return "/".join(["jobs", job_id, *parts])

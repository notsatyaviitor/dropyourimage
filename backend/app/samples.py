"""Server-side sample images, so a demo can start a real job without uploading anything.

The client's own PSDs are ~130 MB each. Four of them pre-loaded into a browser would be 520 MB
fetched, held as `File` objects, and uploaded straight back — which breaks every memory rule in
`frontend/CLAUDE.md` before a single byte reached the API. So the samples live here, and the
browser sends only the flag saying to use them.

**These are genuinely processed.** The archive built below is the same shape `POST /jobs` stores
for a real upload, so the pipeline, the worker, the caps and the cut-out cache all behave exactly
as they do for a customer's own files. Nothing downstream knows the difference, which is the point:
the demo has to show what the product does, not a reproduction of it. The prototype's four
pre-loaded stock photos with unrelated "after" images are what this deliberately is not — see
`frontend/src/steps/UploadStep.tsx`.

Configured by `SAMPLES_DIR`. Empty or missing means the feature is simply off: `/samples` reports
none and the UI hides the option, rather than a deployment failing over a demo convenience.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.core.settings import Settings
from app.imaging import formats as F

#: Cap on how many files are offered, so pointing SAMPLES_DIR at a folder of 132 PSDs does not
#: silently create a 17 GB job from one button.
MAX_SAMPLES = 8


@dataclass(frozen=True)
class Sample:
    name: str
    size_bytes: int


def samples_dir(settings: Settings) -> Path | None:
    if not settings.samples_dir:
        return None
    path = Path(settings.samples_dir)
    return path if path.is_dir() else None


def list_samples(settings: Settings) -> list[Sample]:
    """Every usable sample, sorted by name so the demo is the same order every time.

    Filtered to formats the pipeline can actually read, because a stray `.txt` in the directory
    would otherwise be offered and then fail at ingest — a confusing way to start a demo.
    """
    directory = samples_dir(settings)
    if directory is None:
        return []

    found = [
        Sample(name=p.name, size_bytes=p.stat().st_size)
        for p in sorted(directory.iterdir())
        if p.is_file() and F.source_from_name(p.name) is not None
    ]
    return found[:MAX_SAMPLES]


def resolve(settings: Settings, names: list[str] | None) -> list[Sample]:
    """The samples a request asked for, restricted to ones that really exist.

    **`names` is never used to build a path.** It selects from `list_samples()` by exact basename,
    so `../../etc/passwd` or `/etc/shadow` simply matches nothing. Taking the caller's string and
    joining it onto `SAMPLES_DIR` would turn a demo convenience into arbitrary file read on a
    server with no authentication.

    `None` means "all of them", which is what the plain `use_samples=true` call sends.
    """
    available = list_samples(settings)
    if names is None:
        return available

    wanted = set(names)
    return [s for s in available if s.name in wanted]


def build_archive(settings: Settings, names: list[str] | None = None) -> bytes:
    """Pack the samples into the same zip shape a browser upload produces.

    Stored uncompressed: a PSD is already compressed internally, so deflate spends CPU on 520 MB
    to save almost nothing, and this runs inside the request that creates the job.

    Held in memory because `_accept_batch` takes bytes, which is the existing contract — one
    archive resident is the same peak the upload path already has (`MAX_ZIP_BYTES` is 1 GB). If
    the sample set ever grows past that, this is the line to make streaming.
    """
    directory = samples_dir(settings)
    if directory is None:
        raise FileNotFoundError("SAMPLES_DIR is not configured or does not exist")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for sample in resolve(settings, names):
            archive.write(directory / sample.name, sample.name)
    return buffer.getvalue()

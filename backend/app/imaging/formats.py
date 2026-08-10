"""The single source of truth for what this pipeline accepts, and what it can hand back.

Three separate questions get confused constantly, so they are three separate tables here:

1. **What can we read?** All 14 requested formats, plus WebP.
2. **What can we write?** Eight of them. This is the smaller set, and the gap is the whole reason
   this module exists.
3. **When we cannot write the source format, what do we deliver instead?**

Why the read/write sets differ
------------------------------
Camera raw (``CRW`` ``CR2`` ``CR3`` ``NEF`` ``RAW``) is **decode-only, permanently**. These are not
image containers — they are undemosaiced sensor readout plus proprietary MakerNotes and per-camera
calibration. No encoder exists in any library, because Canon and Nikon publish none and every
decoder including libraw is reverse-engineered. More fundamentally, the format cannot express what
this pipeline produces: a CR2 means "this is what the sensor recorded", and there is no valid CR2
meaning "that photo, but composited onto #F5F5F5".

Two workarounds were considered and rejected:

* *Rename the output to .cr2.* Photoshop and Lightroom reject it as corrupt. A file that will not
  open is worse than one with an honest extension.
* *Rewrite the JPEG preview embedded in the original raw.* Thumbnailers would show the processed
  image while the sensor data underneath stayed unedited, so any real editor would open the
  original. That is the same failure mode as the blank-PSD-preview bug (see docs/PSD.md) — right
  in the preview, wrong in the editor — and strictly worse than substituting the format honestly.

**DNG is the one genuine exception** and is deliberately *not* implemented. It is an open,
TIFF-based Adobe spec, so a Linear DNG could be written — but it could not be verified here (no
Adobe software), and shipping an unverifiable writer is exactly the mistake docs/PSD.md records.
It is grouped with the other raws until someone can open the output in Photoshop.

So a raw input is delivered as **16-bit TIFF**, which is what Lightroom and Capture One export a
processed raw as: lossless, keeps the bit depth the raw actually carried, and carries the ICC
profile. Every such result gets ``Note.FORMAT_SUBSTITUTED`` so the swap is never silent.
"""

from __future__ import annotations

from app.models import OutputFormat, SourceFormat

# --- reading -----------------------------------------------------------------

#: Lower-case file extension -> canonical source format. Extensions are a *hint* only; the zip
#: reader still sniffs content, because an extension is attacker-controlled (docs/SECURITY.md).
EXTENSION_TO_SOURCE: dict[str, SourceFormat] = {
    "png": SourceFormat.PNG,
    "jpg": SourceFormat.JPEG,
    "jpeg": SourceFormat.JPEG,
    "bmp": SourceFormat.BMP,
    "tif": SourceFormat.TIFF,
    "tiff": SourceFormat.TIFF,
    "webp": SourceFormat.WEBP,
    "eps": SourceFormat.EPS,
    "psd": SourceFormat.PSD,
    "crw": SourceFormat.CRW,
    "cr2": SourceFormat.CR2,
    "cr3": SourceFormat.CR3,
    "dng": SourceFormat.DNG,
    "nef": SourceFormat.NEF,
    "raw": SourceFormat.RAW,
}

#: Formats that must go through libraw (rawpy) rather than Pillow.
RAW_SOURCES: frozenset[SourceFormat] = frozenset(
    {
        SourceFormat.CRW,
        SourceFormat.CR2,
        SourceFormat.CR3,
        SourceFormat.DNG,
        SourceFormat.NEF,
        SourceFormat.RAW,
    }
)

# --- writing -----------------------------------------------------------------

#: Source format -> the format we deliver when `ExportSpec.match_source` is on and we *can* write
#: it. Only the entries here round-trip exactly.
SOURCE_TO_OUTPUT: dict[SourceFormat, OutputFormat] = {
    SourceFormat.PNG: OutputFormat.PNG,
    SourceFormat.JPEG: OutputFormat.JPEG,
    SourceFormat.BMP: OutputFormat.BMP,
    SourceFormat.TIFF: OutputFormat.TIFF,
    SourceFormat.WEBP: OutputFormat.WEBP,
    SourceFormat.EPS: OutputFormat.EPS,
    SourceFormat.PSD: OutputFormat.PSD,
}

#: What a source we cannot write is delivered as instead. See the module docstring.
RAW_SUBSTITUTE: OutputFormat = OutputFormat.TIFF


def source_from_name(name: str) -> SourceFormat | None:
    """Canonical source format from a filename, or None if the extension is unknown."""
    _, _, ext = name.rpartition(".")
    return EXTENSION_TO_SOURCE.get(ext.strip().lower()) if ext else None


def is_raw(source: SourceFormat) -> bool:
    return source in RAW_SOURCES


def can_round_trip(source: SourceFormat) -> bool:
    """Whether a job asking for "same format out" will genuinely get the same format."""
    return source in SOURCE_TO_OUTPUT


def output_for(source: SourceFormat) -> tuple[OutputFormat, bool]:
    """Resolve `source` to the format we will deliver.

    Returns ``(format, substituted)``. `substituted` is True when the source format cannot be
    written and the caller must surface `Note.FORMAT_SUBSTITUTED` — it is the caller's job to
    report it, not this module's, so this stays a pure lookup.
    """
    matched = SOURCE_TO_OUTPUT.get(source)
    if matched is not None:
        return matched, False
    return RAW_SUBSTITUTE, True


#: Formats with no alpha channel. Requesting one alongside `background.transparent` is rejected by
#: `JobConfig`, but `match_source` picks a format *after* that validation runs — so the same check
#: has to exist here too. Mirrors `export._NO_ALPHA_FORMATS`.
NO_ALPHA_FORMATS: frozenset[OutputFormat] = frozenset(
    {
        OutputFormat.JPEG,
        OutputFormat.BMP,
        OutputFormat.EPS,
    }
)

#: What `match_source` delivers instead when the source format cannot carry the transparency the
#: job asked for. PNG: lossless, universally viewable, and alpha is the whole point here.
TRANSPARENT_SUBSTITUTE: OutputFormat = OutputFormat.PNG


#: Formats a browser will actually paint inside an `<img>` tag.
#:
#: TIFF, EPS and PSD are **not** among them. Chrome, Firefox and Edge render none of the three;
#: Safari manages TIFF alone. This matters because the results grid puts an output URL straight
#: into an `<img>`, so a job delivering only TIFF showed an empty card — the file was correct and
#: downloadable, but the reviewer saw nothing. BMP is fine, despite being the odd one out.
BROWSER_RENDERABLE: frozenset[OutputFormat] = frozenset(
    {
        OutputFormat.PNG,
        OutputFormat.JPEG,
        OutputFormat.WEBP,
        OutputFormat.BMP,
    }
)

#: What a preview is rendered as when none of the delivered formats can be shown. PNG because it
#: is lossless and carries alpha, so a transparent result still previews honestly on a chequerboard.
PREVIEW_FORMAT: OutputFormat = OutputFormat.PNG


def preview_format_for(delivered: list[OutputFormat]) -> OutputFormat | None:
    """The extra format to render purely so the UI has something to show, or None.

    Returns None whenever at least one delivered format is already viewable — the common case, and
    one that must not pay for an extra encode.
    """
    if any(fmt in BROWSER_RENDERABLE for fmt in delivered):
        return None
    return PREVIEW_FORMAT


#: Source formats a browser can paint directly from the user's own picked `File`.
#:
#: The mirror of `BROWSER_RENDERABLE` for the INPUT side. `SourceFormat` is a wider set than
#: `OutputFormat` (camera raw decodes but cannot be written), so this cannot simply reuse it.
_RENDERABLE_SOURCES: frozenset[SourceFormat] = frozenset(
    {
        SourceFormat.PNG,
        SourceFormat.JPEG,
        SourceFormat.WEBP,
        SourceFormat.BMP,
    }
)


def is_browser_renderable_source(source: SourceFormat) -> bool:
    """Whether the UI can show this upload without the backend rendering a thumbnail for it.

    False for EPS, PSD, TIFF and every camera raw. The results page shows the user's source file
    beside the processed output by putting it straight into an `<img>`, which is blank for all of
    those — the same blind spot `preview_format_for` fixes on the output side. When this returns
    False, `jobs.py` stores a small PNG of the decoded source instead.
    """
    return source in _RENDERABLE_SOURCES


#: Formats a segmentation vendor will accept as an upload.
#:
#: The engines receive the *original file bytes*, not our decoded pixels, because a vendor's own
#: decoder generally beats a re-encode. That breaks down for formats no vendor reads: an EPS
#: upload came back from Photoroom as a flat HTTP 400 (measured), and PSD and camera raw are the
#: same class of problem. For those, `pipeline` re-encodes the decoded image to PNG before the
#: vendor call — the mask is unaffected, since it is computed on identical pixels either way.
#:
#: BMP and TIFF are in the safe set because they were *measured* working end to end against a live
#: Photoroom key, not because the documentation promised it.
VENDOR_SAFE_SOURCES: frozenset[SourceFormat] = frozenset(
    {
        SourceFormat.PNG,
        SourceFormat.JPEG,
        SourceFormat.BMP,
        SourceFormat.TIFF,
        SourceFormat.WEBP,
    }
)


def needs_reencode_for_vendor(source: SourceFormat | None) -> bool:
    """Whether the segmentation engines need a PNG rather than the original bytes.

    Unknown extensions (`None`) are treated as safe: the file decoded, so it is some ordinary
    image, and re-encoding every unrecognised upload would cost more than it saves.
    """
    return source is not None and source not in VENDOR_SAFE_SOURCES


#: TIFF is written 16-bit for these, because the whole point of substituting it for a raw is to
#: keep the extra bit depth the raw carried. An 8-bit TIFF would throw away the only real advantage
#: the source had over a JPEG.
DEEP_TIFF_SOURCES: frozenset[SourceFormat] = RAW_SOURCES

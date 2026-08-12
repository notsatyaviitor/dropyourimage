"""The frozen contract.

This module is the ONLY coupling between backend and frontend. Three developers work in
parallel against it, so treat changes as breaking: update `docs/API_CONTRACT.md` and
`frontend/src/api/types.ts` in the same commit, or the UI silently drifts.

Layout mirrors the five UI tabs deliberately — one spec object per tab, composed into a single
`JobConfig`. There is one job per upload, not five. See `frontend/CLAUDE.md`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Bumped whenever a change alters output pixels. Recorded on every result so a stakeholder can
# tell whether two assets came from the same pipeline.
PIPELINE_VERSION = "0.1.0"

_HEX_RE = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class StrictModel(BaseModel):
    """Reject unknown fields.

    A typo in a frontend payload should fail loudly at the boundary, not be silently dropped
    and debugged on Day 5.
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EngineId(str, Enum):
    """Segmentation backends. `LOCAL` is the offline control — see docs/ENGINES.md."""

    PHOTOROOM = "photoroom"
    REMOVEBG = "removebg"
    FALAI = "falai"
    GEMINI = "gemini"
    LOCAL = "local"


class EngineStrategy(str, Enum):
    AUTO = "auto"      # run the configured pair concurrently, keep the better alpha
    SINGLE = "single"  # one named engine; cheaper, and how you isolate an engine for review


class FitMode(str, Enum):
    """How the product is fitted into the target canvas.

    CONTAIN and PAD both preserve aspect ratio and never crop; they differ only in that PAD
    guarantees the full canvas is produced even when the product is tiny. COVER crops, so it
    can cut off product — offered because marketplace banners need it, but not the default.
    """

    CONTAIN = "contain"
    PAD = "pad"
    COVER = "cover"


class CentringMode(str, Enum):
    """Bounding box is the e-commerce convention and the default.

    CENTROID centres on the alpha's centre of mass, which differs for asymmetric products (a
    mug with a handle). We report the centroid offset either way so the difference is visible
    rather than argued about — this matches the client report's Feature 1 crop rule,
    "contour centroid vs frame centre".
    """

    BBOX = "bbox"
    CENTROID = "centroid"


class ShadowMode(str, Enum):
    """What happens to the shadow the photographer put there.

    PRESERVE reconstructs it onto the new background via a multiplicative ratio map. It
    requires a uniform original background and silently produces nonsense on a lifestyle shot,
    so it is gated by a uniformity check that emits `SHADOW_GATE_FAILED`.
    """

    PRESERVE = "preserve"
    REMOVE = "remove"


class OutputFormat(str, Enum):
    """Formats this pipeline can **write**. Strictly smaller than what it can read.

    Camera raw is absent and always will be — see `app/imaging/formats.py` for why that is a
    property of the format rather than a missing library.
    """

    PNG = "png"
    JPEG = "jpeg"
    TIFF = "tiff"
    WEBP = "webp"
    BMP = "bmp"
    EPS = "eps"
    PSD = "psd"


class SourceFormat(str, Enum):
    """Formats this pipeline can **read**.

    Everything here decodes. Only the subset in `formats.SOURCE_TO_OUTPUT` can also be written, so
    a raw source delivered under `export.match_source` comes back as 16-bit TIFF carrying
    `Note.FORMAT_SUBSTITUTED`.
    """

    PNG = "png"
    JPEG = "jpeg"
    BMP = "bmp"
    TIFF = "tiff"
    WEBP = "webp"
    EPS = "eps"
    PSD = "psd"

    # --- camera raw: decode-only ---
    CRW = "crw"
    CR2 = "cr2"
    CR3 = "cr3"
    DNG = "dng"
    NEF = "nef"
    RAW = "raw"


class ColorProfile(str, Enum):
    """ADOBE_RGB is required for PSD deliverables per the client report (8-bit embedded)."""

    SRGB = "srgb"
    ADOBE_RGB = "adobe_rgb"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"   # every image reached a terminal state; some may have failed
    FAILED = "failed"         # the job itself failed (bad zip, storage down)
    CANCELLED = "cancelled"


class ImageState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"       # rejected at ingest, e.g. not an image


class ErrorCode(str, Enum):
    """Error taxonomy.

    The point of separating these is that the UI must distinguish "the vendor throttled us"
    from "your file is unsupported" from "we have a bug". A single generic failure toast makes
    a demo look broken when it is actually rate-limited.
    """

    VENDOR_RATE_LIMITED = "vendor_rate_limited"
    VENDOR_ERROR = "vendor_error"
    VENDOR_TIMEOUT = "vendor_timeout"
    VENDOR_UNAUTHORIZED = "vendor_unauthorized"     # missing or rejected key
    VENDOR_OUT_OF_CREDITS = "vendor_out_of_credits"  # key is valid, the account needs topping up
    VENDOR_PAYLOAD_TOO_LARGE = "vendor_payload_too_large"  # vendor 413; retrying sends the same bytes
    NO_FOREGROUND_FOUND = "no_foreground_found"      # engine saw nothing to cut out
    UNSUPPORTED_FILE = "unsupported_file"
    FILE_TOO_LARGE = "file_too_large"
    OUTPUT_TOO_LARGE = "output_too_large"           # requested canvas beyond the pixel budget
    MALICIOUS_ARCHIVE = "malicious_archive"         # zip-slip / bomb guard tripped
    IMAGE_DECODE_FAILED = "image_decode_failed"
    PSD_UNAVAILABLE = "psd_unavailable"             # Adobe creds absent; see docs/PSD.md
    BATCH_TOO_LARGE = "batch_too_large"             # job-level image/byte cap, not a per-file one
    BULK_REQUIRES_WORKER = "bulk_requires_worker"   # too big to run inline; needs Redis + rq worker
    BUDGET_EXCEEDED = "budget_exceeded"             # job hit MAX_JOB_COST_USD; partial results kept
    JOB_CANCELLED = "job_cancelled"                 # cancelled by the operator mid-run
    INTERNAL_ERROR = "internal_error"


class Note(str, Enum):
    """Non-fatal outcomes the UI MUST surface.

    Every one of these means "we did something other than what you literally asked for".
    Rendering them silently is how a demo turns into a bug report — see frontend/CLAUDE.md.
    """

    SHADOW_GATE_FAILED = "shadow_gate_failed"
    """Original background was not uniform enough to separate shadow from scene; shadow removed."""

    UPSCALE_SKIPPED = "upscale_skipped"
    """Requested canvas exceeds the master and the upscaler is off; padded instead of stretched."""

    UPSCALED = "upscaled"
    """Routed through the AI upscaler. Pixels are synthesised, not photographed."""

    ENGINE_FALLBACK_USED = "engine_fallback_used"
    """Preferred engine failed or was rejected; the other candidate was used."""

    SINGLE_ENGINE_ONLY = "single_engine_only"
    """Auto-pick requested but only one engine was configured or reachable."""

    TIEBREAK_DETERMINISTIC = "tiebreak_deterministic"
    """Candidates scored equal and no Gemini key was set; picked by deterministic score alone."""

    TIEBREAK_VISION = "tiebreak_vision"
    """Candidates scored equal and a vision model chose between them.

    Distinct from TIEBREAK_DETERMINISTIC on purpose: this outcome is a model's *judgement*, not a
    measurement, and the UI must not present it as one. It is also the only note whose cause is
    non-deterministic, so two runs of the same job can legitimately differ here.
    """

    ALPHA_SUSPECT = "alpha_suspect"
    """Cut-out passed the reject rules but scored poorly. Worth a human look."""

    SUBJECT_LOCATED = "subject_located"
    """A text prompt isolated one object before segmenting; only that region was cut out."""

    SUBJECT_NOT_FOUND = "subject_not_found"
    """A subject prompt was given but the object could not be located.

    The image was segmented whole-frame instead, which on a multi-object scene very likely cut out
    the wrong thing. This is the note that must never be swallowed.
    """

    BUSY_SCENE = "busy_scene"
    """The original background is not a uniform studio backdrop.

    Every downstream assumption here is built for a packshot. On a furnished-room photograph the
    cut-out may be of the wrong object entirely, and shadow reconstruction is skipped. Emitted
    alongside a low alpha score so the UI can warn rather than deliver silently.
    """

    SCENE_LARGEST_OBJECT = "scene_largest_object"
    """The engine returned several separate objects, and only the largest was kept.

    Emitted when a mask would otherwise have been rejected as fragmented — a furnished-room
    photograph where segmentation correctly found five fixtures, none of them "the product".
    Rather than failing the image, the largest connected object is isolated and the rest are
    discarded, so the job delivers something instead of nothing.

    **This is a guess, and the UI must say so.** Largest is not the same as wanted: on a bathroom
    interior the biggest object is the washbasin, which is only right if the basin is what the
    order was for. `cutout.subject_prompt` is the accurate path and always beats this — name the
    object and the whole recovery is skipped.

    Deterministic and free: connected components on the alpha support, no model and no extra vendor
    call. Soft edges survive because the components are labelled on `alpha > 0.05` rather than on
    the binary core, so the kept object keeps its full transition band (invariant 2).
    """

    MULTI_OBJECT = "multi_object"
    """The scene was split into one PSD layer per object rather than a single cut-out.

    A model enumerated the objects and each was segmented separately, so the PSD carries a named
    layer and saved path per object. Costs one segmentation call *per object*, and the object list
    is a model's judgement about what counts as a thing — both facts the UI must surface.

    `ImageResult.layers` names what was found.
    """

    FORMAT_SUBSTITUTED = "format_substituted"
    """`match_source` was asked for, but the source format could not be delivered. Two causes:

    * **Camera raw** (CRW/CR2/CR3/DNG/NEF/RAW) is decode-only — undemosaiced sensor data, with no
      encoder in any library. Delivered as 16-bit TIFF, matching what Lightroom and Capture One
      export a processed raw as.
    * **A transparent job on a JPEG, BMP or EPS source.** None of those carry an alpha channel,
      so returning the source format would silently drop the transparency that was asked for.
      Delivered as PNG instead.

    `ImageResult.source_format` says what arrived, so the UI can name both sides of the swap.
    """

    VENDOR_DOWNSCALED = "vendor_downscaled"
    """The image was reduced before being sent to the segmentation vendor, so the mask was
    computed at less than full resolution and then scaled back up.

    **Only the mask is affected — no output pixel is.** Engines return alpha and never colour
    (`app/engines/base.py`), and the pipeline composites its own full-resolution original, so the
    product is untouched. What degrades is edge precision: a boundary resolved at 4096px and
    interpolated up to 5504px is softer and less accurate than one resolved natively.

    Emitted only when re-encoding alone could not get the payload under
    `VENDOR_MAX_UPLOAD_BYTES` — a 130 MB BMP becomes a 41 MB PNG of identical pixels and needs no
    downscaling at all. It has to be surfaced because it is a real quality difference the operator
    did not ask for, and `docs/LIMITATIONS.md` forbids quietly degrading output.
    """

    HARD_EDGED_MASK = "hard_edged_mask"
    """The chosen engine returned a hard boundary, not a coverage field. No soft alpha exists.

    Emitted for `EngineId.GEMINI`, whose API returns the mask as a polygon rather than a matte.
    Measured on the studio fixture: 2 distinct alpha values and 0 soft edge pixels, against 27 for
    ground truth. Two consequences the UI must not hide:

    * Edge quality is permanently lower — `backend/CLAUDE.md` invariant 2 exists because a
      thresholded alpha cannot be recovered later.
    * `decontaminate_edges` has no partially-transparent band to work on, so a white studio
      backdrop can leave a halo when composited onto a saturated colour.

    This is a property of the engine the operator chose, not a failure, so the image still
    delivers. It is the note that makes an accepted trade-off visible instead of silent.
    """

    PSD_FALLBACK_RASTER = "psd_fallback_raster"
    """PSD written without a vector clipping path — layers and mask only."""

    COVER_CROPPED = "cover_cropped"
    """COVER fit removed product pixels to fill the canvas."""


# ---------------------------------------------------------------------------
# Tab 1 — Clipping (cut-out)
# ---------------------------------------------------------------------------


class CutoutSpec(StrictModel):
    """Stage 1. The only AI in the default pipeline."""

    strategy: EngineStrategy = EngineStrategy.AUTO
    engine: EngineId | None = Field(
        default=None,
        description="Required when strategy is SINGLE; ignored when AUTO.",
    )
    keep_losing_candidate: bool = Field(
        default=True,
        description=(
            "Retain the rejected candidate for side-by-side review. Costs storage, not API "
            "calls — both engines already ran."
        ),
    )
    multi_object: bool = Field(
        default=False,
        description=(
            "Split a SCENE into one PSD layer per object instead of producing a single cut-out. "
            "A model enumerates the objects and each is segmented separately, so the PSD carries "
            "a named layer and saved path per object. Costs one segmentation call PER OBJECT. "
            "Skips centring, shadow reconstruction and background replacement, none of which are "
            "meaningful for a room. For a packshot, leave this off."
        ),
    )
    subject_prompt: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "Which object to extract, in plain words — e.g. 'coffee table'. Needed only for "
            "scenes containing several objects: background removal answers foreground vs "
            "background and cannot tell which foreground you meant. Null means whole-frame "
            "segmentation, which is correct for a packshot."
        ),
    )
    subject_padding_pct: Annotated[float, Field(ge=0, le=50)] = Field(
        default=6.0,
        description=(
            "Context kept around the located object before segmenting, as a percentage of the "
            "box's own size. A box cropped flush to the object gives the engine no surround to "
            "place the edge against."
        ),
    )

    @model_validator(mode="after")
    def _engine_required_for_single(self) -> CutoutSpec:
        if self.strategy is EngineStrategy.SINGLE and self.engine is None:
            raise ValueError("cutout.engine is required when strategy is 'single'")
        return self

    @field_validator("subject_prompt")
    @classmethod
    def _blank_prompt_is_none(cls, v: str | None) -> str | None:
        """An empty or whitespace-only box in the UI means "no prompt", not "find nothing"."""
        if v is None:
            return None
        return v.strip() or None


# ---------------------------------------------------------------------------
# Tab 2 — Background Services
# ---------------------------------------------------------------------------


class BackgroundSpec(StrictModel):
    """Stage 4 (applied after geometry — see docs/ARCHITECTURE.md for why).

    `color` is an **sRGB** triple. When the output profile is Adobe RGB it is converted, not
    copied; copying the numbers would deliver a different colour than requested.
    """

    transparent: bool = False
    color: str | None = Field(
        default="#FFFFFF",
        description="sRGB hex, '#RRGGBB'. Ignored when transparent is true.",
    )
    shadow: ShadowMode = ShadowMode.PRESERVE
    decontaminate_edges: bool = Field(
        default=True,
        description=(
            "Remove residual original-background colour from semi-transparent edge pixels. "
            "Off only for A/B demonstration of the halo it prevents."
        ),
    )

    @field_validator("color")
    @classmethod
    def _normalise_hex(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _HEX_RE.match(v):
            raise ValueError(f"invalid hex colour {v!r}; expected '#RRGGBB' or '#RGB'")
        h = v.lstrip("#")
        if len(h) == 3:                      # #ABC -> #AABBCC
            h = "".join(c * 2 for c in h)
        return f"#{h.upper()}"

    @model_validator(mode="after")
    def _need_colour_unless_transparent(self) -> BackgroundSpec:
        if not self.transparent and self.color is None:
            raise ValueError("background.color is required unless transparent is true")
        return self


# ---------------------------------------------------------------------------
# Tab 3 — Size
# ---------------------------------------------------------------------------


class SizeSpec(StrictModel):
    """Stage 3a. Output dimensions are exact: 500x500 means 500x500, asserted before return."""

    width: Annotated[int, Field(ge=1, le=20000)] = 500
    height: Annotated[int, Field(ge=1, le=20000)] = 500
    match_source: bool = Field(
        default=False,
        description=(
            "Deliver at the source photograph's own pixel dimensions, ignoring width/height. "
            "The canvas is exact either way; this only chooses where the numbers come from. "
            "Set this when 'the output looks softer than what I uploaded' — a 50 MP raw asked "
            "for at the 500x500 default keeps 0.5% of its pixels, and no resampler can undo "
            "that. Still bounded by MAX_OUTPUT_PIXELS, so an enormous source is refused rather "
            "than silently shrunk."
        ),
    )
    fit: FitMode = FitMode.CONTAIN
    margin_pct: Annotated[float, Field(ge=0, le=45)] = Field(
        default=5.0,
        description=(
            "Empty margin on each side as a percentage of the canvas, so the product occupies "
            "(100 - 2*margin)%. Capped at 45 because both sides are applied."
        ),
    )
    allow_upscale: bool = Field(
        default=False,
        description=(
            "When the product is smaller than the target, permit enlargement. Lanczos "
            "upscaling is never used: either the AI upscaler runs (UPSCALED) or the image is "
            "padded (UPSCALE_SKIPPED)."
        ),
    )


# ---------------------------------------------------------------------------
# Tab 4 — Centring
# ---------------------------------------------------------------------------


class CentringSpec(StrictModel):
    """Stage 3b. Deterministic geometry from the alpha channel — no model involved."""

    mode: CentringMode = CentringMode.BBOX
    include_shadow_in_bounds: bool = Field(
        default=False,
        description=(
            "Whether a preserved shadow counts toward the product bounds. False centres the "
            "product itself, which is usually what a catalogue wants; True keeps the "
            "product-plus-shadow group centred. A real product decision, so it is explicit."
        ),
    )
    alpha_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.05,
        description=(
            "Alpha above which a pixel counts as product for bounds measurement only. This "
            "does NOT threshold the stored alpha — soft alpha is preserved end to end."
        ),
    )


# ---------------------------------------------------------------------------
# Tab 5 — PSD
# ---------------------------------------------------------------------------


class PsdSpec(StrictModel):
    """Stage 5. Layer names follow the client report's Feature 2 convention."""

    enabled: bool = False
    vector_clipping_path: bool = Field(
        default=True,
        description=(
            "Trace the alpha into a real vector path in the Paths panel. When unavailable the "
            "PSD is still written with layers and a raster mask, flagged PSD_FALLBACK_RASTER."
        ),
    )
    path_name: str = Field(default="PATH", min_length=1, max_length=63)
    product_layer_name: str = Field(default="PROD", min_length=1, max_length=63)
    shadow_layer_name: str = Field(default="SHADOW", min_length=1, max_length=63)
    background_layer_name: str = Field(default="BG", min_length=1, max_length=63)
    profile: ColorProfile = ColorProfile.ADOBE_RGB


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class ExportSpec(StrictModel):
    formats: list[OutputFormat] = Field(
        default_factory=lambda: [OutputFormat.PNG],
        description=(
            "Formats delivered for EVERY image, regardless of what arrived. May be empty when "
            "`match_source` is true, which is how you ask for 'give me back exactly what I "
            "uploaded, in its own format, and nothing else'."
        ),
    )
    match_source: bool = Field(
        default=True,
        description=(
            "Also deliver each image in the format it arrived as. A PNG in yields a PNG out, a "
            "TIFF a TIFF, and so on. Camera raw cannot be written, so those deliver 16-bit TIFF "
            "and carry Note.FORMAT_SUBSTITUTED. `formats` is still honoured on top of this, so "
            "the delivered set is the union — set this false for a fixed format for every image."
        ),
    )
    profile: ColorProfile = ColorProfile.SRGB
    jpeg_quality: Annotated[int, Field(ge=1, le=100)] = 92
    webp_quality: Annotated[int, Field(ge=1, le=100)] = 90

    @field_validator("formats")
    @classmethod
    def _unique(cls, v: list[OutputFormat]) -> list[OutputFormat]:
        seen: list[OutputFormat] = []
        for f in v:                          # order-preserving dedupe; order is user intent
            if f not in seen:
                seen.append(f)
        return seen

    @model_validator(mode="after")
    def _something_is_delivered(self) -> "ExportSpec":
        """An empty `formats` is legal only when `match_source` fills it in.

        The old rule was a flat "must not be empty", which made "the same format I uploaded, and
        only that" impossible to express: `formats` needed at least one entry, so every image
        picked up an extra file nobody asked for — a PNG beside every PSD, doubling a 132-image
        order into 264 files.

        Both empty and false still has to be rejected, because it asks for no output at all.
        """
        if not self.formats and not self.match_source:
            raise ValueError(
                "export.formats is empty and match_source is false, so nothing would be "
                "delivered. Set match_source true to return each image in its own format, or "
                "name at least one format."
            )
        return self


# ---------------------------------------------------------------------------
# Job configuration
# ---------------------------------------------------------------------------


class JobConfig(StrictModel):
    """One upload, one config, one run. Tabs 1-5 map onto the fields below in order."""

    cutout: CutoutSpec = Field(default_factory=CutoutSpec)
    background: BackgroundSpec = Field(default_factory=BackgroundSpec)
    size: SizeSpec = Field(default_factory=SizeSpec)
    centring: CentringSpec = Field(default_factory=CentringSpec)
    psd: PsdSpec = Field(default_factory=PsdSpec)
    export: ExportSpec = Field(default_factory=ExportSpec)

    preset_name: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _cross_field_rules(self) -> JobConfig:
        fmts = set(self.export.formats)

        # JPEG has no alpha channel. Asking for a transparent JPEG is a contradiction, and
        # silently flattening it onto white is exactly the kind of surprise that discredits a
        # demo — so reject it at the boundary.
        if self.background.transparent and OutputFormat.JPEG in fmts:
            raise ValueError(
                "JPEG cannot carry transparency: either set background.transparent=false "
                "or drop 'jpeg' from export.formats"
            )

        # Keep the PSD tab and the format list from disagreeing about whether a PSD is wanted.
        if self.psd.enabled and OutputFormat.PSD not in fmts:
            raise ValueError("psd.enabled is true but 'psd' is missing from export.formats")
        if OutputFormat.PSD in fmts and not self.psd.enabled:
            raise ValueError("'psd' is in export.formats but psd.enabled is false")

        return self

    def config_hash(self) -> str:
        """Stable hash of the settings that affect output pixels.

        Used with the image hash for the cut-out cache and stamped on results so two assets can
        be compared for provenance. `preset_name` is excluded: renaming a preset must not
        invalidate a cache entry.
        """
        payload = self.model_dump(mode="json", exclude={"preset_name"})
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class ErrorInfo(StrictModel):
    code: ErrorCode
    message: str = Field(description="Human-readable, safe to display. Never contains a key.")
    retryable: bool = False


class EngineCandidate(StrictModel):
    """One engine's attempt, kept so the UI can show why a winner won."""

    engine: EngineId
    score: float | None = Field(default=None, description="Auto-pick score; higher is better.")
    soft_alpha_ratio: float | None = Field(
        default=None,
        description="Fraction of pixels with alpha in (0.05, 0.95). Binary-looking masks score low.",
    )
    rejected_reason: str | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    preview_url: str | None = None


class OutputAsset(StrictModel):
    format: OutputFormat
    url: str = Field(
        description=(
            "Short-lived signed URL, minted when this record is READ rather than when the asset "
            "was written — see `key`. Valid for SIGNED_URL_TTL_SECONDS from the response, not "
            "from whenever during the job the image happened to finish."
        )
    )
    key: str = Field(
        default="",
        description=(
            "Storage key the URL is signed from. Present so the URL can be re-minted on every "
            "read: a 400-image job runs far longer than the signed-URL TTL, so a URL frozen at "
            "write time is already dead by the time the results page renders. Empty only on "
            "records written before this field existed."
        ),
    )
    width: int
    height: int
    bytes: int
    profile: ColorProfile


class ImageResult(StrictModel):
    source_name: str
    state: ImageState

    outputs: list[OutputAsset] = Field(default_factory=list)
    notes: list[Note] = Field(
        default_factory=list,
        description="Non-fatal deviations from the request. The UI must render these.",
    )
    error: ErrorInfo | None = None

    chosen_engine: EngineId | None = None
    candidates: list[EngineCandidate] = Field(default_factory=list)

    # Measured, never estimated. Empty when the stage did not run.
    centroid_offset_px: tuple[float, float] | None = Field(
        default=None,
        description="Product centroid minus canvas centre, (dx, dy). Reported for both modes.",
    )
    background_uniformity: float | None = Field(
        default=None,
        description="0-1; drives the shadow gate. Low means a busy original background.",
    )
    source_size: tuple[int, int] | None = None
    preview_format: OutputFormat | None = Field(
        default=None,
        description=(
            "An extra output produced ONLY so the UI has something to display, set when no "
            "requested format is viewable in a browser (TIFF, EPS and PSD are not). It is a real "
            "asset in `outputs` but was not asked for, so it is excluded from the download "
            "bundle. None whenever a delivered format is already viewable."
        ),
    )
    layers: list[str] = Field(
        default_factory=list,
        description="Named object layers in the PSD, largest first. Only set for multi_object.",
    )
    source_format: SourceFormat | None = Field(
        default=None,
        description=(
            "The format this image arrived as. Paired with Note.FORMAT_SUBSTITUTED it tells the "
            "UI both halves of a swap — 'you sent CR2, we delivered TIFF'."
        ),
    )
    source_preview_url: str | None = Field(
        default=None,
        description=(
            "Small PNG of the file AS UPLOADED, for the UI's 'before' panel. Set only when the "
            "source format is one no browser can paint (EPS, PSD, TIFF, camera raw) — for PNG, "
            "JPEG, BMP and WebP the client shows the local file instead, which is free and full "
            "resolution. Signed on read like every other asset, and NOT a deliverable: it is "
            "excluded from the download bundle."
        ),
    )
    source_preview_key: str | None = Field(
        default=None, description="Storage key behind `source_preview_url`; re-signed on read."
    )

    cost_usd: float = 0.0
    duration_ms: int | None = None
    cache_hit: bool = False
    pipeline_version: str = PIPELINE_VERSION


class JobStatus(StrictModel):
    job_id: str
    state: JobState
    config_hash: str
    pipeline_version: str = PIPELINE_VERSION

    total: int = 0
    completed: int = 0
    failed: int = 0

    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    images: list[ImageResult] = Field(default_factory=list)
    error: ErrorInfo | None = None

    cost_usd: float = Field(default=0.0, description="Running vendor spend for this job.")
    bundle_url: str | None = Field(
        default=None, description="Signed zip of all outputs; present once COMPLETED."
    )
    bundle_key: str | None = Field(
        default=None,
        description="Storage key behind `bundle_url`, re-signed on read for the same reason.",
    )

    # --- bulk ---------------------------------------------------------------------------------
    batches: int = Field(
        default=0,
        description=(
            "Upload archives accepted for this job. A browser splits a large selection across "
            "several POSTs to keep its own memory bounded; each arrives as one archive."
        ),
    )
    upload_bytes: int = Field(
        default=0, description="Accumulated size of the accepted archives, against MAX_JOB_UPLOAD_BYTES."
    )
    accepting_uploads: bool = Field(
        default=False,
        description=(
            "True between the first batch and `POST /jobs/{id}/start`. While true the job is a "
            "draft: it has a config and some images, and nothing has been billed."
        ),
    )
    rate_limit_events: int = Field(
        default=0,
        description=(
            "Times a vendor throttled us during this job. Reported so a slow batch can be "
            "explained honestly rather than looking like the pipeline stalled."
        ),
    )
    images_total: int = Field(
        default=0,
        description=(
            "How many ImageResults this job has, independent of how many are in `images` — the "
            "poll response can omit or paginate them. Equal to len(images) on a full response."
        ),
    )

    @property
    def progress_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return round(100.0 * (self.completed + self.failed) / self.total, 1)


class ImagePage(StrictModel):
    """One page of `JobStatus.images`, from `GET /jobs/{id}/images`.

    Exists because polling a 400-image job every 1.2s for its full record moves megabytes per
    second for data the UI is not showing — the grid renders a page at a time.
    """

    job_id: str
    offset: int
    limit: int
    total: int
    images: list[ImageResult] = Field(default_factory=list)


class JobCreated(StrictModel):
    """POST /jobs response. Deliberately minimal — poll GET /jobs/{id} for everything else."""

    job_id: str
    state: Literal[JobState.QUEUED] = JobState.QUEUED
    total: int
    config_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    batches: int = Field(
        default=0, description="Archives accepted so far, including the one this call added."
    )
    accepting_uploads: bool = Field(
        default=False,
        description=(
            "True when this call sent `start=false`, meaning the job is holding for more batches. "
            "The client must finish with `POST /jobs/{id}/start` or the job never runs."
        ),
    )


class SampleFile(StrictModel):
    """One image the server can start a job from without an upload."""

    name: str
    size_bytes: int


class SampleList(StrictModel):
    """`GET /samples` response.

    Empty when `SAMPLES_DIR` is unset or missing — that is the signal for the UI to hide the
    option rather than an error, because a demo convenience must never break a deployment.

    `total_bytes` is published so the UI can show the real weight of what it is about to process.
    It matters here more than for an upload: the client's PSDs are ~130 MB each, and the whole
    reason these live server-side is that four of them in a browser tab is half a gigabyte.
    """

    files: list[SampleFile] = Field(default_factory=list)
    total_bytes: int = 0


class CostEstimate(StrictModel):
    """`POST /jobs/estimate` response.

    **Every figure here is an estimate**, and the field names say so. Vendor prices in this
    codebase are list prices recorded per engine (`BackgroundRemover.cost_per_image_usd`), not
    measured invoices, and the real total moves with cache hits, retries and per-object
    multiplication. It is published so a 400-image click is an informed one, not so it can be
    quoted at anybody — see the pricing note in `frontend/src/steps/UploadStep.tsx`.
    """

    images: int
    engines: list[EngineId]
    cost_per_image_usd: float
    estimated_cost_usd: float
    per_object_pricing: bool = Field(
        default=False,
        description=(
            "True when cutout.multi_object is on, in which case the real cost is this figure "
            "MULTIPLIED by the object count found in each scene — nine objects is nine calls. "
            "The estimate cannot know that count in advance, so it is a floor, not a total."
        ),
    )
    ceiling_usd: float = Field(
        default=0.0, description="MAX_JOB_COST_USD. The job stops here. 0 means no ceiling."
    )
    exceeds_ceiling: bool = False

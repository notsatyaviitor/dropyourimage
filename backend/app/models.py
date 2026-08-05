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
    PNG = "png"
    JPEG = "jpeg"
    TIFF = "tiff"
    WEBP = "webp"
    PSD = "psd"


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
    UNSUPPORTED_FILE = "unsupported_file"
    FILE_TOO_LARGE = "file_too_large"
    MALICIOUS_ARCHIVE = "malicious_archive"         # zip-slip / bomb guard tripped
    IMAGE_DECODE_FAILED = "image_decode_failed"
    PSD_UNAVAILABLE = "psd_unavailable"             # Adobe creds absent; see docs/PSD.md
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

    ALPHA_SUSPECT = "alpha_suspect"
    """Cut-out passed the reject rules but scored poorly. Worth a human look."""

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

    @model_validator(mode="after")
    def _engine_required_for_single(self) -> CutoutSpec:
        if self.strategy is EngineStrategy.SINGLE and self.engine is None:
            raise ValueError("cutout.engine is required when strategy is 'single'")
        return self


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
    formats: list[OutputFormat] = Field(default_factory=lambda: [OutputFormat.PNG])
    profile: ColorProfile = ColorProfile.SRGB
    jpeg_quality: Annotated[int, Field(ge=1, le=100)] = 92
    webp_quality: Annotated[int, Field(ge=1, le=100)] = 90

    @field_validator("formats")
    @classmethod
    def _non_empty_unique(cls, v: list[OutputFormat]) -> list[OutputFormat]:
        if not v:
            raise ValueError("export.formats must not be empty")
        seen: list[OutputFormat] = []
        for f in v:                          # order-preserving dedupe; order is user intent
            if f not in seen:
                seen.append(f)
        return seen


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
    url: str = Field(description="Short-lived signed URL; expires per SIGNED_URL_TTL_SECONDS.")
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

    @property
    def progress_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return round(100.0 * (self.completed + self.failed) / self.total, 1)


class JobCreated(StrictModel):
    """POST /jobs response. Deliberately minimal — poll GET /jobs/{id} for everything else."""

    job_id: str
    state: Literal[JobState.QUEUED] = JobState.QUEUED
    total: int
    config_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

"""Configuration from the environment. The only place secrets enter the process.

Nothing here is ever serialised into a response. `ErrorInfo.message` is built from typed errors
in `app.core.errors`, never from a settings value, so a key cannot leak through an error path.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import EngineId


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- segmentation engines -------------------------------------------------
    photoroom_api_key: str = ""
    removebg_api_key: str = ""
    fal_key: str = ""
    # remove.bg is deliberately absent: retired on accuracy, and at $0.20/image it was also the
    # most expensive engine by 10x. The adapter and its tests remain in the tree, unused, so
    # re-adding it here is the only change needed to bring it back.
    #
    # `gemini` is also absent, and that is load-bearing rather than an oversight. Its mask is a
    # rasterised polygon, which scores 0.7246 and passes every one of autopick's reject rules —
    # so in an AUTO pair it could out-score Photoroom while shipping a visibly worse edge. It is
    # reachable only through EngineStrategy.SINGLE, where an operator has chosen it knowingly.
    engine_pool: str = "photoroom,falai"

    # --- auto-pick tie-break --------------------------------------------------
    gemini_api_key: str = ""
    # NOT "gemini-3-flash" — that id does not exist. Verified live against listModels: the only
    # Gemini 3 Flash is this preview. gemini-2.5-flash is stable but measured as unable to
    # discriminate cut-out quality at all (answered "no difference" on an obvious 25px erosion).
    gemini_model: str = "gemini-3.5-flash"
    gemini_tiebreak_enabled: bool = True
    # Deliberately short. The tie-break is advisory and the deterministic score already has an
    # answer, so a slow vision call must degrade rather than hold up a batch. Measured 7-32s on
    # gemini-3-flash-preview, so this will sometimes cut it off — that is the intended trade.
    gemini_timeout_seconds: float = 15.0

    # --- Gemini as a segmentation engine (app/engines/gemini_segment.py) ------
    # Separate from `gemini_model` on purpose. One setting used to drive both the locator and the
    # tie-break, so tuning one silently retuned the other; segmentation would have made it three.
    #
    # Pinned to gemini-3.5-flash because the mask format differs by model, measured 7 Aug 2026:
    # 3.5-flash returns a polygon (which this engine can rasterise), 3.6-flash returns COCO
    # compressed RLE (which it cannot — it raises rather than guessing). Change this only
    # alongside gemini_segment.py's parser.
    gemini_segment_model: str = "gemini-3.5-flash"
    # NOT gemini_timeout_seconds. That one is short because the tie-break is advisory and must
    # degrade; this call *is* the mask, so it gets the same budget as any other vendor.
    gemini_segment_timeout_seconds: float = 60.0

    # --- Adobe (PSD) ----------------------------------------------------------
    adobe_client_id: str = ""
    adobe_client_secret: str = ""
    adobe_org_id: str = ""

    upscaler_enabled: bool = False

    # --- infrastructure -------------------------------------------------------
    redis_url: str = "redis://localhost:6379"
    queue_name: str = "dyi"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "dyi-poc"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    signed_url_ttl_seconds: int = 900

    # --- limits: security controls, not tuning knobs (see docs/SECURITY.md) ---
    max_zip_bytes: int = 524_288_000
    max_zip_entries: int = 200
    max_uncompressed_bytes: int = 2_147_483_648
    max_image_bytes: int = 52_428_800
    max_image_pixels: int = 80_000_000

    # Output-side counterpart to max_image_pixels. SizeSpec caps each axis at 20000, which leaves
    # 400 MP (~8 GB of float32 buffers) reachable from a tiny request body. See
    # pipeline._check_output_size.
    max_output_pixels: int = 80_000_000
    vendor_timeout_seconds: float = 60.0
    vendor_max_retries: int = 3
    worker_concurrency: int = 8

    cache_cutouts: bool = True
    log_level: str = "INFO"

    @field_validator(
        "photoroom_api_key",
        "removebg_api_key",
        "fal_key",
        "gemini_api_key",
        "adobe_client_id",
        "adobe_client_secret",
        "adobe_org_id",
        mode="after",
    )
    @classmethod
    def _reject_stray_comment_as_a_key(cls, v: str) -> str:
        """Catch `KEY=   # note` in a .env file.

        python-dotenv reads the comment as the value when the value is empty, so the app believes
        a key is configured, sends the comment text to the vendor, and gets a 401 that looks
        exactly like a genuinely bad key. Cheap to detect, expensive to debug.
        """
        stripped = v.strip()
        if stripped.startswith("#"):
            raise ValueError(
                "looks like a comment, not a key — in .env, put comments on their own line "
                "rather than after the value"
            )
        return stripped

    @field_validator("engine_pool")
    @classmethod
    def _known_engines_only(cls, v: str) -> str:
        known = {e.value for e in EngineId}
        for name in (p.strip() for p in v.split(",") if p.strip()):
            if name not in known:
                raise ValueError(f"unknown engine {name!r} in ENGINE_POOL; known: {sorted(known)}")
        return v

    # --- derived --------------------------------------------------------------

    @property
    def engine_priority(self) -> list[EngineId]:
        """Engines in configured priority order. The first two form the auto-pick pair."""
        return [EngineId(p.strip()) for p in self.engine_pool.split(",") if p.strip()]

    def key_for(self, engine: EngineId) -> str:
        return {
            EngineId.PHOTOROOM: self.photoroom_api_key,
            EngineId.REMOVEBG: self.removebg_api_key,
            EngineId.FALAI: self.fal_key,
            # Shares the one Gemini key with the locator and the tie-break; only the model id and
            # timeout are per-task.
            EngineId.GEMINI: self.gemini_api_key,
            EngineId.LOCAL: "n/a",
        }.get(engine, "")

    @property
    def adobe_configured(self) -> bool:
        """Whether the PSD stage can use the Photoshop API, or must run the fallback."""
        return bool(self.adobe_client_id and self.adobe_client_secret and self.adobe_org_id)

    def redacted(self) -> dict[str, object]:
        """Safe-to-log view. Used by the startup banner and the /health endpoint."""
        return {
            "engines_configured": [e.value for e in self.engine_priority if self.key_for(e)],
            # Reported separately from `engines_configured` because it is deliberately not in
            # ENGINE_POOL — it is selectable, but never auto-picked. See the engine_pool comment.
            "gemini_segment_selectable": bool(self.gemini_api_key),
            "gemini_tiebreak": bool(self.gemini_api_key),
            "adobe_psd": self.adobe_configured,
            "upscaler": self.upscaler_enabled,
            "queue": self.queue_name,
            "cache_cutouts": self.cache_cutouts,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

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
    huggingface_api_key: str = ""
    # briaai/RMBG-2.0 (the BiRefNet family). Per Hugging Face's own Inference Providers docs, this
    # model is served ONLY through the fal-ai provider — same underlying vendor FalAiEngine already
    # calls directly (http.py). Verified live against huggingface_hub 1.26.0: a real call routes to
    # https://router.huggingface.co/fal-ai/fal-ai/bria/background/remove.
    huggingface_model: str = "briaai/RMBG-2.0"
    huggingface_provider: str = "fal-ai"
    huggingface_timeout_seconds: float = 30.0
    engine_pool: str = "photoroom,falai,removebg,huggingface"

    # --- auto-pick tie-break --------------------------------------------------
    gemini_api_key: str = ""
    # NOT "gemini-3-flash" — that id does not exist. Verified live against listModels: the only
    # Gemini 3 Flash is this preview. gemini-2.5-flash is stable but measured as unable to
    # discriminate cut-out quality at all (answered "no difference" on an obvious 25px erosion).
    gemini_model: str = "gemini-3-flash-preview"
    gemini_tiebreak_enabled: bool = True
    # Deliberately short. The tie-break is advisory and the deterministic score already has an
    # answer, so a slow vision call must degrade rather than hold up a batch. Measured 7-32s on
    # gemini-3-flash-preview, so this will sometimes cut it off — that is the intended trade.
    gemini_timeout_seconds: float = 15.0

    # --- Gemini image-edit engine (EXPLICIT OVERRIDE of the project's prime directive) ---------
    # See docs/ENGINES.md "Gemini image-edit engine" section before touching any of this. Default
    # OFF: false + absent from ENGINE_POOL, so the deterministic dual-engine/local path is exactly
    # what runs unless this is deliberately switched on. To switch back to the pre-existing
    # behaviour, either flip this back to false or drop "gemini_edit" out of ENGINE_POOL — nothing
    # else needs to change.
    gemini_edit_enabled: bool = False
    # Not independently verified against a live listModels call the way GEMINI_MODEL was (see the
    # tie-break's own comment above) — confirm this id is current before enabling for real spend.
    gemini_edit_model: str = "gemini-2.5-flash-image"
    gemini_edit_timeout_seconds: float = 30.0

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
    vendor_timeout_seconds: float = 60.0
    vendor_max_retries: int = 3
    worker_concurrency: int = 8

    cache_cutouts: bool = True
    log_level: str = "INFO"

    @field_validator(
        "photoroom_api_key",
        "removebg_api_key",
        "fal_key",
        "huggingface_api_key",
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
            EngineId.LOCAL: "n/a",
            # Reuses GEMINI_API_KEY (the tie-break's key) rather than a separate credential — same
            # Google AI Studio key covers both, and `available()` additionally requires
            # gemini_edit_enabled, so this alone never turns the engine on.
            EngineId.GEMINI_EDIT: self.gemini_api_key,
            EngineId.HUGGINGFACE: self.huggingface_api_key,
        }.get(engine, "")

    @property
    def adobe_configured(self) -> bool:
        """Whether the PSD stage can use the Photoshop API, or must run the fallback."""
        return bool(self.adobe_client_id and self.adobe_client_secret and self.adobe_org_id)

    def redacted(self) -> dict[str, object]:
        """Safe-to-log view. Used by the startup banner and the /health endpoint."""
        return {
            "engines_configured": [e.value for e in self.engine_priority if self.key_for(e)],
            "gemini_tiebreak": bool(self.gemini_api_key),
            # Surfaced deliberately: this is a prime-directive override (see docs/ENGINES.md), so
            # /health must make it visible whenever it's actually live, not just log it quietly.
            "gemini_edit_engine": self.gemini_edit_enabled and bool(self.gemini_api_key),
            "adobe_psd": self.adobe_configured,
            "upscaler": self.upscaler_enabled,
            "queue": self.queue_name,
            "cache_cutouts": self.cache_cutouts,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

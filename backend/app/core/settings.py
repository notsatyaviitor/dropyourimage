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
    # `gemini` was absent here until 14 Aug 2026, because its mask is a rasterised polygon that
    # scores 0.7246 and passes every one of autopick's reject rules — in a pair it could out-score
    # a better engine while shipping a visibly worse edge.
    #
    # It is now deployable in an AUTO pair, but **only against an engine that beats it on the
    # deterministic score**, which was measured rather than assumed. On the studio fixture:
    #
    #     birefnet  0.9973   (99 distinct alpha values, 1307 soft edge pixels)
    #     gemini    0.7246   ( 2 distinct alpha values,    0 soft edge pixels)
    #
    # The 0.27 margin comes from `_W_EDGE` (0.55 of the score) measuring transition-band width,
    # where a hard polygon mask cannot compete. Pair gemini with anything that does NOT clear it
    # comfortably and the hazard in the paragraph above is live again — re-measure before changing
    # this line, do not reason about it.
    #
    # The shipped default stays photoroom-led on purpose. `ENGINE_POOL=gemini,birefnet` is right
    # for a deployment with a GPU and the weights installed; on a box without torch, birefnet is
    # unavailable and that pool silently degrades to gemini alone — the hard-edged engine, running
    # unopposed. Set it per deployment in `.env`, where the machine is known.
    engine_pool: str = "photoroom,falai"

    # --- automatic subject detection -----------------------------------------
    #
    # When `cutout.subject_prompt` is empty, enumerate the objects in the frame and crop to the
    # dominant one instead of segmenting whole-frame. Costs one Gemini call per image on top of
    # segmentation, on every image — about $0.0065 each, so roughly $2.60 on a 400-image order,
    # plus its latency. It also silently chooses between objects on an ambiguous scene, which is
    # why `SUBJECT_AUTO_AMBIGUOUS` exists and must not be swallowed by the UI.
    #
    # An operational switch rather than a `JobConfig` field on purpose: it changes how every image
    # in the deployment is treated, and the frozen API contract should not grow a flag to say
    # "behave the way this server is configured to behave".
    #
    # On an image showing several products this crops to one of them without asking. Naming the
    # subject in `cutout.subject_prompt` is the exact path and always wins.
    auto_subject_enabled: bool = True

    # --- self-hosted BiRefNet (app/engines/birefnet_local.py) -----------------
    #
    # The one engine in this codebase that is not a metered API call. Off unless
    # `BIREFNET_ENABLED=true`, because enabling it changes the deployment's requirements: torch
    # and its dependencies are ~2-3 GB installed, and without a GPU inference is measured in
    # seconds per image rather than Photoroom's ~0.8 s.
    birefnet_enabled: bool = False
    birefnet_model_id: str = "ZhengPeng7/BiRefNet"
    # Pinned, not floating on `main`. The model is loaded with `trust_remote_code=True`, which
    # executes modelling code straight from the Hub — pinning a revision is what makes that
    # auditable rather than "whatever was pushed this morning". Re-pin deliberately.
    birefnet_revision: str = "main"
    # "auto" picks CUDA when torch reports it, else CPU. Force with "cpu" or "cuda".
    birefnet_device: str = "auto"
    #: Square input the network sees. 1024 is BiRefNet's training resolution; smaller is faster
    #: and visibly softer. The mask is resized back to the source by `fit_alpha_to_source`.
    birefnet_input_size: int = 1024
    # Off by default so that neither `pytest` nor a cold container ever reaches the network on
    # its own. Warm the cache deliberately (see docs/ENGINES.md), then leave this false.
    birefnet_allow_download: bool = False

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

    # --- demo samples --------------------------------------------------------
    #
    # Directory of images the API can start a job from without a browser upload. The client's PSDs
    # are ~130 MB each, so pre-loading four into a tab is half a gigabyte fetched, held and sent
    # back; keeping them server-side makes that zero bytes over the wire.
    #
    # Empty (the default) turns the feature off entirely: `/samples` returns none and the UI hides
    # the option. A demo convenience must never be able to fail a deployment.
    samples_dir: str = ""

    # --- deployment ----------------------------------------------------------
    #
    # Comma-separated origins the browser may call this API from. Defaults to "*" so local
    # development and the test suite are unchanged, but a real deployment must name its frontend
    # origin: with no auth in the app itself, "*" means any page on the internet can drive an
    # endpoint that spends money.
    cors_allow_origins: str = "*"

    # Rough peak RSS for ONE image at `max_image_pixels`, used only by the startup sizing check.
    # Measured 4,435 MB for a real 50.6 MP DNG through the full Photoroom path (12 Aug 2026), so
    # ~88 MB per megapixel; 80 MP therefore lands near 7 GB. A deliberate over-estimate: the cost
    # of being wrong high is a warning to read, and of being wrong low is the OOM killer taking
    # out a paid job at 90%.
    memory_mb_per_megapixel: float = 88.0

    # Refuse to start when WORKER_CONCURRENCY x per-image memory exceeds what the machine has.
    # Set false on a box where the estimate does not apply (small images only, or a cgroup limit
    # this cannot see) — it downgrades to a warning rather than being silently ignored.
    enforce_memory_headroom: bool = True

    # --- Google Cloud Storage (alternative to S3/MinIO) -----------------------
    #
    # Setting `gcp_bucket_name` selects GCS over S3; nothing else has to change, because both sit
    # behind `StorageBackend` (app/storage/base.py). `S3_ENDPOINT_URL=memory` still wins over
    # both, since that is the explicit local/test escape hatch — see app/api/deps.py.
    #
    # `gcp_key_file` is a path to a service-account JSON, never the JSON itself: a private key in
    # an env var ends up in `docker inspect`, shell history and crash dumps. Left empty, the
    # client falls back to Application Default Credentials, which is the right answer on GCE/GKE
    # or Cloud Run where the instance already has an attached service account and no key file
    # needs to exist at all.
    gcp_project_id: str = ""
    gcp_bucket_name: str = ""
    gcp_key_file: str = ""

    # --- limits: security controls, not tuning knobs (see docs/SECURITY.md) ---
    #
    # These bound ONE uploaded archive. A bulk job arrives as several archives (see
    # `app/api/routes.py`'s chunked upload), so each of them is screened independently by exactly
    # the guards it always had — a batch is not a trusted caller just because an earlier batch of
    # the same job passed.
    # **Every byte cap here treats 0 as "no limit."** Same convention as `max_job_cost_usd`.
    max_zip_bytes: int = 1_073_741_824
    max_zip_entries: int = 200
    max_uncompressed_bytes: int = 4_294_967_296

    # Unlimited by default, deliberately, and this is the one limit here that is NOT a security
    # control — which is why it is the one that can be switched off.
    #
    # A byte count measures the container, not the threat. The client's PSDs are 130 MB and one is
    # 260 MB, all 45.4 MP; a 500 KB PNG can decode to 400 MP. So a byte cap refuses legitimate
    # source material while a real decompression bomb walks straight past it. Every value tried
    # here was wrong for someone: 50 MB rejected all 132 test images, 200 MB rejected the 260 MB
    # one — and each rejection cost a debugging round to trace back to a number in a config file.
    #
    # **`max_image_pixels` below is the actual guard and is unchanged.** It bounds what decode
    # allocates, which is what a bomb attacks. What removing the byte cap really does is make
    # available RAM the ceiling instead of a configured number, since an entry is held in memory
    # while it is read — see docs/SECURITY.md.
    max_image_bytes: int = 0
    max_image_pixels: int = 80_000_000

    # --- job-level limits: the bulk counterparts of the per-archive guards above -------------
    #
    # Per-archive caps bound a single upload; without these, N uploads against one job id would
    # multiply straight through them. Both are enforced on the *accumulated* job, in
    # `routes.py::_accept_batch`, at the point a new batch is appended.
    max_job_images: int = 500
    # 24 GB. Sized against the real thing rather than a round number: the client's PSD set is
    # 132 x 130 MB = 16.8 GB for ONE order, so the previous 4 GB refused it at about file 30.
    # This is storage, not memory — the worker holds one archive at a time (see app/jobs.py).
    max_job_upload_bytes: int = 25_769_803_776

    # Above this, memory mode refuses the job rather than running it inline in the request
    # handler. Processing 400 images takes minutes to tens of minutes, and a POST that blocks for
    # that long is a timeout with a half-finished job behind it, not a slow success. Small jobs
    # keep running inline so `pytest` and a keyless first run need no Redis and no MinIO — the
    # same trade `app/queue.py` documents. See `errors.BulkRequiresWorker`.
    inline_job_max_images: int = 25

    # Hard ceiling on one job's vendor spend. A 400-image batch on the dual-engine pool is ~$24,
    # against a POC budget of roughly $1,300/month — so a mis-clicked or runaway job is a real
    # fraction of it. The job stops and reports partial results rather than spending past this;
    # see `app/jobs.py::_budget_exhausted`. Set to 0 to disable.
    max_job_cost_usd: float = 25.0

    # Output-side counterpart to max_image_pixels. SizeSpec caps each axis at 20000, which leaves
    # 400 MP (~8 GB of float32 buffers) reachable from a tiny request body. See
    # pipeline._check_output_size.
    max_output_pixels: int = 80_000_000
    vendor_timeout_seconds: float = 60.0
    vendor_max_retries: int = 3

    # Largest payload we will send a segmentation vendor. Distinct from `max_image_bytes`, which
    # is about what WE accept — this is about what the vendor will.
    #
    # Measured against Photoroom: a 130 MB BMP was refused with a bare 413, while the same pixels
    # re-encoded to PNG (41.3 MB) were accepted and segmented. 48 MB sits above the known-good
    # figure and far below the known-bad one. Lower it if a vendor starts 413-ing; the pipeline
    # then re-encodes, and downscales only if re-encoding alone is not enough.
    vendor_max_upload_bytes: int = 50_331_648

    # Floor for that downscaling. Below this a mask stops being useful for a full-resolution
    # composite, so failing with the vendor's own error beats silently shipping a mask derived
    # from a thumbnail.
    vendor_min_long_edge_px: int = 1024

    # Parallel images within one job. **4, not the 8 this was**, because 8 was never safe at the
    # resolutions this pipeline accepts: one 50.6 MP image peaked at 4,435 MB resident (measured
    # 12 Aug 2026, real Photoroom path), so 8 implies ~35 GB in flight and the failure mode is the
    # OOM killer taking out a job the vendor has already been paid for.
    #
    # 4 is still not universally safe — it is a starting point that fits a 32 GB machine. The
    # startup check in `app/core/sizing.py` does the arithmetic against actual RAM and refuses,
    # naming a value that fits, rather than leaving it to be discovered under load.
    worker_concurrency: int = 4

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
            # Self-hosted: there is no key to configure, and `available()` answers the real
            # question (are the weights on disk) instead.
            EngineId.BIREFNET: "n/a",
        }.get(engine, "")

    @property
    def adobe_configured(self) -> bool:
        """Whether the PSD stage can use the Photoshop API, or must run the fallback."""
        return bool(self.adobe_client_id and self.adobe_client_secret and self.adobe_org_id)

    @property
    def storage_backend_name(self) -> str:
        """Which backend `app.api.deps.get_storage` will pick, in the same order it picks it.

        Kept here rather than in `deps` so `/health` and the startup banner can report it without
        constructing a client — asking the selector would mean connecting to the bucket just to
        answer a health check.
        """
        if self.s3_endpoint_url in ("", "memory"):
            return "memory"
        return "gcs" if self.gcp_bucket_name else "s3"

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
            # Which object store is actually live. Names only — never a bucket credential, a key
            # file path or an endpoint. Worth publishing because "the assets went somewhere else"
            # is otherwise invisible until a download 404s.
            "storage": self.storage_backend_name,
            "cache_cutouts": self.cache_cutouts,
            # Published so the browser sizes its upload batches from the server's actual limits
            # rather than a hardcoded guess that drifts out of step with them. `bulk_enabled`
            # answers the one question the UI has to ask before offering a 400-image upload:
            # is there a worker behind this, or will anything over `inline_max_images` be
            # refused? See frontend/src/api/client.ts::getLimits.
            "limits": {
                "max_batch_images": self.max_zip_entries,
                "max_batch_bytes": self.max_zip_bytes,
                "max_job_images": self.max_job_images,
                "max_job_upload_bytes": self.max_job_upload_bytes,
                # Published so an over-size file is caught in the picker rather than after it has
                # been uploaded and rejected. A 130 MB PSD reported back as a per-image result is
                # a round trip and a confusing error for something knowable before sending.
                "max_image_bytes": self.max_image_bytes,
                "inline_max_images": self.inline_job_max_images,
                "bulk_enabled": self.redis_url not in ("", "memory"),
                "max_job_cost_usd": self.max_job_cost_usd,
            },
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

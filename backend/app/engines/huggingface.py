"""Hugging Face Inference Providers — a normal commercial segmentation adapter.

Unlike `gemini_edit.py`, this is **not** an exception to the project's prime directive: background
removal via a commercial segmentation API is exactly what stage 1 is meant to be (root CLAUDE.md's
own "Legitimate AI uses" list). It targets `briaai/RMBG-2.0` (the BiRefNet family) — per Hugging
Face's own Inference Providers docs, that model is served ONLY through the `fal-ai` provider, which
is the same underlying vendor `FalAiEngine` (`http.py`) already calls directly. This adapter exists
for when billing through a Hugging Face token is preferred over a separate fal.ai account.

Uses the official `huggingface_hub` client rather than hand-rolled HTTP. Hugging Face's own docs
are explicit that non-chat tasks (image-segmentation included) route through provider-specific wire
formats that are only handled correctly by the official client libraries — the raw HTTP shape for
the `fal-ai` provider is not published. See docs/ENGINES.md for what was verified live before this
was written (the real routed URL and a live 401's exact shape), rather than assumed from docs.
"""

from __future__ import annotations

import time

import numpy as np
from huggingface_hub import AsyncInferenceClient
from huggingface_hub.errors import HfHubHTTPError, InferenceTimeoutError

from app.core import errors
from app.core.settings import Settings
from app.engines.base import AlphaResult, fit_alpha_to_source
from app.models import EngineId

# ⚠️ Estimate only. Hugging Face states Inference Providers pass through the underlying provider's
# rate with no markup, but the fal.ai rate for this specific routed endpoint
# (fal-ai/bria/background/remove) has not been independently confirmed here — check
# https://huggingface.co/docs/inference-providers/pricing before relying on this for real spend.
_ESTIMATED_COST_USD = 0.02


class HuggingFaceEngine:
    """Background removal via Hugging Face's Inference Providers, routed through fal-ai."""

    id = EngineId.HUGGINGFACE
    cost_per_image_usd = _ESTIMATED_COST_USD

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def available(self) -> bool:
        return bool(self._settings.huggingface_api_key)

    async def alpha_for(self, image_bytes: bytes, width: int, height: int) -> AlphaResult:
        if not self.available():
            raise errors.VendorUnauthorized(
                "No API key configured for huggingface. Set HUGGINGFACE_API_KEY in backend/.env."
            )

        started = time.perf_counter()
        client = AsyncInferenceClient(
            provider=self._settings.huggingface_provider,
            token=self._settings.huggingface_api_key,
            timeout=self._settings.huggingface_timeout_seconds,
        )
        try:
            results = await client.image_segmentation(
                image_bytes, model=self._settings.huggingface_model
            )
        except InferenceTimeoutError as exc:
            raise errors.VendorTimeout("Hugging Face did not respond in time.") from exc
        except HfHubHTTPError as exc:
            raise _classify(exc) from exc

        alpha = _pick_alpha(results)
        return AlphaResult(
            alpha=fit_alpha_to_source(alpha, width, height),
            engine=self.id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=self.cost_per_image_usd,
        )


def _classify(exc: HfHubHTTPError) -> errors.PipelineError:
    """Maps the real status code on `HfHubHTTPError` (verified live: `exc.response.status_code`)
    onto the same taxonomy `http.py` uses, so this engine's failures classify identically."""
    status = exc.response.status_code if exc.response is not None else 0
    if status == 429:
        return errors.VendorRateLimited("Hugging Face is rate-limiting requests; retrying shortly.")
    if status in (401, 402, 403):
        return errors.VendorUnauthorized("Hugging Face rejected our credentials or quota is exhausted.")
    if status >= 500:
        return errors.VendorError(f"Hugging Face returned a server error ({status}).")
    return errors.VendorError(f"Hugging Face rejected the request ({status}).")


def _pick_alpha(results: list) -> np.ndarray:
    """Turn `image_segmentation`'s output into one (H, W) float32 alpha.

    RMBG-2.0 is a background-removal-specific model, so a single-segment result is expected. This
    still defends against more than one (e.g. if `HUGGINGFACE_MODEL` is ever pointed at a general
    multi-class segmenter) by keeping the highest-confidence segment rather than crashing — but
    that multi-segment path is NOT independently verified against a real response, unlike the
    single-segment case (see docs/ENGINES.md).

    `mask` arrives as an already-decoded `PIL.Image` (mode "L") — the client library handles the
    base64 decode the raw API returns, confirmed via `inspect` against huggingface_hub 1.26.0.
    """
    if not results:
        raise errors.VendorError("Hugging Face returned no segmentation result.")
    best = max(results, key=lambda r: r.score if r.score is not None else 0.0)
    return (np.asarray(best.mask.convert("L"), dtype=np.float32) / 255.0).astype(np.float32)

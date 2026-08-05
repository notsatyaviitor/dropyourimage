"""Gemini image-edit engine — an EXPLICIT, deliberate override of this project's prime directive.

Root CLAUDE.md is unambiguous: *"Only stage 1 (segmentation) is an AI API call... Never route
[stages 2-5] through a generative or vision model"* — and even within stage 1, the sanctioned AI
uses are commercial segmentation APIs (`http.py`) plus a narrow, currently-unwired tie-break
(`vision.py`). A generative image editor was never on that list, for the exact reasons the
directive gives: it cannot return the same result twice, and it re-renders the pixels it touches.

This module exists anyway, at the maintainer's explicit request, made with full knowledge of that
conflict. It is wired so the override is opt-in and fully reversible, not a silent replacement:

* `GEMINI_EDIT_ENABLED` defaults to `false`. Unset (or false), this engine is never selected.
* It is never in `ENGINE_POOL` by default, so `registry.select_pool`'s AUTO strategy will not
  pick it up even if someone flips the flag without also editing the pool.
* It keeps only the vendor's ALPHA channel, discarding the colour Gemini regenerates (see
  `base.py`'s "an engine returns only alpha, never colour" design note) — this does not fix the
  non-determinism, but it does stop the regenerated pixels from reaching the final composite,
  which is the one piece of the directive's reasoning this integration *can* still honour.

See docs/ENGINES.md for the full rationale, verification status and how to switch back.
"""

from __future__ import annotations

import base64
import time

import httpx

from app.core import errors
from app.core.settings import Settings
from app.engines.base import AlphaResult, alpha_from_rgba_png
from app.models import EngineId

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# ⚠️ Not independently verified against a live call the way vision.py's tie-break model was (that
# one has a measured table in docs/ENGINES.md; this one does not, yet). Confirm the model id,
# `responseModalities` field name/casing, and the response's inline-image field path against
# https://ai.google.dev/gemini-api/docs/image-generation before relying on this for a real demo.
_PROMPT = (
    "Remove the background from this product photograph completely. Output the product only, "
    "on a fully transparent background, as a PNG. Do not add, remove, recolour, or otherwise "
    "alter any product pixel — change only what counts as background versus subject."
)

# ⚠️ Estimate only, not billed and verified the way the http.py adapters' costs are (those carry a
# "verified <date>" note). Gemini's image-output pricing is token-metered per output image, not a
# flat per-call rate quoted with confidence here — check https://ai.google.dev/gemini-api/docs/pricing
# before enabling this for anything with real spend attached.
_ESTIMATED_COST_USD = 0.04


class GeminiEditEngine:
    """Calls a Gemini image-capable model to remove the background; keeps only its alpha.

    Implements the same `BackgroundRemover` protocol as every other engine (`base.py`), so it
    slots into `registry.py` and `autopick.py` exactly like a commercial adapter — the rest of the
    pipeline has no idea this one is generative rather than a classifier/matting model.
    """

    id = EngineId.GEMINI_EDIT
    cost_per_image_usd = _ESTIMATED_COST_USD

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def available(self) -> bool:
        """Both the master flag AND a key must be present — see the module docstring."""
        return self._settings.gemini_edit_enabled and bool(self._settings.gemini_api_key)

    async def alpha_for(self, image_bytes: bytes, width: int, height: int) -> AlphaResult:
        if not self.available():
            raise errors.VendorUnauthorized(
                "Gemini image-edit engine is not enabled. Set GEMINI_EDIT_ENABLED=true and "
                "GEMINI_API_KEY in backend/.env — see docs/ENGINES.md before doing so."
            )

        started = time.perf_counter()
        png = await self._call(image_bytes)
        return AlphaResult(
            alpha=alpha_from_rgba_png(png, width, height),
            engine=self.id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=self.cost_per_image_usd,
        )

    async def _call(self, image_bytes: bytes) -> bytes:
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": _PROMPT},
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                # Deterministic as far as the API allows — it will still not be byte-reproducible
                # (this is the directive's second objection, and setting this to 0 does not
                # resolve it), but there is no reason to add extra sampling noise on top.
                "temperature": 0.0,
            },
        }

        url = _ENDPOINT.format(model=self._settings.gemini_edit_model)
        try:
            async with httpx.AsyncClient(timeout=self._settings.gemini_edit_timeout_seconds) as client:
                response = await client.post(url, params={"key": self._settings.gemini_api_key}, json=payload)
        except httpx.TimeoutException as exc:
            raise errors.VendorTimeout("Gemini did not respond in time.") from exc
        except httpx.HTTPError as exc:
            raise errors.VendorError("Could not reach the Gemini API.") from exc

        self._classify(response)

        image_bytes_out = _extract_image_bytes(response.json())
        if image_bytes_out is None:
            # Deliberately does not include the response body: it can echo request content
            # (matches vision.py's convention for the same reason).
            raise errors.VendorError(
                "Gemini's response contained no image part — check the model id and "
                "responseModalities against current docs (see docs/ENGINES.md)."
            )
        return image_bytes_out

    def _classify(self, response: httpx.Response) -> None:
        """Mirrors `http.py`'s status-code taxonomy so this engine's failures classify the same way."""
        status = response.status_code
        if status == 200:
            return
        if status == 429:
            raise errors.VendorRateLimited("Gemini is rate-limiting requests; retrying shortly.")
        if status in (401, 402, 403):
            raise errors.VendorUnauthorized("Gemini rejected our credentials or quota is exhausted.")
        if status >= 500:
            raise errors.VendorError(f"Gemini returned a server error ({status}).")
        raise errors.VendorError(f"Gemini rejected the request ({status}).")


def _extract_image_bytes(body: object) -> bytes | None:
    """Pull the first inline image out of a generateContent response.

    Tolerant by design, matching `vision.py::_parse_verdict`: accepts either casing for the
    inline-data field (`inlineData`/`inline_data`, `mimeType`/`mime_type`) since the exact casing
    returned by this specific response shape has not been independently confirmed here (see the
    module-level warning) — degrading to `None` on anything unexpected is far better than a raw
    KeyError leaking a vendor response shape into the pipeline.
    """
    if not isinstance(body, dict):
        return None
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None

    parts = (candidates[0].get("content") or {}).get("parts")
    if not isinstance(parts, list):
        return None

    for part in parts:
        if not isinstance(part, dict):
            continue
        inline = part.get("inlineData") or part.get("inline_data")
        if not isinstance(inline, dict):
            continue
        mime = inline.get("mimeType") or inline.get("mime_type") or ""
        data = inline.get("data")
        if isinstance(mime, str) and mime.startswith("image/") and isinstance(data, str):
            try:
                return base64.b64decode(data)
            except (ValueError, TypeError):
                return None
    return None

"""Commercial segmentation adapters: Photoroom, remove.bg, fal.ai.

⚠️ **Verify every endpoint, header and field name against current vendor documentation before
the bake-off.** The shapes below are the documented forms at time of writing, but vendor APIs
move and the client report itself warns that third-party pricing and API pages lag. Each adapter
keeps its wire details in module-level constants so a correction is a one-line change.

All three return a cut-out PNG; we keep only its alpha channel (see `base.py` for why).

Costs are per the client's AI Enablement Report, verified 4 Aug 2026. They are recorded per call
into the job's cost ledger, not used for routing.
"""

from __future__ import annotations

import time
from typing import Final

import httpx

from app.core import errors
from app.core.settings import Settings
from app.engines.base import AlphaResult, alpha_from_rgba_png
from app.models import EngineId

# --- wire details: check these first if an engine starts failing ------------------------
PHOTOROOM_URL: Final = "https://sdk.photoroom.com/v1/segment"
PHOTOROOM_KEY_HEADER: Final = "x-api-key"
PHOTOROOM_FILE_FIELD: Final = "image_file"

REMOVEBG_URL: Final = "https://api.remove.bg/v1.0/removebg"
REMOVEBG_KEY_HEADER: Final = "X-Api-Key"
REMOVEBG_FILE_FIELD: Final = "image_file"

# fal.ai runs many models behind one gateway; this one is a high-resolution matting model chosen
# for soft alpha quality. Swap the path to try another.
FALAI_URL: Final = "https://fal.run/fal-ai/birefnet/v2"


class _HttpEngine:
    """Shared retry, timeout and error-classification behaviour."""

    id: EngineId
    cost_per_image_usd: float = 0.0

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def available(self) -> bool:
        return bool(self._settings.key_for(self.id))

    async def alpha_for(self, image_bytes: bytes, width: int, height: int) -> AlphaResult:
        if not self.available():
            raise errors.VendorUnauthorized(
                f"No API key configured for {self.id.value}. Set it in backend/.env."
            )

        started = time.perf_counter()
        png = await self._call_with_retries(image_bytes)
        return AlphaResult(
            alpha=alpha_from_rgba_png(png, width, height),
            engine=self.id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=self.cost_per_image_usd,
        )

    async def _call_with_retries(self, image_bytes: bytes) -> bytes:
        """Retry only what retrying can fix.

        A 429 or 5xx is worth another attempt; a 401 or 400 will fail identically forever, and
        retrying it just burns the demo's time budget.
        """
        attempts = max(1, self._settings.vendor_max_retries)
        last: errors.PipelineError | None = None

        async with httpx.AsyncClient(timeout=self._settings.vendor_timeout_seconds) as client:
            for attempt in range(attempts):
                try:
                    return await self._request(client, image_bytes)
                except errors.PipelineError as exc:
                    last = exc
                    if not exc.retryable or attempt == attempts - 1:
                        raise
                    # Exponential backoff. Vendors send Retry-After too; honouring it properly is
                    # a follow-up, and at demo volume this is sufficient.
                    import asyncio

                    await asyncio.sleep(0.5 * (2**attempt))

        raise last or errors.VendorError("request failed with no recorded reason")

    async def _request(self, client: httpx.AsyncClient, image_bytes: bytes) -> bytes:
        raise NotImplementedError

    def _classify(self, response: httpx.Response) -> None:
        """Map an HTTP status onto the error taxonomy the UI can display."""
        status = response.status_code
        if status == 200:
            return
        if status == 429:
            raise errors.VendorRateLimited(
                f"{self.id.value} is rate-limiting requests; retrying shortly."
            )
        if status in (401, 402, 403):
            # 402 is 'out of credits' at several vendors — same user action, top up or swap key.
            raise errors.VendorUnauthorized(
                f"{self.id.value} rejected our credentials or the account has no credit."
            )
        if status >= 500:
            raise errors.VendorError(f"{self.id.value} returned a server error ({status}).")
        raise errors.VendorError(f"{self.id.value} rejected the request ({status}).")

    @staticmethod
    async def _send(client: httpx.AsyncClient, *args, **kwargs) -> httpx.Response:
        try:
            return await client.post(*args, **kwargs)
        except httpx.TimeoutException as exc:
            raise errors.VendorTimeout("The segmentation service did not respond in time.") from exc
        except httpx.HTTPError as exc:
            raise errors.VendorError("Could not reach the segmentation service.") from exc


class PhotoroomEngine(_HttpEngine):
    """Photoroom Remove Background. ~$0.02/image on the Basic plan — the volume engine."""

    id = EngineId.PHOTOROOM
    cost_per_image_usd = 0.02

    async def _request(self, client: httpx.AsyncClient, image_bytes: bytes) -> bytes:
        response = await self._send(
            client,
            PHOTOROOM_URL,
            headers={PHOTOROOM_KEY_HEADER: self._settings.photoroom_api_key},
            files={PHOTOROOM_FILE_FIELD: ("image.png", image_bytes, "image/png")},
        )
        self._classify(response)
        return response.content


class RemoveBgEngine(_HttpEngine):
    """remove.bg. ~$0.20/image — the premium half of the auto-pick pair."""

    id = EngineId.REMOVEBG
    cost_per_image_usd = 0.20

    async def _request(self, client: httpx.AsyncClient, image_bytes: bytes) -> bytes:
        response = await self._send(
            client,
            REMOVEBG_URL,
            headers={REMOVEBG_KEY_HEADER: self._settings.removebg_api_key},
            files={REMOVEBG_FILE_FIELD: ("image.png", image_bytes, "image/png")},
            # size=full so the mask comes back at source resolution rather than being capped;
            # base.fit_alpha_to_source would otherwise be upscaling a small mask.
            data={"size": "full", "format": "png"},
        )
        self._classify(response)
        return response.content


class FalAiEngine(_HttpEngine):
    """fal.ai hosted matting. ~$0.03-0.05/image, strong soft alpha.

    Takes a data URI rather than a hosted URL so nothing has to be publicly readable first —
    which also means no image leaves our control except to the vendor itself.
    """

    id = EngineId.FALAI
    cost_per_image_usd = 0.04

    async def _request(self, client: httpx.AsyncClient, image_bytes: bytes) -> bytes:
        import base64

        data_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        response = await self._send(
            client,
            FALAI_URL,
            headers={
                "Authorization": f"Key {self._settings.fal_key}",
                "Content-Type": "application/json",
            },
            json={"image_url": data_uri},
        )
        self._classify(response)

        payload = response.json()
        url = _first_image_url(payload)
        if not url:
            raise errors.VendorError("fal.ai response contained no image URL.")

        fetched = await client.get(url)
        if fetched.status_code != 200:
            raise errors.VendorError("Could not download the fal.ai result image.")
        return fetched.content


def _first_image_url(payload: object) -> str | None:
    """Pull an image URL out of fal.ai's response.

    Kept tolerant on purpose: the gateway's envelope differs between models (`image.url`,
    `images[0].url`), and a demo should not fall over because a model returns a list.
    """
    if not isinstance(payload, dict):
        return None
    image = payload.get("image")
    if isinstance(image, dict) and isinstance(image.get("url"), str):
        return image["url"]
    images = payload.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict) and isinstance(first.get("url"), str):
            return first["url"]
    if isinstance(payload.get("url"), str):
        return payload["url"]
    return None

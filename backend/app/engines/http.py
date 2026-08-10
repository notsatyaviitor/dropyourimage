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
from app.engines import throttle
from app.engines.base import AlphaResult, alpha_from_rgba_png
from app.models import EngineId

# --- wire details: check these first if an engine starts failing ------------------------
PHOTOROOM_URL: Final = "https://sdk.photoroom.com/v1/segment"
PHOTOROOM_KEY_HEADER: Final = "x-api-key"
PHOTOROOM_FILE_FIELD: Final = "image_file"

REMOVEBG_URL: Final = "https://api.remove.bg/v1.0/removebg"
REMOVEBG_KEY_HEADER: Final = "X-Api-Key"
REMOVEBG_FILE_FIELD: Final = "image_file"

# "auto" returns the highest resolution the plan allows. The older `full` alias still works but is
# deprecated and caps at 25 MP; `auto` is the documented current value. Resolution matters here
# because `base.fit_alpha_to_source` would otherwise be upscaling a smaller mask over
# full-resolution pixels, which misaligns the edge by a few pixels and looks like a bad cut-out.
REMOVEBG_SIZE: Final = "auto"

# `size=preview` returns up to 0.25 MP. Below that ceiling it is measurably *identical* to `auto`: a
# 268x142 subject crop requested at `preview` came back 268x142 — full source resolution, nothing
# upscaled. So on a small image `auto` asks for something `preview` already gives.
#
# What this does NOT do, despite an earlier claim in this file: reliably save a paid credit. Observed
# on a live account with 0 paid credits, a 0.53 MP `auto` request succeeded and decremented the free
# call allowance — the account's `api.sizes` field reports "all", so free calls are not restricted to
# preview there. Billing appears to depend on plan, and this codebase has not established the rule.
# The defensible reasons to keep it are the ones actually verified: identical output below the
# ceiling, and a smaller response to transfer and decode.
#
# Kept conservative — if a response ever does come back smaller than the source,
# `base.fit_alpha_to_source` resizes it, so a wrong guess degrades quality slightly rather than
# breaking.
REMOVEBG_PREVIEW_MAX_PIXELS: Final = 250_000
REMOVEBG_PREVIEW_SIZE: Final = "preview"

# This is a packshot pipeline, so tell remove.bg that rather than making it guess. Valid values:
# auto | person | product | car | animal | graphic | transportation. Change this one line if the
# demo set stops being products.
REMOVEBG_TYPE: Final = "product"

# Semitransparency defaults to true at the vendor and we want it: soft alpha is preserved end to
# end here (invariant 2), so a matte that resolves partial coverage is strictly better input.
# Stated explicitly so a change in the vendor's default cannot silently harden our edges.
REMOVEBG_SEMITRANSPARENCY: Final = "true"

# fal.ai runs many models behind one gateway; this one is a high-resolution matting model chosen
# for soft alpha quality. Swap the path to try another.
FALAI_URL: Final = "https://fal.run/fal-ai/birefnet/v2"

# Ceiling on an honoured Retry-After. A vendor is free to ask for minutes; a demo cannot wait that
# long, and it is better to fail one image loudly than to stall the batch.
_MAX_RETRY_AFTER_S: Final = 30.0


# Vendor error codes meaning "there is nothing here to cut out". remove.bg returns
# `unknown_foreground`; the others are defensive spellings so a rename degrades to a generic error
# rather than a wrong one.
_NO_FOREGROUND_CODES: Final = frozenset(
    {"unknown_foreground", "no_foreground", "foreground_not_found"}
)


def _vendor_error_code(response: httpx.Response) -> str:
    """Pull the machine-readable code out of a vendor error body, tolerantly.

    remove.bg returns ``{"errors": [{"title": ..., "code": "unknown_foreground"}]}``. Anything
    unexpected yields "" so the caller falls back to a generic error — a body we cannot parse must
    never become a misclassified error.
    """
    try:
        body = response.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    items = body.get("errors")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return ""
    return str(items[0].get("code", ""))


def size_for(image_bytes: bytes) -> str:
    """Smallest remove.bg `size` that still returns the mask at full source resolution.

    Returns ``preview`` when the image fits inside its 0.25 MP ceiling, where it is measurably
    identical to ``auto``. Above the ceiling ``auto`` is required, or the mask comes back downscaled
    and has to be upscaled over full-resolution pixels, which visibly misaligns the cut-out edge.

    Falls back to ``auto`` when the dimensions cannot be read: requesting more than needed is the
    safe error, since the alternative is silently degrading every mask.
    """
    from app.imaging import export as E

    try:
        width, height = E.dimensions_of(image_bytes)
    except Exception:
        return REMOVEBG_SIZE
    return REMOVEBG_PREVIEW_SIZE if width * height <= REMOVEBG_PREVIEW_MAX_PIXELS else REMOVEBG_SIZE


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse `Retry-After`, which may be a delta in seconds or an HTTP date.

    Returns None when absent or unparseable, so the caller falls back to exponential backoff. A
    malformed header must never be the reason an image fails.
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return None

    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    from email.utils import parsedate_to_datetime

    try:
        target = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None

    from datetime import datetime, timezone

    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())


class _HttpEngine:
    """Shared retry, timeout and error-classification behaviour."""

    id: EngineId
    cost_per_image_usd: float = 0.0

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def available(self) -> bool:
        return bool(self._settings.key_for(self.id))

    @property
    def _timeout_seconds(self) -> float:
        """Per-request timeout. Overridable so an engine with its own budget can say so."""
        return self._settings.vendor_timeout_seconds

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

        Every attempt runs inside `throttle.slot()`. That is what makes a 429 discovered by one
        image slow the *whole job* rather than only this call — see `app/engines/throttle.py` for
        why per-call backoff alone collapses at batch scale. With no governor installed (a unit
        test, or a direct `process_image` call) the slot is a no-op and this behaves exactly as it
        did before.
        """
        attempts = max(1, self._settings.vendor_max_retries)
        last: errors.PipelineError | None = None

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            for attempt in range(attempts):
                try:
                    async with throttle.slot(self.id.value):
                        result = await self._request(client, image_bytes)
                except errors.PipelineError as exc:
                    last = exc
                    if isinstance(exc, errors.VendorRateLimited):
                        await throttle.report_rate_limited(
                            self.id.value, getattr(exc, "retry_after_seconds", None)
                        )
                    if not exc.retryable or attempt == attempts - 1:
                        raise

                    import asyncio

                    # Prefer the vendor's own Retry-After over our guess — it knows its limit and
                    # we do not. Capped so one throttled image cannot stall a whole batch past the
                    # per-vendor timeout budget.
                    hinted = getattr(exc, "retry_after_seconds", None)
                    backoff = 0.5 * (2**attempt)
                    delay = min(hinted, _MAX_RETRY_AFTER_S) if hinted is not None else backoff
                    await asyncio.sleep(delay)
                else:
                    await throttle.report_success(self.id.value)
                    return result

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
                f"{self.id.value} is rate-limiting requests; retrying shortly.",
                retry_after_seconds=_retry_after_seconds(response),
            )
        if status == 402:
            # Distinct from 401/403 on purpose: the key is fine, the account is empty. Reported as
            # itself because "top up the account" and "fix the key" are different actions, and
            # remove.bg returns 402 with {"code": "insufficient_credits"} for exactly this.
            raise errors.VendorOutOfCredits(
                f"{self.id.value} accepted the key but the account is out of credits — top it up."
            )
        if status in (401, 403):
            raise errors.VendorUnauthorized(
                f"{self.id.value} rejected our credentials. Check the key in backend/.env."
            )
        if status == 413:
            # Called out rather than left to the generic VendorError below, which is retryable —
            # and retrying a 413 re-sends identical bytes for an identical refusal. See
            # `errors.VendorPayloadTooLarge` and `pipeline._encode_under_budget`.
            raise errors.VendorPayloadTooLarge(
                f"{self.id.value} refused the upload as too large. Lower "
                "VENDOR_MAX_UPLOAD_BYTES so the image is re-encoded smaller before sending."
            )
        if status >= 500:
            raise errors.VendorError(f"{self.id.value} returned a server error ({status}).")

        # A 400 carrying a machine-readable reason is worth reading. `unknown_foreground` means the
        # engine found no figure/ground separation — deterministic, so retrying wastes the rate
        # budget and reports a generic error at the end of it.
        if _vendor_error_code(response) in _NO_FOREGROUND_CODES:
            raise errors.NoForegroundFound(
                f"{self.id.value} could not find a subject to cut out in this image. On a cropped "
                "subject, try a larger padding or a different subject description."
            )
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
    """remove.bg. The premium half of the auto-pick pair.

    Returns the default `channels=rgba` cut-out PNG and we keep only its alpha. Requesting
    `channels=alpha` instead would halve the payload, but it returns a single-channel matte — which
    `alpha_from_rgba_png` would read as fully opaque, since the matte lands in RGB rather than in
    the alpha channel. Not worth a second decode path at demo volume; see docs/ENGINES.md.

    `cost_per_image_usd` is a ledger figure, not a routing input. remove.bg bills in credits whose
    unit price depends on plan and volume, so set this to the actual per-image cost of your plan if
    the reported spend needs to be accurate.
    """

    id = EngineId.REMOVEBG
    cost_per_image_usd = 0.20

    async def _request(self, client: httpx.AsyncClient, image_bytes: bytes) -> bytes:
        response = await self._send(
            client,
            REMOVEBG_URL,
            headers={REMOVEBG_KEY_HEADER: self._settings.removebg_api_key},
            files={REMOVEBG_FILE_FIELD: ("image.png", image_bytes, "image/png")},
            data={
                "size": size_for(image_bytes),
                "type": REMOVEBG_TYPE,
                "semitransparency": REMOVEBG_SEMITRANSPARENCY,
                "format": "png",
            },
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

"""Adobe Photoshop API client — the primary path for PSD delivery, when it is available.

⚠️ **Never exercised against the real API in this build.** No Adobe credentials were provided
(`ADOBE_CLIENT_ID` / `ADOBE_CLIENT_SECRET` / `ADOBE_ORG_ID` are all blank — see
`Settings.adobe_configured`), so nothing below has been run against Adobe's servers. The shapes
here — the IMS token endpoint, the Photoshop API base URL, the document-manipulation request
body — are transcribed from Adobe's published Firefly Services / Photoshop API documentation at
the time of writing. **Verify every endpoint, field name and auth flow against current Adobe docs
before this is used against a real account** — Adobe's own docs warn their APIs move, and this
client has zero live-traffic confidence behind it. Treat it as a well-informed first draft, not a
tested integration.

Architecture, once credentials exist: upload the three finished layers (product cut-out, shadow,
background fill) plus the hand-authored `templates/base.psd` template (named `PROD`/`SHADOW`/`BG`/
`PATH` groups already in place, per the client report's Feature 2 convention) to Adobe's storage,
call the Photoshop API's document-manipulation endpoint to place each layer's pixels into its
matching template group and to run a Make Work Path action against the alpha channel for the
vector path, then download the result. That work is real Photoshop compute, so it produces a
genuine vector path with no approximation — unlike `vector_path.py`'s straight-line fallback.
"""

from __future__ import annotations

import time

import httpx

from app.core import errors
from app.core.settings import Settings

# Adobe Identity Management System — OAuth2 client-credentials token endpoint.
_IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
_IMS_SCOPE = "openid,AdobeID,firefly_api,ff_apis"

# Photoshop API (Firefly Services). Base path per Adobe's published Photoshop API docs.
_PHOTOSHOP_API_BASE = "https://image.adobe.io/pie/psdService"


class PhotoshopApiClient:
    """Calls Adobe's Photoshop API to assemble a layered PSD from finished pixel layers.

    Every method raises `app.core.errors.PsdUnavailable` immediately if credentials are not
    configured, so a caller never has to guess whether a network call is about to happen.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def available(self) -> bool:
        return self._settings.adobe_configured

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        """Client-credentials OAuth2 token from Adobe IMS, cached until near expiry.

        ⚠️ Unverified shape: Adobe's IMS has changed its supported grant types and scope strings
        across API generations. Confirm this against the credentials' actual onboarding docs —
        Adobe issues different setup instructions per product integration.
        """
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token

        response = await client.post(
            _IMS_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._settings.adobe_client_id,
                "client_secret": self._settings.adobe_client_secret,
                "scope": _IMS_SCOPE,
            },
        )
        if response.status_code != 200:
            raise errors.VendorUnauthorized(
                "Adobe rejected the IMS token request — check ADOBE_CLIENT_ID/SECRET."
            )

        body = response.json()
        self._token = body["access_token"]
        # Refresh a minute early rather than exactly at expiry.
        self._token_expires_at = time.monotonic() + max(0, int(body.get("expires_in", 3600)) - 60)
        return self._token

    async def build_layered_psd(
        self,
        *,
        product_png: bytes,
        shadow_png: bytes | None,
        background_png: bytes,
        template_psd: bytes,
        layer_names: dict[str, str],
        trace_vector_path: bool,
    ) -> bytes:
        """Assemble a layered PSD from finished layer pixels via the Photoshop API.

        Raises `errors.PsdUnavailable` if not configured, `errors.VendorError` /
        `errors.VendorTimeout` / `errors.VendorRateLimited` on a call failure — the same taxonomy
        every other vendor adapter in this codebase uses, so the API layer needs no special case
        for PSD failures.

        ⚠️ The actual multipart/JSON request shape for Photoshop API's document-manipulation
        endpoint (which fields carry the uploaded assets, how a target layer group is addressed,
        how "run this action" is expressed) has not been confirmed against a live account. This
        method's body is a placeholder shaped like Adobe's documented pattern — replace it once
        real credentials exist and the first live call's actual response shape is known.
        """
        if not self.available():
            raise errors.PsdUnavailable(
                "Adobe Photoshop API is not configured (ADOBE_CLIENT_ID/SECRET/ORG_ID unset). "
                "Falling back to the pytoshop-based PSD writer — see docs/PSD.md."
            )

        async with httpx.AsyncClient(timeout=self._settings.vendor_timeout_seconds) as client:
            token = await self._access_token(client)
            headers = {
                "Authorization": f"Bearer {token}",
                "x-api-key": self._settings.adobe_client_id,
            }

            try:
                response = await client.post(
                    f"{_PHOTOSHOP_API_BASE}/documentManipulation",
                    headers=headers,
                    files={
                        "template": ("base.psd", template_psd, "image/vnd.adobe.photoshop"),
                        "product": ("product.png", product_png, "image/png"),
                        "background": ("background.png", background_png, "image/png"),
                        **(
                            {"shadow": ("shadow.png", shadow_png, "image/png")}
                            if shadow_png is not None
                            else {}
                        ),
                    },
                    data={
                        "layerNames": layer_names,
                        "traceVectorPath": str(trace_vector_path).lower(),
                    },
                )
            except httpx.TimeoutException as exc:
                raise errors.VendorTimeout(
                    "The Photoshop API did not respond in time."
                ) from exc
            except httpx.HTTPError as exc:
                raise errors.VendorError("Could not reach the Photoshop API.") from exc

            if response.status_code == 429:
                raise errors.VendorRateLimited("The Photoshop API is rate-limiting requests.")
            if response.status_code in (401, 403):
                raise errors.VendorUnauthorized("The Photoshop API rejected our credentials.")
            if response.status_code != 200:
                raise errors.VendorError(
                    f"The Photoshop API returned an error (HTTP {response.status_code})."
                )

            return response.content

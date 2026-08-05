"""Vision tie-break for auto-pick — the one place a model exercises judgement.

Only called when the deterministic reject rules and geometric score in `autopick.py` cannot
separate two candidate masks. That keeps it rare and cheap, and it matches the role the client's
AI Enablement Report assigns Gemini 3 Flash in Feature 1: genuine judgement, not measurement.

The model never sees the pipeline's settings and never influences colour, geometry or output —
it returns one of two engine names, and nothing else.

Measured behaviour (counterbalanced A/B on a deliberately eroded mask, see docs/ENGINES.md)
--------------------------------------------------------------------------------------------
* ``gemini-3-flash-preview`` — 2/2 correct in both orderings, accurate reasons, 7-32s per call.
* ``gemini-2.5-flash`` — answered "no difference" on an obvious 25px erosion, 0/2 useful.
* The deterministic scorer in `autopick.py` also got 2/2, at 0ms and no cost.

Two consequences. First, ``GEMINI_MODEL`` defaults to the preview, because it is the only id that
works — the client report's ``gemini-3-flash`` does not exist in the API at all, and the stable
2.5-flash cannot do the task. A preview dependency is a real risk to flag, not to hide. Second,
the tie-break is genuinely optional: it fires only on near-ties, carries a short timeout, and
degrades to the deterministic answer rather than delaying a batch.
"""

from __future__ import annotations

import base64
import json

import httpx
import numpy as np

from app.core.settings import Settings
from app.imaging import color as C
from app.imaging import composite as X
from app.imaging import export as E
from app.models import EngineId, OutputFormat

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Mid grey, chosen so both a bright halo and a dark fringe are visible against it. A white or
# black backdrop would hide one of the two failure modes we are asking about.
_REVIEW_BACKGROUND = "#808080"

# Long edge of the preview sent to the model. Small on purpose: the judgement is about edge
# quality and obvious mistakes, and tokens scale with area.
_PREVIEW_LONG_EDGE = 768

_PROMPT = """Two automated background-removal masks were applied to the same product photograph.
Both results are shown composited onto plain mid-grey so that edge defects are visible.

Compare them closely and decide which mask is better. Check specifically:
- Is any part of the product missing, shaved off, or eaten into at the edge?
- Is any background left behind that should have been removed?
- Is there a pale or dark halo, or a rim of leftover background colour, around the product?
- Are naturally soft edges kept soft, rather than cut hard like scissors?

The two images are the same photograph, so any difference in the product's outline or thickness is
a difference in mask quality, not in framing. Look carefully before concluding they are identical;
the differences can be subtle.

Answer with the label of the better mask, "A" or "B", and a reason of at most 12 words. If after
close comparison they really are equivalent, answer "equivalent".
"""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        # "equivalent" is offered explicitly so the model has an honest way out. Without it, a
        # model that cannot see a difference still has to name one, which is how position bias
        # gets in — measured at 100% A-preference on gemini-2.5-flash before this was added.
        "better": {"type": "string", "enum": ["A", "B", "equivalent"]},
        "reason": {"type": "string"},
    },
    "required": ["better"],
}


class TiebreakUnavailable(Exception):
    """No key configured, or the vision call failed. Callers fall back to the deterministic score."""


class GeminiTiebreak:
    """Asks a vision model which of two cut-outs is better. Advisory only."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def available(self) -> bool:
        return bool(self._settings.gemini_api_key) and self._settings.gemini_tiebreak_enabled

    async def pick(
        self,
        rgb_linear: np.ndarray,
        candidates: list[tuple[EngineId, np.ndarray]],
    ) -> tuple[EngineId, str]:
        """Return the winning engine and the model's stated reason.

        `candidates` is exactly two ``(engine, alpha)`` pairs. Raises `TiebreakUnavailable` on any
        problem — a tie-break failing must never fail the image, since a deterministic answer
        already exists.
        """
        if not self.available():
            raise TiebreakUnavailable("no GEMINI_API_KEY configured")
        if len(candidates) != 2:
            raise TiebreakUnavailable(f"expected 2 candidates, got {len(candidates)}")

        previews = [self._review_preview(rgb_linear, alpha) for _, alpha in candidates]

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": _PROMPT},
                        {"text": "Cut-out A:"},
                        {"inline_data": {"mime_type": "image/png", "data": previews[0]}},
                        {"text": "Cut-out B:"},
                        {"inline_data": {"mime_type": "image/png", "data": previews[1]}},
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
                # Deterministic as far as the API allows, so a demo re-run does not flip.
                "temperature": 0.0,
                "maxOutputTokens": 2048,
            },
        }

        url = _ENDPOINT.format(model=self._settings.gemini_model)
        try:
            async with httpx.AsyncClient(timeout=self._settings.gemini_timeout_seconds) as client:
                response = await client.post(
                    url,
                    params={"key": self._settings.gemini_api_key},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise TiebreakUnavailable(f"vision request failed: {type(exc).__name__}") from exc

        if response.status_code != 200:
            # Deliberately does not include the body: it can echo request content.
            raise TiebreakUnavailable(f"vision call returned HTTP {response.status_code}")

        verdict = _parse_verdict(response.json())
        if verdict is None:
            raise TiebreakUnavailable("vision response was not usable")

        better, reason = verdict
        if better == "equivalent":
            # An honest "cannot tell" is more useful than a coin flip. Fall back to the
            # deterministic score rather than inventing a preference.
            raise TiebreakUnavailable(f"model saw no difference: {reason}")

        return candidates[0 if better == "A" else 1][0], reason

    @staticmethod
    def _review_preview(rgb_linear: np.ndarray, alpha: np.ndarray) -> str:
        """Composite a candidate onto mid grey and return base64 PNG."""
        bg = C.hex_to_srgb_linear(_REVIEW_BACKGROUND)
        composited, _ = X.flatten(rgb_linear, alpha, background_linear=bg)

        h, w = alpha.shape
        scale = min(1.0, _PREVIEW_LONG_EDGE / max(h, w))
        if scale < 1.0:
            import cv2

            composited = cv2.resize(
                composited,
                (max(1, int(w * scale)), max(1, int(h * scale))),
                interpolation=cv2.INTER_AREA,
            )

        png = E.encode(composited, None, fmt=OutputFormat.PNG, embed_profile=False)
        return base64.b64encode(png).decode("ascii")


def _parse_verdict(body: object) -> tuple[str, str] | None:
    """Pull ``(better, reason)`` out of a generateContent response.

    Tolerant by design: a schema-constrained response should be clean JSON, but a tie-break is
    advisory, so anything unexpected degrades to "no answer" rather than raising into the pipeline.
    """
    if not isinstance(body, dict):
        return None
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None

    parts = (candidates[0].get("content") or {}).get("parts")
    if not isinstance(parts, list):
        return None

    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text.strip():
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None

    better = parsed.get("better")
    if better not in ("A", "B", "equivalent"):
        return None
    return better, str(parsed.get("reason", ""))[:120]

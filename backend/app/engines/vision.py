"""Vision tie-break for auto-pick — the one place a model exercises judgement.

Only called when the deterministic reject rules and geometric score in `autopick.py` cannot
separate two candidate masks. That keeps it rare and cheap, and it matches the role the client's
AI Enablement Report assigns Gemini 3 Flash in Feature 1: genuine judgement, not measurement.

The model never sees the pipeline's settings and never influences colour, geometry or output —
it returns one of two engine names, and nothing else.

Measured behaviour (counterbalanced A/B, 4 trials per model: 25px and 12px erosion x both
presentation orders, run 6 Aug 2026 against a live key — see docs/ENGINES.md)
--------------------------------------------------------------------------------------------
* ``gemini-3.5-flash`` — **3/4**. Correct in both orders at 25px; missed the 12px case one way.
* ``gemini-3.6-flash`` — 2/4. Order-independent, but missed the subtler 12px erosion both ways.
* ``gemini-3-flash-preview`` — 2/4, and **the 2 are an artefact**: it chose whichever mask was
  shown first in all 4 trials. That is pure position bias, not partial competence.
* ``gemini-2.5-flash`` — answered "no difference" on an obvious 25px erosion.
* The deterministic scorer in `autopick.py` gets these right at 0 ms and no cost.

Three consequences, all load-bearing:

1. ``GEMINI_MODEL`` defaults to ``gemini-3.5-flash``: best measured, and *stable* rather than a
   preview. The client report's ``gemini-3-flash`` does not exist in the API at all.
2. **Comparisons are counterbalanced** — see `pick`. Position bias was measurable, so a single call
   cannot be trusted; a winner is returned only when both orderings agree.
3. The tie-break stays genuinely optional. Even the best model here is 3/4 on a deliberately
   obvious defect, so it is advisory: it fires only on near-ties, where the deterministic scorer
   has already established the two masks are close to equivalent, and it degrades to that answer on
   any disagreement, timeout or error. It must never be presented as a quality measurement.
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

        **The comparison is counterbalanced.** The same pair is judged twice, once in each
        presentation order, and a winner is only returned when both runs name the same engine.
        This is not belt-and-braces: position bias was *measured* here, and it was the dominant
        failure mode. On a 25px erosion, `gemini-3-flash-preview` picked whichever mask was shown
        first in 4/4 trials — 2/4 overall accuracy that looks like partial competence but is
        actually no signal at all. Disagreement across orderings is exactly that condition, and it
        degrades to the deterministic score instead of laundering a coin flip as judgement.

        Cost is two calls per tie-break. Tie-breaks fire only on near-ties, so this is rare.
        """
        if not self.available():
            raise TiebreakUnavailable("no GEMINI_API_KEY configured")
        if len(candidates) != 2:
            raise TiebreakUnavailable(f"expected 2 candidates, got {len(candidates)}")

        previews = [self._review_preview(rgb_linear, alpha) for _, alpha in candidates]

        import asyncio

        forward, reverse = await asyncio.gather(
            self._judge(previews[0], previews[1]),
            self._judge(previews[1], previews[0]),
            return_exceptions=True,
        )

        if isinstance(forward, BaseException) or isinstance(reverse, BaseException):
            raise TiebreakUnavailable("one or both vision calls failed")

        fwd_better, fwd_reason = forward
        rev_better, _ = reverse

        if fwd_better == "equivalent" or rev_better == "equivalent":
            raise TiebreakUnavailable("model saw no reliable difference")

        # Map each verdict back to an index into `candidates`. The reverse call saw them swapped.
        fwd_idx = 0 if fwd_better == "A" else 1
        rev_idx = 1 if rev_better == "A" else 0

        if fwd_idx != rev_idx:
            raise TiebreakUnavailable(
                "verdict flipped when the order was reversed — position bias, not judgement"
            )

        return candidates[fwd_idx][0], fwd_reason

    async def _judge(self, preview_a: str, preview_b: str) -> tuple[str, str]:
        """One comparison call. Returns ``(better, reason)`` where better is A | B | equivalent."""
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": _PROMPT},
                        {"text": "Cut-out A:"},
                        {"inline_data": {"mime_type": "image/png", "data": preview_a}},
                        {"text": "Cut-out B:"},
                        {"inline_data": {"mime_type": "image/png", "data": preview_b}},
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
        return verdict

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

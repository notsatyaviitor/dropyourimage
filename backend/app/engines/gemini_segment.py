"""Gemini as a segmentation engine — selectable, never auto-picked, and honest about its edge.

What this is, and the trade it makes
------------------------------------
This is Gemini's *native segmentation* capability, not the image-edit path. That distinction
matters: a generative image editor would re-render the product pixels, which the root CLAUDE.md
prime directive forbids outright. This asks for a mask and receives one, so nothing here produces
or alters an output pixel — the same standing the commercial adapters in `http.py` have.

What it does cost is edge quality, measured 7 Aug 2026 against a live key on the project's studio
fixture (`tests/test_shadow.py::studio_scene`):

    source 200x200          Gemini        ground truth     local control engine
    distinct alpha values   2             27               77
    soft edge pixels        0             248              --
    polygon vertices        24            --               --
    autopick score          0.7246        --               0.9659

    source 800x800          Gemini        ground truth
    distinct alpha values   2             81
    soft edge pixels        0             952
    polygon vertices        13            --

Zero soft edge pixels, because the API returns the mask as a **polygon** — a boundary, not a
coverage field. Rasterising a boundary can only ever produce a binary mask. That breaks
`backend/CLAUDE.md` invariant 2 ("never threshold alpha to binary"), and it leaves
`imaging.composite.decontaminate_edges` with no partially-transparent band to correct, so a white
studio backdrop can halo when composited onto a saturated colour.

The polygon is also coarse, and does not get finer with resolution: 24 vertices at 200px, 13 at
800px. It is describing a circle as a 13-gon.

This engine is provided anyway, at the maintainer's explicit request and with those numbers in
hand. Two things keep the trade visible rather than silent:

* `pipeline._stage1_cutout` emits `Note.HARD_EDGED_MASK` whenever this engine's mask wins, and the
  UI renders it as a full banner.
* It is deliberately **absent from the default `ENGINE_POOL`**, so `EngineStrategy.AUTO` never
  selects it. A score of 0.7246 passes every one of `autopick`'s reject rules, so in an AUTO pair
  it could out-score Photoroom while shipping the worse edge. It is reachable only through
  `EngineStrategy.SINGLE` — an operator choosing it knowingly.

No feathering is applied to soften the polygon. That would invent coverage that was never
measured, which is the same class of claim `docs/LIMITATIONS.md` refuses to make elsewhere.

Wire-format notes, all measured rather than read
------------------------------------------------
* **Vertices are y-first**, `[y, x]`, matching `box_2d` — *not* the `[x, y]` the published docs
  state. Verified on a circle: the first vertex's y equalled `box_2d`'s ymin while its x sat at the
  box's horizontal centre, i.e. the top-centre point. Getting this backwards yields a transposed
  mask that still looks plausible.
* Vertices are in the **same full-image 0-1000 space as `box_2d`**, not relative to the box. So
  `box_2d` is informational here; the polygon alone determines the mask.
* The **same model returned three different vertex shapes across calls** — nested pairs, one flat
  run wrapped in a list, and a bare flat run. All three are handled; a rigid `responseSchema` was
  deliberately not used, because it would fight a model that legitimately varies its output shape.
* `thinkingLevel: MINIMAL` is load-bearing, not a tuning knob. With it, calls measured 1.7-4.7s.
  `docs/ENGINES.md` previously recorded 182-245s for this capability and concluded it was ~100x
  too slow to use; that measurement was taken without it and has been retracted.
* `gemini-3.6-flash` returns COCO compressed RLE instead of a polygon. This module does not decode
  RLE — it raises a typed error naming the setting, because a wrong mask is far worse than a clear
  failure. See `settings.gemini_segment_model`.
"""

from __future__ import annotations

import base64
import json
import time

import cv2
import httpx
import numpy as np

from app.core import errors
from app.engines.base import AlphaResult
from app.engines.http import _HttpEngine
from app.models import EngineId

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Gemini's documented spatial output space, independent of image size. Shared by `box_2d` and, as
# measured here, by the polygon vertices too.
_NORM = 1000.0

# Token-metered, so unlike the flat vendor rates this is an average rather than a price.
# gemini-3.5-flash paid tier: $1.50/1M input, $9.00/1M output.
#   input   ~1300 tokens (a typical packshot tiles to ~1290) + ~60 prompt  -> ~$0.0020
#   output  ~500 tokens (measured 282-908 across calls)                    -> ~$0.0045
# A large image tiles into more input tokens, so treat this as a ledger figure for a mid-sized
# packshot, not a quote. Cheaper than Photoroom's $0.02 — the cost is edge quality, not money.
_ESTIMATED_COST_USD = 0.0065

_PROMPT = """Give the segmentation mask for the foreground subject of this product photograph.

Output a JSON list where each entry contains the 2D bounding box in the key "box_2d", the
segmentation mask in the key "mask", and a short text label in the key "label".

Include every part of the foreground subject, including any separate components that belong to it.
Do not include the background, the surface it rests on, or its cast shadow or reflection.
"""


class GeminiSegmentEngine(_HttpEngine):
    """Native Gemini segmentation. Returns a hard-edged mask — see the module docstring."""

    id = EngineId.GEMINI
    cost_per_image_usd = _ESTIMATED_COST_USD

    @property
    def _timeout_seconds(self) -> float:
        # Not `gemini_timeout_seconds`: that one is short because the tie-break is advisory and
        # must degrade. This call *is* the mask, so it gets a vendor-sized budget.
        return self._settings.gemini_segment_timeout_seconds

    async def alpha_for(self, image_bytes: bytes, width: int, height: int) -> AlphaResult:
        """Return a hard-edged mask sized exactly (height, width).

        Overridden rather than inherited because `_HttpEngine.alpha_for` expects the vendor to
        return a cut-out PNG. Gemini returns JSON geometry, so the rasterisation happens here where
        the source dimensions are in scope. Retries, backoff and error classification still come
        from the base class.
        """
        if not self.available():
            raise errors.VendorUnauthorized(
                "No API key configured for gemini. Set GEMINI_API_KEY in backend/.env."
            )

        started = time.perf_counter()
        body = await self._call_with_retries(image_bytes)
        alpha = _alpha_from_response(body, width, height)
        return AlphaResult(
            alpha=alpha,
            engine=self.id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=self.cost_per_image_usd,
        )

    async def _request(self, client: httpx.AsyncClient, image_bytes: bytes) -> bytes:
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
                "responseMimeType": "application/json",
                # Deterministic as far as the API allows. The cut-out cache keys on
                # sha256(image)+engine, so in practice a repeat run is served from cache anyway.
                "temperature": 0.0,
                # Load-bearing. See the module docstring: this is the difference between ~2s and
                # the minutes the retracted measurement recorded.
                "thinkingConfig": {"thinkingLevel": "MINIMAL"},
            },
        }

        response = await self._send(
            client,
            _ENDPOINT.format(model=self._settings.gemini_segment_model),
            params={"key": self._settings.gemini_api_key},
            json=payload,
        )
        self._classify(response)
        return response.content


def _alpha_from_response(body: bytes, width: int, height: int) -> np.ndarray:
    """Parse a generateContent response into a full-frame float32 mask.

    Unions every returned entry: "remove the background" means keep all of the foreground, and a
    product photographed with a detachable part can legitimately come back as two polygons.
    """
    entries = _parse_entries(body)
    if not entries:
        raise errors.NoForegroundFound(
            "Gemini returned no segmentation mask for this image. On a cropped subject, try a "
            "larger padding or a different subject description."
        )

    canvas = np.zeros((height, width), dtype=np.uint8)
    filled = 0
    for entry in entries:
        points = _polygon_points(entry.get("mask"))
        if len(points) < 3:
            continue
        _fill(canvas, points, width, height)
        filled += 1

    if filled == 0:
        raise errors.VendorError(
            "Gemini returned segmentation entries but none contained a usable polygon."
        )

    return (canvas.astype(np.float32) / 255.0).clip(0.0, 1.0)


def _fill(canvas: np.ndarray, points: list[tuple[int, int]], width: int, height: int) -> None:
    """Rasterise one y-first, 0-1000-normalised polygon into `canvas` in place."""
    scaled = np.array(
        [
            (
                min(width - 1, max(0, int(round(x / _NORM * width)))),
                min(height - 1, max(0, int(round(y / _NORM * height)))),
            )
            for y, x in points
        ],
        dtype=np.int32,
    )
    cv2.fillPoly(canvas, [np.ascontiguousarray(scaled)], 255)


def _polygon_points(mask: object) -> list[tuple[int, int]]:
    """Normalise the `mask` field into ``[(y, x), ...]``, tolerating every shape observed live.

    Raises for a string mask: `gemini-3.6-flash` returns COCO compressed RLE there, and decoding
    it wrongly would produce a confidently incorrect cut-out. A named failure is worth more.
    """
    if isinstance(mask, str):
        raise errors.VendorError(
            "Gemini returned an encoded mask string (COCO RLE) rather than a polygon, which this "
            "engine cannot decode. Set GEMINI_SEGMENT_MODEL to a model that returns polygons "
            "(gemini-3.5-flash is the verified one)."
        )
    if not isinstance(mask, list) or not mask:
        return []

    first = mask[0]
    # Bare flat run: [y, x, y, x, ...]
    if isinstance(first, (int, float)):
        return _pairs(mask)
    # One flat run wrapped in a list: [[y, x, y, x, ...]]
    if len(mask) == 1 and isinstance(first, list) and len(first) > 2:
        return _pairs(first)
    # Nested pairs: [[y, x], [y, x], ...]
    return [
        (int(p[0]), int(p[1]))
        for p in mask
        if isinstance(p, list) and len(p) >= 2 and all(isinstance(v, (int, float)) for v in p[:2])
    ]


def _pairs(flat: list) -> list[tuple[int, int]]:
    numeric = [v for v in flat if isinstance(v, (int, float))]
    return [(int(numeric[i]), int(numeric[i + 1])) for i in range(0, len(numeric) - 1, 2)]


def _parse_entries(body: bytes) -> list[dict]:
    """Pull the JSON list out of a generateContent response, tolerantly.

    Mirrors `locate._parse` and `vision._parse_verdict`: anything unexpected degrades to an empty
    result rather than raising a shape error, so the caller decides what a miss means.
    """
    try:
        parsed_body = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(parsed_body, dict):
        return []

    candidates = parsed_body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return []

    parts = (candidates[0].get("content") or {}).get("parts")
    if not isinstance(parts, list):
        return []

    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text.strip():
        return []

    try:
        entries = json.loads(text)
    except json.JSONDecodeError:
        # Observed live: an occasional malformed JSON body. The base class already retried any
        # retryable HTTP failure; this one is a content problem, so report a miss.
        return []

    if isinstance(entries, dict):
        entries = [entries]
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

"""Subject localisation from a text prompt — "which object", not "where are its edges".

Why this module exists
---------------------
Background removal answers *one* question: foreground versus background. It has no notion of
*which* foreground you meant. Hand remove.bg a photograph of a furnished room and it returns a
confident, high-quality mask of whatever it judged most salient — in testing on a living-room
scene, the sofa — and the rest of the pipeline then dutifully centres and delivers that. The
result is not a bad cut-out; it is a cut-out of the wrong object, which is worse, because nothing
downstream can detect the difference.

So a multi-object scene needs a step that background removal structurally cannot provide: a way to
say "the coffee table". That is a judgement about semantics, which is exactly the kind of work the
prime directive in the root CLAUDE.md assigns to a model. It is *not* the mask — the mask still
comes from the segmentation engine, at full precision, with soft alpha intact.

The division of labour
----------------------
1. **Model** (here): text prompt -> one bounding box. Judgement. Cheap, one call, no pixels made.
2. **Deterministic code**: pad and clamp that box, crop. Pure geometry, `roi_for`.
3. **Segmentation engine**: crop -> soft alpha. Unchanged; it just gets an easier question.

Nothing here touches output pixels. A wrong box produces a cut-out of the wrong region, which the
existing `ALPHA_SUSPECT` scoring and the centroid-offset report already surface — it cannot silently
corrupt colour or geometry.

Measured 6 Aug 2026 against a live key on a furnished-room photograph (940x564), gemini-3.5-flash:

    "coffee table"  -> (415, 382, 655, 508)   correct
    "centre table"  -> (415, 382, 654, 508)   correct, phrasing-insensitive
    "sofa"          -> ( 26, 297, 324, 514)   correct
    "tv unit"       -> (362, 319, 720, 391)   correct
    "potted plant"  -> (826, 220, 938, 385)   correct

Gemini returns boxes normalised to 0-1000 as ``[ymin, xmin, ymax, xmax]`` — note the y-first order,
which is the opposite of the (x, y) convention used everywhere else in this codebase. The
conversion happens once, here, and `app.imaging.geometry.BBox` is x-first like the rest.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import httpx

from app.core.settings import Settings
from app.imaging.geometry import BBox

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Segmentation quality drops on a box cropped flush to the object: the engine loses the contrast
# between subject and surround that it uses to place the edge, and a tight box can clip a soft
# edge outright. A little context back is worth more than the pixels it costs.
DEFAULT_PADDING_PCT = 6.0

# Gemini's documented detection output space, independent of image size.
_NORM = 1000.0

_PROMPT = """Locate the single object best described as: "{phrase}".

Return one bounding box as normalised integers in the range 0-1000, ordered [ymin, xmin, ymax, xmax].
Include the whole object and nothing else. Do not include other furniture or objects that merely
touch or overlap it, and do not include its shadow or reflection.

If several objects match, pick the one most likely to be the photograph's subject — usually the
largest, most central, and least occluded.
If nothing in the image matches the description, set found to false.
"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        # "found: false" is offered explicitly so a missing object has an honest answer. Without
        # it, a model asked for a box will invent one, and an invented box is indistinguishable
        # from a correct one downstream.
        "box_2d": {"type": "array", "items": {"type": "integer"}},
        "label": {"type": "string"},
    },
    "required": ["found"],
}


class SubjectNotLocated(Exception):
    """No key, no match, or an unusable response. Callers fall back to whole-frame segmentation."""


@dataclass(frozen=True)
class Located:
    box: BBox
    """Padded, clamped, in pixel coordinates of the source image."""

    raw_box: BBox
    """The model's box before padding — reported so the padding is auditable."""

    label: str


#: Automatic subject detection: `_PROMPT` with the phrase discovered rather than supplied.
#:
#: Deliberately built from `_PROMPT` rather than from `_ENUMERATE_PROMPT`. Measured on a real
#: client packshot (a supplement bottle on a display stand, 14 Aug 2026): the enumerate prompt
#: returned nothing usable and detection silently did not run, while the named-subject path with
#: "bottle" produced a correct tight crop. The sentence that does the work is "nothing else ...
#: objects that merely touch or overlap it" — a product sits ON its stand, so without that clause
#: the box swallows the stand and the segmenter has no reason to drop it.
_AUTO_PROMPT = """Identify the single product being photographed in this image, and locate it.

Return one bounding box as normalised integers in the range 0-1000, ordered
[ymin, xmin, ymax, xmax], and a short lower-case label naming the product, e.g. "bottle", "jar",
"shoe", "handbag".

Include the whole product and nothing else. Do NOT include a stand, riser, plinth, pedestal,
mount, bracket, clamp, pole, hook or any prop that holds, lifts or presents the product — exclude
these even where they touch or are overlapped by the product. Do NOT include the backdrop, the
surface it rests on, its shadow or its reflection.

If several products are shown, pick the one most likely to be the photograph's subject — usually
the largest, most central, and least occluded.
If there is no product in the image, set found to false.
"""


class GeminiLocator:
    """Text prompt -> one bounding box. Advisory: failure degrades, never fails an image."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def available(self) -> bool:
        return bool(self._settings.gemini_api_key)

    async def locate(
        self,
        image_bytes: bytes,
        phrase: str,
        width: int,
        height: int,
        *,
        padding_pct: float = DEFAULT_PADDING_PCT,
    ) -> Located:
        """Find `phrase` in the image. Raises `SubjectNotLocated` if it cannot."""
        if not self.available():
            raise SubjectNotLocated("no GEMINI_API_KEY configured")
        if not phrase.strip():
            raise SubjectNotLocated("empty subject prompt")

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": _PROMPT.format(phrase=phrase.strip())},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _SCHEMA,
                # Deterministic as far as the API allows: two runs of the same job should crop the
                # same region, or the pipeline's determinism story has a hole in it.
                "temperature": 0.0,
            },
        }

        url = _ENDPOINT.format(model=self._settings.gemini_model)
        try:
            async with httpx.AsyncClient(timeout=self._settings.gemini_timeout_seconds) as client:
                response = await client.post(
                    url, params={"key": self._settings.gemini_api_key}, json=payload
                )
        except httpx.HTTPError as exc:
            raise SubjectNotLocated(f"locator request failed: {type(exc).__name__}") from exc

        if response.status_code != 200:
            # Body deliberately omitted: it can echo the request, including the image.
            raise SubjectNotLocated(f"locator returned HTTP {response.status_code}")

        parsed = _parse(response.json())
        if parsed is None:
            raise SubjectNotLocated("locator response was not usable")

        found, box_norm, label = parsed
        if not found or box_norm is None:
            raise SubjectNotLocated(f"'{phrase}' was not found in this image")

        raw = _to_pixels(box_norm, width, height)
        if raw.is_empty():
            raise SubjectNotLocated("locator returned a degenerate box")

        return Located(box=pad_and_clamp(raw, width, height, padding_pct), raw_box=raw, label=label)

    async def detect(
        self,
        image_bytes: bytes,
        width: int,
        height: int,
        *,
        padding_pct: float = DEFAULT_PADDING_PCT,
    ) -> Located:
        """Find the product with no phrase supplied. Raises `SubjectNotLocated` if it cannot.

        Shares `locate`'s request shape, schema, parser, padding and failure semantics on purpose:
        that path is the one measured to produce a tight box on a real packshot, and a second
        near-copy would drift from it. The only difference is the prompt.
        """
        if not self.available():
            raise SubjectNotLocated("no GEMINI_API_KEY configured")

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": _AUTO_PROMPT},
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _SCHEMA,
                "temperature": 0.0,
            },
        }

        url = _ENDPOINT.format(model=self._settings.gemini_model)
        try:
            async with httpx.AsyncClient(timeout=self._settings.gemini_timeout_seconds) as client:
                response = await client.post(
                    url, params={"key": self._settings.gemini_api_key}, json=payload
                )
        except httpx.HTTPError as exc:
            raise SubjectNotLocated(f"detector request failed: {type(exc).__name__}") from exc

        if response.status_code != 200:
            # Body deliberately omitted: it can echo the request, including the image.
            raise SubjectNotLocated(f"detector returned HTTP {response.status_code}")

        parsed = _parse(response.json())
        if parsed is None:
            raise SubjectNotLocated("detector response was not usable")

        found, box_norm, label = parsed
        if not found or box_norm is None:
            raise SubjectNotLocated("no product found in this image")

        raw = _to_pixels(box_norm, width, height)
        if raw.is_empty():
            raise SubjectNotLocated("detector returned a degenerate box")

        return Located(box=pad_and_clamp(raw, width, height, padding_pct), raw_box=raw, label=label)


#: Upper bound on objects returned for one scene. Each one costs a segmentation call, so an
#: over-eager model listing every cushion and light fitting turns a $0.02 image into a $0.40 one.
#: Ranked by area, so the cap drops the least significant things first.
MAX_OBJECTS = 12

_ENUMERATE_PROMPT = """List the distinct physical objects in this photograph that a retoucher
would want on separate layers — furniture and significant fixtures, not surfaces.

Return a JSON array. Each entry must have:
  "box_2d": [ymin, xmin, ymax, xmax] as normalised integers 0-1000
  "label":  a short lower-case name, e.g. "bed", "pillow", "potted plant"

Rules:
* One entry per object. If several of a kind are separable, list them separately.
* Do NOT list the floor, the ceiling, the walls, or the room itself — those are the background.
* Do NOT list shadows, reflections, or light spill as objects.
* Order the array from largest and most prominent to smallest.
* At most {limit} entries.
"""


async def locate_many(
    locator: GeminiLocator,
    image_bytes: bytes,
    width: int,
    height: int,
    *,
    padding_pct: float = DEFAULT_PADDING_PCT,
    limit: int = MAX_OBJECTS,
    mime_type: str = "image/png",
) -> list[Located]:
    """Enumerate every separable object in a scene, each with a padded pixel box.

    The multi-object counterpart to `locate`. Same division of labour and the same reason for it:
    deciding *what counts as an object* is semantics, so a model does it; every pixel that reaches
    the output still comes from the segmentation engine at full precision.

    Returns `[]` rather than raising — an enumeration failure should degrade to the ordinary
    single-subject path, not fail the image. Duplicate labels are disambiguated (`pillow`,
    `pillow-2`) because PSD layer names are how a retoucher addresses a layer, and two layers
    called `pillow` are not addressable.
    """
    if not locator.available():
        return []

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": _ENUMERATE_PROMPT.format(limit=limit)},
                    {
                        "inline_data": {
                            # Must match the bytes actually sent. Declaring image/jpeg while
                            # sending a PNG made Gemini return an empty list rather than an
                            # error — a silent zero-object result that looked like "this scene
                            # has nothing in it".
                            "mime_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.0,
            # Same reasoning as gemini_segment: spatial work is measurably faster and no worse
            # with thinking off, and a slow enumeration holds up every object behind it.
            "thinkingConfig": {"thinkingLevel": "MINIMAL"},
        },
    }

    settings = locator._settings
    url = _ENDPOINT.format(model=settings.gemini_model)
    try:
        async with httpx.AsyncClient(timeout=settings.gemini_timeout_seconds) as client:
            response = await client.post(
                url, params={"key": settings.gemini_api_key}, json=payload
            )
    except httpx.HTTPError:
        return []

    if response.status_code != 200:
        # Body omitted deliberately: it can echo the request, including the image.
        return []

    entries = _parse_many(response.json())
    located: list[Located] = []
    used: dict[str, int] = {}

    for entry in entries[:limit]:
        raw = _to_pixels(entry["box_2d"], width, height)
        if raw.is_empty():
            continue

        label = entry["label"] or "object"
        used[label] = used.get(label, 0) + 1
        unique = label if used[label] == 1 else f"{label}-{used[label]}"

        located.append(
            Located(
                box=pad_and_clamp(raw, width, height, padding_pct),
                raw_box=raw,
                label=unique,
            )
        )

    return located


def _parse_many(body: object) -> list[dict]:
    """Pull a list of ``{box_2d, label}`` out of a generateContent response, tolerantly."""
    if not isinstance(body, dict):
        return []
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return []

    parts = (candidates[0].get("content") or {}).get("parts")
    if not isinstance(parts, list):
        return []

    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text.strip():
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []

    out: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        box = item.get("box_2d")
        if not (isinstance(box, list) and len(box) >= 4):
            continue
        if not all(isinstance(v, (int, float)) for v in box[:4]):
            continue
        out.append(
            {
                "box_2d": [int(v) for v in box[:4]],
                "label": str(item.get("label", "")).strip().lower()[:40],
            }
        )
    return out


def pad_and_clamp(box: BBox, width: int, height: int, padding_pct: float) -> BBox:
    """Grow a box by a percentage of its own size, clamped to the image. Pure geometry.

    Padding is relative to the box, not the image, so a small object gets proportionally the same
    context as a large one.
    """
    pad_x = int(round(box.width * padding_pct / 100.0))
    pad_y = int(round(box.height * padding_pct / 100.0))
    return BBox(
        max(0, box.x0 - pad_x),
        max(0, box.y0 - pad_y),
        min(width, box.x1 + pad_x),
        min(height, box.y1 + pad_y),
    )


def _to_pixels(box_norm: list[int], width: int, height: int) -> BBox:
    """``[ymin, xmin, ymax, xmax]`` normalised 0-1000 -> pixel `BBox`, x-first.

    The y-first ordering is Gemini's, and getting it backwards produces a plausible-looking box in
    the wrong place — so it is converted exactly once, here.
    """
    y0, x0, y1, x1 = box_norm[:4]
    # Sort each axis: a model occasionally emits min/max reversed, and a negative-width box would
    # crop to nothing.
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    return BBox(
        max(0, min(width, int(x0 / _NORM * width))),
        max(0, min(height, int(y0 / _NORM * height))),
        max(0, min(width, int(x1 / _NORM * width))),
        max(0, min(height, int(y1 / _NORM * height))),
    )


def _parse(body: object) -> tuple[bool, list[int] | None, str] | None:
    """Pull ``(found, box, label)`` out of a generateContent response, tolerantly."""
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
    if not isinstance(parsed, dict):
        return None

    box = parsed.get("box_2d")
    if not (isinstance(box, list) and len(box) >= 4 and all(isinstance(v, int) for v in box[:4])):
        box = None

    return bool(parsed.get("found")), box, str(parsed.get("label", ""))[:64]


# ---------------------------------------------------------------------------
# Automatic subject selection
# ---------------------------------------------------------------------------


#: A candidate covering more of the frame than this is the model boxing the whole scene rather
#: than an object in it. Cropping to it is a no-op at best; at worst it silently disables the
#: whole-frame path that was already correct.
_MAX_FRAME_FRACTION = 0.92

#: Below this, the "object" is a fitting, a label or a highlight, not the subject of a packshot.
_MIN_FRAME_FRACTION = 0.005

def box_is_plausible_subject(box: BBox, width: int, height: int) -> bool:
    """Is this box a product, or the model boxing the whole scene / a fitting?

    Shared by both automatic paths so "plausible subject" has exactly one definition. Pure
    arithmetic: the model proposes, this disposes.
    """
    frame = float(width * height)
    if frame <= 0 or box.is_empty():
        return False
    fraction = (box.width * box.height) / frame
    return _MIN_FRAME_FRACTION <= fraction <= _MAX_FRAME_FRACTION




"""Hand-encoded PSD vector path resources — the clipping path, in pure Python.

The client report and the original delivery plan both treat a real vector clipping path as the
highest-risk item in the PSD feature, assuming it needs either the Adobe Photoshop API or
"hand-authoring PSD Image Resource blocks — 8.24 fixed-point Bezier knot records" as a
last-resort, high-risk fallback. Building `app/psd/fallback.py` surfaced that the second option is
more tractable than assumed: `pytoshop` exposes a raw `GenericImageResourceBlock(resource_id, name,
data)` escape hatch, and Adobe's path resource format (Resource IDs 2000-2997, plus 2999 for which
saved path is *the* clipping path) is fully documented and is just binary records — no compiled
Photoshop dependency required to construct it.

**What this module produces and does not produce.** It writes a structurally correct clipping
path in the documented Adobe format, with straight-line segments (each Bezier knot's control
points collapsed onto the anchor — a legitimate, valid Bezier representation of a polygon, just
not smoothed curves). It has been verified two ways: an independent decoder round-trips a test
polygon back to sub-pixel accuracy (see `tests/test_psd_vector_path.py`), and `psd-tools` opens a
file containing it without erroring. It has **not** been confirmed inside actual Adobe Photoshop —
this environment has no Photoshop install to check against, and that gap should be closed before
this ships to a client, not assumed away. If a real Photoshop check ever contradicts anything
here, trust Photoshop and fix this module, not the other way round.

Binary format, condensed from Adobe's "Photoshop File Formats" path resource specification:

* A path resource is a sequence of fixed-length 26-byte records, each beginning with a 2-byte
  selector: 0/3 = subpath length header (closed/open), 1/2 = closed Bezier knot (linked/unlinked),
  4/5 = open Bezier knot, 6 = fill rule, 8 = initial fill rule.
* Each knot record holds three points — preceding control handle, anchor, following control
  handle — each point a **(vertical, horizontal)** pair, each component a signed 8.24 fixed-point
  fraction of the canvas dimension (not absolute pixels). 2 (selector) + 3 x 2 x 4 bytes = 26.
* Resource ID 2999 designates which named path (by Pascal-string name) is *the* document's
  clipping path. Its payload is the Pascal string alone — **verified directly against an
  independent reader**: an earlier version of this module appended a 4-byte "flatness" value
  that several online descriptions of the format mention, but `psd-tools`' own reader for this
  resource models it as a bare Pascal string and silently drops anything appended after it on
  re-serialization. Byte-parsed the actual resource block psd-tools wrote back out to confirm
  the true payload is exactly `len(name) + name`, nothing more, before trusting that over the
  secondhand description.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import cv2
import numpy as np

# Bezier-knot selector for a CLOSED, linked-handle point — what a straight polygon vertex is.
_SELECTOR_CLOSED_LENGTH = 0
_SELECTOR_CLOSED_KNOT_LINKED = 1

_RECORD_SIZE = 26

# Simplification tolerance for Douglas-Peucker, as a fraction of the contour's perimeter.
# Measured on two test shapes at a 120px canvas: a sharp-cornered square holds exactly 4 vertices
# at every value from 0.0015 to 0.03 (Douglas-Peucker preserves real corners regardless), while a
# smooth radius-40 circle drops from an unusable 72 vertices at 0.0015 to a clean 16 at 0.004-0.008
# and 8 by 0.015. 0.005 sits in the middle of that clean range — few enough vertices to be usable
# in the Paths panel, close enough to the pixel contour not to visibly round off silhouette
# detail like a mug handle.
_APPROX_EPSILON_FRACTION = 0.005

# Below this vertex count a "simplified" polygon does not meaningfully represent a closed shape.
_MIN_VERTICES = 3

# Path resource IDs occupy 2000-2997; the resource that designates which one is the document's
# clipping path is fixed at 2999. Only one saved path is written here, so it always gets 2000.
PATH_RESOURCE_ID = 2000
CLIPPING_PATH_DESIGNATION_ID = 2999


@dataclass
class ClippingPathResources:
    """Raw bytes for the two image resources a clipping path needs.

    ``path_data`` goes in a resource with `PATH_RESOURCE_ID` and the path's name; ``designation``
    goes in `CLIPPING_PATH_DESIGNATION_ID` unconditionally, since exactly one path is ever
    written. Kept as plain bytes (not `GenericImageResourceBlock` objects) so this module has no
    dependency on `pytoshop` — only `fallback.py`, which assembles the actual file, does.
    """

    path_data: bytes
    designation: bytes
    vertex_count: int


def _to_fixed_8_24(fraction: float) -> int:
    """A coordinate fraction (0..1 of canvas size) as a signed 8.24 fixed-point 32-bit int."""
    return int(round(fraction * (1 << 24))) & 0xFFFFFFFF


def _encode_knot(y_frac: float, x_frac: float) -> bytes:
    """One 24-byte knot payload: three identical (vertical, horizontal) points.

    All three points — preceding handle, anchor, following handle — are set to the same
    coordinate. That produces a straight line segment on each side of the knot, which is a
    completely valid closed Bezier path; it is simply not curve-smoothed. Fitting genuine cubic
    Bezier curves to the contour would look better in the Paths panel but adds real geometric
    complexity for a POC deadline — documented here as a improvement to make later, not hidden.
    """
    point = struct.pack('>II', _to_fixed_8_24(y_frac), _to_fixed_8_24(x_frac))
    return point * 3


def _ensure_winding(points: np.ndarray, want_ccw: bool) -> np.ndarray:
    """Force a consistent winding direction via the shoelace signed area.

    The client report's Feature 2 explicitly checks for "correct winding direction" — Photoshop
    paths and printers care about this because it determines which side of the outline the
    nonzero-winding fill rule treats as "inside". `want_ccw` is evaluated in **image coordinates**
    (y increases downward), which is the opposite handedness from standard maths convention, so
    getting this backwards is an easy, silent mistake — hence the explicit test in
    `tests/test_psd_vector_path.py` that checks the sign directly rather than trusting intuition.
    """
    x = points[:, 0].astype(np.float64)
    y = points[:, 1].astype(np.float64)
    signed_area = float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)) / 2.0
    is_ccw = signed_area < 0  # negative under image-coordinate (y-down) handedness
    if is_ccw == want_ccw:
        return points
    return points[::-1].copy()


def contour_from_alpha(alpha: np.ndarray, threshold: float = 0.5) -> np.ndarray | None:
    """The single largest external contour of the alpha mask, simplified to a polygon.

    Returns vertices as an (N, 2) array of (x, y) pixel coordinates, or ``None`` if there is
    nothing to trace. Only the external boundary is used — holes (a mug's handle gap, for
    instance) are not represented as separate subpaths. That is a real, documented limitation
    (see docs/PSD.md), not an oversight: multi-subpath paths are a straightforward extension of
    the format above, just additional length-header/knot groups, but were out of scope for the
    time available.
    """
    mask = (np.asarray(alpha, dtype=np.float32) > threshold).astype(np.uint8)
    if not mask.any():
        return None

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    largest = max(contours, key=cv2.contourArea)
    perimeter = cv2.arcLength(largest, closed=True)
    if perimeter <= 0:
        return None

    epsilon = _APPROX_EPSILON_FRACTION * perimeter
    simplified = cv2.approxPolyDP(largest, epsilon, closed=True)
    points = simplified.reshape(-1, 2)

    if len(points) < _MIN_VERTICES:
        return None
    return points


def build_clipping_path(
    alpha: np.ndarray,
    width: int,
    height: int,
    *,
    name: str = "PATH",
    threshold: float = 0.5,
) -> ClippingPathResources | None:
    """Trace the alpha channel and encode it as clipping-path image resources.

    Returns ``None`` when there is nothing to trace (an empty mask) — the caller falls back to a
    raster-only PSD and records `Note.PSD_FALLBACK_RASTER`, exactly as it would for any other
    reason the vector path could not be produced.
    """
    points = contour_from_alpha(alpha, threshold)
    if points is None:
        return None

    # Counter-clockwise in image coordinates is the conventional winding Photoshop's own
    # path-drawing tools produce for an outer boundary under the nonzero-winding fill rule.
    points = _ensure_winding(points, want_ccw=True)

    body = bytearray()
    body += struct.pack('>H', _SELECTOR_CLOSED_LENGTH)
    body += struct.pack('>H', len(points))
    body += b'\x00' * (_RECORD_SIZE - 4)  # pad the length record to the fixed 26-byte size
    for x, y in points:
        body += struct.pack('>H', _SELECTOR_CLOSED_KNOT_LINKED)
        body += _encode_knot(float(y) / height, float(x) / width)

    name_bytes = name.encode('macroman', errors='replace')[:255]
    designation = bytes([len(name_bytes)]) + name_bytes  # pure Pascal string — see module docstring

    return ClippingPathResources(
        path_data=bytes(body), designation=designation, vertex_count=len(points)
    )


def decode_path_records(data: bytes) -> list[tuple[str, object]]:
    """Independent decoder for `build_clipping_path`'s output — used only by tests.

    Deliberately a second, separately-written implementation of the record layout rather than a
    thin wrapper around the encoder, so a round-trip test through both catches a mistake that is
    symmetric between encode and decode (the kind a single shared implementation could hide).
    """
    records: list[tuple[str, object]] = []
    i = 0
    while i + _RECORD_SIZE <= len(data):
        selector = struct.unpack('>H', data[i : i + 2])[0]
        if selector in (0, 3):
            count = struct.unpack('>H', data[i + 2 : i + 4])[0]
            records.append(('length', count))
        elif selector in (1, 2, 4, 5):
            points = []
            offset = i + 2
            for _ in range(3):
                v_raw, h_raw = struct.unpack('>II', data[offset : offset + 8])
                points.append((_signed32(v_raw) / (1 << 24), _signed32(h_raw) / (1 << 24)))
                offset += 8
            records.append(('knot', points))
        else:
            records.append(('other', selector))
        i += _RECORD_SIZE
    return records


def _signed32(raw: int) -> int:
    return raw - (1 << 32) if raw >= (1 << 31) else raw

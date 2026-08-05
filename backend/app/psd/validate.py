"""Read-back validation of a written PSD, using `psd-tools` — a library independent of whatever
wrote the file (`pytoshop` fallback or the Adobe API), so a bug shared between writer and
validator is far less likely to hide here.

Checks mirror the client report's Feature 2 task list directly: layer naming convention, required
groups present, colour profile, and clipping path validity including winding direction.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field

from psd_tools import PSDImage

from app.models import ColorProfile, PsdSpec
from app.psd import vector_path as vp


@dataclass
class PsdValidation:
    ok: bool
    problems: list[str] = field(default_factory=list)
    layer_names: list[str] = field(default_factory=list)
    has_vector_path: bool = False
    path_vertex_count: int | None = None
    color_mode: str | None = None
    depth: int | None = None
    width: int | None = None
    height: int | None = None


def _clean_name(name: str) -> str:
    """Strip the stray trailing NUL pytoshop's Unicode layer name emits — see fallback.py."""
    return name.rstrip('\x00')


def validate_psd(data: bytes, spec: PsdSpec, *, expected_size: tuple[int, int] | None = None) -> PsdValidation:
    """Structural validation of a written PSD against the naming/profile/path it was asked for.

    Never raises for a malformed file — a validation failure is data (`ok=False` plus
    `problems`), not an exception, so a caller can decide whether to ship the file anyway, retry,
    or fall back further.
    """
    problems: list[str] = []

    try:
        psd = PSDImage.open(io.BytesIO(data))
    except Exception as exc:
        return PsdValidation(ok=False, problems=[f"could not open PSD: {exc}"])

    result = PsdValidation(
        ok=True,
        color_mode=str(psd.color_mode),
        depth=psd.depth,
        width=psd.width,
        height=psd.height,
    )

    if expected_size is not None and (psd.width, psd.height) != expected_size:
        problems.append(f"canvas is {psd.width}x{psd.height}, expected {expected_size}")

    if 'RGB' not in str(psd.color_mode):
        problems.append(f"colour mode is {psd.color_mode}, expected RGB")
    if psd.depth != 8:
        problems.append(f"bit depth is {psd.depth}, expected 8")

    layer_names = [_clean_name(layer.name) for layer in psd]
    result.layer_names = layer_names

    required = {spec.product_layer_name, spec.background_layer_name}
    if spec.enabled:
        pass  # shadow layer is conditional on shadow preservation; not required unconditionally
    missing = required - set(layer_names)
    if missing:
        problems.append(f"missing required layer(s): {sorted(missing)}")

    icc_resource = _find_resource(psd, 1039)
    if icc_resource is None:
        problems.append("no ICC profile embedded (resource 1039 absent)")
    else:
        expected_desc = "Adobe RGB" if spec.profile is ColorProfile.ADOBE_RGB else "sRGB"
        if expected_desc.split()[0].lower() not in icc_resource.lower():
            problems.append(
                f"embedded ICC profile does not look like {expected_desc} "
                f"(checked for a case-insensitive substring in the profile description)"
            )

    if spec.vector_clipping_path:
        path_check = _validate_vector_path(psd, spec.path_name)
        result.has_vector_path = path_check.has_vector_path
        result.path_vertex_count = path_check.path_vertex_count
        problems.extend(path_check.problems)

    result.ok = not problems
    result.problems = problems
    return result


def _find_resource(psd: PSDImage, resource_id: int) -> str | None:
    """Raw bytes of an image resource by ID, decoded permissively for substring matching against
    a profile description — not a real ICC parser, just enough to confirm *which* profile got
    embedded without pulling in a second ICC library.

    `psd._record.image_resources` is a dict keyed by `Resource`, an `IntEnum` — plain-int lookup
    via `.get()` works directly because `IntEnum` compares and hashes equal to its int value;
    confirmed directly rather than assumed, since psd-tools' own iteration yields the enum/int
    *keys*, not the resource objects, which is easy to get wrong from the outside.

    Null bytes are stripped before decoding because ICC description tags are not consistently
    ASCII: this codebase's own synthesised Adobe RGB profile uses the older plain-ASCII `desc`
    type, but littleCMS's generated sRGB profile (`ImageCms.createProfile("sRGB")`) uses the
    modern `mluc` (multiLocalizedUnicode) type, which is UTF-16BE — "sRGB" appears as
    `s\x00R\x00G\x00B\x00`. Verified directly on the actual bytes rather than assumed. Stripping
    nulls collapses that down to plain ASCII for a substring check without needing a real ICC tag
    parser, and is a no-op on profiles that were already ASCII.
    """
    entry = psd._record.image_resources.get(resource_id)
    if entry is None:
        return None
    return bytes(entry.data).replace(b'\x00', b'').decode('latin-1', errors='replace')


@dataclass
class _PathCheck:
    has_vector_path: bool = False
    path_vertex_count: int | None = None
    problems: list[str] = field(default_factory=list)


def _validate_vector_path(psd: PSDImage, expected_name: str) -> _PathCheck:
    """Decode the raw path resource with `vector_path.decode_path_records` — the independent
    decoder, not the encoder — and check it is well-formed and correctly wound.

    A missing path here is reported as a problem only when `spec.vector_clipping_path` was
    requested; a raster-only PSD (by choice, or because tracing failed and
    `Note.PSD_FALLBACK_RASTER` was recorded) legitimately has no path resource at all.
    """
    check = _PathCheck()

    path_data = _find_raw_resource(psd, vp.PATH_RESOURCE_ID)
    # Resource 2999 gets psd-tools' own typed reader (a `PascalString`, not raw bytes) — read it
    # through that rather than re-parsing bytes ourselves. See vector_path.py's module docstring
    # for why: an earlier version of this pair assumed a trailing 4-byte flatness field that
    # psd-tools' independently-implemented reader does not recognise.
    designation_entry = psd._record.image_resources.get(vp.CLIPPING_PATH_DESIGNATION_ID)

    if path_data is None or designation_entry is None:
        check.problems.append(
            "vector clipping path was requested but no path resource was found "
            "(expected with Note.PSD_FALLBACK_RASTER if tracing legitimately failed)"
        )
        return check

    # `PascalString` is not a `str` subclass and `str()` on it oddly returns "'PATH'" (quotes
    # included, its own __repr__) rather than the bare value — verified directly. Its `__eq__`
    # against a plain string does work correctly, so compare through that rather than str().
    name_matches = designation_entry.data == expected_name
    if not name_matches:
        check.problems.append(
            f"clipping path designates {designation_entry.data!r}, expected {expected_name!r}"
        )

    records = vp.decode_path_records(path_data)
    if not records or records[0][0] != 'length':
        check.problems.append("path data does not start with a subpath length record")
        return check

    declared_count = records[0][1]
    knots = [r for r in records[1:] if r[0] == 'knot']
    if len(knots) != declared_count:
        check.problems.append(
            f"declared {declared_count} knots but found {len(knots)} knot records"
        )

    if len(knots) < 3:
        check.problems.append("fewer than 3 knots — not a closed polygon")
        return check

    # Winding direction, independently recomputed from the decoded points — not by trusting the
    # encoder's own claim, which is the entire point of an independent validator.
    points = [(k[1][1][1], k[1][1][0]) for k in knots]  # (x_frac, y_frac) from each knot's anchor
    area = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        area += x0 * y1 - x1 * y0
    is_ccw = area < 0  # image coordinates: y increases downward
    if not is_ccw:
        check.problems.append("path winds clockwise; expected counter-clockwise")

    check.has_vector_path = not check.problems
    check.path_vertex_count = len(knots)
    return check


def _find_raw_resource(psd: PSDImage, resource_id: int) -> bytes | None:
    entry = psd._record.image_resources.get(resource_id)
    return bytes(entry.data) if entry is not None else None

"""Minimal ICC v2 matrix-shaper profile writer.

Why this exists: the client report requires PSD deliverables in **Adobe RGB (1998), 8-bit
embedded**, and an output file claiming Adobe RGB without an embedded profile is worse than
useless — Photoshop will read the numbers as sRGB and show the wrong colour. Pillow can only
generate an sRGB profile (``ImageCms.createProfile`` accepts "sRGB", "LAB" and "XYZ" and nothing
else), and shipping Adobe's own ``.icc`` file makes a licensed binary asset part of the repo.

So we synthesise one. A matrix-shaper RGB profile is small and fully specified: primaries as
XYZ colorants in the D50 PCS, plus a gamma curve per channel. Roughly 500 bytes.

Correctness is not assumed — `tests/test_icc.py` builds a transform from the generated profile
to sRGB with littleCMS and checks known colours land where they should.
"""

from __future__ import annotations

import struct
from functools import lru_cache

# ICC requires matrix-shaper colorants in the D50 profile connection space, so these are the
# Bradford-adapted Adobe RGB (1998) values, not the D65 ones used for pixel maths in `color.py`.
_ADOBE_RGB_COLORANTS_D50 = {
    "rXYZ": (0.60974, 0.31111, 0.01947),
    "gXYZ": (0.20528, 0.62567, 0.06087),
    "bXYZ": (0.14919, 0.06322, 0.74457),
}
_D50_WHITE = (0.96420, 1.00000, 0.82491)

_ADOBE_RGB_GAMMA = 563.0 / 256.0

# Fixed so profile bytes are byte-identical run to run. A timestamp here would break the
# pipeline's determinism test for no benefit.
_FIXED_DATETIME = (2026, 1, 1, 0, 0, 0)


def _s15f16(value: float) -> bytes:
    """s15Fixed16Number, big-endian."""
    return struct.pack(">i", int(round(value * 65536.0)))


def _u8f8(value: float) -> bytes:
    """u8Fixed8Number, big-endian."""
    return struct.pack(">H", int(round(value * 256.0)))


def _pad4(data: bytes) -> bytes:
    return data + b"\x00" * ((-len(data)) % 4)


def _xyz_type(x: float, y: float, z: float) -> bytes:
    return b"XYZ " + b"\x00" * 4 + _s15f16(x) + _s15f16(y) + _s15f16(z)


def _curve_type_gamma(gamma: float) -> bytes:
    """curveType holding a single gamma value."""
    return b"curv" + b"\x00" * 4 + struct.pack(">I", 1) + _u8f8(gamma)


def _text_description_type(text: str) -> bytes:
    """textDescriptionType — the ICC v2 form of 'desc'. v4's multiLocalizedUnicode is not it."""
    ascii_bytes = text.encode("ascii", "replace") + b"\x00"
    return (
        b"desc"
        + b"\x00" * 4
        + struct.pack(">I", len(ascii_bytes))
        + ascii_bytes
        + struct.pack(">I", 0)          # unicode language code
        + struct.pack(">I", 0)          # unicode count
        + struct.pack(">H", 0)          # scriptcode code
        + struct.pack(">B", 0)          # scriptcode count
        + b"\x00" * 67                  # scriptcode description, fixed length
    )


def _text_type(text: str) -> bytes:
    return b"text" + b"\x00" * 4 + text.encode("ascii", "replace") + b"\x00"


def build_matrix_shaper_profile(
    *,
    description: str,
    colorants: dict[str, tuple[float, float, float]],
    gamma: float,
    copyright_text: str = "Public Domain",
) -> bytes:
    """Assemble an ICC v2.1 RGB matrix-shaper profile.

    `colorants` maps ``rXYZ``/``gXYZ``/``bXYZ`` to D50-adapted XYZ triples.
    """
    tags: list[tuple[bytes, bytes]] = [
        (b"desc", _text_description_type(description)),
        (b"wtpt", _xyz_type(*_D50_WHITE)),
        (b"rXYZ", _xyz_type(*colorants["rXYZ"])),
        (b"gXYZ", _xyz_type(*colorants["gXYZ"])),
        (b"bXYZ", _xyz_type(*colorants["bXYZ"])),
        (b"rTRC", _curve_type_gamma(gamma)),
        (b"gTRC", _curve_type_gamma(gamma)),
        (b"bTRC", _curve_type_gamma(gamma)),
        (b"cprt", _text_type(copyright_text)),
    ]

    # The three TRC tags are identical, so point them at one block — this is standard practice
    # and what real profiles do.
    header_size = 128
    table_size = 4 + 12 * len(tags)
    offset = header_size + table_size

    blobs: list[bytes] = []
    entries: list[tuple[bytes, int, int]] = []
    seen: dict[bytes, tuple[int, int]] = {}

    for sig, data in tags:
        if data in seen:
            entries.append((sig, *seen[data]))
            continue
        padded = _pad4(data)
        entries.append((sig, offset, len(data)))
        seen[data] = (offset, len(data))
        blobs.append(padded)
        offset += len(padded)

    total = offset

    header = bytearray(header_size)
    struct.pack_into(">I", header, 0, total)
    header[4:8] = b"none"                                   # preferred CMM: none
    struct.pack_into(">I", header, 8, 0x02100000)           # v2.1.0
    header[12:16] = b"mntr"                                 # device class: display
    header[16:20] = b"RGB "
    header[20:24] = b"XYZ "
    struct.pack_into(">6H", header, 24, *_FIXED_DATETIME)
    header[36:40] = b"acsp"
    header[40:44] = b"\x00" * 4                             # platform: none
    struct.pack_into(">I", header, 44, 0)                   # flags
    header[48:52] = b"\x00" * 4                             # manufacturer
    header[52:56] = b"\x00" * 4                             # model
    struct.pack_into(">Q", header, 56, 0)                   # attributes
    struct.pack_into(">I", header, 64, 0)                   # rendering intent: perceptual
    header[68:80] = _s15f16(_D50_WHITE[0]) + _s15f16(_D50_WHITE[1]) + _s15f16(_D50_WHITE[2])
    header[80:84] = b"\x00" * 4                             # creator
    # bytes 84..100 profile ID, 100..128 reserved: left zero

    table = bytearray(struct.pack(">I", len(entries)))
    for sig, off, size in entries:
        table += sig + struct.pack(">II", off, size)

    return bytes(header) + bytes(table) + b"".join(blobs)


def adobe_rgb_1998_profile() -> bytes:
    """An Adobe RGB (1998) compatible matrix-shaper profile.

    Named "Compatible with Adobe RGB (1998)" rather than claiming to *be* Adobe's profile, since
    it is independently generated. Colorimetrically equivalent; not Adobe's file.
    """
    return build_matrix_shaper_profile(
        description="Compatible with Adobe RGB (1998)",
        colorants=_ADOBE_RGB_COLORANTS_D50,
        gamma=_ADOBE_RGB_GAMMA,
    )


@lru_cache(maxsize=1)
def srgb_profile() -> bytes:
    """sRGB profile bytes, from littleCMS via Pillow.

    Cached deliberately. ``ImageCms.createProfile("sRGB")`` embeds a *creation timestamp* in the
    ICC header — littleCMS sets it to the current system time — so two calls a second apart
    produce genuinely different bytes. Measured directly: identical calls 1.2s apart differed at
    byte offset 35, the ICC date field.

    That silently broke the pipeline's own determinism contract (same input -> same output, every
    time): the export stage calls this on every PNG/TIFF/WebP write, so two runs of the same job
    landing in different wall-clock seconds produced different output bytes even though every
    actual pixel was identical. It surfaced as a flaky test failure under load — slower runs are
    more likely to straddle a second boundary — which first looked like a multi-threaded
    floating-point rounding issue (see `app/__init__.py`) before the ICC timestamp was found to be
    the real cause. Caching the bytes once per process makes every call within a run return the
    same profile, which is what the determinism contract actually requires and is being tested.
    """
    from io import BytesIO

    from PIL import ImageCms

    profile = ImageCms.createProfile("sRGB")
    buf = BytesIO()
    buf.write(ImageCms.ImageCmsProfile(profile).tobytes())
    return buf.getvalue()


def profile_bytes(name: str) -> bytes:
    """Look up embeddable profile bytes by `ColorProfile` enum value."""
    if name == "srgb":
        return srgb_profile()
    if name == "adobe_rgb":
        return adobe_rgb_1998_profile()
    raise ValueError(f"no ICC profile available for {name!r}")

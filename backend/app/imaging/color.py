"""Colour management: transfer functions, colourspace conversion, hex parsing.

Pure functions. No I/O, no config reads — see `backend/CLAUDE.md`.

Conventions used throughout `app/imaging`:

* Images are ``float32`` in ``[0, 1]``, shape ``(H, W, 3)`` for colour, ``(H, W)`` for a single
  channel. Alpha is always carried as a separate ``(H, W)`` float32 array.
* "encoded" means gamma-encoded in some profile's transfer function (what is in a PNG).
  "linear" means linear-light in that profile's primaries (what you may composite in).
* 8-bit conversion happens once, at export.

Why this module exists at all: compositing gamma-encoded values is wrong, and it is wrong in a
way that looks like a subtle dark rim around every cut-out rather than like a crash.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ---------------------------------------------------------------------------
# Primaries (D65). Rows are R, G, B.
# ---------------------------------------------------------------------------

_SRGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float64,
)

_XYZ_TO_ADOBE_RGB = np.array(
    [
        [2.0413690, -0.5649464, -0.3446944],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0134474, -0.1183897, 1.0154096],
    ],
    dtype=np.float64,
)

# Adobe RGB (1998) fully contains sRGB, so this direction never needs gamut mapping.
_SRGB_LIN_TO_ADOBE_LIN = (_XYZ_TO_ADOBE_RGB @ _SRGB_TO_XYZ).astype(np.float32)
_ADOBE_LIN_TO_SRGB_LIN = np.linalg.inv(_XYZ_TO_ADOBE_RGB @ _SRGB_TO_XYZ).astype(np.float32)

# Adobe RGB (1998) is a pure power function of 563/256 = 2.19921875.
_ADOBE_GAMMA = 563.0 / 256.0


# ---------------------------------------------------------------------------
# Transfer functions
# ---------------------------------------------------------------------------


def srgb_decode(encoded: np.ndarray) -> np.ndarray:
    """sRGB EOTF: gamma-encoded -> linear light.

    The real piecewise curve, not an approximate ``x ** 2.2``. The linear toe below 0.04045
    matters here because cut-out edge pixels and soft shadows live in exactly that range.
    """
    x = np.asarray(encoded, dtype=np.float32)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def srgb_encode(linear: np.ndarray) -> np.ndarray:
    """sRGB inverse EOTF: linear light -> gamma-encoded."""
    x = np.clip(np.asarray(linear, dtype=np.float32), 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1.0 / 2.4) - 0.055).astype(
        np.float32
    )


def adobe_rgb_decode(encoded: np.ndarray) -> np.ndarray:
    x = np.clip(np.asarray(encoded, dtype=np.float32), 0.0, 1.0)
    return (x**_ADOBE_GAMMA).astype(np.float32)


def adobe_rgb_encode(linear: np.ndarray) -> np.ndarray:
    x = np.clip(np.asarray(linear, dtype=np.float32), 0.0, 1.0)
    return (x ** (1.0 / _ADOBE_GAMMA)).astype(np.float32)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """A colourspace: its transfer function plus how its linear values relate to sRGB linear.

    Adding a profile is adding one of these. Nothing else in `app/imaging` needs to change.
    """

    name: str
    icc_name: str
    """Human-readable profile name, used in output metadata and error messages.

    Not a path: ICC bytes are synthesised at run time by ``app/imaging/icc.py`` rather than read
    from disk, so there is no ``profiles/`` directory to keep in sync.
    """

    def decode(self, encoded: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def encode(self, linear: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def from_srgb_linear(self, linear_srgb: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def to_srgb_linear(self, linear_self: np.ndarray) -> np.ndarray:
        raise NotImplementedError


class _SRGB(Profile):
    def decode(self, encoded: np.ndarray) -> np.ndarray:
        return srgb_decode(encoded)

    def encode(self, linear: np.ndarray) -> np.ndarray:
        return srgb_encode(linear)

    def from_srgb_linear(self, linear_srgb: np.ndarray) -> np.ndarray:
        return np.asarray(linear_srgb, dtype=np.float32)

    def to_srgb_linear(self, linear_self: np.ndarray) -> np.ndarray:
        return np.asarray(linear_self, dtype=np.float32)


class _AdobeRGB(Profile):
    def decode(self, encoded: np.ndarray) -> np.ndarray:
        return adobe_rgb_decode(encoded)

    def encode(self, linear: np.ndarray) -> np.ndarray:
        return adobe_rgb_encode(linear)

    def from_srgb_linear(self, linear_srgb: np.ndarray) -> np.ndarray:
        return _apply_matrix(linear_srgb, _SRGB_LIN_TO_ADOBE_LIN)

    def to_srgb_linear(self, linear_self: np.ndarray) -> np.ndarray:
        return _apply_matrix(linear_self, _ADOBE_LIN_TO_SRGB_LIN)


SRGB = _SRGB(name="srgb", icc_name="sRGB-IEC61966-2.1.icc")
ADOBE_RGB = _AdobeRGB(name="adobe_rgb", icc_name="AdobeRGB1998.icc")

_BY_NAME: dict[str, Profile] = {SRGB.name: SRGB, ADOBE_RGB.name: ADOBE_RGB}


def get_profile(name: str) -> Profile:
    """Look up a profile by its `ColorProfile` enum value."""
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(
            f"unknown colour profile {name!r}; known: {sorted(_BY_NAME)}"
        ) from None


def _apply_matrix(img: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Apply a 3x3 matrix to the last axis of an (..., 3) array."""
    a = np.asarray(img, dtype=np.float32)
    if a.shape[-1] != 3:
        raise ValueError(f"expected last axis of size 3, got shape {a.shape}")
    return np.einsum("ij,...j->...i", m, a, optimize=True).astype(np.float32)


# ---------------------------------------------------------------------------
# Hex colours
# ---------------------------------------------------------------------------


def hex_to_srgb8(hex_colour: str) -> tuple[int, int, int]:
    """'#F5F5F5' -> (245, 245, 245). Assumes the value is already validated and normalised.

    `BackgroundSpec` normalises 3-digit forms and casing, so this stays strict on purpose:
    surprising input here means a caller bypassed the contract.
    """
    h = hex_colour.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"expected normalised '#RRGGBB', got {hex_colour!r}")
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        raise ValueError(f"non-hexadecimal digits in {hex_colour!r}") from None


def hex_to_srgb_linear(hex_colour: str) -> np.ndarray:
    """Hex (sRGB, gamma-encoded) -> linear-light sRGB triple, shape (3,) float32.

    This is the entry point for the background colour. The hex the user types is an **sRGB**
    value; the pipeline composites in linear light and may deliver in Adobe RGB, so it is
    converted at both steps rather than copied. Copying the numbers into an Adobe RGB file
    would deliver a visibly different colour than the one requested.
    """
    rgb8 = np.array(hex_to_srgb8(hex_colour), dtype=np.float32) / 255.0
    return srgb_decode(rgb8)


def srgb8_from_linear(linear_srgb: np.ndarray) -> tuple[int, int, int]:
    """Inverse of `hex_to_srgb_linear`, for round-trip assertions in tests."""
    enc = srgb_encode(np.asarray(linear_srgb, dtype=np.float32))
    v = np.rint(np.clip(enc, 0.0, 1.0) * 255.0).astype(np.int32).reshape(-1)
    return int(v[0]), int(v[1]), int(v[2])


# ---------------------------------------------------------------------------
# 8-bit boundaries
# ---------------------------------------------------------------------------


def to_uint8(encoded: np.ndarray) -> np.ndarray:
    """Encoded float [0,1] -> uint8, with round-half-away-from-zero.

    ``np.rint`` alone rounds halves to even, which would make a flat #808080 fill land on 127
    or 128 depending on the value's exact float representation. For a feature whose contract is
    "the background is exactly this colour", that is not acceptable.
    """
    x = np.clip(np.asarray(encoded, dtype=np.float32), 0.0, 1.0) * 255.0
    return np.floor(x + 0.5).astype(np.uint8)


def from_uint8(a: np.ndarray) -> np.ndarray:
    """uint8 -> encoded float32 [0,1]."""
    return (np.asarray(a, dtype=np.float32) / 255.0).astype(np.float32)


def to_uint16(encoded: np.ndarray) -> np.ndarray:
    """Encoded float [0,1] -> uint16, for 16-bit TIFF output."""
    x = np.clip(np.asarray(encoded, dtype=np.float32), 0.0, 1.0) * 65535.0
    return np.floor(x + 0.5).astype(np.uint16)


# ---------------------------------------------------------------------------
# Cross-profile conversion
# ---------------------------------------------------------------------------


def convert_linear(linear: np.ndarray, src: Profile, dst: Profile) -> np.ndarray:
    """Convert linear-light values between two profiles' primaries.

    Routed through sRGB linear as the connection space. That is exact for the profiles here —
    both are D65, so no chromatic adaptation is involved — and keeps each `Profile` responsible
    for only one relationship instead of N-squared pairs.
    """
    if src is dst:
        return np.asarray(linear, dtype=np.float32)
    return dst.from_srgb_linear(src.to_srgb_linear(linear))

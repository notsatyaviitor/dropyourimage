"""Image decode/normalise on the way in, and encode on the way out.

The boundary between file bytes and the float32 linear-light arrays the rest of `app/imaging`
works with. Everything between these two functions is profile-agnostic linear light; all the
messy real-world variation is handled here, once.

Decode normalises aggressively on purpose. A supplier's zip contains CMYK TIFFs, 16-bit PNGs,
phone JPEGs with EXIF rotation, and files tagged Adobe RGB — each of which silently produces a
wrong result if passed through unexamined.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageCms, ImageOps

from app.imaging import color as C
from app.imaging import icc
from app.models import ColorProfile, Note, OutputFormat

# Pillow's own decompression-bomb guard. Overridden per-call from settings; this is the ceiling.
Image.MAX_IMAGE_PIXELS = 80_000_000

_PIL_FORMAT = {
    OutputFormat.PNG: "PNG",
    OutputFormat.JPEG: "JPEG",
    OutputFormat.TIFF: "TIFF",
    OutputFormat.WEBP: "WEBP",
}


@dataclass
class DecodedImage:
    """A decoded source image in linear-light sRGB, whatever it arrived as."""

    rgb_linear: np.ndarray
    """(H, W, 3) float32 linear-light, sRGB primaries."""

    alpha: np.ndarray | None
    """(H, W) float32, or None when the source had no alpha channel."""

    source_size: tuple[int, int]
    """(width, height) after EXIF rotation — i.e. as a human sees it."""

    notes: list[Note] = field(default_factory=list)


class UnsupportedImageError(ValueError):
    """The bytes are not a usable image. Maps to `ErrorCode.IMAGE_DECODE_FAILED`."""


def decode(data: bytes, *, max_pixels: int | None = None) -> DecodedImage:
    """Decode arbitrary image bytes to linear-light sRGB.

    Handles, in order:

    * **EXIF orientation** — applied, so a phone shot is not silently sideways. Every downstream
      measurement (bounds, centring) would otherwise be computed on a rotated frame.
    * **Embedded ICC profiles** — converted to sRGB rather than ignored. A file tagged Adobe RGB
      whose numbers are read as sRGB comes out visibly desaturated.
    * **CMYK, greyscale, palette, 16-bit** — normalised to 8-bit RGB(A).
    """
    if max_pixels is not None:
        Image.MAX_IMAGE_PIXELS = max_pixels

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Image.DecompressionBombError:
        raise
    except Exception as exc:
        raise UnsupportedImageError(f"could not decode image: {exc}") from exc

    img = ImageOps.exif_transpose(img) or img

    src_profile = img.info.get("icc_profile")
    if src_profile and img.mode in ("RGB", "RGBA", "CMYK"):
        img = _convert_to_srgb(img, src_profile)

    has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    target_mode = "RGBA" if has_alpha else "RGB"
    if img.mode != target_mode:
        try:
            img = img.convert(target_mode)
        except Exception as exc:
            raise UnsupportedImageError(f"could not convert mode {img.mode!r}: {exc}") from exc

    arr = np.asarray(img)
    if arr.dtype == np.uint16:
        encoded = (arr.astype(np.float32) / 65535.0).astype(np.float32)
    elif arr.dtype == np.uint8:
        encoded = C.from_uint8(arr)
    else:
        encoded = np.clip(arr.astype(np.float32), 0.0, 1.0)

    if encoded.ndim == 2:
        encoded = np.dstack([encoded] * 3)

    alpha = encoded[..., 3].copy() if encoded.shape[-1] == 4 else None
    rgb_linear = C.srgb_decode(encoded[..., :3])

    return DecodedImage(
        rgb_linear=rgb_linear.astype(np.float32),
        alpha=alpha,
        source_size=(img.width, img.height),
    )


def _convert_to_srgb(img: Image.Image, src_profile: bytes) -> Image.Image:
    """Convert an image with an embedded profile into sRGB.

    Failures here are non-fatal: a malformed profile should not lose the whole image, so we fall
    back to treating the numbers as sRGB, which is what every other tool does anyway.
    """
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(src_profile))
        dst = ImageCms.createProfile("sRGB")
        out_mode = "RGBA" if img.mode == "RGBA" else "RGB"
        return ImageCms.profileToProfile(
            img, src, dst, renderingIntent=1, outputMode=out_mode
        ) or img
    except Exception:
        return img


def encode(
    rgb_linear: np.ndarray,
    alpha: np.ndarray | None,
    *,
    fmt: OutputFormat,
    profile: ColorProfile = ColorProfile.SRGB,
    jpeg_quality: int = 92,
    webp_quality: int = 90,
    embed_profile: bool = True,
) -> bytes:
    """Encode linear-light sRGB to file bytes in the requested format and colour profile.

    Conversion order matters: linear sRGB -> linear target primaries -> target transfer function
    -> 8-bit. Skipping the primaries step and just re-encoding is the "copy the hex instead of
    converting it" bug, which costs up to ~30 levels on a saturated colour.
    """
    if fmt is OutputFormat.PSD:
        raise ValueError("PSD is written by app.psd, not by the raster encoder")

    target = C.get_profile(profile.value)
    lin = C.convert_linear(np.asarray(rgb_linear, dtype=np.float32), C.SRGB, target)
    encoded8 = C.to_uint8(target.encode(lin))

    if fmt is OutputFormat.JPEG:
        if alpha is not None and float(np.asarray(alpha).min()) < 0.999:
            raise ValueError(
                "JPEG cannot carry transparency; flatten onto a background first "
                "(the contract rejects transparent+JPEG, so this is a pipeline bug)"
            )
        img = Image.fromarray(encoded8, mode="RGB")
    elif alpha is not None:
        a8 = C.to_uint8(np.asarray(alpha, dtype=np.float32))
        img = Image.fromarray(np.dstack([encoded8, a8]), mode="RGBA")
    else:
        img = Image.fromarray(encoded8, mode="RGB")

    params: dict[str, object] = {}
    if embed_profile:
        params["icc_profile"] = icc.profile_bytes(profile.value)

    if fmt is OutputFormat.JPEG:
        params.update(quality=jpeg_quality, subsampling=0, optimize=True)
        # subsampling=0 is 4:4:4. Chroma subsampling on a product shot smears saturated edges,
        # which is exactly what a packshot is judged on.
    elif fmt is OutputFormat.PNG:
        params.update(compress_level=6)
    elif fmt is OutputFormat.TIFF:
        params.update(compression="tiff_lzw")
    elif fmt is OutputFormat.WEBP:
        params.update(quality=webp_quality, method=4)

    buf = io.BytesIO()
    img.save(buf, format=_PIL_FORMAT[fmt], **params)
    return buf.getvalue()


def dimensions_of(data: bytes) -> tuple[int, int]:
    """Read (width, height) without decoding pixels — used for cheap output metadata."""
    with Image.open(io.BytesIO(data)) as img:
        return img.width, img.height

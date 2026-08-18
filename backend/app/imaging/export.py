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
from app.models import ColorProfile, Note, OutputFormat, SourceFormat

# Pillow's own decompression-bomb guard. Overridden per-call from settings; this is the ceiling.
Image.MAX_IMAGE_PIXELS = 80_000_000

_PIL_FORMAT = {
    OutputFormat.PNG: "PNG",
    OutputFormat.JPEG: "JPEG",
    OutputFormat.TIFF: "TIFF",
    OutputFormat.WEBP: "WEBP",
    OutputFormat.BMP: "BMP",
    OutputFormat.EPS: "EPS",
}

# Formats with no alpha channel at all. Handing them RGBA silently drops the mask, so the encoder
# refuses instead — the same reasoning the JPEG guard below already uses.
_NO_ALPHA_FORMATS = frozenset({OutputFormat.JPEG, OutputFormat.BMP, OutputFormat.EPS})


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


def decode(
    data: bytes, *, max_pixels: int | None = None, source: SourceFormat | None = None
) -> DecodedImage:
    """Decode arbitrary image bytes to linear-light sRGB.

    Handles, in order:

    * **Camera raw** — routed to libraw when `source` says so, since Pillow cannot read it.
    * **EXIF orientation** — applied, so a phone shot is not silently sideways. Every downstream
      measurement (bounds, centring) would otherwise be computed on a rotated frame.
    * **Embedded ICC profiles** — converted to sRGB rather than ignored. A file tagged Adobe RGB
      whose numbers are read as sRGB comes out visibly desaturated.
    * **CMYK, greyscale, palette, 16-bit** — normalised to RGB(A).

    `source` is a hint from the filename. It only selects the *decoder*; it is never trusted as
    proof of content, which the zip reader sniffs separately (docs/SECURITY.md).
    """
    if max_pixels is not None:
        Image.MAX_IMAGE_PIXELS = max_pixels

    from app.imaging import formats as F

    if source is not None and F.is_raw(source):
        return _decode_raw(data, source, max_pixels)

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
    if arr.ndim == 2:
        arr = np.dstack([arr] * 3)

    if arr.dtype == np.uint8:
        # The overwhelmingly common case, and the one worth a fast path: the table gives the
        # identical float32 values the elementwise EOTF would, without evaluating a power over
        # every pixel of a full-resolution master.
        alpha = C.from_uint8(arr[..., 3]) if arr.shape[-1] == 4 else None
        rgb_linear = C.srgb_decode_u8(arr[..., :3])
    else:
        if arr.dtype == np.uint16:
            encoded = (arr.astype(np.float32) / 65535.0).astype(np.float32)
        else:
            encoded = np.clip(arr.astype(np.float32), 0.0, 1.0)
        alpha = encoded[..., 3].copy() if encoded.shape[-1] == 4 else None
        rgb_linear = C.srgb_decode(encoded[..., :3])

    return DecodedImage(
        rgb_linear=rgb_linear.astype(np.float32),
        alpha=alpha,
        source_size=(img.width, img.height),
    )


def _decode_raw(
    data: bytes, source: SourceFormat, max_pixels: int | None
) -> DecodedImage:
    """Decode camera raw via libraw, returning linear-light sRGB.

    A raw file is undemosaiced sensor readout, so "decoding" it means a real rendering decision:
    demosaic, white balance, and the camera's colour matrix. The settings below are chosen so the
    result lands in the same space every other decoder path produces, because the rest of the
    pipeline assumes linear-light sRGB and nothing downstream can tell where the pixels came from.

    * ``output_bps=16`` — the entire reason a raw is worth accepting. Dropping to 8 here would
      make the 16-bit TIFF we substitute for it pointless.
    * ``gamma=(1, 1)`` and ``no_auto_bright=True`` — ask libraw for **linear** output and no
      auto-exposure. Its default is an sRGB-gamma, auto-brightened render, which would then be
      sRGB-decoded again below and come out wrong. Auto-brightness is also per-image, which would
      make two shots of the same product inconsistent.
    * ``use_camera_wb=True`` — the white balance the photographer set in-camera, rather than
      libraw guessing. For a packshot the studio WB is deliberate and should be honoured.
    * ``output_color=sRGB`` — the pipeline's working primaries.
    """
    try:
        import rawpy
    except ImportError as exc:  # pragma: no cover - rawpy is pinned in requirements.txt
        raise UnsupportedImageError(
            f"{source.value.upper()} needs the rawpy/libraw decoder, which is not installed. "
            "Install it with `pip install -r backend/requirements.txt`."
        ) from exc

    try:
        with rawpy.imread(io.BytesIO(data)) as raw:
            rgb16 = raw.postprocess(
                output_bps=16,
                gamma=(1, 1),
                no_auto_bright=True,
                use_camera_wb=True,
                output_color=rawpy.ColorSpace.sRGB,
            )
    except Exception as exc:
        raise UnsupportedImageError(
            f"could not decode {source.value.upper()} raw file: {exc}"
        ) from exc

    height, width = rgb16.shape[:2]
    ceiling = max_pixels if max_pixels is not None else Image.MAX_IMAGE_PIXELS
    if ceiling and width * height > ceiling:
        # Mirror Pillow's guard: raws are routinely 40-100 MP, so this is a live limit, not
        # theoretical. Raising the same exception type keeps the caller's handling uniform.
        raise Image.DecompressionBombError(
            f"raw image is {width * height} pixels, over the {ceiling} limit"
        )

    # Already linear thanks to gamma=(1, 1); only the 16-bit scale needs undoing. No srgb_decode
    # here — applying it would darken a linear image that never had a transfer curve applied.
    rgb_linear = (rgb16.astype(np.float32) / 65535.0).clip(0.0, 1.0)

    return DecodedImage(
        rgb_linear=rgb_linear,
        alpha=None,  # raw sensor data has no alpha channel; there is nothing to preserve
        source_size=(width, height),
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
    deep: bool = False,
) -> bytes:
    """Encode linear-light sRGB to file bytes in the requested format and colour profile.

    Conversion order matters: linear sRGB -> linear target primaries -> target transfer function
    -> 8-bit. Skipping the primaries step and just re-encoding is the "copy the hex instead of
    converting it" bug, which costs up to ~30 levels on a saturated colour.

    `deep` requests 16-bit output, and is only honoured for TIFF — the one writable format here
    that carries more than 8 bits. It exists for camera raw, which is substituted to TIFF
    precisely so the source's extra bit depth survives (`app/imaging/formats.py`).
    """
    if fmt is OutputFormat.PSD:
        raise ValueError("PSD is written by app.psd, not by the raster encoder")

    target = C.get_profile(profile.value)
    lin = C.convert_linear(np.asarray(rgb_linear, dtype=np.float32), C.SRGB, target)
    gamma_encoded = target.encode(lin)

    if deep and fmt is OutputFormat.TIFF:
        return _encode_tiff16(gamma_encoded, profile, embed_profile)

    encoded8 = C.to_uint8(gamma_encoded)

    if fmt in _NO_ALPHA_FORMATS:
        if alpha is not None and float(np.asarray(alpha).min()) < 0.999:
            raise ValueError(
                f"{fmt.value.upper()} cannot carry transparency; flatten onto a background "
                "first (the contract rejects transparent+JPEG, so this is a pipeline bug)"
            )
        img = Image.fromarray(encoded8, mode="RGB")
    elif alpha is not None:
        a8 = C.to_uint8(np.asarray(alpha, dtype=np.float32))
        img = Image.fromarray(np.dstack([encoded8, a8]), mode="RGBA")
    else:
        img = Image.fromarray(encoded8, mode="RGB")

    params: dict[str, object] = {}
    # EPS is PostScript; Pillow's writer takes no ICC profile and would raise on the keyword.
    if embed_profile and fmt is not OutputFormat.EPS:
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


#: TIFF tag 34675 (ICCProfile). tifffile takes raw tags rather than a Pillow-style keyword.
_TIFF_ICC_TAG = 34675


def _encode_tiff16(
    gamma_encoded: np.ndarray, profile: ColorProfile, embed_profile: bool
) -> bytes:
    """Write a true 16-bit RGB TIFF.

    **Pillow cannot do this.** Its `RGB` mode is 8-bit only, and `Image.fromarray(uint16_array,
    mode="RGB")` does not raise — it silently reinterprets the buffer and writes an 8-bit file with
    scrambled pixels. That was a real bug in the first version of this function, caught only by
    reading back `BitsPerSample`, which said `(8, 8, 8)`. Pillow's 16-bit support (`I;16`) is
    single-channel, so there is no Pillow path to 16-bit RGB at all.

    `tifffile` is already in the tree as a `psd-tools` dependency and writes this correctly, with
    the ICC profile attached as a raw tag. `zlib` rather than LZW because tifffile's LZW encoder
    needs the optional `imagecodecs` package; zlib is built in and compresses comparably.

    No alpha path: raw is the only source that asks for 16-bit and raw carries no alpha.
    """
    import tifffile

    extratags = []
    if embed_profile:
        blob = icc.profile_bytes(profile.value)
        extratags.append((_TIFF_ICC_TAG, "B", len(blob), blob, True))

    buf = io.BytesIO()
    tifffile.imwrite(
        buf,
        C.to_uint16(gamma_encoded),
        photometric="rgb",
        compression="zlib",
        extratags=extratags,
    )
    return buf.getvalue()


def dimensions_of(data: bytes) -> tuple[int, int]:
    """Read (width, height) without decoding pixels — used for cheap output metadata."""
    with Image.open(io.BytesIO(data)) as img:
        return img.width, img.height


#: Long edge of the "before" thumbnail. The side-by-side panel renders at roughly 370 CSS px, so
#: this covers a 2x display with room to spare while costing a few tens of KB rather than the tens
#: of megabytes a full-resolution re-encode would.
SOURCE_THUMBNAIL_LONG_EDGE = 768


def source_thumbnail(
    data: bytes,
    *,
    max_pixels: int | None = None,
    source: SourceFormat | None = None,
    long_edge: int = SOURCE_THUMBNAIL_LONG_EDGE,
) -> bytes:
    """A small PNG of a file *as it was uploaded*, for the UI's "before" panel.

    **No browser renders EPS, PSD, TIFF or camera raw.** `preview_format` already solves that for
    delivered outputs, but the before/after row shows the user's *source* file, which the browser
    was being asked to decode directly — so every PSD and EPS upload rendered an empty box next to
    a correct "after". The comparison is the whole point of that row, and for this client's
    material (132 PSDs) it was blank every time.

    Deliberately **not** run for formats a browser can already paint: those use the local `File`
    the user picked, which costs nothing, needs no round trip and is full resolution.

    Alpha is flattened onto white rather than preserved. This is a "what you uploaded" reference
    image, and a checkerboard here would be confused with the transparency the *output* panel next
    to it is genuinely showing.
    """
    import cv2

    decoded = decode(data, max_pixels=max_pixels, source=source)
    rgb = decoded.rgb_linear

    if decoded.alpha is not None:
        white = np.ones_like(rgb)
        rgb = rgb * decoded.alpha[..., None] + white * (1.0 - decoded.alpha[..., None])

    height, width = rgb.shape[:2]
    scale = min(1.0, long_edge / max(width, height))
    if scale < 1.0:
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
        rgb = np.stack(
            [cv2.resize(rgb[..., c], (new_w, new_h), interpolation=cv2.INTER_AREA) for c in range(3)],
            axis=-1,
        )

    return encode(rgb, None, fmt=OutputFormat.PNG, embed_profile=False)

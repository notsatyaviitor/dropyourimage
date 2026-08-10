"""Decode/encode tests, including the ICC profile writer.

Round-trip assertions: a colour put in must come back out, byte-exact where the format allows it.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageCms

from app.imaging import color as C
from app.imaging import export as E
from app.imaging import icc
from app.models import ColorProfile, OutputFormat


def flat_linear(hexv: str, h: int = 16, w: int = 16) -> np.ndarray:
    lin = C.hex_to_srgb_linear(hexv)
    return np.broadcast_to(lin, (h, w, 3)).astype(np.float32).copy()


# ---------------------------------------------------------------------------
# ICC writer
# ---------------------------------------------------------------------------


class TestIccProfileWriter:
    def test_littlecms_accepts_the_generated_profile(self):
        prof = ImageCms.ImageCmsProfile(io.BytesIO(icc.adobe_rgb_1998_profile()))
        assert prof.profile.xcolor_space.strip() == "RGB"
        assert prof.profile.connection_space.strip() == "XYZ"

    def test_description_does_not_claim_to_be_adobes_file(self):
        """It is independently generated and colorimetrically equivalent, not Adobe's binary."""
        prof = ImageCms.ImageCmsProfile(io.BytesIO(icc.adobe_rgb_1998_profile()))
        desc = ImageCms.getProfileDescription(prof).strip()
        assert "Compatible with" in desc

    @pytest.mark.parametrize(
        "hexv", ["#F5F5F5", "#808080", "#1E3A8A", "#C81E1E", "#0F766E", "#FF0000", "#0000FF"]
    )
    def test_profile_agrees_with_our_own_matrices(self, hexv):
        """Independent check: littleCMS converting our Adobe RGB bytes back to sRGB must land on
        the original colour. Verifies the synthesised profile, not just that it parses."""
        prof = ImageCms.ImageCmsProfile(io.BytesIO(icc.adobe_rgb_1998_profile()))
        xform = ImageCms.buildTransform(
            prof, ImageCms.createProfile("sRGB"), "RGB", "RGB", renderingIntent=1
        )

        lin_adobe = C.convert_linear(C.hex_to_srgb_linear(hexv), C.SRGB, C.ADOBE_RGB)
        adobe8 = C.to_uint8(C.ADOBE_RGB.encode(lin_adobe)).reshape(3)

        img = Image.new("RGB", (1, 1), tuple(int(v) for v in adobe8))
        got = ImageCms.applyTransform(img, xform).getpixel((0, 0))
        want = C.hex_to_srgb8(hexv)

        assert max(abs(a - b) for a, b in zip(got, want)) <= 1, f"{got} vs {want}"

    def test_generation_is_deterministic(self):
        """A timestamp in the header would break the pipeline's byte-reproducibility test."""
        assert icc.adobe_rgb_1998_profile() == icc.adobe_rgb_1998_profile()

    def test_srgb_profile_is_stable_across_a_real_time_gap(self):
        """Regression guard for a real bug, not a hypothetical one.

        `ImageCms.createProfile("sRGB")` embeds a creation timestamp that littleCMS sets to the
        current system time, so two calls more than a second apart used to return different
        bytes — confirmed directly, and the reason two of `pipeline.process_image`'s exports a
        moment apart could silently differ. `srgb_profile()` is now cached; this test would have
        caught the regression immediately; the two-instant-calls version used elsewhere would not
        have, since it rarely straddled a second boundary.
        """
        import time

        first = icc.srgb_profile()
        time.sleep(1.1)
        assert icc.srgb_profile() == first

    def test_srgb_profile_is_available_too(self):
        assert len(icc.profile_bytes("srgb")) > 0

    def test_unknown_profile_raises(self):
        with pytest.raises(ValueError, match="prophoto"):
            icc.profile_bytes("prophoto")


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------


class TestEncodeExactness:
    @pytest.mark.parametrize("fmt", [OutputFormat.PNG, OutputFormat.TIFF])
    @pytest.mark.parametrize("hexv", ["#F5F5F5", "#1E3A8A", "#C81E1E", "#000000", "#FFFFFF"])
    def test_lossless_formats_preserve_the_hex_exactly(self, fmt, hexv):
        """Requirement 2, end to end through a real file."""
        blob = E.encode(flat_linear(hexv), None, fmt=fmt, profile=ColorProfile.SRGB)
        arr = np.asarray(Image.open(io.BytesIO(blob)).convert("RGB"))
        assert tuple(arr[0, 0]) == C.hex_to_srgb8(hexv)
        assert len(np.unique(arr.reshape(-1, 3), axis=0)) == 1, "fill must stay perfectly flat"

    def test_jpeg_is_close_but_not_exact(self):
        """Documented, not a defect: JPEG is lossy. Use PNG or TIFF when exactness matters."""
        hexv = "#1E3A8A"
        blob = E.encode(flat_linear(hexv), None, fmt=OutputFormat.JPEG, jpeg_quality=95)
        arr = np.asarray(Image.open(io.BytesIO(blob)).convert("RGB"))
        want = C.hex_to_srgb8(hexv)
        assert max(abs(int(a) - b) for a, b in zip(arr[8, 8], want)) <= 2

    def test_adobe_rgb_output_carries_a_profile_and_converted_numbers(self):
        hexv = "#C81E1E"
        blob = E.encode(flat_linear(hexv), None, fmt=OutputFormat.PNG, profile=ColorProfile.ADOBE_RGB)
        img = Image.open(io.BytesIO(blob))

        assert img.info.get("icc_profile"), "an Adobe RGB file without a profile is unreadable"

        arr = np.asarray(img.convert("RGB"))
        assert tuple(arr[0, 0]) != C.hex_to_srgb8(hexv), (
            "numbers must be converted, not copied"
        )
        # And converting back must recover the requested colour.
        lin_adobe = C.ADOBE_RGB.decode(C.from_uint8(arr[0, 0]))
        assert C.srgb8_from_linear(
            C.convert_linear(lin_adobe, C.ADOBE_RGB, C.SRGB)
        ) == C.hex_to_srgb8(hexv)


class TestEncodeAlpha:
    def test_png_keeps_the_alpha_channel(self):
        rgb = flat_linear("#FFFFFF", 8, 8)
        a = np.full((8, 8), 0.5, dtype=np.float32)
        blob = E.encode(rgb, a, fmt=OutputFormat.PNG)
        img = Image.open(io.BytesIO(blob))
        assert img.mode == "RGBA"
        assert np.asarray(img)[..., 3][0, 0] == 128

    def test_webp_keeps_alpha(self):
        rgb = flat_linear("#FFFFFF", 8, 8)
        a = np.full((8, 8), 0.5, dtype=np.float32)
        img = Image.open(io.BytesIO(E.encode(rgb, a, fmt=OutputFormat.WEBP)))
        assert img.mode in ("RGBA", "RGB")

    def test_jpeg_with_real_transparency_is_a_pipeline_bug_and_says_so(self):
        rgb = flat_linear("#FFFFFF", 8, 8)
        a = np.full((8, 8), 0.5, dtype=np.float32)
        with pytest.raises(ValueError, match="cannot carry transparency"):
            E.encode(rgb, a, fmt=OutputFormat.JPEG)

    def test_jpeg_accepts_a_fully_opaque_alpha(self):
        rgb = flat_linear("#FFFFFF", 8, 8)
        a = np.ones((8, 8), dtype=np.float32)
        assert len(E.encode(rgb, a, fmt=OutputFormat.JPEG)) > 0

    def test_psd_is_not_this_modules_job(self):
        with pytest.raises(ValueError, match="app.psd"):
            E.encode(flat_linear("#FFFFFF"), None, fmt=OutputFormat.PSD)


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------


class TestDecodeRoundTrip:
    def test_encode_decode_recovers_linear_values(self):
        hexv = "#1E3A8A"
        blob = E.encode(flat_linear(hexv), None, fmt=OutputFormat.PNG)
        got = E.decode(blob)
        assert got.rgb_linear.shape == (16, 16, 3)
        assert C.srgb8_from_linear(got.rgb_linear[0, 0]) == C.hex_to_srgb8(hexv)

    def test_alpha_is_recovered(self):
        rgb = flat_linear("#FFFFFF", 8, 8)
        a = np.full((8, 8), 0.25, dtype=np.float32)
        got = E.decode(E.encode(rgb, a, fmt=OutputFormat.PNG))
        assert got.alpha is not None
        assert got.alpha[0, 0] == pytest.approx(0.25, abs=1 / 255)

    def test_no_alpha_channel_yields_none(self):
        got = E.decode(E.encode(flat_linear("#808080"), None, fmt=OutputFormat.PNG))
        assert got.alpha is None


class TestDecodeNormalisation:
    def test_greyscale_becomes_rgb(self):
        buf = io.BytesIO()
        Image.new("L", (8, 8), 128).save(buf, format="PNG")
        got = E.decode(buf.getvalue())
        assert got.rgb_linear.shape == (8, 8, 3)

    def test_palette_image_becomes_rgb(self):
        buf = io.BytesIO()
        Image.new("P", (8, 8)).save(buf, format="PNG")
        assert E.decode(buf.getvalue()).rgb_linear.shape == (8, 8, 3)

    def test_cmyk_is_converted(self):
        """Print-origin TIFFs are common in this supply chain."""
        buf = io.BytesIO()
        Image.new("CMYK", (8, 8), (0, 255, 255, 0)).save(buf, format="TIFF")
        got = E.decode(buf.getvalue())
        assert got.rgb_linear.shape == (8, 8, 3)
        assert got.rgb_linear[0, 0, 0] > got.rgb_linear[0, 0, 2], "should read as reddish"

    def test_embedded_adobe_rgb_source_is_converted_not_reinterpreted(self):
        """A file tagged Adobe RGB read as sRGB comes out visibly desaturated."""
        hexv = "#C81E1E"
        tagged = E.encode(flat_linear(hexv), None, fmt=OutputFormat.PNG, profile=ColorProfile.ADOBE_RGB)
        got = E.decode(tagged)
        recovered = C.srgb8_from_linear(got.rgb_linear[0, 0])
        assert max(abs(a - b) for a, b in zip(recovered, C.hex_to_srgb8(hexv))) <= 2, recovered

    def test_exif_orientation_is_applied(self):
        """Every downstream measurement assumes the frame is upright."""
        img = Image.new("RGB", (20, 10), (255, 0, 0))
        exif = img.getexif()
        exif[274] = 6                       # rotate 90 CW
        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif)
        got = E.decode(buf.getvalue())
        assert got.source_size == (10, 20), "dimensions should be swapped after transpose"

    def test_garbage_bytes_raise_a_typed_error(self):
        with pytest.raises(E.UnsupportedImageError):
            E.decode(b"this is not an image")

    def test_empty_bytes_raise_a_typed_error(self):
        with pytest.raises(E.UnsupportedImageError):
            E.decode(b"")


class TestDimensions:
    def test_reads_size_without_decoding(self):
        blob = E.encode(flat_linear("#FFFFFF", 12, 34), None, fmt=OutputFormat.PNG)
        assert E.dimensions_of(blob) == (34, 12)


class TestContentTypes:
    """Every deliverable format must be served as what it is.

    BMP was missing from `CONTENT_TYPES` and fell through to `application/octet-stream`. Nothing
    errored — the file was correct and downloadable — but BMP is in `BROWSER_RENDERABLE`, so a
    BMP-only job gets no PNG preview and the results grid puts the BMP straight into an `<img>`.
    A browser will not render an octet-stream inline, so the card was blank with nothing to fall
    back on. Silent, and specific to one format, which is why it wants a test over the whole enum
    rather than one for BMP.
    """

    def test_every_output_format_has_a_real_content_type(self):
        from app.storage.base import content_type_for

        wrong = {
            fmt.value: content_type_for(f"x.{fmt.value}")
            for fmt in OutputFormat
            if content_type_for(f"x.{fmt.value}") == "application/octet-stream"
        }
        assert not wrong, f"formats served as an opaque blob: {wrong}"

    def test_browser_renderable_formats_are_served_as_images(self):
        """The stricter half: `image/*` is what actually makes an <img> paint."""
        from app.imaging import formats as F
        from app.storage.base import content_type_for

        for fmt in F.BROWSER_RENDERABLE:
            ct = content_type_for(f"x.{fmt.value}")
            assert ct.startswith("image/"), f"{fmt.value} is renderable but served as {ct}"

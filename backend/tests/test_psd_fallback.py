"""PSD fallback writer + validator tests. Requires `pytoshop` — see
requirements-psd-fallback.txt. Skips cleanly if it is not installed, so the rest of the suite
never depends on it.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

pytoshop = pytest.importorskip("pytoshop", reason="PSD fallback writer needs pytoshop")

from app.imaging import color as C
from app.models import ColorProfile, PsdSpec
from app.psd import vector_path as vp
from app.psd.fallback import build_psd
from app.psd.validate import validate_psd


def product_scene(size: int = 120):
    """Product + shadow, matching what pipeline.py's placement stage hands to the PSD builder."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    dist = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
    alpha = np.clip(size * 0.28 - dist + 0.5, 0.0, 1.0).astype(np.float32)

    product_rgb = np.zeros((size, size, 3), dtype=np.float32)
    product_rgb[..., 0] = C.srgb_decode(np.float32(0.78))

    shadow_ratio = np.ones((size, size), dtype=np.float32)
    shadow_ratio[int(size * 0.7) : int(size * 0.85), int(size * 0.3) : int(size * 0.7)] = 0.6

    return product_rgb, alpha, shadow_ratio


DEFAULT_SPEC = PsdSpec()


class TestBuildPsd:
    def test_produces_nonempty_bytes(self):
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#1E3A8A")
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=DEFAULT_SPEC,
        )
        assert len(result.data) > 0

    def test_starts_with_the_psd_magic_bytes(self):
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#FFFFFF")
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=DEFAULT_SPEC,
        )
        assert result.data[:4] == b"8BPS"

    def test_without_a_shadow_no_shadow_layer_and_no_crash(self):
        product_rgb, alpha, _ = product_scene()
        bg = C.hex_to_srgb_linear("#FFFFFF")
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=None, spec=DEFAULT_SPEC,
        )
        v = validate_psd(result.data, DEFAULT_SPEC)
        assert DEFAULT_SPEC.shadow_layer_name not in v.layer_names

    def test_custom_layer_names_are_honoured(self):
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#FFFFFF")
        spec = PsdSpec(
            product_layer_name="ITEM", shadow_layer_name="SHDW", background_layer_name="CANVAS"
        )
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=spec,
        )
        v = validate_psd(result.data, spec)
        assert set(v.layer_names) == {"ITEM", "SHDW", "CANVAS"}

    def test_vector_path_disabled_produces_no_path_resource(self):
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#FFFFFF")
        spec = PsdSpec(vector_clipping_path=False)
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=spec,
        )
        from app.models import Note

        assert Note.PSD_FALLBACK_RASTER in result.notes
        v = validate_psd(result.data, spec)
        assert v.ok  # no path was requested, so its absence is not a validation problem

    def test_empty_alpha_falls_back_to_raster_with_a_note(self):
        """Nothing to trace — the caller gets a usable file, flagged, not an exception."""
        h = w = 60
        product_rgb = np.zeros((h, w, 3), dtype=np.float32)
        alpha = np.zeros((h, w), dtype=np.float32)
        bg = C.hex_to_srgb_linear("#FFFFFF")
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=None, spec=DEFAULT_SPEC,
        )
        from app.models import Note

        assert Note.PSD_FALLBACK_RASTER in result.notes
        assert len(result.data) > 0


class TestComposite:
    """Renders the actual layer stack and checks it — the class of bug that name/structure
    checks alone cannot catch. Two real defects were found exactly this way while building this
    module: layers passed in the wrong order (an opaque background covered everything above it,
    since `nested_layers_to_psd` wants top-to-bottom input — confirmed against its source, see
    fallback.py) and the shadow being applied twice (baked into the background layer's own pixels
    *and* present as a separate shadow layer stacked on top of it). Both produced a file that
    looked completely fine on every structural check (right layer names, right count, valid ICC)
    and only showed the actual problem when rendered.

    `psd_tools.PSDImage.composite(force=True)` is required here: `pytoshop` never bothers writing
    a real flattened top-level preview, only a blank placeholder, and `psd-tools`' composite()
    silently prefers that stale preview over real layer compositing whenever the file merely
    *declares* one exists — regardless of whether it has real content. `force=True` bypasses that
    and always composites from the actual layer stack, which is what a genuine open in Photoshop
    would do too.
    """

    def test_product_layer_is_on_top_not_hidden_under_the_background(self):
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#1E3A8A")
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=DEFAULT_SPEC,
        )

        composited = _composite(result.data)
        centre = composited[60, 60]  # product_scene() centres its product at (60, 60) on a 120px canvas
        bg8 = C.hex_to_srgb8("#1E3A8A")

        assert not _close(centre, bg8, tol=10), (
            f"centre pixel is background colour {tuple(centre)} — the product layer is hidden "
            "beneath an opaque background layer rather than stacked on top of it"
        )
        assert centre[0] > centre[2], "expected a reddish product, got a blue-dominant pixel"

    def test_shadow_is_applied_once_not_twice(self):
        """Checks the *darkening ratio* the composite actually shows, not exact RGB channel
        values.

        Measured directly: `psd-tools`' compositor blends the shadow layer's alpha in
        gamma-encoded space, not linear light — a composited shadow pixel comes out close to
        `encoded_background x ratio`, not the raster pipeline's linear-light-correct multiply. That
        is an inherent, understood approximation of representing a linear-light shadow as an
        alpha-blended "Normal" layer (see fallback.py's shadow-layer comment), not a bug, and
        chasing exact RGB agreement with the raster pipeline would be chasing an impossible
        target. The luminance *ratio*, however, survives the gamma/linear difference well enough
        to distinguish "applied once" (~ratio) from "applied twice" (~ratio^2) clearly — that
        distinction is what actually matters here, and it is what this test checks.
        """
        product_rgb, alpha, ratio = product_scene()
        bg_linear = C.hex_to_srgb_linear("#1E3A8A")

        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg_linear, shadow_ratio=ratio, spec=DEFAULT_SPEC,
        )
        composited = _composite(result.data)

        shadow_mask = (ratio < 0.9) & (alpha < 0.01)
        assert shadow_mask.any(), "fixture must actually contain a shadow region to test this"
        ys, xs = np.nonzero(shadow_mask)
        py, px = ys[len(ys) // 2], xs[len(xs) // 2]  # a representative interior shadow pixel
        true_ratio = float(ratio[py, px])

        flat_bg_encoded = C.to_uint8(C.SRGB.encode(bg_linear))
        lum_flat = _luma(flat_bg_encoded.astype(np.float64))
        lum_shadow = _luma(composited[py, px].astype(np.float64))
        observed_ratio = lum_shadow / lum_flat

        # Generous absolute tolerance around the true ratio — covers 8-bit quantization, the
        # gamma-vs-linear approximation, and psd-tools' own compositing rounding — but nowhere
        # near wide enough to also cover ratio^2 (0.36 here vs a true ratio of 0.6).
        assert abs(observed_ratio - true_ratio) < 0.15, (
            f"observed darkening ratio {observed_ratio:.3f} does not track the requested "
            f"ratio {true_ratio:.3f} (ratio^2 would be {true_ratio**2:.3f}) — looks like the "
            f"shadow was applied more than once"
        )


def _composite(psd_bytes: bytes) -> np.ndarray:
    import io

    from psd_tools import PSDImage

    psd = PSDImage.open(io.BytesIO(psd_bytes))
    return np.asarray(psd.composite(force=True).convert("RGB"))


def _close(a, b, tol: int) -> bool:
    return bool(np.all(np.abs(np.asarray(a, dtype=np.int32) - np.asarray(b, dtype=np.int32)) <= tol))


def _luma(rgb) -> float:
    """Rec.709 weights on encoded (not linear) values — crude but sufficient for a ratio check
    that only needs to distinguish "roughly ratio" from "roughly ratio squared"."""
    return float(0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2])


class TestValidatePsdHappyPath:
    def test_a_correctly_built_file_validates_clean(self):
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#1E3A8A")
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=DEFAULT_SPEC,
        )
        v = validate_psd(result.data, DEFAULT_SPEC, expected_size=(120, 120))
        assert v.ok, v.problems
        assert set(v.layer_names) == {"PROD", "SHADOW", "BG"}
        assert v.has_vector_path
        assert v.path_vertex_count is not None and v.path_vertex_count >= 3
        assert v.color_mode is not None and "RGB" in v.color_mode
        assert v.depth == 8

    def test_icc_profile_is_adobe_rgb_by_default(self):
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#FFFFFF")
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=DEFAULT_SPEC,
        )
        v = validate_psd(result.data, DEFAULT_SPEC)
        assert v.ok

    def test_srgb_profile_is_also_detected_correctly(self):
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#FFFFFF")
        spec = PsdSpec(profile=ColorProfile.SRGB)
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=spec,
        )
        v = validate_psd(result.data, spec)
        assert v.ok, v.problems


class TestValidatePsdCatchesRealProblems:
    def test_garbage_bytes_do_not_raise(self):
        v = validate_psd(b"this is not a PSD file", DEFAULT_SPEC)
        assert not v.ok
        assert v.problems

    def test_wrong_canvas_size_is_caught(self):
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#FFFFFF")
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=DEFAULT_SPEC,
        )
        v = validate_psd(result.data, DEFAULT_SPEC, expected_size=(999, 999))
        assert not v.ok
        assert any("999" in p for p in v.problems)

    def test_missing_required_layer_is_caught(self):
        """Ask the validator to check for a layer name the file was never built with."""
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#FFFFFF")
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=DEFAULT_SPEC,
        )
        mismatched_spec = PsdSpec(product_layer_name="NOT_WHAT_WAS_BUILT")
        v = validate_psd(result.data, mismatched_spec)
        assert not v.ok
        assert any("NOT_WHAT_WAS_BUILT" in p for p in v.problems)

    def test_clockwise_wound_path_is_flagged(self):
        """The independent validator must catch a real winding-direction defect, not just agree
        with whatever the encoder produced — construct one with the wrong winding directly."""
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#FFFFFF")
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=DEFAULT_SPEC,
        )

        import io

        import pytoshop
        from pytoshop.image_resources import GenericImageResourceBlock, ImageResources

        psd = pytoshop.read(io.BytesIO(result.data))
        # Rebuild the path data with points in reverse (clockwise) order.
        square = [(20.0, 20.0), (100.0, 20.0), (100.0, 100.0), (20.0, 100.0)]  # CW in image coords
        body = bytearray()
        body += struct.pack('>H', 0)
        body += struct.pack('>H', len(square))
        body += b'\x00' * 22
        for x, y in square:
            body += struct.pack('>H', 1)
            body += vp._encode_knot(y / 120, x / 120)

        blocks = [
            b for b in psd.image_resources.blocks
            if getattr(b, 'resource_id', None) not in (vp.PATH_RESOURCE_ID,)
        ]
        blocks.append(
            GenericImageResourceBlock(name=DEFAULT_SPEC.path_name, resource_id=vp.PATH_RESOURCE_ID, data=bytes(body))
        )
        psd.image_resources = ImageResources(blocks=blocks)

        buf = io.BytesIO()
        psd.write(buf)

        v = validate_psd(buf.getvalue(), DEFAULT_SPEC)
        assert not v.ok
        assert any("clockwise" in p for p in v.problems)

    def test_vector_path_requested_but_absent_is_flagged(self):
        """A file built without a path, validated against a spec that expects one."""
        product_rgb, alpha, ratio = product_scene()
        bg = C.hex_to_srgb_linear("#FFFFFF")
        built_without_path = PsdSpec(vector_clipping_path=False)
        result = build_psd(
            product_rgb_linear=product_rgb, product_alpha=alpha,
            background_color_linear=bg, shadow_ratio=ratio, spec=built_without_path,
        )
        spec_expecting_path = PsdSpec(vector_clipping_path=True)
        v = validate_psd(result.data, spec_expecting_path)
        assert not v.ok
        assert any("no path resource" in p for p in v.problems)

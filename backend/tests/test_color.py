"""Colour management tests.

No fixtures, no images, no keys — pure maths against known values.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.imaging import color as C


class TestTransferFunctions:
    def test_srgb_round_trip(self):
        x = np.linspace(0, 1, 4096, dtype=np.float32)
        assert np.allclose(C.srgb_encode(C.srgb_decode(x)), x, atol=1e-6)

    def test_adobe_rgb_round_trip(self):
        x = np.linspace(0, 1, 4096, dtype=np.float32)
        assert np.allclose(C.adobe_rgb_encode(C.adobe_rgb_decode(x)), x, atol=1e-6)

    def test_srgb_anchors(self):
        assert C.srgb_decode(np.float32(0.0)) == pytest.approx(0.0, abs=1e-9)
        assert C.srgb_decode(np.float32(1.0)) == pytest.approx(1.0, abs=1e-6)
        # The canonical value for 50% grey. If this moves, the transfer function is wrong.
        assert C.srgb_decode(np.float32(0.5)) == pytest.approx(0.214041, abs=1e-5)

    def test_srgb_uses_the_linear_toe_not_a_pure_power(self):
        """Below 0.04045 sRGB is linear. Cut-out edges and soft shadows live here."""
        x = np.float32(0.02)
        assert C.srgb_decode(x) == pytest.approx(0.02 / 12.92, rel=1e-6)
        assert C.srgb_decode(x) != pytest.approx(0.02**2.4, rel=1e-3)


class TestHexHandling:
    @pytest.mark.parametrize("hexv", ["#F5F5F5", "#000000", "#FFFFFF", "#1E3A8A", "#808080"])
    def test_hex_to_linear_and_back_is_exact(self, hexv):
        assert C.srgb8_from_linear(C.hex_to_srgb_linear(hexv)) == C.hex_to_srgb8(hexv)

    def test_rejects_unnormalised_input(self):
        """`BackgroundSpec` normalises; reaching here with a short form means a bypassed contract."""
        with pytest.raises(ValueError):
            C.hex_to_srgb8("#FFF")

    def test_rejects_non_hex(self):
        with pytest.raises(ValueError):
            C.hex_to_srgb8("#GGGGGG")


class TestUint8Boundary:
    def test_every_level_round_trips_exactly(self):
        """A flat fill must not drift by a level.

        Banker's rounding would land #808080 on 127 or 128 depending on float representation.
        For a feature whose contract is "the background is exactly this colour", that is a bug.
        """
        for v in range(256):
            flat = np.full((4, 4, 3), v / 255.0, dtype=np.float32)
            assert (C.to_uint8(flat) == v).all(), f"level {v} drifted"

    def test_clamps_out_of_range(self):
        assert C.to_uint8(np.array([-0.5, 1.5], dtype=np.float32)).tolist() == [0, 255]


class TestProfileConversion:
    def test_srgb_to_itself_is_identity(self):
        lin = C.hex_to_srgb_linear("#1E3A8A")
        assert np.array_equal(C.convert_linear(lin, C.SRGB, C.SRGB), lin)

    def test_adobe_rgb_contains_srgb_so_nothing_clips(self):
        """Adobe RGB's gamut is a superset of sRGB; conversion never needs gamut mapping."""
        for hexv in ["#FF0000", "#00FF00", "#0000FF", "#FF00FF", "#00FFFF", "#FFFF00"]:
            adobe = C.convert_linear(C.hex_to_srgb_linear(hexv), C.SRGB, C.ADOBE_RGB)
            assert (adobe >= -1e-4).all() and (adobe <= 1.0 + 1e-4).all(), hexv

    @pytest.mark.parametrize(
        "hexv", ["#F5F5F5", "#808080", "#1E3A8A", "#C81E1E", "#0F766E", "#FFFFFF", "#000000"]
    )
    def test_conversion_is_colorimetrically_lossless(self, hexv):
        lin = C.hex_to_srgb_linear(hexv)
        adobe = C.convert_linear(lin, C.SRGB, C.ADOBE_RGB)
        back = C.convert_linear(adobe, C.ADOBE_RGB, C.SRGB)
        assert C.srgb8_from_linear(back) == C.hex_to_srgb8(hexv)

    def test_neutral_axis_stays_neutral(self):
        """Both profiles are D65, so greys must not pick up a colour cast."""
        for v in (16, 64, 128, 200, 240):
            hexv = f"#{v:02X}{v:02X}{v:02X}"
            adobe = C.convert_linear(C.hex_to_srgb_linear(hexv), C.SRGB, C.ADOBE_RGB)
            assert adobe.max() - adobe.min() < 1e-5, hexv


class TestWhyConversionMatters:
    """Regression guard on the size of the effect, so the docs stay honest.

    Measured on this implementation: copying sRGB bytes verbatim into an Adobe RGB file is
    harmless for white but produces a large error for saturated brand colours. These numbers
    are quoted in docs/IMAGE_PIPELINE.md.
    """

    @staticmethod
    def _naive_copy_error(hexv: str) -> int:
        """Error a viewer sees if bytes are copied into an Adobe RGB file instead of converted."""
        copied = np.array(C.hex_to_srgb8(hexv), dtype=np.float32) / 255.0
        seen = C.srgb8_from_linear(
            C.convert_linear(C.ADOBE_RGB.decode(copied), C.ADOBE_RGB, C.SRGB)
        )
        return max(abs(a - b) for a, b in zip(C.hex_to_srgb8(hexv), seen))

    def test_white_is_unaffected(self):
        """Why nobody noticed: the common e-commerce background is white."""
        assert self._naive_copy_error("#FFFFFF") == 0
        assert self._naive_copy_error("#F5F5F5") == 0

    @pytest.mark.parametrize("hexv,floor", [("#1E3A8A", 20), ("#C81E1E", 20)])
    def test_saturated_brand_colours_are_badly_wrong(self, hexv, floor):
        assert self._naive_copy_error(hexv) >= floor

    def test_correct_path_has_zero_error(self):
        """Converting rather than copying is exact for every case above."""
        for hexv in ["#FFFFFF", "#F5F5F5", "#1E3A8A", "#C81E1E"]:
            lin = C.hex_to_srgb_linear(hexv)
            adobe = C.convert_linear(lin, C.SRGB, C.ADOBE_RGB)
            back = C.srgb8_from_linear(C.convert_linear(adobe, C.ADOBE_RGB, C.SRGB))
            assert back == C.hex_to_srgb8(hexv), hexv


class TestProfileRegistry:
    def test_lookup_by_enum_value(self):
        assert C.get_profile("srgb") is C.SRGB
        assert C.get_profile("adobe_rgb") is C.ADOBE_RGB

    def test_unknown_profile_names_the_known_ones(self):
        with pytest.raises(ValueError, match="adobe_rgb"):
            C.get_profile("prophoto")

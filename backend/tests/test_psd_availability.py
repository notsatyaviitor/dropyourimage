"""Regressions for the two defects that made layered PSD fail outright.

Both were invisible because `test_psd_fallback.py` and `test_psd_pipeline.py` skipped on a missing
`pytoshop`, so the suite was green while the whole PSD stage was dead. That is the real lesson
here: a skipped suite is not a passing one, and these two tests are written so they cannot skip.

1. **`pytoshop` was imported at module scope.** `service.py` imports `fallback.py` and
   `pipeline.py` imports `service.py`, so an absent wheel raised ModuleNotFoundError during import
   and every PSD job died as `internal_error` — even though `PsdUnavailable` exists for this.
2. **The validator string-matched an IntEnum.** Python 3.11 changed `IntEnum.__str__` to return the
   bare number, so `'RGB' not in str(psd.color_mode)` became true for every valid RGB PSD, and the
   validator rejected correct files.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.errors import PsdUnavailable
from app.models import ErrorCode, PsdSpec
from app.psd import fallback as F
from app.psd.validate import _mode_name


class TestMissingPytoshopIsTyped:
    def test_build_psd_raises_psd_unavailable_rather_than_a_module_error(self, monkeypatch):
        monkeypatch.setattr(F, "pytoshop", None)
        monkeypatch.setattr(F, "PYTOSHOP_IMPORT_ERROR", "No module named 'pytoshop'")

        with pytest.raises(PsdUnavailable) as exc:
            F.build_psd(
                product_rgb_linear=np.zeros((4, 4, 3), np.float32),
                product_alpha=np.zeros((4, 4), np.float32),
                background_color_linear=np.zeros(3, np.float32),
                shadow_ratio=None,
                spec=PsdSpec(),
            )

        assert exc.value.code is ErrorCode.PSD_UNAVAILABLE

    def test_the_error_says_what_to_do_about_it(self, monkeypatch):
        """An operator reading this in the UI needs the fix, not just the diagnosis."""
        monkeypatch.setattr(F, "pytoshop", None)
        monkeypatch.setattr(F, "PYTOSHOP_IMPORT_ERROR", "No module named 'pytoshop'")

        with pytest.raises(PsdUnavailable) as exc:
            F.build_psd(
                product_rgb_linear=np.zeros((4, 4, 3), np.float32),
                product_alpha=np.zeros((4, 4), np.float32),
                background_color_linear=np.zeros(3, np.float32),
                shadow_ratio=None,
                spec=PsdSpec(),
            )

        message = str(exc.value)
        assert "requirements.txt" in message
        assert "pytoshop" in message

    def test_importing_the_module_never_depends_on_pytoshop(self):
        """The import itself must be safe, or a PNG-only job dies on a box with no PSD writer."""
        import importlib

        module = importlib.import_module("app.psd.fallback")
        assert hasattr(module, "build_psd")
        assert hasattr(module, "PYTOSHOP_IMPORT_ERROR")


class TestMergedComposite:
    """The delivered PSD rendered as a solid black image everywhere except Photoshop.

    `nested_layers_to_psd` writes layer records but never populates `PsdFile.image_data`, the
    flattened preview at the end of the file. Photoshop ignores it and re-composites from the
    layers, so the file looked right in the one place nobody could check — while OS thumbnailers,
    preview apps and `PSDImage.composite()` all read the blank section and showed black.

    Reported from a real delivery: layers `BG` mean 1.000 and `PROD` mean 0.161, composite mean
    0.000. Structural validation passed the whole time, which is why these assert on *pixels*.
    """

    @staticmethod
    def _built(size: int = 64, *, shadow: bool = True):
        from tests.test_shadow import studio_scene

        observed, alpha, ratio = studio_scene(size=size)
        return F.build_psd(
            product_rgb_linear=observed,
            product_alpha=alpha,
            background_color_linear=np.full(3, 0.96, np.float32),
            shadow_ratio=ratio if shadow else None,
            spec=PsdSpec(enabled=True),
        )

    def _composite(self, data: bytes) -> np.ndarray:
        import io

        from psd_tools import PSDImage

        return np.asarray(PSDImage.open(io.BytesIO(data)).composite()).astype(np.float32)

    def test_the_flattened_preview_is_not_black(self):
        merged = self._composite(self._built().data)
        assert merged.max() > 0, "merged image section is all zeros — the original bug"
        assert merged.mean() > 20, f"preview is near-black (mean {merged.mean():.1f})"

    def test_the_background_corner_survives_into_the_preview(self):
        """A corner is pure background, so it must read light — not black, not the product."""
        merged = self._composite(self._built().data)
        assert merged[0, 0].min() > 200, f"corner is {tuple(merged[0, 0])}, expected near-white"

    def test_the_preview_matches_an_independent_render_of_the_layers(self):
        """A preview that disagrees with its own layers is a worse bug than a blank one."""
        import io

        from psd_tools import PSDImage

        data = self._built().data
        psd = PSDImage.open(io.BytesIO(data))
        merged = np.asarray(psd.composite()).astype(np.float32)

        layers = {layer.name.rstrip("\x00"): layer.numpy() for layer in psd}
        out = layers["BG"][..., :3].copy()
        for name in ("SHADOW", "PROD"):
            lay = layers[name]
            out = lay[..., :3] * lay[..., 3:4] + out * (1.0 - lay[..., 3:4])

        assert np.abs(merged - out * 255.0).max() <= 2.0

    def test_works_without_a_shadow_layer(self):
        """The reported file had no SHADOW layer — the shadow gate had failed on it."""
        merged = self._composite(self._built(shadow=False).data)
        assert merged.max() > 0
        assert merged[0, 0].min() > 200


class TestNonSquareCanvas:
    """Every PSD this project wrote was square, which hid a transposed canvas for weeks.

    `pytoshop.user.nested_layers.nested_layers_to_psd` documents its `size` argument as
    ``(height, width)`` and then implements ``width, height = size``. The docstring is wrong.
    `build_psd` trusted it and passed ``(h, w)``, so any non-square PSD came out with its canvas
    transposed against its own layer data.

    Nothing caught it because the PSD fixtures and every real 500x500 delivery were square. These
    tests are deliberately non-square, and asymmetric enough that a transpose cannot pass.
    """

    @staticmethod
    def _build(h: int, w: int):
        rgb = np.full((h, w, 3), 0.4, np.float32)
        alpha = np.zeros((h, w), np.float32)
        alpha[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4] = 1.0
        return F.build_psd(
            product_rgb_linear=rgb,
            product_alpha=alpha,
            background_color_linear=np.full(3, 0.9, np.float32),
            shadow_ratio=None,
            spec=PsdSpec(enabled=True),
        )

    @pytest.mark.parametrize(
        "h,w", [(30, 50), (628, 1200), (1200, 628)], ids=["landscape", "banner", "portrait"]
    )
    def test_canvas_matches_the_source_orientation(self, h, w):
        import io

        from psd_tools import PSDImage

        psd = PSDImage.open(io.BytesIO(self._build(h, w).data))
        assert (psd.width, psd.height) == (w, h)

    def test_a_non_square_psd_validates_against_its_true_size(self):
        from app.psd.validate import validate_psd

        data = self._build(30, 50).data
        assert validate_psd(data, PsdSpec(enabled=True), expected_size=(50, 30)).ok

    def test_a_non_square_psd_still_writes_a_readable_composite(self):
        """The transpose used to make pytoshop raise 'Image is the wrong shape' on write."""
        import io

        from psd_tools import PSDImage

        psd = PSDImage.open(io.BytesIO(self._build(30, 50).data))
        assert np.asarray(psd.composite()).max() > 0


class TestColourModeNaming:
    def test_reads_the_name_off_an_int_enum(self):
        """The actual regression: str() on an IntEnum returns '3' on Python 3.11+, not 'RGB'."""
        from psd_tools.constants import ColorMode

        assert str(ColorMode.RGB) == "3", "psd-tools changed; re-check this helper"
        assert _mode_name(ColorMode.RGB) == "RGB"

    @pytest.mark.parametrize(
        "value,expected",
        [
            pytest.param(3, "3", id="bare-int"),
            pytest.param("RGB", "RGB", id="already-a-string"),
        ],
    )
    def test_tolerates_non_enum_inputs(self, value, expected):
        assert _mode_name(value) == expected

    def test_a_real_rgb_psd_validates_clean(self):
        """End to end: build one and confirm the validator accepts it. This is the assertion that
        would have caught the bug, and it cannot skip."""
        from app.psd.validate import validate_psd

        spec = PsdSpec(enabled=True)
        built = F.build_psd(
            product_rgb_linear=np.full((32, 32, 3), 0.5, np.float32),
            product_alpha=np.ones((32, 32), np.float32),
            background_color_linear=np.full(3, 0.9, np.float32),
            shadow_ratio=None,
            spec=spec,
        )
        result = validate_psd(built.data, spec, expected_size=(32, 32))

        assert result.color_mode == "RGB"
        assert result.ok, result.problems

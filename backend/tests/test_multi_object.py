"""Layer-per-object PSD for a scene.

The packshot path delivers one product on a replaced background. This delivers a *scene*: the
frame as the bottom layer, then one named, individually-masked layer per object, each with its own
saved path. Different enough that it diverges in `process_image` rather than inside stage 1.

Gemini's enumeration is stubbed — the object list is a model's judgement and cannot be asserted
against, and the suite must run with no keys. What *is* asserted is everything deterministic
downstream: layer count, naming, stacking order, path allocation, and the canvas transform.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from psd_tools import PSDImage

from app.core.settings import Settings
from app.engines.base import AlphaResult
from app.imaging.geometry import BBox
from app.models import (
    BackgroundSpec,
    CutoutSpec,
    EngineId,
    EngineStrategy,
    ExportSpec,
    JobConfig,
    Note,
    OutputFormat,
    PsdSpec,
    SizeSpec,
)
from app.psd import vector_path as vp
from app.psd.fallback import build_layered_psd


def settings(**kw) -> Settings:
    base = dict(_env_file=None, gemini_api_key="k", photoroom_api_key="p")
    base.update(kw)
    return Settings(**base)


def frame(h: int = 80, w: int = 80) -> np.ndarray:
    rgb = np.full((h, w, 3), 0.35, np.float32)
    rgb[10:40, 10:40] = 0.8
    return rgb


def blob(h: int, w: int, box: tuple[int, int, int, int]) -> np.ndarray:
    a = np.zeros((h, w), np.float32)
    x0, y0, x1, y1 = box
    a[y0:y1, x0:x1] = 1.0
    return a


class TestLayeredPsdWriter:
    def _psd(self, objects):
        data = build_layered_psd(
            frame_rgb_linear=frame(), objects=objects, spec=PsdSpec(enabled=True)
        ).data
        return PSDImage.open(io.BytesIO(data)), data

    def test_one_layer_per_object_plus_the_frame(self):
        objects = [
            ("bed", blob(80, 80, (5, 30, 70, 75))),
            ("pillow", blob(80, 80, (10, 32, 25, 45))),
            ("lamp", blob(80, 80, (60, 10, 72, 28))),
        ]
        psd, _ = self._psd(objects)
        names = [layer.name.rstrip("\x00") for layer in psd]

        assert names[0] == "BG", "the frame must be the bottom layer"
        assert set(names[1:]) == {"bed", "pillow", "lamp"}
        assert len(names) == 4

    def test_smaller_objects_stack_above_larger_ones(self):
        """A pillow sits on the bed; buried under it, the layer would be unusable."""
        objects = [
            ("bed", blob(80, 80, (5, 30, 75, 78))),
            ("pillow", blob(80, 80, (10, 32, 25, 45))),
        ]
        psd, _ = self._psd(objects)
        names = [layer.name.rstrip("\x00") for layer in psd]

        assert names.index("pillow") > names.index("bed")

    def test_each_object_gets_its_own_saved_path(self):
        objects = [
            ("bed", blob(80, 80, (5, 30, 70, 75))),
            ("pillow", blob(80, 80, (10, 32, 25, 45))),
        ]
        psd, _ = self._psd(objects)
        ids = {int(k) for k in psd._record.image_resources if 2000 <= int(k) <= 2997}

        assert ids == {vp.PATH_RESOURCE_ID, vp.PATH_RESOURCE_ID + 1}

    def test_exactly_one_path_is_designated_the_clipping_path(self):
        """PSD allows many saved paths but only one clipping path — the largest object."""
        objects = [
            ("bed", blob(80, 80, (5, 30, 70, 75))),
            ("pillow", blob(80, 80, (10, 32, 25, 45))),
        ]
        psd, _ = self._psd(objects)
        designations = [
            k for k in psd._record.image_resources
            if int(k) == vp.CLIPPING_PATH_DESIGNATION_ID
        ]
        assert len(designations) == 1

    def test_each_layer_masks_only_its_own_object(self):
        objects = [
            ("left", blob(80, 80, (0, 0, 30, 80))),
            ("right", blob(80, 80, (50, 0, 80, 80))),
        ]
        psd, _ = self._psd(objects)
        by_name = {l.name.rstrip("\x00"): l.numpy() for l in psd}

        assert by_name["left"][40, 10, 3] > 0.5     # inside left
        assert by_name["left"][40, 70, 3] < 0.5     # not inside left
        assert by_name["right"][40, 70, 3] > 0.5

    def test_the_flattened_preview_is_the_frame_not_black(self):
        psd, _ = self._psd([("bed", blob(80, 80, (5, 30, 70, 75)))])
        assert np.asarray(psd.composite()).mean() > 20

    def test_no_objects_is_a_typed_error(self):
        from app.core.errors import PsdUnavailable

        with pytest.raises(PsdUnavailable):
            build_layered_psd(
                frame_rgb_linear=frame(), objects=[], spec=PsdSpec(enabled=True)
            )

    def test_a_non_square_scene_keeps_its_orientation(self):
        data = build_layered_psd(
            frame_rgb_linear=frame(40, 90),
            objects=[("bed", blob(40, 90, (5, 5, 80, 35)))],
            spec=PsdSpec(enabled=True),
        ).data
        psd = PSDImage.open(io.BytesIO(data))
        assert (psd.width, psd.height) == (90, 40)


class _Spy:
    """Segments whatever crop it is handed, so ROI geometry is exercised for real."""

    id = EngineId.LOCAL
    cost_per_image_usd = 0.02

    def available(self):
        return True

    async def alpha_for(self, data, width, height):
        a = np.zeros((height, width), np.float32)
        a[height // 5 : 4 * height // 5, width // 5 : 4 * width // 5] = 1.0
        return AlphaResult(alpha=a, engine=self.id, latency_ms=1, cost_usd=0.02)


def _stub_enumeration(monkeypatch, found):
    async def fake(_locator, _bytes, _w, _h, **_kw):
        return found

    monkeypatch.setattr("app.engines.locate.locate_many", fake)


class TestMultiObjectPipeline:
    def _config(self, **kw):
        base = dict(
            cutout=CutoutSpec(
                strategy=EngineStrategy.SINGLE, engine=EngineId.LOCAL, multi_object=True
            ),
            background=BackgroundSpec(color="#FFFFFF"),
            size=SizeSpec(width=120, height=120, margin_pct=0.0),
            psd=PsdSpec(enabled=True),
            export=ExportSpec(formats=[OutputFormat.PSD], match_source=False),
        )
        base.update(kw)
        return JobConfig(**base)

    async def test_produces_a_layer_per_enumerated_object(self, monkeypatch):
        from app.engines.locate import Located
        from app import pipeline
        from app.imaging import export as E

        _stub_enumeration(monkeypatch, [
            Located(box=BBox(5, 5, 60, 60), raw_box=BBox(5, 5, 60, 60), label="bed"),
            Located(box=BBox(60, 10, 90, 40), raw_box=BBox(60, 10, 90, 40), label="lamp"),
        ])

        png = E.encode(frame(100, 100), None, fmt=OutputFormat.PNG)
        result, outputs = await pipeline.process_image(
            png, "room.png", self._config(), settings(), engines=[_Spy()]
        )

        assert result.state.value == "done", result.error
        assert Note.MULTI_OBJECT in result.notes
        assert result.layers == ["bed", "lamp"]
        assert OutputFormat.PSD in outputs

    async def test_cost_scales_with_the_object_count(self, monkeypatch):
        """One segmentation call per object — the headline cost of this mode."""
        from app.engines.locate import Located
        from app import pipeline
        from app.imaging import export as E

        _stub_enumeration(monkeypatch, [
            Located(box=BBox(5, 5, 60, 60), raw_box=BBox(5, 5, 60, 60), label=f"o{i}")
            for i in range(4)
        ])

        png = E.encode(frame(100, 100), None, fmt=OutputFormat.PNG)
        result, _ = await pipeline.process_image(
            png, "room.png", self._config(), settings(), engines=[_Spy()]
        )

        assert result.cost_usd == pytest.approx(0.08)  # 4 objects x $0.02

    async def test_no_objects_found_fails_with_an_actionable_error(self, monkeypatch):
        from app import pipeline
        from app.imaging import export as E

        _stub_enumeration(monkeypatch, [])

        png = E.encode(frame(100, 100), None, fmt=OutputFormat.PNG)
        result, outputs = await pipeline.process_image(
            png, "room.png", self._config(), settings(), engines=[_Spy()]
        )

        assert result.state.value == "failed"
        assert result.error.code.value == "no_foreground_found"
        assert "turn it off" in result.error.message
        assert outputs == {}

    async def test_canvas_pads_with_the_background_colour_not_black(self, monkeypatch):
        """Zeros are black; a frame smaller than the canvas floated in a black surround."""
        from app.engines.locate import Located
        from app import pipeline
        from app.imaging import export as E

        # Box must sit inside the 50x50 frame; an out-of-bounds ROI crops to nothing and the
        # image fails with no layers, which is correct behaviour but not what this test is for.
        _stub_enumeration(monkeypatch, [
            Located(box=BBox(5, 5, 45, 45), raw_box=BBox(5, 5, 45, 45), label="bed"),
        ])

        png = E.encode(frame(50, 50), None, fmt=OutputFormat.PNG)
        config = self._config(
            psd=PsdSpec(enabled=False),
            export=ExportSpec(formats=[OutputFormat.PNG], match_source=False),
        )
        _result, outputs = await pipeline.process_image(
            png, "room.png", config, settings(), engines=[_Spy()]
        )

        from PIL import Image

        out = np.asarray(Image.open(io.BytesIO(outputs[OutputFormat.PNG])).convert("RGB"))
        assert tuple(out[0, 0]) == (255, 255, 255)

    async def test_off_by_default_leaves_the_packshot_path_untouched(self, monkeypatch):
        """`multi_object` off must not produce layers.

        `locate_many` is used as the tripwire, but it stopped being exclusive to this feature on
        14 Aug 2026 — automatic subject detection enumerates through the same function. Auto
        detection is disabled here so the tripwire means what it says; that it also runs is
        covered in tests/test_auto_subject.py.
        """
        from app import pipeline
        from app.imaging import export as E

        def explode(*_a, **_kw):
            pytest.fail("enumeration ran with multi_object off")

        monkeypatch.setattr("app.engines.locate.locate_many", explode)

        png = E.encode(frame(100, 100), None, fmt=OutputFormat.PNG)
        config = JobConfig(
            cutout=CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.LOCAL),
            export=ExportSpec(formats=[OutputFormat.PNG], match_source=False),
        )
        result, _ = await pipeline.process_image(
            png,
            "shot.png",
            config,
            settings(auto_subject_enabled=False),
            engines=[_Spy()],
        )

        assert Note.MULTI_OBJECT not in result.notes
        assert result.layers == []


class TestDuplicateLabels:
    def test_repeated_labels_are_disambiguated(self):
        """Two layers called 'pillow' are not addressable by a retoucher."""
        from app.engines.locate import _parse_many

        body = {"candidates": [{"content": {"parts": [{"text":
            '[{"box_2d":[0,0,10,10],"label":"pillow"},'
            ' {"box_2d":[20,20,30,30],"label":"pillow"}]'}]}}]}
        entries = _parse_many(body)
        assert [e["label"] for e in entries] == ["pillow", "pillow"]
        # Disambiguation happens in locate_many; see its `used` counter.


class TestFailureReporting:
    """Every object failing must report *why*, not blame the photograph.

    `_segment_objects` discarded the per-object failures and returned `SUBJECT_NOT_FOUND`, so a
    vendor rate-limit surfaced as "No separable objects were found in this image" — pointing the
    operator at a room plainly full of objects. Found on a real run when Photoroom started
    429-ing; this mode fires one call per object, so a nine-object room is a nine-call burst and
    rate limiting is an expected failure rather than a rare one.
    """

    class _RateLimited:
        id = EngineId.LOCAL
        cost_per_image_usd = 0.02

        def available(self):
            return True

        async def alpha_for(self, data, width, height):
            from app.core import errors

            raise errors.VendorRateLimited("photoroom is rate-limiting requests")

    class _NoForeground:
        id = EngineId.LOCAL
        cost_per_image_usd = 0.02

        def available(self):
            return True

        async def alpha_for(self, data, width, height):
            from app.core import errors

            raise errors.NoForegroundFound("nothing to cut out")

    def _config(self):
        return JobConfig(
            cutout=CutoutSpec(
                strategy=EngineStrategy.SINGLE, engine=EngineId.LOCAL, multi_object=True
            ),
            background=BackgroundSpec(color="#FFFFFF"),
            size=SizeSpec(width=120, height=120, margin_pct=0.0),
            psd=PsdSpec(enabled=True),
            export=ExportSpec(formats=[OutputFormat.PSD], match_source=False),
        )

    def _found(self, monkeypatch, n=3):
        from app.engines.locate import Located

        _stub_enumeration(monkeypatch, [
            Located(box=BBox(5, 5, 60, 60), raw_box=BBox(5, 5, 60, 60), label=f"o{i}")
            for i in range(n)
        ])

    async def test_a_rate_limit_is_reported_as_a_rate_limit(self, monkeypatch):
        from app import pipeline
        from app.imaging import export as E

        self._found(monkeypatch)
        png = E.encode(frame(100, 100), None, fmt=OutputFormat.PNG)
        result, _ = await pipeline.process_image(
            png, "room.png", self._config(), settings(), engines=[self._RateLimited()]
        )

        assert result.state.value == "failed"
        assert result.error.code.value == "vendor_rate_limited"
        assert result.error.retryable is True

    async def test_a_vendor_failure_is_not_reported_as_an_empty_scene(self, monkeypatch):
        """The exact misreport: objects were enumerated, so 'no objects found' is a lie."""
        from app import pipeline
        from app.imaging import export as E

        self._found(monkeypatch)
        png = E.encode(frame(100, 100), None, fmt=OutputFormat.PNG)
        result, _ = await pipeline.process_image(
            png, "room.png", self._config(), settings(), engines=[self._RateLimited()]
        )

        assert result.error.code.value != "no_foreground_found"
        assert "No separable objects" not in result.error.message

    async def test_a_genuine_empty_scene_still_says_so(self, monkeypatch):
        """The honest case must survive the fix: nothing enumerated means nothing was there."""
        from app import pipeline
        from app.imaging import export as E

        _stub_enumeration(monkeypatch, [])
        png = E.encode(frame(100, 100), None, fmt=OutputFormat.PNG)
        result, _ = await pipeline.process_image(
            png, "room.png", self._config(), settings(), engines=[self._RateLimited()]
        )

        assert result.error.code.value == "no_foreground_found"
        assert "turn it off" in result.error.message

    async def test_the_most_actionable_failure_wins(self, monkeypatch):
        """Ranked like _stage1_cutout: a rate limit outranks 'no foreground' across objects."""
        from app import pipeline
        from app.imaging import export as E

        self._found(monkeypatch, n=2)
        png = E.encode(frame(100, 100), None, fmt=OutputFormat.PNG)
        result, _ = await pipeline.process_image(
            png, "room.png", self._config(), settings(),
            engines=[self._NoForeground(), self._RateLimited()],
        )

        assert result.error.code.value == "vendor_rate_limited"

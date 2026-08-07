"""Input format coverage and same-format-out delivery.

The requirement is "accept these 14 formats, and give me back what I sent". Eight of them can be
written, six cannot, and the interesting tests are the ones that pin *which* and prove the swap is
reported rather than silent.

What is genuinely covered here, and what is not
-----------------------------------------------
Real encode/decode round trips for every writable format. For camera raw, the *routing and
substitution* logic is covered with a stubbed libraw — there are no camera files in the repo (and
none may be added: `data/` is gitignored, and tests must run with no images). So these tests prove
we call libraw for a `.cr2` and deliver 16-bit TIFF with a note; they do **not** prove libraw
decodes a real Canon file correctly. `docs/LIMITATIONS.md` says so too.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from app.imaging import export as E
from app.imaging import formats as F
from app.models import (
    ColorProfile,
    ExportSpec,
    JobConfig,
    Note,
    OutputFormat,
    SourceFormat,
)
from app.pipeline import _resolve_formats

WRITABLE = [
    OutputFormat.PNG,
    OutputFormat.JPEG,
    OutputFormat.TIFF,
    OutputFormat.WEBP,
    OutputFormat.BMP,
    OutputFormat.EPS,
]


def scene(h: int = 40, w: int = 60) -> np.ndarray:
    rgb = np.zeros((h, w, 3), np.float32)
    rgb[:, :, 0] = 0.5
    rgb[10:30, 20:40, :] = 0.9
    return rgb


class TestFormatRegistry:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("a.png", SourceFormat.PNG), ("a.PNG", SourceFormat.PNG),
            ("a.jpg", SourceFormat.JPEG), ("a.jpeg", SourceFormat.JPEG),
            ("a.bmp", SourceFormat.BMP),
            ("a.tif", SourceFormat.TIFF), ("a.tiff", SourceFormat.TIFF),
            ("a.eps", SourceFormat.EPS), ("a.psd", SourceFormat.PSD),
            ("a.crw", SourceFormat.CRW), ("a.cr2", SourceFormat.CR2),
            ("a.cr3", SourceFormat.CR3), ("a.dng", SourceFormat.DNG),
            ("a.nef", SourceFormat.NEF), ("a.raw", SourceFormat.RAW),
            ("no-extension", None), ("a.txt", None),
        ],
    )
    def test_every_requested_extension_is_recognised(self, name, expected):
        assert F.source_from_name(name) is expected

    def test_a_dotted_filename_uses_the_last_extension(self):
        assert F.source_from_name("shot.v2.final.cr2") is SourceFormat.CR2

    @pytest.mark.parametrize(
        "source",
        [SourceFormat.CRW, SourceFormat.CR2, SourceFormat.CR3,
         SourceFormat.DNG, SourceFormat.NEF, SourceFormat.RAW],
    )
    def test_raw_cannot_round_trip_and_substitutes_16bit_tiff(self, source):
        assert F.is_raw(source)
        assert not F.can_round_trip(source)
        fmt, substituted = F.output_for(source)
        assert fmt is OutputFormat.TIFF
        assert substituted is True

    @pytest.mark.parametrize(
        "source,expected",
        [
            (SourceFormat.PNG, OutputFormat.PNG),
            (SourceFormat.JPEG, OutputFormat.JPEG),
            (SourceFormat.BMP, OutputFormat.BMP),
            (SourceFormat.TIFF, OutputFormat.TIFF),
            (SourceFormat.WEBP, OutputFormat.WEBP),
            (SourceFormat.EPS, OutputFormat.EPS),
            (SourceFormat.PSD, OutputFormat.PSD),
        ],
    )
    def test_writable_sources_round_trip_exactly(self, source, expected):
        fmt, substituted = F.output_for(source)
        assert fmt is expected
        assert substituted is False

    def test_every_writable_output_has_a_pillow_or_psd_writer(self):
        """Guards against adding an OutputFormat with no way to actually produce it."""
        for fmt in OutputFormat:
            assert fmt is OutputFormat.PSD or fmt in E._PIL_FORMAT


class TestEncodeEveryWritableFormat:
    @pytest.mark.parametrize("fmt", WRITABLE, ids=[f.value for f in WRITABLE])
    def test_encodes_and_decodes_back_at_the_same_size(self, fmt):
        data = E.encode(scene(), None, fmt=fmt)
        assert data, f"{fmt.value} produced no bytes"
        decoded = E.decode(data)
        assert decoded.source_size == (60, 40)

    @pytest.mark.parametrize("fmt", WRITABLE, ids=[f.value for f in WRITABLE])
    def test_pillow_agrees_on_the_format_written(self, fmt):
        data = E.encode(scene(), None, fmt=fmt)
        with Image.open(io.BytesIO(data)) as img:
            assert img.format == E._PIL_FORMAT[fmt]

    @pytest.mark.parametrize(
        "fmt", [OutputFormat.JPEG, OutputFormat.BMP, OutputFormat.EPS],
        ids=["jpeg", "bmp", "eps"],
    )
    def test_alpha_free_formats_refuse_transparency_rather_than_dropping_it(self, fmt):
        """Silently discarding the mask would deliver an opaque cut-out that looks 'nearly right'."""
        alpha = np.zeros((40, 60), np.float32)
        with pytest.raises(ValueError, match="cannot carry transparency"):
            E.encode(scene(), alpha, fmt=fmt)

    def test_eps_gets_no_icc_keyword(self):
        """Pillow's EPS writer rejects icc_profile; passing it would raise on every EPS job."""
        assert E.encode(scene(), None, fmt=OutputFormat.EPS, embed_profile=True)

    def test_psd_is_not_written_by_the_raster_encoder(self):
        with pytest.raises(ValueError, match="written by app.psd"):
            E.encode(scene(), None, fmt=OutputFormat.PSD)


class TestSixteenBitTiff:
    """Pillow silently writes 8-bit when handed a uint16 array as mode RGB — the original bug."""

    def test_deep_tiff_is_actually_sixteen_bit(self):
        data = E.encode(scene(), None, fmt=OutputFormat.TIFF, deep=True)
        with Image.open(io.BytesIO(data)) as img:
            assert img.tag_v2[258] == (16, 16, 16)

    def test_shallow_tiff_stays_eight_bit(self):
        data = E.encode(scene(), None, fmt=OutputFormat.TIFF, deep=False)
        with Image.open(io.BytesIO(data)) as img:
            assert img.tag_v2[258] == (8, 8, 8)

    def test_deep_tiff_still_embeds_the_icc_profile(self):
        data = E.encode(scene(), None, fmt=OutputFormat.TIFF, deep=True,
                        profile=ColorProfile.ADOBE_RGB)
        with Image.open(io.BytesIO(data)) as img:
            assert 34675 in img.tag_v2

    def test_deep_tiff_round_trips_through_our_own_decoder(self):
        data = E.encode(scene(), None, fmt=OutputFormat.TIFF, deep=True)
        assert E.decode(data).source_size == (60, 40)

    def test_deep_is_ignored_for_formats_that_cannot_carry_it(self):
        """Only TIFF has a 16-bit path; asking for deep PNG must not error or change the format."""
        data = E.encode(scene(), None, fmt=OutputFormat.PNG, deep=True)
        with Image.open(io.BytesIO(data)) as img:
            assert img.format == "PNG"


class TestResolveFormats:
    def _config(self, **export) -> JobConfig:
        return JobConfig(export=ExportSpec(**export))

    def test_match_source_adds_the_source_format(self):
        cfg = self._config(formats=[OutputFormat.PNG], match_source=True)
        formats, notes = _resolve_formats(cfg, SourceFormat.TIFF)
        assert formats == [OutputFormat.PNG, OutputFormat.TIFF]
        assert notes == []

    def test_match_source_does_not_duplicate_an_already_requested_format(self):
        cfg = self._config(formats=[OutputFormat.PNG], match_source=True)
        formats, _ = _resolve_formats(cfg, SourceFormat.PNG)
        assert formats == [OutputFormat.PNG]

    def test_match_source_off_honours_only_the_explicit_list(self):
        cfg = self._config(formats=[OutputFormat.PNG], match_source=False)
        formats, notes = _resolve_formats(cfg, SourceFormat.TIFF)
        assert formats == [OutputFormat.PNG]
        assert notes == []

    def test_raw_source_resolves_to_tiff_and_reports_the_substitution(self):
        cfg = self._config(formats=[OutputFormat.PNG], match_source=True)
        formats, notes = _resolve_formats(cfg, SourceFormat.CR2)
        assert OutputFormat.TIFF in formats
        assert Note.FORMAT_SUBSTITUTED in notes

    def test_an_unknown_extension_falls_back_to_the_explicit_list(self):
        cfg = self._config(formats=[OutputFormat.PNG], match_source=True)
        formats, notes = _resolve_formats(cfg, None)
        assert formats == [OutputFormat.PNG]
        assert notes == []

    def test_explicit_request_order_is_preserved_ahead_of_the_source_format(self):
        cfg = self._config(formats=[OutputFormat.JPEG, OutputFormat.PNG], match_source=True)
        formats, _ = _resolve_formats(cfg, SourceFormat.BMP)
        assert formats == [OutputFormat.JPEG, OutputFormat.PNG, OutputFormat.BMP]


class TestVendorReadableUpload:
    """Engines get the original bytes — except for formats no vendor can read.

    Found end to end, not in a unit test: an EPS upload came back from Photoroom as a bare HTTP
    400. PSD and camera raw are the same class of problem. The fix re-encodes to PNG for the vendor
    call while keeping the original bytes as the cache identity.
    """

    @pytest.mark.parametrize(
        "source",
        [SourceFormat.PNG, SourceFormat.JPEG, SourceFormat.BMP,
         SourceFormat.TIFF, SourceFormat.WEBP],
    )
    def test_web_formats_go_to_the_vendor_untouched(self, source):
        assert F.needs_reencode_for_vendor(source) is False

    @pytest.mark.parametrize(
        "source",
        [SourceFormat.EPS, SourceFormat.PSD, SourceFormat.CR2,
         SourceFormat.CR3, SourceFormat.NEF, SourceFormat.DNG,
         SourceFormat.CRW, SourceFormat.RAW],
    )
    def test_vendor_unreadable_formats_are_reencoded(self, source):
        assert F.needs_reencode_for_vendor(source) is True

    def test_an_unknown_extension_is_left_alone(self):
        """It decoded, so it is an ordinary image; re-encoding every stranger costs more."""
        assert F.needs_reencode_for_vendor(None) is False

    async def test_the_engine_receives_a_png_for_an_eps_source(self):
        """The bug: Photoroom rejected the EPS bytes outright with a 400."""
        from app.core.settings import Settings
        from app.engines.base import AlphaResult
        from app.models import CutoutSpec, EngineId, EngineStrategy

        seen: dict = {}

        class Spy:
            id = EngineId.LOCAL
            cost_per_image_usd = 0.0

            def available(self):
                return True

            async def alpha_for(self, data, width, height):
                seen["magic"] = data[:4]
                a = np.zeros((height, width), np.float32)
                a[height // 4 : 3 * height // 4, width // 4 : 3 * width // 4] = 1.0
                return AlphaResult(alpha=a, engine=self.id, latency_ms=1, cost_usd=0.0)

        from app import pipeline

        eps = E.encode(scene(80, 80), None, fmt=OutputFormat.EPS)
        config = JobConfig(
            cutout=CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.LOCAL)
        )
        result, _ = await pipeline.process_image(
            eps, "art.eps", config, Settings(_env_file=None), engines=[Spy()]
        )

        assert result.state.value == "done", result.error
        assert seen["magic"] == b"\x89PNG", "vendor got raw EPS bytes, not a PNG"


class TestPsdViaMatchSource:
    async def test_a_psd_source_delivers_a_psd_without_psd_enabled(self):
        """`match_source` resolved PSD but stage 5b only ran on `psd.enabled`, so none was written."""
        from app.core.settings import Settings
        from app.models import CutoutSpec, EngineId, EngineStrategy, PsdSpec
        from app.psd.fallback import build_psd
        from app import pipeline

        rgb = scene(60, 60)
        built = build_psd(
            product_rgb_linear=rgb,
            product_alpha=np.ones((60, 60), np.float32),
            background_color_linear=np.full(3, 0.9, np.float32),
            shadow_ratio=None,
            spec=PsdSpec(enabled=True),
        )

        config = JobConfig(
            cutout=CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.LOCAL),
            export=ExportSpec(formats=[OutputFormat.PNG], match_source=True),
        )
        assert config.psd.enabled is False

        result, outputs = await pipeline.process_image(
            built.data, "layered.psd", config, Settings(_env_file=None)
        )

        assert result.state.value == "done", result.error
        assert OutputFormat.PSD in outputs
        assert outputs[OutputFormat.PSD][:4] == b"8BPS"


class TestRawDecodeRouting:
    """libraw is stubbed: no camera files exist in the repo and none may be added."""

    @staticmethod
    def _stub(monkeypatch, pixels: np.ndarray):
        import sys
        import types

        captured: dict = {}

        class FakeRaw:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def postprocess(self, **kw):
                captured.update(kw)
                return pixels

        module = types.ModuleType("rawpy")
        module.imread = lambda _fh: FakeRaw()
        module.ColorSpace = types.SimpleNamespace(sRGB="sRGB")
        monkeypatch.setitem(sys.modules, "rawpy", module)
        return captured

    def test_a_cr2_is_routed_to_libraw_not_pillow(self, monkeypatch):
        pixels = np.full((10, 20, 3), 30000, np.uint16)
        self._stub(monkeypatch, pixels)

        decoded = E.decode(b"not-a-png", source=SourceFormat.CR2)

        assert decoded.source_size == (20, 10)
        assert decoded.alpha is None

    def test_libraw_is_asked_for_linear_16bit_camera_white_balance(self, monkeypatch):
        """Each of these changes the pixels materially — see _decode_raw's docstring."""
        captured = self._stub(monkeypatch, np.zeros((4, 4, 3), np.uint16))
        E.decode(b"raw", source=SourceFormat.NEF)

        assert captured["output_bps"] == 16
        assert captured["gamma"] == (1, 1)
        assert captured["no_auto_bright"] is True
        assert captured["use_camera_wb"] is True

    def test_raw_output_is_treated_as_already_linear(self, monkeypatch):
        """gamma=(1,1) means libraw returns linear light; srgb_decode would darken it wrongly."""
        self._stub(monkeypatch, np.full((4, 4, 3), 32768, np.uint16))
        decoded = E.decode(b"raw", source=SourceFormat.CR3)

        assert float(decoded.rgb_linear.max()) == pytest.approx(0.5, abs=0.01)

    def test_an_oversized_raw_trips_the_pixel_guard(self, monkeypatch):
        self._stub(monkeypatch, np.zeros((3000, 3000, 3), np.uint16))
        with pytest.raises(Image.DecompressionBombError):
            E.decode(b"raw", source=SourceFormat.CR2, max_pixels=1_000_000)

    def test_a_libraw_failure_becomes_an_unsupported_image_error(self, monkeypatch):
        import sys
        import types

        module = types.ModuleType("rawpy")

        def boom(_fh):
            raise RuntimeError("unrecognised file format")

        module.imread = boom
        module.ColorSpace = types.SimpleNamespace(sRGB="sRGB")
        monkeypatch.setitem(sys.modules, "rawpy", module)

        with pytest.raises(E.UnsupportedImageError, match="CR2"):
            E.decode(b"junk", source=SourceFormat.CR2)

    def test_non_raw_sources_never_reach_libraw(self, monkeypatch):
        """A PNG must decode through Pillow even though a source hint was supplied."""
        import sys
        import types

        module = types.ModuleType("rawpy")
        module.imread = lambda _fh: pytest.fail("libraw called for a PNG")
        module.ColorSpace = types.SimpleNamespace(sRGB="sRGB")
        monkeypatch.setitem(sys.modules, "rawpy", module)

        png = E.encode(scene(), None, fmt=OutputFormat.PNG)
        assert E.decode(png, source=SourceFormat.PNG).source_size == (60, 40)


class TestBrowserPreview:
    """A correct TIFF that no browser can paint showed as an empty results card.

    `CompleteStep` puts an output URL straight into an `<img>`. Chrome, Firefox and Edge render
    none of TIFF, EPS or PSD, so a job delivering only those produced files that were perfectly
    good and a UI that looked broken. The backend now adds one viewable PNG purely to display —
    reported as `preview_format`, and kept out of the download bundle because nobody asked for it.
    """

    @staticmethod
    async def _run(formats, *, name="shot.png", match=False, psd=False):
        from app import pipeline
        from app.core.settings import Settings
        from app.models import CutoutSpec, EngineId, EngineStrategy, PsdSpec

        png = E.encode(scene(120, 120), None, fmt=OutputFormat.PNG)
        config = JobConfig(
            cutout=CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.LOCAL),
            psd=PsdSpec(enabled=psd),
            export=ExportSpec(formats=formats, match_source=match),
        )
        return await pipeline.process_image(png, name, config, Settings(_env_file=None))

    @pytest.mark.parametrize(
        "fmt", [OutputFormat.TIFF, OutputFormat.EPS], ids=["tiff", "eps"]
    )
    async def test_an_unviewable_only_job_gains_a_png_preview(self, fmt):
        result, outputs = await self._run([fmt])

        assert result.preview_format is OutputFormat.PNG
        assert OutputFormat.PNG in outputs
        assert fmt in outputs, "the requested format must still be delivered"

    @pytest.mark.parametrize(
        "fmt",
        [OutputFormat.PNG, OutputFormat.JPEG, OutputFormat.WEBP, OutputFormat.BMP],
        ids=["png", "jpeg", "webp", "bmp"],
    )
    async def test_a_viewable_job_pays_for_no_extra_encode(self, fmt):
        result, outputs = await self._run([fmt])

        assert result.preview_format is None
        assert list(outputs) == [fmt]

    async def test_psd_only_also_gains_a_preview(self):
        result, outputs = await self._run([OutputFormat.PSD], psd=True)

        assert result.preview_format is OutputFormat.PNG
        assert OutputFormat.PSD in outputs

    async def test_every_job_ends_up_with_something_viewable(self):
        """The property that actually matters: the card can never be blank again."""
        for formats, psd in [
            ([OutputFormat.TIFF], False),
            ([OutputFormat.EPS], False),
            ([OutputFormat.PSD], True),
            ([OutputFormat.TIFF, OutputFormat.EPS], False),
        ]:
            _result, outputs = await self._run(formats, psd=psd)
            assert any(f in F.BROWSER_RENDERABLE for f in outputs), formats

    async def test_a_tiff_upload_matching_its_source_still_previews(self):
        """The reported case: upload a .tif, get a .tif, see nothing."""
        result, outputs = await self._run([OutputFormat.TIFF], name="shot.tif", match=True)

        assert OutputFormat.TIFF in outputs
        assert result.preview_format is OutputFormat.PNG

    async def test_the_preview_is_excluded_from_the_download_bundle(self):
        """Shipping an unrequested PNG beside every TIFF would be its own bug."""
        import io
        import zipfile

        from app.core.settings import Settings
        from app.jobs import run_job_async
        from app.core.jobstore import MemoryJobStore
        from app.storage.memory import MemoryStorage
        from app.models import JobStatus, JobState, CutoutSpec, EngineId, EngineStrategy

        settings = Settings(_env_file=None)
        storage, store = MemoryStorage(), MemoryJobStore()

        src = E.encode(scene(120, 120), None, fmt=OutputFormat.PNG)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("shot.png", src)

        config = JobConfig(
            cutout=CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.LOCAL),
            export=ExportSpec(formats=[OutputFormat.TIFF], match_source=False),
        )
        job_id = "j1"
        storage.put(f"jobs/{job_id}/source.zip", archive.getvalue(), "application/zip")
        storage.put(
            f"jobs/{job_id}/config.json", config.model_dump_json().encode(), "application/json"
        )
        from datetime import datetime, timezone

        store.save(JobStatus(
            job_id=job_id, state=JobState.QUEUED, config_hash=config.config_hash(),
            created_at=datetime.now(timezone.utc),
        ))

        status = await run_job_async(job_id, storage, store, settings)
        image = status.images[0]
        assert image.preview_format is OutputFormat.PNG

        bundle = storage.get(f"jobs/{job_id}/outputs.zip")
        names = zipfile.ZipFile(io.BytesIO(bundle)).namelist()
        assert names == ["shot.tiff"], f"preview leaked into the bundle: {names}"


class TestTransparentSourceSubstitution:
    """`match_source` could append a format that cannot hold the transparency being asked for.

    `JobConfig` rejects `transparent` alongside JPEG in `export.formats` — but match_source picks
    its format *after* validation, so a transparent job on a `.jpg` upload appended JPEG, hit the
    encoder's alpha guard, and died as a bare `internal_error`. Found by reproducing a real job,
    not by a unit test, because the two features are individually fine and only collide.
    """

    @staticmethod
    async def _run(name: str, *, transparent: bool):
        from app import pipeline
        from app.core.settings import Settings
        from app.models import BackgroundSpec, CutoutSpec, EngineId, EngineStrategy

        png = E.encode(scene(120, 120), None, fmt=OutputFormat.PNG)
        config = JobConfig(
            cutout=CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.LOCAL),
            background=BackgroundSpec(
                transparent=transparent, color=None if transparent else "#FFFFFF"
            ),
            export=ExportSpec(formats=[OutputFormat.TIFF], match_source=True),
        )
        return await pipeline.process_image(png, name, config, Settings(_env_file=None))

    @pytest.mark.parametrize(
        "name", ["shot.jpg", "shot.jpeg", "shot.bmp", "shot.eps"],
    )
    async def test_an_alpha_free_source_becomes_png_when_transparent(self, name):
        result, outputs = await self._run(name, transparent=True)

        assert result.state.value == "done", result.error
        assert OutputFormat.PNG in outputs
        assert Note.FORMAT_SUBSTITUTED in result.notes
        for fmt in (OutputFormat.JPEG, OutputFormat.BMP, OutputFormat.EPS):
            assert fmt not in outputs, f"{fmt.value} cannot hold the requested transparency"

    @pytest.mark.parametrize("name", ["shot.jpg", "shot.bmp", "shot.eps"])
    async def test_an_opaque_job_still_round_trips_the_source_format(self, name):
        """The substitution must not fire when there is no transparency to protect."""
        result, outputs = await self._run(name, transparent=False)

        expected = F.source_from_name(name)
        assert F.SOURCE_TO_OUTPUT[expected] in outputs
        assert Note.FORMAT_SUBSTITUTED not in result.notes

    async def test_an_alpha_capable_source_is_untouched_by_transparency(self):
        result, outputs = await self._run("shot.tif", transparent=True)

        assert OutputFormat.TIFF in outputs
        assert Note.FORMAT_SUBSTITUTED not in result.notes

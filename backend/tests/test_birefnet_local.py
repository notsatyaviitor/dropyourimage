"""Self-hosted BiRefNet engine — wiring, availability and preprocessing.

**None of these tests may skip.** That is the whole point of the file. `docs/PSD.md` records the
failure this is written against: `test_psd_fallback.py` skipped when `pytoshop` was absent, the
suite reported "393 passed, 2 skipped" and looked healthy, and behind those two skips sat seven
genuinely failing assertions on a feature that was completely dead. So everything here runs with
or without torch installed, and the torch-dependent parts are exercised through fakes rather than
skipped.

What is deliberately NOT covered: mask quality. That needs the real weights and real photographs,
neither of which belongs in this suite.
"""

from __future__ import annotations

import io
import sys

import numpy as np
import pytest
from PIL import Image

from app.core.settings import Settings
from app.engines import birefnet_local as B
from app.engines.registry import build_all, select_pool
from app.models import CutoutSpec, EngineId, EngineStrategy


def settings(**over) -> Settings:
    """Settings with the env/.env layer neutralised, so a developer's own keys cannot change
    what these assertions see."""
    base = dict(
        birefnet_enabled=True,
        birefnet_allow_download=False,
        photoroom_api_key="",
        removebg_api_key="",
        fal_key="",
        gemini_api_key="",
        s3_endpoint_url="memory",
    )
    base.update(over)
    return Settings(**base)


def png_bytes(w: int = 64, h: int = 40, colour=(200, 40, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "PNG")
    return buf.getvalue()


class TestContractWiring:
    """The engine must be reachable through every layer that names an engine."""

    def test_engine_id_exists_and_is_stable(self):
        assert EngineId.BIREFNET.value == "birefnet"

    def test_registry_constructs_it_without_importing_torch(self):
        # Constructing must not pull torch in — that is what keeps a torch-less box working.
        before = "torch" in sys.modules
        engines = build_all(settings())
        assert EngineId.BIREFNET in engines
        assert engines[EngineId.BIREFNET].id is EngineId.BIREFNET
        if not before:
            assert "torch" not in sys.modules, "build_all imported torch; keep the import lazy"

    def test_costs_nothing_per_image(self):
        # Self-hosted. A non-zero value here would corrupt the MAX_JOB_COST_USD ceiling, which
        # exists to bound *vendor* spend.
        assert build_all(settings())[EngineId.BIREFNET].cost_per_image_usd == 0.0

    def test_needs_no_key(self):
        assert settings().key_for(EngineId.BIREFNET) == "n/a"

    def test_engine_pool_accepts_it(self):
        # The ENGINE_POOL validator rejects unknown names, so a new engine that is not in EngineId
        # would make this a hard startup failure rather than a quiet no-op.
        assert settings(engine_pool="photoroom,birefnet").engine_priority == [
            EngineId.PHOTOROOM,
            EngineId.BIREFNET,
        ]

    def test_single_strategy_selects_it_regardless_of_pool(self, monkeypatch):
        # SINGLE bypasses ENGINE_POOL by design — an operator naming an engine gets that engine.
        monkeypatch.setattr(B.BiRefNetLocalEngine, "available", lambda self: True)
        pool = select_pool(
            settings(engine_pool="photoroom,gemini"),
            CutoutSpec(strategy=EngineStrategy.SINGLE, engine=EngineId.BIREFNET),
        )
        assert [e.id for e in pool] == [EngineId.BIREFNET]


class TestAvailability:
    """`available()` must answer "can this run right now, offline" — never "could it, given a
    download". `pytest` runs on a clean checkout with no network."""

    def test_disabled_by_default(self):
        """The *declared* default, read off the field rather than an instance.

        `Settings()` merges `backend/.env`, so asserting through an instance passes or fails
        depending on whether the developer running the suite happens to have BiRefNet enabled
        locally. This has to pin the shipped default: a fresh deployment must not start running a
        self-hosted model because someone added a line to their own env file.
        """
        assert Settings.model_fields["birefnet_enabled"].default is False
        assert Settings.model_fields["birefnet_allow_download"].default is False

    def test_unavailable_when_disabled_even_with_everything_installed(self, monkeypatch):
        monkeypatch.setattr(B, "_torch", lambda: object())
        monkeypatch.setattr(B, "_transformers", lambda: object())
        monkeypatch.setattr(B.BiRefNetLocalEngine, "_weights_cached", lambda self: True)
        assert B.BiRefNetLocalEngine(settings(birefnet_enabled=False)).available() is False

    def test_unavailable_without_torch(self, monkeypatch):
        monkeypatch.setattr(B, "_torch", lambda: None)
        monkeypatch.setattr(B, "_transformers", lambda: object())
        assert B.BiRefNetLocalEngine(settings()).available() is False

    def test_unavailable_without_transformers(self, monkeypatch):
        # torch alone is not enough — the Hub loader lives in transformers.
        monkeypatch.setattr(B, "_torch", lambda: object())
        monkeypatch.setattr(B, "_transformers", lambda: None)
        assert B.BiRefNetLocalEngine(settings()).available() is False

    def test_unavailable_when_weights_are_not_cached(self, monkeypatch):
        monkeypatch.setattr(B, "_torch", lambda: object())
        monkeypatch.setattr(B, "_transformers", lambda: object())
        monkeypatch.setattr(B.BiRefNetLocalEngine, "_weights_cached", lambda self: False)
        assert B.BiRefNetLocalEngine(settings()).available() is False

    def test_available_when_enabled_torch_present_and_weights_cached(self, monkeypatch):
        monkeypatch.setattr(B, "_torch", lambda: object())
        monkeypatch.setattr(B, "_transformers", lambda: object())
        monkeypatch.setattr(B.BiRefNetLocalEngine, "_weights_cached", lambda self: True)
        assert B.BiRefNetLocalEngine(settings()).available() is True

    def test_broken_torch_install_reads_as_unavailable_not_a_crash(self, monkeypatch):
        """A half-installed torch raises OSError on import, not ImportError.

        `except ImportError` would let that escape and become an `internal_error` — the same
        class of bug docs/PSD.md records for pytoshop. This drives the real import through a
        failing loader rather than stubbing `_torch`, so it tests the actual except clause.
        """
        import builtins

        real_import = builtins.__import__

        def failing_import(name, *args, **kwargs):
            if name == "torch" or name.startswith("torch."):
                raise OSError("libcudart.so.12: cannot open shared object file")
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(sys.modules, "torch", raising=False)
        monkeypatch.setattr(builtins, "__import__", failing_import)

        assert B._torch() is None
        assert B.resolve_device("auto") == "cpu"
        assert B.BiRefNetLocalEngine(settings()).available() is False


class TestDeviceResolution:
    def test_explicit_preference_is_taken_literally(self):
        assert B.resolve_device("cpu") == "cpu"
        assert B.resolve_device("cuda") == "cuda"

    def test_auto_falls_back_to_cpu_without_torch(self, monkeypatch):
        monkeypatch.setattr(B, "_torch", lambda: None)
        assert B.resolve_device("auto") == "cpu"

    def test_auto_picks_cuda_when_torch_reports_one(self, monkeypatch):
        class FakeCuda:
            @staticmethod
            def is_available() -> bool:
                return True

        monkeypatch.setattr(B, "_torch", lambda: type("T", (), {"cuda": FakeCuda})())
        assert B.resolve_device("auto") == "cuda"

    def test_auto_picks_cpu_when_torch_reports_no_gpu(self, monkeypatch):
        class FakeCuda:
            @staticmethod
            def is_available() -> bool:
                return False

        monkeypatch.setattr(B, "_torch", lambda: type("T", (), {"cuda": FakeCuda})())
        assert B.resolve_device("auto") == "cpu"


class FakeTorch:
    """Just enough torch for `_preprocess`, so normalisation is tested without a 2 GB install."""

    class _T:
        def __init__(self, arr):
            self.arr = arr

        def unsqueeze(self, axis):
            return FakeTorch._T(np.expand_dims(self.arr, axis))

    @staticmethod
    def from_numpy(arr):
        return FakeTorch._T(arr)


class TestPreprocess:
    def test_produces_nchw_at_the_configured_input_size(self):
        eng = B.BiRefNetLocalEngine(settings(birefnet_input_size=256))
        out = eng._preprocess(png_bytes(), FakeTorch)
        assert out.arr.shape == (1, 3, 256, 256)

    def test_applies_imagenet_normalisation_not_raw_0_1(self):
        # Feeding un-normalised values degrades the mask quietly rather than failing, which is the
        # worst way for this to be wrong — so pin it. Mid-grey must map to roughly zero.
        eng = B.BiRefNetLocalEngine(settings(birefnet_input_size=32))
        grey = eng._preprocess(png_bytes(colour=(124, 116, 104)), FakeTorch).arr
        assert np.allclose(grey, 0.0, atol=0.05), "input does not look ImageNet-normalised"

    def test_uses_display_referred_srgb_not_linear_light(self):
        # The imaging stages work in linear light; the network was trained on 8-bit sRGB. Decoding
        # to linear here would darken every input relative to training. Mid-grey 128 in sRGB is
        # ~0.216 in linear, which would normalise to about -1.1 rather than ~0.
        eng = B.BiRefNetLocalEngine(settings(birefnet_input_size=32))
        mid = eng._preprocess(png_bytes(colour=(128, 128, 128)), FakeTorch).arr
        assert mid.mean() > -0.5, "looks like linear light reached the network"

    def test_a_non_image_raises_a_typed_decode_error(self):
        from app.core import errors

        eng = B.BiRefNetLocalEngine(settings())
        with pytest.raises(errors.ImageDecodeFailed):
            eng._preprocess(b"this is not a png", FakeTorch)


class TestInferenceGuards:
    @pytest.mark.asyncio
    async def test_without_torch_it_raises_a_typed_error_not_module_not_found(self, monkeypatch):
        # The exact failure docs/PSD.md records: a missing wheel surfaced as a bare
        # ModuleNotFoundError reported to the user as `internal_error`.
        from app.core import errors

        monkeypatch.setattr(B, "_torch", lambda: None)
        eng = B.BiRefNetLocalEngine(settings())
        with pytest.raises(errors.PipelineError):
            await eng.alpha_for(png_bytes(), 64, 40)

    @pytest.mark.asyncio
    async def test_mask_is_resized_to_the_source_dimensions(self, monkeypatch):
        # Whatever square the network ran at, the pipeline downstream assumes the alpha matches
        # the source exactly — misalignment here looks identical to a bad cut-out.
        monkeypatch.setattr(
            B.BiRefNetLocalEngine, "_infer", lambda self, b: np.full((256, 256), 0.5, np.float32)
        )
        res = await B.BiRefNetLocalEngine(settings()).alpha_for(png_bytes(), 64, 40)
        assert res.alpha.shape == (40, 64)
        assert res.alpha.dtype == np.float32
        assert res.engine is EngineId.BIREFNET
        assert res.cost_usd == 0.0

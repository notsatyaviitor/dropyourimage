"""Self-hosted BiRefNet — the one engine here that is not a metered API call.

**Read this before enabling it.** The root `CLAUDE.md` states the constraint plainly: *"No GPU,
no self-hosted models. Architectural constraint from the client's report. Everything AI is a
metered API call."* This module is a deliberate, requested exception to that, so the cost of the
exception is written down here rather than discovered in production:

* **Dependencies.** torch, torchvision, transformers and timm are roughly 2-3 GB installed.
  `docs/DEPLOY.md`'s package list does not install them, and `scripts/setup.sh` will not pull
  them unless `requirements-birefnet.txt` is installed explicitly.
* **Latency without a GPU.** BiRefNet at 1024x1024 is seconds per image on CPU against
  Photoroom's measured 0.79-0.86 s over the network. On a 400-image order that is the difference
  between minutes and hours, and the production box has no GPU.
* **It is the same model fal.ai serves** (`fal.run/fal-ai/birefnet/v2`). Choosing this over that
  is a decision about cost, data residency and vendor independence — not about mask quality.

What it is good at: it costs nothing per image, has no rate limit, needs no key, and the
photograph never leaves the server. That last one is the argument that survives scrutiny.

Design notes
------------
**Nothing torch-shaped is imported at module scope.** `app/psd/fallback.py` imported `pytoshop`
at module level and a missing wheel surfaced to the user as a bare `internal_error` — see
`docs/PSD.md`. The import here is inside `_load_model`, so a machine without torch gets
`available() == False` and every other engine keeps working.

**The model is loaded once per process, behind a lock.** It is ~450 MB of weights; loading it per
request would be pathological, and two concurrent requests racing the first load would hold two
copies.

**Inference runs in a worker thread.** `alpha_for` is async and the caller's event loop is
running other images' HTTP calls; a multi-second blocking forward pass on the loop thread would
stall the whole batch.
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from typing import Any

import numpy as np

from app.core import errors
from app.core.settings import Settings
from app.engines.base import AlphaResult, fit_alpha_to_source
from app.models import EngineId

#: ImageNet statistics. BiRefNet is trained with this normalisation; changing it degrades the
#: mask quietly rather than failing, which is the worst way for it to be wrong.
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)

#: One loaded model per (model_id, revision, device). Module-level so it survives across requests
#: within a worker process; the lock guards the first load, not inference.
_MODELS: dict[tuple[str, str, str], Any] = {}
_LOAD_LOCK = threading.Lock()


def _torch() -> Any | None:
    """Import torch, or return None. Never raises — `available()` depends on that."""
    try:
        import torch
    except Exception:  # noqa: BLE001 - a broken install must read as "unavailable", not crash
        return None
    return torch


def _transformers() -> Any | None:
    """Import transformers, or return None. Same contract as `_torch`.

    A function rather than an inline `import` inside `available()` so it is one seam a test can
    replace — an unmockable import is how a wiring test ends up silently asserting "this developer
    happens not to have the package installed".
    """
    try:
        import transformers
    except Exception:  # noqa: BLE001
        return None
    return transformers


def resolve_device(preference: str) -> str:
    """`"auto"` -> cuda when torch reports one, else cpu. Anything else is taken literally."""
    if preference != "auto":
        return preference
    torch = _torch()
    if torch is not None and torch.cuda.is_available():
        return "cuda"
    return "cpu"


class BiRefNetLocalEngine:
    """BiRefNet run in-process. See the module docstring for what enabling it costs."""

    id = EngineId.BIREFNET
    #: Self-hosted, so nothing is billed per image. Electricity and wall-clock are the real price
    #: and neither belongs in `JobStatus.cost_usd`, which exists to bound *vendor* spend.
    cost_per_image_usd = 0.0

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # -- availability ---------------------------------------------------------

    def available(self) -> bool:
        """True only if this can actually run *now*, offline.

        Deliberately strict on two points. It returns False unless `BIREFNET_ENABLED` is set, so
        the engine cannot be picked up by accident on a box that happens to have torch. And it
        does not consider "the weights could be downloaded" to be available, because `pytest`
        must pass on a clean checkout with no network — see the root `CLAUDE.md`.
        """
        if not self._settings.birefnet_enabled:
            return False
        if _torch() is None or _transformers() is None:
            return False
        return self._settings.birefnet_allow_download or self._weights_cached()

    def _weights_cached(self) -> bool:
        """Whether the weights are already on disk, without touching the network."""
        key = (
            self._settings.birefnet_model_id,
            self._settings.birefnet_revision,
            resolve_device(self._settings.birefnet_device),
        )
        if key in _MODELS:
            return True
        try:
            from huggingface_hub import snapshot_download

            snapshot_download(
                self._settings.birefnet_model_id,
                revision=self._settings.birefnet_revision,
                local_files_only=True,
            )
        except Exception:  # noqa: BLE001 - "not cached" and "no hub library" are both False
            return False
        return True

    # -- inference ------------------------------------------------------------

    async def alpha_for(self, image_bytes: bytes, width: int, height: int) -> AlphaResult:
        started = time.perf_counter()
        # to_thread, not a bare call: the forward pass is seconds of blocking compute on CPU and
        # this coroutine shares its loop with every other image's vendor call.
        alpha = await asyncio.to_thread(self._infer, image_bytes)
        return AlphaResult(
            alpha=fit_alpha_to_source(alpha, width, height),
            engine=self.id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            cost_usd=0.0,
        )

    def _infer(self, image_bytes: bytes) -> np.ndarray:
        torch = _torch()
        if torch is None:
            raise errors.VendorError("torch is not installed; BiRefNet cannot run")

        model, device = self._load_model()
        tensor = self._preprocess(image_bytes, torch)
        # `_load_model` puts the model in half precision on CUDA, so the input has to match or
        # torch raises a dtype mismatch on the first conv.
        tensor = tensor.to(device).half() if device == "cuda" else tensor.to(device)

        try:
            with torch.inference_mode():
                out = model(tensor)
            # BiRefNet returns a list of supervision maps at several scales; the last is the
            # final prediction. Taking [0] here would silently return a coarse one.
            logits = out[-1] if isinstance(out, (list, tuple)) else out
            if isinstance(logits, (list, tuple)):
                logits = logits[-1]
            mask = logits.sigmoid().squeeze().float().cpu().numpy()
        except Exception as exc:  # noqa: BLE001
            raise errors.VendorError(f"BiRefNet inference failed: {exc}") from exc

        if mask.ndim != 2:
            raise errors.VendorError(f"BiRefNet returned an unexpected shape {mask.shape}")
        return np.clip(mask.astype(np.float32), 0.0, 1.0)

    def _preprocess(self, image_bytes: bytes, torch: Any) -> Any:
        """Decode -> square resize -> ImageNet normalise -> NCHW float tensor.

        Uses Pillow rather than the pipeline's own `imaging.export.decode`, on purpose: what the
        network needs is display-referred 8-bit sRGB at its training normalisation, not the
        linear-light float the imaging stages work in. Feeding linear light here would darken
        every input relative to what the model was trained on.
        """
        from PIL import Image

        size = int(self._settings.birefnet_input_size)
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                rgb = img.convert("RGB").resize((size, size), Image.BILINEAR)
                arr = np.asarray(rgb, dtype=np.float32) / 255.0
        except Exception as exc:  # noqa: BLE001
            raise errors.ImageDecodeFailed(f"BiRefNet could not decode the image: {exc}") from exc

        arr = (arr - np.asarray(_MEAN, dtype=np.float32)) / np.asarray(_STD, dtype=np.float32)
        return torch.from_numpy(arr.transpose(2, 0, 1)).unsqueeze(0)

    def _load_model(self) -> tuple[Any, str]:
        """Load once per (model, revision, device) and keep it. Guarded against a load race."""
        torch = _torch()
        assert torch is not None  # _infer checked
        device = resolve_device(self._settings.birefnet_device)
        key = (self._settings.birefnet_model_id, self._settings.birefnet_revision, device)

        cached = _MODELS.get(key)
        if cached is not None:
            return cached, device

        with _LOAD_LOCK:
            cached = _MODELS.get(key)  # another thread may have won the race
            if cached is not None:
                return cached, device
            try:
                from transformers import AutoModelForImageSegmentation

                model = AutoModelForImageSegmentation.from_pretrained(
                    self._settings.birefnet_model_id,
                    revision=self._settings.birefnet_revision,
                    # Required by BiRefNet: its architecture lives in the repo, not in
                    # transformers. Pinned by revision above so what executes is auditable.
                    trust_remote_code=True,
                    local_files_only=not self._settings.birefnet_allow_download,
                )
            except Exception as exc:  # noqa: BLE001
                raise errors.VendorError(
                    f"BiRefNet weights could not be loaded ({exc}). Warm the cache first, or "
                    "set BIREFNET_ALLOW_DOWNLOAD=true."
                ) from exc

            model.eval().to(device)
            if device == "cuda":
                # Halves weight memory and is materially faster on tensor cores. Not applied on
                # CPU, where float16 is emulated and slower than float32.
                model.half()
            _MODELS[key] = model
            return model, device

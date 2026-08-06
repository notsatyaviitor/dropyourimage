"""Engine registry. Adding an engine means one entry here and one new module.

Degradation is deliberate and loud: with no vendor keys configured the pool falls back to the
offline control engine so the whole pipeline still runs end to end. That is what keeps
procurement off the critical path — see docs/SETUP.md.
"""

from __future__ import annotations

from app.core.settings import Settings
from app.engines.base import BackgroundRemover
from app.engines.gemini_edit import GeminiEditEngine
from app.engines.http import FalAiEngine, PhotoroomEngine, RemoveBgEngine
from app.engines.huggingface import HuggingFaceEngine
from app.engines.local import LocalEngine
from app.models import CutoutSpec, EngineId, EngineStrategy

_CONSTRUCTORS = {
    EngineId.PHOTOROOM: PhotoroomEngine,
    EngineId.REMOVEBG: RemoveBgEngine,
    EngineId.FALAI: FalAiEngine,
    # A normal commercial engine like the three above — gated only by key presence, same as them.
    # See docs/ENGINES.md; unlike GEMINI_EDIT below, this is not a prime-directive exception.
    EngineId.HUGGINGFACE: HuggingFaceEngine,
    # Built and registered, but ignored by AUTO unless "gemini_edit" is deliberately added to
    # ENGINE_POOL — its own `available()` gate (GEMINI_EDIT_ENABLED + a key) is a second,
    # independent lock on top of that. See docs/ENGINES.md — this is an explicit prime-directive
    # override, not a default engine.
    EngineId.GEMINI_EDIT: GeminiEditEngine,
}

# How many engines the AUTO strategy runs concurrently. Two is where the yield gain is; a third
# adds cost for diminishing returns (it is listed as a buy-back option in the delivery plan).
AUTO_POOL_SIZE = 2


def build_all(settings: Settings) -> dict[EngineId, BackgroundRemover]:
    """Every engine that could be used, keyed by id. Includes unavailable ones."""
    engines: dict[EngineId, BackgroundRemover] = {EngineId.LOCAL: LocalEngine()}
    for engine_id, ctor in _CONSTRUCTORS.items():
        engines[engine_id] = ctor(settings)
    return engines


def select_pool(settings: Settings, spec: CutoutSpec) -> list[BackgroundRemover]:
    """Resolve the configured strategy into the concrete engines to run for one image.

    For AUTO, the first two *available* engines in `ENGINE_POOL` order form the pair. Availability
    is "has a key", so an unconfigured engine is skipped silently rather than failing the job —
    but if that leaves only one, `autopick` emits `SINGLE_ENGINE_ONLY` so the result says so.
    """
    all_engines = build_all(settings)

    if spec.strategy is EngineStrategy.SINGLE:
        assert spec.engine is not None, "contract guarantees engine is set for SINGLE"
        chosen = all_engines[spec.engine]
        if not chosen.available():
            # Explicitly asked for an engine we cannot reach. Falling back silently would make a
            # demo look like it honoured a request it did not, so let the vendor error surface.
            return [chosen]
        return [chosen]

    available = [
        all_engines[e] for e in settings.engine_priority if all_engines[e].available()
    ]
    if not available:
        return [all_engines[EngineId.LOCAL]]

    return available[:AUTO_POOL_SIZE]

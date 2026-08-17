"""Dual-engine auto-pick: run two segmenters, keep the better mask.

Different engines fail differently, so running two and choosing raises first-pass yield above
either alone. At POC volume the extra ~$0.22 per image is irrelevant, which is why the client
report's own recommendation for Features 3 and 4 is the same two-engine routing shape.

Selection order — cheapest and most certain first:

1. **Reject** on measurable faults. No judgement involved.
2. **Score** deterministically on mask geometry.
3. **Tie-break** with a vision model, and *only* when 1 and 2 cannot separate the candidates.

Step 3 is the one legitimate use of AI here: it is a genuine judgement call about which cut-out
looks right, which is exactly the role the client report assigns Gemini 3 Flash in Feature 1.
Everything above it is measurement.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

import cv2
import numpy as np

from app.engines.base import AlphaResult
from app.models import EngineCandidate, EngineId, Note

# --- reject thresholds ------------------------------------------------------------------
_MIN_COVERAGE = 0.001      # an empty mask found nothing
_MAX_COVERAGE = 0.995      # a full mask removed nothing
_MIN_SOLIDITY = 0.60       # largest blob's share of total mask area; below this it is confetti

# Alpha above this counts as "part of an object" when labelling connected components. Deliberately
# far below 0.5: the soft transition band belongs to the object it surrounds, and labelling on the
# binary core instead would cut every kept object's edge back to the 0.5 contour — invariant 2.
_SUPPORT_THRESHOLD = 0.05


class _Reject(enum.Enum):
    """Why a mask was rejected, as a value rather than prose.

    `rejected_reason` stays human-readable for the UI; this is what code branches on. Only
    `FRAGMENTED` is recoverable — the others describe a mask with nothing worth salvaging.
    """

    EMPTY = "empty"
    FULL_FRAME = "full_frame"
    INVERTED = "inverted"
    FRAGMENTED = "fragmented"

# --- scoring ----------------------------------------------------------------------------
# Target average edge width, **in pixels at `_REFERENCE_LONG_EDGE`**. A hard binary mask sits near
# 0 and looks cut out with scissors; a mushy mask looks out of focus. Real matting lands at 1-2px
# at this reference size.
_TARGET_EDGE_WIDTH = 1.5
_EDGE_WIDTH_TOLERANCE = 1.8

#: The resolution the target above is calibrated at, and what makes it fair across engines.
#:
#: Measured 17 Aug 2026, BiRefNet on the same subject at four sizes — it segments at its native
#: 1024 and the mask is then resized to the source, so the band widens in lockstep with the
#: upscale factor:
#:
#:     source        edge width   width / upscale
#:     900x1200        1.95 px         1.6
#:     2000x2600       4.17 px         1.67
#:     3500x5200       7.84 px         1.54
#:     5504x8256      11.97 px         1.48
#:
#: The intrinsic edge is ~1.5px every time. Against a fixed 1.5px target the last two scored
#: **0.0000**, so on a 45 MP client PSD a real matte lost to a rasterised polygon with no soft
#: alpha at all. An absolute target measures the engine's output resolution, not its edge quality.
_REFERENCE_LONG_EDGE = 1024.0

#: Below this share of soft pixels the mask has no transition band worth the name — it is a
#: stencil. Scored at zero rather than merely low, because invariant 2 in backend/CLAUDE.md is
#: that soft alpha is never recoverable once thrown away: there is nothing for edge
#: decontamination to correct and the halo is permanent.
_BINARY_MASK_SOFT_RATIO = 1e-5

_W_EDGE = 0.55
_W_SOLIDITY = 0.30
_W_COVERAGE = 0.15

# Scores closer than this are treated as indistinguishable, and go to the tie-break.
_TIE_EPSILON = 0.04

# Below this the mask is kept but flagged for a human look.
_SUSPECT_SCORE = 0.45


@dataclass
class Verdict:
    winner: AlphaResult | None
    candidates: list[EngineCandidate]
    notes: list[Note] = field(default_factory=list)


@dataclass
class _Measured:
    result: AlphaResult
    score: float
    soft_alpha_ratio: float
    rejected_reason: str | None
    reject: _Reject | None = None

    def to_candidate(self) -> EngineCandidate:
        return EngineCandidate(
            engine=self.result.engine,
            score=round(self.score, 4) if self.rejected_reason is None else None,
            soft_alpha_ratio=round(self.soft_alpha_ratio, 5),
            rejected_reason=self.rejected_reason,
            latency_ms=self.result.latency_ms,
            cost_usd=self.result.cost_usd,
        )


def measure(result: AlphaResult) -> _Measured:
    """Reject rules then geometric score. No model, no network."""
    a = np.asarray(result.alpha, dtype=np.float32)
    total_px = a.size

    coverage = float((a > 0.5).sum()) / total_px
    soft_ratio = float(((a > 0.05) & (a < 0.95)).sum()) / total_px

    if coverage < _MIN_COVERAGE:
        return _Measured(result, 0.0, soft_ratio, "empty mask - nothing detected", _Reject.EMPTY)
    if coverage > _MAX_COVERAGE:
        return _Measured(
            result, 0.0, soft_ratio,
            "mask covers the whole frame - nothing removed", _Reject.FULL_FRAME,
        )

    core = (a > 0.5).astype(np.uint8)
    if _touches_all_four_edges(core):
        return _Measured(
            result, 0.0, soft_ratio,
            "mask reaches all four edges - probably inverted", _Reject.INVERTED,
        )

    solidity = _largest_component_share(core)
    if solidity < _MIN_SOLIDITY:
        return _Measured(
            result, 0.0, soft_ratio,
            f"mask is fragmented (largest blob {solidity:.0%})", _Reject.FRAGMENTED,
        )

    score = (
        _W_EDGE * _edge_quality(a, core)
        + _W_SOLIDITY * solidity
        + _W_COVERAGE * _coverage_plausibility(coverage)
    )
    return _Measured(result, float(np.clip(score, 0.0, 1.0)), soft_ratio, None)


def keep_largest_object(alpha: np.ndarray) -> np.ndarray | None:
    """Zero every separate object in `alpha` except the biggest, or None if there is only one.

    The rescue for a scene that segmented into several correct objects when the order wanted one.
    Deterministic — connected components, no model, no vendor call, no cost.

    Two details that matter:

    * **Components are labelled on the alpha *support* (> 0.05), not on the binary core (> 0.5).**
      An object's soft transition band lies outside its own core, so labelling on the core would
      slice every kept edge back to the 0.5 contour and hard-edge it — the permanent damage
      `backend/CLAUDE.md` invariant 2 exists to prevent. Labelling on the support keeps the whole
      band attached to the object it belongs to.
    * **Components are ranked by core area, not support area.** A large diffuse halo can outweigh a
      small solid product on support alone, which would pick the haze over the object.
    """
    a = np.asarray(alpha, dtype=np.float32)
    support = (a > _SUPPORT_THRESHOLD).astype(np.uint8)
    count, labels = cv2.connectedComponents(support, connectivity=8)
    if count <= 2:  # label 0 is background, so this is one object or none: nothing to isolate
        return None

    core = a > 0.5
    areas = [(int((core & (labels == lbl)).sum()), lbl) for lbl in range(1, count)]
    best_area, best_label = max(areas)
    if best_area <= 0:
        return None

    return np.where(labels == best_label, a, 0.0).astype(np.float32)


def _recover_fragmented(measured: list[_Measured]) -> list[_Measured]:
    """Re-measure fragmented candidates with only their largest object kept.

    Only `FRAGMENTED` is attempted. The other rejections describe a mask with nothing to salvage —
    isolating the largest blob of an inverted or empty mask just produces a confident wrong answer,
    which is worse than failing.

    A candidate that still fails after isolation is dropped, so this can only turn a failure into a
    delivery, never a good mask into a worse one.
    """
    recovered: list[_Measured] = []
    for m in measured:
        if m.reject is not _Reject.FRAGMENTED:
            continue
        isolated = keep_largest_object(m.result.alpha)
        if isolated is None:
            continue
        again = measure(
            AlphaResult(
                alpha=isolated,
                engine=m.result.engine,
                latency_ms=m.result.latency_ms,
                cost_usd=m.result.cost_usd,
                cache_hit=m.result.cache_hit,
            )
        )
        if again.rejected_reason is None:
            recovered.append(again)
    return recovered


def _touches_all_four_edges(core: np.ndarray) -> bool:
    return bool(
        core[0].any() and core[-1].any() and core[:, 0].any() and core[:, -1].any()
    )


def _largest_component_share(core: np.ndarray) -> float:
    total = float(core.sum())
    if total <= 0:
        return 0.0
    count, _, stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
    if count <= 1:
        return 0.0
    return float(stats[1:, cv2.CC_STAT_AREA].max()) / total


def _edge_quality(alpha: np.ndarray, core: np.ndarray) -> float:
    """Score average edge width against the target, at this image's resolution.

    Width is soft-alpha pixel count divided by perimeter length, giving the mean width of the
    transition band in pixels — independent of how big the product is in frame, unlike a raw
    soft-pixel fraction.

    Two things this must get right, both of which it once got wrong:

    * **A mask with no transition band scores zero, not half.** A rasterised polygon has
      `soft_px == 0`, which against a 1.5px target still scored ~0.50 through the Gaussian — half
      marks for having thrown the edge away. Measured on a real client PSD: Gemini 0.7164 with
      `soft_alpha_ratio` exactly 0.0, beating BiRefNet's 0.4647 real matte.
    * **The target scales with the image.** An engine that segments at 1024 and has its mask
      resized up to a 45 MP source has a band 8x wider in absolute pixels for the same quality of
      edge. Comparing that against a fixed pixel target ranks engines by output resolution.
    """
    contours, _ = cv2.findContours(core, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    perimeter = sum(len(c) for c in contours)
    if perimeter < 8:
        return 0.0

    soft_px = float(((alpha > 0.05) & (alpha < 0.95)).sum())
    if soft_px / max(alpha.size, 1) < _BINARY_MASK_SOFT_RATIO:
        return 0.0

    # Never below 1.0. Scaling *down* for a small image asks for a sub-pixel transition band —
    # on a 200px fixture the target became 0.29px, so a genuinely soft edge scored 0.05 and a
    # binary one nearly beat it. An edge cannot be finer than a pixel; the reference is the
    # native resolution engines work at, not a proportion to be applied in both directions.
    scale = max(1.0, max(alpha.shape[0], alpha.shape[1]) / _REFERENCE_LONG_EDGE)
    target = _TARGET_EDGE_WIDTH * scale
    tolerance = _EDGE_WIDTH_TOLERANCE * scale

    width = soft_px / perimeter
    return float(np.exp(-(((width - target) / tolerance) ** 2)))


def _coverage_plausibility(coverage: float) -> float:
    """Product photography sits roughly between 5% and 70% of frame.

    Outside that a mask is not necessarily wrong — a small accessory or a full-bleed sofa are both
    real — so this is a mild nudge with low weight, never a rejection.
    """
    if 0.05 <= coverage <= 0.70:
        return 1.0
    if coverage < 0.05:
        return float(coverage / 0.05)
    return float(max(0.0, 1.0 - (coverage - 0.70) / 0.30))


def choose(results: list[AlphaResult]) -> Verdict:
    """Pick the better mask from the candidates, deterministically.

    Returns a `Verdict` whose `winner` is None only when every candidate was rejected — the
    caller turns that into a failed image with the reasons attached, so the UI can say *why*
    rather than just "failed".
    """
    if not results:
        return Verdict(winner=None, candidates=[], notes=[])

    measured = [measure(r) for r in results]
    notes: list[Note] = []

    if len(measured) == 1:
        notes.append(Note.SINGLE_ENGINE_ONLY)

    usable = [m for m in measured if m.rejected_reason is None]
    candidates = [m.to_candidate() for m in measured]

    if not usable:
        # Last resort before failing the image: a mask rejected only for holding several objects
        # still contains a usable one. Keep the largest and say loudly that we guessed — a scene
        # photograph delivered as its biggest object beats delivering nothing, but it is a guess,
        # and `cutout.subject_prompt` is the accurate answer whenever the caller knows it.
        recovered = _recover_fragmented(measured)
        if not recovered:
            return Verdict(winner=None, candidates=candidates, notes=notes)
        notes.append(Note.SCENE_LARGEST_OBJECT)
        usable = recovered
        candidates = [m.to_candidate() for m in recovered]

    usable.sort(key=lambda m: m.score, reverse=True)
    best = usable[0]

    if len(usable) > 1 and (best.score - usable[1].score) < _TIE_EPSILON:
        # Genuinely indistinguishable on geometry. A vision tie-break belongs here; without a
        # Gemini key we say which way it was decided rather than pretending it was judged.
        notes.append(Note.TIEBREAK_DETERMINISTIC)

    # Did the preferred engine lose? Worth surfacing: it is the signal that the primary engine is
    # struggling on a category, which is exactly what the bake-off wants to know.
    if best.result.engine is not results[0].engine:
        notes.append(Note.ENGINE_FALLBACK_USED)

    if best.score < _SUSPECT_SCORE:
        notes.append(Note.ALPHA_SUSPECT)

    return Verdict(winner=best.result, candidates=candidates, notes=notes)


def needs_vision_tiebreak(verdict: Verdict) -> bool:
    """Whether a vision call would actually change anything.

    Kept separate so the pipeline can decide whether to spend the ~$0.0012 rather than having
    `choose` reach out to the network — `choose` stays pure and unit-testable.
    """
    return Note.TIEBREAK_DETERMINISTIC in verdict.notes

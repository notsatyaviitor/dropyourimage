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

from dataclasses import dataclass, field

import cv2
import numpy as np

from app.engines.base import AlphaResult
from app.models import EngineCandidate, EngineId, Note

# --- reject thresholds ------------------------------------------------------------------
_MIN_COVERAGE = 0.001      # an empty mask found nothing
_MAX_COVERAGE = 0.995      # a full mask removed nothing
_MIN_SOLIDITY = 0.60       # largest blob's share of total mask area; below this it is confetti

# --- scoring ----------------------------------------------------------------------------
# Target average edge width in pixels. A hard binary mask sits near 0 and looks cut out with
# scissors; a mushy mask above ~4px looks out of focus. Real matting lands around 1-2px.
_TARGET_EDGE_WIDTH = 1.5
_EDGE_WIDTH_TOLERANCE = 1.8

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
        return _Measured(result, 0.0, soft_ratio, "empty mask - nothing detected")
    if coverage > _MAX_COVERAGE:
        return _Measured(result, 0.0, soft_ratio, "mask covers the whole frame - nothing removed")

    core = (a > 0.5).astype(np.uint8)
    if _touches_all_four_edges(core):
        return _Measured(
            result, 0.0, soft_ratio, "mask reaches all four edges - probably inverted"
        )

    solidity = _largest_component_share(core)
    if solidity < _MIN_SOLIDITY:
        return _Measured(
            result, 0.0, soft_ratio, f"mask is fragmented (largest blob {solidity:.0%})"
        )

    score = (
        _W_EDGE * _edge_quality(a, core)
        + _W_SOLIDITY * solidity
        + _W_COVERAGE * _coverage_plausibility(coverage)
    )
    return _Measured(result, float(np.clip(score, 0.0, 1.0)), soft_ratio, None)


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
    """Score average edge width against the target.

    Computed as soft-alpha pixel count divided by perimeter length, which gives the mean width of
    the transition band in pixels — a scale-invariant measure, unlike a raw soft-pixel fraction
    that would reward a large product over a small one.
    """
    contours, _ = cv2.findContours(core, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    perimeter = sum(len(c) for c in contours)
    if perimeter < 8:
        return 0.0

    soft_px = float(((alpha > 0.05) & (alpha < 0.95)).sum())
    width = soft_px / perimeter
    return float(np.exp(-(((width - _TARGET_EDGE_WIDTH) / _EDGE_WIDTH_TOLERANCE) ** 2)))


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
        return Verdict(winner=None, candidates=candidates, notes=notes)

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

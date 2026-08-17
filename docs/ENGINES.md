# Segmentation engines and auto-pick

Stage 1 is the only AI in the default pipeline. Every measurement below is real, not projected.

## The local control engine (`app/engines/local.py`)

Offline, free, no key required — exists so the whole pipeline is runnable and testable before any
vendor credential arrives, and to give the eventual bake-off a free baseline. **Not production
quality**, and must never be the default when a real key is configured.

**Method: solve the two-colour linear mixture per pixel**, not chromaticity distance. For a pixel
that is part product and part backdrop, `O = a*F + b*B` — two unknowns (`a` the coverage we want,
`b` a free scale on the backdrop), three equations (one per channel), solved by least squares.
Letting `b` float rather than fixing it at `1-a` buys two things:

- **Shadows are ignored for free.** A shadowed backdrop is `O = k*B`, which solves to `a=0, b=k` —
  critical, since eating the shadow would destroy the thing the shadow-preservation stage exists
  to recover.
- **Coverage is measured, not just detected.** An earlier chromaticity-distance version saturated
  — 10% real coverage already maxed the score — producing near-full-opacity edge pixels that
  carried backdrop spill decontamination couldn't remove. Measured: **608 over-claimed pixels**
  on a 600px test frame, visible as a pink rim. The mixture solve: **alpha error 0.0058 → 0.0001,
  over-claims 608 → 0**.

Two degenerate cases handled explicitly: a near-black product (where `a*F` carries no information
for any `a` — solved instead from how much backdrop the pixel *lost*, gated to the detector's
footprint since that measure can't distinguish a black product from its own shadow) and a product
colour parallel to the backdrop's (white on white — genuinely unsolvable by colour alone).

**Measured on synthetic scenes:** IoU ~1.0 on both a coloured product and a black product on
white; alpha correctly stays at 0 inside a cast shadow.

## Commercial adapters (`app/engines/http.py`)

| Engine | Cost/image | Role |
|---|---|---|
| Photoroom | ~$0.02 | **The default engine** |
| fal.ai (BiRefNet-class) | ~$0.03–0.05 | Strong soft alpha |
| Gemini | ~$0.0065 | Selectable, never auto-picked — see below |
| BiRefNet (self-hosted) | $0 | Same model fal.ai serves, run in-process — see below |
| ~~remove.bg~~ | ~~$0.20~~ | **Retired 7 Aug 2026** |

### Self-hosted BiRefNet (`app/engines/birefnet_local.py`), 14 Aug 2026

Added at the maintainer's request. **It is the one engine here that is not a metered API call**,
so enabling it retires the root `CLAUDE.md` constraint *"No GPU, no self-hosted models. Everything
AI is a metered API call."* for that engine. That is a deliberate exception, not an oversight.

It is the same model family fal.ai serves at `fal.run/fal-ai/birefnet/v2`. Choosing it over that
is a decision about cost, rate limits, vendor independence and data residency — **not about mask
quality**, which should be comparable.

What it costs:

- **~2–3 GB of dependencies** (torch, torchvision, transformers, timm), kept out of
  `requirements.txt` in `requirements-birefnet.txt` so CI and the base install are unaffected.
- **Latency without a GPU.** Seconds per image against Photoroom's measured 0.79–0.86 s. The
  production box is 4-core with no GPU; a 400-image order changes from minutes to hours.
- `trust_remote_code=True` is required — BiRefNet's architecture lives in the Hub repo, not in
  `transformers`. Pin `BIREFNET_REVISION` to a commit rather than `main` before production use,
  so what executes is auditable.

Off unless `BIREFNET_ENABLED=true`, and `available()` returns False unless the weights are already
cached locally — `pytest` must pass on a clean checkout with no network, so nothing downloads on
its own. torch is imported lazily inside the engine for the same reason `pytoshop` no longer is
(see `docs/PSD.md`): a missing wheel must read as "unavailable", never as `internal_error`.

### remove.bg retirement, 7 Aug 2026

Dropped on accuracy: its cut-outs did not meet expectation on the client's material. At ~$0.20/image
it was also the most expensive engine by 10×, so nothing argues for keeping it in the pool.

The retirement is a **configuration** change, not a deletion. `RemoveBgEngine` and its 422-line test
suite remain in the tree and still pass; `EngineId.REMOVEBG` remains in the frozen contract so an
older saved `JobConfig` naming it still validates. To bring it back: set `REMOVEBG_API_KEY` and add
`removebg` to `ENGINE_POOL`. Nothing else.

### Verification status, 7 Aug 2026

| Engine | Wire format | Live traffic |
|---|---|---|
| Photoroom | ✅ confirmed against `docs.photoroom.com/llms-full.txt` | ✅ **verified** — 793–861 ms, mask at source resolution, **204 distinct alpha values, 3737 soft edge pixels**, full five-stage pipeline to exact 500×500 and byte-exact `#F5F5F5` |
| Gemini | ✅ confirmed by live probe (the published docs are wrong on vertex order — see below) | ✅ **verified** — 3.6–8.8 s, full pipeline, `hard_edged_mask` note emitted |
| fal.ai | ✅ confirmed — `image_url` accepts a base64 data URI, response is `image.url`, which `_first_image_url` already handles | ❌ no key |
| remove.bg | ✅ confirmed (historic) | ✅ verified 6 Aug 2026, before retirement |

**The Photoroom check found no defects** — unlike the remove.bg one, which turned up two: `size=full`
was a deprecated alias capped at 25 MP (now chosen by pixel count — see `size_for`), and
`type=product` was not being sent at all, so the vendor was guessing at a packshot pipeline.

Two corrections came out of the remove.bg verification, both now applied: `size=full` was a
deprecated alias capped at 25 MP (now chosen by pixel count — see `size_for`), and `type=product`
was not being sent at all, so the vendor was guessing at a packshot pipeline.

Free information currently discarded, if it is ever wanted: Photoroom returns an
`x-uncertainty-score` response header. It is a vendor-side quality signal that could inform
auto-pick — deliberately not wired in, because mixing a vendor's score with the deterministic
geometric score would make `EngineCandidate.score` mean two different things depending on engine.

fal.ai also accepts `mask_only: true`, which would halve the payload. Not used, for the same reason
`channels=alpha` is not used on remove.bg: the response becomes a single-channel matte, and
`alpha_from_rgba_png` would read it as fully opaque. It needs a second decode path to be worth it.

Endpoint URLs, headers and field names live in module-level constants specifically so a correction
stays a one-line change.

## Gemini for background removal — measured, and it does not work

Asked for directly, so it was tested rather than argued about. Two distinct approaches, both
measured against a live key on 6 Aug 2026:

**Image-edit (`gemini-2.5-flash-image`, `gemini-3.1-flash-image`).** The idea was to ask a
generative image model to remove the background and keep only the alpha channel of what it returns.
It cannot work: **these models do not emit transparency.**

| Model | Latency | Returned | Alpha channel |
|---|---|---|---|
| `gemini-2.5-flash-image` | 12.0 s | PNG, mode **RGB**, 1024×1024 | none — `min = max = 1.000`, one distinct value |
| `gemini-3.1-flash-image` | 24.9 s | **JPEG**, 1024×1024 | none — JPEG cannot carry alpha at all |

Both complied with "output a PNG" and ignored "on a transparent background". Since
`alpha_from_rgba_png` converts to RGBA, an RGB input yields alpha = 255 everywhere, so the engine
would hand the pipeline a fully-opaque mask — which auto-pick's reject rules correctly treat as
"the vendor found nothing to remove". **Every image would fail.** Both models also returned
1024×1024 for a 300×300 input, re-rendering at their own resolution and framing rather than
preserving the source.

An implementation exists on the `experiment/gemini-edit-engine` branch and is **deliberately not
merged**. Its own docstring flagged the response shape as unverified; this is what that turned out
to mean.

**Native segmentation** (Gemini returning a real mask, not a regenerated image). This does work, is
architecturally defensible — a mask is what `base.py` wants — and **is now implemented**:
`app/engines/gemini_segment.py`. See the next section.

> ### ⚠️ Retraction, 7 Aug 2026
>
> This document previously stated that native Gemini segmentation took **182–245 s** for a 200×200
> input, was "roughly 100–200× slower" than remove.bg, and was therefore unusable. **That is wrong
> and has been withdrawn.** Re-measured against a live key: **1.7–8.8 s**.
>
> The original figure was almost certainly taken with thinking left at its default. The Gemini docs
> now say plainly, for segmentation, *"disable thinking by setting the thinking level to
> 'minimal'"* — with `thinkingLevel: MINIMAL` the calls return in seconds. No script for the
> original measurement was ever committed, so it could not be reproduced or checked.
>
> Latency was never the real objection. The real one is edge quality, below — which the old
> latency claim had obscured. Do not quote the retracted figure to the client.

## Gemini as a segmentation engine (`app/engines/gemini_segment.py`)

Added 7 Aug 2026 at the maintainer's explicit request, with the measurements below in hand.

**It works, it is fast, and it is cheap. What it costs is the edge.** The API returns the mask as a
**polygon** — a boundary, not a coverage field — so rasterising it can only ever produce a binary
mask. Measured against the studio fixture's known-soft ground truth:

| | Gemini | ground truth | Photoroom (live) |
|---|---|---|---|
| distinct alpha values, 200px | **2** | 27 | 204 |
| soft edge pixels, 200px | **0** | 248 | 3737 |
| distinct alpha values, 800px | **2** | 81 | — |
| soft edge pixels, 800px | **0** | 952 | — |
| polygon vertices, 200px / 800px | 24 / **13** | — | — |
| foreground coverage | 18.8% | — | 12.2% |
| latency | 1.7–8.8 s | — | 0.79–0.86 s |
| cost/image | ~$0.0065 | — | ~$0.02 |

Three consequences worth stating plainly:

1. **Invariant 2 is broken by construction.** `backend/CLAUDE.md` says never threshold alpha to
   binary, because the detail cannot be recovered afterwards. A polygon has no detail to keep.
2. **Edge decontamination has nothing to work on.** `decontaminate_edges` un-mixes the old backdrop
   out of partially-transparent pixels. With none, a white studio backdrop can halo against a
   saturated background colour.
3. **The polygon is coarse and does not improve with resolution** — 24 vertices at 200px, 13 at
   800px. It describes a circle as a 13-gon, and over-covers by ~54% against Photoroom on the same
   frame.

No feathering is applied to disguise this. Synthesising a soft edge would invent coverage that was
never measured — the class of claim `LIMITATIONS.md` refuses to make.

### How the trade is kept visible

* Every result cut out by Gemini carries `Note.HARD_EDGED_MASK`, rendered by the UI as a full
  banner, not a tooltip chip.
* The engine picker states it at the point of choosing, before the job runs.
* **Gemini is deliberately absent from `ENGINE_POOL`**, so `EngineStrategy.AUTO` never selects it.
  This is the important one: its mask still scores **0.7246** and passes every one of auto-pick's
  reject rules, so in an AUTO pair it could out-score Photoroom (0.9659 on the same frame) while
  shipping the visibly worse edge. It is reachable only by explicit single-engine selection.

### Wire format — the docs are wrong, so these were measured

* **Polygon vertices are y-first, `[y, x]`**, matching `box_2d`. The published docs say `[x, y]`.
  Reading them x-first yields a transposed mask that still looks plausible;
  `test_gemini_segment.py` pins this with an asymmetric fixture.
* Vertices live in the same full-image 0–1000 space as `box_2d`, not relative to the box.
* The **same model returned three different vertex shapes across calls** — nested pairs, a wrapped
  flat run, and a bare flat run. All three are handled, and a rigid `responseSchema` was
  deliberately not used because it would fight that variation.
* `thinkingLevel: MINIMAL` is load-bearing (see the retraction above), not a tuning knob.
* **`gemini-3.6-flash` returns COCO compressed RLE, not a polygon.** The engine raises a typed error
  naming `GEMINI_SEGMENT_MODEL` rather than attempting to decode it — a confidently wrong mask is
  worse than a clear failure. `GEMINI_SEGMENT_MODEL` is separate from `GEMINI_MODEL` for this
  reason, and because one setting driving both the locator and the tie-break was already a latent
  hazard.

### When to choose it

Reach for Photoroom by default. Gemini is worth choosing when the subject has a genuinely hard edge
anyway (boxed goods, flat-sided packaging), when cost dominates at volume, or when Photoroom is
unavailable. Avoid it on anything soft-edged, and avoid pairing it with a saturated background
colour.

**Note on benchmarking.** The synthetic studio fixture cannot be used to compare mask *quality*
between engines: it is a clean two-colour composite that the offline colour-keyer solves exactly
(MAE 0.0000, IoU 0.9998 against ground truth). Any engine comparison on it is meaningless. This is
the same gap `LIMITATIONS.md` records — there is no ground-truth mask set for real photographs.

## Auto-pick (`app/engines/autopick.py`)

Deterministic reject rules (empty mask, full-frame mask, edges-touching mask, fragmented mask)
then a geometric score (edge softness + solidity + coverage plausibility) — no model. A vision
tie-break only fires when two candidates score within `_TIE_EPSILON` of each other.

## The Gemini vision tie-break — a real finding, not a plan assumption

The client report specifies **"Gemini 3 Flash"**. Verified live against `listModels`: **that
model id does not exist.** The only Gemini 3 Flash is `gemini-3-flash-preview` — a preview model.

### Measured, 6 Aug 2026 — and it overturns the earlier result

Re-tested against a live key with **4 counterbalanced trials per model**: a good mask versus a
deliberately eroded one, at two severities (25px and 12px), each shown in both presentation orders.
The earlier round used only 2 trials at one severity, which was not enough to separate competence
from position bias.

| Model | Correct | What the trials actually showed |
|---|---|---|
| `gemini-3.5-flash` (stable) | **3/4** | Correct in both orders at 25px; flipped on the subtler 12px case |
| `gemini-3.6-flash` (stable) | 2/4 | Order-independent, but missed the 12px erosion both ways |
| `gemini-3-flash-preview` | 2/4 | **Chose whichever mask was shown first in all 4 trials.** The 2 "correct" answers are an artefact of ordering, not judgement |
| `gemini-2.5-flash` (stable) | — | Answered "no difference" on an obvious 25px erosion |
| The deterministic scorer | 4/4 | 0 ms, $0 |

Two corrections to what this document previously claimed. The preview model was recorded as 2/2;
with more trials that result **does not hold** — it is a first-position bias that a 2-trial test
cannot distinguish from skill. And `GEMINI_MODEL` no longer defaults to the preview: `gemini-3.5-flash`
is both better measured *and* stable, so the preview dependency is gone.

### Counterbalancing is a correctness guard, not a nicety

Because position bias was measurable, `GeminiTiebreak.pick()` judges every pair **twice — once in
each order — and returns a winner only when both runs agree.** Disagreement means the model is
responding to position rather than to the images, and the call degrades to the deterministic score.

Verified live after the change:

| Model | Erosion | Outcome |
|---|---|---|
| `gemini-3.5-flash` | 25px | Both orders agree → correct pick |
| `gemini-3.5-flash` | 12px | Verdict flipped → **falls back**, no guess |
| `gemini-3-flash-preview` | 25px & 12px | Flips every time → **always falls back** |

So the guard converts "confidently wrong half the time" into "correct when consistent, deterministic
otherwise". Cost is two calls per tie-break (~$0.0024), and tie-breaks fire only on near-ties.

### Wired into the pipeline

`pipeline.py::_vision_tiebreak` is called when — and only when — `autopick.needs_vision_tiebreak()
`reports that two candidates scored within `_TIE_EPSILON`. Guarantees:

- **No tie, no call.** Measurement resolves it for free wherever it can.
- **One engine, no call.** There is nothing to compare.
- **Every failure falls back.** No key, bias detected, timeout, HTTP error or unparseable body all
  return the deterministic winner. The `except` in `_vision_tiebreak` is deliberately broad: an
  advisory call must not add a way for an image to fail.
- **The outcome is labelled honestly.** A model-decided pick emits `Note.TIEBREAK_VISION`, distinct
  from `TIEBREAK_DETERMINISTIC`. It is a *judgement*, not a measurement, and it is the only
  non-deterministic step in the pipeline — two runs of the same job can legitimately differ here.

Even the best model measured 3/4 on a deliberately obvious defect, so treat this as a tie-breaker of
last resort, never as a quality score. See LIMITATIONS.md.

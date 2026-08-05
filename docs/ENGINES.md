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
| Photoroom | ~$0.02 | Volume engine |
| remove.bg | ~$0.20 | Premium half of the auto-pick pair |
| fal.ai (BiRefNet-class) | ~$0.03–0.05 | Strong soft alpha |

⚠️ **Endpoint URLs, headers and field names are the documented shapes at time of writing, not
verified against live traffic.** Re-confirm against current vendor docs before the bake-off —
isolated in module-level constants specifically so a correction is a one-line change.

## Auto-pick (`app/engines/autopick.py`)

Deterministic reject rules (empty mask, full-frame mask, edges-touching mask, fragmented mask)
then a geometric score (edge softness + solidity + coverage plausibility) — no model. A vision
tie-break only fires when two candidates score within `_TIE_EPSILON` of each other.

## The Gemini vision tie-break — a real finding, not a plan assumption

The client report specifies **"Gemini 3 Flash"**. Verified live against `listModels`: **that
model id does not exist.** The only Gemini 3 Flash is `gemini-3-flash-preview` — a preview model.

Live-tested both a stable and the preview model on a deliberately eroded (bad) mask vs a good one,
counterbalanced across both orderings to rule out position bias:

| Model | Correct | Notes |
|---|---|---|
| `gemini-2.5-flash` (stable) | 0/2 | Answered "no difference" on an obvious 25px erosion — position-biased before the prompt was fixed to offer an honest "equivalent" answer |
| `gemini-3-flash-preview` | 2/2 | Accurate reasons ("A preserves the circular shape; B has flat, jagged edges"), 7–32s latency |
| The deterministic scorer | 2/2 | 0ms, $0 |

`GEMINI_MODEL` defaults to the preview because it is the only id that actually works — flagged
explicitly as a real dependency risk, not hidden. The tie-break has a short, separate timeout
(`GEMINI_TIMEOUT_SECONDS`) so a slow call degrades to the deterministic answer rather than
stalling a batch, and an honest "equivalent" response is accepted rather than forcing a pick (an
earlier prompt without this option showed 100% position bias on one model).

### Not currently wired into the pipeline

`GeminiTiebreak` (`app/engines/vision.py`) is built, tested, and live-verified against the table
above — but **`pipeline.py` never calls it.** `autopick.choose()` computes
`Note.TIEBREAK_DETERMINISTIC` when two candidates are too close to call and there is a
`needs_vision_tiebreak()` helper that detects exactly that condition, but nothing connects that
signal to an actual `GeminiTiebreak.pick()` call. This is a deliberate decision, not an oversight:
the deterministic scorer already resolved both test cases correctly, for free and instantly (see
the table above), so wiring in a live call to a preview-tier model — 7–32s of latency and a real
network dependency, on top of an API key that would otherwise sit unused — was judged not worth
it against unproven benefit. If a future bake-off finds real cases the deterministic scorer gets
wrong on close ties, this is the place to connect it.

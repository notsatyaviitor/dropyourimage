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

## Hugging Face Inference Providers engine (`app/engines/huggingface.py`) — a normal commercial engine

**Not a prime-directive exception.** Background removal via a commercial segmentation API is
exactly what stage 1 is meant to be — this is the fourth engine of that kind, alongside Photoroom,
remove.bg and fal.ai. It targets `briaai/RMBG-2.0` (the BiRefNet family), gated only by
`HUGGINGFACE_API_KEY` being set, same as the other three.

### Why it exists alongside `FalAiEngine`, not instead of it

Per Hugging Face's own Inference Providers docs (`docs/api-inference/tasks/image-segmentation`),
`briaai/RMBG-2.0` is served **only through the `fal-ai` provider** — the exact same vendor
`FalAiEngine` (`http.py`) already calls directly at `https://fal.run/fal-ai/birefnet/v2`. Adding
this engine does not add a new vendor to the bake-off; it adds a second way to pay for the same
one — useful specifically when a Hugging Face token is preferred over opening a separate fal.ai
account, e.g. because billing is already consolidated there.

### What was verified live before this was written, and how

Unlike the Gemini image-edit engine's model id and field-name warnings, every load-bearing claim
about this integration was checked against a real call before being written down:

- **The client library, not hand-rolled HTTP.** Hugging Face's own docs are explicit that non-chat
  tasks route through provider-specific wire formats that "may vary between providers" and are
  only handled correctly by the official client libraries — the raw HTTP shape for the `fal-ai`
  provider is not published for direct use. `huggingface_hub`'s `AsyncInferenceClient` is used
  instead of `httpx` for exactly this reason.
- **Method signature**, confirmed via `inspect.signature` against the installed
  `huggingface_hub==1.26.0`: `image_segmentation(image, *, model=None, ...)` on the client, with
  `provider` and `token` set at `InferenceClient(provider=..., token=..., timeout=...)`
  construction time, not per-call.
- **Response shape**: a `list[ImageSegmentationOutputElement]`, each with `.label`, `.score`, and
  `.mask` — and `.mask` arrives as an **already-decoded `PIL.Image`** (mode `"L"`), not a base64
  string the way the raw API table describes it. The client handles that decode step.
- **Error classification**: triggered a real live call with a deliberately invalid token and
  inspected the actual exception — `HfHubHTTPError` (raised via the library's own
  `hf_raise_for_status`) carries a real `.response.status_code` (confirmed: `401`), so it classifies
  onto the same taxonomy `http.py` uses. `InferenceTimeoutError` is a separate exception (not a
  subclass of `HfHubHTTPError`) for the model-unavailable/timeout case.
- **The actual routed URL**, from that same live call's error message:
  `https://router.huggingface.co/fal-ai/fal-ai/bria/background/remove?_subdomain=queue` — matching
  the `providerModelId: "fal-ai/bria/background/remove"` mapping the docs describe for this model
  under the `fal-ai` provider.

### What is NOT verified

- **Pricing.** Hugging Face states Inference Providers pass through the underlying provider's rate
  with no markup, but the actual fal.ai rate for `fal-ai/bria/background/remove` was not confirmed
  against a real successful (non-error) call — `_ESTIMATED_COST_USD = 0.02` in `huggingface.py` is
  a placeholder. Check <https://huggingface.co/docs/inference-providers/pricing> before relying on
  it for real spend.
- **Multi-segment responses.** RMBG-2.0 is a background-removal-specific model, so a single-segment
  result is the expected shape and is what `_pick_alpha()` is built around (falling back to the
  highest-confidence segment if more than one comes back) — that fallback path has not itself been
  exercised against a real multi-segment response.
- **A real successful segmentation call end to end** — the live verification above deliberately used
  an invalid token to check error classification without needing a real credential; the happy path
  (an actual mask coming back and improving on the `local` engine's output) still needs checking
  once a real `HUGGINGFACE_API_KEY` is available.

## Gemini image-edit engine (`app/engines/gemini_edit.py`) — an explicit prime-directive override

**This engine breaks the root `CLAUDE.md` rule on purpose, at the maintainer's explicit
instruction, given with full knowledge of the conflict.** The rule: *"Only stage 1 (segmentation)
is an AI API call... Never route [stages 2-5] through a generative or vision model"* — and even
within stage 1, the sanctioned uses were commercial segmentation APIs plus the narrow, unwired
tie-break above. A generative image editor was never on that list. The reasons the directive gives
still apply in full to this engine:

1. It is not byte-reproducible — a second call on the same input is not guaranteed to return the
   same alpha, which breaks the determinism story the rest of this codebase is built to prove.
2. It regenerates the pixels it touches. This integration keeps only the returned image's **alpha
   channel** and discards the colour it invented (same `alpha_from_rgba_png` extraction every
   commercial adapter uses, see `base.py`) — but the mask itself is still the output of a
   generative pass over the image, not a matting/classification model, so its behaviour on a given
   input is not guaranteed stable run to run.
3. Cost and latency are materially higher per image than the commercial adapters — see the
   pricing caveat below.

### Why it exists anyway

Requested directly, as an experiment/comparison path, with the explicit requirement that it be
switchable — never the silent default — so the deterministic dual-engine/auto-pick behaviour this
POC was built around stays exactly as-is unless someone deliberately opts in.

### How it's gated (three independent locks, all default-closed)

1. `GEMINI_EDIT_ENABLED=false` by default in `.env.example`. `GeminiEditEngine.available()`
   returns `False` unless this is `true` **and** `GEMINI_API_KEY` is set.
2. It is never in the default `ENGINE_POOL`, so the `AUTO` strategy's `select_pool()` will not
   pick it up even if the flag above is flipped without also editing the pool.
3. Requesting it explicitly via the `SINGLE` strategy (`cutout.engine = "gemini_edit"`) still goes
   through `available()`, so lock #1 holds even then.

**To try it:** set `GEMINI_EDIT_ENABLED=true`, confirm `GEMINI_API_KEY` is set, and either add
`gemini_edit` to `ENGINE_POOL` (to enter the AUTO comparison pool) or request it by name via
`SINGLE`. **To revert to the pre-existing behaviour:** set `GEMINI_EDIT_ENABLED=false` (or remove
`gemini_edit` from `ENGINE_POOL`) — nothing else changes; the deterministic dual-engine/local path
is exactly what runs.

### What is NOT independently verified here (unlike the tie-break above)

The tie-break section above has a measured table because that model call was actually exercised
against the live API before being documented. This engine has **not** had the same treatment:

- **Model id** (`GEMINI_EDIT_MODEL=gemini-2.5-flash-image`) — not confirmed against a live
  `listModels` call the way `gemini-3-flash-preview` was. Check
  <https://ai.google.dev/gemini-api/docs/image-generation> for the current id before relying on
  this for a real demo.
- **Request/response field names** (`responseModalities`, `inlineData`/`inline_data` casing) — the
  code accepts either casing defensively (`_extract_image_bytes` in `gemini_edit.py`) precisely
  *because* this wasn't nailed down against live traffic the way `http.py`'s vendor shapes are
  flagged as needing re-confirmation.
- **Pricing** (`_ESTIMATED_COST_USD = 0.04` in `gemini_edit.py`) — an estimate, not a verified
  figure the way the commercial engines' costs are dated. Check
  <https://ai.google.dev/gemini-api/docs/pricing> before enabling this anywhere real spend
  matters.

Do the same live-verification pass the tie-break table above went through before trusting this
path for anything beyond an experiment.

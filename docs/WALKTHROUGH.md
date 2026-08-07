# Walkthrough — the four core capabilities, field to pixel

How each of the four requested capabilities is actually implemented: which config field the browser
sends, which validator normalises it, which stage consumes it, and which test proves it.

1. Background removal
2. Background colour by hex code
3. Exact output resize (e.g. 500 × 500)
4. Automatic centring

This is a map of the code, not a restatement of the maths. For *why* each operation is done the way
it is — linear light, premultiplied resampling, ratio maps, measured error figures — read
[IMAGE_PIPELINE.md](IMAGE_PIPELINE.md). For the field names and types, [API_CONTRACT.md](API_CONTRACT.md).

## Two spelling traps

The contract mixes conventions, and both are load-bearing because `JobConfig` is `extra="forbid"` —
a misspelled field is a hard `422`, not a silently ignored default.

- The centring group is **`centring`** (British), as are `CentringSpec` and `CentringMode`.
- The colour field is **`color`** (US), inside `background`.

So a valid payload contains both `"centring"` and `"color"`.

## The five stages, and why order matters

`backend/app/pipeline.py::process_image` is the only module that knows the sequence.

| # | Stage | AI? | Module |
|---|---|---|---|
| 1 | Cut-out — two segmentation APIs, auto-pick | **yes, the only one** | `app/engines/` |
| 2 | Shadow extract — background plate + ratio map | no | `app/imaging/shadow.py` |
| 3 | Geometry — scale, centre, exact canvas | no | `app/imaging/geometry.py` |
| 4 | Background — hex fill, shadow, composite | no | `app/imaging/composite.py` |
| 5 | Export — PNG / JPEG / TIFF / WebP | no | `app/imaging/export.py` |

**Stage 3 runs before stage 4, deliberately.** Resizing after filling would resample the product
against the flat background colour and bake edge halos in permanently — they cannot be removed
afterwards. This is invariant 3 in [`../backend/CLAUDE.md`](../backend/CLAUDE.md); the comment at
`pipeline.py` says not to "tidy" it, and that is why.

A related consequence: the hex colour is applied to a canvas that is *already* the final size, so
the flat fill is never resampled and stays byte-exact.

---

## 1. Background removal

**Field:** `cutout` → `CutoutSpec` (`app/models.py`)

```json
{"cutout": {"strategy": "auto", "engine": null, "keep_losing_candidate": true}}
```

**Path:** `pipeline.py::_stage1_cutout` → `registry.select_pool()` → `_run_pool()` → `autopick.choose()`

Three things worth knowing:

**An engine returns only alpha, never colour.** (`app/engines/base.py`) Vendors hand back a cut-out
PNG whose transparent region has been zeroed to their taste — unusable here, because stages 2 and 4
both need the *original backdrop pixels*: the plate estimate that drives shadow reconstruction, and
the edge decontamination that un-mixes the backdrop out of the edge. So the pipeline keeps its own
decoded original and takes only the mask. Switching engines therefore cannot change product colour;
only mask quality moves.

**Already-cut-out inputs are reused, not re-segmented.** If the source PNG's own alpha has
`min() < 0.999`, that mask is used as-is — it costs nothing and is almost certainly better than
re-segmenting a mask someone already approved.

**Engines run concurrently and failures are dropped, not raised.** One vendor being down degrades to
single-engine (emitting `SINGLE_ENGINE_ONLY`) instead of failing the image. Both engines are billed
whether or not their mask wins — the accepted cost of the dual-engine design.

With no keys configured, `LocalEngine` (`app/engines/local.py`) runs: classical two-colour linear
mixture keying, free and offline. Not production quality, and it must never be the default once a
real key exists.

**Cost control.** Cut-outs are cached on `sha256(image_bytes) + engine_id`
(`app/engines/cache.py`, keyed by `pipeline.py::cache_key`). Segmentation is deterministic per
engine and stages 2–5 cannot change the mask, so re-running a zip to try a different colour or
canvas costs nothing. Alpha is stored as lossless float32 rather than an 8-bit PNG — see invariant 2
below.

**Proven by:** `tests/test_pipeline.py::TestTheFourRequirements::test_all_four_together` asserts no
pixel of the original white backdrop survives. `tests/test_engines.py` covers the mixture solve,
reject rules and auto-pick scoring. `tests/test_cutout_cache.py::TestCacheInThePipeline` asserts the
second run does not call the engine and is byte-identical to the uncached one.

#### When the photograph contains more than one object

Background removal answers *one* question — foreground or background — and cannot say **which**
foreground you meant. Hand it a furnished room and it returns a clean, confident mask of whatever it
judged most salient. Measured on a real living-room photo: asked for the coffee table, it returned
the sofa. Nothing downstream can detect that, because the mask itself is perfectly good.

So `cutout.subject_prompt` adds the missing step, splitting the work three ways:

| Step | What decides | Where |
|---|---|---|
| Which object is "the coffee table"? | model — judgement | `app/engines/locate.py` |
| Pad that box and crop to it | deterministic geometry | `locate.pad_and_clamp`, `pipeline._encode_crop` |
| Where exactly are its edges? | segmentation engine, unchanged | `app/engines/` |

The mask comes back ROI-sized and is pasted into a full-frame alpha with zeros outside the box
(`pipeline._paste_alpha`), so `alpha_bbox` then finds the subject and nothing else — the centring
stage needs no special case. The surrounding pixels stay available for stage 2's plate estimate, and
`source_size` still matches the uploaded file.

Two failure paths, both loud rather than silent:

- **Object not found** → `SUBJECT_NOT_FOUND`, whole-frame segmentation. Very likely the wrong object,
  so the UI shows a banner, not a badge.
- **Busy backdrop plus a weak alpha score** → `BUSY_SCENE`. Either signal alone has innocent
  explanations; together they are the signature of "cut out the wrong object in a furnished room".
  Suppressed once a subject prompt has succeeded.

The ROI is part of the cut-out cache key, since the same photo cropped to the table and to the sofa
are different problems with different answers.

**Proven by:** `tests/test_subject_prompt.py` — including the y-first coordinate conversion (Gemini
returns `[ymin, xmin, ymax, xmax]`, the opposite of this codebase's convention, and getting it
backwards yields a plausible box in the wrong place).

---

## 2. Background colour by hex code

**Field:** `background.color` → `BackgroundSpec` (`app/models.py`)

```json
{"background": {"transparent": false, "color": "#F5F5F5", "shadow": "preserve", "decontaminate_edges": true}}
```

**Path:** `_normalise_hex` validator → `pipeline.py::_stage4_background` →
`color.py::hex_to_srgb_linear` → `composite.py::flatten` → `solid_background` → `composite_over`

Accepts `#RRGGBB`, `#RGB`, and either without the leading `#`; normalises to uppercase `#RRGGBB`
(`#ABC` → `#AABBCC`). Rejecting happens at the boundary, so a bad value is a `422` rather than a
surprise mid-pipeline.

**Exactness is the requirement, and three separate things protect it:**

- **Compositing happens in linear light.** The hex is decoded sRGB → linear before use and
  re-encoded at export. Compositing gamma-encoded values under-weights the darker contributor,
  which reads as a thin dark rim on every cut-out edge.
- **`color.py::to_uint8` rounds half *away from zero*, not half to even.** `np.rint` would land a
  flat `#808080` on 127 or 128 depending on the value's exact float representation. For a feature
  whose contract is "the background is exactly this colour", that is not acceptable.
- **Adobe RGB output is converted, not copied** (invariant 4). The hex is an sRGB triple; writing
  those same numbers into an Adobe RGB file delivers a visibly different colour. Verified: `#1E3A8A`
  correctly becomes `(44, 61, 135)` in Adobe RGB and round-trips back within 1 level, whereas
  copying would show the viewer `(4, 58, 140)` — **26 levels off**.

`transparent: true` sets `background_linear=None`, passes the product's own alpha through, and drops
any shadow map — a shadow has nothing to modulate, so it is discarded explicitly rather than
silently multiplied into the product's colour.

**Shadow preservation** (`shadow: "preserve"`) is what makes the new background look photographed
rather than pasted. Stage 2 extracts a *multiplicative ratio map*: a ratio of 0.6 means "this pixel
received 60% of the light the bare backdrop did". That is a property of the scene, not of the
backdrop, so it transfers correctly onto any replacement colour. It is gated on background
uniformity (≥ 0.50) and emits `SHADOW_GATE_FAILED` on a lifestyle shot where the separation would be
nonsense.

**Proven by:** `test_pipeline.py::test_background_hex_is_exact_for_any_colour`, parametrised over
`#FFFFFF`, `#000000`, `#F5F5F5`, `#1E3A8A`, `#C81E1E`, asserting the corner pixel equals
`hex_to_srgb8(hex)` exactly. `tests/test_color.py::TestWhyConversionMatters` pins the Adobe RGB
error. `tests/test_export.py` checks the embedded ICC profiles.

---

## 3. Exact output resize

**Field:** `size` → `SizeSpec` (`app/models.py`)

```json
{"size": {"width": 500, "height": 500, "fit": "contain", "margin_pct": 5.0, "allow_upscale": false}}
```

**Path:** `pipeline.py` → `geometry.py::place_on_canvas` → `_compute_scale` → `resample_rgba`

The canvas is allocated at exactly `(height, width)` and **asserted before return**:

```python
assert canvas_rgb.shape == (out_h, out_w, 3), "canvas dimensions must be exact"
```

"Output 500 × 500 px" is a literal requirement, so it is checked rather than assumed. Verified
exact at 1×1, 500×500, 501×499, 1200×628 and 20000×3.

**`margin_pct` is per-side**, so the product occupies `(100 − 2·margin)%` of the canvas. Capped at
45 because both sides apply.

**Resampling is done properly** (`resample_rgba`), and two details matter:

- **Premultiply before interpolating.** Resampling straight-alpha RGBA lets the colour of fully
  transparent pixels bleed into the edge; transparent black bleeding in reads as a dark rim.
  Premultiplied alpha is the only interpolation-safe representation.
- **Step down with area-averaging on large reductions.** `INTER_LANCZOS4` has no prefilter, so a 5×
  reduction point-samples and aliases. Halving with `INTER_AREA` first, then one final Lanczos step,
  gives a clean result.

**Upscaling is refused by default** (invariant 5): a stretched master looks worse than a padded one,
so `allow_upscale=false` clamps scale to 1.0 and emits `UPSCALE_SKIPPED`.

**Fit modes.** `COVER` fills the canvas and crops, emitting `COVER_CROPPED` when product pixels are
lost; it ignores `margin_pct` by design, since an inset margin would contradict filling the frame.

> **Known gap:** `FitMode.PAD` currently behaves identically to `CONTAIN` — `_compute_scale` only
> branches on `COVER`. The enum docstring and the UI dropdown ("Pad — always fills the exact
> canvas") describe a distinction the code does not implement. Likewise, `margin_pct` remains
> adjustable in the UI under `COVER` where it has no effect, and `allow_upscale=true` interpolates
> without emitting any note (`Note.UPSCALED` is never emitted; there is no upscaler module). None of
> these produce wrong dimensions — output size stays exact — but the UI promises behaviour that does
> not exist.

**Proven by:** `test_pipeline.py::test_output_size_is_exact` parametrised over `(500,500)`,
`(1000,1000)`, `(1200,628)`, `(256,256)`. `tests/test_geometry.py` covers exact canvases across all
fit modes, the upscale policy, margin fractions and cover-cropping, plus
`test_premultiplied_resize_does_not_darken_edges` and its companion proving the naive path *would*
have failed.

---

## 4. Automatic centring

**Field:** `centring` → `CentringSpec` (`app/models.py`)

```json
{"centring": {"mode": "bbox", "include_shadow_in_bounds": false, "alpha_threshold": 0.05}}
```

**Path:** `geometry.py::alpha_bbox` / `alpha_centroid` → `place_on_canvas`

Pure deterministic measurement from the alpha channel — contours and bounding boxes. No model is
involved and none should be: "centre the product" has an exact answer, and the client's own report
puts crop/alignment in its *"No — measured"* column.

**Two modes, because they genuinely disagree.** `BBOX` (the default, and the e-commerce convention)
centres the bounding box. `CENTROID` centres the alpha-weighted centre of mass. For an asymmetric
product — a mug with a handle — these differ: the mug looks centred by bbox but sits off-centre by
centroid. Both are offered and the difference is *reported* rather than argued about.

**Placement offsets are integers, and the residual is measured rather than hidden.** Sub-pixel
positioning would mean a second resample and a second dose of softness for at most half a pixel of
accuracy. Instead the leftover error is reported on every result as
`ImageResult.centroid_offset_px`, and the UI prints it. In practice it is ±0.5 px.

**`alpha_threshold` affects measurement only.** It decides which pixels count toward the bounding
box; the stored alpha is never thresholded (invariant 2 — thresholding destroys soft edges
irrecoverably). Verified: sweeping the threshold from 0.0 to 0.9 shifts placement by about 6 px
while the stored alpha keeps all 480 of its soft pixels and all 14 distinct values.

> **Known gap:** `alpha_threshold` is the one contract field with no UI control, though it
> measurably changes placement.

**`include_shadow_in_bounds`** decides whether a preserved shadow counts toward the product bounds.
`false` centres the product itself, which is usually what a catalogue wants; `true` keeps the
product-plus-shadow group centred. A real product decision, so it is explicit rather than implied.

**Proven by:** `test_pipeline.py::test_all_four_together` asserts `|centroid_offset_px| < 3.0` on
both axes. `tests/test_geometry.py` covers bbox centring within half a pixel, centroid mode placing
the centre of mass on the canvas centre, and the two modes disagreeing on an asymmetric product.

---

## Determinism

Stages 2–5 are byte-reproducible: the same input twice produces identical output bytes, and there is
a test for it. It exists to catch a model or a random seed entering the deterministic path. Do not
loosen the comparison to make it pass.

Two real non-determinism sources were found here, and neither was a maths bug:

1. **`ImageCms.createProfile("sRGB")` embeds a creation timestamp.** littleCMS sets it to current
   system time, so two calls a second apart return different bytes. `icc.py::srgb_profile()` is
   `@lru_cache`d for exactly this reason.
2. **BLAS is not reliably pinned to one thread by env vars alone.** `threadpool_limits(1)` is applied
   both process-wide and at the `np.linalg.lstsq` call site in `shadow.py` — the call-site wrap is
   the one that matters, since it runs after numpy is certainly loaded.

Both are guarded by tests that force the actual failure condition (a real time gap, real concurrent
load), because rapid successive calls rarely trigger either.

If new non-determinism appears, suspect a library embedding wall-clock time or thread count into its
output before suspecting the pipeline's maths.

## Seeing it work

```bash
cd backend && source .venv/bin/activate
pytest                       # 582 passed, 0 skipped — no keys, no images
python scripts/demo.py       # 7 configs -> ../data/output/
```

`demo.py` needs no credentials and no photographs: it synthesises a studio shot (white sweep with
falloff, a product, a real cast shadow) and runs the full pipeline. Useful pairs to compare:

- `navy-500.png` vs `white-500.png` — the same cut-out on two exact hex backgrounds
- `lightgrey-shadow-kept.png` vs `lightgrey-shadow-removed.png` — the shadow ratio map, and a
  perfectly flat `#F5F5F5` where no product covers
- `navy-500.png` vs `halo-demo-no-decontam.png` — the halo that edge decontamination prevents
- `banner-1200x628-centroid.png` — a non-square exact canvas

## Out of scope

Glass and clear plastic, sheer fabric, and dedicated hair/fur handling are **not supported**. Soft
alpha is preserved throughout so they degrade gracefully rather than being actively broken, but they
will look bad if shown.

There is also **no measured quality claim available**: no ground-truth mask set and no SAD/MSE/ΔE
harness exist, so the demo can show quality but cannot put a number on it, nor answer "which API
should we buy". See [LIMITATIONS.md](LIMITATIONS.md) before demoing.

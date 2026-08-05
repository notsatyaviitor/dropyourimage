# The imaging maths

Everything here is pure, deterministic code in `backend/app/imaging/` — no AI, no network. Every
number below is measured, not estimated; see the cited test for how.

## Colour management (`color.py`)

Linear-light sRGB is the working space throughout the pipeline. Two profiles are supported:
`SRGB` and `ADOBE_RGB` (required for PSD, 8-bit embedded, per the client report).

**Why conversion, not copying, matters:** the hex a user types is an sRGB value. Copying its bytes
into an Adobe RGB file instead of converting them is invisible on white (`#FFFFFF`, `#F5F5F5` —
zero error) and badly wrong on saturated colours — measured directly:

| Colour | Copy-instead-of-convert error |
|---|---|
| `#1E3A8A` (navy) | 30 levels |
| `#C81E1E` (red) | 33 levels |

`convert_linear()` always does this correctly. `test_export.py::TestEncodeExactness` and
`test_color.py::TestWhyConversionMatters` pin these numbers.

### The embeddable Adobe RGB ICC profile is synthesised, not Adobe's file

Pillow can only generate an sRGB profile via littleCMS; Adobe's own Adobe RGB (1998) `.icc` is a
licensed binary asset, not something to redistribute. `icc.py` hand-builds a 484-byte ICC v2
matrix-shaper profile from the documented primaries and gamma. Validated against littleCMS
independently: converting our Adobe RGB bytes back to sRGB agrees with our own matrices to within
1 level (pure 8-bit quantisation) — see `test_export.py::TestIccProfileWriter`.

**A real, non-obvious bug found here:** `ImageCms.createProfile("sRGB")` embeds littleCMS's
*current system time* in the ICC header, so two calls a second apart return different bytes.
That silently broke the pipeline's own byte-reproducibility guarantee. Fixed by caching
`icc.srgb_profile()` — see `backend/CLAUDE.md`'s Determinism section for the full story.

## Geometry (`geometry.py`)

- **Premultiplied resampling.** Resizing straight-alpha RGBA lets transparent pixels' colour
  bleed into partial-coverage edges — measured: a naive resize pulled mean edge colour from 1.0
  down to ~0.87 (worst pixel ~0.29) on a white-on-transparent test disc; the premultiplied path
  held exactly 1.0. `test_geometry.py::TestResampleRGBA`.
- **Area-averaging before Lanczos** on large reductions — Lanczos has no prefilter and aliases on
  a >2x reduction otherwise.
- **Never upscale past 100%** by default. A padded master beats a stretched one; `allow_upscale`
  opts in, and even then the pipeline pads rather than stretches unless an AI upscaler is wired up.
- **Centring** is alpha-bbox or alpha-centroid, both exactly measured — never a model guess. The
  client report's own Feature 1 rule ("contour centroid vs frame centre") is the same idea.

## Compositing (`composite.py`)

- **Linear-light `over`.** Compositing gamma-encoded values under-weights the darker
  contributor — the textbook cause of a dark rim on every cut-out edge. Golden-pixel test: 50%
  white over black composites to linear 0.5, which encodes to sRGB **188**, not 128 — getting 128
  means the composite ran in gamma space.
- **Edge decontamination.** A product cut from a white studio backdrop keeps white in its
  semi-transparent edge pixels; compositing that onto navy shows a halo. Un-mixes the real
  backdrop colour out of the edge band. Measured on a saturated background: edge-band green
  channel **119.8 → 84.6** (navy's own green is 58) after decontamination.

## Shadow and reflection preservation (`shadow.py`)

No AI — the physically correct model for a shadow cast on a uniform backdrop. For a bare-backdrop
pixel the camera saw `O = P`; where the product blocks light, `O = P * r` with `r < 1`. That `r`
is a property of the scene's lighting, not the backdrop's colour, so multiplying a *different*
backdrop colour by the same `r` reproduces the same shadow on it.

- **Quadratic, not planar, backdrop-plate fit.** Real studio lighting falloff is quadratic in
  radius (vignetting, softbox falloff). A planar fit left up to ~2.8% residual at the corners —
  enough to exceed the ratio deadband and visibly drift the "flat" replacement background off the
  requested hex. Quadratic fit: **residual 2.76% → 0.19%**.
- **Normalised convolution, not a plain blur**, when smoothing the ratio map — a plain blur drags
  shadow values under the product and washes out the ground-contact shadow, exactly the part that
  sells the effect.
- Measured shadow-ratio recovery against ground truth: **mean absolute error 0.0012**.
- **Reflection (`r > 1`) clips at white** rather than being invented — a blown highlight on the
  original genuinely cannot be reconstructed.
- **Uniformity gate.** Below `UNIFORMITY_GATE` (0.50, provisional — re-tune against real
  photographs), the shadow is dropped and `Note.SHADOW_GATE_FAILED` is emitted rather than
  producing a smear on a lifestyle shot.

## Export (`export.py`)

Decode normalises EXIF orientation, embedded ICC profiles (converted to sRGB, not
reinterpreted — a file tagged Adobe RGB read as sRGB comes out visibly desaturated), CMYK, and
16-bit depth. JPEG uses 4:4:4 chroma subsampling (not 4:2:0) — subsampling smears saturated
product edges, exactly what a packshot is judged on.

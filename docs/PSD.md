# PSD delivery (requirement 5)

A layered Photoshop file — named `PROD`/`SHADOW`/`BG` layers plus a vector clipping path, per the
client report's Feature 2 naming convention (confirmed against pages 6–7 of the report). This is
the highest-risk item in the delivery plan, and the one most worth reading carefully before
demoing or shipping.

## Architecture: try Adobe, fall back to pure Python

```
app/psd/service.py        produce_psd() — the only entry point pipeline.py calls
  ├─ app/psd/photoshop_api.py   Adobe Photoshop API client — UNVERIFIED, see below
  └─ app/psd/fallback.py        pytoshop-based writer — the path actually exercised in this build
       └─ app/psd/vector_path.py   hand-encoded PSD path resources
  app/psd/validate.py       independent read-back check, using psd-tools
```

`produce_psd()` tries Adobe first when `Settings.adobe_configured` is true; any failure there
falls through to the local writer. **No Adobe credentials were provided for this build**, so
every run in practice takes the fallback path. That path is fully built, tested, and — this is
the important finding — capable of a real vector clipping path, not just raster layers.

## The Adobe Photoshop API client is unverified

`app/psd/photoshop_api.py` is transcribed from Adobe's published Firefly Services / Photoshop API
documentation, but **has never been run against a live account**. `ADOBE_CLIENT_ID` /
`ADOBE_CLIENT_SECRET` / `ADOBE_ORG_ID` are blank in this environment. Before this client is used
against a real account:

- Confirm the IMS token endpoint and scope string against current onboarding docs.
- Confirm the Photoshop API's document-manipulation request shape (field names, how a target
  layer group is addressed, how to trigger a Make Work Path action) against a real response —
  the body in this client is a well-informed placeholder, not a tested integration.
- Author `templates/base.psd` by hand (the `PROD`/`SHADOW`/`BG`/`PATH` groups the client expects
  to receive pixel content into). It does not exist yet — building it before there is a live
  account to test it against would be effort spent on an unverifiable guess.

## The fallback writer works, with two real upstream defects worked around

`pytoshop==1.2.1` (the latest release; the plan's original `1.2.4` pin doesn't exist) has two
genuine packaging/implementation defects, found while building this, not assumed in advance:

1. **RLE compression is broken in the installed wheel.** Its compiled `packbits` Cython extension
   did not build/ship, so `compress_rle` raises `NameError: name 'packbits' is not defined` the
   moment it runs. Worked around by writing every PSD with `compression=raw` — larger files, but
   a completely standard, valid PSD compression mode with no quality cost.
2. **Layer names round-trip with a stray trailing NUL** via the extended Unicode name tagged
   block (readable through `psd-tools`' high-level `.name`; the legacy Pascal-string name is
   clean). `validate.py` strips trailing NULs before comparing names.

## A real vector clipping path — better than the plan assumed

The original delivery plan treated a real vector path as needing either the Adobe API or
"hand-authoring PSD Image Resource blocks" as a high-risk last resort. Building this surfaced
that the second option is more tractable than assumed: `pytoshop` exposes a raw
`GenericImageResourceBlock(resource_id, name, data)` escape hatch, and Adobe's path resource
format (IDs 2000–2997, plus 2999 designating which one is *the* clipping path) is fully
documented, binary, and requires no compiled Photoshop dependency to construct.

`app/psd/vector_path.py` traces the alpha channel (`cv2.findContours` + Douglas-Peucker
simplification), encodes it as straight-line Bezier knots (each knot's control points collapsed
onto its anchor — a valid closed path, just not curve-smoothed), and writes both resources by
hand. Verified two ways:

- An independent decoder (a second, separately-written implementation of the record layout, not
  a wrapper around the encoder) round-trips test polygons to sub-pixel accuracy.
- `psd-tools` opens files containing it without error, and — this mattered — its own typed reader
  for resource 2999 caught a real mistake: an earlier version appended a 4-byte "flatness" field
  that a real independent implementation does not expect and silently drops on re-serialization.
  Fixed once the actual wire format was confirmed by parsing psd-tools' own output byte-by-byte.

**Not yet confirmed:** an actual open in Adobe Photoshop. This environment has none installed.
Do this before the file goes to a client — if a real Photoshop check ever contradicts anything
here, trust Photoshop and fix this module, not the other way round.

## Two real bugs found only by rendering the composite, not by structural checks

Both files passed every structural check (right layer names, right count, valid ICC profile) and
only showed the actual problem when the layer stack was actually rendered:

1. **Layer order was backwards.** `pytoshop.user.nested_layers.nested_layers_to_psd` takes its
   input **top-to-bottom** (confirmed against its source: `_flatten_layers` reverses the list
   before writing PSD's bottom-first file records). Passing layers bottom-to-top made the opaque
   background layer cover everything above it — the product and shadow were completely hidden,
   even though each layer, viewed in isolation, looked correct.
2. **The shadow was applied twice.** The background layer had the shadow ratio baked directly
   into its own pixels *and* a separate semi-transparent black shadow layer sat on top of it —
   doubling the darkening, and making the shadow impossible to remove by deleting one layer
   (defeating the entire point of a separate layer). Fixed by keeping the background layer flat
   and letting the shadow layer alone carry the effect.

Neither would have been caught by checking layer names or file structure alone —
`tests/test_psd_fallback.py::TestComposite` now renders the actual layer stack
(`psd_tools.PSDImage.composite(force=True)`) and checks pixel values, specifically to guard
against this class of defect recurring.

### `force=True` is required to see the real content

`pytoshop` never writes a genuine flattened top-level preview (only a blank placeholder), and
`psd-tools`' `.composite()` silently prefers that stale preview over real layer compositing
whenever the file merely *declares* one exists — regardless of content. The first composite
render of a demo file came back solid black for exactly this reason, before `force=True` was
found. A real Photoshop open always recomposites from the actual layers regardless, so this is a
`psd-tools`-quick-preview quirk, not a defect in the files this module writes.

### The shadow layer is a known, understood approximation

A PSD "Normal" blend layer composites in gamma-encoded space, not the linear light the raster
pipeline's own multiply uses — measured directly: a composited shadow pixel tracks
`encoded_background x ratio`, not the linear-light-correct result. The PSD's shadow will
therefore look very slightly different in intensity from the PNG/JPEG output for the same job.
The *magnitude* is correct (proportional to the requested ratio, confirmed via luminance-ratio
tests — not compounded, not doubled); the colour-space nuance is not bit-exact, and making it so
would mean either baking the shadow into the background layer (tried, reverted — see above) or
emulating Photoshop's internal compositing space precisely, out of scope here.

## Known limitations

- **External contour only.** A product with a hole in its silhouette (a mug handle's gap, for
  instance) is not represented as a separate subpath. The format supports it (more
  length-header/knot record groups), it was simply out of scope for the time available.
- **Straight-line Bezier segments**, not curve-fitted. Looks slightly faceted rather than smooth
  in the Paths panel; a retoucher can smooth it manually. True cubic Bezier fitting is a real
  follow-up, not attempted here.
- **Single saved path.** No support for multiple named paths in one file.
- **Never opened in real Adobe Photoshop.** See above — the single most important gap to close
  before this ships.

## Verifying it yourself

```bash
cd backend && source .venv/bin/activate
pip install -r requirements-psd-fallback.txt   # pytoshop; skipped gracefully if absent
pytest tests/test_psd_fallback.py tests/test_psd_vector_path.py tests/test_psd_pipeline.py -q
python scripts/demo.py   # writes data/output/ including a .psd — open it and look
```

`validate_psd()` (`app/psd/validate.py`) is also callable directly against any PSD to get a
structural report: layer names present, colour profile and depth, and — if a vector path was
requested — its vertex count and winding direction.

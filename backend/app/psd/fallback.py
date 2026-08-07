"""Pure-Python layered PSD writer — the path used whenever the Adobe Photoshop API is
unavailable, which is every run in this build (no Adobe credentials were provided).

Uses `pytoshop`. Two real defects in the installed `pytoshop==1.2.1` wheel were found while
building this and are worked around here, not upstream:

1. **RLE compression is broken in this wheel.** Its compiled `packbits` Cython extension did not
   build/ship, so `compress_rle` raises `NameError: name 'packbits' is not defined` the moment it
   is used. Worked around by writing with `compression=raw` throughout — larger files, but a
   completely valid, standard PSD compression mode; there is no quality or correctness cost.
2. **Layer names round-trip with a stray trailing NUL** via the extended Unicode name tagged
   block, readable through `psd-tools`' high-level `.name` (the legacy Pascal-string name is
   clean). Worked around in `validate.py` by stripping trailing NULs before comparing names.

The vector clipping path (see `vector_path.py`) is injected as a raw `GenericImageResourceBlock`
— `pytoshop` has no dedicated API for path resources, but PSD resource blocks are just tagged
`(id, name, data)` triples, so nothing about that requires `pytoshop`'s cooperation.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np

# `pytoshop` is required for a PSD and irrelevant to every other output format, so a missing one
# must not take the module down at import time. It used to: `service.py` imports this module, and
# `pipeline.py` imports that, so an absent wheel surfaced as a bare ModuleNotFoundError reported to
# the user as `internal_error` — despite `PsdUnavailable` existing in the taxonomy for exactly this.
# Deferring the failure to `build_psd` lets a PNG/JPEG/TIFF job run normally on a box with no
# pytoshop, and gives a PSD job an error that says what to install.
try:
    import pytoshop
    from pytoshop import enums as pt_enums
    from pytoshop.image_data import ImageData
    from pytoshop.image_resources import GenericImageResourceBlock, ImageResources
    from pytoshop.user import nested_layers

    PYTOSHOP_IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover - exercised by test_psd_unavailable
    pytoshop = None  # type: ignore[assignment]
    pt_enums = None  # type: ignore[assignment]
    ImageData = None  # type: ignore[assignment]
    GenericImageResourceBlock = ImageResources = nested_layers = None  # type: ignore[assignment]
    PYTOSHOP_IMPORT_ERROR = str(exc)

from app.core.errors import PsdUnavailable
from app.imaging import color as C
from app.imaging import icc
from app.models import Note, PsdSpec
from app.psd import vector_path as vp

# See vector_path.py's module docstring for what these are.
_ICC_PROFILE_RESOURCE_ID = 1039  # Adobe "Photoshop File Formats" resource ID for an ICC profile


@dataclass
class PsdBuildResult:
    data: bytes
    notes: list[Note]


def _layer_channels(rgb_encoded_u8: np.ndarray, alpha_u8: np.ndarray) -> dict:
    """pytoshop's per-channel dict: 0/1/2 = R/G/B, -1 = alpha."""
    return {
        0: rgb_encoded_u8[..., 0],
        1: rgb_encoded_u8[..., 1],
        2: rgb_encoded_u8[..., 2],
        -1: alpha_u8,
    }


def _encode_layer_float(rgb_linear: np.ndarray, profile: C.Profile) -> np.ndarray:
    """Linear-light sRGB -> `profile`-encoded float in [0, 1], before 8-bit quantisation.

    Split out from `_encode_layer_pixels` so the merged-composite builder can blend in encoded
    space at full float precision and quantise exactly once, rather than compositing already-
    rounded 8-bit layers and accumulating error at every stage.
    """
    return profile.encode(C.convert_linear(rgb_linear, C.SRGB, profile))


def _encode_layer_pixels(rgb_linear: np.ndarray, profile: C.Profile) -> np.ndarray:
    """Linear-light sRGB -> `profile`-encoded 8-bit.

    Must route through whichever profile `spec.profile` actually asks for. An earlier version of
    this hardcoded Adobe RGB regardless of the caller's request, while the embedded ICC tag
    correctly followed `spec.profile` — so an sRGB request produced Adobe-RGB-encoded pixel bytes
    under an sRGB profile tag, a real colour defect caught by
    `test_srgb_profile_is_also_detected_correctly` cross-checking the two against each other.
    """
    return C.to_uint8(_encode_layer_float(rgb_linear, profile))


def _merged_composite(
    bg_encoded: np.ndarray,
    product_encoded: np.ndarray,
    product_alpha: np.ndarray,
    shadow_ratio: np.ndarray | None,
) -> np.ndarray:
    """Render the layer stack into PSD's merged image section. Returns (3, h, w) uint8.

    **Why this exists.** `nested_layers_to_psd` writes layer records but never populates
    `PsdFile.image_data`, the flattened preview PSD carries at the end of the file. Photoshop
    ignores it and re-composites from the layers, so the file looked correct there — but every
    *other* consumer reads that section, and it was all zeros. The delivered PSD showed as a solid
    black image in OS thumbnailers, preview apps, and `psd_tools.PSDImage.composite()`. Measured on
    a real 500x500 delivery: layers `BG` mean 1.000 and `PROD` mean 0.161, composite mean 0.000.

    **Why it blends in encoded space rather than linear light.** This must reproduce what the layer
    stack renders to, not what the PNG export renders to. Photoshop's Normal blend on these layers
    composites in gamma-encoded space (the same measured behaviour `build_psd` already documents
    for the shadow layer). Compositing in linear light here would produce a preview that disagrees
    with the layers it is supposed to be previewing — a worse bug than the one being fixed.

    So the shadow term is exactly the black-at-alpha-(1-ratio) layer above it resolves to:
    ``bg * ratio``.
    """
    out = bg_encoded.astype(np.float32, copy=True)

    if shadow_ratio is not None:
        out *= np.clip(shadow_ratio, 0.0, 1.0)[..., None]

    a = np.clip(product_alpha, 0.0, 1.0)[..., None].astype(np.float32)
    out = product_encoded.astype(np.float32) * a + out * (1.0 - a)

    # PSD wants channel-first; quantise once, here.
    return np.ascontiguousarray(C.to_uint8(np.clip(out, 0.0, 1.0)).transpose(2, 0, 1))


def build_psd(
    *,
    product_rgb_linear: np.ndarray,
    product_alpha: np.ndarray,
    background_color_linear: np.ndarray,
    shadow_ratio: np.ndarray | None,
    spec: PsdSpec,
) -> PsdBuildResult:
    """Assemble the layered PSD: `spec.background_layer_name` / `spec.shadow_layer_name` /
    `spec.product_layer_name` bottom to top, Adobe RGB (1998) 8-bit, plus an optional vector
    clipping path traced from `product_alpha`.

    Inputs are the same linear-light arrays the raster export stage works with — this function
    does not re-run any pipeline stage, it only re-encodes what stage 4 already produced into
    PSD's layer model instead of a single flattened raster.

    Raises `PsdUnavailable` when pytoshop is missing — a typed, actionable error rather than the
    ModuleNotFoundError this used to raise at import time.
    """
    if pytoshop is None:
        raise PsdUnavailable(
            "The PSD writer needs pytoshop, which is not installed "
            f"({PYTOSHOP_IMPORT_ERROR}). Install it with "
            "`pip install -r backend/requirements.txt`, or drop 'psd' from the requested "
            "output formats to deliver the raster formats only."
        )

    h, w = product_alpha.shape
    notes: list[Note] = []
    profile = C.get_profile(spec.profile.value)

    # Flat, undarkened fill. The shadow lives ENTIRELY in its own layer below (never also baked
    # in here) — a retoucher must be able to delete or adjust just that one layer and get a clean
    # flat background back. Baking it into both would double the effect and make it undoable.
    bg_rgb = np.broadcast_to(background_color_linear, (h, w, 3)).astype(np.float32)

    bg_layer = nested_layers.Image(
        name=spec.background_layer_name,
        channels=_layer_channels(
            _encode_layer_pixels(bg_rgb, profile), np.full((h, w), 255, dtype=np.uint8)
        ),
        top=0,
        left=0,
        bottom=h,
        right=w,
    )

    # `nested_layers_to_psd` takes its `layers` argument TOP-TO-BOTTOM (confirmed against its
    # source: `_flatten_layers` reverses the list before writing PSD's bottom-first layer
    # records) — passing bottom-to-top here once made every layer under BG invisible, since an
    # opaque "top" layer covers everything below it. Built top-down: product, then shadow, then
    # background, appended in reverse and flipped once at the end so each `if` block below can
    # read naturally without worrying about final order.
    layers_bottom_up = [bg_layer]

    if shadow_ratio is not None:
        # The shadow as its own layer: solid black whose alpha is (1 - ratio), so a "Normal"
        # blend darkens whatever sits beneath it by approximately the requested ratio — a
        # retoucher can then adjust or delete this one layer without touching BG or PROD.
        # `shadow_ratio` is already forced to 1.0 under the product by shadow.py's own logic
        # (there is nothing to measure there), so this needs no separate product-area masking.
        #
        # Known, understood approximation: alpha-compositing in a PSD "Normal" blend layer
        # happens in gamma-encoded space, not the linear light the raster pipeline's own
        # multiply uses (measured directly — a composited shadow pixel tracks
        # `encoded_background x ratio`, not the linear-light-correct result). The PSD's shadow
        # will therefore look very slightly different in intensity from the PNG/JPEG output for
        # the same job. Getting them to match exactly would mean either baking the shadow into
        # the background layer's own pixels (which was tried and reverted — it made the shadow
        # undeletable, defeating the purpose of a separate layer) or emulating Photoshop's
        # internal compositing colour space precisely, which is out of scope here. The magnitude
        # is correct (proportional to the requested ratio, not compounded); the colour-space
        # nuance is not bit-exact. See tests/test_psd_fallback.py::TestComposite for the
        # measurement this claim rests on.
        shadow_alpha = np.clip(1.0 - shadow_ratio, 0.0, 1.0)
        shadow_rgb = np.zeros((h, w, 3), dtype=np.float32)
        shadow_layer = nested_layers.Image(
            name=spec.shadow_layer_name,
            channels=_layer_channels(
                _encode_layer_pixels(shadow_rgb, profile), C.to_uint8(shadow_alpha)
            ),
            top=0,
            left=0,
            bottom=h,
            right=w,
        )
        layers_bottom_up.append(shadow_layer)

    product_layer = nested_layers.Image(
        name=spec.product_layer_name,
        channels=_layer_channels(
            _encode_layer_pixels(product_rgb_linear, profile), C.to_uint8(product_alpha)
        ),
        top=0,
        left=0,
        bottom=h,
        right=w,
    )
    layers_bottom_up.append(product_layer)

    psd = nested_layers.nested_layers_to_psd(
        layers_bottom_up[::-1],  # nested_layers_to_psd wants top-to-bottom — see comment above
        color_mode=pt_enums.ColorMode.rgb,
        depth=pt_enums.ColorDepth.depth8,
        # (width, height) — NOT (height, width). pytoshop's own docstring says "The shape in the
        # form ``(height, width)``" and its code immediately does `width, height = size`. The
        # docstring is wrong; trust the source. Passing (h, w) here produced a canvas transposed
        # against its own layers, which was invisible for as long as every PSD this project wrote
        # was square (500x500 deliveries, square test fixtures) and only surfaced on a
        # non-square image. See tests/test_psd_availability.py::TestNonSquareCanvas.
        size=(w, h),
        compression=pt_enums.Compression.raw,  # see module docstring: RLE is broken in this wheel
    )

    resource_blocks = list(psd.image_resources.blocks) if psd.image_resources else []
    resource_blocks.append(
        GenericImageResourceBlock(
            name='', resource_id=_ICC_PROFILE_RESOURCE_ID, data=icc.profile_bytes(spec.profile.value)
        )
    )

    if spec.vector_clipping_path:
        clip = vp.build_clipping_path(product_alpha, w, h, name=spec.path_name)
        if clip is not None:
            resource_blocks.append(
                GenericImageResourceBlock(
                    name=spec.path_name, resource_id=vp.PATH_RESOURCE_ID, data=clip.path_data
                )
            )
            resource_blocks.append(
                GenericImageResourceBlock(
                    name='',
                    resource_id=vp.CLIPPING_PATH_DESIGNATION_ID,
                    data=clip.designation,
                )
            )
        else:
            notes.append(Note.PSD_FALLBACK_RASTER)
    else:
        notes.append(Note.PSD_FALLBACK_RASTER)

    psd.image_resources = ImageResources(blocks=resource_blocks)

    # Populate the flattened preview. `nested_layers_to_psd` leaves this empty, which reads as a
    # solid black image in every consumer that trusts it (Photoshop does not — it re-composites
    # from the layers, which is why this survived review). See `_merged_composite`.
    psd.image_data = ImageData(
        channels=_merged_composite(
            _encode_layer_float(bg_rgb, profile),
            _encode_layer_float(product_rgb_linear, profile),
            product_alpha,
            shadow_ratio,
        )
    )

    buf = io.BytesIO()
    psd.write(buf)
    return PsdBuildResult(data=buf.getvalue(), notes=notes)


def build_layered_psd(
    *,
    frame_rgb_linear: np.ndarray,
    objects: list[tuple[str, np.ndarray]],
    spec: PsdSpec,
) -> PsdBuildResult:
    """One layer per object, stacked over the original frame.

    Layer order, bottom to top: the full frame (named by `spec.background_layer_name`), then each
    object with the smallest last, so a large item cannot hide a small one resting on it — a pillow
    must end up above the bed, not buried under it.

    Each object also gets its own **saved path**. PSD allocates path resources across IDs
    2000-2997, so N paths simply occupy 2000..2000+N-1. Resource 2999 designates which single path
    is *the* document clipping path; there can only be one, so it names the largest object.

    Distinct from `build_psd`, which delivers a product on a replaced background with a separate
    shadow layer. Here the background *is* the photograph and there is no shadow to reconstruct.
    """
    if pytoshop is None:
        raise PsdUnavailable(
            "The PSD writer needs pytoshop, which is not installed "
            f"({PYTOSHOP_IMPORT_ERROR}). Install it with `pip install -r backend/requirements.txt`."
        )
    if not objects:
        raise PsdUnavailable("A layered PSD needs at least one object layer.")

    h, w = frame_rgb_linear.shape[:2]
    notes: list[Note] = []
    profile = C.get_profile(spec.profile.value)
    frame_encoded = _encode_layer_pixels(frame_rgb_linear, profile)

    # Largest first for path allocation and the 2999 designation; reversed for stacking.
    ordered = sorted(objects, key=lambda kv: float(np.asarray(kv[1]).sum()), reverse=True)

    layers_bottom_up = [
        nested_layers.Image(
            name=spec.background_layer_name,
            channels=_layer_channels(frame_encoded, np.full((h, w), 255, np.uint8)),
            top=0, left=0, bottom=h, right=w,
        )
    ]
    # `ordered` is largest-first and this list is bottom-up, so appending in order puts the
    # largest just above the frame and the smallest on top. Reversing here (as a first version
    # did) buried every small object under the big one it sits on.
    for name, alpha in ordered:
        layers_bottom_up.append(
            nested_layers.Image(
                name=name,
                channels=_layer_channels(frame_encoded, C.to_uint8(np.clip(alpha, 0.0, 1.0))),
                top=0, left=0, bottom=h, right=w,
            )
        )

    psd = nested_layers.nested_layers_to_psd(
        layers_bottom_up[::-1],           # top-to-bottom — see build_psd's note
        color_mode=pt_enums.ColorMode.rgb,
        depth=pt_enums.ColorDepth.depth8,
        size=(w, h),                      # (width, height); pytoshop's docstring is wrong
        compression=pt_enums.Compression.raw,
    )

    blocks = list(psd.image_resources.blocks) if psd.image_resources else []
    blocks.append(
        GenericImageResourceBlock(
            name='', resource_id=_ICC_PROFILE_RESOURCE_ID,
            data=icc.profile_bytes(spec.profile.value),
        )
    )

    traced = 0
    if spec.vector_clipping_path:
        for index, (name, alpha) in enumerate(ordered):
            if vp.PATH_RESOURCE_ID + index > vp.MAX_PATH_RESOURCE_ID:
                break
            clip = vp.build_clipping_path(alpha, w, h, name=name)
            if clip is None:
                continue
            blocks.append(
                GenericImageResourceBlock(
                    name=name, resource_id=vp.PATH_RESOURCE_ID + index, data=clip.path_data
                )
            )
            if traced == 0:
                # Only one path can be *the* clipping path; the largest object is the one someone
                # cutting this image out would mean.
                blocks.append(
                    GenericImageResourceBlock(
                        name='', resource_id=vp.CLIPPING_PATH_DESIGNATION_ID,
                        data=clip.designation,
                    )
                )
            traced += 1

    if traced == 0:
        notes.append(Note.PSD_FALLBACK_RASTER)

    psd.image_resources = ImageResources(blocks=blocks)

    # Every object is already visible in the frame layer, so the flattened preview is the frame.
    psd.image_data = ImageData(channels=np.ascontiguousarray(frame_encoded.transpose(2, 0, 1)))

    buf = io.BytesIO()
    psd.write(buf)
    return PsdBuildResult(data=buf.getvalue(), notes=notes)

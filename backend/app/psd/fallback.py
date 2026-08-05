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
import pytoshop
from pytoshop import enums as pt_enums
from pytoshop.image_resources import GenericImageResourceBlock, ImageResources
from pytoshop.user import nested_layers

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


def _encode_layer_pixels(rgb_linear: np.ndarray, profile: C.Profile) -> np.ndarray:
    """Linear-light sRGB -> `profile`-encoded 8-bit.

    Must route through whichever profile `spec.profile` actually asks for. An earlier version of
    this hardcoded Adobe RGB regardless of the caller's request, while the embedded ICC tag
    correctly followed `spec.profile` — so an sRGB request produced Adobe-RGB-encoded pixel bytes
    under an sRGB profile tag, a real colour defect caught by
    `test_srgb_profile_is_also_detected_correctly` cross-checking the two against each other.
    """
    converted = C.convert_linear(rgb_linear, C.SRGB, profile)
    return C.to_uint8(profile.encode(converted))


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
    """
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
        size=(h, w),
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

    buf = io.BytesIO()
    psd.write(buf)
    return PsdBuildResult(data=buf.getvalue(), notes=notes)

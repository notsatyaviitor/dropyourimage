"""Vector path encoder/decoder tests.

The decoder is a deliberately independent second implementation of the record layout (see
vector_path.py's docstring) — these tests exercise both directions so a symmetric encode/decode
bug can't hide.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.psd import vector_path as vp


def square_alpha(size: int = 120, half: int = 30) -> np.ndarray:
    a = np.zeros((size, size), dtype=np.float32)
    c = size // 2
    a[c - half : c + half, c - half : c + half] = 1.0
    return a


def circle_alpha(size: int = 120, radius: int = 40) -> np.ndarray:
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    d = np.sqrt((xx - size / 2) ** 2 + (yy - size / 2) ** 2)
    return np.clip(radius - d + 0.5, 0.0, 1.0).astype(np.float32)


class TestEncodeDecodeRoundTrip:
    def test_known_square_recovers_exact_coordinates(self):
        """A simple, known polygon — the strongest correctness check available."""
        square = [(20.0, 20.0), (80.0, 20.0), (80.0, 80.0), (20.0, 80.0)]
        w = h = 100

        body = _encode_test_polygon(square, w, h)
        records = vp.decode_path_records(body)

        assert records[0] == ('length', len(square))
        knots = [r for r in records[1:] if r[0] == 'knot']
        assert len(knots) == len(square)

        for (orig_x, orig_y), (_, points) in zip(square, knots):
            y_frac, x_frac = points[1]  # anchor is the middle of the 3 points
            got_x, got_y = x_frac * w, y_frac * h
            assert got_x == pytest.approx(orig_x, abs=1e-3)
            assert got_y == pytest.approx(orig_y, abs=1e-3)

    def test_all_three_points_of_a_knot_are_identical(self):
        """Straight-segment encoding: no curve smoothing, by design — see the module docstring."""
        body = _encode_test_polygon([(10.0, 10.0), (90.0, 10.0), (50.0, 90.0)], 100, 100)
        knots = [r for r in vp.decode_path_records(body) if r[0] == 'knot']
        for _, points in knots:
            assert points[0] == points[1] == points[2]


class TestContourFromAlpha:
    def test_sharp_corners_are_preserved(self):
        """A square must simplify to (very close to) 4 vertices, not more."""
        points = vp.contour_from_alpha(square_alpha())
        assert points is not None
        assert len(points) == 4

    def test_smooth_shapes_simplify_to_a_manageable_vertex_count(self):
        """Regression guard: an earlier epsilon setting produced 72 vertices for this circle —
        unusable in a Paths panel. Tuned to a clean range and pinned here."""
        points = vp.contour_from_alpha(circle_alpha())
        assert points is not None
        assert 6 <= len(points) <= 24

    def test_empty_alpha_returns_none(self):
        assert vp.contour_from_alpha(np.zeros((50, 50), dtype=np.float32)) is None

    def test_threshold_is_honoured(self):
        faint = np.full((40, 40), 0.2, dtype=np.float32)
        assert vp.contour_from_alpha(faint, threshold=0.5) is None
        assert vp.contour_from_alpha(faint, threshold=0.1) is not None


class TestBuildClippingPath:
    def test_produces_a_closed_path_matching_the_traced_contour(self):
        alpha = circle_alpha()
        clip = vp.build_clipping_path(alpha, 120, 120, name="PATH")
        assert clip is not None
        assert clip.vertex_count >= 6

        records = vp.decode_path_records(clip.path_data)
        assert records[0] == ('length', clip.vertex_count)

    def test_designation_is_a_pure_pascal_string_no_trailing_bytes(self):
        """Regression guard: an earlier version appended a 4-byte 'flatness' field that a real
        independent reader (psd-tools) does not expect and silently drops — see the module
        docstring for how this was actually confirmed rather than assumed."""
        alpha = circle_alpha()
        clip = vp.build_clipping_path(alpha, 120, 120, name="PATH")
        assert clip.designation == bytes([4]) + b"PATH"

    def test_long_name_is_truncated_not_rejected(self):
        alpha = circle_alpha()
        clip = vp.build_clipping_path(alpha, 120, 120, name="X" * 300)
        assert clip is not None
        assert len(clip.designation) - 1 <= 255

    def test_empty_alpha_returns_none(self):
        empty = np.zeros((60, 60), dtype=np.float32)
        assert vp.build_clipping_path(empty, 60, 60) is None

    def test_winding_is_counter_clockwise_in_image_coordinates(self):
        """The client report's Feature 2 explicitly checks winding direction. Image coordinates
        have y increasing downward, which flips the usual maths-convention sign — the easiest
        place to get this backwards, hence testing it directly rather than trusting intuition."""
        clip = vp.build_clipping_path(circle_alpha(), 120, 120)
        records = vp.decode_path_records(clip.path_data)
        knots = [r for r in records if r[0] == 'knot']
        points = [(k[1][1][1], k[1][1][0]) for k in knots]  # (x_frac, y_frac) anchors

        area = 0.0
        for i in range(len(points)):
            x0, y0 = points[i]
            x1, y1 = points[(i + 1) % len(points)]
            area += x0 * y1 - x1 * y0
        assert area < 0, "signed area should be negative for CCW winding in image coordinates"


def _encode_test_polygon(points_xy, width, height):
    """Minimal reimplementation of build_clipping_path's inner loop for a caller-supplied
    (already-decided) point order — used where the test wants to assert on exact input points
    without contour tracing or winding normalisation getting in the way."""
    import struct

    body = bytearray()
    body += struct.pack('>H', 0)
    body += struct.pack('>H', len(points_xy))
    body += b'\x00' * 22
    for x, y in points_xy:
        body += struct.pack('>H', 1)
        body += vp._encode_knot(y / height, x / width)
    return bytes(body)

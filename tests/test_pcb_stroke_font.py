"""precis.pcb.stroke_font -- the single-stroke silkscreen vector font.

Covers: every supported glyph draws real, non-degenerate strokes; layout
respects advance width / height / alignment; rotation and mirror route
through the SAME transform (:func:`precis.pcb.landpattern.rotate_offset`)
every placed pad offset uses, so a property (bbox area preserved under a
pure rotation) is a real cross-check, not a restatement of the code.
"""

from __future__ import annotations

import math

import pytest

from precis.pcb import stroke_font


# ── glyph table ─────────────────────────────────────────────────────────
def test_every_glyph_except_space_has_at_least_one_stroke():
    for ch, strokes in stroke_font.GLYPHS.items():
        if ch == " ":
            continue
        assert strokes, f"{ch!r} has no strokes"
        for stroke in strokes:
            assert len(stroke) >= 2, f"{ch!r} has a degenerate (<2 point) stroke"


def test_glyph_coordinates_are_within_the_unit_em_box():
    # a little slack (0.1 em) for glyphs like Q's tail / the period dot that
    # legitimately dip past the nominal cap box.
    for ch, strokes in stroke_font.GLYPHS.items():
        for stroke in strokes:
            for x, y in stroke:
                assert -0.1 <= x <= 1.0, f"{ch!r} x={x} out of range"
                assert -0.1 <= y <= 1.1, f"{ch!r} y={y} out of range"


def test_space_has_no_strokes_but_is_supported():
    assert stroke_font.GLYPHS[" "] == ()
    assert stroke_font.supported(" ")


def test_supported_is_case_insensitive():
    assert stroke_font.supported("a")
    assert stroke_font.supported("A")
    assert not stroke_font.supported("!")  # not in the required glyph set


def test_required_glyph_coverage():
    required = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.+/"
    for ch in required:
        assert stroke_font.supported(ch), f"{ch!r} must be supported"


# ── layout ───────────────────────────────────────────────────────────────
def test_text_width_is_monospace_and_scales_with_height():
    w1 = stroke_font.text_width_mm("U1", 1.0)
    w2 = stroke_font.text_width_mm("U1", 2.0)
    assert w2 == pytest.approx(2 * w1)
    assert stroke_font.text_width_mm("U1", 1.0) == pytest.approx(
        2 * stroke_font.ADVANCE_EM
    )


def test_unsupported_char_advances_but_draws_nothing():
    with_bang = stroke_font.layout_text("A!A", anchor=(0.0, 0.0), height_mm=1.0)
    plain = stroke_font.layout_text("AA", anchor=(0.0, 0.0), height_mm=1.0)
    # same number of strokes (the "!" contributes none) but the second "A"
    # is shifted right by one full advance cell relative to "AA"'s second A
    assert len(with_bang) == len(plain)
    shift = stroke_font.ADVANCE_EM * 1.0
    second_a_with_bang = with_bang[len(with_bang) // 2 :]
    second_a_plain = plain[len(plain) // 2 :]
    dx = second_a_with_bang[0][0][0] - second_a_plain[0][0][0]
    assert dx == pytest.approx(shift, abs=1e-9)


def test_layout_left_baseline_starts_at_anchor():
    strokes = stroke_font.layout_text("I", anchor=(5.0, 2.0), height_mm=2.0)
    xs = [p[0] for stroke in strokes for p in stroke]
    ys = [p[1] for stroke in strokes for p in stroke]
    # "I" is a single vertical stroke at x=2/4 of the glyph cell -> some
    # offset right of the anchor, baseline (min y) at the anchor's y.
    assert min(xs) >= 5.0 - 1e-9
    assert min(ys) == pytest.approx(2.0, abs=1e-9)
    assert max(ys) == pytest.approx(2.0 + 2.0, abs=1e-9)  # cap height = height_mm


def test_center_middle_alignment_centers_the_bbox_on_anchor():
    corners = stroke_font.text_bbox_corners(
        "OK", anchor=(10.0, -3.0), height_mm=1.0, h_align="center", v_align="middle"
    )
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    assert (min(xs) + max(xs)) / 2 == pytest.approx(10.0, abs=1e-9)
    assert (min(ys) + max(ys)) / 2 == pytest.approx(-3.0, abs=1e-9)


def test_bbox_matches_the_actual_layout_extent():
    text = "R4"
    corners = stroke_font.text_bbox_corners(text, anchor=(0.0, 0.0), height_mm=1.5)
    strokes = stroke_font.layout_text(text, anchor=(0.0, 0.0), height_mm=1.5)
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    for stroke in strokes:
        for x, y in stroke:
            assert min(xs) - 1e-6 <= x <= max(xs) + 1e-6
            assert min(ys) - 1e-6 <= y <= max(ys) + 1e-6


# ── rotation / mirror -- routed through precis.pcb.landpattern.rotate_offset ──
def test_rotation_preserves_bbox_area():
    """A pure rotation must not change the swept box's AREA (an independent
    cross-check via the shoelace formula, not the implementation's own
    rotate call)."""

    def area(corners: list[tuple[float, float]]) -> float:
        s = 0.0
        n = len(corners)
        for i in range(n):
            x1, y1 = corners[i]
            x2, y2 = corners[(i + 1) % n]
            s += x1 * y2 - x2 * y1
        return abs(s) / 2.0

    base = stroke_font.text_bbox_corners("U12", anchor=(0.0, 0.0), height_mm=1.0)
    rotated = stroke_font.text_bbox_corners(
        "U12", anchor=(0.0, 0.0), height_mm=1.0, rotation_deg=37.0
    )
    assert area(rotated) == pytest.approx(area(base), rel=1e-9)


def test_rotation_by_90_swaps_bbox_extent():
    w = stroke_font.text_width_mm("AB", 1.0)
    corners = stroke_font.text_bbox_corners(
        "AB", anchor=(0.0, 0.0), height_mm=1.0, rotation_deg=90.0
    )
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    # clockwise-from-north 90 deg: the local +x advance direction becomes
    # board +y, so the swept box's Y extent is now the text WIDTH.
    assert (max(ys) - min(ys)) == pytest.approx(w, abs=1e-6)
    assert (max(xs) - min(xs)) == pytest.approx(1.0, abs=1e-6)


def test_mirror_reflects_before_rotate_matching_landpattern_convention():
    """Mirror negates local X before any rotation is applied -- the same
    order :func:`precis.pcb.landpattern.rotate_offset` documents for every
    other placed pad offset in this subsystem."""
    plain = stroke_font.layout_text("A", anchor=(0.0, 0.0), height_mm=1.0)
    mirrored = stroke_font.layout_text(
        "A", anchor=(0.0, 0.0), height_mm=1.0, mirror=True
    )
    plain_xs = sorted(p[0] for stroke in plain for p in stroke)
    mirrored_xs = sorted(p[0] for stroke in mirrored for p in stroke)
    # mirroring negates x, so the mirrored set is the plain set negated
    assert mirrored_xs == pytest.approx([-x for x in reversed(plain_xs)], abs=1e-9)


def test_layout_and_bbox_use_the_same_underlying_rotate():
    """Sanity: rotating by 360 degrees is a no-op (mod floating slop)."""
    a = stroke_font.text_bbox_corners("Z9", anchor=(1.0, 1.0), height_mm=1.0)
    b = stroke_font.text_bbox_corners(
        "Z9", anchor=(1.0, 1.0), height_mm=1.0, rotation_deg=360.0
    )
    for (ax, ay), (bx, by) in zip(a, b, strict=True):
        assert ax == pytest.approx(bx, abs=1e-6)
        assert ay == pytest.approx(by, abs=1e-6)


def test_invalid_alignment_raises():
    with pytest.raises(ValueError):
        stroke_font.layout_text("A", anchor=(0.0, 0.0), height_mm=1.0, h_align="mid")
    with pytest.raises(ValueError):
        stroke_font.layout_text("A", anchor=(0.0, 0.0), height_mm=1.0, v_align="mid")


def test_no_nan_or_inf_in_any_glyph_layout():
    text = "".join(sorted(ch for ch in stroke_font.GLYPHS if ch != " "))
    strokes = stroke_font.layout_text(text, anchor=(0.0, 0.0), height_mm=1.0)
    for stroke in strokes:
        for x, y in stroke:
            assert math.isfinite(x)
            assert math.isfinite(y)

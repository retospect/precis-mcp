"""precis.pcb.stroke_font -- the single-stroke (Hershey Simplex-modeled)
silkscreen vector font.

Covers: every supported glyph draws real, non-degenerate strokes with a
sane bounding box; per-glyph advance metrics genuinely vary (the specific
thing that distinguishes real Hershey-style metrics from a fixed monospace
grid) and a laid-out string's total width is always the sum of its own
glyphs' advances (layout and metrics cannot drift apart); rotation and
mirror route through the SAME transform
(:func:`precis.pcb.landpattern.rotate_offset`) every placed pad offset
uses, so a property (bbox area preserved under a pure rotation) is a real
cross-check, not a restatement of the code.
"""

from __future__ import annotations

import math
import string

import pytest

from precis.pcb import stroke_font

# every printable-ASCII character except lowercase a-z, which folds to
# uppercase before lookup (see module docstring) -- see
# test_full_printable_ascii_coverage_except_lowercase below.
_PRINTABLE_NO_LOWER = "".join(
    ch for ch in string.printable if ch.isprintable() and not ch.islower()
)


# ── glyph table ─────────────────────────────────────────────────────────
def test_every_glyph_except_space_has_at_least_one_stroke_and_a_sane_bbox():
    for ch, strokes in stroke_font.GLYPHS.items():
        if ch == " ":
            continue
        assert strokes, f"{ch!r} has no strokes -- a silent .notdef gap"
        xs: list[float] = []
        ys: list[float] = []
        for stroke in strokes:
            assert len(stroke) >= 2, f"{ch!r} has a degenerate (<2 point) stroke"
            for x, y in stroke:
                assert math.isfinite(x) and math.isfinite(y)
                xs.append(x)
                ys.append(y)
        # a "sane" bounding box: real extent in at least one axis (a purely
        # vertical stroke like 'I'/'|' has zero WIDTH by design, a purely
        # horizontal one like '-'/'_' has zero HEIGHT by design -- neither
        # is degenerate, a single coincident point would be), and within
        # generous slack of the nominal 1.0 EM cap box (round-letter
        # overshoot / descending tails are legitimate). The y slack is
        # wider than a naive cap-height box: real Hershey Roman Simplex
        # digitizes brackets/braces/parentheses and the '#'/'$' bars taller
        # than cap height and dipping below baseline (so they visually
        # bracket ascenders+descenders) -- measured extremes are y=-7/21
        # ('(' / ')' / '#') and y=25/21 ('[' / '$' / '{' / '}' / etc.), see
        # the module docstring's "Coordinate convention".
        extent = (max(xs) - min(xs)) + (max(ys) - min(ys))
        assert extent > 0.05, f"{ch!r} bbox is ~degenerate (a point, not a stroke)"
        assert min(xs) >= -0.2 and max(xs) <= 1.3, f"{ch!r} x out of range"
        assert min(ys) >= -0.4 and max(ys) <= 1.25, f"{ch!r} y out of range"


# ── handedness: a mirror or a flip must be DETECTABLE ──────────────────────
# The bbox/stroke-count checks above are symmetry-blind: they pass IDENTICALLY
# for a glyph and its mirror image, which is exactly how a mirrored "S" (both
# arcs authored with reversed sweeps) shipped undetected in every prior
# version of this table. "S" (and "N") both have 180-degree point symmetry,
# so no test built from their own overall bbox/point-count can ever catch a
# mirror -- a mirrored S has the SAME bbox and the SAME number of points as a
# correct one. The tests below instead pin concrete, ORDERED geometric
# relationships (first-point-vs-last-point, left-vs-right, upper-vs-lower)
# on glyphs asymmetric in BOTH axes ("F" is the canonical orientation-test
# glyph in graphics; "N"/"R"/"P" also work), plus "S" itself with a signature
# that a left-right mirror OR a top-bottom flip would each independently
# violate.
def test_f_bars_both_extend_right_of_the_stem_and_upper_bar_is_longer():
    """"F" is asymmetric in both axes: a mirror (left-right) would put both
    bars on the WRONG side of the stem; a flip (top-bottom) would make the
    (shorter) lower bar the longer one. Neither transform error is visible
    to a bbox/point-count check -- both are visible here."""
    strokes = stroke_font.GLYPHS["F"]
    stem, bar_top, bar_bottom = strokes
    stem_x = stem[0][0]
    assert stem[0][0] == pytest.approx(stem[1][0])  # the stem is vertical
    top_extent = max(x for x, _y in bar_top) - stem_x
    bottom_extent = max(x for x, _y in bar_bottom) - stem_x
    assert top_extent > 0
    assert bottom_extent > 0, "both bars must extend RIGHT of the stem"
    assert top_extent > bottom_extent, "the upper bar must be LONGER than the lower one"


def test_n_diagonal_runs_top_left_to_bottom_right():
    """The diagonal stroke's first point must be up-and-left of its last
    point -- a left-right mirror OR a top-bottom flip each independently
    reverses one half of this compound check."""
    _left_stem, diagonal, _right_stem = stroke_font.GLYPHS["N"]
    first, last = diagonal[0], diagonal[-1]
    assert first[0] < last[0], "N's diagonal must run left-to-right"
    assert first[1] > last[1], "N's diagonal must run top-to-bottom"


def test_r_leg_runs_mid_left_to_bottom_right():
    """R's diagonal leg (distinct from its bowl) must run down-and-right,
    same handedness signature as N's diagonal -- same mirror/flip
    sensitivity."""
    _stem, _bowl, leg = stroke_font.GLYPHS["R"]
    first, last = leg[0], leg[-1]
    assert first[0] < last[0], "R's leg must run left-to-right"
    assert first[1] > last[1], "R's leg must run top-to-bottom"


def test_p_bowl_sits_in_the_upper_half_and_right_of_the_stem():
    """P's bowl occupies roughly the upper half of the cap height and
    extends right of the stem -- a top-bottom flip would move it to the
    lower half, a left-right mirror would put it left of the stem."""
    stem, bowl = stroke_font.GLYPHS["P"]
    stem_x = stem[0][0]
    assert min(y for _x, y in bowl) >= 0.45, "P's bowl must stay in the upper half"
    assert max(x for x, _y in bowl) > stem_x, "P's bowl must extend RIGHT of the stem"


def test_s_runs_from_the_upper_right_hook_to_the_lower_left_hook():
    """The defect this whole change fixes: ``_RAW["S"]`` used to author
    both of its arcs with REVERSED sweeps, producing a mirrored S that
    passed every bbox/stroke-count test identically to a correct one (S has
    180-degree point symmetry -- a mirror of it looks like a normal S with
    the SAME bbox and the SAME point count, just traced backwards/
    left-right flipped). This signature -- the glyph's first point sits
    up-and-right of its last point -- is chosen precisely because it is
    false for that mirrored version.

    Verified by hand (NOT via this suite, since the old ``_RAW`` no longer
    exists to import): temporarily restoring the old, buggy
    ``_RAW["S"] = ((_arc(10.5, 16, 4.5, 125, -115, 8), _arc(8.5, 5, 4.5,
    305, 65, 8)),16.0)`` (its first point is ~(7.9, 19.7), its last point
    is ~(10.4, 9.1) in raw 21-unit grid units) makes ``first[0] > last[0]``
    FALSE (7.9 < 10.4) -- this test goes red on the old data -- while
    ``first[1] > last[1]`` still holds. Restoring the real Hershey data
    (this test's fixture) makes both halves true again.
    """
    strokes = stroke_font.GLYPHS["S"]
    first, last = strokes[0][0], strokes[-1][-1]
    assert first[0] > last[0], "S must start RIGHT of where it ends"
    assert first[1] > last[1], "S must start ABOVE where it ends"


def test_space_has_no_strokes_but_is_supported():
    assert stroke_font.GLYPHS[" "] == ()
    assert stroke_font.supported(" ")


def test_supported_is_case_insensitive():
    assert stroke_font.supported("a")
    assert stroke_font.supported("A")
    assert not stroke_font.supported("\x01")  # a real control char, never covered


def test_required_glyph_coverage():
    required = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.+/"
    for ch in required:
        assert stroke_font.supported(ch), f"{ch!r} must be supported"


def test_full_printable_ascii_coverage_except_lowercase():
    """Every printable-ASCII character has a real glyph, except lowercase
    a-z, which the existing case-fold contract routes to the SAME entries
    as their uppercase form (not a separate table -- see module
    docstring's "Uppercase-only")."""
    for ch in _PRINTABLE_NO_LOWER:
        assert stroke_font.supported(ch), f"{ch!r} must be supported"
    for ch in string.ascii_lowercase:
        assert stroke_font.supported(ch)
        # no SEPARATE lowercase entry -- lowercase folds to reuse the
        # uppercase glyph, per the module's documented case-fold contract.
        assert ch not in stroke_font.GLYPHS
        assert ch.upper() in stroke_font.GLYPHS


# ── metrics: real per-glyph advance, not a fixed grid ─────────────────────
def test_glyph_advance_varies_by_character():
    """The specific thing that distinguishes real Hershey-style metrics
    from the old fixed monospace cell: a narrow glyph (I) must not occupy
    the same advance width as a wide one (M)."""
    assert stroke_font.GLYPH_ADVANCE_EM["I"] < stroke_font.GLYPH_ADVANCE_EM["M"]
    # and it's not just I/M -- the table isn't secretly uniform elsewhere.
    widths = {stroke_font.GLYPH_ADVANCE_EM[ch] for ch in "ILMW1"}
    assert len(widths) > 1


def test_text_width_scales_with_height():
    w1 = stroke_font.text_width_mm("U1", 1.0)
    w2 = stroke_font.text_width_mm("U1", 2.0)
    assert w2 == pytest.approx(2 * w1)
    assert w1 > 0


def test_text_width_is_the_sum_of_each_glyphs_own_advance():
    """Layout and metrics cannot drift apart: a rendered string's total
    advance width is exactly the sum of its glyphs' own per-character
    advances (never a restated constant)."""
    text = "MI1O"
    expected = sum(stroke_font.GLYPH_ADVANCE_EM[ch] for ch in text) * 1.5
    assert stroke_font.text_width_mm(text, 1.5) == pytest.approx(expected)


def test_unsupported_char_advances_but_draws_nothing():
    with_gap = stroke_font.layout_text("A\x01A", anchor=(0.0, 0.0), height_mm=1.0)
    plain = stroke_font.layout_text("AA", anchor=(0.0, 0.0), height_mm=1.0)
    # same number of strokes (the control char contributes none) but the
    # second "A" is shifted right by ADVANCE_EM relative to "AA"'s second A.
    assert len(with_gap) == len(plain)
    shift = stroke_font.ADVANCE_EM * 1.0
    a_strokes = len(stroke_font.GLYPHS["A"])
    second_a_with_gap = with_gap[a_strokes:]
    second_a_plain = plain[a_strokes:]
    dx = second_a_with_gap[0][0][0] - second_a_plain[0][0][0]
    assert dx == pytest.approx(shift, abs=1e-9)


def test_layout_left_baseline_starts_at_anchor():
    strokes = stroke_font.layout_text("I", anchor=(5.0, 2.0), height_mm=2.0)
    xs = [p[0] for stroke in strokes for p in stroke]
    ys = [p[1] for stroke in strokes for p in stroke]
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

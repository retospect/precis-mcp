"""precis.pcb.silk -- the silkscreen builder. No DB (a plain-dict graph via
precis.pcb.ir.from_graph, the same fixture style tests/test_pcb_realize.py
uses).

Covers: courtyard/pin-1/refdes geometry off a real IR, refdes suppression
+ relocation when the default placement would overlap a pad, the "silk
never overlaps a pad" invariant (checked with an INDEPENDENT geometry
routine, not the builder's own private overlap predicate), and the
gerber.py round-trip (real D01/D02, readable back via gerber_view).
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from precis.pcb import DEFAULT_STACKUP, gerber, gerber_view, realize, stroke_font
from precis.pcb.ir import from_graph
from precis.pcb.planes import plane_pours, point_in_pour
from precis.pcb.silk import (
    COURTYARD_MARGIN_MM,
    FIDUCIAL_COPPER_DIA_MM,
    FIDUCIAL_MARGIN_MM,
    FIDUCIAL_MASK_DIA_MM,
    SN_LABEL,
    SN_LABEL_HEIGHT_MM,
    build_fiducials,
    build_silk,
    build_sn_patch,
    build_title_block,
    obstacle_from_bbox,
)


def _graph(
    refdes: str, n_pins: int, *, x: float = 0.0, y: float = 0.0, rot: float = 0.0
):
    """A single placed instance with ``n_pins`` distinct pins (one
    single-member net per pin — enough for ``from_graph`` to synthesize a
    real land pattern via ``precis.pcb.landpattern.offsets_for``, no
    segments needed for silk itself)."""
    return {
        "instances": [{"refdes": refdes, "x": x, "y": y, "rot": rot}],
        "nets": [
            {
                "name": f"N{i}",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": refdes, "pin": str(i + 1)}],
            }
            for i in range(n_pins)
        ],
    }


def _multi(*graphs: dict) -> dict:
    instances: list = []
    nets: list = []
    for g in graphs:
        instances += g["instances"]
        nets += g["nets"]
    return {"instances": instances, "nets": nets}


# ── independent overlap checker (deliberately NOT silk.py's own helper) ──
def _segment_hits_pad(a, b, pad, *, n=25) -> bool:
    """Sample the raw centreline (no stroke-width inflation) and test each
    sample point against the pad's own boundary -- a strictly SIMPLER,
    independently-written check than the builder's SAT-based one. If this
    ever trips, the builder's inflated/SAT check has a real bug, not a
    disagreement over margins."""
    shape = pad.get("shape", "circle")
    for k in range(n + 1):
        t = k / n
        px = a[0] + (b[0] - a[0]) * t
        py = a[1] + (b[1] - a[1]) * t
        if shape == "circle":
            if math.hypot(px - pad["x"], py - pad["y"]) < pad["w"] / 2.0:
                return True
        else:
            w, h = pad["w"], pad.get("h", pad["w"])
            if (
                pad["x"] - w / 2 < px < pad["x"] + w / 2
                and pad["y"] - h / 2 < py < pad["y"] + h / 2
            ):
                return True
    return False


def _draws_hit_any_pad(draws: list[dict], pads: list[dict]) -> bool:
    for draw in draws:
        for seg in draw["segments"]:
            for pad in pads:
                if _segment_hits_pad(seg["start"], seg["end"], pad):
                    return True
    return False


def _point_to_segment_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _segments_hit(seg_a, seg_b, stroke_width_mm: float, *, n=25) -> bool:
    """Independently-written (no SAT, no segment-box) stroke-vs-stroke
    overlap check: sample one segment's centreline finely and test each
    sample point's distance to the OTHER segment against the combined
    half-widths (both strokes share ``stroke_width_mm`` here, so the
    threshold is the full width) -- a strictly simpler check than the
    builder's own SAT-based one."""
    a, b = seg_a
    for k in range(n + 1):
        t = k / n
        p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        if _point_to_segment_dist(p, *seg_b) < stroke_width_mm:
            return True
    return False


def _refdes_hits_own_silk(draws: list[dict], stroke_width_mm: float) -> bool:
    """Does any refdes draw overlap the SAME instance's own outline/pin-1
    draw? Groups by ``refdes`` first (a label overlapping a DIFFERENT
    part's silk is a separate, pad/via-shaped hazard, not this one)."""
    by_refdes: dict[str, list[dict]] = {}
    for d in draws:
        by_refdes.setdefault(d["refdes"], []).append(d)
    for group in by_refdes.values():
        refdes_segs = [
            seg for d in group if d["role"] == "refdes" for seg in d["segments"]
        ]
        own_segs = [
            seg for d in group if d["role"] != "refdes" for seg in d["segments"]
        ]
        for rseg in refdes_segs:
            for oseg in own_segs:
                if _segments_hit(
                    (rseg["start"], rseg["end"]),
                    (oseg["start"], oseg["end"]),
                    stroke_width_mm,
                ):
                    return True
    return False


# ── basic shape / no-overlap-with-nothing case ────────────────────────────
def test_build_silk_places_refdes_courtyard_and_pin1_for_a_normal_part():
    ir = from_graph(_graph("U1", 16, x=10.0, y=5.0), stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[])
    assert not result.dropped
    assert not result.relocated
    top = result.draws["top"]
    assert top  # something was drawn
    roles = {d["role"] for d in top}
    assert roles == {"outline", "pin1", "refdes"}
    for d in top:
        assert d["refdes"] == "U1"
        assert d["source"] == "synthesized"


def test_unplaced_instance_gets_no_silk():
    graph = {
        "instances": [{"refdes": "U1"}],  # no x/y
        "nets": [
            {
                "name": "N0",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U1", "pin": "1"}],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[])
    assert result.draws == {"top": [], "bottom": []}
    assert not result.dropped
    assert not result.relocated


def test_pinless_instance_gets_no_courtyard_or_pin1_but_still_a_refdes():
    graph = {
        "instances": [{"refdes": "MH1", "x": 0.0, "y": 0.0}],
        "nets": [],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[])
    roles = {d["role"] for d in result.draws["top"]}
    assert roles == {"refdes"}  # no pins -> no courtyard, no pin-1 marker


def test_side_routes_to_the_bottom_bucket():
    ir = from_graph(_graph("C1", 2, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[], instance_sides={"C1": "bottom"})
    assert result.draws["top"] == []
    assert result.draws["bottom"]


# ── refdes lands within its part's extent (the default, centered case) ───
def test_refdes_text_lands_within_the_part_extent_when_unobstructed():
    ir = from_graph(_graph("U2", 16, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[])
    assert not result.relocated and not result.dropped
    refdes_draws = [d for d in result.draws["top"] if d["role"] == "refdes"]
    assert refdes_draws
    outline = next(d for d in result.draws["top"] if d["role"] == "outline")
    reach = max(
        abs(p) for seg in outline["segments"] for p in seg["start"] + seg["end"]
    )
    for d in refdes_draws:
        for seg in d["segments"]:
            for x, y in (seg["start"], seg["end"]):
                assert abs(x) <= reach + 1e-6
                assert abs(y) <= reach + 1e-6


# ── suppression: relocate, then drop ──────────────────────────────────────
def test_refdes_relocates_when_the_default_center_spot_overlaps_a_pad():
    ir = from_graph(_graph("U3", 16, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    # a big pad sitting right on the part's centre -- the default candidate
    # (centered on the part) cannot possibly clear it.
    pads = [{"shape": "circle", "x": 0.0, "y": 0.0, "w": 3.0, "net": "FOREIGN"}]
    result = build_silk(ir, pads=pads)
    assert not result.dropped
    assert result.relocated
    assert any("U3" in msg for msg in result.relocated)
    refdes_draws = [
        d for d in result.draws["top"] if d["role"] == "refdes" and d["refdes"] == "U3"
    ]
    assert refdes_draws
    assert not _draws_hit_any_pad(refdes_draws, pads)


def test_refdes_dropped_when_every_candidate_overlaps_a_pad():
    ir = from_graph(_graph("U4", 16, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    # a pad so large it blankets the part and every relocation candidate.
    pads = [{"shape": "circle", "x": 0.0, "y": 0.0, "w": 40.0, "net": "FOREIGN"}]
    result = build_silk(ir, pads=pads)
    assert any("U4" in msg and "dropped" in msg for msg in result.dropped)
    refdes_draws = [
        d for d in result.draws["top"] if d["role"] == "refdes" and d["refdes"] == "U4"
    ]
    assert refdes_draws == []
    # and nothing else for U4 (courtyard/pin1) survived either -- consistent
    # with "silk never overlaps a pad" below, not a special case for text.
    other_draws = [d for d in result.draws["top"] if d["refdes"] == "U4"]
    assert not _draws_hit_any_pad(other_draws, pads)


# ── silk never overlaps a pad (property, independent checker) ────────────
@pytest.mark.parametrize("pad_w", [0.5, 2.0, 6.0, 15.0])
def test_silk_never_overlaps_a_pad_at_various_sizes(pad_w):
    ir = from_graph(
        _multi(_graph("U5", 8, x=0.0, y=0.0), _graph("C5", 2, x=6.0, y=0.0)),
        stackup=DEFAULT_STACKUP,
    )
    pads = [
        {"shape": "circle", "x": 0.0, "y": 0.0, "w": pad_w, "net": "N"},
        {"shape": "rect", "x": 6.0, "y": 0.0, "w": pad_w, "h": pad_w / 2, "net": "M"},
    ]
    result = build_silk(ir, pads=pads)
    assert not _draws_hit_any_pad(result.draws["top"], pads)
    assert not _draws_hit_any_pad(result.draws["bottom"], pads)


def test_silk_never_overlaps_a_pad_with_rotation_and_mirror():
    ir = from_graph(_graph("U6", 16, x=2.0, y=-3.0, rot=53.0), stackup=DEFAULT_STACKUP)
    pads = [{"shape": "circle", "x": 2.0, "y": -3.0, "w": 1.5, "net": "N"}]
    result = build_silk(ir, pads=pads, instance_sides={"U6": "bottom"})
    assert not _draws_hit_any_pad(result.draws["bottom"], pads)


# ── real pads_for_ir input (the synthesized-bound fallback the handler
# falls back to when the footprint cache is empty) ────────────────────────
def test_silk_clears_pads_for_irs_own_synthesized_pads():
    graph = _graph("U7", 8, x=0.0, y=0.0)
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    pads = realize.pads_for_ir(ir, [layer["name"] for layer in DEFAULT_STACKUP])
    assert pads  # the fixture actually produced pad geometry
    result = build_silk(ir, pads=pads)
    assert not result.dropped
    assert not _draws_hit_any_pad(result.draws["top"], pads)


# ── vias: same "a fab scrapes silk off exposed copper" hazard as a pad ───
def _via_as_pad(via: dict) -> dict:
    """The SAME reshape ``silk.py``'s own ``_via_pad`` does, written
    independently here so the property tests below check the builder's
    behaviour against a geometry routine that isn't the builder's own."""
    return {"shape": "circle", "x": via["x"], "y": via["y"], "w": via["dia_mm"]}


def test_refdes_relocates_or_drops_when_a_via_sits_where_it_would_land():
    ir = from_graph(_graph("U9", 16, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    # a through via right on the part's centre -- the default candidate
    # (centered on the part) cannot possibly clear it.
    via = {"x": 0.0, "y": 0.0, "dia_mm": 3.0, "drill_mm": 0.3, "span": ["F.Cu", "B.Cu"]}
    result = build_silk(ir, pads=[], vias=[via])
    assert result.dropped or result.relocated
    assert any("U9" in msg for msg in (*result.dropped, *result.relocated))
    refdes_draws = [
        d for d in result.draws["top"] if d["role"] == "refdes" and d["refdes"] == "U9"
    ]
    assert not _draws_hit_any_pad(refdes_draws, [_via_as_pad(via)])


@pytest.mark.parametrize("dia_mm", [0.6, 1.5, 3.0])
def test_silk_never_overlaps_a_through_via(dia_mm):
    ir = from_graph(
        _multi(_graph("U10", 8, x=0.0, y=0.0), _graph("C10", 2, x=6.0, y=0.0)),
        stackup=DEFAULT_STACKUP,
    )
    vias = [
        {
            "x": 0.0,
            "y": 0.0,
            "dia_mm": dia_mm,
            "drill_mm": 0.3,
            "span": ["F.Cu", "B.Cu"],
        },
        {
            "x": 6.0,
            "y": 0.0,
            "dia_mm": dia_mm,
            "drill_mm": 0.3,
            "span": ["F.Cu", "B.Cu"],
        },
    ]
    result = build_silk(ir, pads=[], vias=vias)
    via_pads = [_via_as_pad(v) for v in vias]
    assert not _draws_hit_any_pad(result.draws["top"], via_pads)
    assert not _draws_hit_any_pad(result.draws["bottom"], via_pads)


def test_via_blocks_silk_only_on_the_side_its_barrel_reaches():
    """A blind via reaching only F.Cu (index 0 -- the top copper layer of
    ``DEFAULT_STACKUP``) is exposed copper on the top and must block top
    silk over it, but it never reaches B.Cu (the bottom) and must NOT block
    bottom silk -- unlike a through via, which reaches (and must block)
    both."""
    graph = _multi(
        _graph("UT", 16, x=0.0, y=0.0),  # top-side instance
        _graph("UB", 16, x=20.0, y=0.0),  # bottom-side instance
    )
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    # Same span (reaches only the TOP layer, F.Cu -> In1.Cu) sitting under
    # each instance's default label spot.
    via_under_top = {
        "x": 0.0,
        "y": 0.0,
        "dia_mm": 3.0,
        "drill_mm": 0.3,
        "span": ["F.Cu", "In1.Cu"],
    }
    via_under_bottom = {**via_under_top, "x": 20.0, "y": 0.0}
    result = build_silk(
        ir,
        pads=[],
        vias=[via_under_top, via_under_bottom],
        instance_sides={"UT": "top", "UB": "bottom"},
    )
    messages = (*result.dropped, *result.relocated)
    assert any("UT" in msg for msg in messages)
    assert not any("UB" in msg for msg in messages)
    # And the bottom silk really did land where the (non-reaching) via is --
    # not just "no message", but geometry that a blocked instance couldn't
    # have produced.
    bottom_refdes = [
        d
        for d in result.draws["bottom"]
        if d["role"] == "refdes" and d["refdes"] == "UB"
    ]
    assert bottom_refdes


# ── refdes never overlaps its OWN silk (outline / pin-1) ──────────────────
def test_refdes_never_overlaps_its_own_courtyard_or_pin1_for_a_normal_part():
    ir = from_graph(
        _multi(_graph("U5", 8, x=0.0, y=0.0), _graph("C5", 2, x=6.0, y=0.0)),
        stackup=DEFAULT_STACKUP,
    )
    result = build_silk(ir, pads=[])
    assert not _refdes_hits_own_silk(result.draws["top"], gerber.DEFAULT_SILK_WIDTH_MM)


def test_refdes_relocates_to_clear_its_own_courtyard_when_the_label_outgrows_the_part():
    """A small part's courtyard can be smaller than a legible label at a
    normal ``height_mm`` (the task brief's 0402 case) -- the centered
    candidate (index 0) then collides with the part's OWN outline, not a
    foreign pad/via, and the ladder must still walk to a clear spot rather
    than drawing overlapping ink."""
    ir = from_graph(_graph("U1", 2, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[], height_mm=5.0)
    assert not result.dropped
    assert result.relocated
    assert any("U1" in msg for msg in result.relocated)
    assert not _refdes_hits_own_silk(result.draws["top"], gerber.DEFAULT_SILK_WIDTH_MM)


def test_refdes_relocates_or_drops_rather_than_overlap_its_own_pin1_marker():
    """Same property, but exercised at a variety of rotations/mirrors so
    it isn't just a top/0-degree coincidence -- the self-overlap check
    routes through the SAME candidate ladder as the pad/via check, so it
    must hold under rotation and mirror too."""
    for rot in (0.0, 37.0, 90.0, 180.0):
        for side in ("top", "bottom"):
            ir = from_graph(
                _graph("U1", 4, x=0.0, y=0.0, rot=rot), stackup=DEFAULT_STACKUP
            )
            result = build_silk(ir, pads=[], height_mm=4.0, instance_sides={"U1": side})
            for bucket in result.draws.values():
                assert not _refdes_hits_own_silk(bucket, gerber.DEFAULT_SILK_WIDTH_MM)


# ── cross-part avoidance (avoidance is GLOBAL, not per-instance) ─────────
def _any_cross_part_silk_overlap(draws: list[dict], stroke_width_mm: float) -> bool:
    """Do any two DIFFERENT parts' committed silk overlap? Independently
    written (reuses only the low-level ``_segments_hit`` sampler, not any
    of ``silk.py``'s own obstacle machinery) -- the property the fix in
    this task exists to establish: a dense cluster's silk must not
    collide across parts, not just within one part's own courtyard/tick/
    label."""
    by_refdes: dict[str, list[dict]] = {}
    for d in draws:
        by_refdes.setdefault(d["refdes"], []).append(d)
    refdes_list = list(by_refdes)
    for i in range(len(refdes_list)):
        segs_i = [seg for d in by_refdes[refdes_list[i]] for seg in d["segments"]]
        for j in range(i + 1, len(refdes_list)):
            segs_j = [seg for d in by_refdes[refdes_list[j]] for seg in d["segments"]]
            for a in segs_i:
                for b in segs_j:
                    if _segments_hit(
                        (a["start"], a["end"]), (b["start"], b["end"]), stroke_width_mm
                    ):
                        return True
    return False


def test_a_dense_cluster_never_overlaps_silk_across_different_parts():
    """Before this fix, ``own_silk`` was per-instance and reset every
    loop iteration -- a part's silk was checked against its OWN
    courtyard/tick and nothing else, so a dense cluster produced
    overlapping, illegible ink between NEIGHBOURING parts. Four small
    parts packed 1mm apart (well inside each other's default courtyard +
    label reach) forces exactly that contention; the fix folds every
    committed courtyard/tick/label into the shared obstacle set every
    later part also reads, so no two parts' ink may touch."""
    ir = from_graph(
        _multi(
            _graph("C1", 2, x=0.0, y=0.0),
            _graph("C2", 2, x=1.0, y=0.0),
            _graph("C3", 2, x=2.0, y=0.0),
            _graph("C4", 2, x=3.0, y=0.0),
        ),
        stackup=DEFAULT_STACKUP,
    )
    result = build_silk(ir, pads=[])
    assert result.draws["top"]  # something was actually drawn
    assert not _any_cross_part_silk_overlap(
        result.draws["top"], gerber.DEFAULT_SILK_WIDTH_MM
    )


def test_processing_order_is_natural_refdes_order_not_array_order():
    """``ir``'s instance array lists C14 before C1 -- natural-refdes-order
    processing (module docstring) must still let C1 claim the contested
    spot first: C1's refdes label lands at its default (candidate 0,
    centered) position, never relocated, while C14 -- processed after,
    once C1's silk is already committed -- is the one that has to move
    or drop."""
    ir = from_graph(
        _multi(
            _graph("C14", 8, x=0.7, y=0.0),  # listed FIRST in the array
            _graph("C1", 8, x=0.0, y=0.0),  # listed SECOND, but sorts first
        ),
        stackup=DEFAULT_STACKUP,
    )
    result = build_silk(ir, pads=[])
    messages = (*result.dropped, *result.relocated)
    assert not any(msg.startswith("C1:") for msg in messages)
    assert any(msg.startswith("C14:") for msg in messages)


# ── pin-1 tick never survives its own courtyard being dropped ────────────
def test_pin1_tick_is_skipped_when_its_own_courtyard_is_dropped():
    """A tick is a corner-cut of the courtyard outline -- meaningless, and
    unreadable as stray ink, without it (module docstring's "A pin-1 tick
    never survives alone" decision). A pad placed exactly on the part's
    courtyard forces the courtyard to drop; the tick must not survive it,
    and the suppression must be reported."""
    ir = from_graph(_graph("U1", 4, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    # A big pad blanketing the whole courtyard (but not necessarily the
    # relocated refdes candidates further out).
    pads = [{"shape": "circle", "x": 0.0, "y": 0.0, "w": 20.0, "net": "FOREIGN"}]
    result = build_silk(ir, pads=pads)
    roles = {d["role"] for d in result.draws["top"] if d["refdes"] == "U1"}
    assert "outline" not in roles  # sanity: the courtyard really did drop
    assert "pin1" not in roles  # and the tick did not survive it
    assert any("U1" in msg and "pin-1 marker skipped" in msg for msg in result.dropped)


# ── courtyard margin: the boundary must clear the pad edge, not touch it ──
def test_courtyard_clears_its_own_pad_at_the_old_bugs_exact_tangent_point():
    """Reproduces the C2 self-collision defect (task brief, 2026-08-29):
    before :data:`COURTYARD_MARGIN_MM` existed, ``_courtyard_reach_mm``
    was exactly ``instance_pad_radius + half_pad`` -- the boundary landed
    ON the pad's own outer edge, zero clearance, so ANY nonzero
    ``stroke_width_mm`` inked over that pad and the courtyard was
    (wrongly) dropped as if a NEIGHBOUR had encroached on it, when it had
    only ever touched its own copper.

    Pin A alone drives BOTH terms of the old formula at once -- it has
    both the larger offset (``instance_pad_radius``) and the larger pad
    (``half_pad``) -- so ``R + S`` (2.0 + 0.7) lands EXACTLY on pin A's
    own pad edge at x=2.7, the precise shape that used to trip every
    2-pad part on the reference board. Pin B is close-in, small, and
    off-diagonal purely to break the fixture's symmetry: a square
    courtyard around a symmetric pin pair cannot distinguish a correct
    x/y or pin-index computation from a swapped one (this module's own
    recent S/N label bug was exactly this class of blind spot)."""
    ir = from_graph(_graph("C2", 2, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    # Pin A: far offset, wide pad -- the sole driver of both
    # `instance_pad_radius` (reach 2.0) and `half_pad` (0.7, from its own
    # w=1.4 > h=0.6) at once, so the OLD unmargined formula's boundary
    # sat exactly on ITS pad edge (2.0 + 0.7 == 2.7).
    ir.pin_dx[0], ir.pin_dy[0] = 2.0, 0.0
    ir.pin_w[0], ir.pin_h[0] = 1.4, 0.6
    # Pin B: close-in, small, off-axis -- never the max of anything here,
    # present only to make x<->y and pin-index mixups visible.
    ir.pin_dx[1], ir.pin_dy[1] = 0.3, -0.9
    ir.pin_w[1], ir.pin_h[1] = 0.4, 0.4
    pads = [
        {"shape": "rect", "x": 2.0, "y": 0.0, "w": 1.4, "h": 0.6, "net": "A"},
        {"shape": "rect", "x": 0.3, "y": -0.9, "w": 0.4, "h": 0.4, "net": "B"},
    ]
    result = build_silk(ir, pads=pads)
    courtyard_row = next(
        c for c in result.census if c.refdes == "C2" and c.kind == "courtyard"
    )
    assert courtyard_row.outcome == "placed"
    assert courtyard_row.reason is None
    assert "outline" in {d["role"] for d in result.draws["top"] if d["refdes"] == "C2"}


def test_courtyard_margin_still_drops_for_a_genuine_foreign_encroachment():
    """The margin fix must not become "never drops" -- a large-enough
    fudge could trivially satisfy the tangent test above while quietly
    disabling the one property that makes a courtyard worth drawing at
    all (task brief). This foreign pad is placed to overlap ONLY the
    NEW, margined boundary -- it sits just outside the OLD (zero-margin)
    reach and well inside the new one -- so the assertion below is
    sensitive to the actual :data:`COURTYARD_MARGIN_MM` value, not just
    to "is anything ever dropped": drop the margin back to 0.0 and this
    same pad, which genuinely violates the IPC-7351 nominal clearance,
    goes undetected again.

    Instance A itself is built the SAME asymmetric way as the tangent
    test above but with slack, not tangency -- pin A drives the reach
    (offset 3.0) while pin B (offset 0.5, pad half 0.5) drives
    ``half_pad`` -- so A's OWN pad sits well inside its own courtyard at
    ANY margin from 0.0 up, and a drop here can only be explained by the
    foreign pad, never by A's own copper (isolating the property this
    test exists to check from the self-collision defect the other test
    covers)."""
    ir = from_graph(_graph("A1", 2, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    ir.pin_dx[0], ir.pin_dy[0] = 3.0, 0.0  # drives instance_pad_radius (3.0)
    ir.pin_w[0], ir.pin_h[0] = 0.1, 0.1
    ir.pin_dx[1], ir.pin_dy[1] = 0.5, 0.0  # drives half_pad (0.5)
    ir.pin_w[1], ir.pin_h[1] = 1.0, 1.0
    reach_old = 3.0 + 0.5  # the pre-fix, zero-margin formula: 3.5
    reach_new = reach_old + COURTYARD_MARGIN_MM  # 3.75
    own_pads = [
        {"shape": "rect", "x": 3.0, "y": 0.0, "w": 0.1, "h": 0.1, "net": "A"},
        {"shape": "rect", "x": 0.5, "y": 0.0, "w": 1.0, "h": 1.0, "net": "B"},
    ]
    # A small foreign pad straddling the NEW boundary exactly (centred on
    # reach_new) but with its near edge well past the OLD boundary plus
    # the stroke's own half-width -- outside the old formula's reach
    # entirely, inside the new one.
    foreign_pad = {
        "shape": "rect",
        "x": reach_new,
        "y": 0.0,
        "w": 0.06,
        "h": 0.06,
        "net": "FOREIGN",
    }
    result = build_silk(ir, pads=[*own_pads, foreign_pad])
    courtyard_row = next(
        c for c in result.census if c.refdes == "A1" and c.kind == "courtyard"
    )
    assert courtyard_row.outcome == "dropped"
    assert courtyard_row.reason is not None
    assert "A1" in " ".join(result.dropped)


# ── gerber round-trip ──────────────────────────────────────────────────
def test_silk_round_trips_through_gerber_and_parses_back():
    ir = from_graph(_graph("U8", 16, x=1.0, y=1.0), stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[])
    model = {
        "layers": [layer["name"] for layer in DEFAULT_STACKUP],
        "silkscreen": result.draws,
    }
    top_gerber = gerber.silkscreen_gerber(model, "top")
    assert "D01*" in top_gerber
    assert "D02*" in top_gerber
    assert top_gerber.strip().endswith("M02*")

    art = gerber_view.parse_gerber(top_gerber)
    assert art.strokes  # real geometry came back, not an empty legend file
    total_points = sum(len(s.points) for s in art.strokes)
    assert total_points > 4  # more than one trivial 2-point stroke

    # every parsed stroke width matches the draw width we asked for
    for s in art.strokes:
        assert s.width == pytest.approx(gerber.DEFAULT_SILK_WIDTH_MM)


# ── refdes text reads from ONE side, at ANY part rotation ─────────────────
def _refdes_segments(result):
    return [
        seg
        for d in result.draws["top"]
        if d["role"] == "refdes"
        for seg in d["segments"]
    ]


def _normalized_segments(segs):
    """Segment endpoints, shifted so the whole set's own min corner sits at
    the origin -- comparing two of these strips out any common translation
    but preserves rotation/reflection, so two sets that are only a
    translation apart normalize to the SAME sorted list, and two that
    differ by a rotation or a point-reflection do not."""
    pts = [p for seg in segs for p in (seg["start"], seg["end"])]
    ox, oy = min(p[0] for p in pts), min(p[1] for p in pts)
    return sorted(
        (
            round(seg["start"][0] - ox, 4),
            round(seg["start"][1] - oy, 4),
            round(seg["end"][0] - ox, 4),
            round(seg["end"][1] - oy, 4),
        )
        for seg in segs
    )


@pytest.mark.parametrize("rot", [0.0, 45.0, 90.0, 135.0, 180.0, 270.0])
def test_refdes_glyphs_are_a_pure_translation_of_the_0deg_glyphs_at_any_rotation(rot):
    """ "Read from one side": a refdes label's GLYPHS never rotate with the
    part, however the part itself is oriented -- only the label's anchor
    point moves. So the label strokes at ANY part rotation must be exactly
    the 0-degree strokes translated (same shape, same size, same
    orientation) -- never rotated, and never point-reflected (which a
    180-degree glyph rotation would produce and a mere extent check would
    not catch)."""
    up = build_silk(
        from_graph(_graph("U1", 16, x=10.0, y=5.0, rot=0.0), stackup=DEFAULT_STACKUP),
        pads=[],
    )
    rotated = build_silk(
        from_graph(_graph("U1", 16, x=10.0, y=5.0, rot=rot), stackup=DEFAULT_STACKUP),
        pads=[],
    )
    up_segs = _refdes_segments(up)
    rotated_segs = _refdes_segments(rotated)
    assert up_segs and rotated_segs
    assert _normalized_segments(up_segs) == _normalized_segments(rotated_segs)


def test_a_part_rotated_180_gets_upright_text_not_mirrored_text():
    """A refdes exists to be read. Rotating the part must move WHERE the
    label sits without turning the glyphs upside-down -- so the strokes of
    a part at 180 must be the 0-degree strokes translated, never the
    0-degree strokes point-reflected. (Now a special case of the stronger
    "any rotation" property above, kept as its own test since it's the
    original, most legible statement of the rule.)"""
    up = build_silk(
        from_graph(_graph("U1", 16, x=10.0, y=5.0, rot=0.0), stackup=DEFAULT_STACKUP),
        pads=[],
    )
    flipped = build_silk(
        from_graph(_graph("U1", 16, x=10.0, y=5.0, rot=180.0), stackup=DEFAULT_STACKUP),
        pads=[],
    )
    assert _normalized_segments(_refdes_segments(up)) == _normalized_segments(
        _refdes_segments(flipped)
    )


def test_bottom_side_text_still_mirrors():
    """ "Read from one side" means one orientation PER SIDE, not that
    bottom silk should read from the top: B.Silkscreen is viewed through
    the board, so its text is still mirrored in the file (unlike the
    glyph-rotation pin, the mirror is NOT removed by this change)."""
    top = build_silk(
        from_graph(_graph("U1", 16, x=0.0, y=0.0, rot=0.0), stackup=DEFAULT_STACKUP),
        pads=[],
        instance_sides={"U1": "top"},
    )
    bottom = build_silk(
        from_graph(_graph("U1", 16, x=0.0, y=0.0, rot=0.0), stackup=DEFAULT_STACKUP),
        pads=[],
        instance_sides={"U1": "bottom"},
    )
    top_xs = sorted(
        p[0] for seg in _refdes_segments(top) for p in (seg["start"], seg["end"])
    )
    bottom_segs = [
        seg
        for d in bottom.draws["bottom"]
        if d["role"] == "refdes"
        for seg in d["segments"]
    ]
    assert bottom_segs
    bottom_xs = sorted(p[0] for seg in bottom_segs for p in (seg["start"], seg["end"]))
    assert bottom_xs == pytest.approx([-x for x in reversed(top_xs)], abs=1e-6)


def test_empty_silk_still_produces_a_valid_legend_file():
    model = {"layers": [layer["name"] for layer in DEFAULT_STACKUP], "silkscreen": {}}
    content = gerber.silkscreen_gerber(model, "top")
    assert "%TF.FileFunction,Legend,Top*%" in content
    art = gerber_view.parse_gerber(content)
    assert art.strokes == []


# ── fiducials ──────────────────────────────────────────────────────────
_BOARD_OUTLINE = [[0.0, 0.0], [60.0, 0.0], [60.0, 40.0], [0.0, 40.0]]


def _triangle_area2(a, b, c) -> float:
    """Twice the signed area of triangle abc -- zero iff a, b, c are
    collinear. Written locally, independent of anything silk.py uses
    internally, per the task brief's "assert that property in a test
    rather than trusting the placement code"."""
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1]))


def _point_in_poly_independent(p, poly) -> bool:
    """A second, independently-written point-in-polygon test (ray
    casting) -- deliberately not silk.py's own ``_point_in_polygon``, so a
    containment assertion built on this cannot pass merely because it
    shares a bug with the implementation."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_at_y = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < x_at_y:
                inside = not inside
    return inside


def test_build_fiducials_places_three_inside_the_outline():
    result = build_fiducials(_BOARD_OUTLINE, pads=[], layer="F.Cu")
    assert not result.dropped
    assert len(result.fiducials) == 3
    for p in result.fiducials:
        assert _point_in_poly_independent(p, _BOARD_OUTLINE)


def test_build_fiducials_are_non_collinear():
    """Three collinear fiducials cannot resolve rotation -- assert the
    real geometric property (nonzero triangle area) rather than trusting
    the corner-based placement code to have gotten it right."""
    result = build_fiducials(_BOARD_OUTLINE, pads=[], layer="F.Cu")
    a, b, c = result.fiducials
    assert _triangle_area2(a, b, c) > 1.0  # comfortably nonzero, not just != 0.0


def test_build_fiducials_copper_and_mask_diameters_are_the_named_constants():
    result = build_fiducials(_BOARD_OUTLINE, pads=[], layer="F.Cu")
    assert len(result.pads) == 3
    for pad in result.pads:
        assert pad["shape"] == "circle"
        assert pad["layer"] == "F.Cu"
        assert pad["w"] == FIDUCIAL_COPPER_DIA_MM
    assert len(result.silk_keepouts) == 3
    for keepout in result.silk_keepouts:
        assert keepout["w"] == FIDUCIAL_MASK_DIA_MM
    assert len(result.plane_blockers) == 3
    for blocker in result.plane_blockers:
        assert blocker["ctype"] == "via"
        assert blocker["dia_mm"] == FIDUCIAL_MASK_DIA_MM
        assert blocker["layers"] == ["F.Cu"]


def test_build_fiducials_never_overlaps_a_real_pad():
    """A pad sitting exactly where the tight-margin corner candidate
    would land forces that fiducial to either escalate its margin or be
    skipped -- never silently overlap the pad."""
    tight = FIDUCIAL_MARGIN_MM  # the corner candidate nearest (0, 0)
    blocking_pad: dict[str, Any] = {
        "shape": "circle",
        "x": tight,
        "y": tight,
        "w": FIDUCIAL_MASK_DIA_MM * 3,  # big enough to also block the 2x-margin rung
        "net": "GND",
    }
    result = build_fiducials(_BOARD_OUTLINE, pads=[blocking_pad], layer="F.Cu")
    r = FIDUCIAL_MASK_DIA_MM / 2.0
    for fx, fy in result.fiducials:
        dist = math.hypot(fx - blocking_pad["x"], fy - blocking_pad["y"])
        assert dist > r + blocking_pad["w"] / 2.0
    # the near-(0,0) corner had to escalate past its default margin to
    # clear the pad -- never silently landing at the blocked spot.
    assert (FIDUCIAL_MARGIN_MM, FIDUCIAL_MARGIN_MM) not in result.fiducials


def test_build_fiducials_reports_a_corner_it_could_not_clear():
    """When even the escalated-margin rung is still blocked, that corner
    is reported in ``dropped`` -- never silently absorbed into a smaller
    result the caller has no way to notice."""
    covers_both_rungs = {
        "shape": "circle",
        "x": FIDUCIAL_MARGIN_MM,
        "y": FIDUCIAL_MARGIN_MM,
        "w": 20.0,
        "net": "GND",
    }
    result = build_fiducials(_BOARD_OUTLINE, pads=[covers_both_rungs], layer="F.Cu")
    assert result.dropped
    assert any("corner 0" in msg for msg in result.dropped)
    # the 4th corner (not tried by default, since count=3 already
    # succeeds via the other 3) covers for the one that failed here --
    # still exactly `count` fiducials, honestly reported as having used
    # a fallback corner rather than silently landing fewer.
    assert len(result.fiducials) == 3


def test_build_fiducials_is_a_pure_function_of_its_inputs():
    a = build_fiducials(_BOARD_OUTLINE, pads=[], layer="F.Cu")
    b = build_fiducials(_BOARD_OUTLINE, pads=[], layer="F.Cu")
    assert a == b


def test_fiducial_plane_blockers_cut_a_hole_in_a_pour():
    """The antipad guarantee: fold a fiducial's ``plane_blockers`` into
    the SAME blocker list precis.pcb.realize._pad_blockers already builds
    for a real pad, and precis.pcb.planes.plane_pours (read, not edited,
    per this task's file ownership) cuts the SAME antipad hole around it
    that it already cuts around a foreign pad/via -- proving the
    fiducial geometry this module produces is wired correctly for the
    caller's realize-time integration (see FiducialResult's own
    docstring for why this module cannot fold it in itself)."""
    result = build_fiducials(_BOARD_OUTLINE, pads=[], layer="F.Cu")
    pours = plane_pours(
        outline=_BOARD_OUTLINE,
        layers=["F.Cu"],
        plane_nets={0: "GND"},
        copper=result.plane_blockers,
        clearance_mm=0.2,
        edge_clearance_mm=0.5,
    )
    assert len(pours) == 1
    pour = pours[0]
    assert pour.get("holes")
    for fx, fy in result.fiducials:
        assert point_in_pour(pour, fx, fy) is False
    # the board centre, nowhere near a fiducial, is still poured copper.
    assert point_in_pour(pour, 30.0, 20.0) is True


# ── title block ────────────────────────────────────────────────────────
def test_title_block_renders_real_strokes_inside_the_board():
    result = build_title_block(
        _BOARD_OUTLINE, pads=[], name="WIDGET", revision="A", date="2026-08-27"
    )
    assert not result.dropped
    assert result.draws
    assert result.bbox is not None
    for c in result.bbox:
        assert _point_in_poly_independent(c, _BOARD_OUTLINE)
    for draw in result.draws:
        assert draw["segments"]  # real strokes, not empty placeholders


def test_title_block_omits_a_date_and_revision_it_was_never_given():
    """Never fabricates a date/revision -- passing neither renders one
    line, not three, and the two extra stacked-line gaps are absent from
    the resulting bbox height."""
    name_only = build_title_block(_BOARD_OUTLINE, pads=[], name="WIDGET")
    full = build_title_block(
        _BOARD_OUTLINE, pads=[], name="WIDGET", revision="A", date="2026-08-27"
    )
    assert name_only.bbox is not None and full.bbox is not None
    h_name_only = name_only.bbox[2][1] - name_only.bbox[0][1]
    h_full = full.bbox[2][1] - full.bbox[0][1]
    assert h_full > h_name_only


def test_title_block_relocates_away_from_a_fiducial_at_its_default_corner():
    fids = build_fiducials(_BOARD_OUTLINE, pads=[], layer="F.Cu")
    unblocked = build_title_block(_BOARD_OUTLINE, pads=[], name="WIDGET")
    assert unblocked.bbox is not None
    blocking = {
        "shape": "circle",
        "x": unblocked.bbox[0][0],
        "y": unblocked.bbox[0][1],
        "w": 6.0,
    }
    blocked = build_title_block(
        _BOARD_OUTLINE, pads=[], name="WIDGET", avoid=[blocking, *fids.silk_keepouts]
    )
    assert blocked.dropped or blocked.bbox != unblocked.bbox


def test_title_block_placed_first_keeps_part_silk_clear_of_it():
    """Placed FIRST (module docstring): fold the block's own bbox into
    build_silk's `reserved` and a part dropped right where the title
    block sits gets its silk relocated/dropped around it, never printed
    on top."""
    tb = build_title_block(_BOARD_OUTLINE, pads=[], name="WIDGET", revision="A")
    assert tb.bbox is not None
    bx = sum(c[0] for c in tb.bbox) / 4.0
    by = sum(c[1] for c in tb.bbox) / 4.0
    ir = from_graph(_graph("U1", 4, x=bx, y=by), stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[], reserved={"top": [obstacle_from_bbox(tb.bbox)]})
    part_segs = [seg for d in result.draws["top"] for seg in d["segments"]]
    title_segs = [seg for d in tb.draws for seg in d["segments"]]
    for pseg in part_segs:
        for tseg in title_segs:
            assert not _segments_hit(
                (pseg["start"], pseg["end"]), (tseg["start"], tseg["end"]), 0.15
            )


def _corner_blocker(cx: float, cy: float, size: float = 25.0) -> dict[str, Any]:
    """A rect big enough to cover every margin rung (``margin_mm``,
    ``1.5x``, ``2x``) AND a whole rendered line's advance box near
    outline corner ``(cx, cy)`` -- oversized on purpose so a test using it
    isn't quietly fragile to the exact margin/text-width arithmetic."""
    return {"shape": "rect", "x": cx, "y": cy, "w": size, "h": size}


def test_title_block_falls_back_to_a_second_corner_when_the_first_is_blocked():
    """Blocking only the bottom-right corner (the default/first rung)
    must not drop the whole block -- the ladder has to actually try (and
    land at) a different corner, not just retry margins at the same one."""
    unblocked = build_title_block(_BOARD_OUTLINE, pads=[], name="WIDGET")
    assert unblocked.bbox is not None
    blocked = build_title_block(
        _BOARD_OUTLINE,
        pads=[],
        name="WIDGET",
        avoid=[_corner_blocker(60.0, 0.0)],
    )
    assert not blocked.dropped
    assert blocked.bbox is not None
    for c in blocked.bbox:
        assert _point_in_poly_independent(c, _BOARD_OUTLINE)
        # actually landed away from the blocked bottom-right corner, not
        # just a slightly-nudged variant still inside the blocker's reach.
        assert c[0] < 30.0
    # every drawn stroke is still clear of the blocking obstacle.
    strokes = [seg for d in blocked.draws for seg in d["segments"]]
    blocker = _corner_blocker(60.0, 0.0)
    bx0 = blocker["x"] - blocker["w"] / 2.0
    bx1 = blocker["x"] + blocker["w"] / 2.0
    by0 = blocker["y"] - blocker["h"] / 2.0
    by1 = blocker["y"] + blocker["h"] / 2.0
    for seg in strokes:
        for px, py in (seg["start"], seg["end"]):
            assert not (bx0 <= px <= bx1 and by0 <= py <= by1)


def test_title_block_left_aligns_and_stacks_downward_at_a_top_left_corner():
    """Blocking bottom-right, bottom-left AND top-right forces the ladder
    all the way to top-left -- the one corner that is BOTH left-aligned
    (not the default right-align) and stacked DOWN into the board (not
    up, which would walk the second/third line off the top edge). A
    wrong h_align pushes the box's far edge past x=0 (out of the outline);
    a wrong stack direction pushes REV/date past y=40 (out of the
    outline) -- either bug makes every rung at this corner fail
    containment, so a passing ``not result.dropped`` here already proves
    both were fixed, not just that SOME corner happened to work."""
    result = build_title_block(
        _BOARD_OUTLINE,
        pads=[],
        name="WIDGET",
        revision="A",
        date="2026-08-27",
        avoid=[
            _corner_blocker(60.0, 0.0),
            _corner_blocker(0.0, 0.0),
            _corner_blocker(60.0, 40.0),
        ],
    )
    assert not result.dropped
    assert result.bbox is not None
    for c in result.bbox:
        assert _point_in_poly_independent(c, _BOARD_OUTLINE)
    bx0, bx1 = result.bbox[0][0], result.bbox[1][0]
    by0, by1 = result.bbox[0][1], result.bbox[2][1]
    assert bx0 < 30.0 and bx1 < 30.0  # left half -- not the right-aligned default
    assert by1 > 20.0  # top half -- landed at a top, not a bottom, corner


def test_title_block_reports_every_corner_tried_when_all_are_blocked():
    """Every corner occupied still says so -- the drop path must remain
    reachable, and the message must name the corners the ladder actually
    walked, not just repeat the old single-corner wording."""
    result = build_title_block(
        _BOARD_OUTLINE,
        pads=[],
        name="WIDGET",
        avoid=[
            _corner_blocker(0.0, 0.0),
            _corner_blocker(60.0, 0.0),
            _corner_blocker(0.0, 40.0),
            _corner_blocker(60.0, 40.0),
        ],
    )
    assert result.draws == []
    assert result.bbox is None
    assert result.dropped
    msg = " ".join(result.dropped)
    for corner in ("bottom-right", "bottom-left", "top-right", "top-left"):
        assert corner in msg


def test_title_block_side_bottom_still_mirrors():
    top = build_title_block(_BOARD_OUTLINE, pads=[], name="WIDGET", side="top")
    bottom = build_title_block(_BOARD_OUTLINE, pads=[], name="WIDGET", side="bottom")
    assert not top.dropped and not bottom.dropped
    assert bottom.draws and bottom.draws != top.draws
    assert bottom.bbox is not None
    for c in bottom.bbox:
        assert _point_in_poly_independent(c, _BOARD_OUTLINE)


def test_obstacle_from_bbox_matches_the_bbox_extent():
    bbox = [(1.0, 2.0), (5.0, 2.0), (5.0, 6.0), (1.0, 6.0)]
    obstacle = obstacle_from_bbox(bbox)
    assert obstacle == {"shape": "rect", "x": 3.0, "y": 4.0, "w": 4.0, "h": 4.0}


# ── S/N label patch ────────────────────────────────────────────────────
def test_sn_patch_draws_a_solid_box_and_clear_knockout_letters():
    result = build_sn_patch(_BOARD_OUTLINE, pads=[])
    assert not result.dropped
    assert result.bbox is not None
    for c in result.bbox:
        assert _point_in_poly_independent(c, _BOARD_OUTLINE)
    box_draws = [d for d in result.draws if d.get("shape") == "region"]
    text_draws = [d for d in result.draws if d.get("shape") != "region"]
    assert len(box_draws) == 1
    assert len(box_draws[0]["polygon"]) >= 4
    assert text_draws  # real knockout strokes, not empty
    for d in text_draws:
        # this is THE assertion that would silently pass a knockout that
        # is never actually clear-polarity -- the task brief's own named
        # failure mode.
        assert d["polarity"] == "clear"
        assert d["segments"]


def test_sn_patch_box_leaves_real_writing_room_beyond_the_label():
    """Not just a tight label bbox -- the box must leave clear writing
    space for an assembler to actually Sharpie a serial number onto
    (task brief, verbatim)."""
    result = build_sn_patch(_BOARD_OUTLINE, pads=[])
    assert result.bbox is not None
    box_w = result.bbox[1][0] - result.bbox[0][0]
    label_w = stroke_font.text_width_mm(SN_LABEL, SN_LABEL_HEIGHT_MM)
    assert box_w > label_w * 2.0


def test_sn_patch_round_trips_through_gerber_as_a_real_cutout():
    """The gerber-level half of "never actually clear-polarity": the
    knockout must ride out as %LPC*% ... %LPD*% around real strokes, AND
    read back as a hole in the box -- never as ordinary dark ink."""
    result = build_sn_patch(_BOARD_OUTLINE, pads=[])
    assert not result.dropped
    model = {
        "layers": [layer["name"] for layer in DEFAULT_STACKUP],
        "silkscreen": {"top": result.draws, "bottom": []},
    }
    top_gerber = gerber.silkscreen_gerber(model, "top")
    assert "G36*" in top_gerber and "G37*" in top_gerber
    assert "%LPC*%" in top_gerber
    lpc_idx = top_gerber.index("%LPC*%")
    lpd_idx = top_gerber.rindex("%LPD*%")
    assert lpc_idx < lpd_idx  # polarity is restored to dark after the letters

    art = gerber_view.parse_gerber(top_gerber)
    assert art.regions
    assert art.regions[0].solid  # the box itself
    assert any(not r.solid for r in art.regions)  # at least one knockout hole
    # a clear-polarity letter must never ALSO survive as ordinary ink
    assert art.strokes == []


def test_sn_patch_sits_adjacent_to_the_title_block():
    tb = build_title_block(_BOARD_OUTLINE, pads=[], name="WIDGET")
    assert tb.bbox is not None
    sn = build_sn_patch(_BOARD_OUTLINE, pads=[], title_bbox=tb.bbox)
    assert not sn.dropped
    assert sn.bbox is not None
    for c in sn.bbox:
        assert _point_in_poly_independent(c, _BOARD_OUTLINE)
    tx0 = min(p[0] for p in tb.bbox)
    tx1 = max(p[0] for p in tb.bbox)
    ty0 = min(p[1] for p in tb.bbox)
    ty1 = max(p[1] for p in tb.bbox)
    sx0 = min(p[0] for p in sn.bbox)
    sx1 = max(p[0] for p in sn.bbox)
    sy0 = min(p[1] for p in sn.bbox)
    sy1 = max(p[1] for p in sn.bbox)
    # never overlapping the title block itself...
    assert sx1 <= tx0 or sx0 >= tx1 or sy1 <= ty0 or sy0 >= ty1
    # ...but genuinely NEXT TO it (task brief), not dropped clear across
    # the board to some unrelated corner.
    assert (
        abs(sy0 - ty1) < 5.0
        or abs(sy1 - ty0) < 5.0
        or abs(sx0 - tx1) < 5.0
        or abs(sx1 - tx0) < 5.0
    )


def test_sn_patch_is_an_obstacle_a_later_refdes_label_avoids():
    """The other named failure mode: a box that is never folded into
    build_silk's obstacle list. Proven both directions -- WITHOUT the
    patch's own bbox reserved, a part placed right on top of it really
    does draw silk there (so this isn't a vacuous "never collides
    anyway" test); WITH it reserved, none of that part's silk lands
    inside the patch."""
    tb = build_title_block(_BOARD_OUTLINE, pads=[], name="WIDGET")
    assert tb.bbox is not None
    sn = build_sn_patch(_BOARD_OUTLINE, pads=[], title_bbox=tb.bbox)
    assert not sn.dropped
    assert sn.bbox is not None
    bx = sum(c[0] for c in sn.bbox) / 4.0
    by = sum(c[1] for c in sn.bbox) / 4.0
    ox0 = min(c[0] for c in sn.bbox)
    ox1 = max(c[0] for c in sn.bbox)
    oy0 = min(c[1] for c in sn.bbox)
    oy1 = max(c[1] for c in sn.bbox)

    ir = from_graph(_graph("U1", 4, x=bx, y=by), stackup=DEFAULT_STACKUP)

    unreserved = build_silk(ir, pads=[])
    unreserved_segs = [seg for d in unreserved.draws["top"] for seg in d["segments"]]
    assert any(
        ox0 <= px <= ox1 and oy0 <= py <= oy1
        for seg in unreserved_segs
        for px, py in (seg["start"], seg["end"])
    ), "fixture must actually collide when the patch isn't reserved"

    reserved = {"top": [obstacle_from_bbox(tb.bbox), obstacle_from_bbox(sn.bbox)]}
    result = build_silk(ir, pads=[], reserved=reserved)
    part_segs = [seg for d in result.draws["top"] for seg in d["segments"]]
    for seg in part_segs:
        for px, py in (seg["start"], seg["end"]):
            assert not (ox0 <= px <= ox1 and oy0 <= py <= oy1)


def test_sn_patch_reports_a_reason_when_every_spot_is_blocked():
    """The drop path must remain reachable -- not just documented."""
    tb = build_title_block(_BOARD_OUTLINE, pads=[], name="WIDGET")
    blocked = build_sn_patch(
        _BOARD_OUTLINE,
        pads=[],
        title_bbox=tb.bbox,
        avoid=[_corner_blocker(30.0, 20.0, size=200.0)],
    )
    assert blocked.draws == []
    assert blocked.bbox is None
    assert blocked.dropped
    assert "S/N" in " ".join(blocked.dropped)


def test_sn_patch_side_bottom_still_mirrors_the_letters_not_the_box():
    top = build_sn_patch(_BOARD_OUTLINE, pads=[], side="top")
    bottom = build_sn_patch(_BOARD_OUTLINE, pads=[], side="bottom")
    assert not top.dropped and not bottom.dropped
    top_box = next(d for d in top.draws if d.get("shape") == "region")
    bottom_box = next(d for d in bottom.draws if d.get("shape") == "region")
    assert top_box["polygon"] == bottom_box["polygon"]  # the box is symmetric

    top_text = [d for d in top.draws if d.get("polarity") == "clear"]
    bottom_text = [d for d in bottom.draws if d.get("polarity") == "clear"]
    assert top_text and bottom_text
    top_xs = sorted(
        p[0]
        for d in top_text
        for seg in d["segments"]
        for p in (seg["start"], seg["end"])
    )
    bottom_xs = sorted(
        p[0]
        for d in bottom_text
        for seg in d["segments"]
        for p in (seg["start"], seg["end"])
    )
    assert bottom_xs != top_xs  # the letters actually mirrored


def test_build_sn_patch_is_a_pure_function_of_its_inputs():
    a = build_sn_patch(_BOARD_OUTLINE, pads=[])
    b = build_sn_patch(_BOARD_OUTLINE, pads=[])
    assert a == b


# ── determinism ────────────────────────────────────────────────────────
def test_build_title_block_is_a_pure_function_of_its_inputs():
    a = build_title_block(
        _BOARD_OUTLINE, pads=[], name="WIDGET", revision="A", date="2026-08-27"
    )
    b = build_title_block(
        _BOARD_OUTLINE, pads=[], name="WIDGET", revision="A", date="2026-08-27"
    )
    assert a == b


def test_export_fab_with_fiducials_and_title_block_is_byte_identical_twice():
    """End-to-end determinism: fiducial pads + a title block folded into
    a real gerber model, exported twice, must be byte-identical -- this
    module invents nothing time-based (no ``datetime.now()``), so this
    must hold as long as the caller doesn't feed it anything that isn't."""
    ir = from_graph(_graph("U1", 4, x=30.0, y=20.0), stackup=DEFAULT_STACKUP)
    fids = build_fiducials(_BOARD_OUTLINE, pads=[], layer="F.Cu")
    tb = build_title_block(
        _BOARD_OUTLINE,
        pads=[],
        name="WIDGET",
        revision="A",
        date="2026-08-27",
        avoid=fids.silk_keepouts,
    )
    assert tb.bbox is not None
    reserved = {"top": [*fids.silk_keepouts, obstacle_from_bbox(tb.bbox)]}
    silk_result = build_silk(ir, pads=fids.pads, reserved=reserved)
    top_draws = tb.draws + silk_result.draws["top"]

    def _model() -> dict:
        return {
            "layers": [layer["name"] for layer in DEFAULT_STACKUP],
            "outline": _BOARD_OUTLINE,
            "copper": [],
            "pads": fids.pads,
            "silkscreen": {"top": top_draws, "bottom": silk_result.draws["bottom"]},
        }

    files_a = gerber.export_fab(_model(), name="widget", allow_synthesized=True)
    files_b = gerber.export_fab(_model(), name="widget", allow_synthesized=True)
    assert files_a == files_b


# ── census (SilkPlacement) -- dropped/relocated are DERIVED from this,
# never built independently alongside it. Refdes fixtures below are
# deliberately asymmetric (`TP9`, `TP4`, `TP3`, `TP1`, `TP5`) -- not
# anything with a rotational/mirror symmetry like `S`/`N` -- per this
# subsystem's own fixture-symmetry lesson (see the module's silk
# postmortem notes).
def test_census_records_placed_outcome_for_every_kind_on_a_clean_part():
    ir = from_graph(_graph("TP9", 16, x=10.0, y=5.0), stackup=DEFAULT_STACKUP)
    result = build_silk(ir, pads=[])
    by_kind = {c.kind: c for c in result.census if c.refdes == "TP9"}
    assert set(by_kind) == {"courtyard", "pin1", "refdes"}
    for c in by_kind.values():
        assert c.outcome == "placed"
        assert c.reason is None
        assert c.side == "top"
        assert c.stroke_width_mm > 0
    # height_mm is meaningful ONLY for the refdes (text) item -- a
    # courtyard/pin-1 tick is a stroke, not text (SilkPlacement's own
    # docstring).
    assert by_kind["refdes"].height_mm is not None
    assert by_kind["courtyard"].height_mm is None
    assert by_kind["pin1"].height_mm is None


def test_census_records_dropped_outcome_with_a_reason_matching_the_derived_prose():
    ir = from_graph(_graph("TP4", 16, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    # blankets the whole part and every relocation candidate -- everything
    # for TP4 drops (same fixture shape as
    # test_refdes_dropped_when_every_candidate_overlaps_a_pad above).
    pads = [{"shape": "circle", "x": 0.0, "y": 0.0, "w": 40.0, "net": "FOREIGN"}]
    result = build_silk(ir, pads=pads)
    refdes_row = next(
        c for c in result.census if c.refdes == "TP4" and c.kind == "refdes"
    )
    assert refdes_row.outcome == "dropped"
    assert refdes_row.reason is not None
    assert f"TP4: {refdes_row.reason}" in result.dropped


def test_census_records_relocated_outcome_with_the_candidate_index():
    ir = from_graph(_graph("TP3", 16, x=0.0, y=0.0), stackup=DEFAULT_STACKUP)
    # a pad on the part's centre forces the default (candidate 0) spot to
    # relocate -- same fixture shape as
    # test_refdes_relocates_when_the_default_center_spot_overlaps_a_pad.
    pads = [{"shape": "circle", "x": 0.0, "y": 0.0, "w": 3.0, "net": "FOREIGN"}]
    result = build_silk(ir, pads=pads)
    refdes_row = next(
        c for c in result.census if c.refdes == "TP3" and c.kind == "refdes"
    )
    assert refdes_row.outcome == "relocated"
    assert refdes_row.reason is not None and "candidate" in refdes_row.reason
    assert f"TP3: {refdes_row.reason}" in result.relocated


def test_dropped_and_relocated_are_exactly_derived_from_the_census():
    """The load-bearing property (module docstring): ``dropped``/
    ``relocated`` must be a bijection with the census's own dropped/
    relocated rows -- never an independently-built second record that
    could drift from it (this subsystem's own recurring, named defect)."""
    ir = from_graph(
        _multi(_graph("TP1", 16, x=0.0, y=0.0), _graph("TP5", 8, x=6.0, y=0.0)),
        stackup=DEFAULT_STACKUP,
    )
    pads = [{"shape": "circle", "x": 0.0, "y": 0.0, "w": 40.0, "net": "FOREIGN"}]
    result = build_silk(ir, pads=pads)
    assert result.census  # sanity: the fixture actually produced rows
    expected_dropped = tuple(
        f"{c.refdes}: {c.reason}" for c in result.census if c.outcome == "dropped"
    )
    expected_relocated = tuple(
        f"{c.refdes}: {c.reason}" for c in result.census if c.outcome == "relocated"
    )
    assert result.dropped == expected_dropped
    assert result.relocated == expected_relocated

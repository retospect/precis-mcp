"""Geometric DRC on realized copper (pcb-guided-place-route Slice 8). No DB.

Covers: each rule firing on a deliberate violation and staying quiet on a
clean board, the two-tier (error/warn) margin numbers, ``None`` capability
fields not crashing, V-cut vs. routed board-edge clearance, and — the
highest-value test here — the O(n^2) reference oracle (:func:`precis.pcb.
drc.clearance_violations_naive`) checked against the STRtree-accelerated
engine (:func:`precis.pcb.drc.clearance_pairs_indexed`) over many
randomized track/via layouts.
"""

from __future__ import annotations

import math
import random
from typing import Any

import pytest

from precis.pcb import DEFAULT_STACKUP, drc
from precis.pcb.capabilities import capability_for
from precis.pcb.ir import from_graph
from precis.pcb.realize import RealizeConfig, realize, to_gerber_model
from precis.pcb.rules import NetRules
from precis.pcb.silk import SilkPlacement

_CAP4 = capability_for("4layer")
# 4-layer trace_spacing_mm: jlc_min=0.09, house_default=0.15 (capabilities.py)


def _track(
    net: str, layer: str, start, end, *, width_mm: float = 0.2
) -> dict[str, Any]:
    return {
        "ctype": "track",
        "layer": layer,
        "net": net,
        "width_mm": width_mm,
        "segments": [{"shape": "line", "start": list(start), "end": list(end)}],
    }


def _via(
    net: str, layer: str, x: float, y: float, *, dia_mm: float, drill_mm: float
) -> dict[str, Any]:
    return {
        "ctype": "via",
        "net": net,
        "x": x,
        "y": y,
        "dia_mm": dia_mm,
        "drill_mm": drill_mm,
        "layers": [layer],
    }


# ── clearance ─────────────────────────────────────────────────────────


def test_check_clearance_fires_error_below_jlc_min():
    # two parallel tracks 1mm long, 0.2mm wide (0.1mm half-width each) on
    # F.Cu, 0.05mm apart edge-to-edge -> well under jlc_min=0.09mm.
    gap = 0.05
    model = {
        "layers": ["F.Cu", "B.Cu"],
        "copper": [
            _track("A", "F.Cu", (0.0, 0.0), (1.0, 0.0)),
            _track("B", "F.Cu", (0.0, 0.2 + gap), (1.0, 0.2 + gap)),
        ],
    }
    findings = drc.check_clearance(model, _CAP4)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "clearance" and f.severity == "error"
    assert f.margin_mm is not None and f.margin_mm < 0
    jlc_min = _CAP4.jlc_min["trace_spacing_mm"]
    assert jlc_min is not None
    assert f.margin_mm == pytest.approx(gap - jlc_min, abs=1e-4)


def test_check_clearance_warns_between_jlc_min_and_house_default():
    jlc_min = _CAP4.jlc_min["trace_spacing_mm"]
    house = _CAP4.house_default["trace_spacing_mm"]
    assert jlc_min is not None and house is not None
    gap = (jlc_min + house) / 2.0  # strictly between the two tiers
    model = {
        "layers": ["F.Cu"],
        "copper": [
            _track("A", "F.Cu", (0.0, 0.0), (1.0, 0.0)),
            _track("B", "F.Cu", (0.0, 0.2 + gap), (1.0, 0.2 + gap)),
        ],
    }
    findings = drc.check_clearance(model, _CAP4)
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "warn"
    assert f.margin_mm == pytest.approx(gap - house, abs=1e-4)


def test_check_clearance_quiet_on_clean_board():
    house = _CAP4.house_default["trace_spacing_mm"]
    assert house is not None
    gap = house + 0.5  # comfortably clear
    model = {
        "layers": ["F.Cu"],
        "copper": [
            _track("A", "F.Cu", (0.0, 0.0), (1.0, 0.0)),
            _track("B", "F.Cu", (0.0, 0.2 + gap), (1.0, 0.2 + gap)),
        ],
    }
    assert drc.check_clearance(model, _CAP4) == []


def test_check_clearance_ignores_same_net_and_different_layer():
    model = {
        "layers": ["F.Cu", "B.Cu"],
        "copper": [
            _track("A", "F.Cu", (0.0, 0.0), (1.0, 0.0)),
            _track("A", "F.Cu", (0.0, 0.01), (1.0, 0.01)),  # same net, touching
            _track("B", "B.Cu", (0.0, 0.0), (1.0, 0.0)),  # different layer, coincident
        ],
    }
    assert drc.check_clearance(model, _CAP4) == []


# ── check_clearance with a resolved net_rules override (gr-shaped: Gap B —
# pcb_net_classes.rules had no consumer) ─────────────────────────────────
def test_check_clearance_net_rules_warn_on_a_gap_the_generic_house_default_clears():
    """A gap that comfortably clears the GENERIC house_default (0.15mm)
    but falls short of a net-class-elevated requirement (e.g. a 20V PD
    rail wanting more room) must now WARN once a net_rules map is
    supplied — the exact consumer this module previously lacked."""
    house = _CAP4.house_default["trace_spacing_mm"]
    assert house is not None
    required = 0.5  # a class rule asking for more than the generic house default
    gap = (house + required) / 2.0  # clears house, falls short of `required`
    model = {
        "layers": ["F.Cu"],
        "copper": [
            _track("VBUS_20V", "F.Cu", (0.0, 0.0), (1.0, 0.0)),
            _track("SIG", "F.Cu", (0.0, 0.2 + gap), (1.0, 0.2 + gap)),
        ],
    }
    assert drc.check_clearance(model, _CAP4) == []  # quiet without net_rules

    net_rules = {
        "VBUS_20V": NetRules(track_width_mm=0.5, clearance_mm=required),
        "SIG": NetRules(track_width_mm=house, clearance_mm=house),
    }
    findings = drc.check_clearance(model, _CAP4, net_rules=net_rules)
    assert len(findings) == 1
    assert findings[0].severity == "warn"
    assert findings[0].margin_mm == pytest.approx(gap - required, abs=1e-4)


def test_check_clearance_net_rules_error_tier_unaffected_by_a_higher_requirement():
    """The error tier is the fab's own hard minimum — an elevated
    net-class requirement can only WIDEN the warn band, never move the
    error floor."""
    jlc_min = _CAP4.jlc_min["trace_spacing_mm"]
    assert jlc_min is not None
    gap = jlc_min / 2.0  # well under the fab's own manufacturable floor
    model = {
        "layers": ["F.Cu"],
        "copper": [
            _track("VBUS_20V", "F.Cu", (0.0, 0.0), (1.0, 0.0)),
            _track("SIG", "F.Cu", (0.0, 0.2 + gap), (1.0, 0.2 + gap)),
        ],
    }
    net_rules = {"VBUS_20V": NetRules(track_width_mm=0.5, clearance_mm=1.0)}
    findings = drc.check_clearance(model, _CAP4, net_rules=net_rules)
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].margin_mm == pytest.approx(gap - jlc_min, abs=1e-4)


def test_check_clearance_net_rules_absent_net_falls_back_to_generic_house():
    """A net_rules map that simply doesn't cover a particular net behaves
    exactly like today's capability-only path for that pair."""
    house = _CAP4.house_default["trace_spacing_mm"]
    assert house is not None
    gap = house + 0.5
    model = {
        "layers": ["F.Cu"],
        "copper": [
            _track("A", "F.Cu", (0.0, 0.0), (1.0, 0.0)),
            _track("B", "F.Cu", (0.0, 0.2 + gap), (1.0, 0.2 + gap)),
        ],
    }
    assert drc.check_clearance(model, _CAP4, net_rules={}) == []


# ── pour antipads (gripe 269908: a pour's ``holes`` must not be dropped,
# turning a real antipad into phantom solid copper for clearance) ───────


def _pour(
    net: str, layer: str, polygon: list[tuple[float, float]], *, holes=None
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "ctype": "pour",
        "layer": layer,
        "net": net,
        "polygon": [list(p) for p in polygon],
    }
    if holes:
        item["holes"] = [[list(p) for p in hole] for hole in holes]
    return item


# A 10x10mm GND pour with a 2x2mm antipad hole punched around (5, 5) — the
# same shape :func:`precis.pcb.planes.plane_pours` emits around a foreign
# via/track passing through a poured layer.
_POUR_SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
_POUR_HOLE = [(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)]


def test_check_clearance_quiet_for_foreign_copper_inside_a_pour_antipad():
    """A foreign-net track sitting well inside a pour's ``holes`` entry
    (its own antipad) must NOT be reported — before the fix,
    ``_copper_item_polygon`` dropped ``holes`` entirely and treated the
    pour as solid copper, so this legitimately-antipadded track collided
    with a sheet that, on the real board, has a hole cut exactly there."""
    pour = _pour("GND", "In1.Cu", _POUR_SQUARE, holes=[_POUR_HOLE])
    # Track centered in the 2x2mm hole (4..6, 4..6), comfortably inside:
    # 0.5mm to the nearest hole edge on every side, well past house_default.
    foreign = _track("SIG", "In1.Cu", (4.5, 5.0), (5.5, 5.0))
    model = {"layers": ["F.Cu", "In1.Cu", "B.Cu"], "copper": [pour, foreign]}
    assert drc.check_clearance(model, _CAP4) == []


def test_check_clearance_fires_for_foreign_copper_on_a_pour_solid_region():
    """The converse of the antipad test above, or the fix is only half
    tested: foreign copper actually overlapping the pour's SOLID fill
    (outside any hole) must still be reported — a fix that made pours
    invisible to clearance entirely would pass the antipad test and be a
    worse bug than the one it replaced."""
    pour = _pour("GND", "In1.Cu", _POUR_SQUARE, holes=[_POUR_HOLE])
    # Squarely inside the solid fill, nowhere near the (4..6, 4..6) hole.
    foreign = _track("SIG", "In1.Cu", (1.0, 1.0), (1.5, 1.0))
    model = {"layers": ["F.Cu", "In1.Cu", "B.Cu"], "copper": [pour, foreign]}
    findings = drc.check_clearance(model, _CAP4)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "clearance" and f.severity == "error"
    assert f.margin_mm is not None and f.margin_mm < 0


# ── trace width ───────────────────────────────────────────────────────


def test_check_trace_width_fires_and_stays_quiet():
    jlc_min = _CAP4.jlc_min["trace_width_mm"]
    house = _CAP4.house_default["trace_width_mm"]
    assert jlc_min is not None and house is not None
    thin = _track("A", "F.Cu", (0, 0), (1, 0), width_mm=jlc_min / 2)
    wide = _track("B", "F.Cu", (0, 5), (1, 5), width_mm=house + 0.5)
    model = {"layers": ["F.Cu"], "copper": [thin, wide]}
    findings = drc.check_trace_width(model, _CAP4)
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].margin_mm == pytest.approx(jlc_min / 2 - jlc_min, abs=1e-9)


# ── annular ring ──────────────────────────────────────────────────────


def test_check_annular_ring_fires_and_stays_quiet():
    jlc_min = _CAP4.jlc_min["annular_ring_mm"]
    assert jlc_min is not None
    bad = _via("GND", "F.Cu", 0, 0, dia_mm=0.30, drill_mm=0.30 - jlc_min * 0.4)
    good = _via("GND", "F.Cu", 5, 5, dia_mm=1.2, drill_mm=0.3)
    model = {"layers": ["F.Cu"], "copper": [bad, good]}
    findings = drc.check_annular_ring(model, _CAP4)
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_check_annular_ring_fires_on_a_real_realized_via():
    """The exact gap this task closes: check_annular_ring was correct but
    had ZERO production input before realize.py emitted via geometry (the
    master backlog's own documented gap). Run it against REAL realize.py
    output — not the synthetic ``_via`` dict fixture above.

    **2026-08-28 update**: this used to assert the finding FIRED, because
    the fab-floor via_diameter_mm/drill_mm pairing (0.4/0.25mm, house
    default) rang at (0.4-0.25)/2 = 0.075mm -- below this process's own
    jlc_min ring (0.15mm), i.e. every default-sized via was genuinely
    unmanufacturable per capabilities.py's own published figures. That was
    the defect (via_diameter_mm margined as an independent tunable instead
    of derived from drill+ring) -- now that via_diameter_mm is DERIVED
    (:func:`precis.pcb.capabilities._derive_via_diameter_mm`), a
    default-sized via's ring matches house_default's own annular_ring_mm
    exactly, and this must stay QUIET."""
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 10.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "SIG",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U0", "pin": "1"}, {"refdes": "U1", "pin": "1"}],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)  # In1.Cu -- a layer transition, forces vias
    result = realize(ir, config=RealizeConfig(fab_caps=_CAP4, router="tangent"))
    assert result.vias  # sanity: this task's realize.py change actually produced vias
    layers = [layer["name"] for layer in DEFAULT_STACKUP]
    model = to_gerber_model(
        result, ir, layers=layers, outline=[[0, -5], [20, -5], [20, 5], [0, 5]]
    )
    findings = drc.check_annular_ring(model, _CAP4)
    assert findings == []


def test_check_annular_ring_none_field_never_crashes():
    aluminum = capability_for("aluminum")
    assert aluminum.jlc_min["annular_ring_mm"] is None
    model = {
        "layers": ["F.Cu"],
        "copper": [_via("GND", "F.Cu", 0, 0, dia_mm=0.3, drill_mm=0.29)],
    }
    assert drc.check_annular_ring(model, aluminum) == []


# ── NPTH clearance ────────────────────────────────────────────────────


def test_check_npth_clearance_fires_and_ignores_plated_holes():
    jlc_min = _CAP4.jlc_min["npth_annular_ring_mm"]
    assert jlc_min is not None
    model = {
        "layers": ["F.Cu"],
        "copper": [_track("A", "F.Cu", (0.0, 0.0), (5.0, 0.0), width_mm=0.2)],
        "drills": [
            {"x": 1.0, "y": jlc_min / 4, "dia_mm": 0.6, "plated": False},
            {"x": 1.0, "y": 10.0, "dia_mm": 0.6, "plated": True},  # a via hole, ignored
        ],
    }
    findings = drc.check_npth_clearance(model, _CAP4)
    assert len(findings) == 1
    assert findings[0].severity == "error"


def test_check_npth_clearance_empty_when_no_npth_holes():
    model = {"layers": ["F.Cu"], "copper": [_track("A", "F.Cu", (0, 0), (1, 0))]}
    assert drc.check_npth_clearance(model, _CAP4) == []


# ── pad helper (shared by pad-vs-copper clearance and via-to-pad keep-out
# tests below) ────────────────────────────────────────────────────────────


def _pad(
    net: str,
    layer: str,
    x: float,
    y: float,
    *,
    w: float,
    h: float | None = None,
    shape: str = "rect",
) -> dict[str, Any]:
    return {
        "layer": layer,
        "net": net,
        "shape": shape,
        "x": x,
        "y": y,
        "w": w,
        "h": w if h is None else h,
    }


# ── pads as clearance-checked copper (the user's shorted board: a pad is a
# separate top-level ``model["pads"]`` key, and ``clearance_pairs_indexed``
# used to iterate ``model["copper"]`` only — a pad vs. pour/track/via/pad
# pair was never a candidate on EITHER side) ─────────────────────────────


def test_check_clearance_fires_for_a_pad_overlapping_a_foreign_net_pour():
    """The user's actual board, reproduced: a fill pour flowing straight
    over a foreign-net pad's solid land. Before this fix this reported
    ZERO DRC errors — this is the case that must fail BEFORE the fix and
    pass after it."""
    pour = _pour("GND", "In1.Cu", _POUR_SQUARE)  # solid fill, no antipad hole
    pad = _pad("SIG", "In1.Cu", 1.0, 1.0, w=1.0, h=1.0)  # squarely in the solid fill
    model = {"layers": ["F.Cu", "In1.Cu", "B.Cu"], "copper": [pour], "pads": [pad]}
    findings = drc.check_clearance(model, _CAP4)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "clearance" and f.severity == "error"
    assert f.margin_mm is not None and f.margin_mm < 0


def test_check_clearance_quiet_for_a_pad_inside_the_pours_own_antipad_hole():
    """The converse of the pad-vs-pour test above, mirroring the existing
    track-vs-pour antipad pair: a pad legitimately sitting in ITS OWN
    antipad hole (e.g. a through-hole pad on a poured layer) must not be
    reported — a fix that made every pad-on-a-poured-layer an error,
    antipad or not, would be a worse bug than the one it replaces."""
    pour = _pour("GND", "In1.Cu", _POUR_SQUARE, holes=[_POUR_HOLE])
    pad = _pad("SIG", "In1.Cu", 5.0, 5.0, w=1.0, h=1.0)  # centered in the 2x2mm hole
    model = {"layers": ["F.Cu", "In1.Cu", "B.Cu"], "copper": [pour], "pads": [pad]}
    assert drc.check_clearance(model, _CAP4) == []


def test_check_clearance_fires_for_a_pad_overlapping_a_foreign_net_track():
    pad = _pad("SIG", "F.Cu", 0.0, 0.0, w=1.0, h=1.0)
    track = _track("OTHER", "F.Cu", (-2.0, 0.0), (2.0, 0.0))  # runs straight through it
    model = {"layers": ["F.Cu"], "copper": [track], "pads": [pad]}
    findings = drc.check_clearance(model, _CAP4)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "clearance" and f.severity == "error"
    assert f.margin_mm is not None and f.margin_mm < 0


def test_check_clearance_ignores_a_same_net_track_landing_on_its_pad():
    """The exemption this fix must preserve exactly: a trace legitimately
    ends ON its own pad — that is how a trace joins a pad, and must stay
    legal. A fix that made a pad clearance-checked against EVERY neighbour
    (same net included) would "work" only by making a correctly-routed
    board report an error on every single pad, which is worse than the bug
    it replaces."""
    pad = _pad("SIG", "F.Cu", 0.0, 0.0, w=0.6, h=0.6)
    track = _track("SIG", "F.Cu", (0.0, 0.0), (2.0, 0.0))  # starts ON the pad centre
    model = {"layers": ["F.Cu"], "copper": [track], "pads": [pad]}
    assert drc.check_clearance(model, _CAP4) == []


def test_check_clearance_fires_for_a_pad_overlapping_a_foreign_net_via():
    pad = _pad("SIG", "F.Cu", 0.0, 0.0, w=1.0, h=1.0)
    via = _via("OTHER", "F.Cu", 0.0, 0.0, dia_mm=0.6, drill_mm=0.3)
    model = {"layers": ["F.Cu"], "copper": [via], "pads": [pad]}
    findings = drc.check_clearance(model, _CAP4)
    assert len(findings) == 1
    assert findings[0].rule == "clearance" and findings[0].severity == "error"
    assert findings[0].margin_mm is not None and findings[0].margin_mm < 0


def test_check_clearance_fires_for_two_overlapping_foreign_net_pads():
    a = _pad("SIG", "F.Cu", 0.0, 0.0, w=1.0, h=1.0)
    b = _pad(
        "OTHER", "F.Cu", 0.3, 0.0, w=1.0, h=1.0
    )  # 0.3mm centres, 0.5mm half-widths
    model: dict[str, Any] = {"layers": ["F.Cu"], "copper": [], "pads": [a, b]}
    findings = drc.check_clearance(model, _CAP4)
    assert len(findings) == 1
    assert findings[0].rule == "clearance" and findings[0].severity == "error"
    assert findings[0].margin_mm is not None and findings[0].margin_mm < 0
    # Both pair indices land in the PAD segment of the combined item list
    # (past every copper item) -- pins clearance_pairs_indexed's documented
    # "copper then pads" index convention, not just check_clearance's output.
    pairs = drc.clearance_pairs_indexed(model, required_mm=1.0)
    assert len(pairs) == 1
    i, j, _gap = pairs[0]
    assert min(i, j) >= len(model["copper"])


def test_check_clearance_ignores_same_net_overlapping_pads():
    a = _pad("SIG", "F.Cu", 0.0, 0.0, w=1.0, h=1.0)
    b = _pad("SIG", "F.Cu", 0.3, 0.0, w=1.0, h=1.0)
    model = {"layers": ["F.Cu"], "copper": [], "pads": [a, b]}
    assert drc.check_clearance(model, _CAP4) == []


def test_copper_item_polygon_pad_shapes_are_geometrically_correct():
    """:func:`drc._copper_item_polygon` is the ONE shape function a pad's
    geometry goes through (no second circle/rect approximation for the
    clearance path) -- pinned directly per shape rather than only through
    a clearance-finding's pass/fail, so a wrong-but-still-firing shape
    can't hide behind a coincidentally-correct finding."""
    circle = drc._copper_item_polygon(
        {"ctype": "pad", "shape": "circle", "x": 0.0, "y": 0.0, "w": 2.0}
    )
    assert circle is not None
    assert circle.area == pytest.approx(math.pi * 1.0**2, rel=1e-2)

    rect = drc._copper_item_polygon(
        {"ctype": "pad", "shape": "rect", "x": 0.0, "y": 0.0, "w": 2.0, "h": 1.0}
    )
    assert rect is not None
    assert rect.area == pytest.approx(2.0, abs=1e-6)
    assert rect.bounds == pytest.approx((-1.0, -0.5, 1.0, 0.5), abs=1e-6)

    # An obround (stadium): a 1mm-wide, 3mm-long capsule -- area is the
    # 2x1mm rectangular middle plus a full 1mm-diameter circle's worth of
    # area split between the two round end caps.
    obround = drc._copper_item_polygon(
        {"ctype": "pad", "shape": "obround", "x": 0.0, "y": 0.0, "w": 3.0, "h": 1.0}
    )
    assert obround is not None
    expected_area = 2.0 * 1.0 + math.pi * 0.5**2
    assert obround.area == pytest.approx(expected_area, rel=1e-2)

    zero_size = drc._copper_item_polygon(
        {"ctype": "pad", "shape": "circle", "x": 0.0, "y": 0.0, "w": 0.0}
    )
    assert zero_size is None

    with pytest.raises(ValueError):
        drc._copper_item_polygon(
            {"ctype": "pad", "shape": "hexagon", "x": 0.0, "y": 0.0, "w": 1.0}
        )


# ── via-to-pad keep-out ─────────────────────────────────────────────────


def test_check_via_pad_keepout_fires_when_a_via_lands_on_a_same_net_pad():
    """The exact 'they have a courtyard too' case: same-net copper is
    legal by :func:`drc.check_clearance` (a trace legitimately touches its
    own pad), so this is the ONE rule that has to catch a via dropped
    squarely on a pad it is nominally allowed to overlap."""
    pad = _pad("SIG", "F.Cu", 0.0, 0.0, w=1.0, h=1.0)
    via = _via("SIG", "F.Cu", 0.0, 0.0, dia_mm=0.6, drill_mm=0.3)
    model = {"layers": ["F.Cu"], "copper": [via], "pads": [pad]}
    # A same-net pair is completely invisible to check_clearance...
    assert drc.check_clearance(model, _CAP4) == []
    # ...but the via keep-out rule fires anyway.
    findings = drc.check_via_pad_keepout(model, _CAP4)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "via_pad_keepout" and f.severity == "error"
    required = _CAP4.jlc_min["trace_spacing_mm"]
    assert required is not None
    assert f.margin_mm == pytest.approx(-0.3 - 0.5 - required, abs=1e-9)


def test_check_via_pad_keepout_fires_regardless_of_net():
    pad = _pad("OTHER", "F.Cu", 0.0, 0.0, w=1.0, h=1.0)
    via = _via("SIG", "F.Cu", 0.0, 0.0, dia_mm=0.6, drill_mm=0.3)
    model = {"layers": ["F.Cu"], "copper": [via], "pads": [pad]}
    findings = drc.check_via_pad_keepout(model, _CAP4)
    assert len(findings) == 1


def test_check_via_pad_keepout_quiet_when_clear():
    required = _CAP4.jlc_min["trace_spacing_mm"]
    assert required is not None
    pad = _pad("SIG", "F.Cu", 0.0, 0.0, w=1.0, h=1.0)
    # via edge sits comfortably clear of the pad edge.
    via = _via(
        "SIG", "F.Cu", 0.5 + 0.3 + required + 0.05, 0.0, dia_mm=0.6, drill_mm=0.3
    )
    model = {"layers": ["F.Cu"], "copper": [via], "pads": [pad]}
    assert drc.check_via_pad_keepout(model, _CAP4) == []


def test_check_via_pad_keepout_ignores_a_pad_outside_the_vias_span():
    """A blind/buried via physically only reaches the layers it spans — a
    pad on a layer the barrel never touches cannot be drilled through by
    it, no matter how close in (x, y)."""
    pad = _pad("SIG", "B.Cu", 0.0, 0.0, w=1.0, h=1.0)
    via = {
        "ctype": "via",
        "net": "SIG",
        "x": 0.0,
        "y": 0.0,
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        "layers": ["F.Cu"],  # never reaches B.Cu
    }
    model = {"layers": ["F.Cu", "B.Cu"], "copper": [via], "pads": [pad]}
    assert drc.check_via_pad_keepout(model, _CAP4) == []


def test_check_via_pad_keepout_none_field_never_crashes():
    aluminum = capability_for("aluminum")
    pad = _pad("SIG", "F.Cu", 0.0, 0.0, w=1.0, h=1.0)
    via = _via("SIG", "F.Cu", 0.0, 0.0, dia_mm=0.6, drill_mm=0.3)
    model = {"layers": ["F.Cu"], "copper": [via], "pads": [pad]}
    # aluminum still publishes trace_spacing_mm, so this stays a real
    # check -- included alongside the other rules' None-field regression
    # tests for the shape, not because this field goes None on this row.
    assert drc.check_via_pad_keepout(model, aluminum) != []


def test_check_via_pad_keepout_fires_on_a_real_realized_via():
    """Reachability, not just synthetic geometry: run against REAL
    realize.py output (mirrors :func:`test_check_annular_ring_fires_on_a_
    real_realized_via`'s own "real production input, not just a fixture"
    discipline). ``_vias_for_track`` places a single-via group's k=0 via
    at OFFSET ZERO from the track's own endpoint -- which is the pad
    coordinate itself -- so this is not a contrived overlap, it is what
    realize.py already emits today for the single-via case."""
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 10.0, "y": 0.0},
        ],
        "nets": [
            {
                "name": "SIG",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U0", "pin": "1"}, {"refdes": "U1", "pin": "1"}],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)  # In1.Cu -- a layer transition, forces vias
    result = realize(ir, config=RealizeConfig(fab_caps=_CAP4, router="tangent"))
    assert result.vias  # sanity: a layer-transition via was actually emitted
    layers = [layer["name"] for layer in DEFAULT_STACKUP]
    model = to_gerber_model(
        result, ir, layers=layers, outline=[[0, -5], [20, -5], [20, 5], [0, 5]]
    )
    findings = drc.check_via_pad_keepout(model, _CAP4)
    assert len(findings) == 2  # both endpoints' vias land on their own pad
    assert {f.severity for f in findings} == {"error"}


# ── via-to-via keep-out ──────────────────────────────────────────────


def _model_with_two_vias(
    center_gap_mm: float,
    *,
    dia_mm: float = 0.6,
    drill_mm: float = 0.2,
    net_a: str = "SIG",
    net_b: str = "SIG",
    layer: str = "F.Cu",
) -> dict[str, Any]:
    via_a = _via(net_a, layer, 0.0, 0.0, dia_mm=dia_mm, drill_mm=drill_mm)
    via_b = _via(net_b, layer, center_gap_mm, 0.0, dia_mm=dia_mm, drill_mm=drill_mm)
    return {"layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], "copper": [via_a, via_b]}


def test_check_via_via_keepout_fires_on_copper_overlap_same_net():
    """Same net is completely invisible to check_clearance -- the exact
    gap this rule exists to close, mirroring check_via_pad_keepout's own
    'they have a courtyard too' case."""
    # centres 0.55mm apart, dia 0.6mm -> copper gap -0.05mm (below the
    # 4-layer jlc_min of 0.09mm); drill 0.2mm -> hole gap 0.35mm (clear).
    model = _model_with_two_vias(0.55)
    assert drc.check_clearance(model, _CAP4) == []
    findings = drc.check_via_via_keepout(model, _CAP4)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "via_via_keepout" and f.severity == "error"
    required = _CAP4.jlc_min["trace_spacing_mm"]
    assert required is not None
    copper_gap = 0.55 - 0.6
    assert f.margin_mm == pytest.approx(copper_gap - required, abs=1e-9)
    assert "barrel" in f.detail.lower()


def test_check_via_via_keepout_fires_on_both_copper_and_hole_overlap():
    """Two SEPARATE findings when both geometries collide -- copper and
    drill are different questions with different thresholds (module
    docstring)."""
    # centres 0.15mm apart: copper gap -0.45mm, hole gap -0.05mm -- both
    # margins violated.
    model = _model_with_two_vias(0.15)
    findings = drc.check_via_via_keepout(model, _CAP4)
    assert len(findings) == 2
    assert {f.severity for f in findings} == {"error"}
    hole_findings = [f for f in findings if "hole" in f.detail.lower()]
    copper_findings = [f for f in findings if "barrel" in f.detail.lower()]
    assert len(hole_findings) == 1 and len(copper_findings) == 1
    assert hole_findings[0].margin_mm == pytest.approx(0.15 - 0.2, abs=1e-9)


def test_check_via_via_keepout_a_hole_violation_always_implies_a_copper_one():
    """Physically impossible for holes to overlap while barrels don't:
    ``dia_mm >= drill_mm`` always (a positive annular ring), so
    ``copper_gap <= hole_gap`` for any pair -- whenever the hole margin is
    negative the copper margin is too. Asserted here as the property it
    is, not assumed."""
    for gap in (0.01, 0.1, 0.19, 0.24):
        model = _model_with_two_vias(gap, dia_mm=0.6, drill_mm=0.25)
        findings = drc.check_via_via_keepout(model, _CAP4)
        by_kind = {
            ("hole" if "hole" in f.detail.lower() else "copper") for f in findings
        }
        if "hole" in by_kind:
            assert "copper" in by_kind


def test_check_via_via_keepout_fires_regardless_of_net():
    model = _model_with_two_vias(0.15, net_a="SIG_A", net_b="SIG_B")
    findings = drc.check_via_via_keepout(model, _CAP4)
    assert len(findings) == 2  # same numbers as the same-net case above


def test_check_via_via_keepout_quiet_when_clear():
    required = _CAP4.jlc_min["trace_spacing_mm"]
    assert required is not None
    model = _model_with_two_vias(0.6 + required + 0.05, dia_mm=0.6, drill_mm=0.2)
    assert drc.check_via_via_keepout(model, _CAP4) == []


def test_check_via_via_keepout_ignores_vias_with_no_shared_layer():
    """A blind/buried via pair whose spans never share a copper layer are
    not the same physical barrel/hole at all -- mirrors check_via_pad_
    keepout's own layer-span restriction."""
    via_a = {
        "ctype": "via",
        "net": "SIG",
        "x": 0.0,
        "y": 0.0,
        "dia_mm": 0.6,
        "drill_mm": 0.2,
        "layers": ["F.Cu"],
    }
    via_b = {
        "ctype": "via",
        "net": "SIG",
        "x": 0.0,
        "y": 0.0,
        "dia_mm": 0.6,
        "drill_mm": 0.2,
        "layers": ["B.Cu"],
    }
    model = {"layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], "copper": [via_a, via_b]}
    assert drc.check_via_via_keepout(model, _CAP4) == []


def test_check_via_via_keepout_stitched_group_pitch_stays_quiet():
    """A same-net ampacity group spread by :mod:`precis.pcb.realize`'s
    ``_route_pass`` (module docstring) sits at pitch ``via_dia_mm +
    clearance_mm`` -- reproduced here with the SAME formula, not a
    stand-in constant, so this is a real property of that spread, not a
    number chosen to make the test pass."""
    dia_mm, drill_mm = _CAP4.jlc_min["via_diameter_mm"], _CAP4.jlc_min["drill_mm"]
    assert dia_mm is not None and drill_mm is not None
    house_clearance = _CAP4.house_default["trace_spacing_mm"]
    assert house_clearance is not None
    pitch = dia_mm + house_clearance
    model = _model_with_two_vias(pitch, dia_mm=dia_mm, drill_mm=drill_mm)
    assert drc.check_via_via_keepout(model, _CAP4) == []


def test_check_via_via_keepout_none_field_never_crashes():
    aluminum = capability_for("aluminum")
    # aluminum still publishes trace_spacing_mm (see test_check_via_pad_
    # keepout_none_field_never_crashes's own note) -- included for the
    # same shape/None-field regression coverage as every other rule here.
    model = _model_with_two_vias(0.15)
    assert drc.check_via_via_keepout(model, aluminum) != []


def test_check_via_via_keepout_fires_on_a_real_realized_via():
    """Reachability against REAL realize.py output, not just synthetic
    geometry (mirrors test_check_via_pad_keepout_fires_on_a_real_realized_
    via's own discipline). Two instances 0.2mm apart on ONE net, forced to
    change layer, so realize.py drops a via at each pad -- 0.2mm apart is
    close enough that both the copper barrel AND the drilled hole (JLC
    default dia 0.75mm/drill 0.25mm) collide, entirely invisible to
    check_clearance because it is the SAME net at both ends."""
    graph = {
        "instances": [
            {"refdes": "U0", "x": 0.0, "y": 0.0},
            {"refdes": "U1", "x": 0.2, "y": 0.0},
        ],
        "nets": [
            {
                "name": "SIG",
                "net_class": "signal",
                "domain": "electrical",
                "members": [{"refdes": "U0", "pin": "1"}, {"refdes": "U1", "pin": "1"}],
            }
        ],
    }
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    ir.set_layer(0, 1)  # In1.Cu -- a layer transition, forces vias at both ends
    result = realize(ir, config=RealizeConfig(fab_caps=_CAP4, router="tangent"))
    assert len(result.vias) == 2  # sanity: both endpoints got a via
    layers = [layer["name"] for layer in DEFAULT_STACKUP]
    model = to_gerber_model(
        result, ir, layers=layers, outline=[[-2, -5], [20, -5], [20, 5], [-2, 5]]
    )
    assert drc.check_clearance(model, _CAP4) == []  # same net -- invisible to clearance
    findings = drc.check_via_via_keepout(model, _CAP4)
    assert len(findings) == 2  # copper AND hole, both collide at 0.2mm apart
    assert {f.severity for f in findings} == {"error"}


# ── courtyard overlap ─────────────────────────────────────────────────


def _square(cx: float, cy: float, half: float = 1.0) -> list[tuple[float, float]]:
    return [
        (cx - half, cy - half),
        (cx + half, cy - half),
        (cx + half, cy + half),
        (cx - half, cy + half),
    ]


def test_check_courtyard_overlap_fires_and_stays_quiet():
    # 2x2 squares 1.5mm apart: 0.5mm of overlap in x, full 2mm in y.
    overlapping = [("U1", _square(0.0, 0.0)), ("U2", _square(1.5, 0.0))]
    findings = drc.check_courtyard_overlap(overlapping)
    assert len(findings) == 1
    assert findings[0].severity == "error"
    # 0.5mm of x-overlap across the full 2mm height -> 1.0mm^2, 0.5mm deep.
    assert "1.0000mm^2" in findings[0].detail
    assert findings[0].margin_mm == pytest.approx(-0.5, abs=1e-6)

    clear = [("U1", _square(0.0, 0.0)), ("U2", _square(10.0, 0.0))]
    assert drc.check_courtyard_overlap(clear) == []


def test_courtyards_that_merely_touch_are_not_an_overlap():
    """**Pinned deliberately, and constructed rather than sampled.** Two
    courtyards sharing exactly one edge have zero-area intersection —
    shapely's ``intersects`` says yes, and reporting that as an overlap
    would fail every board whose parts sit exactly at their legal minimum
    spacing, which is where a good placer puts them. A sampled near-touch
    never produces this case (it is measure-zero), so the coordinates are
    shared exactly."""
    touching = [("U1", _square(0.0, 0.0)), ("U2", _square(2.0, 0.0))]
    assert drc.check_courtyard_overlap(touching) == []
    # One shared vertex, the corner case of the corner case.
    corner = [("U1", _square(0.0, 0.0)), ("U2", _square(2.0, 2.0))]
    assert drc.check_courtyard_overlap(corner) == []


def test_courtyard_overlap_sees_a_rotation_a_radius_could_not():
    """The reason this rule takes polygons at all. Two 6x1 bars crossing
    at right angles, centres 2mm apart, overlap — but their circumscribed
    circles (radius ~3.04) would have called ANY pair within 6mm of each
    other overlapping, and two bars end-to-end at 4mm apart do not touch
    at all. A radius cannot answer this question in either direction."""
    horizontal = [(-3.0, -0.5), (3.0, -0.5), (3.0, 0.5), (-3.0, 0.5)]
    vertical = [(-0.5, -3.0), (0.5, -3.0), (0.5, 3.0), (-0.5, 3.0)]
    crossing = [
        ("U1", horizontal),
        ("U2", [(x + 2.0, y) for x, y in vertical]),
    ]
    assert len(drc.check_courtyard_overlap(crossing)) == 1

    end_to_end = [("U1", horizontal), ("U2", [(x + 7.0, y) for x, y in horizontal])]
    assert drc.check_courtyard_overlap(end_to_end) == []


# ── board-edge clearance ──────────────────────────────────────────────


def test_check_board_edge_clearance_vcut_default_and_routed_override():
    outline = [[0.0, 0.0], [20.0, 0.0], [20.0, 20.0], [0.0, 20.0]]
    vcut = _CAP4.jlc_min["board_edge_clearance_vcut_mm"]
    routed_min = _CAP4.jlc_min["board_edge_clearance_routed_mm"]
    routed_house = _CAP4.house_default["board_edge_clearance_routed_mm"]
    assert vcut is not None and routed_min is not None and vcut > routed_min
    assert routed_house is not None
    # a (zero-width, so centerline == copper edge) trace sitting comfortably
    # clear of the routed-edge house default but still inside V-cut's
    # (stricter) minimum: fails the V-cut default, clean under 'routed'.
    gap = routed_house + 0.05
    assert gap < vcut
    model = {
        "layers": ["F.Cu"],
        "copper": [_track("A", "F.Cu", (gap, 5.0), (gap, 15.0), width_mm=0.0)],
    }
    unknown_panel = drc.check_board_edge_clearance(model, _CAP4, outline=outline)
    assert len(unknown_panel) == 1 and unknown_panel[0].severity == "error"

    routed_panel = drc.check_board_edge_clearance(
        model, _CAP4, outline=outline, panel_type="routed"
    )
    assert routed_panel == []


def test_check_board_edge_clearance_no_outline_is_a_noop():
    model = {"layers": ["F.Cu"], "copper": [_track("A", "F.Cu", (0, 0), (1, 0))]}
    assert drc.check_board_edge_clearance(model, _CAP4, outline=None) == []
    assert drc.check_board_edge_clearance(model, _CAP4, outline=[[0, 0], [1, 1]]) == []


# ── board-edge clearance, pads (the third member of the "pads live in a
# separate model['pads'] list and this rule forgot" family — see
# clearance_pairs_indexed's own docstring for the first two) ──────────────

_EDGE_OUTLINE = [[0.0, 0.0], [20.0, 0.0], [20.0, 20.0], [0.0, 20.0]]


def test_check_board_edge_clearance_fires_for_a_pad_too_near_the_edge():
    """A pad squarely inside the outline but nearer the left edge than the
    fab's V-cut minimum -- must be reported, error tier, negative margin."""
    jlc_min = _CAP4.jlc_min["board_edge_clearance_vcut_mm"]
    assert jlc_min is not None
    radius = 0.05
    target_gap = jlc_min / 2.0  # inside jlc_min -> error tier
    px = target_gap + radius  # so boundary.distance(pad) == target_gap exactly
    pad = _pad("SIG", "F.Cu", px, 10.0, w=radius * 2, h=radius * 2, shape="circle")
    model = {"layers": ["F.Cu"], "copper": [], "pads": [pad]}
    findings = drc.check_board_edge_clearance(model, _CAP4, outline=_EDGE_OUTLINE)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "board_edge_clearance" and f.severity == "error"
    assert f.margin_mm is not None and f.margin_mm < 0
    assert f.margin_mm == pytest.approx(target_gap - jlc_min, abs=1e-4)


def test_check_board_edge_clearance_quiet_for_a_pad_comfortably_inside():
    house = _CAP4.house_default["board_edge_clearance_vcut_mm"]
    assert house is not None
    radius = 0.05
    px = house + radius + 0.5  # comfortably clear of every edge
    pad = _pad("SIG", "F.Cu", px, 10.0, w=radius * 2, h=radius * 2, shape="circle")
    model = {"layers": ["F.Cu"], "copper": [], "pads": [pad]}
    assert drc.check_board_edge_clearance(model, _CAP4, outline=_EDGE_OUTLINE) == []


def test_pad_outside_the_board_near_the_edge_fires_both_rules_deliberately():
    """A pad hanging just past the edge is simultaneously (a) not on the
    board at all -- ``check_outline_containment``'s question -- and (b) too
    close to the edge LINE to be manufacturable if it were on the board --
    ``check_board_edge_clearance``'s unsigned-distance question (that
    function's own docstring: symmetric about the edge, fires on copper on
    either side). Both statements are true of the SAME pad and answer
    different questions, so both firing is the two rules working, not a
    double-report bug -- the same overlap ``check_outline_containment``'s
    own docstring already documents for tracks/vias/pours, now inherited by
    pads too since pads are checked by both rules."""
    jlc_min = _CAP4.jlc_min["board_edge_clearance_vcut_mm"]
    assert jlc_min is not None
    radius = 0.05
    near_gap = jlc_min / 2.0
    px = -(radius + near_gap)  # circle wholly at x < 0: wholly outside the board
    pad = _pad("SIG", "F.Cu", px, 10.0, w=radius * 2, h=radius * 2, shape="circle")
    model = {"layers": ["F.Cu"], "copper": [], "pads": [pad]}

    edge = drc.check_board_edge_clearance(model, _CAP4, outline=_EDGE_OUTLINE)
    assert len(edge) == 1 and edge[0].rule == "board_edge_clearance"
    assert edge[0].severity == "error"
    assert edge[0].margin_mm is not None and edge[0].margin_mm < 0

    contained = drc.check_outline_containment(model, outline=_EDGE_OUTLINE)
    assert len(contained) == 1 and contained[0].rule == "outline_containment"
    assert contained[0].severity == "error"


def test_check_board_edge_clearance_does_not_double_report_a_single_pad_as_two_findings():
    """Neither rule alone reports MORE than one finding for one pad --
    the double-report risk this task called out is two ENTRIES from the
    SAME rule for the same pad, not the (deliberate, different-question)
    cross-rule overlap covered above."""
    jlc_min = _CAP4.jlc_min["board_edge_clearance_vcut_mm"]
    assert jlc_min is not None
    radius = 0.05
    px = jlc_min / 4.0
    pad = _pad("SIG", "F.Cu", px, 10.0, w=radius * 2, h=radius * 2, shape="circle")
    model = {"layers": ["F.Cu"], "copper": [], "pads": [pad]}
    assert len(drc.check_board_edge_clearance(model, _CAP4, outline=_EDGE_OUTLINE)) == 1


# ── silkscreen vs the board's own outline: containment + edge clearance ──


def _silk_draw(
    role: str, refdes: str, start, end, *, width_mm: float = 0.2
) -> dict[str, Any]:
    return {
        "width_mm": width_mm,
        "segments": [{"shape": "line", "start": list(start), "end": list(end)}],
        "source": "synthesized",
        "role": role,
        "refdes": refdes,
    }


def test_check_outline_containment_flags_silk_drawn_outside_the_board():
    """A refdes label whose ink lands past the outline is as absent from
    the delivered board as copper outside it would be -- the same
    "a fab images only what is inside the profile" rule
    ``check_outline_containment`` already applies to copper/pads/parts,
    extended here to ``model["silkscreen"]``."""
    model = {
        "silkscreen": {
            "top": [_silk_draw("refdes", "U1", (-2.0, 5.0), (-1.0, 5.0))],
            "bottom": [],
        }
    }
    findings = drc.check_outline_containment(model, outline=_EDGE_OUTLINE)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "outline_containment" and f.severity == "error"
    assert f.objects[0]["refdes"] == "U1"


def test_check_outline_containment_quiet_for_silk_well_inside_the_board():
    model = {
        "silkscreen": {
            "top": [_silk_draw("refdes", "U1", (9.0, 10.0), (11.0, 10.0))],
            "bottom": [],
        }
    }
    assert drc.check_outline_containment(model, outline=_EDGE_OUTLINE) == []


def test_check_silk_edge_clearance_fires_for_silk_near_the_edge():
    """Silk sitting inside the board but closer to the cut edge than the
    fab's V-cut minimum -- the same two-tier bar
    ``check_board_edge_clearance`` applies to copper, now applied to ink."""
    jlc_min = _CAP4.jlc_min["board_edge_clearance_vcut_mm"]
    assert jlc_min is not None
    gap = jlc_min / 2.0  # inside jlc_min -> error tier
    model = {
        "silkscreen": {
            "top": [_silk_draw("refdes", "U1", (gap, 5.0), (gap, 15.0), width_mm=0.0)],
            "bottom": [],
        }
    }
    findings = drc.check_silk_edge_clearance(model, _CAP4, outline=_EDGE_OUTLINE)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "silk_edge_clearance" and f.severity == "error"
    assert f.margin_mm is not None and f.margin_mm < 0
    assert f.margin_mm == pytest.approx(gap - jlc_min, abs=1e-4)


def test_check_silk_edge_clearance_quiet_for_silk_comfortably_inside():
    house = _CAP4.house_default["board_edge_clearance_vcut_mm"]
    assert house is not None
    gap = house + 0.5  # comfortably clear of every edge
    model = {
        "silkscreen": {
            "top": [_silk_draw("refdes", "U1", (gap, 5.0), (gap, 15.0), width_mm=0.0)],
            "bottom": [],
        }
    }
    assert drc.check_silk_edge_clearance(model, _CAP4, outline=_EDGE_OUTLINE) == []


def test_check_silk_edge_clearance_no_outline_is_a_noop():
    model = {
        "silkscreen": {
            "top": [_silk_draw("refdes", "U1", (0.0, 5.0), (0.0, 15.0), width_mm=0.0)],
            "bottom": [],
        }
    }
    assert drc.check_silk_edge_clearance(model, _CAP4, outline=None) == []


def test_check_silk_edge_clearance_wired_into_run_geometric_drc():
    jlc_min = _CAP4.jlc_min["board_edge_clearance_vcut_mm"]
    assert jlc_min is not None
    gap = jlc_min / 2.0
    model = {
        "layers": ["F.Cu"],
        "copper": [],
        "silkscreen": {
            "top": [_silk_draw("refdes", "U1", (gap, 5.0), (gap, 15.0), width_mm=0.0)],
            "bottom": [],
        },
    }
    findings = drc.run_geometric_drc(model, capability=_CAP4, outline=_EDGE_OUTLINE)
    assert any(f.rule == "silk_edge_clearance" for f in findings)


# ── process_for_stackup ───────────────────────────────────────────────


def test_process_for_stackup_maps_known_counts_and_raises_otherwise():
    assert drc.process_for_stackup([{}] * 4) == "4layer"
    assert drc.process_for_stackup([{}] * 2) == "2layer"
    with pytest.raises(ValueError, match="6-layer"):
        drc.process_for_stackup([{}] * 6)


# ── run_geometric_drc orchestrator ───────────────────────────────────


def test_run_geometric_drc_aggregates_every_rule():
    jlc_min = _CAP4.jlc_min["trace_width_mm"]
    assert jlc_min is not None
    model = {
        "layers": ["F.Cu"],
        "copper": [
            _track("A", "F.Cu", (0, 0), (1, 0), width_mm=jlc_min / 2),
            _via("SIG", "F.Cu", 5.0, 0.0, dia_mm=0.6, drill_mm=0.2),
            _via("SIG", "F.Cu", 5.15, 0.0, dia_mm=0.6, drill_mm=0.2),
        ],
    }
    findings = drc.run_geometric_drc(
        model,
        capability=_CAP4,
        outline=None,
        courtyards=[("U1", _square(0.0, 0.0)), ("U2", _square(0.5, 0.0))],
    )
    rules = {f.rule for f in findings}
    assert "trace_width" in rules
    assert "courtyard_overlap" in rules
    assert "via_via_keepout" in rules  # wired in, not just defined -- see
    # check_via_via_keepout's own tests for the module-level coverage


def test_run_geometric_drc_threads_net_rules_into_clearance_only():
    house = _CAP4.house_default["trace_spacing_mm"]
    assert house is not None
    required = 0.5
    gap = (house + required) / 2.0
    model = {
        "layers": ["F.Cu"],
        "copper": [
            _track("VBUS_20V", "F.Cu", (0.0, 0.0), (1.0, 0.0)),
            _track("SIG", "F.Cu", (0.0, 0.2 + gap), (1.0, 0.2 + gap)),
        ],
    }
    net_rules = {"VBUS_20V": NetRules(track_width_mm=0.5, clearance_mm=required)}
    findings = drc.run_geometric_drc(model, capability=_CAP4, net_rules=net_rules)
    clearance_findings = [f for f in findings if f.rule == "clearance"]
    assert len(clearance_findings) == 1
    assert clearance_findings[0].severity == "warn"


# ── silk missing / printability ────────────────────────────────────────
#
# `precis.pcb.silk.SilkPlacement` fixtures below deliberately use refdes
# strings with no symmetry (`TP7`, `U9`) rather than anything like `S`/`N`
# -- this module's own recent lesson (see docs/glossary.md and this
# subsystem's silk fixture postmortem): a symmetric fixture cannot see a
# bug a count-based or shape-based assertion would otherwise catch.


def _placement(
    refdes: str,
    kind: str,
    *,
    outcome: str,
    side: str = "top",
    reason: str | None = None,
    stroke_width_mm: float = 0.15,
    height_mm: float | None = None,
) -> SilkPlacement:
    return SilkPlacement(
        refdes=refdes,
        kind=kind,
        side=side,
        outcome=outcome,
        reason=reason,
        stroke_width_mm=stroke_width_mm,
        height_mm=height_mm,
    )


def test_check_silk_missing_fires_an_error_for_a_dropped_refdes():
    census = [
        _placement(
            "TP7",
            "refdes",
            outcome="dropped",
            reason="refdes label dropped -- every candidate placement overlaps a pad",
        )
    ]
    model: dict[str, Any] = {"silkscreen": {"top": [], "bottom": []}}
    findings = drc.check_silk_missing(census, model)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "silk_missing" and f.severity == "error"
    assert "TP7" in f.where and "refdes" in f.where
    assert "overlaps a pad" in f.detail


def test_check_silk_missing_quiet_when_everything_placed_and_drawn():
    census = [_placement("TP7", "refdes", outcome="placed", height_mm=1.0)]
    model = {
        "silkscreen": {
            "top": [
                {"role": "refdes", "refdes": "TP7", "segments": [], "width_mm": 0.15}
            ],
            "bottom": [],
        }
    }
    assert drc.check_silk_missing(census, model) == []


def test_check_silk_missing_cross_check_fires_when_census_claims_success_the_model_lacks():
    """The guard, not the report (task brief, verbatim: "this is the
    guard; do not skip it"): a census row claiming ``"placed"``/
    ``"relocated"`` must have a matching draw in ``model['silkscreen']`` --
    an empty silk layer paired with a "successful" census entry is exactly
    the lie this cross-check exists to catch, independent of whatever the
    census itself claims."""
    census = [_placement("TP7", "refdes", outcome="placed", height_mm=1.0)]
    model: dict[str, Any] = {
        "silkscreen": {"top": [], "bottom": []}
    }  # nothing was actually drawn
    findings = drc.check_silk_missing(census, model)
    assert len(findings) == 1
    assert findings[0].rule == "silk_missing"
    assert "does not describe the board" in findings[0].detail


def test_check_silk_printability_fires_below_jlc_min_stroke_width():
    jlc_min = _CAP4.jlc_min["silk_width_mm"]
    assert jlc_min is not None
    census = [
        _placement("TP7", "courtyard", outcome="placed", stroke_width_mm=jlc_min / 2)
    ]
    findings = drc.check_silk_printability(census, _CAP4)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "silk_printability" and f.severity == "error"
    assert f.margin_mm is not None and f.margin_mm < 0


def test_check_silk_printability_quiet_at_or_above_jlc_min():
    jlc_min = _CAP4.jlc_min["silk_width_mm"]
    assert jlc_min is not None
    census = [
        _placement("TP7", "courtyard", outcome="placed", stroke_width_mm=jlc_min + 0.05)
    ]
    assert drc.check_silk_printability(census, _CAP4) == []


def test_check_silk_printability_warns_below_the_legibility_floor():
    census = [
        _placement(
            "TP7",
            "refdes",
            outcome="placed",
            stroke_width_mm=0.2,
            height_mm=drc.SILK_LEGIBILITY_HEIGHT_MM / 2,
        )
    ]
    findings = drc.check_silk_printability(census, _CAP4)
    assert len(findings) == 1
    assert findings[0].severity == "warn"


def test_check_silk_printability_ignores_dropped_items():
    """A dropped item's stroke width is moot -- it never reached the fab,
    and is already covered by check_silk_missing; checking it here too
    would double-report the same one defect."""
    census = [
        _placement(
            "TP7", "courtyard", outcome="dropped", reason="x", stroke_width_mm=0.01
        )
    ]
    assert drc.check_silk_printability(census, _CAP4) == []


def test_run_geometric_drc_wires_silk_census_when_supplied():
    census = (
        _placement("TP7", "refdes", outcome="dropped", reason="dropped for a test"),
    )
    model = {"layers": ["F.Cu"], "copper": [], "silkscreen": {"top": [], "bottom": []}}
    findings = drc.run_geometric_drc(model, capability=_CAP4, census=census)
    rules = {f.rule for f in findings}
    assert "silk_missing" in rules


def test_run_geometric_drc_with_no_census_stays_clean():
    """No ``census=`` argument at all -- ``run_geometric_drc`` must not
    crash and must not invent a silk finding, matching ``unrouted=None``'s
    own "silent about a thing this module was never told" contract."""
    model = {"layers": ["F.Cu"], "copper": []}
    findings = drc.run_geometric_drc(model, capability=_CAP4)
    assert not any(f.rule.startswith("silk_") for f in findings)


# ── the O(n^2) reference oracle vs. the STRtree-accelerated engine ─────


def _random_line_track(
    rng: random.Random, net: str, layer: str, span: float
) -> dict[str, Any]:
    x0, y0 = rng.uniform(0, span), rng.uniform(0, span)
    x1, y1 = x0 + rng.uniform(-2.0, 2.0), y0 + rng.uniform(-2.0, 2.0)
    return _track(net, layer, (x0, y0), (x1, y1), width_mm=rng.uniform(0.1, 0.5))


def _random_arc_track(
    rng: random.Random, net: str, layer: str, span: float
) -> dict[str, Any]:
    cx, cy = rng.uniform(0, span), rng.uniform(0, span)
    r = rng.uniform(0.5, 2.0)
    a0 = rng.uniform(0, 2 * math.pi)
    sweep = rng.uniform(
        -math.pi + 0.05, math.pi - 0.05
    )  # short-way, matches this codebase's producer
    a1 = a0 + sweep
    start = (cx + r * math.cos(a0), cy + r * math.sin(a0))
    end = (cx + r * math.cos(a1), cy + r * math.sin(a1))
    seg = {
        "shape": "arc",
        "start": list(start),
        "end": list(end),
        "center": [cx, cy],
        "cw": sweep < 0,
    }
    return {
        "ctype": "track",
        "layer": layer,
        "net": net,
        "width_mm": rng.uniform(0.1, 0.5),
        "segments": [seg],
    }


def _random_via_item(
    rng: random.Random, net: str, layer: str, span: float
) -> dict[str, Any]:
    return _via(
        net,
        layer,
        rng.uniform(0, span),
        rng.uniform(0, span),
        dia_mm=rng.uniform(0.3, 0.9),
        drill_mm=rng.uniform(0.15, 0.25),
    )


def _random_model(rng: random.Random, *, n_items: int, span: float) -> dict[str, Any]:
    layers = ["F.Cu", "B.Cu"]
    nets = ["N0", "N1", "N2"]
    items = []
    for _ in range(n_items):
        net = rng.choice(nets)
        layer = rng.choice(layers)
        kind = rng.random()
        if kind < 0.5:
            items.append(_random_line_track(rng, net, layer, span))
        elif kind < 0.8:
            items.append(_random_arc_track(rng, net, layer, span))
        else:
            items.append(_random_via_item(rng, net, layer, span))
    return {"layers": layers, "copper": items}


def test_clearance_oracle_matches_strtree_engine_over_random_layouts():
    """The highest-value test in this file: a dependency-free O(n^2)
    reference (no shapely, no spatial index) must agree EXACTLY with the
    STRtree-accelerated engine — both which pairs violate and the gap
    number itself — over many randomized track/via layouts. A spatial-
    index bug (a query that silently misses a neighbour) would show up
    here as a pair the accelerated engine drops that the oracle still
    finds."""
    rng = random.Random(1234)
    required_mm = 0.25
    total_violations = 0
    for trial in range(300):
        model = _random_model(rng, n_items=rng.randint(3, 9), span=4.0)
        naive = {
            (i, j): gap
            for i, j, gap in drc.clearance_violations_naive(
                model, required_mm=required_mm
            )
        }
        indexed = {
            (i, j): gap
            for i, j, gap in drc.clearance_pairs_indexed(model, required_mm=required_mm)
        }
        assert set(naive) == set(indexed), (trial, model, set(naive) ^ set(indexed))
        for key, gap in naive.items():
            assert gap == pytest.approx(indexed[key], abs=1e-4), (
                trial,
                key,
                gap,
                indexed[key],
            )
        total_violations += len(naive)
    # sanity: the randomized generator actually produced violations to compare
    assert total_violations > 20


def _random_real_board_model(
    rng: random.Random, *, n_nets: int, span: float
) -> dict[str, Any]:
    """A REAL board run through realize.py — instances, nets, some segments
    forced onto a non-pad layer so realize.py emits real
    :class:`~precis.pcb.realize.RealizedVia` objects, converted through the
    production :func:`~precis.pcb.realize.to_gerber_model` hand-off — NOT
    the synthetic ``_via`` dict helper the rest of this file uses. The
    randomized oracle-agreement property must exercise real vias, per this
    task's own requirement (the master backlog's via-geometry gap)."""
    instances: list[dict[str, Any]] = []
    nets: list[dict[str, Any]] = []
    for i in range(n_nets):
        a_ref, b_ref = f"U{2 * i}", f"U{2 * i + 1}"
        instances.append(
            {"refdes": a_ref, "x": rng.uniform(0, span), "y": rng.uniform(0, span)}
        )
        instances.append(
            {"refdes": b_ref, "x": rng.uniform(0, span), "y": rng.uniform(0, span)}
        )
        nets.append(
            {
                "name": f"N{i}",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": a_ref, "pin": "1"},
                    {"refdes": b_ref, "pin": "1"},
                ],
            }
        )
    ir = from_graph({"instances": instances, "nets": nets}, stackup=DEFAULT_STACKUP)
    for seg_id in range(ir.n_segments):
        ir.set_layer(seg_id, rng.choice([0, 1, 2, 3]))
    result = realize(
        ir, config=RealizeConfig(clearance_mm=0.15, fab_caps=_CAP4, router="tangent")
    )
    layers = [layer["name"] for layer in DEFAULT_STACKUP]
    outline = [[0.0, 0.0], [span, 0.0], [span, span], [0.0, span]]
    return to_gerber_model(result, ir, layers=layers, outline=outline)


def test_clearance_oracle_matches_strtree_engine_on_real_realized_vias():
    """The randomized oracle-agreement property, but over REAL production
    geometry (realize.py's actual vias via to_gerber_model), closing the
    task's own requirement that this test exercise real vias, not just the
    synthetic ``_via`` fixture the rest of this file uses."""
    rng = random.Random(4242)
    required_mm = 0.2
    total_vias = 0
    for trial in range(40):
        model = _random_real_board_model(rng, n_nets=rng.randint(3, 6), span=6.0)
        total_vias += sum(1 for c in model["copper"] if c["ctype"] == "via")
        n_copper = len(model["copper"])
        naive = {
            (i, j): gap
            for i, j, gap in drc.clearance_violations_naive(
                model, required_mm=required_mm
            )
        }
        # `to_gerber_model` (real production output) carries real pads, and
        # the oracle deliberately excludes pads from its closed-form
        # circle/capsule alphabet exactly like it already excludes pours
        # (module docstring) -- so a pad-involving pair is only ever found
        # by the indexed engine. Restricted here to the track/via-only
        # comparison this oracle actually claims to make; pad clearance
        # itself is covered directly in the pad-vs-copper tests above.
        indexed = {
            (i, j): gap
            for i, j, gap in drc.clearance_pairs_indexed(model, required_mm=required_mm)
            if i < n_copper and j < n_copper
        }
        assert set(naive) == set(indexed), (trial, set(naive) ^ set(indexed))
        for key, gap in naive.items():
            assert gap == pytest.approx(indexed[key], abs=1e-4)
    assert total_vias > 20  # sanity: real vias were actually produced and exercised


_BOARD = [[0.0, 0.0], [30.0, 0.0], [30.0, 20.0], [0.0, 20.0]]


def _containment(model: dict[str, Any], courtyards: Any = None) -> Any:
    return drc.check_outline_containment(model, outline=_BOARD, courtyards=courtyards)


def test_copper_on_the_board_is_not_a_containment_finding() -> None:
    model = {"layers": ["F.Cu"], "copper": [_track("N", "F.Cu", (5, 5), (25, 15))]}
    assert _containment(model) == []


def test_copper_off_the_board_is_an_error_edge_clearance_cannot_see() -> None:
    """The bug this rule exists for, stated as a comparison.

    ``check_board_edge_clearance`` measures ``boundary.distance(geom)`` —
    unsigned distance to the outline as a LINE. It is therefore symmetric
    about the edge and silent on anything far from it, on either side. A
    track 20mm off the board is not near the edge, so that rule passes it.
    """
    model = {"layers": ["F.Cu"], "copper": [_track("N", "F.Cu", (50, 50), (60, 55))]}
    assert drc.check_board_edge_clearance(model, _CAP4, outline=_BOARD) == []

    found = _containment(model)
    assert [f.rule for f in found] == ["outline_containment"]
    assert found[0].severity == "error"
    assert "entirely outside" in found[0].detail


def test_finding_count_rises_as_more_of_the_design_leaves_the_board() -> None:
    """The property that was inverted, and why it went unnoticed.

    Measured on the reference design before this rule: at a 20mm-square
    outline (24 of 29 parts off the board) DRC reported 10 errors; at 2mm
    (every pad off the board) it reported NINE. Fewer errors for a more
    broken board reads as "nearly fine" — the direction of the number was
    wrong, not just its magnitude.
    """
    near = {"layers": ["F.Cu"], "copper": [_track("N", "F.Cu", (5, 5), (25, 15))]}
    partly = {"layers": ["F.Cu"], "copper": [_track("N", "F.Cu", (25, 15), (35, 15))]}
    wholly = {"layers": ["F.Cu"], "copper": [_track("N", "F.Cu", (50, 50), (60, 55))]}
    assert [len(_containment(m)) for m in (near, partly, wholly)] == [0, 1, 1]
    assert "partly outside" in _containment(partly)[0].detail
    assert "entirely outside" in _containment(wholly)[0].detail


def test_a_pad_off_the_board_is_reported() -> None:
    """Pads matter most here: a pad off the board is a part that cannot be
    soldered, and pads are not in ``copper`` at all."""
    model = {
        "layers": ["F.Cu"],
        "copper": [],
        "pads": [
            {"layer": "F.Cu", "net": "N", "shape": "circle", "x": 5, "y": 5, "w": 0.9},
            {"layer": "F.Cu", "net": "N", "shape": "circle", "x": 40, "y": 5, "w": 0.9},
        ],
    }
    found = _containment(model)
    assert len(found) == 1
    assert "pad[N]" in found[0].where


def test_a_part_placed_off_the_board_is_reported() -> None:
    """The placer's seed shelf-packs from the origin; on an outline
    narrower than its natural row width it put parts straight off the edge,
    and ``bounds_for`` then clamped every move that could have rescued
    them. Nothing reported it."""
    found = _containment(
        {"layers": ["F.Cu"], "copper": []},
        courtyards=[("U1", _square(5.0, 5.0)), ("U2", _square(45.0, 5.0))],
    )
    assert [f.objects[0]["refdes"] for f in found] == ["U2"]


def test_containment_is_silent_with_no_outline() -> None:
    """No authored outline means no board edge to be outside of. Inventing
    one would constrain a design that never asked to be — the same call
    ``realize._outline_clip`` makes."""
    model = {
        "layers": ["F.Cu"],
        "copper": [_track("N", "F.Cu", (500, 500), (600, 550))],
    }
    assert drc.check_outline_containment(model, outline=None) == []


def test_clearance_oracle_matches_on_dense_close_layout():
    """A denser, tighter-packed variant (smaller span, more items) — biases
    toward many near-touching / crossing segments, the geometry regime most
    likely to expose an off-by-one in the segment-segment intersection
    special case."""
    rng = random.Random(99)
    required_mm = 0.3
    for trial in range(150):
        model = _random_model(rng, n_items=rng.randint(4, 8), span=1.5)
        naive = set(
            (i, j)
            for i, j, _ in drc.clearance_violations_naive(
                model, required_mm=required_mm
            )
        )
        indexed = set(
            (i, j)
            for i, j, _ in drc.clearance_pairs_indexed(model, required_mm=required_mm)
        )
        assert naive == indexed, (trial, model, naive ^ indexed)


# ── octilinear (every drawn wire a multiple of 45 degrees) ────────────


def test_check_octilinear_fires_on_an_off_angle_segment():
    """A 55-degree run is exactly the defect the 2026-08-31 user review
    reported ("R2->R1 at ~55deg") -- one error per offending segment,
    naming the angle so the fix starts from the finding."""
    model = {
        "layers": ["F.Cu"],
        "copper": [_track("A", "F.Cu", (0.0, 0.0), (7.0, 10.0))],
    }
    findings = drc.check_octilinear(model)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "octilinear" and f.severity == "error"
    assert "55.0deg" in f.detail


def test_check_octilinear_quiet_on_axis_diagonal_and_arcs():
    """Axis runs, true diagonals (|dx| == |dy|) and fillet arcs are all
    legal -- the rule grades LINE direction only."""
    arc_track = {
        "ctype": "track",
        "layer": "F.Cu",
        "net": "C",
        "width_mm": 0.2,
        "segments": [
            {
                "shape": "arc",
                "start": [0.0, 0.0],
                "end": [1.0, 1.0],
                "center": [1.0, 0.0],
                "cw": False,
            }
        ],
    }
    model = {
        "layers": ["F.Cu"],
        "copper": [
            _track("A", "F.Cu", (0.0, 0.0), (5.0, 0.0)),
            _track("A", "F.Cu", (5.0, 0.0), (8.0, 3.0)),
            _track("B", "F.Cu", (0.0, 1.0), (0.0, 6.0)),
            arc_track,
        ],
    }
    assert drc.check_octilinear(model) == []

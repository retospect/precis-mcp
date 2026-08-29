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


# ── courtyard overlap ─────────────────────────────────────────────────


def test_check_courtyard_overlap_fires_and_stays_quiet():
    overlapping = [("U1", 0.0, 0.0, 1.0), ("U2", 1.5, 0.0, 1.0)]  # 0.5mm overlap
    findings = drc.check_courtyard_overlap(overlapping)
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].margin_mm == pytest.approx(-0.5, abs=1e-6)

    clear = [("U1", 0.0, 0.0, 1.0), ("U2", 10.0, 0.0, 1.0)]
    assert drc.check_courtyard_overlap(clear) == []


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
        "copper": [_track("A", "F.Cu", (0, 0), (1, 0), width_mm=jlc_min / 2)],
    }
    findings = drc.run_geometric_drc(
        model,
        capability=_CAP4,
        outline=None,
        courtyards=[("U1", 0.0, 0.0, 1.0), ("U2", 0.5, 0.0, 1.0)],
    )
    rules = {f.rule for f in findings}
    assert "trace_width" in rules
    assert "courtyard_overlap" in rules


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
        courtyards=[("U1", 5.0, 5.0, 1.0), ("U2", 45.0, 5.0, 1.0)],
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

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

from precis.pcb import drc
from precis.pcb.capabilities import capability_for

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

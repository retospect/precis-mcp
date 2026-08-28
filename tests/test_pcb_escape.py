"""Unit tests for the footprint escape-routing precompute
(precis.pcb.escape) — shell decomposition, gap capacity arithmetic, and
required_layers. No DB, no network: pads are synthetic dicts in the exact
shape precis.pcb.easyeda.parse_component produces.
"""

from __future__ import annotations

import pytest

from precis.pcb.capabilities import capability_for
from precis.pcb.escape import (
    EscapeGap,
    EscapeGraph,
    compute_escape_graph,
    compute_gaps,
    compute_shells,
    escape_graph_from_dict,
    escape_graph_to_dict,
    gap_capacity,
    required_layers,
)

_CAP = capability_for("4layer")


def _pad(number: str, x: float, y: float, w: float = 0.4, h: float = 0.4) -> dict:
    return {
        "number": number,
        "shape": "RECT",
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "rot": 0.0,
        "layer": "F.Cu",
        "drill": None,
    }


def _soic8(pitch: float = 1.27, w: float = 0.6, h: float = 1.5) -> list[dict]:
    """A synthetic 8-pin perimeter package: two columns of 4 pads each,
    5mm apart (well clear of each other), pitch mm apart within a column —
    every pad sits on the outline, nothing interior."""
    pads = []
    for i in range(4):
        pads.append(_pad(f"L{i + 1}", 0.0, i * pitch, w=w, h=h))
    for i in range(4):
        pads.append(_pad(f"R{i + 1}", 5.0, i * pitch, w=w, h=h))
    return pads


def _bga_grid(n: int, pitch: float, pad_size: float = 0.4) -> list[dict]:
    """A synthetic n x n area-array package on an even grid."""
    pads = []
    for xi in range(n):
        for yi in range(n):
            pads.append(
                _pad(f"{xi}{yi}", xi * pitch, yi * pitch, w=pad_size, h=pad_size)
            )
    return pads


def _rotate90(pads: list[dict]) -> list[dict]:
    """Rotate an entire footprint 90 degrees: pad centers rotate
    ``(x, y) -> (-y, x)`` *and* each pad's own w/h swap (a pad that was
    tall becomes wide) — the whole rigid footprint turns, not just the
    centers. A rigid rotation is an isometry (preserves every pairwise
    distance), so :func:`compute_shells`/:func:`compute_gaps` — pure
    functions of pairwise geometry — must land on EXACTLY the same
    per-pad-number result before and after, not merely the same
    aggregate stats. That is the "rotation permutes [coordinates] but
    does not change [the escape graph]" decisions-log claim under test:
    what a placement rotation permutes is which *physical board
    direction* a given shell/gap sits toward, never the footprint-
    intrinsic escape graph itself."""
    return [{**p, "x": -p["y"], "y": p["x"], "w": p["h"], "h": p["w"]} for p in pads]


# ── gap_capacity: hand-computed arithmetic ───────────────────────────────
def test_gap_capacity_hand_computed():
    # (pitch - extent - 2*clearance) / (trace + clearance)
    # = (1.0 - 0.3 - 0.3) / (0.15 + 0.15) = 0.4 / 0.3 = 1.33 -> floor 1
    assert gap_capacity(1.0, 0.3, 0.15, 0.15) == 1
    # (0.8 - 0.4 - 0.3) / 0.3 = 0.1 / 0.3 = 0.33 -> floor 0
    assert gap_capacity(0.8, 0.4, 0.15, 0.15) == 0
    # negative free width floors at 0, not a negative capacity
    assert gap_capacity(0.5, 0.6, 0.15, 0.15) == 0
    # exact multiple: (1.6 - 0.4 - 2*0.3) / (0.3+0.3) = 0.6 / 0.6 = 1.0 -> 1
    assert gap_capacity(1.6, 0.4, 0.3, 0.3) == 1
    # (2.2 - 0.4 - 2*0.3) / (0.3+0.3) = 1.2 / 0.6 = 2.0 -> 2
    assert gap_capacity(2.2, 0.4, 0.3, 0.3) == 2


def test_gap_capacity_never_negative():
    assert gap_capacity(0.1, 5.0, 0.15, 0.15) == 0


# ── shell decomposition ───────────────────────────────────────────────────
def test_perimeter_package_all_shell_zero():
    shells = compute_shells(_soic8())
    assert len(shells) == 8
    assert set(shells.values()) == {0}


def test_bga_4x4_two_shells():
    shells = compute_shells(_bga_grid(4, pitch=0.8))
    assert len(shells) == 16
    counts: dict[int, int] = {}
    for s in shells.values():
        counts[s] = counts.get(s, 0) + 1
    # outer ring of a 4x4 grid = 12 pads, inner 2x2 = 4 pads
    assert counts == {0: 12, 1: 4}
    # the 4 interior positions are exactly the inner 2x2
    inner = {number for number, shell in shells.items() if shell == 1}
    assert inner == {"11", "12", "21", "22"}


def test_single_pad_is_shell_zero():
    shells = compute_shells([_pad("1", 0.0, 0.0)])
    assert shells == {"1": 0}


def test_empty_pads_gives_empty_shells():
    assert compute_shells([]) == {}


# ── gaps ────────────────────────────────────────────────────────────────
def test_compute_gaps_only_connects_nearest_neighbours():
    pads = _bga_grid(3, pitch=1.0)
    gaps = compute_gaps(pads, _CAP)
    # a 3x3 grid has 2*3 horizontal + 2*3... actually 3 rows * 2 h-gaps +
    # 3 cols * 2 v-gaps = 12 nearest-neighbour gaps; no diagonals (those
    # sit at sqrt(2) x pitch, outside the 1.05x adjacency threshold).
    assert len(gaps) == 12
    for gap in gaps:
        assert gap.pitch_mm == pytest.approx(1.0)


def test_compute_gaps_defaults_to_house_default_capability():
    pads = _bga_grid(2, pitch=1.2, pad_size=0.4)
    gaps = compute_gaps(pads)  # no capability arg -> 4-layer house default
    assert gaps
    for gap in gaps:
        # (1.2 - 0.4 - 2*0.15) / (0.15+0.15) = 0.5/0.3 -> floor 1
        assert gap.capacity == 1


# ── required_layers: the payoff ───────────────────────────────────────────
def test_soic_needs_one_layer():
    assert required_layers(_soic8()) == 1


def test_dense_fine_pitch_bga_needs_more_than_one_layer():
    # 6x6 grid, pitch 1.2mm, 0.4mm pads: escape demand at the outer ring
    # (all 36 pads must ultimately cross it) outruns that ring's own gap
    # capacity (20 boundary pads x 1 strand/gap), forcing >1 layer.
    pads = _bga_grid(6, pitch=1.2, pad_size=0.4)
    graph = compute_escape_graph(pads)
    assert graph.n_shells == 3
    assert graph.required_layers > 1


def test_required_layers_matches_compute_escape_graph():
    pads = _bga_grid(4, pitch=0.8)
    assert required_layers(pads) == compute_escape_graph(pads).required_layers


# ── rotation: an isometry, so the escape graph is invariant ─────────────
def test_rotation_does_not_change_bga_escape_graph():
    pads = _bga_grid(4, pitch=0.8, pad_size=0.4)
    rotated = _rotate90(pads)

    graph = compute_escape_graph(pads)
    graph_r = compute_escape_graph(rotated)

    assert graph.required_layers == graph_r.required_layers
    # exact equality per pad number, not just the same aggregate stats —
    # a rigid rotation is an isometry, so shell membership (a pure
    # function of pairwise distances) cannot move at all.
    assert graph.shells == graph_r.shells
    assert graph.per_shell_capacity == graph_r.per_shell_capacity
    assert sorted(g.capacity for g in graph.gaps) == sorted(
        g.capacity for g in graph_r.gaps
    )


def test_rotation_does_not_change_soic_escape_graph():
    pads = _soic8()
    rotated = _rotate90(pads)
    assert compute_shells(pads) == compute_shells(rotated)
    assert required_layers(pads) == required_layers(rotated) == 1


# ── serialization round-trip ──────────────────────────────────────────────
def test_escape_graph_dict_round_trip():
    pads = _bga_grid(4, pitch=0.8)
    graph = compute_escape_graph(pads)
    data = escape_graph_to_dict(graph)
    # jsonb round-trip: keys must be strings
    assert all(isinstance(k, str) for k in data["per_shell_capacity"])
    restored = escape_graph_from_dict(data)
    assert restored == graph


def test_escape_graph_round_trip_preserves_gap_objects():
    pads = _soic8()
    graph = compute_escape_graph(pads)
    restored = escape_graph_from_dict(escape_graph_to_dict(graph))
    assert restored.gaps == graph.gaps
    assert all(isinstance(g, EscapeGap) for g in restored.gaps)
    assert isinstance(restored, EscapeGraph)


# ── capability handling: None fields must raise, not silently 0 ─────────
def test_missing_capability_field_raises_not_zero():
    from dataclasses import replace

    broken = replace(_CAP, house_default={**_CAP.house_default, "trace_width_mm": None})
    with pytest.raises(ValueError, match="trace_width_mm"):
        compute_gaps(_bga_grid(2, pitch=0.8), broken)

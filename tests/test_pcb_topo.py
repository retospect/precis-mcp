"""CDT region graph and physical portal capacities — the slice-5 probe of
docs/backlog/pcb-vector-router.md. Hand-built layouts with known gaps
first, then the comparison against the flow tile model on the QFP plaza
member and the EWOD dogfood-6 replay (slow): for each connection, does a
portal sequence exist, and where the tile model reads 0 lanes at a
terminal, does the CDT still find one?"""

from __future__ import annotations

from typing import Any

import pytest
from shapely.geometry import Point, box

from precis.pcb import rules, topo
from precis.pcb.capabilities import capability_for

W = 0.1  # track
S = 0.1  # clearance
OUTLINE = [[0.0, 0.0], [10.0, 0.0], [10.0, 6.0], [0.0, 6.0]]


def _ctx(net_rules: dict[str, rules.NetRules] | None = None) -> rules.RuleContext:
    """A minimal rule context: the 2-layer fab row, the given per-net
    resolved rules, both copper layers outer (the IPC column split)."""
    return rules.RuleContext(
        fab_caps=capability_for("2layer"),
        net_rules=net_rules or {},
        outer_layers=frozenset({"F.Cu", "B.Cu"}),
        coated=True,
    )


def _ob(net: str, geom) -> topo.Obstacle:
    return topo.Obstacle(net, "pad", topo.outward(geom))


def _graph(obstacles) -> topo.RegionGraph:
    return topo.build_region_graph(
        obstacles, OUTLINE, "F.Cu", track_mm=W, clearance_mm=S
    )


def test_portal_capacity_counts_tracks_with_clearance_both_sides():
    assert topo.portal_capacity(S + 3 * (W + S), W, S) == 3  # exact fit
    assert topo.portal_capacity(S + 3 * (W + S) - 1e-3, W, S) == 2
    assert topo.portal_capacity(0.05, W, S) == 0
    with pytest.raises(ValueError, match="positive"):
        topo.portal_capacity(1.0, 0.0, 0.0)


def test_outward_contains_the_exact_shape_within_delta():
    exact = Point(0.0, 0.0).buffer(0.3, quad_segs=256)
    out = topo.outward(exact)
    assert out.buffer(1e-9).contains(exact)
    assert out.hausdorff_distance(exact) <= topo.DELTA_MM + 1e-9
    # Far fewer vertices than the 1024-gon it came from.
    assert len(out.exterior.coords) < 200


def test_gap_between_two_pads_is_a_portal_of_that_length():
    # S + 3 lanes: exactly 3 tracks on the exact shapes; the outward
    # drawing takes up to delta off the gap, so the probe reads 2.
    gap = S + 3 * (W + S)
    a = _ob("A", box(2.0, 2.0, 3.0, 3.0))
    b = _ob("B", box(3.0 + gap, 2.0, 4.0 + gap, 3.0))
    g = _graph([a, b])
    between = [p for p in g.portals if set(p.ends) == {0, 1}]
    assert between
    shortest = min(between, key=lambda p: p.length_mm)
    # The outward drawing narrows the gap by at most delta.
    assert gap - topo.DELTA_MM - 1e-9 <= shortest.length_mm <= gap
    assert shortest.capacity == topo.portal_capacity(shortest.length_mm, W, S) == 2
    assert all(p.capacity >= 0 for p in g.portals)
    assert sum(g.capacity_histogram().values()) == len(g.portals)


def _walled(slit_mm: float | None) -> tuple[topo.RegionGraph, int, int]:
    """Pad A left, pad B right, a full-height wall between them with an
    optional slit of ``slit_mm`` centred at y = 3."""
    obs = [_ob("A", box(1.0, 2.5, 2.0, 3.5)), _ob("B", box(8.0, 2.5, 9.0, 3.5))]
    if slit_mm is None:
        obs.append(_ob("W", box(4.9, -1.0, 5.1, 7.0)))
    else:
        obs.append(_ob("W", box(4.9, -1.0, 5.1, 3.0 - slit_mm / 2)))
        obs.append(_ob("W", box(4.9, 3.0 + slit_mm / 2, 5.1, 7.0)))
    return _graph(obs), 0, 1


def test_a_full_wall_has_no_portal_sequence():
    g, a, b = _walled(None)
    assert g.portal_path(a, b) is None


def test_a_slit_narrower_than_one_lane_blocks_and_a_wider_one_passes():
    narrow, a, b = _walled(0.25)  # < S + (W + S) = 0.3
    assert narrow.portal_path(a, b) is None
    wide, a, b = _walled(0.5)
    path = wide.portal_path(a, b)
    assert path is not None and path
    assert all(wide.portals[i].capacity >= 1 for i in path)
    # Asking for more lanes than the slit holds closes it again.
    assert wide.portal_path(a, b, min_capacity=2) is None


def test_board_obstacles_follow_layers_drills_and_via_spans():
    model: dict[str, Any] = {
        "layers": ["F.Cu", "In1.Cu", "B.Cu"],
        "pads": [
            {
                "layer": "F.Cu",
                "net": "A",
                "shape": "rect",
                "x": 1.0,
                "y": 1.0,
                "w": 0.5,
                "h": 1.0,
            },
            {
                "layer": "F.Cu",
                "net": "B",
                "shape": "circle",
                "x": 3.0,
                "y": 1.0,
                "w": 1.0,
                "drill": 0.5,
            },
        ],
        "copper": [
            {
                "ctype": "via",
                "net": "C",
                "x": 5.0,
                "y": 1.0,
                "dia_mm": 0.45,
                "span": ["F.Cu", "In1.Cu"],
            },
            {
                "ctype": "track",
                "net": "D",
                "layer": "B.Cu",
                "width_mm": 0.2,
                "segments": [{"shape": "line", "start": [0.0, 3.0], "end": [4.0, 3.0]}],
            },
        ],
    }
    nets = {
        lay: sorted(o.net for o in topo.board_obstacles(model, lay))
        for lay in model["layers"]
    }
    assert nets == {"F.Cu": ["A", "B", "C"], "In1.Cu": ["B", "C"], "B.Cu": ["B", "D"]}


def test_summarize_counts_reachable_connections():
    g, _a, _b = _walled(0.5)
    blocked, _, _ = _walled(None)
    conn = topo.Connection(
        "A", topo.Terminal(1.5, 3.0, ("F.Cu",)), topo.Terminal(1.5, 3.0, ("F.Cu",))
    )
    across = topo.Connection(
        "A", topo.Terminal(1.5, 3.0, ("F.Cu",)), topo.Terminal(8.5, 3.0, ("F.Cu",))
    )
    # Terminals resolve by net: B's pad is not net A, so `across` has no
    # destination obstacle and is unreachable on any graph.
    out = topo.summarize({"F.Cu": g}, [conn, across])
    assert out["connections"] == 2
    assert out["reachable"] == 1
    assert out["unreachable"] == ["A"]
    assert out["layers"]["F.Cu"]["portals"] == len(g.portals)
    assert topo.summarize({"F.Cu": blocked}, [conn])["reachable"] == 1

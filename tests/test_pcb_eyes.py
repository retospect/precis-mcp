"""Pure unit tests for the PCB eyes — geometry, ratsnest +
crossing count, signal trace, measures. No DB.

``drc_lite`` is retired (pcb-guided-place-route Slice 8, superseded by
:mod:`precis.pcb.drc` behind ``view='drc'``) — see ``tests/test_pcb_drc.py``.
"""

from __future__ import annotations

from precis.pcb import eyes, ratsnest
from precis.pcb.geom import segments_cross, shares_endpoint


# ── geometry ─────────────────────────────────────────────────────────
def test_segments_cross_proper():
    assert segments_cross((0, 0), (2, 2), (0, 2), (2, 0)) is True  # an X


def test_segments_parallel_dont_cross():
    assert segments_cross((0, 0), (2, 0), (0, 1), (2, 1)) is False


def test_shared_endpoint_is_not_a_crossing():
    # two wires fanning from a common pin/component — not a crossing
    assert shares_endpoint((0, 0), (1, 1), (0, 0), (1, -1)) is True
    assert segments_cross((0, 0), (1, 1), (0, 0), (1, -1)) is False


def test_touch_without_crossing_is_false():
    # T-junction: endpoint of one lies on the other but doesn't straddle
    assert segments_cross((0, 0), (2, 0), (1, 0), (1, 2)) is False


# ── ratsnest + crossings ─────────────────────────────────────────────
def _net(name, cls, *refdes):
    return {"name": name, "net_class": cls, "members": [{"refdes": r} for r in refdes]}


def test_ratsnest_mst_edges_count():
    # 3 instances on one net → MST has 2 edges
    placed: dict[str, tuple[float, float]] = {
        "U1": (0, 0),
        "U2": (10, 0),
        "U3": (20, 0),
    }
    wires = ratsnest.build_airwires(placed, [_net("D", "signal", "U1", "U2", "U3")])
    assert len(wires) == 2


def test_plane_nets_excluded_from_ratsnest():
    placed: dict[str, tuple[float, float]] = {
        "U1": (0, 0),
        "C1": (10, 0),
        "C2": (20, 0),
    }
    wires = ratsnest.build_airwires(placed, [_net("GND", "gnd", "U1", "C1", "C2")])
    assert wires == []  # gnd is a plane net → no airwires


def test_unplaced_parts_excluded():
    placed: dict[str, tuple[float, float]] = {"U1": (0, 0)}  # U2 unplaced
    wires = ratsnest.build_airwires(placed, [_net("D", "signal", "U1", "U2")])
    assert wires == []  # only one placed member → no airwire


def test_crossing_count_two_nets():
    # two signal nets whose airwires form an X
    placed: dict[str, tuple[float, float]] = {
        "A": (0, 0),
        "B": (2, 2),
        "C": (0, 2),
        "D": (2, 0),
    }
    nets = [_net("N1", "signal", "A", "B"), _net("N2", "signal", "C", "D")]
    wires = ratsnest.build_airwires(placed, nets)
    assert len(ratsnest.crossings(wires)) == 1


def test_no_crossing_when_separated():
    placed: dict[str, tuple[float, float]] = {
        "A": (0, 0),
        "B": (1, 0),
        "C": (5, 0),
        "D": (6, 0),
    }
    nets = [_net("N1", "signal", "A", "B"), _net("N2", "signal", "C", "D")]
    wires = ratsnest.build_airwires(placed, nets)
    assert ratsnest.crossings(wires) == []


# ── side-aware MST (gripe 449579) ──────────────────────────────────────
def _edge_set(wires: list[ratsnest.Airwire]) -> set[frozenset[str]]:
    return {frozenset((w.a, w.b)) for w in wires}


def test_side_crossing_net_prefers_via_minimal_tree():
    # ewod-dogfood-2's reported case, collinear on x=20: J_INSTR (top) @
    # y=5, U_TEMP (bottom) @ y=0, R_BLEED (top) @ y=-6. Pure-Euclidean MST
    # chains J_INSTR-U_TEMP-U_TEMP-R_BLEED (5mm+6mm=11mm, shortest 2D tree)
    # through BOTH sides — 2 vias. The via-minimal tree is J_INSTR-R_BLEED
    # direct (11mm, one side, 0 vias) plus a J_INSTR-U_TEMP spur (1 via).
    placed: dict[str, tuple[float, float]] = {
        "J_INSTR": (20.0, 5.0),
        "U_TEMP": (20.0, 0.0),
        "R_BLEED": (20.0, -6.0),
    }
    net = _net("SIG", "signal", "J_INSTR", "U_TEMP", "R_BLEED")
    bottom = frozenset({"U_TEMP"})

    # Red before green: side-blind (no `bottom`) reproduces the reported bug.
    blind = ratsnest.build_airwires(placed, [net])
    assert _edge_set(blind) == {
        frozenset({"J_INSTR", "U_TEMP"}),
        frozenset({"U_TEMP", "R_BLEED"}),
    }

    aware = ratsnest.build_airwires(placed, [net], bottom=bottom)
    assert _edge_set(aware) == {
        frozenset({"J_INSTR", "R_BLEED"}),
        frozenset({"J_INSTR", "U_TEMP"}),
    }
    # Exactly one side-crossing edge in the via-minimal tree (J_INSTR-
    # U_TEMP), vs. two in the side-blind tree above.
    n_crossing = sum(1 for w in aware if (w.a in bottom) != (w.b in bottom))
    assert n_crossing == 1


def test_side_crossing_net_reordered_members_same_result():
    # Member order shouldn't matter — Prim's always starts at index 0, so
    # this pins that the fix isn't an artifact of the reported case's
    # particular authored member order.
    placed: dict[str, tuple[float, float]] = {
        "U_TEMP": (20.0, 0.0),
        "R_BLEED": (20.0, -6.0),
        "J_INSTR": (20.0, 5.0),
    }
    net = _net("SIG", "signal", "U_TEMP", "R_BLEED", "J_INSTR")
    bottom = frozenset({"U_TEMP"})
    aware = ratsnest.build_airwires(placed, [net], bottom=bottom)
    assert _edge_set(aware) == {
        frozenset({"J_INSTR", "R_BLEED"}),
        frozenset({"J_INSTR", "U_TEMP"}),
    }


def test_single_side_net_unaffected_by_bottom_set():
    # Common case: no side-crossing available in this net at all (R_BLEED
    # isn't even a member) — the via term must never perturb a pure
    # single-side tree, `bottom` given or not.
    placed: dict[str, tuple[float, float]] = {
        "U1": (0.0, 0.0),
        "U2": (10.0, 0.0),
        "U3": (20.0, 0.0),
    }
    net = _net("D", "signal", "U1", "U2", "U3")
    blind = ratsnest.build_airwires(placed, [net])
    with_unrelated_bottom = ratsnest.build_airwires(
        placed, [net], bottom=frozenset({"U1", "U2", "U3"})
    )
    assert _edge_set(blind) == _edge_set(with_unrelated_bottom)
    assert _edge_set(blind) == {
        frozenset({"U1", "U2"}),
        frozenset({"U2", "U3"}),
    }


def test_mst_tie_break_is_deterministic():
    # A unit square: A-B and A-D both tie at distance 1 for the first Prim's
    # step, and B-C/D-C tie at distance 1 for the last — the nested-loop
    # "first strict `<` wins" rule must pick the SAME edge set every time,
    # unperturbed by the new via term (`bottom` omitted, so it's a no-op).
    placed: dict[str, tuple[float, float]] = {
        "A": (0.0, 0.0),
        "B": (1.0, 0.0),
        "C": (1.0, 1.0),
        "D": (0.0, 1.0),
    }
    net = _net("SQ", "signal", "A", "B", "C", "D")
    expected = {
        frozenset({"A", "B"}),
        frozenset({"A", "D"}),
        frozenset({"B", "C"}),
    }
    for _ in range(5):
        assert _edge_set(ratsnest.build_airwires(placed, [net])) == expected


# ── signal trace ─────────────────────────────────────────────────────
def test_trace_hops_through_series_resistor():
    # NET_A -- R1(2-pin) -- NET_B -- U1(multi-pin, terminus)
    graph = {
        "instances": [
            {
                "refdes": "R1",
                "x": 0,
                "y": 0,
                "roles": [],
                "label": "4.7k",
                "height_mm": None,
                "n_pins": 2,
            },
            {
                "refdes": "U1",
                "x": 5,
                "y": 0,
                "roles": [],
                "label": "MCU",
                "height_mm": None,
                "n_pins": 8,
            },
        ],
        "nets": [
            {
                "name": "NET_A",
                "net_class": "signal",
                "members": [{"refdes": "R1", "pin": "1"}],
            },
            {
                "name": "NET_B",
                "net_class": "signal",
                "members": [{"refdes": "R1", "pin": "2"}, {"refdes": "U1", "pin": "7"}],
            },
        ],
        "unconnected": [],
    }
    tr = eyes.trace(graph, "NET_A")
    path_nets = [p["net"] for p in tr["path"]]
    assert path_nets == ["NET_A", "NET_B"]  # hopped through R1
    assert "U1.7" in tr["ends"]  # terminates at the multi-pin part


# ── measures ─────────────────────────────────────────────────────────
def _graph_two(a_xy, b_xy, *, a_roles=("sensitive",), b_roles=("noisy",)):
    return {
        "instances": [
            {
                "refdes": "U1",
                "x": a_xy[0],
                "y": a_xy[1],
                "roles": list(a_roles),
                "label": "opamp",
                "height_mm": 1.0,
                "n_pins": 8,
            },
            {
                "refdes": "Q1",
                "x": b_xy[0],
                "y": b_xy[1],
                "roles": list(b_roles),
                "label": "FET",
                "height_mm": 2.5,
                "n_pins": 3,
            },
        ],
        "nets": [],
        "unconnected": [],
    }


def test_separation_measure_ok_and_violated():
    m = [
        {
            "metric": "separation",
            "goal": 10.0,
            "strength": "soft",
            "operands": [{"role": "sensitive"}, {"role": "noisy"}],
            "reason": "EMI",
        }
    ]
    far = eyes.evaluate_measures(_graph_two((0, 0), (20, 0)), m)
    assert far[0]["verdict"] == "ok" and far[0]["value"] == 20.0
    near = eyes.evaluate_measures(_graph_two((0, 0), (5, 0)), m)
    assert near[0]["verdict"] == "VIOLATED" and near[0]["value"] == 5.0


def test_height_measure():
    m = [
        {
            "metric": "height",
            "goal": 2.0,
            "strength": "gauge",
            "operands": [{"instance": "Q1"}],
            "reason": "lid clearance",
        }
    ]
    res = eyes.evaluate_measures(_graph_two((0, 0), (5, 0)), m)
    assert res[0]["value"] == 2.5 and res[0]["verdict"] == "VIOLATED"


def test_direction_flips_height_verdict():
    # keep_above: "must be at least 3mm tall" — Q1 is 2.5 → VIOLATED, and the
    # same part under the default ceiling sense with goal 3 would be ok.
    m = [
        {
            "metric": "height",
            "goal": 3.0,
            "strength": "gauge",
            "direction": "keep_above",
            "operands": [{"instance": "Q1"}],
            "reason": "heatsink contact",
        }
    ]
    res = eyes.evaluate_measures(_graph_two((0, 0), (5, 0)), m)
    assert res[0]["verdict"] == "VIOLATED" and res[0]["value"] == 2.5
    m[0]["direction"] = None  # default = ceiling → 2.5 ≤ 3.0 is ok
    res = eyes.evaluate_measures(_graph_two((0, 0), (5, 0)), m)
    assert res[0]["verdict"] == "ok"


def test_direction_keep_below_separation():
    # "gap must stay below 5mm" — the binding pair is the furthest one.
    m = [
        {
            "metric": "separation",
            "goal": 5.0,
            "strength": "gauge",
            "direction": "keep_below",
            "operands": [{"role": "sensitive"}, {"role": "noisy"}],
            "reason": "shared shield can",
        }
    ]
    near = eyes.evaluate_measures(_graph_two((0, 0), (3, 0)), m)
    assert near[0]["verdict"] == "ok" and near[0]["value"] == 3.0
    far = eyes.evaluate_measures(_graph_two((0, 0), (20, 0)), m)
    assert far[0]["verdict"] == "VIOLATED" and far[0]["value"] == 20.0


def test_direction_target_tolerance():
    m = [
        {
            "metric": "proximity",
            "goal": 10.0,
            "strength": "gauge",
            "direction": "target",
            "operands": [{"instance": "U1"}, {"instance": "Q1"}],
            "reason": "antenna spacing",
        }
    ]
    on = eyes.evaluate_measures(_graph_two((0, 0), (10.5, 0)), m)
    assert on[0]["verdict"] == "ok"  # within ±10%
    off = eyes.evaluate_measures(_graph_two((0, 0), (15, 0)), m)
    assert off[0]["verdict"] == "VIOLATED"


def test_connectivity_metric_is_pending():
    m = [{"metric": "supply_path", "operands": [], "strength": "gauge"}]
    res = eyes.evaluate_measures(_graph_two((0, 0), (5, 0)), m)
    assert res[0]["verdict"] == "pending"

"""Pure unit tests for the PCB placer + route-feasibility (ADR 0042 §9).
No DB.
"""

from __future__ import annotations

from precis.pcb import place, ratsnest


def _net(name, cls, *refdes):
    return {"name": name, "net_class": cls, "members": [{"refdes": r} for r in refdes]}


def _inst(refdes, x=None, y=None, fixed=None, roles=()):
    return {"refdes": refdes, "x": x, "y": y, "fixed": fixed, "roles": list(roles)}


# ── autoplace ────────────────────────────────────────────────────────
def test_autoplace_removes_a_crossing():
    # The X: N1=A-B, N2=C-D placed so the airwires cross. The placer should
    # find a crossing-free arrangement of the 4 free parts.
    instances = [
        _inst("A", 0, 0),
        _inst("B", 2, 2),
        _inst("C", 0, 2),
        _inst("D", 2, 0),
    ]
    nets = [_net("N1", "signal", "A", "B"), _net("N2", "signal", "C", "D")]
    res = place.autoplace(instances, nets, iters=2000, seed=1)
    assert res.crossings_before == 1
    assert res.crossings_after == 0
    assert res.objective_after <= res.objective_before


def test_autoplace_respects_fixed():
    # A fixed part must keep its exact coordinates.
    instances = [
        _inst("J1", 0, 0, fixed="xy"),
        _inst("U1", 50, 50),
        _inst("U2", 51, 50),
    ]
    nets = [_net("N", "signal", "U1", "U2")]
    res = place.autoplace(instances, nets, iters=300, seed=2)
    assert res.positions["J1"] == (0.0, 0.0)  # never moved


def test_autoplace_is_deterministic():
    instances = [_inst("A", 0, 0), _inst("B", 2, 2), _inst("C", 0, 2), _inst("D", 2, 0)]
    nets = [_net("N1", "signal", "A", "B"), _net("N2", "signal", "C", "D")]
    r1 = place.autoplace(instances, nets, iters=500, seed=7)
    r2 = place.autoplace(instances, nets, iters=500, seed=7)
    assert r1.positions == r2.positions


def test_autoplace_pulls_soft_separation():
    # opamp (sensitive) starts next to the FET (noisy); a soft separation
    # measure with goal 30 should push them apart.
    instances = [
        _inst("U1", 0, 0, roles=["sensitive"]),
        _inst("Q1", 2, 0, roles=["noisy"]),
    ]
    nets: list[dict] = []
    measures = [
        {
            "metric": "separation",
            "goal": 30.0,
            "strength": "soft",
            "weight": 1.0,
            "operands": [{"role": "sensitive"}, {"role": "noisy"}],
        }
    ]
    res = place.autoplace(instances, nets, measures=measures, iters=2000, seed=3)
    from precis.pcb.geom import dist

    gap = dist(res.positions["U1"], res.positions["Q1"])
    assert gap > 2.0  # moved further apart than the 2mm start


def test_autoplace_zero_weight_measure_is_inert():
    # weight=0 records the measure without letting it steer: the two parts
    # (no nets, nothing else in the objective) must NOT be pushed apart.
    instances = [
        _inst("U1", 0, 0, roles=["sensitive"]),
        _inst("Q1", 2, 0, roles=["noisy"]),
    ]
    measures = [
        {
            "metric": "separation",
            "goal": 30.0,
            "strength": "soft",
            "weight": 0,
            "operands": [{"role": "sensitive"}, {"role": "noisy"}],
        }
    ]
    res = place.autoplace(instances, [], measures=measures, iters=500, seed=3)
    assert res.objective_before == 0.0  # zero-weight penalty contributes nothing
    assert res.objective_after == 0.0


def test_autoplace_direction_flips_measure_pull():
    # keep_below on separation = "stay within 5mm": parts seeded far apart
    # must be pulled together, the opposite of the default keep-apart sense.
    instances = [
        _inst("U1", 0, 0, roles=["sensitive"]),
        _inst("Q1", 30, 0, roles=["noisy"]),
    ]
    measures = [
        {
            "metric": "separation",
            "goal": 5.0,
            "strength": "soft",
            "direction": "keep_below",
            "operands": [{"role": "sensitive"}, {"role": "noisy"}],
        }
    ]
    res = place.autoplace(instances, [], measures=measures, iters=2000, seed=3)
    from precis.pcb.geom import dist

    gap = dist(res.positions["U1"], res.positions["Q1"])
    assert gap < 30.0  # pulled together, not pushed further apart


# ── route feasibility ────────────────────────────────────────────────
def test_route_feasibility_splits_h_v_and_estimates_vias():
    # one horizontal + one vertical airwire that cross at the origin region;
    # on different layers they don't conflict → 0 residual.
    placed: dict[str, tuple[float, float]] = {
        "A": (0, 0),
        "B": (10, 0),
        "C": (5, -5),
        "D": (5, 5),
    }
    nets = [_net("H", "signal", "A", "B"), _net("V", "signal", "C", "D")]
    wires = ratsnest.build_airwires(placed, nets)
    f = place.route_feasibility(wires)
    assert f["h_layer"] == 1 and f["v_layer"] == 1
    assert f["residual_crossings"] == 0  # H and V on separate layers
    assert f["vias_estimate"] == 0

"""Bounded physical witnesses and current-search measurements, without new search."""

from __future__ import annotations

import json
from collections import Counter

import pytest
from shapely.geometry import LineString, Polygon

from precis.pcb import drc, realize
from precis.pcb.capabilities import capability_for
from precis.pcb.topoplace import wires as wm
from precis.pcb.topoplace.realise import _plan_failed_nets, realize_result, route_design
from tests.fixtures.pcb.joint_corridors import abc_chain, parallel_lanes


def _errors(result, ir, footprints):
    model = realize.to_gerber_model(
        result,
        ir,
        layers=[r["name"] for r in ir.stackup],
        outline=ir.outline,
        footprints=footprints,
    )
    return [
        f
        for f in drc.run_geometric_drc(
            model, capability=capability_for("2layer"), outline=model["outline"]
        )
        if f.severity == "error"
    ]


@pytest.mark.parametrize("make", [abc_chain, parallel_lanes], ids=["abc", "parallel"])
def test_simultaneous_witness_has_connectivity_and_physical_clearance(make):
    fixture = make()
    td, ir, footprints, rules, plan = fixture.inputs()
    # The ordinary emitter and shared DRC include the actual terminal pads.
    result = realize_result(plan, td, ir, ir.n_layers, net_rules=rules)
    assert len(result.tracks) == len(fixture.widths)
    assert _errors(result, ir, footprints) == []
    # Keepouts are checked independently as complete swept copper bodies.
    for piece in plan.pieces:
        radius = fixture.widths[td.net_name[piece.net]] / 2
        for wall in fixture.walls:
            assert (
                LineString(piece.points).distance(Polygon(wall)) >= radius + 0.15 - 1e-9
            )


@pytest.mark.parametrize(
    "make",
    [abc_chain, parallel_lanes, lambda: parallel_lanes(overfull=True)],
    ids=["abc", "parallel", "overfull"],
)
def test_current_search_baseline_records_full_failure_set(make, monkeypatch):
    fixture = make()
    td, ir, footprints, rules, _witness = fixture.inputs()
    grows: Counter[int] = Counter()
    original = wm._grow_net

    def counted(*args, **kwargs):
        grows[int(args[4])] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(wm, "_grow_net", counted)
    plan, result = route_design(td, ir, net_rules=rules, footprints=footprints)
    failed = sorted(td.net_name[k] for k in _plan_failed_nets(plan))
    errors = _errors(result, ir, footprints)
    report = {
        "fixture": fixture.name,
        "failed": failed,
        "grow_calls_by_net": dict(grows),
        "grow_calls": sum(grows.values()),
        "shove_budget_per_net": wm._SHOVE_GROWS,
        "drc_errors": dict(Counter(f.rule for f in errors)),
        "tracks": len(result.tracks),
        "vias": len(result.vias),
    }
    print(json.dumps(report, sort_keys=True))
    assert wm._SHOVE_GROWS == 20
    # Counting includes ordinary growth and every retry, not only S2's counter.
    assert sum(grows.values()) <= 20 * len(td.net_pins)
    if "overfull" in fixture.name:
        assert failed
    else:
        assert not failed, report
        assert errors == [], report
        for track in result.tracks:
            for segment in track.segments:
                line = LineString((segment["start"], segment["end"]))
                for wall in fixture.walls:
                    assert (
                        line.distance(Polygon(wall)) >= track.width_mm / 2 + 0.15 - 1e-9
                    )
        if fixture.name == "abc-chain":
            d_id = list(ir.net_name).index("D")
            d_tracks = [track for track in result.tracks if track.net_id == d_id]
            assert len(d_tracks) == 1
            assert d_tracks[0].segments == (
                {
                    "shape": "line",
                    "start": [3.0, 13.5],
                    "end": [27.0, 13.5],
                },
            )


def test_abc_bc_ad_order_observes_real_initial_corridors(monkeypatch):
    """Order alone may not create the proposed B1/C2 incumbent."""
    fixture = abc_chain()
    td, ir, footprints, rules, _ = fixture.inputs()
    order = [list(td.net_name).index(name) for name in ("B", "C", "A", "D")]
    monkeypatch.setattr(wm, "_net_order", lambda *_args: order)
    original_grow = wm._grow_net
    original_swap = wm._Shove._swap
    initial = {}
    grows: Counter[str] = Counter()
    swaps = []

    def observed_grow(*args, **kwargs):
        k = int(args[4])
        grows[td.net_name[k]] += 1
        attempt = original_grow(*args, **kwargs)
        if td.net_name[k] not in initial:
            cuts = []
            for built in attempt.built:
                crossing = LineString(built.branch.points).intersection(
                    LineString(((15.0, 0.0), (15.0, 15.0)))
                )
                if crossing.geom_type == "Point":
                    cuts.append(crossing.y)
            initial[td.net_name[k]] = cuts
        return attempt

    def observed_swap(self, state, k, partners, *args, **kwargs):
        swaps.append([td.net_name[k], [td.net_name[p] for p in partners]])
        return original_swap(self, state, k, partners, *args, **kwargs)

    monkeypatch.setattr(wm, "_grow_net", observed_grow)
    monkeypatch.setattr(wm._Shove, "_swap", observed_swap)
    plan, result = route_design(td, ir, net_rules=rules, footprints=footprints)
    failed = sorted(td.net_name[k] for k in _plan_failed_nets(plan))
    print(
        json.dumps(
            {
                "fixture": "abc-bcad-order",
                "order": [td.net_name[k] for k in order],
                "initial_gate_y": initial,
                "grow_calls_by_net": dict(grows),
                "failed": failed,
                "shove_swap_partners": swaps,
                "controlled_B1_C2": initial.get("B") == [3.0]
                and initial.get("C") == [6.0],
            },
            sort_keys=True,
        )
    )
    assert not failed
    assert _errors(result, ir, footprints) == []
    assert sum(grows.values()) <= 20 * len(td.net_pins)

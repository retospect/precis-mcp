"""Fixed-pose dogfood routed-count regression; no provider or live board writes."""

from __future__ import annotations

import math
from dataclasses import asdict, replace
from itertools import combinations, permutations
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import pinswap, realize, session
from precis.pcb.capabilities import capability_for
from precis.pcb.drc import process_for_stackup
from precis.pcb.snapshot import load_snapshot, read_snapshot
from precis.workers.job_types import pcb_route
from precis.workers.job_types.pcb_route import (
    _apply_pin_swap_warm_start,
    _graph_net_rules,
    _graph_net_voltages,
    _resolve_pin_swap_groups,
)


def _hydrate(store):
    fixture = read_snapshot(Path("tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz"))
    with store.pool.connection() as conn:
        ref_id, _ = load_snapshot(conn, fixture, "ewod-dogfood-6-escape-regression")
    graph = store.pcb_graph(ref_id)
    features = store.pcb_features_list(ref_id)
    local = store.pcb_local_footprints_for(ref_id)
    cached = store.pcb_footprints_for(ref_id)
    fixed = store.pcb_fixed_copper_list(graph["board"]["board_id"])
    ir = session.build_ir(
        graph,
        outline=session.outline_from_features(features),
        mounting_holes=session.mounting_holes_from_features(features),
        footprints_by_lcsc=cached,
        local_footprints_by_name=local,
        fixed_copper=fixed,
    )
    session.apply_pin_swap_overrides(ir, store.pcb_pin_swaps_list(ref_id))
    session.apply_route_overrides(ir, store.pcb_routes_get(ref_id))
    footprints = session.footprints_by_refdes(
        ir,
        cached,
        local_footprints_by_name=local,
        local_names_by_refdes=session.local_footprint_names_by_refdes(graph),
    )
    config = realize.RealizeConfig(
        fab_caps=capability_for(process_for_stackup(ir.stackup)),
        class_rules=graph["net_classes"],
    )
    return ir, graph, features, footprints, fixed, config


def _failed(ir, result):
    return {str(ir.net_name[int(ir.seg_net[s])]) for s in result.unrouted}


def _assert_legal(
    ir, graph, features, footprints, fixed, config, result, layers=("B.Cu",)
):
    findings = session.routed_drc_findings(
        ir,
        result,
        capability=config.fab_caps,
        footprints=footprints,
        fixed_copper=fixed,
        outline=session.outline_from_features(features),
        net_rules=_graph_net_rules(graph, config.fab_caps),
        net_voltages=_graph_net_voltages(graph),
    )
    assert not [f for f in findings if f.severity == "error"]
    assert all(ir.stackup[t.layer]["name"] in layers for t in result.tracks)


@pytest.mark.slow
@pytest.mark.parametrize("open_inner", [False, True])
@pytest.mark.parametrize("method", ["distance", "radial"])
def test_connected_via_channel_assignment_replay(store, open_inner, method):
    ir, graph, features, footprints, fixed, config = _hydrate(store)
    groups = _resolve_pin_swap_groups(ir, graph, [])
    assert len(groups) == 1
    report = _apply_pin_swap_warm_start(ir, groups, footprints, fixed, method)
    assert report[0]["method"] == method
    if method == "distance":
        assert report[0]["distance_mm"] > 0
        before = ir.pin_net.copy()
        repeated = _apply_pin_swap_warm_start(ir, groups, footprints, fixed)
        assert (ir.pin_net == before).all()
        assert repeated == report
    else:
        assert report[0]["distance_mm"] is None
    layers: tuple[str, ...] = ("B.Cu",)
    if open_inner:
        inner = next(
            i for i, layer in enumerate(ir.stackup) if layer["name"] == "In2.Cu"
        )
        ir.stackup[inner] = {"name": "In2.Cu", "role": "signal", "routable": True}
        config.class_rules["ewod_ARR1_escape"]["layers"] = ["In2.Cu", "B.Cu"]
        layers = ("In2.Cu", "B.Cu")
    result = realize.realize(
        ir, config=config, footprints=footprints, fixed_copper=fixed
    )
    expected = {
        ("distance", False): 31,
        ("distance", True): 51,
        ("radial", False): 22,
        ("radial", True): 42,
    }
    assert 55 - len(_failed(ir, result)) == expected[method, open_inner]
    _assert_legal(ir, graph, features, footprints, fixed, config, result, layers)


@pytest.mark.parametrize("tied", [False, True])
def test_distance_assignment_preserves_primary_optimum_replay(store, monkeypatch, tied):
    ir, graph, _, footprints, fixed, _ = _hydrate(store)
    group = _resolve_pin_swap_groups(ir, graph, [])[0]
    far = {}

    def capture(_ir, _group, terminals):
        far.update(terminals)
        return (), 0.0

    with monkeypatch.context() as patch:
        patch.setattr(pinswap, "propose_distance_assignment", capture)
        _apply_pin_swap_warm_start(ir, (group,), footprints, fixed)
    x, y = float(ir.inst_x[group.instance]), float(ir.inst_y[group.instance])
    orders = list(permutations(range(3)))
    # Exhaustive three-channel subsets of the replay's actual pads and vias
    # supply both a valid chord tie-break and a tempting higher-cost alternate.
    for subset in combinations(group.pins, 3):
        sources = sorted(subset, key=lambda p: str(ir.net_name[int(ir.pin_net[p])]))
        near = [pinswap._pin_pos(x, y, group, p) for p in subset]
        manhattan = [
            [abs(far[p][0] - nx) + abs(far[p][1] - ny) for nx, ny in near]
            for p in sources
        ]
        chords = [
            [math.hypot(far[p][0] - nx, far[p][1] - ny) for nx, ny in near]
            for p in sources
        ]
        totals = [
            (
                math.fsum(manhattan[i][j] for i, j in enumerate(order)),
                math.fsum(chords[i][j] for i, j in enumerate(order)),
            )
            for order in orders
        ]
        primary = min(cost for cost, _ in totals)
        alternate = min(range(len(orders)), key=lambda i: totals[i][1])
        alternate_cost, alternate_chord = totals[alternate]
        is_tied = abs(alternate_cost - primary) <= 4 * math.ulp(primary)
        initial = pinswap._hungarian(manhattan)
        initial_chord = math.fsum(chords[i][j] for i, j in enumerate(initial))
        if is_tied != tied or alternate_chord >= initial_chord:
            continue
        small = replace(group, pins=subset)
        proposal = pinswap.propose_distance_assignment(ir, small, far)
        assert proposal is not None
        swaps, reported = proposal
        original = {int(ir.pin_net[p]): i for i, p in enumerate(sources)}
        for a, b in swaps:
            ir.swap_pins(a, b)
        actual = [0] * 3
        for j, pin in enumerate(subset):
            actual[original[int(ir.pin_net[pin])]] = j
        cost = math.fsum(manhattan[i][j] for i, j in enumerate(actual))
        assert cost == pytest.approx(primary, rel=0, abs=4 * math.ulp(primary))
        assert reported == cost
        if tied:
            assert math.fsum(
                chords[i][j] for i, j in enumerate(actual)
            ) == pytest.approx(alternate_chord)
        else:
            assert alternate_cost > cost
        return
    pytest.fail("replay lacks the requested Manhattan/chord trade-off")


def test_distance_warm_start_falls_back_without_connected_vias(store):
    ir, graph, _, footprints, _, _ = _hydrate(store)
    groups = _resolve_pin_swap_groups(ir, graph, [])
    before = ir.pin_net.copy()
    report = _apply_pin_swap_warm_start(ir, groups, footprints, [])
    assert report == [
        {"refdes": "ARR1_SINK_0", "method": "radial", "distance_mm": None}
    ]
    # The replay's stored assignment already is the radial assignment.
    assert (ir.pin_net == before).all()
    assert pinswap.propose_distance_assignment(ir, groups[0], {}) is None
    with pytest.raises(ValueError, match="warm_start"):
        _apply_pin_swap_warm_start(ir, groups, footprints, [], "unknown")


@pytest.mark.slow
def test_route_op_distance_warm_start_replay(store):
    ir, graph, _, _, _, _ = _hydrate(store)
    ref = store.get_ref(kind="pcb", id="ewod-dogfood-6-escape-regression")
    assert ref is not None
    inner = next(i for i, layer in enumerate(ir.stackup) if layer["name"] == "In2.Cu")
    ir.stackup[inner] = {"name": "In2.Cu", "role": "signal", "routable": True}
    store.pcb_set_stackup(graph["board"]["board_id"], ir.stackup)
    rules = graph["net_classes"]["ewod_ARR1_escape"]
    rules["layers"] = ["In2.Cu", "B.Cu"]
    store.pcb_set_class_rules(ref.id, "ewod_ARR1_escape", rules)
    ctx: Any = SimpleNamespace(
        store=store,
        meta={"params": {"pcb_ref_id": ref.id, "iters": 3000, "seed": 0}},
        record_failure=Mock(),
        append_chunk=Mock(),
    )
    pcb_route._dispatch(ctx, pcb_route.SPEC)
    updated = store.get_ref(kind="pcb", id=ref.id)
    last = updated.meta["last_route"]
    assert last["warm_start"][0]["method"] == "distance"
    assert last["warm_start"][0]["distance_mm"] > 0
    assert last["realized"] >= 51
    assert store.pcb_pin_swaps_list(ref.id)


def test_route_op_warm_start_flag_replay(store, monkeypatch):
    _hydrate(store)
    ref = store.get_ref(kind="pcb", id="ewod-dogfood-6-escape-regression")
    assert ref is not None
    hub = Hub(store=store)
    job = Mock()
    job.put.return_value = SimpleNamespace(body="fixture job captured")
    monkeypatch.setattr(hub, "sibling", lambda kind: job)
    handler = PcbHandler(hub=hub)
    handler._enqueue_op(ref, "route", {"warm_start": "radial"})
    assert job.put.call_args.kwargs["params"]["warm_start"] == "radial"
    radial_key = job.put.call_args.kwargs["idem_key"]
    handler._enqueue_op(ref, "route", {})
    assert "warm_start" not in job.put.call_args.kwargs["params"]
    assert job.put.call_args.kwargs["idem_key"] != radial_key
    with pytest.raises(BadInput, match="warm_start"):
        handler._enqueue_op(ref, "route", {"warm_start": "invalid"})
    ctx: Any = SimpleNamespace(
        meta={"params": {"warm_start": "invalid"}}, record_failure=Mock()
    )
    pcb_route._dispatch(ctx, pcb_route.SPEC)
    ctx.record_failure.assert_called_once()


@pytest.mark.slow
def test_exact_dogfood_grid_refinement_improves_count_and_is_deterministic(
    store, monkeypatch
):
    ir, graph, features, footprints, fixed, config = _hydrate(store)
    assert ir.n_segments == 55 and ir.n_nets == 58
    assert realize._PITCH_PER_CLEARANCE == 2.0 / 3.0
    with monkeypatch.context() as old:
        old.setattr(realize, "_PITCH_PER_CLEARANCE", 2.0 / 3.0)
        before = realize.realize(
            ir, config=config, footprints=footprints, fixed_copper=fixed
        )
    assert len(_failed(ir, before)) == 33
    _assert_legal(ir, graph, features, footprints, fixed, config, before)
    # Fine pitch remains an explicit experiment: its net+1 on this fixture
    # regresses reference/fab seeds and must not silently become the default.
    with monkeypatch.context() as fine:
        fine.setattr(realize, "_PITCH_PER_CLEARANCE", 1.0 / 3.0)
        after = realize.realize(
            ir, config=config, footprints=footprints, fixed_copper=fixed
        )
        repeated = realize.realize(
            ir, config=config, footprints=footprints, fixed_copper=fixed
        )
    assert len(_failed(ir, after)) == 32
    assert _failed(ir, repeated) == _failed(ir, after)
    assert asdict(repeated) == asdict(after)
    _assert_legal(ir, graph, features, footprints, fixed, config, after)
    recovered = _failed(ir, before) - _failed(ir, after)
    lost = _failed(ir, after) - _failed(ir, before)
    assert len(recovered) == 12 and len(lost) == 11
    assert {
        f"ARR1_{name}" for name in ("R1C3", "R1C5", "R1C6", "R2C3", "R6C1")
    } <= recovered


def test_exact_dogfood_grid_cap_changes_the_actual_pitch(store, monkeypatch):
    ir, _, _, footprints, fixed, config = _hydrate(store)
    pitches = []
    original = realize.maze.grid_for

    class Captured(Exception):
        pass

    def capture(*args, **kwargs):
        grid = original(*args, **kwargs)
        pitches.append(grid.pitch)
        raise Captured

    monkeypatch.setattr(realize.maze, "grid_for", capture)
    with monkeypatch.context() as old:
        old.setattr(realize, "_PITCH_PER_CLEARANCE", 2.0 / 3.0)
        with pytest.raises(Captured):
            realize.realize(
                ir, config=config, footprints=footprints, fixed_copper=fixed
            )
    with monkeypatch.context() as fine:
        fine.setattr(realize, "_PITCH_PER_CLEARANCE", 1.0 / 3.0)
        with pytest.raises(Captured):
            realize.realize(
                ir, config=config, footprints=footprints, fixed_copper=fixed
            )
    assert pitches[0] > pitches[1]
    assert pitches[1] == pytest.approx(0.05)
    assert pitches[0] < 0.1  # extent already refines the old cap on this board
    print({"old_grid_pitch_mm": pitches[0], "new_grid_pitch_mm": pitches[1]})


def test_stored_no_path_labels_have_full_width_actual_terminal_paths(
    store, monkeypatch
):
    """Reproduce diagnostic/request drift without a routing pass or changing copper."""
    import inspect
    import json

    ir, _, _, footprints, fixed, config = _hydrate(store)
    targets = {
        f"ARR1_{n}"
        for n in (
            "R1C3",
            "R1C5",
            "R1C6",
            "R2C1",
            "R2C3",
            "R3C3",
            "R3C4",
            "R3C5",
            "R4C2",
            "R4C3",
            "R4C6",
            "R5C3",
            "R5C5",
            "R6C1",
            "R6C3",
            "R6C4",
            "R6C6",
        )
    }
    captured: dict[str, Any] = {}
    signature = inspect.signature(realize._route_pass)

    class Captured(Exception):
        pass

    def capture(*args, **kwargs):
        captured.update(signature.bind(*args, **kwargs).arguments)
        raise Captured

    monkeypatch.setattr(realize, "_route_pass", capture)
    monkeypatch.setattr(realize, "_PITCH_PER_CLEARANCE", 2.0 / 3.0)
    with pytest.raises(Captured):
        realize.realize(ir, config=config, footprints=footprints, fixed_copper=fixed)
    probe = realize._pads_only_probe(
        ir, captured["spec"], captured["pads"], captured["clearance"], fixed
    )
    evidence = []
    for seg in captured["order"]:
        net = int(ir.seg_net[seg])
        name = str(ir.net_name[net])
        if name not in targets:
            continue
        req = realize._seg_request(
            ir,
            seg,
            config,
            captured["rules_by_net"],
            captured["clearance"],
            captured["signal_layers"],
            captured["island_terminals"],
            captured["net_layers"],
        )
        assert req is not None and not req.failed
        rules = captured["rules_by_net"][net]
        legacy = realize._diagnose_unrouted(
            ir,
            seg,
            captured["spec"],
            captured["pads"],
            captured["clearance"],
            captured["signal_layers"],
            rules,
            req.n_vias,
            req.group_extent,
            config.max_expansions,
            fixed_copper=fixed,
            probe=probe,
        )
        actual = probe.route(
            net, req.eff_start, req.eff_goal, **{**req.route_kwargs, "attach": False}
        )
        assert (
            actual is not None
        )  # truthful route request; legacy label is evidence only
        assert req.route_kwargs["layers"] == [3]  # stored class B.Cu only
        evidence.append(
            {
                "net": name,
                "legacy": asdict(legacy),
                "actual_full_width_path": True,
                "actual_start_mm": req.eff_start,
                "actual_goal_mm": req.eff_goal,
                "route_kwargs": req.route_kwargs,
                "grid": asdict(captured["spec"]),
                "static_pads": len(captured["pads"]),
                "fixed_copper_rows": len(fixed),
            }
        )
    assert {r["net"] for r in evidence} == targets
    # Optional task recorder: never writes a source fixture or touches the stored board.
    recorder = Path(".scratch/pcb-snapshot")
    if recorder.is_dir():
        (recorder / "actual-terminal-evidence.json").write_text(
            json.dumps(evidence, indent=2, default=lambda v: v.tolist()),
            encoding="utf-8",
        )

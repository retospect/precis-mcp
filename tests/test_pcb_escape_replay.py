"""Fixed-pose dogfood routed-count regression; no provider or live board writes."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from precis.pcb import realize, session
from precis.pcb.capabilities import capability_for
from precis.pcb.drc import process_for_stackup
from precis.pcb.snapshot import load_snapshot, read_snapshot
from precis.workers.job_types.pcb_route import _graph_net_rules, _graph_net_voltages


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


def _assert_legal(ir, graph, features, footprints, fixed, config, result):
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
    assert all(ir.stackup[t.layer]["name"] == "B.Cu" for t in result.tracks)


@pytest.mark.slow
def test_exact_dogfood_grid_refinement_improves_count_and_is_deterministic(
    store, monkeypatch
):
    ir, graph, features, footprints, fixed, config = _hydrate(store)
    assert ir.n_segments == 55 and ir.n_nets == 58
    assert realize._PITCH_PER_CLEARANCE == 1.0 / 3.0
    with monkeypatch.context() as old:
        old.setattr(realize, "_PITCH_PER_CLEARANCE", 2.0 / 3.0)
        before = realize.realize(
            ir, config=config, footprints=footprints, fixed_copper=fixed
        )
    assert len(_failed(ir, before)) == 33
    _assert_legal(ir, graph, features, footprints, fixed, config, before)
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
    with pytest.raises(Captured):
        realize.realize(ir, config=config, footprints=footprints, fixed_copper=fixed)
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

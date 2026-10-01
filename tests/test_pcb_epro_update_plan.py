"""``pcb_epro.plan_update`` — what ``--update`` decides, without a DB.

The DB-backed half (poses written, features not duplicated, refusals) is
in ``test_pcb_epro_import.py``. Here: the cases the tiny fixture cannot
reach cheaply — a rewired pin, a footprint swap, a locked part, a noise-
level pose difference, a changed outline.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from precis.ingest import pcb_epro
from precis.pcb import epro


def _inst(refdes: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "refdes": refdes,
        "x": 10.0,
        "y": 5.0,
        "rot": 0.0,
        "layer": "top",
        "fixed": None,
        "footprint": "R0402",
    }
    base.update(over)
    return base


def _graph(
    instances: list[dict[str, Any]], members: dict[str, list[tuple[str, str]]]
) -> dict[str, Any]:
    return {
        "instances": instances,
        "nets": [
            {"name": n, "members": [{"refdes": r, "pin": p} for r, p in ms]}
            for n, ms in members.items()
        ],
    }


def _design(
    components: list[dict[str, Any]],
    connections: Sequence[tuple[str, str, str]] = (),
    features: list[dict[str, Any]] | None = None,
) -> epro.Design:
    return epro.Design(
        components=components,
        connections=[{"net": n, "refdes": r, "pin": p} for n, r, p in connections],
        features=features or [],
    )


def test_a_rewired_pin_is_reported_not_applied() -> None:
    """precis is where the netlist gets fixed; a re-import that rewired a
    surviving pin would undo that fix silently."""
    graph = _graph([_inst("R1")], {"GND": [("R1", "1")], "VCC": [("R1", "2")]})
    design = _design([_inst("R1")], [("SIG", "R1", "1"), ("VCC", "R1", "2")])
    plan = pcb_epro.plan_update(graph, [], design)
    assert plan.rewired == ["R1.1: board GND, source SIG"]


def test_a_new_parts_pins_are_not_called_rewired() -> None:
    graph = _graph([_inst("R1")], {"VCC": [("R1", "1")]})
    design = _design(
        [_inst("R1"), _inst("R2")], [("VCC", "R1", "1"), ("VCC", "R2", "1")]
    )
    plan = pcb_epro.plan_update(graph, [], design)
    assert plan.added == ["R2"]
    assert plan.rewired == []


def test_a_footprint_swap_refuses_the_whole_update() -> None:
    """Its pins would name different pads; matching by refdes cannot say
    which old connection a new pad inherits."""
    graph = _graph([_inst("R1")], {})
    design = _design([_inst("R1", footprint="R0603")])
    with pytest.raises(pcb_epro.EproImportError, match="R1"):
        pcb_epro.plan_update(graph, [], design)


def test_noise_is_not_a_move_but_a_flip_or_a_turn_is() -> None:
    graph = _graph(
        [
            _inst("R1"),
            _inst("R2"),
            _inst("R3"),
            _inst("R4", rot=359.999),
        ],
        {},
    )
    design = _design(
        [
            _inst("R1", x=10.0 + 1e-5),
            _inst("R2", layer="bottom"),
            _inst("R3", rot=90.0),
            _inst("R4", rot=0.0),
        ]
    )
    plan = pcb_epro.plan_update(graph, [], design)
    assert [m[0] for m in plan.moved] == ["R2", "R3"]


def test_a_locked_part_is_moved_and_says_so() -> None:
    """The source is the author's own edit; a lock in precis guards against
    the OPTIMIZER, not against the author."""
    graph = _graph([_inst("J1", fixed="both")], {})
    design = _design([_inst("J1", x=20.0)])
    plan = pcb_epro.plan_update(graph, [], design)
    assert plan.moved[0][3] is True
    assert any("locked" in line for line in plan.lines())


def test_a_changed_outline_is_counted_both_ways() -> None:
    old = {"ftype": "outline", "geom": {"path": [[0.0, 0.0], [10.0, 0.0]]}}
    new = {"ftype": "outline", "geom": {"path": [[0.0, 0.0], [12.0, 0.0]]}}
    hole = {"ftype": "mounting_hole", "geom": {"x": 1.0, "y": 1.0, "dia_mm": 3.0}}
    stored = [
        {**old, "x": None, "y": None, "rot": 0.0, "feature_id": 1},
        {**hole, "x": None, "y": None, "rot": 0.0, "feature_id": 2},
    ]
    plan = pcb_epro.plan_update(
        _graph([], {}), stored, _design([], features=[new, hole])
    )
    assert (plan.features_added, plan.features_missing) == (1, 1)

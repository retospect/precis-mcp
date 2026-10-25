"""The pcb-loop widen stage (gr515107): seeded DERIVED copper on a
power net is grown to its rules/floor target and the derived set is
rewritten wholesale through ``pcb_copper_replace``; signal nets and
already-wide rows are left alone, and the DRC bracket lands in the
report."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from tests.fixtures.pcb import pcb_loop

_DESIGN = {
    "components": [
        {
            "refdes": "U1",
            "label": "MCU",
            "x": 10.0,
            "y": 10.0,
            "pins": [{"name": "VDD"}, {"name": "GND"}, {"name": "SCL"}],
        },
        {
            "refdes": "R1",
            "label": "4.7k",
            "x": 15.0,
            "y": 10.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
    ],
    "features": [
        {
            "ftype": "outline",
            "geom": {"path": [[0, 0], [40, 0], [40, 40], [0, 40], [0, 0]]},
        }
    ],
    "nets": [
        {"name": "GND", "class": "gnd"},
        {"name": "SIG", "class": "signal"},
    ],
    "connections": [
        {"net": "GND", "refdes": "U1", "pin": "GND"},
        {"net": "GND", "refdes": "R1", "pin": "2"},
        {"net": "SIG", "refdes": "U1", "pin": "SCL"},
        {"net": "SIG", "refdes": "R1", "pin": "1"},
    ],
}


def _net_ids(store, ref_id: int) -> dict[str, int]:
    with store.pool.connection() as conn:
        return {
            str(name): int(net_id)
            for net_id, name in conn.execute(
                "SELECT net_id, name FROM pcb_nets "
                "WHERE ref_id = %s AND retired_at IS NULL",
                (ref_id,),
            ).fetchall()
        }


def _seed(store, pcb) -> tuple[object, int, dict[str, int]]:
    pcb.put(id="widen-board", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="widen-board")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    nids = _net_ids(store, ref.id)
    store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": nids["GND"],
                "geom": {
                    "width_mm": 0.25,
                    "segments": [{"shape": "line", "start": [1, 1], "end": [10, 1]}],
                },
            },
            {
                "ctype": "track",
                "layer": "B.Cu",
                "net_id": nids["SIG"],
                "geom": {
                    "width_mm": 0.25,
                    "segments": [{"shape": "line", "start": [2, 2], "end": [11, 2]}],
                },
            },
            {
                "ctype": "via",
                "layer": "F.Cu",
                "net_id": nids["GND"],
                "geom": {"dia_mm": 0.45, "drill_mm": 0.3, "x": 5.0, "y": 5.0},
            },
        ],
    )
    return ref, board_id, nids


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def test_widen_grows_power_copper_and_rewrites(store, pcb) -> None:
    ref, board_id, _nids = _seed(store, pcb)
    out = pcb_loop._widen_power_copper(store, pcb, ref)
    assert out["power_nets"] == ["GND"]
    assert out["changed"] == 2  # the GND track and the GND via
    gnd = out["per_net"]["GND"]
    assert gnd["tracks"] == 1 and gnd["vias"] == 1
    assert gnd["mm"] == pytest.approx(9.0)
    # the pre/post DRC bracket is in the report
    assert out["drc_pre"] is not None and out["drc_post"] is not None
    items = store.pcb_copper_list(board_id)
    tracks = {str(i["net"]): i for i in items if i["ctype"] == "track"}
    assert tracks["GND"]["width_mm"] >= pcb_loop._WIDEN_TRACK_FLOOR_MM
    # a signal net's copper is not the widen stage's business
    assert tracks["SIG"]["width_mm"] == pytest.approx(0.25)
    (via,) = [i for i in items if i["ctype"] == "via"]
    assert via["dia_mm"] == pytest.approx(pcb_loop._WIDEN_VIA_FLOOR_MM)
    # drill is not grown — the annular ring only ever gets bigger
    assert via["drill_mm"] == pytest.approx(0.3)


def test_widen_is_a_noop_when_copper_already_wide(store, pcb) -> None:
    ref, board_id, _nids = _seed(store, pcb)
    first = pcb_loop._widen_power_copper(store, pcb, ref)
    assert first["changed"] == 2
    # second pass: nothing left under target — no rewrite, no DRC
    # bracket in the report.
    second = pcb_loop._widen_power_copper(store, pcb, ref)
    assert second["changed"] == 0
    assert "drc_pre" not in second

"""PCB power sanity boards (P1 power cross, P3 power cross + 20 crossing
signal nets). Board and asserts: ``src/precis/pcb/sanity_power.py``.

Pure in-memory (no store, no DB, no MCP). Each assertion's numbers are in
the failure message. Iterate without the gate queue::

    uv run python -m precis.pcb.bench power [p1|p3]
"""

from __future__ import annotations

import pytest

from precis.pcb import sanity_power as sp


def test_boards_are_distinct_and_hold_the_documented_parts() -> None:
    p1 = sp.build_graph(sp.SPECS["p1"])
    p3 = sp.build_graph(sp.SPECS["p3"])
    assert {i["refdes"] for i in p1["instances"]} == {
        "J_IN",
        "J_OUT",
        *(f"C{i}" for i in range(1, sp.CAPS + 1)),
    }
    assert {n["name"] for n in p1["nets"]} == {sp.RAIL, sp.GND}
    assert len(p3["nets"]) == 2 + 20
    assert all(i["fixed"] == "both" for i in p3["instances"])
    rail = next(n for n in p1["nets"] if n["name"] == sp.RAIL)
    assert rail["est_current_a"] == sp.CURRENT_A
    assert {(m["refdes"], m["pin"]) for m in rail["members"]} >= {
        ("J_IN", "1"),
        ("J_OUT", "1"),
    }


@pytest.mark.parametrize("name", sorted(sp.SPECS))
def test_power_board_passes_every_assert(name: str) -> None:
    result = sp.route(sp.SPECS[name])
    failed = {k: a for k, a in result.asserts.items() if not a["ok"]}
    assert not failed, f"{name}: {failed}; findings {list(result.findings)}"

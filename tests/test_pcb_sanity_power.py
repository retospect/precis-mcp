"""PCB power sanity boards (P1 power cross, P3 power cross + 20 crossing
signal nets). Board and asserts: ``src/precis/pcb/sanity_power.py``.

Pure in-memory (no store, no DB, no MCP). Each assertion's numbers are in
the failure message. Iterate without the gate queue::

    uv run python -m precis.pcb.bench power [p1|p3]
"""

from __future__ import annotations

import pytest

from precis.pcb import realize as pcb_realize
from precis.pcb import sanity_power as sp
from precis.pcb import session as pcb_session


def test_single_drop_via_per_power_pad_is_reported_as_under_current() -> None:
    built = sp.build(sp.SPECS["p1"])
    rres = pcb_realize.realize(
        built.ir, config=built.config, footprints=built.footprints
    )
    assert pcb_session.power_drop_shortfalls(built.ir, rres) == []
    # Keep one stub+via per pad: 0.75 mm vias carry 2.5 A of the rail's 5 A.
    seen: set[tuple[int, tuple[float, float]]] = set()
    keep = []
    for t in rres.tracks:
        pad = (round(t.segments[0]["start"][0], 3), round(t.segments[0]["start"][1], 3))
        if (t.net_id, pad) not in seen:
            seen.add((int(t.net_id), pad))
            keep.append(t)
    thinned = pcb_realize.RealizeResult(tuple(keep), rres.vias, ())
    short = pcb_session.power_drop_shortfalls(built.ir, thinned)
    assert {n for n, *_ in short} == {sp.RAIL}
    assert len(short) == sp.CAPS + 2
    assert all(cap < need for _n, _pt, cap, need in short)
    rows = pcb_session.power_drop_findings(built.ir, thinned)
    assert {f.rule for f in rows} == {"power_drop_under_current"}


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

"""gr513871 — the compact route-failure digest on the route path.

The route run's per-net notes name ONE problem at a time, so a board
failing many nets on the same blocked corridor read as the same crossing
repeated 183 times with no run-level summary. ``route_digest`` aggregates
``fail.problems`` into binding constraints; the topo route stage produces
it again (``build_digest`` over ``sketch_rows`` — ``meta.topo_run.route``
on the job, ``meta.last_route.digest`` on the board ref,
``pcb.local.route``'s ``LocalRoute.digest`` offline) and
``view='route-status'`` renders it.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb.route_digest import _REASON_HINTS, build_digest, digest_lines

#: A digest in the shape the pcb_route job stamped onto
#: ``refs.meta.last_route.digest`` — kept literal so the renderer test
#: can't drift with a producer that no longer exists.
_DIGEST: dict[str, Any] = {
    "failed": ["GND", "OUT3"],
    "constraints": [
        {
            "reason": "same-layer-crossing",
            "target": "CC1 on F.Cu",
            "count": 183,
            "nets": ["GND"],
        },
        {
            "reason": "search_budget",
            "count": 6,
            "nets": ["OUT3"],
            "hint": "raise max_expansions",
        },
    ],
}


# ── build_digest: sketch_rows -> the stored digest shape ─────────────


def _rows(*specs: tuple[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """``{net: row}`` — each spec is ``(net, problems)``; an empty list
    is a realized net."""
    out: dict[str, dict[str, Any]] = {}
    for name, problems in specs:
        if problems:
            out[name] = {"status": "failed", "fail": {"problems": problems}}
        else:
            out[name] = {"status": "realized"}
    return out


def test_build_digest_groups_by_reason_worst_first() -> None:
    """The point of the digest: many nets failing on the same reason
    collapse into ONE constraint line, ordered by complaint count."""
    rows = _rows(
        ("A", [{"reason": "unrouted"}]),
        ("B", [{"reason": "same-layer-crossing", "with": "C"}]),
        ("C", [{"reason": "same-layer-crossing", "with": "B"}]),
        ("D", [{"reason": "same-layer-crossing", "with": "B"}]),
        ("E", [{"reason": "unrouted"}, {"reason": "no via site"}]),
    )
    d = build_digest(rows)
    assert d["failed"] == ["A", "B", "C", "D", "E"]
    assert [c["reason"] for c in d["constraints"]] == [
        "same-layer-crossing",
        "unrouted",
        "no via site",
    ]
    assert d["constraints"][0]["count"] == 3
    assert d["constraints"][1]["count"] == 2


def test_build_digest_dedupes_a_net_repeating_one_reason() -> None:
    """A net complaining about the same blocker twice is ONE net on the
    constraint, however often the complaint repeats."""
    rows = _rows(
        (
            "A",
            [
                {"reason": "same-layer-crossing", "with": "B", "layer": 0},
                {"reason": "same-layer-crossing", "with": "B", "layer": 0},
            ],
        ),
    )
    d = build_digest(rows)
    (c,) = d["constraints"]
    assert c["count"] == 2 and c["nets"] == ["A"]


def test_build_digest_target_is_the_most_named_blocker_with_layer() -> None:
    """``target`` resolves ``problem.layer`` through ``layer_names`` —
    the corridor the reader should look at."""
    rows = _rows(
        ("A", [{"reason": "same-layer-crossing", "with": "X", "layer": 0}]),
        ("B", [{"reason": "same-layer-crossing", "with": "Y", "layer": 3}]),
        ("C", [{"reason": "same-layer-crossing", "with": "X", "layer": 0}]),
    )
    d = build_digest(rows, layer_names=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    (c,) = d["constraints"]
    assert c["target"] == "X on F.Cu"
    assert c["hint"] == _REASON_HINTS["same-layer-crossing"]


def test_build_digest_hint_and_missing_fields() -> None:
    """A reason with no registered lever carries no ``hint``; a problem
    naming no blocker carries no ``target``; a realized-only board
    digests to the honest empty set."""
    d = build_digest(_rows(("A", [{"reason": "search_budget"}])))
    (c,) = d["constraints"]
    assert "target" not in c and "hint" not in c
    assert build_digest(_rows(("A", []), ("B", []))) == {
        "failed": [],
        "constraints": [],
    }
    assert build_digest({}) == {"failed": [], "constraints": []}


def test_digest_lines_render_header_constraints_and_hint() -> None:
    lines = digest_lines(_DIGEST)
    assert lines[0] == "route digest — 2 failed net(s): GND, OUT3"
    assert any(
        "same-layer-crossing" in line and "CC1 on F.Cu" in line for line in lines
    )
    assert any("search_budget" in line and "max_expansions" in line for line in lines)


def test_digest_lines_empty_when_nothing_failed() -> None:
    assert digest_lines({"failed": [], "constraints": []}) == []
    assert digest_lines(None) == []
    assert digest_lines({}) == []


# ── the view: last_route.digest rendered on route-status ─────────────

_DESIGN: dict[str, Any] = {
    # gr524240: a partless component's `footprint` is a reference to a
    # row this board carries — author the names the components use.
    "footprints": [
        {
            "name": "QFN-32",
            "pads": [
                {"pin": "VDD", "shape": "rect", "x": -1.0, "y": 0.0, "w": 0.4},
                {"pin": "GND", "shape": "rect", "x": 1.0, "y": 0.0, "w": 0.4},
            ],
        },
        {
            "name": "0402",
            "pads": [
                {"pin": "1", "shape": "rect", "x": -0.5, "y": 0.0, "w": 0.8},
                {"pin": "2", "shape": "rect", "x": 0.5, "y": 0.0, "w": 0.8},
            ],
        },
    ],
    "components": [
        {
            "refdes": "U1",
            "label": "ESP32-C3",
            "footprint": "QFN-32",
            "pins": [{"name": "VDD"}, {"name": "GND"}],
        },
        {
            "refdes": "R1",
            "label": "4.7k 0402",
            "footprint": "0402",
            "pins": [{"name": "1"}, {"name": "2"}],
        },
    ],
    "nets": [{"name": "VCC3V3"}, {"name": "GND"}],
    "connections": [
        {"net": "VCC3V3", "refdes": "U1", "pin": "VDD"},
        {"net": "GND", "refdes": "U1", "pin": "GND"},
        {"net": "VCC3V3", "refdes": "R1", "pin": "1"},
    ],
}


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def test_route_status_renders_last_run_digest(pcb, store) -> None:
    pcb.put(id="digest-board", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="digest-board")
    assert ref is not None
    store.pcb_set_pose(
        ref.id,
        {},
        meta={
            "last_route": {
                "digest": _DIGEST,
                "realized": 1,
                "failed": 2,
            }
        },
    )
    body = pcb.get(id="digest-board", view="route-status").body
    assert "# route conflicts (last run)" in body
    assert "2 failed net(s): GND, OUT3" in body
    assert "CC1 on F.Cu" in body


def test_route_status_without_digest_unchanged(pcb) -> None:
    pcb.put(id="plain-board", args=_DESIGN)
    body = pcb.get(id="plain-board", view="route-status").body
    assert "route conflicts" not in body

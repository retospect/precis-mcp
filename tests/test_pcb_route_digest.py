"""gr513871 — the compact route-failure digest on the default route path.

The route run's per-net notes name ONE problem at a time, so a board
failing many nets on the same blocked corridor read as the same crossing
repeated 183 times with no run-level summary. ``route_digest`` aggregates
``fail.problems`` into binding constraints; the job stamps it onto
``refs.meta.last_route.digest`` and ``view='route-status'`` renders it.

Routing is retired (gr515107): nothing produces a NEW digest. These tests
pin the read half — ``digest_lines`` over a stored digest dict — for
boards routed before the cut.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb.route_digest import digest_lines

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

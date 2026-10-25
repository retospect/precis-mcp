"""gr513871 — the compact route-failure digest on the default route path.

The route run's per-net notes name ONE problem at a time, so a board
failing many nets on the same blocked corridor read as the same crossing
repeated 183 times with no run-level summary. ``route_digest`` aggregates
``fail.problems`` into binding constraints; the job stamps it onto
``refs.meta.last_route.digest`` and ``view='route-status'`` renders it.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb.route_digest import conflict_digest, dedupe_details, digest_lines


def _cross(net: str, layer: int) -> dict[str, Any]:
    return {
        "kind": "same-layer-crossing",
        "reason": "same-layer-crossing",
        "layer": layer,
        "with": net,
    }


def _unrouted(reason: str, message: str = "") -> dict[str, Any]:
    return {
        "kind": "unrouted",
        "reason": reason,
        "segment": "U1.1-J1.2",
        "message": message or f"unrouted ({reason})",
    }


def _rows() -> dict[str, dict[str, Any]]:
    problems = [_cross("CC1", 0) for _ in range(183)]
    problems += [_unrouted("no_path")]
    return {
        "GND": {"status": "failed", "fail": {"problems": problems}},
        "OUT3": {
            "status": "failed",
            "fail": {"problems": [_unrouted("search_budget") for _ in range(6)]},
        },
        "V5": {"status": "realized"},
    }


def test_dedupe_details_collapses_repeats_first_seen_order() -> None:
    assert dedupe_details(["a", "a", "b", "a", "c"]) == ["a ×3", "b", "c"]
    assert dedupe_details([]) == []


def test_conflict_digest_aggregates_binding_constraints() -> None:
    digest = conflict_digest(_rows(), ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
    assert digest["failed"] == ["GND", "OUT3"]
    top = digest["constraints"][0]
    # 183 identical crossing problems roll up into ONE constraint naming
    # the peer net and the layer — the row the gripe could not see.
    assert top["reason"] == "same-layer-crossing"
    assert top["target"] == "CC1 on F.Cu"
    assert top["count"] == 183
    assert top["nets"] == ["GND"]
    budget = next(c for c in digest["constraints"] if c["reason"] == "search_budget")
    assert budget["count"] == 6 and budget["nets"] == ["OUT3"]
    # OUT3's complaint: which budget to raise is now named on the digest.
    assert "max_expansions" in budget["hint"]


def test_conflict_digest_groups_no_path_separately_from_crossings() -> None:
    digest = conflict_digest(_rows(), ["F.Cu"])
    reasons = {(c["reason"], c["target"]) for c in digest["constraints"]}
    assert ("same-layer-crossing", "CC1 on F.Cu") in reasons
    assert ("no_path", None) in reasons


def test_digest_lines_render_header_constraints_and_hint() -> None:
    lines = digest_lines(conflict_digest(_rows(), ["F.Cu"]))
    assert lines[0] == "route digest — 2 failed net(s): GND, OUT3"
    assert any(
        "same-layer-crossing" in line and "CC1 on F.Cu" in line for line in lines
    )
    assert any("search_budget" in line and "max_expansions" in line for line in lines)


def test_digest_lines_empty_when_nothing_failed() -> None:
    assert digest_lines(conflict_digest({"V5": {"status": "realized"}})) == []
    assert digest_lines(None) == []
    assert digest_lines({}) == []


# ── the view: last_route.digest rendered on route-status ─────────────

_DESIGN: dict[str, Any] = {
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
                "digest": conflict_digest(_rows(), ["F.Cu"]),
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

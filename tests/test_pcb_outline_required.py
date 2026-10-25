"""A board must have a closed outline, otherwise nothing goes (Reto
2026-10-20: "this fails DRC. Board must have closed outline, otherwise
nothing goes."). Pins the DRC rule ``outline_missing``. The up-front
route refusal died with routing (gr515107): ``op='route'`` now refuses
unconditionally before any outline check."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import drc as pcb_drc
from precis.pcb import session as pcb_session
from tests._pcb_outline import closed_outline

_RECT = closed_outline(0, 0, 40, 30)["geom"]["path"]


def _rules(outline: list[list[float]] | None) -> list[pcb_drc.DrcFinding]:
    return pcb_drc.check_outline_missing(outline)


@pytest.mark.parametrize(
    "outline",
    [
        None,
        [],
        [[0, 0], [5, 0]],  # two points
        [[0, 0], [5, 0], [5, 0], [0, 0]],  # four points, two distinct
        [[0, 0], [10, 10], [10, 0], [0, 10]],  # bow-tie: self-crossing
        [[0, 0], [5, 0], [10, 0]],  # collinear: zero area
    ],
)
def test_outline_missing_flags_a_defective_outline(outline) -> None:
    findings = _rules(outline)
    assert len(findings) == 1, findings
    f = findings[0]
    assert (f.rule, f.severity, f.where) == (
        "outline_missing",
        "error",
        "board outline",
    )
    assert "'ftype':'outline'" in f.detail  # says how to fix it


def test_outline_missing_is_silent_for_a_closed_rectangle() -> None:
    assert _rules(_RECT) == []
    assert pcb_session.outline_defect(_RECT) is None


def test_geometric_drc_reports_outline_missing_only_without_an_outline() -> None:
    from precis.pcb.capabilities import capability_for

    model: dict[str, Any] = {"copper": [], "pads": [], "layers": ["F.Cu", "B.Cu"]}
    cap = capability_for("4layer")
    bare = pcb_drc.run_geometric_drc(model, capability=cap, unrouted=[], outline=None)
    assert [f.rule for f in bare].count("outline_missing") == 1
    ok = pcb_drc.run_geometric_drc(model, capability=cap, unrouted=[], outline=_RECT)
    assert "outline_missing" not in [f.rule for f in ok]


def test_placement_drc_reports_outline_missing_only_without_an_outline() -> None:
    """gr500431: the placement-only gate runs the same predicate — a board
    with no closed outline fails there too, not only at the routed gate."""
    from precis.pcb.capabilities import capability_for

    model: dict[str, Any] = {"copper": [], "pads": [], "layers": ["F.Cu", "B.Cu"]}
    cap = capability_for("4layer")
    bare = pcb_drc.run_placement_drc(model, capability=cap, outline=None)
    flagged = [f for f in bare if f.rule == "outline_missing"]
    assert len(flagged) == 1
    assert flagged[0].severity == "error"
    ok = pcb_drc.run_placement_drc(model, capability=cap, outline=_RECT)
    assert "outline_missing" not in [f.rule for f in ok]


_DESIGN: dict[str, Any] = {
    "components": [
        {"refdes": "A", "label": "ic", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "B", "label": "ic", "x": 5.0, "y": 0.0, "pins": [{"name": "1"}]},
    ],
    "nets": [{"name": "N1"}],
    "connections": [
        {"net": "N1", "refdes": "A", "pin": "1"},
        {"net": "N1", "refdes": "B", "pin": "1"},
    ],
}


@pytest.mark.db
def test_op_route_refuses_before_any_outline_check(store) -> None:
    """Routing is retired (gr515107): ``op='route'`` raises the retired
    refusal — not the outline refusal — and enqueues nothing, on an
    outline-less board and on a closed one alike."""
    pcb = PcbHandler(hub=Hub(store=store))
    pcb.put(id="no-outline", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="no-outline")
    assert ref is not None

    with pytest.raises(BadInput, match="routing is retired") as exc:
        pcb.put(id="no-outline", args={"op": "route"})
    assert "op':'place'" in str(exc.value.next)

    # A closed outline changes nothing: the op is gone, not gated.
    pcb.put(id="no-outline", args={"features": [closed_outline()]})
    with pytest.raises(BadInput, match="routing is retired"):
        pcb.put(id="no-outline", args={"op": "route"})
    with store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'job' AND parent_id = %s",
            (ref.id,),
        ).fetchone()[0]
    assert n == 0

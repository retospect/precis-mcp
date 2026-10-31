"""``pcb_route`` is retired (gr515107, Reto 2026-10-25: the topological
router is the only router; the vector router was deleted, tag
``vroute-final``).

``put(kind='pcb', args={'op':'route'})`` refuses before any job is
minted — a stored ``pcb_routes`` history stays legible
(``view='route-status'``) but no NEW run can start until the topological
realiser lands (``docs/backlog/pcb-topo-place.md`` slice 5).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis.workers.job_types import get_job_type, known_job_types
from tests._pcb_outline import closed_outline

pytestmark = pytest.mark.db

_DESIGN = {
    "components": [
        {"refdes": "U1", "label": "mcu", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "R1", "label": "r", "x": 5.0, "y": 0.0, "pins": [{"name": "1"}]},
    ],
    "nets": [{"name": "N1", "class": "signal"}],
    "connections": [
        {"net": "N1", "refdes": "U1", "pin": "1"},
        {"net": "N1", "refdes": "R1", "pin": "1"},
    ],
    "features": [closed_outline()],
}


def test_op_route_refuses_with_next_and_mints_no_job(store: Store) -> None:
    pcb = PcbHandler(hub=Hub(store=store))
    pcb.put(id="retired-route", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="retired-route")
    assert ref is not None

    with pytest.raises(BadInput, match="routing is retired") as exc:
        pcb.put(id="retired-route", args={"op": "route"})
    next_text = str(exc.value.next)
    assert "op':'place'" in next_text
    assert "'placer':'topo'" in next_text
    assert "'route':True" in next_text

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'job' AND parent_id = %s",
            (ref.id,),
        ).fetchone()
    assert row == (0,)


def test_pcb_route_is_gone_from_the_job_type_registry() -> None:
    assert get_job_type("pcb_route") is None
    assert "pcb_route" not in known_job_types()


def test_op_place_still_enqueues(store: Store) -> None:
    """The one job op left: ``place`` mints a pcb_place job on the same
    board the route op just refused."""
    pcb = PcbHandler(hub=Hub(store=store))
    pcb.put(id="retired-route", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="retired-route")
    assert ref is not None

    pcb.put(id="retired-route", args={"op": "place"})
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.meta->>'job_type' FROM refs r "
            "WHERE r.kind = 'job' AND r.parent_id = %s",
            (ref.id,),
        ).fetchall()
    assert [r[0] for r in rows] == ["pcb_place"]

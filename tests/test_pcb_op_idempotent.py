"""Repeating the SAME ``put(kind='pcb', args={'op': ...})`` is a no-op
(gr266041 follow-up): the enqueued ops collapse onto one job, and every
inline op that writes state leaves the pcb tables and the design ref
untouched on its second identical call.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from tests._pcb_outline import closed_outline

_DESIGN: dict[str, Any] = {
    "features": [closed_outline()],
    "components": [
        {"refdes": "A", "label": "ic", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "B", "label": "ic", "x": 2.0, "y": 2.0, "pins": [{"name": "1"}]},
    ],
    "nets": [{"name": "N1", "class": "signal"}, {"name": "GND", "class": "gnd"}],
    "connections": [
        {"net": "N1", "refdes": "A", "pin": "1"},
        {"net": "N1", "refdes": "B", "pin": "1"},
    ],
}

_STACKUP = [
    {"name": "F.Cu", "role": "signal"},
    {"name": "In1.Cu", "role": "plane", "plane_net": "GND"},
    {"name": "In2.Cu", "role": "signal"},
    {"name": "B.Cu", "role": "signal"},
]


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _state(store: Any, ref_id: int) -> dict[str, Any]:
    """Content of every ``pcb_*`` table (timestamps excluded -- a rewrite of
    identical content is still a no-op for the design) plus the design
    ref's row (its ``updated_at`` INCLUDED: a bumped ref is a new
    revision) and the job-row count."""
    out: dict[str, Any] = {}
    with store.pool.connection() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() "
                "AND table_name LIKE 'pcb\\_%' ORDER BY 1"
            ).fetchall()
        ]
        for t in tables:
            stamps = [
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s "
                    "AND data_type LIKE 'timestamp%%'",
                    (t,),
                ).fetchall()
            ]
            row = conn.execute(
                f"SELECT count(*), md5(coalesce(string_agg("
                f"(to_jsonb(_t) - %s::text[])::text, '|' "
                f"ORDER BY (to_jsonb(_t) - %s::text[])::text), '')) FROM {t} _t",
                (stamps, stamps),
            ).fetchone()
            out[t] = tuple(row) if row else None
        out["ref"] = conn.execute(
            "SELECT title, meta::text, retired_at, updated_at FROM refs "
            "WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
        out["jobs"] = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'job' AND parent_id = %s",
            (ref_id,),
        ).fetchone()
    return out


@pytest.mark.parametrize("op", ["place", "route"])
def test_enqueued_op_twice_is_one_job(pcb, store, op):
    slug = f"idem2-{op}"
    pcb.put(id=slug, args=_DESIGN)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    pcb.put(id=slug, args={"op": op})
    mid = _state(store, ref.id)
    pcb.put(id=slug, args={"op": op})
    assert _state(store, ref.id) == mid
    assert mid["jobs"] == (1,)


_INLINE: list[tuple[str, dict[str, Any]]] = [
    ("move", {"refdes": "A", "x": 3.0, "y": 4.0, "rot": 90}),
    ("move", {"refdes": "A", "fixed": "xy"}),
    ("rip", {"net": "N1"}),
    ("pin_side", {"net": "N1", "a": "A.1", "b": "B.1", "side": 1}),
    ("plane_net", {"layer": "In1.Cu", "net": "GND"}),
    ("class_rules", {"name": "signal", "rules": {"clearance_mm": 0.2}}),
    ("phase", {"phase": "placed"}),
    ("pins", {"refdes": "A", "pins": [{"name": "1", "etype": "passive"}]}),
    ("stackup", {"layers": _STACKUP}),
    ("pin_rename", {"refdes": "A", "pin": "1", "to": "X1"}),
    ("pin_remove", {"refdes": "A", "pin": "1"}),
]


@pytest.mark.parametrize(
    ("op", "extra"), _INLINE, ids=[f"{o}-{i}" for i, (o, _) in enumerate(_INLINE)]
)
def test_inline_op_twice_changes_nothing(pcb, store, op, extra):
    slug = f"idem2-inline-{op}"
    pcb.put(id=slug, args=_DESIGN)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    args = {"op": op, **extra}
    pcb.put(id=slug, args=args)
    after_first = _state(store, ref.id)
    pcb.put(id=slug, args=args)
    assert _state(store, ref.id) == after_first

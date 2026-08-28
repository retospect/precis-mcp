"""``pcb_route`` job_type (pcb-guided-place-route Slice 10) — the enqueued
half of ``put(kind='pcb', args={'op':'route'})``.

Drives ``pcb_route._dispatch`` directly against a minimal fake
``DispatchContext`` (mirrors ``tests/workers/test_embed_batch.py``'s
pattern), asserting the persisted-sketch + derived-copper write-back
rather than the optimizer's own move-generation logic (that's
``tests/test_pcb_optimize.py``'s job).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis.workers.job_types import pcb_route

pytestmark = pytest.mark.db


class _FakeCtx:
    def __init__(self, store: Store, *, params: dict[str, Any]) -> None:
        self.store = store
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="pcb_route test",
            meta={"executor": "job_inproc", "job_type": "pcb_route"},
        )
        self.ref_id = int(ref.id)
        self.title = "pcb_route test"
        self.meta: dict[str, Any] = {"params": params}
        self.failures: list[tuple[str, str | None]] = []
        self.summaries: list[tuple[str, str]] = []

    def record_failure(self, reason: str, *, failure_class: str | None = None) -> None:
        self.failures.append((reason, failure_class))

    def append_chunk(self, kind: str, text: str) -> None:
        self.summaries.append((kind, text))

    def set_status(self, value: str) -> None:  # pragma: no cover — unused here
        pass

    def set_meta(self, **_kw: Any) -> None:  # pragma: no cover — unused here
        pass

    def is_cancel_requested(self) -> bool:  # pragma: no cover — unused here
        return False


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
}


def _seed(store: Store, slug: str, args: dict[str, Any]) -> int:
    handler = PcbHandler(hub=Hub(store=store))
    handler.put(id=slug, args=args)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return int(ref.id)


def test_pcb_route_writes_realized_route_and_copper(store: Store) -> None:
    ref_id = _seed(store, "route-x", _DESIGN)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 500, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]

    assert not ctx.failures
    assert ctx.summaries and ctx.summaries[0][0] == "job_summary"

    status_rows = store.pcb_route_status(ref_id)
    assert len(status_rows) == 1
    assert status_rows[0]["status"] == "realized"

    board_id = store.pcb_ensure_board(ref_id)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM pcb_copper WHERE board_id = %s", (board_id,)
        ).fetchone()
        assert row is not None
        n_copper = row[0]
    assert n_copper >= 1


def test_pcb_route_persists_sketch_survives_rebuild(store: Store) -> None:
    """The settled topology/layer_assign lands in ``pcb_routes`` keyed by
    (a, b) pin pairs — durable across a fresh IR build, not the ephemeral
    in-memory ``seg_id``."""
    ref_id = _seed(store, "route-persist", _DESIGN)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 200, "seed": 3})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]

    routes = store.pcb_routes_get(ref_id)
    assert "N1" in routes
    entry = routes["N1"]
    assert entry["topology"], "expected a persisted per-segment side choice"
    seg = entry["topology"][0]
    assert {"U1.1", "R1.1"} == {seg["a"], seg["b"]}


def test_pcb_route_applies_authored_plane_assignment(store: Store) -> None:
    """``op='plane_net'`` writes ``pcb_planes``; the route job must re-apply
    it onto the fresh IR every run (nothing else does — the IR is rebuilt
    from scratch each time) so the net's pins dog-bone fan out instead of
    routing point-to-point."""
    ref_id = _seed(store, "route-plane", _DESIGN)
    plane_id = store.pcb_assign_plane(ref_id, "In1.Cu", "N1")
    assert plane_id
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 200, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]

    assert not ctx.failures
    board_id = store.pcb_ensure_board(ref_id)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT geom->>'is_dogbone' FROM pcb_copper WHERE board_id = %s",
            (board_id,),
        ).fetchall()
    assert rows and all(r[0] == "true" for r in rows)


def test_pcb_route_is_rerunnable(store: Store) -> None:
    ref_id = _seed(store, "route-rerun", _DESIGN)
    ctx1 = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 200, "seed": 1})
    pcb_route._dispatch(ctx1, pcb_route.SPEC)  # type: ignore[arg-type]
    ctx2 = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 200, "seed": 2})
    pcb_route._dispatch(ctx2, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx2.failures
    status_rows = store.pcb_route_status(ref_id)
    assert len(status_rows) == 1  # upsert, not a duplicate row


def test_pcb_route_fails_legibly_on_no_nets(store: Store) -> None:
    handler = PcbHandler(hub=Hub(store=store))
    handler.put(
        id="route-empty",
        args={
            "components": [{"refdes": "U1", "label": "mcu", "pins": [{"name": "1"}]}]
        },
    )
    ref = store.get_ref(kind="pcb", id="route-empty")
    assert ref is not None
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref.id})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]
    assert ctx.failures
    assert "no nets" in ctx.failures[0][0]

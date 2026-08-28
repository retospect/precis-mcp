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
from precis.workers.auto_check_evaluators import route_complete
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


#: A design mixing a real 2-member net (N1) with a legitimate <2-member
#: ("dangling") one — TP_NET, a test-point net with a single connection.
#: Dangling nets are legal (test point / NC / mounting-hole nets), never a
#: routing failure — see the dangling-net-exemption test below.
_DESIGN_WITH_DANGLING = {
    "components": [
        {"refdes": "U1", "label": "mcu", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "R1", "label": "r", "x": 5.0, "y": 0.0, "pins": [{"name": "1"}]},
        {
            "refdes": "TP1",
            "label": "test point",
            "x": 2.0,
            "y": 3.0,
            "pins": [{"name": "1"}],
        },
    ],
    "nets": [{"name": "N1", "class": "signal"}, {"name": "TP_NET"}],
    "connections": [
        {"net": "N1", "refdes": "U1", "pin": "1"},
        {"net": "N1", "refdes": "R1", "pin": "1"},
        {"net": "TP_NET", "refdes": "TP1", "pin": "1"},
    ],
}


#: A high-current power net -- the exact regression named in the task:
#: pcb_copper.geom.width_mm for a 5A rail must not come out at the old
#: flat 0.25mm default (a fuse), on EITHER an outer or inner layer.
_DESIGN_HIGH_CURRENT = {
    "components": [
        {"refdes": "U1", "label": "buck", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "U2", "label": "load", "x": 5.0, "y": 0.0, "pins": [{"name": "1"}]},
    ],
    "nets": [{"name": "VBUS", "class": "power", "current": 5.0}],
    "connections": [
        {"net": "VBUS", "refdes": "U1", "pin": "1"},
        {"net": "VBUS", "refdes": "U2", "pin": "1"},
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


def test_pcb_route_widens_a_high_current_net_well_past_the_old_flat_default(
    store: Store,
) -> None:
    """pcb-usb-c-pd-nano-testboard.md's Gap A, closed at the actual write
    path: the persisted copper's ``width_mm`` for a 5A net must reflect
    IPC-2221 sizing (multi-mm, on either an outer or an inner layer),
    never the old flat 0.25mm default that used to fire here regardless
    of current."""
    ref_id = _seed(store, "route-current", _DESIGN_HIGH_CURRENT)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 500, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx.failures

    board_id = store.pcb_ensure_board(ref_id)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT geom->>'width_mm' FROM pcb_copper WHERE board_id = %s", (board_id,)
        ).fetchall()
    assert rows
    widths = [float(r[0]) for r in rows]
    assert all(w > 1.0 for w in widths)  # nowhere near the 0.25mm fuse
    assert all(w != pytest.approx(0.25) for w in widths)


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


def test_pcb_route_dangling_net_does_not_block_route_complete(store: Store) -> None:
    """A legitimate <2-member ("dangling") net — a test point, NC net, or
    mounting-hole net — must not permanently wedge ``route_complete``: it
    never gets a segment to route, so it must still get an explicit
    terminal ``pcb_routes`` row (not silently no row at all, which reads
    as ``'unrouted'`` forever)."""
    ref_id = _seed(store, "route-dangling", _DESIGN_WITH_DANGLING)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 500, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]

    assert not ctx.failures
    status_by_name = {r["name"]: r for r in store.pcb_route_status(ref_id)}
    assert status_by_name["N1"]["status"] == "realized"
    assert status_by_name["TP_NET"]["status"] == "realized"
    assert status_by_name["TP_NET"]["note"]  # names the reason, not a bare status

    assert route_complete.evaluate(store, {"pcb": ref_id}) is True


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

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
        # `ctype = 'track'` only (2026-08-28): `pcb_copper` can now also
        # carry `ctype = 'via'` rows (realize.py emits real via geometry
        # at a layer transition, and the optimizer's own `via_count` term
        # -- see precis.pcb.cost/rules -- is exactly what makes a
        # LAYER_ASSIGN move that creates one no longer free, so whether one
        # survives annealing is genuinely seed/board dependent). A via's
        # `geom` has no `width_mm` key -- this test is about TRACK width,
        # so it must not silently pick up an unrelated via row.
        rows = conn.execute(
            "SELECT geom->>'width_mm' FROM pcb_copper "
            "WHERE board_id = %s AND ctype = 'track'",
            (board_id,),
        ).fetchall()
    assert rows
    widths = [float(r[0]) for r in rows]
    assert all(w > 1.0 for w in widths)  # nowhere near the 0.25mm fuse
    assert all(w != pytest.approx(0.25) for w in widths)


def test_pcb_route_persists_realized_vias_at_a_layer_transition(store: Store) -> None:
    """End-to-end: ``pcb_copper`` must gain ``ctype='via'`` rows, carrying a
    layer SPAN (never a scalar ``layer`` inside ``geom``) and a
    current-derived via count, once a net's route actually changes layer —
    the exact production gap this task closes (the master backlog: "no via
    geometry is realized, so every via DRC rule never fires").

    **This used to force the layer change by pre-authoring a
    ``layer_assign`` override, and no longer can.** The maze router
    (``RealizeConfig.router='maze'``, the production default since
    2026-08-28) chooses its own layers — ``seg_layer`` is the sketch's
    preference, not an instruction to the router — so the only way to
    make a via appear is to make one *necessary*. That is what the wall
    of grounded parts below does: it seals the direct path on the pad
    layer, leaving a via as the cheapest remaining route. Testing the
    persistence shape through a genuinely-required via is a better test
    than testing it through a stipulated one anyway.
    """
    design = {
        "components": [
            {
                "refdes": "U1",
                "label": "buck",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "U2",
                "label": "load",
                "x": 24.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
            },
            # A picket fence of grounded pads straight across the gap, tight
            # enough that no VBUS trace fits between any two of them.
            *(
                {
                    "refdes": f"W{i}",
                    "label": "wall",
                    "x": 12.0,
                    "y": -8.0 + i * 1.1,
                    "pins": [{"name": "1"}],
                }
                for i in range(16)
            ),
        ],
        "nets": [
            {"name": "VBUS", "class": "power", "current": 5.0},
            {"name": "GND", "class": "ground"},
        ],
        "connections": [
            {"net": "VBUS", "refdes": "U1", "pin": "1"},
            {"net": "VBUS", "refdes": "U2", "pin": "1"},
            *({"net": "GND", "refdes": f"W{i}", "pin": "1"} for i in range(16)),
        ],
    }
    ref_id = _seed(store, "route-via", design)
    board_id = store.pcb_ensure_board(ref_id)
    # iters=1: the parts are authored with explicit coordinates and the
    # anneal must not move the wall out of the way before the router
    # meets it.
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 1, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx.failures

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT geom FROM pcb_copper WHERE board_id = %s AND ctype = 'via'",
            (board_id,),
        ).fetchall()
    assert rows, "expected at least one realized via at the layer transition"
    for (geom,) in rows:
        assert "layer" not in geom  # never a scalar layer -- span only
        lo, hi = geom["span"]
        assert lo != hi, "a via that spans one layer is not a via"
        assert lo == "F.Cu"  # the pad layer it must leave from
        assert geom["dia_mm"] > 0
        assert geom["drill_mm"] > 0
    # VBUS carries a 5A annotation -- a single via cannot carry it, so more
    # than one via must have been stitched in (via ampacity, not just via
    # geometry).
    assert len(rows) > 1


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


def test_pcb_route_persists_optimizer_derived_plane_promotion(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr267526: the anneal freely mutates ``net_plane_layer`` via
    ``PLANE_PROMOTE``/``PLANE_DEMOTE`` moves, but nothing used to persist
    the result — the job dropped it at exit and ``view='planes'`` reported
    "no plane assignments yet" even after the search settled on a
    promotion. Forces the settled decision deterministically (rather than
    relying on the anneal happening to pick it, which is what
    ``tests/test_pcb_reference_end_to_end.py`` measures over a real
    board) so this test is about the write-back plumbing, not search
    heuristics."""
    real_optimize = pcb_route.optimize

    def _force_promote(ir: Any, config: Any) -> Any:
        result = real_optimize(ir, config)
        net_id = {str(ir.net_name[n]): n for n in range(ir.n_nets)}["N1"]
        ir.promote_plane(net_id, 1)  # In1.Cu — role 'plane' in DEFAULT_STACKUP
        return result

    monkeypatch.setattr(pcb_route, "optimize", _force_promote)

    ref_id = _seed(store, "route-derived-plane", _DESIGN)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx.failures

    rows = store.pcb_planes_list(ref_id)
    assert rows == [
        {"layer": "In1.Cu", "net": "N1", "region_hint": None, "source": "derived"}
    ]

    handler = PcbHandler(hub=Hub(store=store))
    view = handler.get(id="route-derived-plane", view="planes")
    assert "derived" in view.body
    assert "N1" in view.body


def test_pcb_route_never_touches_an_authored_plane_assignment(store: Store) -> None:
    """The one that matters most (gr267526): an authored ``op='plane_net'``
    row is a human instruction and must survive a route run byte-for-byte
    — never deleted, never silently replaced by whatever the anneal
    happened to settle on for that net."""
    ref_id = _seed(store, "route-authored-plane", _DESIGN)
    plane_id = store.pcb_assign_plane(ref_id, "In1.Cu", "N1")
    assert plane_id

    before = store.pcb_planes_list(ref_id)
    assert before == [
        {"layer": "In1.Cu", "net": "N1", "region_hint": None, "source": "authored"}
    ]

    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 200, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx.failures

    after = store.pcb_planes_list(ref_id)
    assert after == before  # untouched: same layer, same net, still 'authored'

    handler = PcbHandler(hub=Hub(store=store))
    view = handler.get(id="route-authored-plane", view="planes")
    assert "authored" in view.body


def test_pcb_route_replaces_derived_plane_rows_across_reruns(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second route run's derived write-back must REPLACE the first
    run's derived rows, never accumulate duplicates alongside them
    (``pcb_planes_replace_derived``'s own DELETE+INSERT discipline, same
    as ``pcb_copper_replace``)."""
    real_optimize = pcb_route.optimize

    def _force_promote(ir: Any, config: Any) -> Any:
        result = real_optimize(ir, config)
        net_id = {str(ir.net_name[n]): n for n in range(ir.n_nets)}["N1"]
        ir.promote_plane(net_id, 1)
        return result

    monkeypatch.setattr(pcb_route, "optimize", _force_promote)

    ref_id = _seed(store, "route-derived-rerun", _DESIGN)
    ctx1 = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1})
    pcb_route._dispatch(ctx1, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx1.failures
    ctx2 = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 2})
    pcb_route._dispatch(ctx2, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx2.failures

    rows = store.pcb_planes_list(ref_id)
    assert len(rows) == 1  # upsert-by-replace, not a second accumulated row
    assert rows[0]["source"] == "derived"


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

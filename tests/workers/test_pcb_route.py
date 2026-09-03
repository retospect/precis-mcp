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
from precis.pcb import session as pcb_session
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
    routing point-to-point.

    **A stub is not a connection.** This asserted only that every copper
    row was a dogbone, which a board with no vias and no pour satisfies
    perfectly — and that is exactly what the engine emitted: pads, stubs,
    and a plane layer with nothing on it. The drop via and the pour are
    the connection, so they are what this pins now.
    """
    ref_id = _seed(
        store,
        "route-plane",
        # A pour needs a board to be poured onto. Without an authored
        # outline it has no extent, and _outline_clip is right that
        # inventing one would constrain a design that never asked to be —
        # so the engine reports the net unrouted instead. That path has its
        # own test below; this one is about the plane working.
        {
            **_DESIGN,
            "features": [
                {
                    "ftype": "outline",
                    "geom": {
                        "path": [[-5.0, -5.0], [15.0, -5.0], [15.0, 10.0], [-5.0, 10.0]]
                    },
                }
            ],
        },
    )
    plane_id = store.pcb_assign_plane(ref_id, "In1.Cu", "N1")
    assert plane_id
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 200, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]

    assert not ctx.failures
    board_id = store.pcb_ensure_board(ref_id)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ctype, layer, geom->>'is_dogbone' FROM pcb_copper "
            "WHERE board_id = %s",
            (board_id,),
        ).fetchall()
    tracks = [r for r in rows if r[0] == "track"]
    assert tracks and all(r[2] == "true" for r in tracks), (
        "a plane-served net's traces are dog-bone stubs, not routed traces"
    )
    assert [r for r in rows if r[0] == "via"], (
        "every stub needs a via down to the plane, or the pad reaches nothing"
    )
    pours = [r for r in rows if r[0] == "pour"]
    assert pours and all(r[1] == "In1.Cu" for r in pours), (
        "the plane layer must actually carry copper on the assigned layer"
    )


def test_plane_net_with_no_board_outline_is_reported_not_silently_stranded(
    store: Store,
) -> None:
    """A pour has no extent without a board outline, so a net promoted to a
    plane on an outline-less design cannot be connected. The engine must
    SAY so: its segments come back unrouted and the net's route row is not
    ``'realized'``.

    The failure this pins is not "no pour" — it is a board that reports
    itself finished while a whole net's pins reach a via that opens onto an
    empty layer. Every geometric rule passes such a board comfortably,
    because they all ask about proximity and it has no copper to be close
    to anything.
    """
    ref_id = _seed(store, "route-plane-noboard", _DESIGN)
    assert store.pcb_assign_plane(ref_id, "In1.Cu", "N1")
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 200, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]

    assert not ctx.failures
    rows = {r["name"]: r for r in store.pcb_route_status(ref_id)}
    assert rows.get("N1", {}).get("status") != "realized", (
        "a net whose plane was never poured is not routed, and saying it is "
        "is the exact silence this rule exists to break"
    )
    # docs/backlog/pcb-engine-plan.md "BOARD TWO" finding 2's sibling note:
    # this used to be reported as generic ``unrouted`` with no distinguishing
    # cause. ``pcb_routes.note`` (this table's own per-net summary) and
    # ``pcb_routes.fail.problems[].reason`` (the per-segment detail) must
    # both name it as an unpourable plane specifically, not just "unrouted".
    assert "unpourable_plane" in (rows["N1"].get("note") or ""), rows["N1"]
    routes = store.pcb_routes_get(ref_id)
    problems = routes["N1"]["fail"]["problems"]
    assert any(p.get("reason") == "unpourable_plane" for p in problems), problems


def test_pcb_route_persists_optimizer_derived_plane_promotion(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr267526: the anneal freely mutates ``net_plane_layers`` via
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
        # Clear first. `promote_plane` ORs a bit in, so promoting on top of
        # whatever the real anneal happened to settle on leaves a TWO-bit
        # mask, and the write-back's `{net: layer for ... for layer in
        # plane_layers_of(...)}` then keeps whichever layer comes last —
        # making this test a reading of the search heuristic it explicitly
        # says it is not about. (It went red exactly that way when the
        # courtyard change moved every placement.) Production cannot reach
        # a two-bit derived mask: `_gen_plane_promote` only offers a bare
        # net a single layer, which is the assumption the write-back's own
        # comment records.
        ir.demote_plane(net_id)
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


def test_pcb_route_locks_an_authored_plane_through_the_anneal(store: Store) -> None:
    """gr267526's sharper form, found with no DB and no router: this cost
    model has no term that wants a plane (measured: 79 PLANE_PROMOTE
    proposals over 3000 iterations, all rejected on cost), so an
    unlocked authored assignment gets PLANE_DEMOTEd and never recovers —
    correct in the persisted row, absent from the realized board, because
    realization reads the POST-ANNEAL IR, not the DB row the comment above
    used to protect. ``OptimizeConfig.locked_plane_nets`` excludes an
    AUTHORED net from PLANE_DEMOTE.

    This checks the REALIZED COPPER, not just ``net_plane_layers`` /
    ``pcb_planes`` — an IR-level assertion alone would pass on a board
    whose pour path silently produced nothing, which is exactly the shape
    of defect this whole file is written around."""
    design = {
        **_DESIGN,
        "features": [
            {
                "ftype": "outline",
                "geom": {
                    "path": [[-5.0, -5.0], [15.0, -5.0], [15.0, 10.0], [-5.0, 10.0]]
                },
            }
        ],
    }
    ref_id = _seed(store, "route-plane-locked", design)
    assert store.pcb_assign_plane(ref_id, "In1.Cu", "N1")

    # 3000 iterations -- the figure the demotion was actually measured at;
    # a short run may not exercise enough PLANE_DEMOTE proposals to be a
    # real test of the lock.
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 3000, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx.failures

    planes = store.pcb_planes_list(ref_id)
    assert planes == [
        {"layer": "In1.Cu", "net": "N1", "region_hint": None, "source": "authored"}
    ], "the authored row itself must survive, same discipline as the sibling test above"

    board_id = store.pcb_ensure_board(ref_id)
    with store.pool.connection() as conn:
        pours = conn.execute(
            "SELECT geom FROM pcb_copper WHERE board_id = %s AND ctype = 'pour'",
            (board_id,),
        ).fetchall()
    assert pours, (
        "an authored plane net must actually be POURED, not merely persisted -- "
        "a demoted-then-reverted net never reaches _pour_planes at all"
    )


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


def test_instance_rotation_survives_the_store_round_trip(store: Store) -> None:
    """``rot`` was WRITE-ONLY: ``pcb_set_pose`` persisted it, ``pcb_graph``
    never selected it, and ``ir.from_graph`` hardcoded ``inst_rot`` to
    zeros. So every IR rebuilt from the store came back with every part at
    0 degrees.

    The consequence was not cosmetic and not local. Placement's settled
    rotations never reached routing, and — because a pin's coordinate is
    the instance pose composed with the land pattern — no rebuilt IR could
    reproduce the pin coordinates of the copper it was looking at. The DRC
    view, which rebuilds an IR to locate pads, therefore measured a board
    whose parts had all been turned back to north: ten nets on the
    reference board came back "disconnected" from that alone.

    Nothing crashed and no rule fired, because rotation is not something
    any geometric rule checks. It just quietly moves every pad.
    """
    ref_id = _seed(store, "route-rot", _DESIGN)
    store.pcb_set_pose(ref_id, {"U1": (3.0, 4.0, 90.0), "R1": (9.0, 4.0, 270.0)})

    graph = store.pcb_graph(ref_id)
    by_refdes = {i["refdes"]: i for i in graph["instances"]}
    assert by_refdes["U1"]["rot"] == 90.0
    assert by_refdes["R1"]["rot"] == 270.0

    ir = pcb_session.build_ir(graph)
    rots = {
        str(ir.instance_refdes[i]): float(ir.inst_rot[i]) for i in range(ir.n_instances)
    }
    assert rots == {"U1": 90.0, "R1": 270.0}


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


# ── PIN_SWAP persistence (docs/backlog/pcb-engine-plan.md "PIN_SWAP is not
# persisted") ───────────────────────────────────────────────────────────
#
# PIN_SWAP is dormant in production: `OptimizeConfig.pin_swap_groups`
# defaults to `()`, `_gen_pin_swap` returns `None` whenever it is empty, and
# nothing in `src/` ever sets it — `pcb_route._dispatch` builds its
# `OptimizeConfig` with no `pin_swap_groups=` at all. Firing the move for
# real needs a caller-supplied admissible pin-equivalence set (which of an
# instance's pins are genuinely interchangeable — datasheet-derived
# domain knowledge :mod:`precis.pcb.pinswap` deliberately never invents),
# and no such data source (footprint pin-function / swap-group) exists in
# the schema yet. These tests therefore force a settled swap the same way
# `test_pcb_route_persists_optimizer_derived_plane_promotion` forces a
# settled plane promotion above: deterministically, via a spy on the
# imported `optimize` name, rather than relying on `pin_swap_groups` ever
# being populated by this job today.
_PIN_SWAP_DESIGN = {
    "components": [
        {
            "refdes": "U0",
            "label": "mcu",
            "x": 0.0,
            "y": 0.0,
            "pins": [{"name": "left"}, {"name": "right"}],
        },
        {"refdes": "U1", "label": "ic", "x": -5.0, "y": 5.0, "pins": [{"name": "1"}]},
        {"refdes": "U2", "label": "ic", "x": 5.0, "y": 5.0, "pins": [{"name": "1"}]},
    ],
    "nets": [{"name": "A", "class": "signal"}, {"name": "B", "class": "signal"}],
    "connections": [
        {"net": "A", "refdes": "U0", "pin": "right"},
        {"net": "A", "refdes": "U1", "pin": "1"},
        {"net": "B", "refdes": "U0", "pin": "left"},
        {"net": "B", "refdes": "U2", "pin": "1"},
    ],
}


def _swap_u0_pins(ir: Any) -> None:
    pin_right = next(
        p
        for p in range(ir.n_pins)
        if str(ir.pin_label[p]) == "right"
        and str(ir.instance_refdes[int(ir.pin_instance[p])]) == "U0"
    )
    pin_left = next(
        p
        for p in range(ir.n_pins)
        if str(ir.pin_label[p]) == "left"
        and str(ir.instance_refdes[int(ir.pin_instance[p])]) == "U0"
    )
    ir.swap_pins(pin_left, pin_right)


def test_pcb_route_persists_optimizer_derived_pin_swap(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The anneal's ``swap_pins`` result must survive the job — persisted
    as a DERIVED override, never as a rewrite of the authored netlist
    itself (``pcb_netconns`` stays exactly what was authored; the override
    is layered on top, mirroring ``pcb_planes``'s authored/derived split,
    gr267526)."""
    real_optimize = pcb_route.optimize

    def _force_swap(ir: Any, config: Any) -> Any:
        _swap_u0_pins(ir)
        return real_optimize(ir, config)

    monkeypatch.setattr(pcb_route, "optimize", _force_swap)

    ref_id = _seed(store, "route-pinswap", _PIN_SWAP_DESIGN)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx.failures

    swaps = {(r["refdes"], r["pin"]): r for r in store.pcb_pin_swaps_list(ref_id)}
    assert swaps[("U0", "right")]["net"] == "B"
    assert swaps[("U0", "left")]["net"] == "A"
    assert all(r["source"] == "derived" for r in swaps.values())

    # pcb_netconns — the authored netlist — is untouched: still U0.right on
    # A and U0.left on B. The override lives ONLY in pcb_pin_swaps.
    graph = store.pcb_graph(ref_id)
    members = {
        n["name"]: {(m["refdes"], m["pin"]) for m in n["members"]}
        for n in graph["nets"]
    }
    assert members["A"] == {("U0", "right"), ("U1", "1")}
    assert members["B"] == {("U0", "left"), ("U2", "1")}


def test_pcb_route_reapplies_persisted_pin_swap_on_fresh_ir(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persisted swap not re-applied on the NEXT rebuild is worse than no
    persistence at all — the copper and the netlist would then disagree in
    the other direction. Pinned by forcing a swap on run 1, then spying on
    run 2's fresh IR BEFORE its own ``optimize()`` call sees it."""
    real_optimize = pcb_route.optimize

    def _force_swap(ir: Any, config: Any) -> Any:
        _swap_u0_pins(ir)
        return real_optimize(ir, config)

    monkeypatch.setattr(pcb_route, "optimize", _force_swap)

    ref_id = _seed(store, "route-pinswap-rebuild", _PIN_SWAP_DESIGN)
    ctx1 = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1})
    pcb_route._dispatch(ctx1, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx1.failures
    assert store.pcb_pin_swaps_list(ref_id)

    seen: dict[str, str] = {}

    def _spy(ir: Any, config: Any) -> Any:
        for p in range(ir.n_pins):
            inst = int(ir.pin_instance[p])
            if str(ir.instance_refdes[inst]) == "U0":
                seen[str(ir.pin_label[p])] = str(ir.net_name[int(ir.pin_net[p])])
        return real_optimize(ir, config)

    monkeypatch.setattr(pcb_route, "optimize", _spy)
    ctx2 = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 2})
    pcb_route._dispatch(ctx2, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx2.failures

    assert seen == {"right": "B", "left": "A"}, (
        "the FRESH IR handed to optimize() on run 2 must already carry the "
        "settled swap from run 1 -- otherwise run 2 would route a netlist "
        "that disagrees with what run 1 persisted"
    )


def test_pcb_place_reapplies_persisted_pin_swap(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pcb_place`` cannot propose PIN_SWAP (``_PLACE_ONLY_SCHEDULE`` has
    no PIN_SWAP weight) but must still see a persisted swap's effect on the
    netlist it places against — same read-only re-application as its
    plane fix (``tests/workers/test_pcb_place.py``)."""
    from precis.workers.job_types import pcb_place

    real_route_optimize = pcb_route.optimize

    def _force_swap(ir: Any, config: Any) -> Any:
        _swap_u0_pins(ir)
        return real_route_optimize(ir, config)

    monkeypatch.setattr(pcb_route, "optimize", _force_swap)
    ref_id = _seed(store, "place-pinswap", _PIN_SWAP_DESIGN)
    route_ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1})
    pcb_route._dispatch(route_ctx, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not route_ctx.failures
    assert store.pcb_pin_swaps_list(ref_id)

    seen: dict[str, str] = {}
    real_place_optimize = pcb_place.optimize

    def _spy(ir: Any, config: Any) -> Any:
        for p in range(ir.n_pins):
            inst = int(ir.pin_instance[p])
            if str(ir.instance_refdes[inst]) == "U0":
                seen[str(ir.pin_label[p])] = str(ir.net_name[int(ir.pin_net[p])])
        return real_place_optimize(ir, config)

    monkeypatch.setattr(pcb_place, "optimize", _spy)
    place_ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 3})
    pcb_place._dispatch(place_ctx, pcb_place.SPEC)  # type: ignore[arg-type]
    assert not place_ctx.failures

    assert seen == {"right": "B", "left": "A"}


def test_pcb_route_never_touches_an_authored_pin_swap(store: Store) -> None:
    """No authoring verb exists yet for ``op='pin_swap'`` (out of this
    change's scope), but the discipline is wired ahead of it: an
    ``authored`` row (seeded directly here, standing in for a future
    authoring call — both endpoints of the swap, the shape a real
    authoring call would produce) must survive a route run byte-for-byte,
    exactly like
    :func:`test_pcb_route_never_touches_an_authored_plane_assignment`."""
    ref_id = _seed(store, "route-pinswap-authored", _PIN_SWAP_DESIGN)
    board_id = store.pcb_ensure_board(ref_id)
    n = store.pcb_pin_swaps_replace_derived(
        ref_id,
        board_id,
        [
            {"refdes": "U0", "pin": "right", "net": "B"},
            {"refdes": "U0", "pin": "left", "net": "A"},
        ],
    )
    assert n == 2
    with store.tx() as conn:
        conn.execute(
            'UPDATE pcb_pin_swaps SET meta = meta || \'{"source": "authored"}\' '
            "WHERE board_id = %s",
            (board_id,),
        )

    before = store.pcb_pin_swaps_list(ref_id)
    assert before == [
        {"refdes": "U0", "pin": "left", "net": "A", "source": "authored"},
        {"refdes": "U0", "pin": "right", "net": "B", "source": "authored"},
    ]

    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 200, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx.failures

    after = store.pcb_pin_swaps_list(ref_id)
    assert after == before  # untouched: still authored, still swapped


def test_routed_net_is_never_failed_by_a_placement_chord_crossing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely fully-routed net must never be reported failed/unrouted
    solely because its PLACEMENT-time centroid chord crosses another net's.

    `_residual_crossings` sweeps `segment_points` — instance-centroid
    straight chords, placement-fidelity geometry — while the maze router's
    real copper is crossing-free by construction (claimed on a shared
    occupancy grid before it is drawn). Before the round-7 fix, that sweep
    flagged finished nets as "unrouted", with the phantom set reshuffling
    on every placement draw (user-visible as pins "not connected to
    anything" on nets whose copper was fine — root-caused 2026-09-03).
    Forcing the sweep to report a crossing for every net makes the
    regression deterministic instead of placement-dependent.
    """

    def _always_crossing(ir: Any, plane_net_ids: set[int]) -> dict[str, Any]:
        return {
            str(ir.net_name[n]): [
                {
                    "kind": "same-layer-crossing",
                    "reason": "same-layer-crossing",
                    "layer": 0,
                    "with": "phantom",
                }
            ]
            for n in range(ir.n_nets)
        }

    monkeypatch.setattr(pcb_route, "_residual_crossings", _always_crossing)
    ref_id = _seed(store, "route-chord-phantom", _DESIGN)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 500, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]

    assert not ctx.failures
    status_rows = store.pcb_route_status(ref_id)
    assert len(status_rows) == 1
    assert status_rows[0]["status"] == "realized"  # not "failed"


def test_residual_crossing_entries_carry_a_reason_for_the_note() -> None:
    """The per-net failure note only surfaces `reason` values — a
    kind-only crossing entry used to fail a net with note=None (a failure
    with no WHY). Pin the shape, not the sweep."""
    import inspect

    src = inspect.getsource(pcb_route._residual_crossings)
    assert '"reason": "same-layer-crossing"' in src

"""pcb-pre-place-route-blocks Slice 1 -- the ROUTER's consumption of
AUTHORED fixed copper (docs/backlog/pcb-pre-place-route-blocks.md,
"The architectural crux" / Slices / "Realize seam"). The storage +
emission seam (``pcb_fixed_copper`` round-tripping through
:meth:`~precis.store._pcb_ops.PcbMixin.pcb_fixed_copper_put`/``_list``)
already has its own coverage in ``tests/test_pcb_fixed_copper.py``; this
module is the OTHER half — what the router does with those rows once
they exist:

* a ratsnest segment whose two pins are already bridged by fixed copper
  is never handed to the maze search at all (:func:`precis.pcb.realize.
  realize`'s ``fixed_copper=`` keyword, :attr:`~precis.pcb.realize.
  RealizeResult.fixed_realized`) -- exercised directly against
  :func:`~precis.pcb.realize.realize` (no DB) for speed/determinism, and
  once through the real ``pcb_route`` job to check the persisted
  ``pcb_routes.status``/``note``.
* fixed copper is a real occupancy-grid obstacle for every OTHER net,
  exactly like a pad -- exercised the same way, at ``realize()`` level
  (no anneal jitter to control for).
* the router never feeds fixed rows back into the DERIVED ``pcb_copper``
  table, and re-realizing never touches ``pcb_fixed_copper`` -- exercised
  at the job/store level, since that is where the two tables actually
  live.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import DEFAULT_STACKUP
from precis.pcb import connectivity as pcb_connectivity
from precis.pcb.ir import from_graph
from precis.pcb.realize import RealizeConfig, realize, to_gerber_model
from precis.store import Store
from precis.workers.job_types import pcb_route

pytestmark = pytest.mark.db


# ── no-DB: realize() driven directly against a tiny hand-built IR ───────


def _two_pin_graph(
    a_xy: tuple[float, float], b_xy: tuple[float, float], *, net: str = "N1"
) -> dict[str, Any]:
    return {
        "instances": [
            {"refdes": "U1", "x": a_xy[0], "y": a_xy[1]},
            {"refdes": "U2", "x": b_xy[0], "y": b_xy[1]},
        ],
        "nets": [
            {
                "name": net,
                "members": [
                    {"refdes": "U1", "pin": "1"},
                    {"refdes": "U2", "pin": "1"},
                ],
            }
        ],
    }


def test_realize_never_routes_a_segment_fully_bridged_by_fixed_copper():
    """(a) A 2-pin net whose two pins already sit on one fixed track needs
    no route: the segment is reported in ``fixed_realized``, never in
    ``tracks`` (no NEW copper for a connection fixed copper already
    made) and never in ``unrouted`` (it was never searched at all)."""
    ir = from_graph(_two_pin_graph((0.0, 0.0), (2.0, 0.0)), stackup=DEFAULT_STACKUP)
    fixed_copper = [
        {
            "ctype": "track",
            "layer": "F.Cu",
            "net": "N1",
            "segments": [{"shape": "line", "start": [0.0, 0.0], "end": [2.0, 0.0]}],
            "width_mm": 0.2,
        }
    ]
    result = realize(ir, config=RealizeConfig(router="maze"), fixed_copper=fixed_copper)
    assert result.fixed_realized == (0,)
    assert result.tracks == ()
    assert result.unrouted == ()


def test_realize_with_no_fixed_copper_routes_normally():
    """Baseline: the SAME board with no ``fixed_copper`` argument routes
    the connection itself -- the short-circuit above is conditional on
    the keyword, never the default behaviour."""
    ir = from_graph(_two_pin_graph((0.0, 0.0), (2.0, 0.0)), stackup=DEFAULT_STACKUP)
    result = realize(ir, config=RealizeConfig(router="maze"))
    assert result.fixed_realized == ()
    assert result.tracks, "the same connection with no fixed copper must self-route"


def test_realize_a_via_bridges_two_coincident_pins():
    """(a) variant, ``ctype='via'``: two pins placed at the SAME (x, y) --
    a real, if unusual, pattern (a via-in-pad / coincident test point) --
    joined only by a fixed VIA's own disk, no track at all. The
    connectivity short-circuit must follow a via's ``span`` exactly like
    it follows a track's segments, not just the line-geometry path the
    test above exercises."""
    graph = _two_pin_graph((0.0, 0.0), (0.0, 0.0), net="N1")
    ir = from_graph(graph, stackup=DEFAULT_STACKUP)
    fixed_copper = [
        {
            "ctype": "via",
            "layer": "F.Cu",
            "net": "N1",
            "x": 0.0,
            "y": 0.0,
            "dia_mm": 0.45,
            "drill_mm": 0.2,
            "span": ["F.Cu", "B.Cu"],
        }
    ]
    result = realize(ir, config=RealizeConfig(router="maze"), fixed_copper=fixed_copper)
    assert result.fixed_realized == (0,)
    assert result.tracks == ()
    assert result.vias == ()


def test_realize_claims_fixed_copper_as_an_obstacle_for_a_foreign_net():
    """(b) A fixed track on net A, spanning the FULL height of a narrow
    board on both signal layers, sits directly across the only corridor
    net B's two pins could route through -- B must come out unrouted
    (walled in on every allowed layer, never merely losing a congestion
    race, since the probe grid now claims fixed copper too), and no
    derived track of B may ever cross the wall's own claimed radius."""
    graph = {
        "instances": [
            # WALL sits ON the wall's own centerline (not off-board) --
            # `maze.grid_for` never clips the routing grid below where a
            # real pad sits (its own docstring), so an anchor placed
            # OUTSIDE the intended narrow strip would silently widen the
            # grid and give B room to detour around the whole scenario.
            {"refdes": "WALL", "x": 5.0, "y": 0.0},
            {"refdes": "B1", "x": 0.0, "y": 0.0},
            {"refdes": "B2", "x": 10.0, "y": 0.0},
        ],
        "nets": [
            {"name": "A", "members": [{"refdes": "WALL", "pin": "1"}]},
            {
                "name": "B",
                "members": [
                    {"refdes": "B1", "pin": "1"},
                    {"refdes": "B2", "pin": "1"},
                ],
            },
        ],
    }
    outline = [(-2.0, -1.0), (12.0, -1.0), (12.0, 1.0), (-2.0, 1.0)]
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=outline)
    # Wide enough (width/2 + default clearance) to clear the whole 2mm-tall
    # strip on both routable layers -- a genuine total block, not merely a
    # tight squeeze.
    fixed_copper = [
        {
            "ctype": "track",
            "layer": layer,
            "net": "A",
            "segments": [{"shape": "line", "start": [5.0, -1.0], "end": [5.0, 1.0]}],
            "width_mm": 4.0,
        }
        for layer in ("F.Cu", "B.Cu")
    ]
    result = realize(ir, config=RealizeConfig(router="maze"), fixed_copper=fixed_copper)
    seg_id = 0  # net A is dangling (1 member -> 0 segments); B's is the only one
    assert seg_id in result.unrouted
    assert result.fixed_realized == ()
    reason = next(r for r in result.unrouted_reasons if r.seg_id == seg_id)
    assert reason.kind == "no_path", reason.message

    wall_lo, wall_hi = 5.0 - 2.15, 5.0 + 2.15  # width/2 + clearance, with slack
    for t in result.tracks:
        for seg in t.segments:
            for pt in (seg["start"], seg["end"]):
                assert not (wall_lo < pt[0] < wall_hi), (t.net_id, seg)


# ── island terminals: routing FROM a fixed via/track, not just onto one ──
#
# pcb-pre-place-route-blocks, gr339236: the EWOD generator's escape net is
# {electrode pad, F.Cu stub, via to B.Cu} + a driver pin -- pad A's own
# F.Cu neighbourhood is walled off entirely (a dense field of foreign
# claims, or here a deliberate ring), so a plain pad-to-pad search never
# finds a path even though the AUTHORED stub+via physically bridges it.
# `RealizeResult.island_terminals` is the seam that lets the search start
# from the via's own centre instead.
#
# The board: pin A sealed inside a small closed ring (net "WALLNET", both
# routable layers -- a genuine total block, same construction as the "(b)"
# wall test above) with an authored stub+via reaching OUT to an open
# "plaza" site V; a second F.Cu-only wall segment sits between V and pin B,
# so any successful route is forced to travel V's B.Cu terminal across the
# gap and cross back to F.Cu only once it reaches B.

_ISLAND_OUTLINE = [[-4.0, -6.0], [20.0, -6.0], [20.0, 6.0], [-4.0, 6.0]]
_ISLAND_VIA_XY = (6.0, 0.0)


def _island_terminal_graph() -> dict[str, Any]:
    return {
        "instances": [
            {"refdes": "A", "x": 0.0, "y": 0.0},
            {"refdes": "B", "x": 16.0, "y": 0.0},
            # Dangling -- exists only so the "WALLNET" name resolves to a
            # real net id for `_claim_fixed_copper`'s by-name lookup, same
            # convention the "(b)" wall test above already uses. Placed
            # well clear of the ring/wall geometry below so its own pad
            # claim cannot interfere.
            {"refdes": "WALLPIN", "x": 0.0, "y": 5.0},
        ],
        "nets": [
            {
                "name": "N1",
                "members": [{"refdes": "A", "pin": "1"}, {"refdes": "B", "pin": "1"}],
            },
            {"name": "WALLNET", "members": [{"refdes": "WALLPIN", "pin": "1"}]},
        ],
    }


def _wall_ring_rows() -> list[dict[str, Any]]:
    """A closed 4x4mm square around pin A (0, 0), on both routable layers
    -- a genuine total block by the same "width/2 + clearance, with slack"
    reasoning the "(b)" wall test above already validates, just folded
    into a ring instead of a single strip."""
    corners = [(-2.0, -2.0), (-2.0, 2.0), (2.0, 2.0), (2.0, -2.0)]
    return [
        {
            "ctype": "track",
            "layer": layer,
            "net": "WALLNET",
            "segments": [
                {
                    "shape": "line",
                    "start": list(corners[i]),
                    "end": list(corners[(i + 1) % 4]),
                }
            ],
            "width_mm": 1.0,
        }
        for layer in ("F.Cu", "B.Cu")
        for i in range(4)
    ]


_WALL2_ROW = {
    # F.Cu ONLY -- B.Cu stays open, so the only way from V to B is across
    # B.Cu, which is the point: this is what proves the derived route
    # actually USES the via's B.Cu terminal rather than just being offered
    # it.
    "ctype": "track",
    "layer": "F.Cu",
    "net": "WALLNET",
    "segments": [{"shape": "line", "start": [10.0, -6.0], "end": [10.0, 6.0]}],
    "width_mm": 4.0,
}

# Ring + wall AFTER the stub/via in list order, so their claims win the
# grid cells where the stub's straight line to V crosses the ring's own
# right edge -- `_claim_fixed_copper` stamps unconditionally in list
# order, and pin A must stay genuinely SEALED on the router's occupancy
# grid (the scenario this test exists for), even though the fixed-copper
# CONNECTIVITY graph (purely geometric, order-independent) still correctly
# sees the stub bridging A to the via regardless.
_ISLAND_FIXED_COPPER = [
    {
        "ctype": "track",
        "layer": "F.Cu",
        "net": "N1",
        "segments": [
            {"shape": "line", "start": [0.0, 0.0], "end": list(_ISLAND_VIA_XY)}
        ],
        "width_mm": 0.2,
    },
    {
        "ctype": "via",
        "layer": "F.Cu",
        "net": "N1",
        "x": _ISLAND_VIA_XY[0],
        "y": _ISLAND_VIA_XY[1],
        "dia_mm": 0.5,
        "drill_mm": 0.25,
        "span": ["F.Cu", "B.Cu"],
    },
    *_wall_ring_rows(),
    _WALL2_ROW,
]


def test_realize_routes_from_a_fixed_via_terminal_when_the_pad_corridor_is_sealed():
    """(a) Pin A is walled in on every routable layer except through its
    own authored stub+via; the router must find that route by seeding the
    search on the via's terminals, not by walking pad A's own (sealed)
    neighbourhood. The derived copper must actually TOUCH the via -- not
    merely land near it -- so `net_islands` sees N1 as one piece once the
    fixed and derived copper are combined."""
    graph = _island_terminal_graph()
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=_ISLAND_OUTLINE)
    result = realize(
        ir, config=RealizeConfig(router="maze"), fixed_copper=_ISLAND_FIXED_COPPER
    )
    assert result.unrouted == (), [r.message for r in result.unrouted_reasons]
    seg_id = 0

    # (3) the honesty seam: this segment's route is reported as having
    # used a fixed terminal, naming the via and the layer it landed on.
    assert seg_id in result.island_terminals
    note = result.island_terminals[seg_id]
    assert "fixed via" in note, note
    assert "B.Cu" in note, note

    # The derived track must start EXACTLY at the via centre, on B.Cu --
    # not near it, and not on F.Cu (which stays walled off past the ring
    # and blocked entirely between V and B by `_WALL2_ROW`).
    b_cu_tracks = [t for t in result.tracks if t.seg_id == seg_id and t.layer == 3]
    assert b_cu_tracks, "expected at least one B.Cu track for this segment"
    assert any(
        t.segments[0]["start"] == pytest.approx(list(_ISLAND_VIA_XY), abs=1e-6)
        for t in b_cu_tracks
    ), [t.segments[0] for t in b_cu_tracks]

    # `net_islands`, run against fixed + derived copper together (the same
    # combination a real DRC pass checks), must see N1 as ONE piece --
    # the derived track genuinely touches the via, not merely nearby.
    layers = [str(layer.get("name")) for layer in ir.stackup]
    model = to_gerber_model(result, ir, layers=layers, outline=_ISLAND_OUTLINE)
    model["copper"] = list(model["copper"]) + [
        {**row, "net": row.get("net") or ""} for row in _ISLAND_FIXED_COPPER
    ]
    islands = pcb_connectivity.net_islands(model)
    assert not any(isl.net == "N1" for isl in islands), islands


def test_realize_without_the_fixed_via_stays_unrouted_pad_sealed_by_wall():
    """(b) The SAME board with the stub+via dropped (ring + F.Cu wall
    only): pin A has no way out at all -- guards that the TERMINAL, not
    some incidentally relaxed claim, is what made (a) succeed. Same
    visible ``no_path`` reason the "(b)" wall test above gets for an
    equivalent total seal."""
    graph = _island_terminal_graph()
    ir = from_graph(graph, stackup=DEFAULT_STACKUP, outline=_ISLAND_OUTLINE)
    fixed_copper = [*_wall_ring_rows(), _WALL2_ROW]
    result = realize(ir, config=RealizeConfig(router="maze"), fixed_copper=fixed_copper)
    seg_id = 0
    assert seg_id in result.unrouted
    assert result.island_terminals == {}
    reason = next(r for r in result.unrouted_reasons if r.seg_id == seg_id)
    assert reason.kind == "no_path", reason.message


def test_realize_fully_bridged_segment_is_unaffected_by_island_terminal_search():
    """(c) No regression: a segment whose both pins are ALREADY bridged by
    fixed copper is still short-circuited into ``fixed_realized`` and
    never reaches the maze search at all -- the island-terminal machinery
    (which only ever touches segments the search receives) must not
    change that."""
    ir = from_graph(_two_pin_graph((0.0, 0.0), (2.0, 0.0)), stackup=DEFAULT_STACKUP)
    fixed_copper = [
        {
            "ctype": "track",
            "layer": "F.Cu",
            "net": "N1",
            "segments": [{"shape": "line", "start": [0.0, 0.0], "end": [2.0, 0.0]}],
            "width_mm": 0.2,
        }
    ]
    result = realize(ir, config=RealizeConfig(router="maze"), fixed_copper=fixed_copper)
    assert result.fixed_realized == (0,)
    assert result.tracks == ()
    assert result.unrouted == ()
    assert result.island_terminals == {}


# ── DB: the real pcb_route job ───────────────────────────────────────────


class _FakeCtx:
    """Mirrors ``tests/workers/test_pcb_route.py``'s own fake dispatch
    context -- duplicated rather than imported, same "small self-contained
    test helper" convention that module's own docstring follows."""

    def __init__(self, store: Store, *, params: dict[str, Any]) -> None:
        self.store = store
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="pcb_route fixed-copper test",
            meta={"executor": "job_inproc", "job_type": "pcb_route"},
        )
        self.ref_id = int(ref.id)
        self.title = "pcb_route fixed-copper test"
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


def _seed(store: Store, slug: str, design: dict[str, Any]) -> int:
    handler = PcbHandler(hub=Hub(store=store))
    handler.put(id=slug, args=design)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return int(ref.id)


_DESIGN_BRIDGED = {
    "components": [
        {
            "refdes": "U1",
            "label": "mcu",
            "x": 0.0,
            "y": 0.0,
            "pins": [{"name": "1"}],
            "fixed": "both",
        },
        {
            "refdes": "R1",
            "label": "r",
            "x": 2.0,
            "y": 0.0,
            "pins": [{"name": "1"}],
            "fixed": "both",
        },
    ],
    "nets": [{"name": "N1", "class": "signal"}],
    "connections": [
        {"net": "N1", "refdes": "U1", "pin": "1"},
        {"net": "N1", "refdes": "R1", "pin": "1"},
    ],
}


def test_pcb_route_job_marks_a_fixed_bridged_net_realized_with_no_derived_track(
    store: Store,
) -> None:
    """(a), end to end: a fixed track bridging N1's only segment means the
    ``pcb_route`` job writes ``status='realized'`` with a note naming why,
    and ``pcb_copper`` gets no derived track for N1 at all."""
    ref_id = _seed(store, "fxroute-a", _DESIGN_BRIDGED)
    board_id = store.pcb_ensure_board(ref_id)
    store.pcb_fixed_copper_put(
        ref_id,
        board_id,
        "GENX",
        "fake_gen",
        "1",
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "N1",
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [0.0, 0.0], "end": [2.0, 0.0]}
                    ],
                    "width_mm": 0.2,
                },
            }
        ],
    )

    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1})
    pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]
    assert not ctx.failures

    status_rows = {r["name"]: r for r in store.pcb_route_status(ref_id)}
    assert status_rows["N1"]["status"] == "realized"
    assert status_rows["N1"]["note"] == "realized by fixed copper"

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM pcb_copper WHERE board_id = %s AND ctype = 'track'",
            (board_id,),
        ).fetchone()
        assert row is not None
        (n_derived_tracks,) = row
    assert n_derived_tracks == 0


_DESIGN_ROUNDTRIP = {
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


def test_realize_twice_leaves_fixed_copper_untouched_and_never_leaks_into_derived(
    store: Store,
) -> None:
    """(c) Two full route runs: ``pcb_fixed_copper`` is read-only from the
    router's side (row count and content identical before/after both
    runs), and the fixed row's own geometry never shows up inside
    ``pcb_copper`` -- the derived table stays router-owned-only, run after
    run."""
    ref_id = _seed(store, "fxroute-c", _DESIGN_ROUNDTRIP)
    board_id = store.pcb_ensure_board(ref_id)
    # A fixed track well away from N1's own corridor -- present so this
    # design carries fixed copper at all, deliberately NOT bridging N1
    # (that is test (a)'s job), so the router still has real, un-short-
    # circuited routing to do on every run.
    store.pcb_fixed_copper_put(
        ref_id,
        board_id,
        "SIDEGEN",
        "fake_gen",
        "1",
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "N1",
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [8.0, 8.0], "end": [9.0, 8.0]}
                    ],
                    "width_mm": 0.2,
                },
            }
        ],
    )
    before = store.pcb_fixed_copper_list(board_id)
    assert len(before) == 1

    for seed in (1, 2):
        ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 200, "seed": seed})
        pcb_route._dispatch(ctx, pcb_route.SPEC)  # type: ignore[arg-type]
        assert not ctx.failures

        after = store.pcb_fixed_copper_list(board_id)
        assert len(after) == len(before) == 1
        assert after[0]["ctype"] == before[0]["ctype"]
        assert after[0]["net"] == before[0]["net"]
        assert after[0]["segments"] == before[0]["segments"]
        assert after[0]["fixed"] is True

        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT geom FROM pcb_copper WHERE board_id = %s", (board_id,)
            ).fetchall()
        for (geom,) in rows:
            for seg in (geom or {}).get("segments") or []:
                # The fixed row's own coordinates must never appear inside
                # a DERIVED pcb_copper row -- pcb_copper_replace's own
                # docstring forbids feeding pcb_copper_list's union back;
                # this is that promise, checked against the real table.
                assert seg.get("start") != [8.0, 8.0]
                assert seg.get("end") != [9.0, 8.0]

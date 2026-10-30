"""``pcb_place`` job_type (pcb-guided-place-route Slice 10) — the enqueued
half of ``put(kind='pcb', args={'op':'place'})``.

Drives ``pcb_place._dispatch`` directly against a minimal fake
``DispatchContext`` (mirrors ``tests/workers/test_embed_batch.py``'s
pattern) rather than a full ``job_inproc`` claim/run cycle — this pins the
job_type's OWN logic (does it actually move instances, respect ``fixed``,
reduce crossings).
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis.workers.job_types import pcb_place
from tests._pcb_outline import closed_outline

pytestmark = pytest.mark.db


class _FakeCtx:
    def __init__(self, store: Store, *, params: dict[str, Any]) -> None:
        self.store = store
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="pcb_place test",
            meta={"executor": "job_inproc", "job_type": "pcb_place"},
        )
        self.ref_id = int(ref.id)
        self.title = "pcb_place test"
        self.meta: dict[str, Any] = {"params": params}
        self.failures: list[tuple[str, str | None]] = []
        self.summaries: list[tuple[str, str]] = []

    def record_failure(self, reason: str, *, failure_class: str | None = None) -> None:
        self.failures.append((reason, failure_class))

    def append_chunk(self, kind: str, text: str) -> None:
        self.summaries.append((kind, text))

    def set_status(self, value: str) -> None:  # pragma: no cover — unused here
        pass

    def set_meta(self, **kw: Any) -> None:
        self.meta.update(kw)

    def is_cancel_requested(self) -> bool:  # pragma: no cover — unused here
        return False


_CROSSED: dict[str, Any] = {
    "components": [
        {"refdes": "A", "label": "ic", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "B", "label": "ic", "x": 2.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "C", "label": "ic", "x": 0.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "D", "label": "ic", "x": 2.0, "y": 0.0, "pins": [{"name": "1"}]},
    ],
    "nets": [
        {"name": "N1", "class": "signal"},
        {"name": "N2", "class": "signal"},
    ],
    "connections": [
        {"net": "N1", "refdes": "A", "pin": "1"},
        {"net": "N1", "refdes": "B", "pin": "1"},
        {"net": "N2", "refdes": "C", "pin": "1"},
        {"net": "N2", "refdes": "D", "pin": "1"},
    ],
}


def _dispatch(ctx: _FakeCtx) -> None:
    """Run the place job on a fake context (duck-typed JobContext)."""
    pcb_place._dispatch(ctx, pcb_place.SPEC)  # type: ignore[arg-type]


def _seed(store: Store, slug: str, args: dict[str, Any]) -> int:
    handler = PcbHandler(hub=Hub(store=store))
    handler.put(id=slug, args=args)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return int(ref.id)


def test_pcb_place_moves_instances_and_reduces_crossings(store: Store) -> None:
    ref_id = _seed(store, "place-x", _CROSSED)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 2000, "seed": 1})
    _dispatch(ctx)

    assert not ctx.failures
    assert ctx.summaries and ctx.summaries[0][0] == "job_summary"

    graph = store.pcb_graph(ref_id)
    placed = {i["refdes"]: (i["x"], i["y"]) for i in graph["instances"]}
    assert all(x is not None and y is not None for x, y in placed.values())

    from precis.pcb import ratsnest

    wires = ratsnest.build_airwires(placed, graph["nets"])
    assert len(ratsnest.crossings(wires)) == 0


def test_pcb_place_refuses_to_commit_a_worse_placement(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr489911: on taz15 the anneal moved every part off its own fixed
    source copper and the job WROTE the poses with 186 pre-route errors
    over a stored placement with 37. The gate is now measured before and
    after the anneal: a worse count refuses the write (nothing persisted),
    ``force=True`` writes it and says so in the summary.

    The board carries a closed outline so the stored placement is clean:
    without one the gate's ``outline_missing`` (gr500431) puts an error on
    BOTH sides and the "stored one has 0" baseline is gone."""
    ref_id = _seed(store, "place-worse", {**_CROSSED, "features": [closed_outline()]})
    before = {
        i["refdes"]: (i["x"], i["y"]) for i in store.pcb_graph(ref_id)["instances"]
    }
    real_optimize = pcb_place.optimize

    def _collapse(ir: Any, config: Any) -> Any:
        result = real_optimize(ir, config)
        # Park every part on A: four pads of two nets on one spot.
        ir.inst_x[:] = ir.inst_x[0]
        ir.inst_y[:] = ir.inst_y[0]
        return result

    monkeypatch.setattr(pcb_place, "optimize", _collapse)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1})
    _dispatch(ctx)
    assert ctx.failures and ctx.failures[0][1] == "non-convergence"
    assert "refusing to commit" in ctx.failures[0][0]
    assert "the stored one has 0" in ctx.failures[0][0]
    assert not ctx.summaries
    after = {
        i["refdes"]: (i["x"], i["y"]) for i in store.pcb_graph(ref_id)["instances"]
    }
    assert after == before, "a refused placement must not be written"

    forced = _FakeCtx(
        store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1, "force": True}
    )
    _dispatch(forced)
    assert not forced.failures
    assert "OVERRIDDEN (force=True)" in forced.summaries[0][1]
    written = {
        i["refdes"]: (i["x"], i["y"]) for i in store.pcb_graph(ref_id)["instances"]
    }
    assert len({tuple(v) for v in written.values()}) == 1


def test_pcb_place_first_placement_is_not_judged_against_an_unplaced_board(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A never-placed board measures ~0 errors before the anneal (NaN poses
    are skipped by the gate), which is no baseline: a first placement with
    a residual error must still be written and reported, not refused."""
    design = {
        **_CROSSED,
        "components": [
            {k: v for k, v in c.items() if k not in ("x", "y")}
            for c in _CROSSED["components"]
        ],
    }
    ref_id = _seed(store, "place-first", design)
    real_optimize = pcb_place.optimize

    def _collapse(ir: Any, config: Any) -> Any:
        result = real_optimize(ir, config)
        ir.inst_x[:] = ir.inst_x[0]
        ir.inst_y[:] = ir.inst_y[0]
        return result

    monkeypatch.setattr(pcb_place, "optimize", _collapse)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1})
    _dispatch(ctx)
    assert not ctx.failures
    assert "pre-route DRC error(s)" in ctx.summaries[0][1]
    assert "OVERRIDDEN" not in ctx.summaries[0][1]


def test_pcb_place_never_moves_a_fixed_instance(store: Store) -> None:
    args = {
        "components": [
            {
                "refdes": "J1",
                "label": "conn",
                "x": 0.0,
                "y": 0.0,
                "fixed": "xy",
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "U1",
                "label": "ic",
                "x": 40.0,
                "y": 40.0,
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "U2",
                "label": "ic",
                "x": 41.0,
                "y": 40.0,
                "pins": [{"name": "1"}],
            },
        ],
        "nets": [{"name": "N", "class": "signal"}],
        "connections": [
            {"net": "N", "refdes": "U1", "pin": "1"},
            {"net": "N", "refdes": "U2", "pin": "1"},
        ],
    }
    ref_id = _seed(store, "place-fixed", args)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 300, "seed": 2})
    _dispatch(ctx)

    assert not ctx.failures
    graph = store.pcb_graph(ref_id)
    j1 = next(i for i in graph["instances"] if i["refdes"] == "J1")
    assert (j1["x"], j1["y"]) == (0.0, 0.0)


def test_pcb_place_snap_and_soft_pin_report_and_stamp(store: Store) -> None:
    """``snap=true`` runs the preferred-number pass and stamps
    ``meta.preferred_snap`` on every part it moved (a part the default
    field polish already rastered onto its grid is not moved again); a ``max_travel_mm``
    instance is reported on the summary's soft-pin line. The frozen part
    is never snapped."""
    args = {
        "components": [
            {
                "refdes": "A",
                "label": "ic",
                "x": 0.0,
                "y": 0.0,
                "fixed": "both",
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "B",
                "label": "ic",
                "x": 8.0,
                "y": 8.0,
                "max_travel_mm": 2.0,
                "pins": [{"name": "1"}, {"name": "2"}],
            },
            {
                "refdes": "C",
                "label": "ic",
                "x": 30.13,
                "y": 30.37,
                "pins": [{"name": str(p)} for p in range(1, 7)],
            },
        ],
        "nets": [{"name": "N", "class": "signal"}],
        "connections": [
            {"net": "N", "refdes": "A", "pin": "1"},
            {"net": "N", "refdes": "B", "pin": "1"},
        ],
    }
    ref_id = _seed(store, "place-snap", args)
    ctx = _FakeCtx(
        store,
        params={"pcb_ref_id": ref_id, "iters": 100, "seed": 1, "snap": True},
    )
    _dispatch(ctx)

    assert not ctx.failures
    summary = ctx.summaries[0][1]
    assert "soft-pinned travel used: B " in summary
    assert "preferred snap:" in summary

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT refdes, meta->'preferred_snap' FROM pcb_instances "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
    snapped = {r[0]: r[1] for r in rows if r[1] is not None}
    assert "A" not in snapped  # frozen parts are never snapped
    assert snapped  # the snap stamped what it moved
    # C ends on its 0.5mm preferred grid. A part already on its own grid
    # gives the snap nothing to move and — per its "parts that actually
    # moved" contract — nothing to stamp.
    placed = {i["refdes"]: i for i in store.pcb_graph(ref_id)["instances"]}
    for axis in ("x", "y"):
        v = float(placed["C"][axis]) / 0.5
        assert abs(v - round(v)) < 1e-6, placed["C"]
    if "C" in snapped:
        assert snapped["C"]["pitch_mm"] == 0.5
    if "B" in snapped:
        # a soft pin may only snap inside its leash
        assert abs(snapped["B"]["x"] - 8.0) <= 2.0 + 1e-9
        assert abs(snapped["B"]["y"] - 8.0) <= 2.0 + 1e-9


def test_pcb_place_reapplies_persisted_plane_without_writing_back(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A human's authored ``op='plane_net'`` declaration must be visible to
    the PLACEMENT anneal too (not just ``pcb_route``'s later re-apply) — see
    ``pcb_place.py``'s docstring comment on the write-boundary. Pinned two
    ways: (1) the IR the optimizer actually runs against has
    ``net_plane_layers`` set for the declared net BEFORE ``optimize()`` is
    called (a spy on the imported ``optimize`` name), and (2) this job never
    writes back to ``pcb_planes`` — it cannot change a plane assignment
    (``_PLACE_ONLY_SCHEDULE`` carries no PLANE_PROMOTE/DEMOTE weight), so
    only the authored row may exist afterward."""
    ref_id = _seed(store, "place-plane", _CROSSED)
    store.pcb_assign_plane(ref_id, "In1.Cu", "N1")

    seen_layers: list[int] = []
    real_optimize = pcb_place.optimize

    def _spy_optimize(ir: Any, config: Any) -> Any:
        net_id = next(n for n in range(ir.n_nets) if str(ir.net_name[n]) == "N1")
        seen_layers.append(int(ir.net_plane_layers[net_id]))
        return real_optimize(ir, config)

    monkeypatch.setattr(pcb_place, "optimize", _spy_optimize)

    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 200, "seed": 3})
    _dispatch(ctx)

    assert not ctx.failures

    assert seen_layers and seen_layers[0] != 0, (
        "the IR handed to optimize() must already have the authored plane "
        "promoted -- placement must see it as fixed context, not learn "
        "about it only after pcb_route re-applies it"
    )

    planes = store.pcb_planes_list(ref_id)
    assert len(planes) == 1
    assert planes[0]["net"] == "N1"
    assert planes[0]["source"] == "authored", (
        "pcb_place cannot change a plane assignment (no PLANE_* move in "
        "its schedule) -- it must never write one back"
    )


def test_pcb_place_honors_an_authored_proximity_measure(store: Store) -> None:
    """The defect this job existed to fix: an authored `put(args={
    'measures':[...]})` proximity/separation intent used to be MEASURED
    (`eyes.py`, `get(view='measures')`) and honoured only by the legacy
    `place.autoplace` quick placer — never by THIS job, the one
    `put(args={'op':'place'})` actually enqueues (precis.pcb.optimize's
    production anneal). Two unconnected instances (no shared net, so
    wirelength/crossings give this job no OTHER reason to move them
    together), seeded 30mm apart, with an authored 'soft' proximity goal
    of 8mm — after a real dispatch through the store (put -> pcb_apply ->
    pcb_measures_list -> resolve_measures -> optimize()) they land close
    to that goal, not merely wherever the initial seed left them."""
    args = {
        "components": [
            {
                "refdes": "A",
                "label": "part",
                "x": 0.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "B",
                "label": "part",
                "x": 30.0,
                "y": 0.0,
                "pins": [{"name": "1"}],
            },
        ],
        "nets": [],
        "connections": [],
        "measures": [
            {
                "metric": "proximity",
                "operands": [{"instance": "A"}, {"instance": "B"}],
                "goal": 8,
                "strength": "soft",
                "reason": "crystal hugs the MCU",
            }
        ],
    }
    ref_id = _seed(store, "place-measure", args)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 6000, "seed": 1})
    _dispatch(ctx)

    assert not ctx.failures
    graph = store.pcb_graph(ref_id)
    placed = {i["refdes"]: (i["x"], i["y"]) for i in graph["instances"]}
    dist = math.hypot(placed["A"][0] - placed["B"][0], placed["A"][1] - placed["B"][1])
    assert dist <= 12.0  # a few mm of the 8mm goal -- not still ~30mm apart


def _part(refdes: str, x: float, fixed: str | None = None) -> dict[str, Any]:
    part: dict[str, Any] = {
        "refdes": refdes,
        "label": "r",
        "x": x,
        "y": 0.0,
        "pins": [{"name": "1"}],
    }
    if fixed:
        part["fixed"] = fixed
    return part


def _track(net: str, start: list[float], end: list[float]) -> dict[str, Any]:
    return {
        "ctype": "track",
        "layer": "F.Cu",
        "net": net,
        "geom": {
            "segments": [{"shape": "line", "start": start, "end": end}],
            "width_mm": 0.2,
        },
    }


#: gr489911 part 2: U1 (locked) -- N1 track -- R1 (unlocked), and U2 --
#: N2 track -- U3 (both locked). Moving R1 must rip N1's track only.
_RIPUP: dict[str, Any] = {
    "components": [
        _part("U1", 0.0, "both"),
        _part("R1", 2.0),
        _part("U2", 10.0, "both"),
        _part("U3", 12.0, "both"),
    ],
    "nets": [{"name": "N1", "class": "signal"}, {"name": "N2", "class": "signal"}],
    "connections": [
        {"net": "N1", "refdes": "U1", "pin": "1"},
        {"net": "N1", "refdes": "R1", "pin": "1"},
        {"net": "N2", "refdes": "U2", "pin": "1"},
        {"net": "N2", "refdes": "U3", "pin": "1"},
    ],
}


def _seed_ripup(store: Store, slug: str, design: dict[str, Any]) -> tuple[int, int]:
    ref_id = _seed(store, slug, design)
    board_id = store.pcb_ensure_board(ref_id)
    store.pcb_fixed_copper_put(
        ref_id,
        board_id,
        "SRC",
        "epro",
        "1",
        [_track("N1", [0.0, 0.0], [2.0, 0.0]), _track("N2", [10.0, 0.0], [12.0, 0.0])],
    )
    return ref_id, board_id


def _instances(store: Store, ref_id: int) -> dict[str, tuple[Any, ...]]:
    return {
        i["refdes"]: (i["x"], i["y"], i["rot"], i.get("fixed"))
        for i in store.pcb_graph(ref_id)["instances"]
    }


def test_pcb_place_rips_up_the_fixed_copper_of_a_moved_part(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reto 2026-10-16 (gr489911 part 2): moving an unlocked part rips the
    fixed copper on its pads, which becomes unrouted work; copper between
    parts that did not move stays, and locked parts keep pose and lock."""
    ref_id, board_id = _seed_ripup(store, "place-ripup", _RIPUP)
    before = _instances(store, ref_id)
    real_optimize = pcb_place.optimize

    def _shift_r1(ir: Any, config: Any) -> Any:
        result = real_optimize(ir, config)
        i = list(ir.instance_refdes).index("R1")
        ir.inst_x[i], ir.inst_y[i] = 2.0, 6.0
        return result

    monkeypatch.setattr(pcb_place, "optimize", _shift_r1)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1})
    _dispatch(ctx)

    assert not ctx.failures
    assert {r["net"] for r in store.pcb_fixed_copper_list(board_id)} == {"N2"}
    assert "ripped up 1 fixed-copper row(s)" in ctx.summaries[0][1]
    assert "N1" in ctx.summaries[0][1]
    after = _instances(store, ref_id)
    assert after["R1"][:2] == (2.0, 6.0)
    for refdes in ("U1", "U2", "U3"):
        assert after[refdes] == before[refdes], refdes


def test_pcb_place_rips_up_fixed_copper_left_on_a_foreign_pad(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prod taz15 after jo489777: the parts had already been moved off their
    copper, so it touched no pad of its own and lay across foreign pads (172
    shorts no router can clear). A place job retires such copper even when
    this run moves nothing."""
    design = {
        **_RIPUP,
        "components": [*_RIPUP["components"], _part("R5", 6.0)],
        "nets": [*_RIPUP["nets"], {"name": "N5", "class": "signal"}],
        "connections": [
            *_RIPUP["connections"],
            {"net": "N5", "refdes": "R5", "pin": "1"},
        ],
    }
    ref_id, board_id = _seed_ripup(store, "place-stale", design)
    store.pcb_fixed_copper_put(
        ref_id, board_id, "STALE", "epro", "1", [_track("N1", [5.0, 0.0], [7.0, 0.0])]
    )
    real_optimize = pcb_place.optimize

    def _stay(ir: Any, config: Any) -> Any:
        x, y, rot = ir.inst_x.copy(), ir.inst_y.copy(), ir.inst_rot.copy()
        result = real_optimize(ir, config)
        ir.inst_x[:], ir.inst_y[:], ir.inst_rot[:] = x, y, rot
        return result

    monkeypatch.setattr(pcb_place, "optimize", _stay)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1})
    _dispatch(ctx)

    assert not ctx.failures
    kept = store.pcb_fixed_copper_list(board_id)
    assert sorted(r["generator_name"] for r in kept) == ["SRC", "SRC"]


def test_pcb_place_keeps_every_lock_and_locked_pose(store: Store) -> None:
    """gr491884 (Reto 2026-10-16, "If the part is locked, it should remain
    locked"): a real anneal never moves a locked part and never changes any
    part's ``fixed`` flag."""
    ref_id, _board_id = _seed_ripup(store, "place-locks", _RIPUP)
    before = _instances(store, ref_id)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 400, "seed": 3})
    _dispatch(ctx)

    assert not ctx.failures
    after = _instances(store, ref_id)
    assert {r: v[3] for r, v in after.items()} == {r: v[3] for r, v in before.items()}
    for refdes in ("U1", "U2", "U3"):
        assert after[refdes] == before[refdes], refdes


def test_pcb_place_never_rips_a_live_generators_copper(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generator's fabric is one unit with its parts: moving a part does
    not retire single rows of it (the import's source copper is rippable
    because it has no ``pcb_generators`` row)."""
    ref_id, board_id = _seed_ripup(store, "place-genkeep", _RIPUP)
    real_generators_for = store.pcb_generators_for
    monkeypatch.setattr(
        store,
        "pcb_generators_for",
        lambda rid: {"SRC": {}} if rid == ref_id else real_generators_for(rid),
    )
    real_optimize = pcb_place.optimize

    def _shift_r1(ir: Any, config: Any) -> Any:
        result = real_optimize(ir, config)
        i = list(ir.instance_refdes).index("R1")
        ir.inst_x[i], ir.inst_y[i] = 2.0, 6.0
        return result

    monkeypatch.setattr(pcb_place, "optimize", _shift_r1)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 50, "seed": 1})
    _dispatch(ctx)

    assert not ctx.failures
    assert {r["net"] for r in store.pcb_fixed_copper_list(board_id)} == {"N1", "N2"}
    assert "ripped up" not in ctx.summaries[0][1]


def test_pcb_place_objective_does_not_see_fixed_tracks(store: Store) -> None:
    """No rip-up cost in the placer objective (Reto 2026-10-16): the same
    seed lands every part on the same pose with and without the fixed
    tracks — the copper only decides what is ripped afterwards."""
    with_copper, _ = _seed_ripup(store, "place-obj-a", _RIPUP)
    without = _seed(store, "place-obj-b", _RIPUP)
    for ref_id in (with_copper, without):
        _dispatch(
            _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 400, "seed": 5})
        )
    poses_a = {r: v[:3] for r, v in _instances(store, with_copper).items()}
    poses_b = {r: v[:3] for r, v in _instances(store, without).items()}
    assert poses_a == poses_b


def test_pcb_place_fails_legibly_on_empty_design(store: Store) -> None:
    handler = PcbHandler(hub=Hub(store=store))
    handler.put(id="place-empty", args={})
    ref = store.get_ref(kind="pcb", id="place-empty")
    assert ref is not None
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref.id})
    _dispatch(ctx)
    assert ctx.failures
    assert "no instances" in ctx.failures[0][0]


def test_pcb_place_topo_writes_poses_and_stores_the_run(store: Store) -> None:
    """topo-place spec §1c (Decision 21): ``placer='topo'`` runs the
    phase-0 kernel, writes poses through the same ``pcb_set_pose`` tail
    (fixed parts still never move), and stores the run files on the job's
    ``meta.topo_run`` for ``view='topo'``."""
    ref_id = _seed(store, "place-topo", {**_CROSSED, "features": [closed_outline()]})
    ctx = _FakeCtx(
        store,
        params={"pcb_ref_id": ref_id, "placer": "topo", "iters": 500, "seed": 1},
    )
    _dispatch(ctx)

    assert not ctx.failures
    assert "topo-place" in ctx.summaries[0][1]
    placed = {i["refdes"]: i["x"] for i in store.pcb_graph(ref_id)["instances"]}
    assert all(x is not None for x in placed.values())
    files = ctx.meta["topo_run"]["files"]
    m = files["manifest.json"]
    assert m["schema"] == "precis.topo-run/3"
    assert m["result"]["crossings"] >= 0
    assert m["seed"] == 1 and m["effort"] == 500
    assert {p["refdes"] for p in m["parts"]} == {"A", "B", "C", "D"}
    assert files["summary.txt"].startswith("topo-place — place-topo")
    assert files["placement.svg"].startswith("<svg")


def test_pcb_place_topo_falls_back_instead_of_refusing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr540612: a worse anneal pose no longer refuses — the topo branch
    goes through the SAME pre-route DRC gate, walking the elites then
    the stored pose for the first candidate at or under the stored
    error count and succeeding with a 'failed the pre-route gate' note.
    ``force=True`` skips the fallback and commits the worse pose with
    the override note."""
    import dataclasses

    from precis.pcb import topoplace as tp

    ref_id = _seed(
        store, "place-topo-worse", {**_CROSSED, "features": [closed_outline()]}
    )
    real_topo = tp.topo_place
    captured: dict[str, Any] = {}

    def _collapse(d: Any, **kw: Any) -> Any:
        res = real_topo(d, **kw)
        captured["td"] = d
        captured["elites"] = res.elites
        pose = res.best_pose.copy()
        pose[:, 0] = pose[0, 0]
        pose[:, 1] = pose[0, 1]
        return dataclasses.replace(res, best_pose=pose)

    monkeypatch.setattr(tp, "topo_place", _collapse)
    ctx = _FakeCtx(
        store,
        params={"pcb_ref_id": ref_id, "placer": "topo", "iters": 50, "seed": 1},
    )
    _dispatch(ctx)
    assert not ctx.failures
    assert "failed the pre-route gate" in ctx.summaries[0][1]
    # The committed pose is the first gate-passing candidate: the
    # collapsed best fails, so elite 1 must be what wrote — compared
    # part for part, not merely "not all identical".
    td = captured["td"]
    elites = captured["elites"]
    assert elites
    assert "committed elite 1/" in ctx.summaries[0][1]
    expected = tp.settle_label_room(td, elites[0].pose)
    after = {
        i["refdes"]: (i["x"], i["y"]) for i in store.pcb_graph(ref_id)["instances"]
    }
    for i, refdes in enumerate(td.parts):
        assert after[refdes] == pytest.approx(
            (float(expected[i, 0]), float(expected[i, 1]))
        ), refdes

    forced = _FakeCtx(
        store,
        params={
            "pcb_ref_id": ref_id,
            "placer": "topo",
            "iters": 50,
            "seed": 1,
            "force": True,
        },
    )
    _dispatch(forced)
    assert not forced.failures
    assert "OVERRIDDEN (force=True)" in forced.summaries[0][1]


def test_pcb_place_topo_pin_conflict_is_an_unclassed_failure(
    store: Store,
) -> None:
    """A ``PinConflict`` out of the kernel (the board script's error)
    surfaces as an unclassed failure naming the contended signals/pins —
    never a half-written placement."""
    args = {
        **_CROSSED,
        "components": [
            {
                **c,
                "pins": [{"name": str(p)} for p in (1, 2, 3, 4)],
                "pin_decls": {
                    "slots": [
                        {"pins": ["1", "4"], "name": n, "net": "N1"}
                        for n in ("A1", "A2", "A3")
                    ]
                },
            }
            if c["refdes"] == "A"
            else c
            for c in _CROSSED["components"]
        ],
    }
    ref_id = _seed(store, "place-topo-conflict", args)
    ctx = _FakeCtx(
        store,
        params={"pcb_ref_id": ref_id, "placer": "topo", "iters": 50, "seed": 1},
    )
    _dispatch(ctx)
    assert ctx.failures and ctx.failures[0][1] is None
    assert "3 signals, 2 pins" in ctx.failures[0][0]
    assert "topo_run" not in ctx.meta


def test_pcb_place_topo_diverged_falls_back_to_an_elite(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr540612: a diverged long anneal must not strand the board — the
    job walks the cost-sorted elites (then the stored pose) through the
    same commit gate and writes the first candidate at or under the
    stored error count, succeeding with a 'failed the pre-route gate'
    note rather than failing with nothing placed."""
    import numpy as np

    from precis.pcb.topoplace.archive import Elite
    from precis.pcb.topoplace.placer import TopoPlaceResult

    ref_id = _seed(store, "place-diverge", {**_CROSSED, "features": [closed_outline()]})

    def _diverged(td: Any, *, objective: Any, effort: int, seed: int, **_k: Any) -> Any:
        bad = np.asarray(td.pose0, dtype=float).copy()
        bad[:, :2] = 999.0  # every part parked outside the board
        good = np.asarray(td.pose0, dtype=float).copy()
        good[0, 0] += 0.5  # elite: the stored pose, one part nudged
        return TopoPlaceResult(
            best_pose=bad,
            best_cost=9.9,
            crossings=0,
            length=0.0,
            evals=1,
            restarts=1,
            seed=seed,
            elites=(Elite(good, 0.1),),
            ladder_scores=(),
            last_improve_eval=0,
        )

    monkeypatch.setattr("precis.pcb.topoplace.topo_place", _diverged)
    monkeypatch.setattr("precis.pcb.topoplace.settle_label_room", lambda td, pose: pose)
    ctx = _FakeCtx(
        store,
        params={"pcb_ref_id": ref_id, "placer": "topo", "iters": 50, "seed": 1},
    )
    _dispatch(ctx)
    assert not ctx.failures
    assert "failed the pre-route gate" in ctx.summaries[0][1]
    assert "committed elite 1/1" in ctx.summaries[0][1]
    placed = {
        i["refdes"]: (i["x"], i["y"]) for i in store.pcb_graph(ref_id)["instances"]
    }
    assert placed["A"] == pytest.approx((0.5, 0.0))


def test_pcb_place_topo_diverged_falls_back_to_the_stored_pose(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr540612: when every elite also fails the gate, the stored pose is
    the last resort — the job still succeeds instead of writing nothing."""
    import numpy as np

    from precis.pcb.topoplace.archive import Elite
    from precis.pcb.topoplace.placer import TopoPlaceResult

    ref_id = _seed(
        store, "place-diverge2", {**_CROSSED, "features": [closed_outline()]}
    )
    before = {
        i["refdes"]: (i["x"], i["y"]) for i in store.pcb_graph(ref_id)["instances"]
    }

    def _diverged(td: Any, *, objective: Any, effort: int, seed: int, **_k: Any) -> Any:
        bad = np.asarray(td.pose0, dtype=float).copy()
        bad[:, :2] = 999.0
        worse = np.asarray(td.pose0, dtype=float).copy()
        worse[:, :2] = 888.0  # an elite that is illegal too
        return TopoPlaceResult(
            best_pose=bad,
            best_cost=9.9,
            crossings=0,
            length=0.0,
            evals=1,
            restarts=1,
            seed=seed,
            elites=(Elite(worse, 0.1),),
            ladder_scores=(),
            last_improve_eval=0,
        )

    monkeypatch.setattr("precis.pcb.topoplace.topo_place", _diverged)
    monkeypatch.setattr("precis.pcb.topoplace.settle_label_room", lambda td, pose: pose)
    ctx = _FakeCtx(
        store,
        params={"pcb_ref_id": ref_id, "placer": "topo", "iters": 50, "seed": 1},
    )
    _dispatch(ctx)
    assert not ctx.failures
    assert "failed the pre-route gate" in ctx.summaries[0][1]
    assert "committed the stored placement" in ctx.summaries[0][1]
    after = {
        i["refdes"]: (i["x"], i["y"]) for i in store.pcb_graph(ref_id)["instances"]
    }
    assert after == before


def test_pcb_place_topo_gate_counts_the_copper_the_commit_keeps(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr540612 fix1: the candidate gate must measure the same copper
    the commit's gate does — moved parts shed their fixed-copper
    branches BEFORE the error count. Judged on the full rows, R1's best
    pose reads one pad-on-copper short over the stored board (its pad
    lands on N2's track); judged after its own rip-up, zero — the best
    pose must commit, not silently fall back to the stored one and turn
    the run into a no-op."""
    import numpy as np

    from precis.pcb.topoplace.placer import TopoPlaceResult

    ref_id = _seed(
        store, "place-topo-ripup", {**_RIPUP, "features": [closed_outline()]}
    )
    board_id = store.pcb_ensure_board(ref_id)
    store.pcb_fixed_copper_put(
        ref_id,
        board_id,
        "SRC",
        "epro",
        "1",
        [
            _track("N1", [0.0, 0.0], [2.0, 0.0]),
            _track("N2", [10.0, 0.0], [20.0, 0.0]),
        ],
    )

    def _topo(td: Any, *, objective: Any, effort: int, seed: int, **_k: Any) -> Any:
        pose = np.asarray(td.pose0, dtype=float).copy()
        pose[td.parts.index("R1"), 0] = 18.0  # R1's pad lands on the N2 track
        return TopoPlaceResult(
            best_pose=pose,
            best_cost=0.1,
            crossings=0,
            length=0.0,
            evals=1,
            restarts=1,
            seed=seed,
            elites=(),
            ladder_scores=(),
            last_improve_eval=0,
        )

    monkeypatch.setattr("precis.pcb.topoplace.topo_place", _topo)
    monkeypatch.setattr("precis.pcb.topoplace.settle_label_room", lambda td, pose: pose)
    ctx = _FakeCtx(
        store,
        params={"pcb_ref_id": ref_id, "placer": "topo", "iters": 50, "seed": 1},
    )
    _dispatch(ctx)
    assert not ctx.failures
    placed = {
        i["refdes"]: (i["x"], i["y"]) for i in store.pcb_graph(ref_id)["instances"]
    }
    assert placed["R1"] == pytest.approx((18.0, 0.0))
    # Both branches retired in the same commit: N1's (its part moved)
    # and N2's (a moved pad sat on it) — the kept set the gate counted
    # zero errors against.
    assert store.pcb_fixed_copper_list(board_id) == []
    assert "ripped up 2 fixed-copper row(s)" in ctx.summaries[0][1]


def test_pcb_place_topo_refuses_when_the_stored_pose_is_not_recoverable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr540612: the stored pose is a fallback only while it still gates
    at or under the stored error count — a board whose pose the topo
    design cannot reproduce (an imported board's non-quarter rotations
    snap in ``design_from_ir``; here ``pose0`` is corrupted outright)
    has no committable candidate. The run's own best is then what the
    real gate judges, and the job still refuses a worse placement with
    nothing written."""
    import dataclasses

    import numpy as np

    from precis.pcb import topoplace as tp
    from precis.pcb.topoplace.archive import Elite
    from precis.pcb.topoplace.placer import TopoPlaceResult

    ref_id = _seed(
        store, "place-topo-refuse", {**_CROSSED, "features": [closed_outline()]}
    )
    before = {
        i["refdes"]: (i["x"], i["y"]) for i in store.pcb_graph(ref_id)["instances"]
    }
    real_design = tp.design_from_ir

    def _unrecoverable(*a: Any, **k: Any) -> Any:
        td = real_design(*a, **k)
        bad0 = np.asarray(td.pose0, dtype=float).copy()
        bad0[:, :2] = bad0[0, :2]  # pose0 cannot reproduce the stored pose
        return dataclasses.replace(td, pose0=bad0)

    def _diverged(td: Any, *, objective: Any, effort: int, seed: int, **_k: Any) -> Any:
        bad = np.asarray(td.pose0, dtype=float).copy()
        bad[:, :2] = 999.0
        worse = np.asarray(td.pose0, dtype=float).copy()
        worse[:, :2] = 888.0  # an elite that is illegal too
        return TopoPlaceResult(
            best_pose=bad,
            best_cost=9.9,
            crossings=0,
            length=0.0,
            evals=1,
            restarts=1,
            seed=seed,
            elites=(Elite(worse, 0.1),),
            ladder_scores=(),
            last_improve_eval=0,
        )

    monkeypatch.setattr(tp, "design_from_ir", _unrecoverable)
    monkeypatch.setattr(tp, "topo_place", _diverged)
    monkeypatch.setattr(tp, "settle_label_room", lambda td, pose: pose)
    ctx = _FakeCtx(
        store,
        params={"pcb_ref_id": ref_id, "placer": "topo", "iters": 50, "seed": 1},
    )
    _dispatch(ctx)
    assert ctx.failures and ctx.failures[0][1] == "non-convergence"
    assert "refusing to commit" in ctx.failures[0][0]
    after = {
        i["refdes"]: (i["x"], i["y"]) for i in store.pcb_graph(ref_id)["instances"]
    }
    assert after == before

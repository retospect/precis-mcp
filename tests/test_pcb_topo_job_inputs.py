"""``pcb_place``'s ``placer='topo'`` job path — what reaches the topo
anneal and what its pin assignment and route stage leave on the board:

- stored ``pcb_measures`` rows reach ``design_from_ir`` (measures are not
  a graph key, so reading them off the graph priced every one 0);
- the flex-slot (``any_of``) choices are wired into the netlist the route
  stage routes and committed with the pose as a resolved-netlist
  snapshot the check reads, released again by the next topo run;
- ``meta.topo_run.route`` counts dangling nets apart from realized ones.

Drives ``pcb_place._dispatch`` on a fake context, the
``tests/workers/test_pcb_place.py`` pattern.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import DANGLING_NET_NOTE_PREFIX, check_run, drc
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


def _dispatch(ctx: _FakeCtx) -> None:
    pcb_place._dispatch(ctx, pcb_place.SPEC)  # type: ignore[arg-type]


def _seed(store: Store, slug: str, args: dict[str, Any]) -> int:
    PcbHandler(hub=Hub(store=store)).put(id=slug, args=args)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return int(ref.id)


def _topo(ref_id: int, **kw: Any) -> dict[str, Any]:
    return {"pcb_ref_id": ref_id, "placer": "topo", "iters": 50, "seed": 1, **kw}


def _members(graph: dict[str, Any]) -> dict[str, set[tuple[str, str]]]:
    return {
        n["name"]: {(m["refdes"], m["pin"]) for m in n["members"]}
        for n in graph["nets"]
    }


def _summary(ctx: _FakeCtx) -> str:
    return next(text for kind, text in ctx.summaries if kind == "job_summary")


def _net(name: str) -> dict[str, Any]:
    return {"name": name, "current": 0.001, "class": "signal"}


_CROSSED: dict[str, Any] = {
    "components": [
        {"refdes": r, "label": "ic", "x": x, "y": y, "pins": [{"name": "1"}]}
        for r, x, y in (
            ("A", 50.0, 50.0),
            ("B", 52.0, 52.0),
            ("C", 50.0, 52.0),
            ("D", 52.0, 50.0),
        )
    ],
    "nets": [_net("N1"), _net("N2")],
    "connections": [
        {"net": "N1", "refdes": "A", "pin": "1"},
        {"net": "N1", "refdes": "B", "pin": "1"},
        {"net": "N2", "refdes": "C", "pin": "1"},
        {"net": "N2", "refdes": "D", "pin": "1"},
    ],
    "features": [closed_outline()],
}

#: A stored-shape proximity measure — the row ``resolve_measures`` reads.
_NEAR: dict[str, Any] = {
    "metric": "proximity",
    "operands": [{"instance": "A"}, {"instance": "C"}],
    "goal": 2.0,
    "strength": "soft",
    "reason": "keep A next to C",
}


# ── authored measures reach the anneal ────────────────────────────────


@pytest.mark.parametrize("with_measure", [True, False])
def test_topo_job_prices_the_stored_measures(
    store: Store, monkeypatch: pytest.MonkeyPatch, with_measure: bool
) -> None:
    """One stored ``near``/proximity row yields a ``TopoMeasure`` and a
    non-zero measures share on the objective the anneal runs (the
    summary's objective line prints it); a measureless board keeps the
    preset's 0 share."""
    from precis.pcb import topoplace as tp

    args = {**_CROSSED, **({"measures": [_NEAR]} if with_measure else {})}
    ref_id = _seed(store, f"topo-measures-{int(with_measure)}", args)
    assert len(store.pcb_measures_list(ref_id)) == int(with_measure)

    real = tp.topo_place
    seen: dict[str, Any] = {}

    def _spy(d: Any, **kw: Any) -> Any:
        seen["td"], seen["objective"] = d, kw["objective"]
        return real(d, **kw)

    monkeypatch.setattr(tp, "topo_place", _spy)
    ctx = _FakeCtx(store, params=_topo(ref_id))
    _dispatch(ctx)

    assert not ctx.failures, ctx.failures
    td, objective = seen["td"], seen["objective"]
    summary = ctx.meta["topo_run"]["files"]["summary.txt"]
    if with_measure:
        assert len(td.measures) == 1
        m = td.measures[0]
        assert (td.parts[m.ia], td.parts[m.ib]) == ("A", "C")
        assert (m.bound, m.goal_mm, m.hard) == ("upper", 2.0, False)
        assert objective.measures > 0
        assert f"/measures {objective.measures:.2f}" in summary
        assert "/measures 0.00" not in summary
    else:
        assert td.measures == ()
        assert objective.measures == 0
        assert "/measures 0.00" in summary


# ── flex-slot pins are wired into the routed netlist ───────────────────

#: U1's four GPIOs are netless in the authored netlist; two ``any_of``
#: slots claim one each. SDA already joins R1 and U3 (the taz-leaf
#: shape: routed R30.1–U3.SDA only); LED has ONE authored member, so
#: before its slot pin joins it reads as a dangling net.
_FLEX: dict[str, Any] = {
    "components": [
        {
            "refdes": "U1",
            "label": "mcu",
            "x": 60.0,
            "y": 60.0,
            "pins": [{"name": f"P{i}"} for i in range(1, 5)],
            "pin_decls": {
                "slots": [
                    {"pins": ["P1", "P2", "P3", "P4"], "name": "SDA", "net": "SDA"},
                    {"pins": ["P1", "P2", "P3", "P4"], "name": "LED", "net": "LED"},
                ]
            },
        },
        {
            "refdes": "R1",
            "label": "res",
            "x": 72.0,
            "y": 60.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
        {
            "refdes": "U3",
            "label": "ic",
            "x": 84.0,
            "y": 60.0,
            "pins": [{"name": "SDA"}],
        },
        {
            "refdes": "D1",
            "label": "led",
            "x": 60.0,
            "y": 72.0,
            "pins": [{"name": "1"}],
        },
    ],
    "nets": [_net("SDA"), _net("LED")],
    "connections": [
        {"net": "SDA", "refdes": "R1", "pin": "1"},
        {"net": "SDA", "refdes": "U3", "pin": "SDA"},
        {"net": "LED", "refdes": "D1", "pin": "1"},
    ],
    "features": [closed_outline()],
}


def test_topo_job_wires_the_flex_slot_pins_it_assigns(store: Store) -> None:
    """The routed sketch rows of each slot net carry the MCU pin the
    solver chose, the netlist the check reads has the pin on the net (the
    authored netconns untouched), and a second run on the same board
    re-solves cleanly with one MCU pin per slot net."""
    ref_id = _seed(store, "topo-flex", _FLEX)
    ctx = _FakeCtx(store, params=_topo(ref_id, route=True, iters=200))
    _dispatch(ctx)

    assert not ctx.failures, ctx.failures
    pins = ctx.meta["topo_run"]["files"]["pins.json"]["pins"]
    assert {n: pins[n]["refdes"] for n in pins} == {"SDA": "U1", "LED": "U1"}
    chosen = {n: str(pins[n]["pin"]) for n in pins}
    assert len(set(chosen.values())) == 2
    assert "flex slots: wired 2 pin(s)" in _summary(ctx)

    # The route stage routed each slot pin on its slot net: the plan's
    # sketch rows name it, and they were persisted.
    route = ctx.meta["topo_run"]["route"]
    assert route["persisted"] is True
    wires = ctx.meta["topo_run"]["files"]["wires.json"]["nets"]
    stored = store.pcb_routes_get(ref_id)
    for net in ("SDA", "LED"):
        mcu = f"U1.{chosen[net]}"
        for rows in (wires, stored):
            ends = {e[k] for e in rows[net]["tree"] for k in ("a", "b")}
            assert mcu in ends, (net, rows[net]["tree"])
    status = {r["name"]: r for r in store.pcb_route_status(ref_id)}
    assert not (status["LED"]["note"] or "").startswith(DANGLING_NET_NOTE_PREFIX)
    assert route["dangling"] == 0

    # The stored netlist as the check reads it (the effective graph);
    # the authored netconns keep the pins netless — the binding is
    # derived, tagged for the next topo run to release.
    eff = check_run.effective_graph(store, ref_id)
    nets = _members(eff)
    assert ("U1", chosen["SDA"]) in nets["SDA"]
    assert ("U1", chosen["LED"]) in nets["LED"]
    netless = {(u["refdes"], u["pin"]) for u in eff["unconnected"]}
    spares = {f"P{i}" for i in range(1, 5)} - set(chosen.values())
    assert netless >= {("U1", p) for p in spares}
    assert not netless & {("U1", p) for p in chosen.values()}
    authored = {(u["refdes"], u["pin"]) for u in store.pcb_graph(ref_id)["unconnected"]}
    assert authored >= {("U1", p) for p in chosen.values()}
    # gr520797's rule, fed what check_run feeds it: no slot pin is left
    # 'netless but assigned' (the spares still read as spares).
    findings = drc.check_unconnected_pins(
        eff["unconnected"],
        phase="routed",
        topo_assign={("U1", p): n for n, p in chosen.items()},
    )
    assert not [f for f in findings if "slots" in f.objects[0]]
    spare_rows = {f.objects[0]["refdes"]: f.objects[0]["pins"] for f in findings}
    assert spare_rows["U1"] == tuple(sorted(spares))
    snap = store.pcb_netlist_snapshot_latest(ref_id)
    assert snap is not None
    assert (snap["meta"]["source"], snap["meta"]["job"]) == ("topo_place", ctx.ref_id)
    assert {
        (e["refdes"], e["pin"], e["net"])
        for e in snap["pins"]
        if e.get("source") == "topo"
    } == {("U1", chosen["SDA"], "SDA"), ("U1", chosen["LED"], "LED")}

    # A re-run releases the last run's bindings and re-solves: no
    # PinConflict, and no slot net ends up holding two MCU pins.
    again = _FakeCtx(store, params=_topo(ref_id, route=True, iters=200, seed=2))
    _dispatch(again)
    assert not again.failures, again.failures
    after = _members(check_run.effective_graph(store, ref_id))
    rechosen = again.meta["topo_run"]["files"]["pins.json"]["pins"]
    for net in ("SDA", "LED"):
        assert {p for r, p in after[net] if r == "U1"} == {str(rechosen[net]["pin"])}


#: Two slots over shared candidates: S_B can only take P1. A previous
#: run declared S_A alone and wired P1 to net A.
_TWO_SLOTS: dict[str, Any] = {
    "components": [
        {
            "refdes": "U1",
            "label": "mcu",
            "x": 60.0,
            "y": 60.0,
            "pins": [{"name": "P1"}, {"name": "P2"}],
            "pin_decls": {
                "slots": [
                    {"pins": ["P1", "P2"], "name": "S_A", "net": "A"},
                    {"pins": ["P1"], "name": "S_B", "net": "B"},
                ]
            },
        },
        {
            "refdes": "R1",
            "label": "res",
            "x": 72.0,
            "y": 60.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
    ],
    "nets": [_net("A"), _net("B")],
    "connections": [
        {"net": "A", "refdes": "R1", "pin": "1"},
        {"net": "B", "refdes": "R1", "pin": "2"},
    ],
    "features": [closed_outline()],
}


def test_topo_rerun_releases_the_previous_slot_binding(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin the last topo run wired is a candidate again: held, it is
    'wired to another net' for every other slot and S_B has no feasible
    pin; released, the solve moves S_A to P2 and gives S_B its only pin."""
    ref_id = _seed(store, "topo-rerun", _TWO_SLOTS)
    board_id = int(store.pcb_graph(ref_id)["board"]["board_id"])
    store.pcb_netlist_snapshot_write(
        board_id,
        content_hash="previous-run",
        pins=[
            {
                "refdes": "U1",
                "pin": "P1",
                "net": "A",
                "declared": None,
                "solver_created": True,
                "source": "topo",
            }
        ],
        prior=[],
        meta={"source": "topo_place", "slots": 1},
    )
    assert ("U1", "P1") in _members(check_run.effective_graph(store, ref_id))["A"]

    # Without the release the stale binding starves S_B.
    with monkeypatch.context() as m:
        m.setattr(pcb_place, "_release_topo_slots", lambda snapshot: snapshot)
        held = _FakeCtx(store, params=_topo(ref_id))
        _dispatch(held)
    assert held.failures and "S_B" in held.failures[0][0]
    assert "no feasible pin" in held.failures[0][0]

    ctx = _FakeCtx(store, params=_topo(ref_id))
    _dispatch(ctx)
    assert not ctx.failures, ctx.failures
    pins = ctx.meta["topo_run"]["files"]["pins.json"]["pins"]
    assert pins["S_A"] == {"refdes": "U1", "pin": "P2"}
    assert pins["S_B"] == {"refdes": "U1", "pin": "P1"}
    nets = _members(check_run.effective_graph(store, ref_id))
    assert ("U1", "P2") in nets["A"] and ("U1", "P1") not in nets["A"]
    assert ("U1", "P1") in nets["B"]


# ── the route meta counts dangling nets apart ──────────────────────────

#: ``_CROSSED`` plus a one-member test-point net: stored ``realized``
#: with the dangling note, but nothing was routed for it.
_DANGLING: dict[str, Any] = {
    **_CROSSED,
    "components": [
        *_CROSSED["components"],
        {"refdes": "E", "label": "tp", "x": 60.0, "y": 60.0, "pins": [{"name": "1"}]},
    ],
    "nets": [*_CROSSED["nets"], _net("TP")],
    "connections": [
        *_CROSSED["connections"],
        {"net": "TP", "refdes": "E", "pin": "1"},
    ],
}


def test_topo_route_meta_counts_dangling_nets_apart(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``realized`` counts routed nets only; the dangling net keeps its
    stored ``realized`` row but is tallied under ``dangling`` — in the
    job meta, the board's ``last_route`` and the summary line. A route
    that writes nothing reports the kept rows the same way."""
    ref_id = _seed(store, "topo-dangling", _DANGLING)
    ctx = _FakeCtx(store, params=_topo(ref_id, route=True, iters=200))
    _dispatch(ctx)

    assert not ctx.failures, ctx.failures
    route = ctx.meta["topo_run"]["route"]
    status = {r["name"]: r for r in store.pcb_route_status(ref_id)}
    assert status["TP"]["status"] == "realized"
    assert status["TP"]["note"].startswith(DANGLING_NET_NOTE_PREFIX)
    routed = sum(1 for n in ("N1", "N2") if status[n]["status"] == "realized")
    failed = sum(1 for n in ("N1", "N2") if status[n]["status"] == "failed")
    assert (route["nets"], route["realized"], route["dangling"], route["failed"]) == (
        3,
        routed,
        1,
        failed,
    )
    assert (
        f"topo-route: 3 net(s) — {routed} realized, 1 dangling (nothing to "
        f"route), {failed} failed, "
    ) in _summary(ctx)
    ref = store.get_ref(kind="pcb", id="topo-dangling")
    assert ref is not None
    last = ref.meta["last_route"]
    assert (last["realized"], last["dangling"], last["failed"]) == (routed, 1, failed)

    # An empty route result keeps the stored rows; the meta's `stored`
    # tally buckets them by the same rule.
    from precis.pcb import realize as pcb_realize
    from precis.pcb.topoplace import realise as _tprealise

    empty = types.SimpleNamespace(
        pieces=(),
        vias=(),
        layers=(),
        crossings=(),
        unreachable=(),
        stranded=(),
        no_via=(),
        guarded=(),
        severed=(),
    )
    monkeypatch.setattr(
        _tprealise,
        "route_design",
        lambda *a, **k: (empty, pcb_realize.RealizeResult((), (), ())),
    )
    kept = _FakeCtx(store, params=_topo(ref_id, route=True, iters=200, seed=2))
    _dispatch(kept)
    assert not kept.failures, kept.failures
    meta = kept.meta["topo_run"]["route"]
    assert meta["persisted"] is False
    assert (meta["realized"], meta["dangling"], meta["failed"]) == (0, 1, 0)
    assert (meta["stored"]["realized"], meta["stored"]["dangling"]) == (routed, 1)
    assert "1 dangling (nothing to route), 0 failed, 2 sketched" in _summary(kept)

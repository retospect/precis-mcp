"""td569571 — the rule ladder: route at near-zero rules, climb to the
real per-net rules in four steps, legalise the carried embedding at
each step, re-route only the nets whose corridor cut goes infeasible.
Only the t=1 board is ever committed."""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import pytest

import precis_geom.pcb as pcb
from precis.pcb import local as kernel
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import design_from_ir, realise
from precis.pcb.topoplace.design import TopoDesign, TopoZone
from precis.pcb.topoplace.realise import _LADDER_ZV, _ladder_rules

_WIDTH = 0.15
_CLR = 0.15


def _channel_board(channel_w: float, n_nets: int = 3) -> tuple[TopoDesign, Any]:
    """``n_nets`` two-pin nets on a 4-layer board, kept apart only by a
    keepout channel ``channel_w`` mm wide at y=10: the channel is the
    cheap path, the keepout ends the long one. At 0.15/0.15 rules the
    greedy one-shot pass walls some nets; near-zero embeds them all."""
    fp = pcb.Footprint(
        "SIP2",
        [
            pcb.pad("1", "rect", -0.8, 0.0, w=0.9, h=1.0),
            pcb.pad("2", "rect", 0.8, 0.0, w=0.9, h=1.0),
        ],
    )
    b = pcb.Board("ladder-test", stackup=4, outline=pcb.rect(40.0, 24.0))
    f = b.footprint(fp)
    for i in range(n_nets):
        y = 10.0 + (i - (n_nets - 1) / 2.0) * 1.2
        pa = b.part(f"A{i}", footprint=f, pins=["1", "2"]).at(4.0, y)
        pb = b.part(f"B{i}", footprint=f, pins=["1", "2"]).at(36.0, y)
        pa["2"].etype("bidi", fmax_hz=0.0, assume="wire end")
        pb["1"].etype("bidi", fmax_hz=0.0, assume="wire end")
        b.wire(pa["2"], pb["1"], name=f"N{i}").current = 0.001
    design = kernel._design(b, None)
    ir = kernel._build_ir(design)
    fps = kernel._footprints(design, ir)
    td = design_from_ir(
        ir, fps, net_classes=design.graph.get("net_classes"), route_planes=True
    )
    half = channel_w / 2.0
    lo = np.asarray(
        [
            [12.0, 0.0],
            [28.0, 0.0],
            [28.0, 10.0 - half],
            [12.0, 10.0 - half],
            [12.0, 0.0],
        ]
    )
    hi = np.asarray(
        [
            [12.0, 10.0 + half],
            [28.0, 10.0 + half],
            [28.0, 24.0],
            [12.0, 24.0],
            [12.0, 10.0 + half],
        ]
    )
    td = dataclasses.replace(
        td,
        zones=tuple(td.zones)
        + (
            TopoZone(kind="keepout", ring=lo, name="lo"),
            TopoZone(kind="keepout", ring=hi, name="hi"),
        ),
    )
    return td, ir


def _rules(n_nets: int) -> dict[str, NetRules]:
    return {
        f"N{i}": NetRules(track_width_mm=_WIDTH, clearance_mm=_CLR)
        for i in range(n_nets)
    }


def _failed_names(plan: Any, td: TopoDesign) -> list[str]:
    names = [str(n) for n in td.net_name]
    return sorted(names[k] for k in realise._route_failed_nets(plan) if k < len(names))


def test_ladder_rules_endpoints_and_monotone() -> None:
    """The helper returns near-zero at t=0, the real rule at t=1, and a
    monotone interpolation between — per net and for the fab floor."""
    real = {
        "NA": NetRules(0.2, 0.25, via_dia_mm=0.6, via_drill_mm=0.3),
        "NB": NetRules(0.1, 0.1),
    }
    r0, f0 = _ladder_rules(real, 0.45, 0.25, 0.05, 0.0)
    assert r0["NA"].track_width_mm == _LADDER_ZV
    assert r0["NA"].clearance_mm == 0.0
    assert r0["NA"].via_dia_mm == _LADDER_ZV
    assert r0["NA"].via_drill_mm == _LADDER_ZV / 2.0
    assert f0 == (_LADDER_ZV, _LADDER_ZV / 2.0, 0.0)
    r1, f1 = _ladder_rules(real, 0.45, 0.25, 0.05, 1.0)
    assert r1["NA"] == real["NA"]
    assert r1["NB"].track_width_mm == 0.1
    assert f1 == (0.45, 0.25, 0.05)
    prev: tuple[float, ...] | None = None
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        rt, ft = _ladder_rules(real, 0.45, 0.25, 0.05, t)
        cur = (
            rt["NA"].track_width_mm,
            rt["NA"].clearance_mm,
            float(rt["NA"].via_dia_mm or 0.0),
            float(rt["NA"].via_drill_mm or 0.0),
            float(ft[0] or 0.0),
            float(ft[1] or 0.0),
            float(ft[2] or 0.0),
        )
        if prev is not None:
            assert all(c >= p - 1e-12 for c, p in zip(cur, prev))
        prev = cur
    # a net absent from the map stays absent — the default-rule path
    # resolves it identically to the real-rule path at every step
    absent, _ = _ladder_rules({}, 0.45, 0.25, 0.05, 0.5)
    assert absent == {}


def test_ladder_routes_what_one_shot_withdraws() -> None:
    """A channel that jams the greedy real-rule pass: the one-shot
    ``_route_pass`` withdraws nets, the ladder routes every net."""
    td, ir = _channel_board(0.5, 3)
    rules = _rules(3)
    p1, _r1 = realise._route_pass(td, ir, net_rules=rules)
    assert _failed_names(p1, td) == ["N0", "N1"]
    plan, rres = realise._route_signals(td, ir, net_rules=rules)
    assert _failed_names(plan, td) == []
    assert list(plan.withdrawn) == []
    assert {str(ir.net_name[t.net_id]) for t in rres.tracks} == {"N0", "N1", "N2"}


def test_scoped_reroute_touches_only_infeasible_nets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_route_head`` runs once over every routable net (the near-zero
    pass); every later call is a scoped re-route and may only ever see
    nets a cut or the LP named — a net that stays feasible is never
    re-searched, and its committed copper is bitwise the last kept
    emission's."""
    td, ir = _channel_board(0.5, 3)
    rules = _rules(3)
    heads: list[frozenset[int]] = []
    real_head = realise._route_head

    def spy_head(d, ir_, **kw):
        heads.append(frozenset(k for k, pins in enumerate(d.net_pins) if len(pins)))
        return real_head(d, ir_, **kw)

    monkeypatch.setattr(realise, "_route_head", spy_head)
    emits: list[tuple[Any, Any]] = []
    real_emit = realise._route_emit

    def spy_emit(work, ir_, **kw):
        out = real_emit(work, ir_, **kw)
        emits.append(out)
        return out

    monkeypatch.setattr(realise, "_route_emit", spy_emit)
    plan, rres = realise._route_signals(td, ir, net_rules=rules)
    assert _failed_names(plan, td) == []
    assert heads[0] == frozenset({0, 1, 2})
    # the scoped calls each carried a strict subset of the routed nets —
    # never the whole board, and never a feasible net for company
    assert heads[1:], "expected at least one scoped re-route"
    for scoped in heads[1:]:
        assert scoped < heads[0]
    # a net the steps never re-routed commits exactly the copper its one
    # last kept emission drew — nothing a scoped pass did touched it
    last_tracks: dict[int, tuple] = {}
    for _p, r in emits:
        per_net: dict[int, list] = {}
        for t in r.tracks:
            per_net.setdefault(int(t.net_id), []).append(t)
        for net_id, tracks in per_net.items():
            last_tracks[net_id] = tuple(tracks)
    final: dict[int, list] = {}
    for t in rres.tracks:
        final.setdefault(int(t.net_id), []).append(t)
    assert {net_id: tuple(tracks) for net_id, tracks in final.items()} == last_tracks


def test_intermediates_never_commit() -> None:
    """Every committed track/via is at the real rule — no step-rule or
    near-zero copper leaks out of the ladder."""
    td, ir = _channel_board(0.5, 3)
    plan, rres = realise._route_signals(td, ir, net_rules=_rules(3))
    assert not _failed_names(plan, td)
    assert rres.tracks
    assert all(t.width_mm == pytest.approx(_WIDTH) for t in rres.tracks)
    for v in rres.vias:
        assert v.dia_mm > _LADDER_ZV * 10.0
        assert v.drill_mm > _LADDER_ZV * 10.0


def test_ladder_deterministic() -> None:
    """Same board, same rules — identical plan and copper, twice."""
    td, ir = _channel_board(0.5, 3)
    rules = _rules(3)
    pa, ra = realise._route_signals(td, ir, net_rules=rules)
    pb, rb = realise._route_signals(td, ir, net_rules=rules)
    assert pa == pb
    assert ra == rb

"""G6 (topo-route-geometry-truth): the router's invariant, as a test.

At near-zero rules (1 um track and via, clearance 0) every wire is as thin
as a wire can be, so nothing the rules ask for can bar a route: the board's
own obstacles are the only thing left. The invariant is that on every board,
in every net order, every signal net routes clean (no unreached component,
no unresolved or live same-layer crossing, no hop without a via site, no
foreign pad hit, and no net the guards withdraw as ``guarded`` or
``severed``) and no plane pad lacks a fanout-via site. One pass of
``realise._route_pass`` through its guards, the search fitting the net's own
via (the pass ``scratch/review/zr_gauge.py`` measures as "strict").

What stays in force at near-zero: the label keepouts (the boards that carry
label spots), every other keepout and mounting hole, the outline margin and
the fab's edge band (``plan_route`` keeps the edge clearance whatever the
net rule says), and the board's pads. Only track width, via size and
clearance are driven to nothing.

Boards: ``sanity`` is built in-test from ``examples/boards/sanity``; the
three taz-leaf poses (1x ``local-66``, ``local-61``; 2x ``x2-local-2``) are
scratch exports read from ``scratch/taz-leaf`` (gitignored, a few hundred
KB) and skip, naming the board, where that directory is absent. The five
net orders are production plus reversed and three seeded shuffles, applied
by wrapping ``wires._net_order`` as ``scratch/review/g2e_cfg.py`` does.

Each (board, order) the near-zero invariant does not yet hold on is a strict xfail
listing the nets that fail, so a slice that fixes it turns the row red until
the marker is dropped. Real-rule rows assert every counting local DRC
finding on a clean net — none is tolerated.
"""

from __future__ import annotations

import collections
import dataclasses
import functools
import importlib.util
import json
import random
import types
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from shapely.geometry import LineString

from precis.pcb import drc as pcb_drc
from precis.pcb import local as pcb_local
from precis.pcb import realize as pcb_realize
from precis.pcb import session as pcb_session
from precis.pcb import topoplace as topo
from precis.pcb.optimize import hole_fab_clearance_mm
from precis.pcb.realize import RealizeResult
from precis.pcb.topoplace import crossings, realise
from precis.pcb.topoplace import mesh as mesh_mod
from precis.pcb.topoplace import wires as wires_mod

ROOT = Path(__file__).resolve().parents[1]
TAZ = ROOT / "scratch" / "taz-leaf"
ZV = 0.001  # 1 um

#: board -> (batch dir, export dir) under scratch/taz-leaf; ``None`` = sanity
BOARDS: dict[str, tuple[str, str] | None] = {
    "sanity": None,
    "local-66": ("out", "local-66_export"),
    "local-61": ("out", "local-61_export"),
    "x2": ("out-x2", "x2-local-2_export"),
}
ORDERS = ("prod", "rev", "shuf1", "shuf2", "shuf3")

#: GND/plane pads (pin index) with no fanout-via site, per board. G2d
#: round 3: missing is the POST-emit count — a deferred escape that
#: finds no legal corridor inside ``_ESCAPE_LEN_CAP_MM`` off routed
#: copper is the finding too (the earlier rounds emitted 28-77 mm
#: stubs that crossed routed tracks). Measured per order; the entry
#: lists the union (a row xfails when any finding stands). local-66's
#: three sit in the unscoped hanger-ring keepout — all layers barred.
_GND_MISSING = {
    "sanity": (),
    "local-66": (14, 16, 34, 37),
    "local-61": (37,),
    "x2": (18, 34, 37),
}

#: (board, order) -> (failing signal nets, why), measured by the G6 gauge
#: (scratch/review/g6_table.py, strict definition) when this test was added.
#: "guarded" is the anti-short guard's withdrawal, named by the check that
#: fired (``track:X`` = a piece of net X closer than the widths plus
#: clearance, here a few um); "severed" is the emit-connectivity check; the
#: rest are no-via branches and unresolved crossings. A row that starts
#: passing must lose its entry (and a board's ``_GND_MISSING`` its pads).
_FAILING: dict[tuple[str, str], tuple[list[str], str]] = {
    # td569540 re-measure: negotiated congestion left the same three nets
    # unreachable on every order, but the failure is honest now —
    # stranded comps the search cannot link, no guarded/severed
    # withdrawals and no live crossings (the pre-negotiation rows also
    # severed VBAT_OV/VSTOR downstream).
    ("local-66", "prod"): (
        ["DEC1", "VBAT_OV", "VSTOR"],
        "unreachable+stranded ['DEC1', 'VBAT_OV', 'VSTOR']",
    ),
    ("local-66", "rev"): (
        ["DEC1", "VBAT_OV", "VSTOR"],
        "unreachable+stranded ['DEC1', 'VBAT_OV', 'VSTOR']",
    ),
    ("local-66", "shuf1"): (
        ["DEC1", "VBAT_OV", "VSTOR"],
        "unreachable+stranded ['DEC1', 'VBAT_OV', 'VSTOR']",
    ),
    ("local-66", "shuf2"): (
        ["DEC1", "VBAT_OV", "VSTOR"],
        "unreachable+stranded ['DEC1', 'VBAT_OV', 'VSTOR']",
    ),
    ("local-66", "shuf3"): (
        ["DEC1", "VBAT_OV", "VSTOR"],
        "unreachable+stranded ['DEC1', 'VBAT_OV', 'VSTOR']",
    ),
    # Base-state failures the board's gnd-missing xfail masked until the
    # G2d escapes cleared it (same nets fail on the pre-G2d code).
    # td569540 re-measure: local-61/shuf1's VSTOR negotiated back to
    # clean (the row still xfails on GND[37]); shuf3's cluster failure
    # moved — OK_HYST routes now, VSTOR is the stranded one.
    # r2 re-measure (touched-set + stall escape): VSTOR did not heal on
    # shuf3, and the negotiation's redistributed equilibria strand
    # VBAT_UV on shuf1 and RF2+VSTOR on shuf2 — honest unreachable
    # nets, no crossings or withdrawals.
    ("local-61", "shuf1"): (
        ["VBAT_UV"],
        "unreachable+stranded+no-via branch ['VBAT_UV']",
    ),
    ("local-61", "shuf2"): (
        ["RF2", "VSTOR"],
        "unreachable+stranded ['RF2', 'VSTOR']",
    ),
    ("local-61", "shuf3"): (
        ["VSTOR"],
        "unreachable+stranded+no-via branch ['VSTOR']",
    ),
    ("x2", "rev"): (
        ["VBAT_UV"],
        "unreachable+stranded+no-via branches ['VBAT_UV']",
    ),
    ("x2", "shuf1"): (
        ["VBAT_UV"],
        "unreachable+stranded+no-via branch ['VBAT_UV'] — OK_HYST routes "
        "under negotiation now; VBAT_UV's remaining refusal is honest",
    ),
}


def _xfail_reason(board: str, order: str) -> str | None:
    nets, why = _FAILING.get((board, order), ([], ""))
    gnd = _GND_MISSING[board]
    if not nets and not gnd:
        return None
    return f"nets not clean: {nets} ({why}); GND/plane pads without a via site: {list(gnd)}"


def _load_taz(batch_dir: str, export: str) -> tuple[Any, Any, Any, Any]:
    exp = TAZ / "out" / export
    poses = json.loads((exp / "poses.json").read_text(encoding="utf-8"))
    spots = json.loads((exp / "label_spots.json").read_text(encoding="utf-8"))
    batch = json.loads((TAZ / batch_dir / "batch.min.json").read_text(encoding="utf-8"))
    for c in batch["components"]:
        p = poses.get(c["refdes"])
        if p:
            c["x"], c["y"], c["rot"], c["layer"] = p["x"], p["y"], p["rot"], p["layer"]
        if c["refdes"] in {"SNS1", "SNS2"}:
            c["polarized"] = False
    t = (TAZ / "out" / "fp_rows.txt").read_text(encoding="utf-8")
    catalog = {r["lcsc"]: r for r in json.loads(t[t.index("[") : t.rindex("]") + 1])}
    design = pcb_local._design(batch, catalog)
    ir, fps, _m = pcb_local._prepare(design)
    return design, ir, fps, spots


def _load_sanity() -> tuple[Any, Any, Any, Any]:
    path = ROOT / "examples" / "boards" / "sanity" / "main.py"
    spec = importlib.util.spec_from_file_location("example_sanity_invariant", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    design = pcb_local._design(mod.build(), None)
    ir, fps, _m = pcb_local._prepare(design)
    return design, ir, fps, None


@functools.cache
def _board(name: str) -> tuple[Any, Any, Any, Any]:
    spec = BOARDS[name]
    return _load_sanity() if spec is None else _load_taz(*spec)


def _set_order(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    if mode == "prod":
        return
    base_order = wires_mod._net_order

    def order(mesh: Any, d: Any, n_layers: int) -> list[int]:
        base = base_order(mesh, d, n_layers)
        if mode == "rev":
            return base[::-1]
        out = list(base)
        random.Random(int(mode[4:])).shuffle(out)
        return out

    monkeypatch.setattr(wires_mod, "_net_order", order)


def _near_zero_pass(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], list[str]]:
    """One route pass at near-zero rules; ``(failing signal nets, plane
    pads without a fanout site)``, by name."""
    design, ir, fps, spots = _board(name)
    real = pcb_session.route_net_rules(design.graph, design.fab_caps)
    net_rules = {
        k: dataclasses.replace(
            v, track_width_mm=ZV, clearance_mm=0.0, via_dia_mm=ZV, via_drill_mm=ZV / 2
        )
        for k, v in real.items()
    }
    td = topo.design_from_ir(
        ir,
        fps,
        net_classes=design.graph.get("net_classes"),
        features=design.features,
        fixed_copper=design.fixed_copper,
        route_planes=True,
        capability=design.fab_caps,
        hole_margin_mm=0.0,
    )
    if spots is not None:
        boxes = topo.label_boxes_for(td, td.pose0, spots)
        td = dataclasses.replace(
            td,
            zones=tuple(td.zones)
            + tuple(
                topo.TopoZone(kind="label_keepout", ring=ring, name=f"label:{r}")
                for r, ring in boxes
            ),
        )
    pose = np.asarray(td.pose0, dtype=float)
    planes = realise._plane_nets(td, ir)
    d = td
    missing: list[tuple[int, int]] = []
    deferred: list[tuple[int, int]] = []
    f_tracks: list[Any] = []
    f_vias: list[Any] = []
    if planes:
        from precis.pcb.realize import RealizeResult

        f_tracks, f_vias, _fell_back, deferred = realise._fanout_vias(
            td,
            ir,
            int(ir.n_layers),
            pose,
            net_rules,
            planes,
            min_via_dia=ZV,
            min_via_drill=ZV / 2,
        )
        fan = RealizeResult(tuple(f_tracks), tuple(f_vias), ())
        d = dataclasses.replace(
            td,
            net_pins=tuple(
                np.zeros(0, dtype=np.int64) if k in planes else pins
                for k, pins in enumerate(td.net_pins)
            ),
            zones=tuple(td.zones) + tuple(realise._copper_zones(fan, td, ir, pose)),
            plane_layers=tuple(sorted(set(planes.values()))),
        )
    names = [str(n) for n in d.net_name]
    # the mesh _route_pass builds is the one the crossing check reads
    meshes: list[Any] = []
    real_build = mesh_mod.build_mesh

    def build_and_keep(*args: Any, **kw: Any) -> Any:
        meshes.append(real_build(*args, **kw))
        return meshes[-1]

    monkeypatch.setattr(mesh_mod, "build_mesh", build_and_keep)
    plan, rres = realise._route_pass(
        d, ir, pose=pose, net_rules=net_rules, min_via_dia=ZV, min_via_drill=ZV / 2
    )
    if deferred:
        # gnd_missing is the post-emit count (G2d round 3): a deferred
        # escape that fails against routed copper counts as missing —
        # and since round 6 one whose via only anchors an isolated pour
        # sliver (the join check is route_design's pre-escape pour).
        frules, _ = realise._via_choice_rules(ir, net_rules, ZV, ZV / 2, None)
        _et, _ev, e_missing, _ed, _en, _er, _es = realise._emit_escapes(
            d,
            ir,
            deferred,
            pose,
            frules,
            RealizeResult(
                rres.tracks + tuple(f_tracks),
                rres.vias + tuple(f_vias),
                rres.warnings,
                pours=rres.pours,
            ),
            int(ir.n_layers),
            min_via_dia=ZV,
            min_via_drill=ZV / 2,
            mesh=meshes[-1],
            joined_pours=realise._joined_plane_pieces(
                td,
                ir,
                planes,
                list(rres.tracks) + f_tracks,
                list(rres.vias) + f_vias,
                pose,
                net_rules,
                fps,
                design.fixed_copper,
                0.0,
            ),
            fixed_copper=design.fixed_copper,
        )
        missing = missing + e_missing
    n_layers = int(ir.n_layers)
    # what the plan itself fails (unreachable, stranded, unresolved crossing,
    # no via site, guarded, severed) plus what its detectors leave: a live
    # same-layer crossing of the emitted pieces and a foreign pad hit
    net_of = {p.branch: p.net for p in plan.pieces}
    bad = _live_crossing_branches(meshes[-1], d, n_layers, plan.pieces)
    bad |= _pad_hit_branches(d, ir, n_layers, pose, names, plan.pieces)
    failed_nets = realise._plan_failed_nets(plan) | {net_of[b] for b in bad}
    failed = sorted(names[k] for k in failed_nets if k < len(names))
    gnd = sorted(f"{names[k]}[{p}]" for k, p in missing)
    return failed, gnd


#: topo-route-shove S1: the copper-geometry rules the gauge's
#: ``drc_local`` column counts — the set a slice may not buy clean nets
#: against (scratch/review/zr_gauge.py keeps the same list). ``clearance``
#: is counted at BOTH severities: a gap between the fab floor and the
#: net's own rule is a warn, and the gauge's ``drc_rule`` column counts it
#: — the test does too.
_DRC_LOCAL_RULES = frozenset(
    {
        "clearance",
        "via_mask_web",
        "via_pad_keepout",
        "via_via_keepout",
        "board_edge_clearance",
        "keepout_copper",
    }
)


def _drc_finding_counts(f: Any) -> bool:
    """The findings a slice may not buy clean nets against: every
    ``_DRC_LOCAL_RULES`` error, plus ``clearance`` warns on router copper
    (the band between the fab floor and the net's own clearance — S1
    review-1: an error-only filter misses it)."""
    if f.rule not in _DRC_LOCAL_RULES:
        return False
    if f.severity == "error":
        return True
    return (
        f.rule == "clearance"
        and f.severity == "warn"
        and bool(pcb_session.router_nets_of(f))
    )


def _real_rules_drc_violations(
    name: str, order: str, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """One route pass at the board's resolved (real) net rules — the
    strict pass ``scratch/review/zr_gauge.py`` measures — then the
    unexpected local error and clearance-warn findings over its
    committed copper plus the fanout stubs whose router copper belongs
    to a CLEAN net. A finding on a failed net is that failure's own
    evidence and does not count; nothing is tolerated, and a routing
    exception propagates."""
    design, ir, fps, spots = _board(name)
    net_rules = pcb_session.route_net_rules(design.graph, design.fab_caps)
    td = topo.design_from_ir(
        ir,
        fps,
        net_classes=design.graph.get("net_classes"),
        features=design.features,
        fixed_copper=design.fixed_copper,
        route_planes=True,
        capability=design.fab_caps,
        hole_margin_mm=hole_fab_clearance_mm(design.fab_caps),
    )
    if spots is not None:
        boxes = topo.label_boxes_for(td, td.pose0, spots)
        td = dataclasses.replace(
            td,
            zones=tuple(td.zones)
            + tuple(
                topo.TopoZone(kind="label_keepout", ring=ring, name=f"label:{r}")
                for r, ring in boxes
            ),
        )
    pose = np.asarray(td.pose0, dtype=float)
    min_via = design.fab_caps.jlc_min.get("via_diameter_mm")
    min_drill = design.fab_caps.jlc_min.get("drill_mm")
    min_ring = design.fab_caps.jlc_min.get("annular_ring_mm")
    planes = realise._plane_nets(td, ir)
    d = td
    f_tracks: list[Any] = []
    f_vias: list[Any] = []
    deferred: list[Any] = []
    if planes:
        f_tracks, f_vias, _fell_back, deferred = realise._fanout_vias(
            td,
            ir,
            int(ir.n_layers),
            pose,
            net_rules,
            planes,
            min_via_dia=min_via,
            min_via_drill=min_drill,
        )
        fan = RealizeResult(tuple(f_tracks), tuple(f_vias), ())
        d = dataclasses.replace(
            td,
            net_pins=tuple(
                np.zeros(0, dtype=np.int64) if k in planes else pins
                for k, pins in enumerate(td.net_pins)
            ),
            zones=tuple(td.zones) + tuple(realise._copper_zones(fan, td, ir, pose)),
            plane_layers=tuple(sorted(set(planes.values()))),
        )
    # the strict row's rules: the route-stage via choice resolves each
    # net's barrel before _route_pass re-resolves it (zr_lib.build).
    net_rules, _cur = realise._via_choice_rules(
        ir, net_rules, min_via, min_drill, min_ring
    )
    names = [str(n) for n in d.net_name]
    meshes: list[Any] = []
    real_build = mesh_mod.build_mesh

    def build_and_keep(*args: Any, **kw: Any) -> Any:
        meshes.append(real_build(*args, **kw))
        return meshes[-1]

    monkeypatch.setattr(mesh_mod, "build_mesh", build_and_keep)
    plan, rres = realise._route_pass(
        d,
        ir,
        pose=pose,
        net_rules=net_rules,
        min_via_dia=min_via,
        min_via_drill=min_drill,
    )
    n_layers = int(ir.n_layers)
    e_tracks: list[Any] = []
    e_vias: list[Any] = []
    if deferred:
        # the escape copper route_design appends after the pass (G2d),
        # emitted as route_design emits it: the pass's own mesh, the
        # pre-escape pour's wired pieces as the site gate (round 6).
        e_tracks, e_vias, _em, _ed, _en, _er, _es = realise._emit_escapes(
            d,
            ir,
            deferred,
            pose,
            net_rules,
            RealizeResult(
                rres.tracks + tuple(f_tracks),
                rres.vias + tuple(f_vias),
                rres.warnings,
                pours=rres.pours,
            ),
            n_layers,
            min_via_dia=min_via,
            min_via_drill=min_drill,
            mesh=meshes[-1],
            joined_pours=realise._joined_plane_pieces(
                td,
                ir,
                planes,
                list(rres.tracks) + f_tracks,
                list(rres.vias) + f_vias,
                pose,
                net_rules,
                fps,
                design.fixed_copper,
                hole_fab_clearance_mm(design.fab_caps),
            ),
            fixed_copper=design.fixed_copper,
        )
    net_of = {p.branch: p.net for p in plan.pieces}
    bad = _live_crossing_branches(meshes[-1], d, n_layers, plan.pieces)
    bad |= _pad_hit_branches(d, ir, n_layers, pose, names, plan.pieces)
    failed_nets = realise._plan_failed_nets(plan) | {net_of[b] for b in bad}
    failed = {names[k] for k in failed_nets if k < len(names)}
    routed = {names[k] for k in net_of.values() if k < len(names)} | {
        names[u] for u in plan.unreachable if u < len(names)
    }
    clean = routed - failed

    # the strict pass's committed copper plus the fanout stubs and the
    # escape tracks/vias — the same set ``zr_gauge.strict_copper``
    # measures (its G2d seam, filled).
    combined = RealizeResult(
        rres.tracks + tuple(f_tracks) + tuple(e_tracks),
        rres.vias + tuple(f_vias) + tuple(e_vias),
        rres.warnings,
        pours=rres.pours,
    )
    model = pcb_session._copper_model(
        ir,
        pcb_realize.result_copper_rows(combined, ir, design.layer_names, derived=True)
        + [{**row, "net": row.get("net") or ""} for row in design.fixed_copper],
        footprints=fps,
    )
    # plane nets carry no routing pins, so they never meet ``clean`` —
    # but an escape's finding is router copper all the same: a GND-GND
    # via_via_keepout or a stub's board-edge violation is a defect this
    # gate must count (G2d round 8 — the ``& clean`` filter dropped it).
    plane_names = {names[k] for k in planes}
    findings = pcb_drc.run_geometric_drc(
        model,
        capability=design.fab_caps,
        outline=[list(p) for p in (design.outline or [])],
        keepouts=pcb_session.keepouts_from_features(design.features),
        net_rules=net_rules,
    )
    return _unexpected_drc_violations(findings, clean, plane_names)


def _unexpected_drc_violations(
    findings: Iterable[pcb_drc.DrcFinding],
    clean: set[str],
    plane_names: Iterable[str] = (),
) -> list[str]:
    out = []
    plane_set = set(plane_names)
    for f in findings:
        if not _drc_finding_counts(f):
            continue
        implicated = pcb_session.router_nets_of(f)
        hit = sorted(implicated & clean)
        if not hit and implicated and implicated <= plane_set:
            hit = sorted(implicated)
        if hit:
            out.append(f"{f.rule}/{f.severity} on {hit}: {f.where} {f.detail}")
    return out


def _by_layer(pieces: Any) -> dict[int, list[tuple[Any, list[tuple[float, float]]]]]:
    by: dict[int, list[tuple[Any, list[tuple[float, float]]]]] = (
        collections.defaultdict(list)
    )
    for pc in pieces:
        pts = [(float(x), float(y)) for x, y in pc.points]
        pts = [q for i, q in enumerate(pts) if i == 0 or q != pts[i - 1]]
        if len(pts) >= 2:
            by[pc.layer].append((pc, pts))
    return by


def _live_crossing_branches(mesh: Any, d: Any, n_layers: int, pieces: Any) -> set[int]:
    """Branches of different nets whose emitted pieces cross on one layer
    (``crossing_events`` semantics; a touch is none, an empty bigon that
    ``resolve_crossings`` cancels is none)."""
    out: set[int] = set()
    obst = realise._obstacle_xy(mesh, d, n_layers)
    for items in _by_layer(pieces).values():
        fake: Any = [
            types.SimpleNamespace(points=pts, edges=(), t=(), net=pc.net)
            for pc, pts in items
        ]
        gone: dict[tuple[int, int], list[Any]] = collections.defaultdict(list)
        for a, b, x, y in crossings.resolve_crossings(mesh, fake, obst)[1]:
            gone[(a, b)] += [x, y]
        for (a, b), evs in crossings.crossing_events(mesh, fake).items():
            if items[a][0].net != items[b][0].net and any(
                e not in gone[(a, b)] for e in evs
            ):
                out |= {items[a][0].branch, items[b][0].branch}
    return out


def _pad_hit_branches(
    d: Any, ir: Any, n_layers: int, pose: Any, names: list[str], pieces: Any
) -> set[int]:
    """Branches whose emitted wire runs through another net's pad."""
    out: set[int] = set()
    pads = realise._pad_polys(d, ir, n_layers, pose)
    for lay, items in _by_layer(pieces).items():
        for pname, poly, _pin in pads.get(lay, []):
            core = poly.buffer(-1e-6)
            if core.is_empty:
                continue
            for pc, pts in items:
                if pname != names[pc.net] and LineString(pts).intersects(core):
                    out.add(pc.branch)
    return out


def _params() -> list[Any]:
    out = []
    for board in BOARDS:
        for order in ORDERS:
            marks: list[Any] = []
            reason = _xfail_reason(board, order)
            if reason is not None:
                marks.append(pytest.mark.xfail(strict=True, reason=reason))
            out.append(pytest.param(board, order, id=f"{board}-{order}", marks=marks))
    return out


@pytest.mark.parametrize(("board", "order"), _params())
def test_near_zero_rules_route_every_net_and_fan_out_every_plane_pad(
    board: str, order: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = BOARDS[board]
    if spec is not None and not (TAZ / "out" / spec[1]).is_dir():
        pytest.skip(
            f"board {board}: scratch export {spec[1]} absent (scratch/ is gitignored)"
        )
    _set_order(monkeypatch, order)
    failed, gnd = _near_zero_pass(board, monkeypatch)
    assert not failed, f"{board}/{order}: nets not clean: {failed}"
    assert not gnd, f"{board}/{order}: plane pads without a via site: {gnd}"


#: No warn-tier ``clearance`` finding is tolerated on a clean net. s1fix's
#: ``_DRC_FANOUT_WARNINGS`` allowlist (per-(board, order) pair counts for
#: a routed track vs a GND fanout via) emptied under topo-fanclear — the
#: whole class traced to an INSCRIBED ``_copper_zones`` ring (now
#: ``outward_polygon``, circumscribed) plus an emit guard that ignored
#: the track half-width. An empty allowlist leaves the mechanism dead
#: code, so it is gone: ``_unexpected_drc_violations`` fails every
#: counting finding outright.


@pytest.mark.parametrize(
    ("board", "order"),
    [
        pytest.param(
            board,
            order,
            id=f"{board}-{order}",
        )
        for board in BOARDS
        for order in ORDERS
    ],
)
def test_real_rules_clean_nets_are_drc_clean(
    board: str, order: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 (topo-route-shove): the strict pass's committed copper must be
    DRC-clean on every net it calls clean — the ``drc_local`` column of
    ``scratch/review/zr_gauge.py`` restricted to clean-net router copper
    is 0 on every real-rule row, so a slice cannot trade clean nets for
    clearance findings."""
    spec = BOARDS[board]
    if spec is not None and not (TAZ / "out" / spec[1]).is_dir():
        pytest.skip(
            f"board {board}: scratch export {spec[1]} absent (scratch/ is gitignored)"
        )
    _set_order(monkeypatch, order)
    violations = _real_rules_drc_violations(board, order, monkeypatch)
    assert not violations, (
        f"{board}/{order}: unexpected DRC on clean nets: {violations}"
    )


def _fanout_warning() -> pcb_drc.DrcFinding:
    return pcb_drc.DrcFinding(
        rule="clearance",
        severity="warn",
        where="track[VRDIV] <-> via[GND] on F.Cu",
        detail="injected clearance finding",
        objects=(
            {"ctype": "track", "net": "VRDIV", "layer": "F.Cu", "derived": True},
            {"ctype": "via", "net": "GND", "layer": "*.Cu", "derived": True},
        ),
    )


def test_the_formerly_allowlisted_fanout_warning_now_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The exact pair s1fix's allowlist tolerated — a derived track on
    # VRDIV vs a derived GND fanout via at warn tier — fails hard now
    # that the class is fixed at the cause, whichever order the finding
    # lists its objects.
    finding = _fanout_warning()
    for objects in (finding.objects, finding.objects[::-1]):
        assert _unexpected_drc_violations(
            [dataclasses.replace(finding, objects=objects)],
            {"VRDIV"},
        )

    def injected(_board: str, _order: str, _patch: pytest.MonkeyPatch) -> list[str]:
        return _unexpected_drc_violations([finding], {"VRDIV"})

    monkeypatch.setitem(BOARDS, "local-66", None)
    monkeypatch.setitem(globals(), "_real_rules_drc_violations", injected)
    with pytest.raises(AssertionError, match="clearance/warn"):
        test_real_rules_clean_nets_are_drc_clean("local-66", "prod", monkeypatch)


def test_real_rules_invariant_has_no_whole_row_xfails() -> None:
    # Pytest applies xfail outside the callable: invoking the assertion
    # below alone cannot prove that an unexpected finding fails the row.
    marks = getattr(test_real_rules_clean_nets_are_drc_clean, "pytestmark", [])
    row_marks = [
        row_mark
        for mark in marks
        if mark.name == "parametrize"
        for row in mark.args[1]
        for row_mark in getattr(row, "marks", ())
    ]
    assert not any(mark.name == "xfail" for mark in [*marks, *row_marks])


@pytest.mark.parametrize(
    ("rule", "severity", "track_changes", "via_changes"),
    [
        pytest.param(
            "clearance", "error", {}, {"net": "OTHER_SIGNAL"}, id="signal-error"
        ),
        pytest.param(
            "clearance", "warn", {}, {"net": "OTHER_SIGNAL"}, id="signal-warn"
        ),
        pytest.param("clearance", "error", {}, {}, id="fanout-error"),
        pytest.param("clearance", "warn", {}, {}, id="fanout-warn"),
        pytest.param("keepout_copper", "error", {}, {}, id="keepout-error"),
        pytest.param("via_mask_web", "error", {}, {}, id="via-mask-web-error"),
        pytest.param("via_pad_keepout", "error", {}, {}, id="via-pad-keepout-error"),
        pytest.param("via_via_keepout", "error", {}, {}, id="via-via-keepout-error"),
        pytest.param("board_edge_clearance", "error", {}, {}, id="board-edge-error"),
        pytest.param(
            "clearance", "warn", {"net": "OTHER_SIGNAL"}, {}, id="unknown-pair"
        ),
        pytest.param("clearance", "warn", {"layer": "In2.Cu"}, {}, id="unknown-layer"),
        pytest.param("clearance", "warn", {}, {"ctype": "track"}, id="ground-track"),
        pytest.param("clearance", "warn", {"ctype": "pad"}, {}, id="signal-pad"),
        pytest.param("clearance", "warn", {}, {"derived": False}, id="authored-via"),
    ],
)
def test_real_rules_invariant_rejects_injected_finding(
    monkeypatch: pytest.MonkeyPatch,
    rule: str,
    severity: str,
    track_changes: dict[str, Any],
    via_changes: dict[str, Any],
) -> None:
    sample = _fanout_warning()
    unexpected = dataclasses.replace(
        sample,
        rule=rule,
        severity=severity,
        objects=(
            {**sample.objects[0], **track_changes},
            {**sample.objects[1], **via_changes},
        ),
    )

    def injected(name: str, order: str, _patch: pytest.MonkeyPatch) -> list[str]:
        return _unexpected_drc_violations([unexpected], {"VRDIV", "OTHER_SIGNAL"})

    # Exercise the real invariant assertion on a formerly xfailed row,
    # without depending on the optional scratch board data.
    monkeypatch.setitem(BOARDS, "local-66", None)
    monkeypatch.setitem(globals(), "_real_rules_drc_violations", injected)
    with pytest.raises(AssertionError, match=f"{rule}/{severity}"):
        test_real_rules_clean_nets_are_drc_clean("local-66", "prod", monkeypatch)


def test_real_rules_invariant_propagates_routing_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_route(*_args: Any) -> list[str]:
        raise RuntimeError("injected routing exception")

    monkeypatch.setitem(BOARDS, "local-66", None)
    monkeypatch.setitem(globals(), "_real_rules_drc_violations", broken_route)
    with pytest.raises(RuntimeError, match="injected routing exception"):
        test_real_rules_clean_nets_are_drc_clean("local-66", "prod", monkeypatch)

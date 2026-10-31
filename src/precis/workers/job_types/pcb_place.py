"""``pcb_place`` job_type — the enqueued, out-of-line half of ``op='place'``
(docs/backlog/pcb-guided-place-route.md, "Tool surface" + Slice 10).

Heavy compute (the annealer, measured at ~880 moves/s on a synthetic 30-
component board — minutes, not milliseconds) must never run inline in an
MCP tool call, the same thread-pool-starvation lesson
``docs/backlog/precis-mcp-fleet-concurrency-limit`` (memory) already
learned the hard way. ``PcbHandler.put(args={'op': 'place', ...})`` only
ever mints one of these; this module is where ``optimize.py`` actually
runs.

Placement-only: restricts the move schedule to
``TRANSLATE``/``ROTATE``/``SWAP`` for the WHOLE run (no topology/layer/
plane/pin-swap moves) — those belong to routing (``_topo_route`` in this
module), which also re-places jointly with the sketch. Positions land via
:meth:`~precis.store._pcb_ops.PcbMixin.pcb_set_pose`, which never
overwrites a ``fixed`` instance (belt-and-suspenders: ``optimize.py``'s own
move generators already respect ``inst_fixed_xy``/``inst_fixed_rot``, so
this is a second, independent guard at the write boundary, not the only
one).

Runs under the ``job_inproc`` executor (bounded, in-process — mirrors
``embed_batch``'s dispatch shape verbatim).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.pcb import realize as pcb_realize
from precis.pcb import resolved as pcb_resolved
from precis.pcb import run_record as pcb_run_record
from precis.pcb import session as pcb_session
from precis.pcb import session_store as pcb_session_store
from precis.pcb.optimize import (
    PLACE_ITERS,
    PLACE_ONLY_SCHEDULE,
    OptimizeConfig,
    digest_toon,
    hole_fab_clearance_mm,
    optimize,
    resolve_measures,
)
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.pcb.ir import PcbIR
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pcb_ref_id": {"type": "integer"},
        "iters": {"type": "integer", "minimum": 1},
        "seed": {"type": "integer"},
        "snap": {"type": "boolean"},
        "groups": {"type": "boolean"},
        "force": {"type": "boolean"},
        # DEPRECATED, accepted-and-ignored (the field polish engine
        # removed, Reto 2026-10-25).
        "field_polish": {"type": "boolean"},
        # topo-place spec §1c / Decision 21: the fast topological placer
        # behind placer='topo' — opt-in until the bench shows it beats
        # the default anneal. Absent = the optimize.py anneal.
        "placer": {"type": "string", "enum": ["topo"]},
        # Route is a second opt-in on top of placer='topo' (GRIPE_FIX
        # ruling): default False so a topo placement never rewrites the
        # board's derived copper unless the caller asked for it.
        "route": {"type": "boolean"},
        # Share-vector preset for placer='topo' (default 'compact'; 'flow'
        # prices the slice-5 route proxies — via estimate, backtrack,
        # direction). Validated against topoplace.PRESETS in the job.
        "preset": {"type": "string"},
    },
    "required": ["pcb_ref_id"],
    "additionalProperties": False,
}

#: job_inproc only — the whole point is bounded in-proc compute, never a
#: blocking MCP call.
COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "Placement-only anneal (translate/rotate/swap) over a pcb design's "
    "current graph — the enqueued half of put(args={'op':'place'})."
)

_DEFAULT_ITERS = PLACE_ITERS


#: Single-stage, placement-only schedule — canonical in
#: ``precis.pcb.optimize`` so this worker and the offline
#: ``precis.pcb.local`` wrapper share the one definition (pcb-workspace-code
#: slice 4). The private name stays for existing importers.
_PLACE_ONLY_SCHEDULE = PLACE_ONLY_SCHEDULE


def _on_fixed_vias_note(refdes: tuple[str, ...]) -> str:
    """The job summary's warning when the anneal ended with a part still
    on authored vias — empty when none is, which is the normal case."""
    if not refdes:
        return ""
    return (
        f"⚠️  {len(refdes)} part(s) still have a solder land on an authored "
        f"via: {', '.join(refdes)} — this placement is NOT routable as it "
        "stands; re-run op='place' with more iters or another seed.\n\n"
    )


def _legalized_note(refdes: tuple[str, ...]) -> str:
    """The job summary's line naming parts moved off an illegal starting
    pose before the anneal — empty when the stored placement was legal."""
    if not refdes:
        return ""
    return (
        f"moved {len(refdes)} part(s) off an illegal starting position "
        f"before annealing: {', '.join(refdes)}\n\n"
    )


def _soft_pin_note(travel: dict[str, float]) -> str:
    """The job summary's per-part travel report for soft-pinned
    (``max_travel_mm``) instances — the leash the anneal honoured, shown
    so a bounded part's movement is auditable rather than silent."""
    if not travel:
        return ""
    shown = ", ".join(f"{r} {mm:g}mm" for r, mm in sorted(travel.items()))
    return f"soft-pinned travel used: {shown}\n\n"


def _preferred_snap_note(snapped: dict[str, dict[str, float]]) -> str:
    """The job summary's line naming parts the preferred-number snap moved
    (``op='place' snap=true``), grid pitch per part — empty when the pass
    was off or nothing moved."""
    if not snapped:
        return ""
    shown = ", ".join(f"{r} @{s['pitch_mm']:g}mm" for r, s in sorted(snapped.items()))
    return f"preferred snap: {shown}\n\n"


def _support_groups_note(groups: tuple[str, ...]) -> str:
    """The job summary's line listing the support-passive rows the
    ``groups=true`` post-anneal pass organised — empty when the pass was
    off or the netlist carries no support structure."""
    if not groups:
        return ""
    return f"support groups: {'; '.join(groups)}\n\n"


def _ir_pose(ir: PcbIR, pose: Any) -> None:
    """Fold one (N, 4) topo pose into the IR's instance arrays — the
    write the run tail commits (x/y/rot + side)."""
    for i in range(len(pose)):
        x, y, rot_q, side = (float(v) for v in pose[i])
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        ir.inst_x[i] = x
        ir.inst_y[i] = y
        ir.inst_rot[i] = rot_q * 90.0
        ir.inst_bottom[i] = side >= 0.5


def _diverged_pose(
    td: Any,
    result: Any,
    settled_pose: Any,
    *,
    gate: Any,
    errors_before: int,
) -> tuple[Any, str]:
    """The pose a diverged anneal commits (gr540612): the settled best
    pose when the gate counts it at or under the stored board's error
    count, else the cost-sorted elites (each given the same label-room
    settle the best pose gets), then the stored pose itself — the first
    candidate under the line wins. Returns ``(pose, note)``; the note is
    "" when the anneal's own best stands. Effort stays a safe dial: a
    run that wandered returns its best committable pose with a 'best
    pose failed the pre-route gate' note instead of failing with nothing
    placed."""
    cands: list[tuple[str, Any, bool]] = [("", settled_pose, False)]
    cands += [
        (f"elite {i + 1}/{len(result.elites)}", e.pose, True)
        for i, e in enumerate(result.elites)
    ]
    cands.append(("the stored placement", td.pose0, False))
    from precis.pcb import topoplace as _tp

    for i, (label, pose, settle) in enumerate(cands):
        cand = _tp.settle_label_room(td, pose) if settle else pose
        n = gate(cand)
        # First-at-or-under, deliberately NOT lowest: the candidate order
        # (the anneal's own best, elites by cost, the stored pose) IS the
        # preference — a marginally cleaner later candidate never
        # preempts the pose the run actually produced.
        if n is not None and n <= errors_before:
            note = ""
            if i:
                note = (
                    f"⚠️  the best pose failed the pre-route gate — "
                    f"committed {label} instead ({n} pre-route DRC "
                    f"error(s), the stored placement had {errors_before})\n\n"
                )
            return cand, note
    return settled_pose, ""


def _topo_run(
    ctx: DispatchContext,
    ir: PcbIR,
    graph: dict[str, Any],
    footprints: dict[str, Any] | None,
    *,
    effort: int,
    seed: int,
    slug: str,
    features: list[dict[str, Any]] | None = None,
    fixed_copper: list[dict[str, Any]] | None = None,
    preset: str = "compact",
    capability: Any = None,
    hole_margin_mm: float | None = None,
    gate: Any = None,
    errors_before: int | None = None,
) -> tuple[Any, dict[str, Any], str, dict[str, str], dict[str, str], str] | None:
    """The ``placer='topo'`` run (topo-place spec §1c, Decision 21):
    ``design_from_ir`` → the proxy placer → ``assign_pins`` → the run
    files, with the winning pose folded back into ``ir`` so the shared
    gate/ripup/``pcb_set_pose`` tail below measures and writes it like
    an anneal result. Returns ``(result, files, summary, side_changes,
    label_spots)`` — ``label_spots`` the refdes → LABEL_SPOT_ORDER pin
    each part's refdes label should try first
    (:func:`precis.pcb.topoplace.label_spots_for`), persisted by the
    run tail as instance ``meta.label_spot`` so the silk draw honors
    the label space the placer preserved — ``None`` after recording an
    unclassed ``PinConflict`` failure (the board script's error, not
    the job's). Lazy imports: the kernel pulls scipy, which this
    worker otherwise never loads."""
    from precis.pcb import topoplace as _tp
    from precis.pcb.topoplace import runstore as _runstore
    from precis.pcb.topoplace import summary as _tpsummary
    from precis_geom.pcb import PinConflict

    try:
        if preset not in _tp.PRESETS:
            raise ValueError(
                f"unknown topo-place preset {preset!r} — known: {sorted(_tp.PRESETS)}"
            )
        td = _tp.design_from_ir(
            ir,
            footprints or {},
            net_classes=graph.get("net_classes"),
            features=features,
            fixed_copper=fixed_copper,
            placement_sides=(
                ((graph.get("board") or {}).get("meta") or {}).get("placement_sides")
            ),
            measures=graph.get("measures"),
            capability=capability,
            hole_margin_mm=hole_margin_mm,
        )
        _tp.check_slots(td)
        # gr520546: authored near()/separation/align measures steer the
        # anneal — the share is priced in only when the design carries
        # resolvable measures (an empty tuple would zero-divide its
        # reference magnitude to the 1.0 floor anyway, but the share
        # staying 0 keeps every measureless run's cost byte-identical).
        base_obj = _tp.PRESETS[preset]
        objective = (
            dataclasses.replace(base_obj, measures=0.2) if td.measures else base_obj
        )
        result = _tp.topo_place(td, objective=objective, effort=effort, seed=seed)
        # Post-anneal label-room repair (Reto 2026-10-29, taz-leaf runs
        # 34-39: 23-37 far labels however the anneal priced the room):
        # deterministic nudges that put every reachable refdes label next
        # to its part and pull edge-squished rings off the outline —
        # the guarantee the anneal's approximate box model cannot give.
        committed = _tp.settle_label_room(td, result.best_pose)
        diverged = ""
        # gr540612: a diverged long anneal must not strand the board at
        # the commit gate — walk the elites then the stored pose through
        # the same gate and commit the first candidate at or under the
        # stored error count. The gate evals mutate ``ir``, so the
        # pre-run side snapshot is taken first.
        bottom_before = np.asarray(ir.inst_bottom, dtype=bool).copy()
        if gate is not None and errors_before is not None:
            committed, diverged = _diverged_pose(
                td, result, committed, gate=gate, errors_before=errors_before
            )
        result = dataclasses.replace(result, best_pose=committed)
        if diverged:
            # A committed fallback must not wear the discarded anneal's
            # numbers — re-score the pose that actually writes (one
            # ProxyState eval, cheap next to the run) so best_cost,
            # crossings and length in the summary, manifest and
            # meta.last_place describe it.
            scored = _tp.ProxyState(td, committed, objective)
            result = dataclasses.replace(
                result,
                best_cost=float(scored.cost),
                crossings=int(scored.crossings),
                length=float(scored.length),
            )
        assignment = _tp.assign_pins(td, result.best_pose)
    except PinConflict as exc:
        ctx.record_failure(str(exc))
        return None
    pm = _tp.pair_map(td)

    _ir_pose(ir, result.best_pose)
    side_changes = {
        str(ir.instance_refdes[i]): ("bottom" if ir.inst_bottom[i] else "top")
        for i in range(len(td.parts))
        if bool(ir.inst_bottom[i]) != bool(bottom_before[i])
    }
    # The refdes-label pins for the settled pose — the placer's margin
    # term priced the label space, this is the spot each part's label
    # actually takes, in silk's greedy order. The run tail persists
    # them as instance meta in the same transaction as the pose.
    label_spots = _tp.label_spots_for(td, result.best_pose)

    board_hash = hashlib.sha256(
        json.dumps(graph, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    summary_text = _tpsummary.render_summary(
        td,
        result,
        assignment,
        pm,
        objective=objective,
        effort=effort,
        seed=seed,
        slug=slug,
    )
    files = _runstore.run_files(
        td,
        result,
        assignment,
        pm,
        summary=summary_text,
        manifest_payload=_runstore.manifest(
            td,
            result,
            objective=objective,
            effort=effort,
            board_hash=board_hash,
            parts=_runstore.manifest_parts(td, graph),
            footprints=_runstore.footprint_shas(footprints or {}),
        ),
    )
    files["label_spots.json"] = dict(sorted(label_spots.items()))
    return result, files, summary_text, side_changes, label_spots, diverged


def _plan_failed_count(plan: Any) -> int:
    """Unresolved-route problems, the metric the failure-driven shove
    loop minimises (0 = every net realized).  Duck-typed: the field
    list matches :class:`precis.pcb.topoplace.realise.RoutePlan`, and
    ``guarded``/``severed`` default absent for pre-field bench fakes."""
    return (
        len(plan.crossings)
        + len(plan.unreachable)
        + len(plan.no_via)
        + len(getattr(plan, "guarded", ()))
        + len(getattr(plan, "severed", ()))
    )


def _route_masks(
    td: Any,
    pose: Any,
    n_layers: int,
    net_rules: Any,
    min_via_dia: Any,
) -> Any:
    """The two-kernel mask set at one pose — shared by the layer-argmin
    (built fresh per route attempt; the caller passes it into
    ``route_design``) and the visibility payload (built again at the
    committed best pose).  Zone handling mirrors the payload's original
    inline block: keepout/mounting_hole/label_keepout only, silk zones
    scoped to the outers."""
    from shapely.geometry import Polygon

    from precis.pcb.topoplace import masks as _tpmasks
    from precis.pcb.topoplace import proxy as _tpproxy

    zone_polys: list[tuple[Any, tuple[int, ...]]] = []
    for z in td.zones:
        if z.kind not in ("keepout", "mounting_hole", "label_keepout"):
            continue
        if len(z.ring) < 4:
            continue
        zl = tuple(int(l) for l in (z.layers or ()))
        if z.kind == "label_keepout":
            # Silkscreen is an OUTER-layer fact: it never blocks an
            # inner corridor, and only the via annulus on the outers
            # cares (the siting walk's rule, mirrored here).
            outers = {0, n_layers - 1}
            zl = tuple(sorted(set(zl) & outers)) if zl else tuple(sorted(outers))
        zg = Polygon(np.asarray(_tpproxy.zone_world_ring(td, z, pose), dtype=float))
        if not zg.is_empty and zg.area > 0.0:
            zone_polys.append((zg, zl))
    rule_widths = [float(r.track_width_mm) for r in (net_rules or {}).values()]
    rule_clrs = [float(r.clearance_mm) for r in (net_rules or {}).values()]
    return _tpmasks.build_masks(
        td,
        pose,
        n_layers=n_layers,
        min_wire_mm=min(rule_widths) if rule_widths else 0.15,
        min_clearance_mm=min(rule_clrs) if rule_clrs else 0.15,
        via_dia_mm=float(min_via_dia) if min_via_dia else 0.6,
        zone_polys=tuple(zone_polys),
    )


def _route_failure_pushes(
    td: Any,
    plan: Any,
    pose: Any,
    net_rules: Any,
    min_via_dia: float | None,
    *,
    cap_mm: float = 2.0,
) -> list[Any]:
    """Blocker-directed shove rows, built from the plan's OWN failures
    (Reto 2026-10-30: "should just cause shove, and vias to be
    generated, and things to be pushed — don't log it, make it work"):

    * a ``no_via`` branch minted no via because the siting walk found
      no keepout-clean point anywhere on the corridor — push the
      branch's endpoint parts apart along their centre line by the via
      window (dia + clearance), stretching the corridor so the re-route
      has edges to site on;
    * a ``guarded`` net had copper withdrawn for approaching another
      net — push the parts owning the closest-approach pieces apart
      along the closest-approach direction by the anti-short veto's
      deficit.

    Rows are capped (``cap_mm``) and deduped by part pair — a push is
    one soft LP row the guards may relax, never a demand.  Geometry
    reads ``plan.pieces`` (which keeps withdrawn/failed geometry "for
    the record"), so no mesh rebuild is needed to build them."""
    from precis.pcb.topoplace.shove import ShovePush

    pose_a = np.asarray(pose, dtype=float)
    pin_part = np.asarray(td.pin_part, dtype=int)

    def _rules(net: int) -> Any:
        if 0 <= int(net) < len(td.net_name):
            return (net_rules or {}).get(str(td.net_name[int(net)]))
        return None

    def _via_window(net: int) -> float:
        r = _rules(net)
        dia = float(getattr(r, "via_dia_mm", None) or min_via_dia or 0.6)
        return min(cap_mm, dia + float(getattr(r, "clearance_mm", 0.15)))

    pushes: list[Any] = []
    by_pair: dict[tuple[int, int], int] = {}

    def _add(pa: int, pb: int, axis: tuple[float, float], sep: float) -> None:
        if sep <= 0.0:
            return
        key = (min(pa, pb), max(pa, pb))
        i = by_pair.get(key)
        if i is None:
            by_pair[key] = len(pushes)
            pushes.append(ShovePush(pa, pb, axis, sep))
        else:
            prev = pushes[i]
            if prev.sep_mm < sep:
                pushes[i] = ShovePush(prev.a, prev.b, prev.axis, sep)

    # ── no_via branches: endpoint separation along the corridor
    for bi in plan.no_via:
        branch_pieces = [p for p in plan.pieces if p.branch == bi]
        if not branch_pieces:
            continue
        pin_a = int(branch_pieces[0].pin_a)
        pin_b = int(branch_pieces[0].pin_b)
        if not (0 <= pin_a < len(pin_part) and 0 <= pin_b < len(pin_part)):
            continue
        pa, pb = int(pin_part[pin_a]), int(pin_part[pin_b])
        if pa == pb or not (0 <= pa < len(pose_a) and 0 <= pb < len(pose_a)):
            continue  # both ends on one part — no separation to gain
        axis = pose_a[pb, :2] - pose_a[pa, :2]
        _add(
            pa, pb, (float(axis[0]), float(axis[1])), _via_window(branch_pieces[0].net)
        )

    # ── guarded nets: closest-approach pair, pushed apart by the deficit
    guarded = set(getattr(plan, "guarded", ()))
    if guarded:
        from shapely.geometry import LineString
        from shapely.ops import nearest_points

        guarded_pieces = [p for p in plan.pieces if p.net in guarded]
        foreign_pieces = [p for p in plan.pieces if p.net not in guarded]
        for gp in guarded_pieces:
            g_line = LineString(gp.points)
            g_bounds = g_line.bounds
            best: tuple[float, Any] | None = None
            for op in foreign_pieces + [q for q in guarded_pieces if q.net != gp.net]:
                if op is gp:
                    continue
                if (
                    best is not None
                    and _bbox_gap(g_bounds, _line_bounds(op.points)) >= best[0]
                ):
                    continue
                d = float(g_line.distance(LineString(op.points)))
                if best is None or d < best[0]:
                    best = (d, op)
            if best is None:
                continue
            dist, op = best
            near_g, near_o = nearest_points(g_line, LineString(op.points))
            # the piece ends at its pins — the owning part is the one
            # whose END is nearest the approach point
            ga_part = _nearest_pin_part(gp, (near_g.x, near_g.y), pin_part, pose_a)
            gb_part = _nearest_pin_part(op, (near_o.x, near_o.y), pin_part, pose_a)
            if ga_part is None or gb_part is None or ga_part == gb_part:
                continue
            rg, ro = _rules(gp.net), _rules(op.net)
            veto = (
                float(getattr(rg, "track_width_mm", 0.2))
                + float(getattr(ro, "track_width_mm", 0.2))
            ) / 2.0 + max(
                float(getattr(rg, "clearance_mm", 0.15)),
                float(getattr(ro, "clearance_mm", 0.15)),
            )
            _add(
                ga_part,
                gb_part,
                (near_o.x - near_g.x, near_o.y - near_g.y),
                min(cap_mm, veto - dist + 0.1),
            )
    return pushes


def _line_bounds(pts: Any) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_gap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    dx = max(a[0], b[0]) - min(a[2], b[2])
    dy = max(a[1], b[1]) - min(a[3], b[3])
    return math.hypot(max(0.0, dx), max(0.0, dy))


def _nearest_pin_part(
    piece: Any, xy: tuple[float, float], pin_part: Any, pose_a: Any
) -> int | None:
    """The part owning the piece END (pin side) nearest ``xy`` — the
    half of the piece that actually approaches the foreign copper."""
    ends = ((piece.pin_a, piece.points[0]), (piece.pin_b, piece.points[-1]))
    best_pin: int | None = None
    best_d = math.inf
    for pin, pt in ends:
        pin = int(pin)
        if not (0 <= pin < len(pin_part)):
            continue
        d = math.hypot(float(pt[0]) - xy[0], float(pt[1]) - xy[1])
        if d < best_d:
            best_d = d
            best_pin = pin
    return int(pin_part[best_pin]) if best_pin is not None else None


def _topo_route(
    ctx: DispatchContext,
    ir: PcbIR,
    graph: dict[str, Any],
    footprints: dict[str, Any] | None,
    *,
    features: list[dict[str, Any]] | None,
    fixed_copper: list[dict[str, Any]] | None,
    capability: Any,
    board_id: Any,
    pcb_ref_id: int,
    label_spots: dict[str, str] | None = None,
    out_files: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """The topo route half of a ``placer='topo'`` run (slice-5 ruling:
    place and route are ONE job — the pose commits above, then the same
    pose is routed and its copper + ``pcb_routes`` rows written). ``ir``
    already carries the placed pose (``_topo_run`` mutated it), so
    ``route_design``'s ``d.pose0`` default IS the routed pose.

    The place half's label pins ride in as ``keepout`` zones
    (:func:`precis.pcb.topoplace.label_boxes_for`) — the router never
    sites a via where the placer promised the refdes label would sit;
    without this the silk pass runs after routing and a via on the pin
    walks the label (taz-leaf run 35: U2's label 13 mm away).

    BEST-EFFORT: never fails the place. No board row or no fab
    capability row skips with a note; a raise is caught by the caller
    and lands as a summary line, because a placed-but-unrouted board is
    strictly better than a failed job that wrote nothing. Returns
    ``(summary_text, meta)`` — ``meta`` lands under
    ``meta.topo_run.route``."""
    from precis.pcb import DANGLING_NET_NOTE_PREFIX
    from precis.pcb import drc as pcb_drc
    from precis.pcb import topoplace as _tp
    from precis.pcb.topoplace import realise as _tprealise

    if board_id is None:
        return (
            "topo-route: skipped — the design has no board row, so there is "
            "nowhere to write copper or route status; the placement stands\n",
            {},
        )
    if capability is None:
        return (
            "topo-route: skipped — no fab capability row for this stackup, "
            "so the widen oracle has nothing to measure against\n",
            {},
        )
    td_route = _tp.design_from_ir(
        ir,
        footprints or {},
        net_classes=graph.get("net_classes"),
        features=features,
        fixed_copper=fixed_copper,
        route_planes=True,
        capability=capability,
        hole_margin_mm=hole_fab_clearance_mm(capability),
    )
    # The place half's label pins become route-time keep-outs: the
    # router never sites a via (or threads a wire) through the box the
    # placer promised the refdes label. td_route's pose0 IS the placed
    # pose, so the boxes land where the silk pass will look for them.
    if label_spots:
        boxes = _tp.label_boxes_for(td_route, td_route.pose0, label_spots)
        if boxes:
            td_route = dataclasses.replace(
                td_route,
                zones=tuple(td_route.zones)
                + tuple(
                    _tp.TopoZone(
                        kind="label_keepout",
                        ring=ring,
                        name=f"label:{refdes}",
                    )
                    for refdes, ring in boxes
                ),
            )
    layers = [str(layer.get("name")) for layer in ir.stackup]
    outline = pcb_session.outline_from_features(features or [])
    keepouts = pcb_session.keepouts_from_features(features or [])
    loaded = ctx.store.pcb_load(pcb_ref_id)
    net_rules = pcb_session.route_net_rules(loaded, capability)
    # gr524334: the siting walk's fallback dia/drill pair — the fab
    # minimum, tried when a net's rule via (the comfortable house
    # default) finds no site on a branch.
    min_via_dia = capability.jlc_min.get("via_diameter_mm")
    min_via_drill = capability.jlc_min.get("drill_mm")

    # gr513870: per-round progress lands as job_event chunks — the live
    # surface a watcher tails while the route runs (phase lines plus
    # one line per assign_layers round and per shove round).
    def _route_event(t: str) -> None:
        ctx.append_chunk("job_event", t)

    event = _route_event
    plan, rres = _tprealise.route_design(
        td_route,
        ir,
        net_rules=net_rules,
        min_via_dia=None if min_via_dia is None else float(min_via_dia),
        min_via_drill=None if min_via_drill is None else float(min_via_drill),
        event=event,
        masks=_route_masks(
            td_route, td_route.pose0, ir.n_layers, net_rules, min_via_dia
        ),
    )

    # Failure-driven shove (Reto 2026-10-30: "should just cause shove, and
    # vias to be generated, and things to be pushed — don't log it, make it
    # work"): the plan's own failures name their blockers — a no_via
    # branch's endpoint parts, a guarded net's closest-approach pair —
    # and those pairs become targeted LP push rows (see
    # _route_failure_pushes).  Bounded rounds, keep the best
    # realization.  A board that routes clean the first time takes the
    # zero-cost path (failed empty -> break before any shove); a shove
    # that stops moving, overflows the mesh, or stops improving gives
    # up and keeps the best plan so far.
    from precis.pcb.topoplace import shove as _tpshove

    route_shove_mm = 0.0
    route_shove_rounds = 0
    pose = td_route.pose0
    best_plan, best_rres = plan, rres
    best_pose = pose
    best_failed = _plan_failed_count(plan)
    for _round in range(3):
        if best_failed == 0:
            break
        pushes = _route_failure_pushes(
            td_route, best_plan, pose, net_rules, min_via_dia
        )
        # pushes name the participants when the plan's geometry supports
        # it; a failing plan that names none still shoves blind (the v1
        # contract the bench fakes encode: failures exist -> shove and
        # re-route, pushes only steer the direction).
        shoved = (
            _tpshove.shove_pose(td_route, pose, rounds=3, push=pushes)
            if pushes
            else _tpshove.shove_pose(td_route, pose, rounds=3)
        )
        route_shove_mm += shoved.moved_mm
        if shoved.moved_mm <= 1e-6:
            break
        route_shove_rounds += 1
        pose = shoved.pose
        ctx.append_chunk(
            "job_event",
            f"topo-route: shove round {route_shove_rounds} — "
            f"{best_failed} unresolved, {len(pushes)} push(es), "
            f"re-routing",
        )
        try:
            plan, rres = _tprealise.route_design(
                td_route,
                ir,
                pose=pose,
                net_rules=net_rules,
                min_via_dia=None if min_via_dia is None else float(min_via_dia),
                event=event,
                masks=_route_masks(td_route, pose, ir.n_layers, net_rules, min_via_dia),
            )
        except _tprealise.CorridorOverflow:
            break
        failed = _plan_failed_count(plan)
        if failed >= best_failed:
            break
        best_plan, best_rres, best_pose, best_failed = plan, rres, pose, failed
    plan, rres = best_plan, best_rres

    # gr541685: the kept plan may come from a SHOVED pose — that pose is
    # the board the copper was routed for, so it must be committed too or
    # the stored pads sit ~δ off the wire ends (check then reports
    # 'connectivity' errors on a freshly routed board). Shove translates
    # only: an x/y re-stamp, side and rot never change. ``ir`` carries it
    # from here so the widen oracle's pads (``pads_for_ir``), the
    # ``pcb_set_pose`` below and the run record's pose map all agree.
    shoved_kept = best_pose is not td_route.pose0
    pose0_x, pose0_y = ir.inst_x.copy(), ir.inst_y.copy()
    if shoved_kept:
        for i in range(len(td_route.parts)):
            ir.inst_x[i] = float(best_pose[i, 0])
            ir.inst_y[i] = float(best_pose[i, 1])

    # The widen oracle's own map — the FULL clearance resolution (per-net
    # class floors included), not the route map's signal-width variant.
    drc_net_rules = pcb_session.clearance_net_rules(loaded, capability)

    def _drc_findings(result: Any) -> list[Any]:
        # The widen stage's clearance oracle — the SAME model + rules the
        # view='drc' gate runs (pads + emitted copper + authored fixed
        # copper + keepouts + per-net clearance), so "clearance allows"
        # is exactly what the gate accepts — a class ``min_clearance_mm``
        # is an error tier the fab floor alone would let a widened rail
        # violate. ``widen_power`` keys the findings, so the oracle must
        # return them, not a count (gr524392).
        model = pcb_session._copper_model(
            ir,
            pcb_realize.result_copper_rows(result, ir, layers, derived=True)
            + [{**row, "net": row.get("net") or ""} for row in (fixed_copper or [])],
            footprints=footprints,
        )
        return [
            f
            for f in pcb_drc.run_geometric_drc(
                model,
                capability=capability,
                outline=outline,
                keepouts=keepouts,
                net_rules=drc_net_rules,
            )
            if f.severity == "error"
        ]

    # The widen targets are the nets' current-derived IPC-2221 widths —
    # clearance_net_rules' resolution, the same map the DRC oracle below
    # measures against. A rail claims its target where the oracle
    # allows; the widen set is the plane/power rails plus the nets whose
    # target exceeds the width they were born at, and an unannotated
    # rail takes the widest step the oracle accepts (gr524239: a real
    # current estimate outranks the class — a 1 mA rail keeps its rule
    # width).
    power_ids, targets = _tprealise.widen_targets(ir, net_rules, drc_net_rules)
    rres, widths = _tprealise.widen_power(
        rres, power_ids, drc_findings=_drc_findings, targets=targets
    )
    rows = _tprealise.sketch_rows(plan, td_route, ir, layers)
    # gr513871's produced half: the run's binding-constraint digest —
    # stamped on meta.topo_run.route (this job's drill-down), on the
    # board ref's meta.last_route.digest (view='route-status' renders
    # it ahead of the per-net table) and on the laptop side's
    # route.json.
    from precis.pcb import route_digest as pcb_route_digest

    digest = pcb_route_digest.build_digest(rows, layer_names=layers)

    # pcb_copper.net_id is a real FK — the IR-local net int resolves back
    # to the DB net_id by name (the deleted pcb_route's own join, verbatim).
    db_net_ids = ctx.store.pcb_net_ids(pcb_ref_id)
    copper_rows: list[dict[str, Any]] = []
    for t in rres.tracks:
        db_id = db_net_ids.get(str(ir.net_name[t.net_id]))
        if db_id is None or t.layer < 0 or t.layer >= len(layers):
            continue
        copper_rows.append(
            {
                "ctype": "track",
                "layer": layers[t.layer],
                "net_id": db_id,
                "route_id": None,
                "geom": {
                    "segments": list(t.segments),
                    "length_mm": round(t.length_mm, 4),
                    "width_mm": t.width_mm,
                    "is_dogbone": bool(t.is_dogbone),
                },
            }
        )
    for v in rres.vias:
        db_id = db_net_ids.get(str(ir.net_name[v.net_id]))
        if db_id is None:
            continue
        lo = v.layer_lo if 0 <= v.layer_lo < len(layers) else 0
        hi = v.layer_hi if 0 <= v.layer_hi < len(layers) else len(layers) - 1
        copper_rows.append(
            {
                # pcb_copper.layer is NOT NULL; a via's real membership is
                # geom["span"] — this column holds the span's lower layer
                # purely for the schema (every reader reads the span).
                "ctype": "via",
                "layer": layers[lo],
                "net_id": db_id,
                "route_id": None,
                "geom": {
                    "x": round(v.x, 4),
                    "y": round(v.y, 4),
                    "dia_mm": v.dia_mm,
                    "drill_mm": v.drill_mm,
                    "span": [layers[lo], layers[hi]],
                },
            }
        )
    for pour in rres.pours:
        db_id = db_net_ids.get(str(pour.get("net", "")))
        if db_id is None:
            continue
        geom: dict[str, Any] = {"polygon": pour["polygon"]}
        if pour.get("holes"):
            geom["holes"] = pour["holes"]
        for key in ("starved_thermal", "fill"):
            if pour.get(key):
                geom[key] = pour[key]
        copper_rows.append(
            {
                "ctype": "pour",
                "layer": str(pour.get("layer", "")),
                "net_id": db_id,
                "route_id": None,
                "geom": geom,
            }
        )
    n_realized = sum(1 for r in rows.values() if r["status"] == "realized")
    n_failed = sum(1 for r in rows.values() if r["status"] == "failed")
    # ONE transaction: a raise between the copper replace and the route
    # rows must never strand copper without route status. An empty route
    # result never wipes stored copper — the replace is skipped so the
    # copper that stays keeps matching view='route-status' — but the
    # run's FAILED rows still land (gr537831 review: an all-withdrawn
    # board wrote nothing at all — no route rows meant every net read
    # 'unrouted' by default and the check surface saw no verdict, which
    # the bench then counted as a clean 0). Non-failure rows stay put:
    # a 'sketched'/'realized' overwrite would contradict the kept copper.
    failed_rows = {k: r for k, r in rows.items() if r["status"] == "failed"}
    with ctx.store.tx() as conn:
        # The kept shoved pose commits inside the copper transaction
        # (gr541685): a routes-write raise rolls the pose back with the
        # copper, and a no-copper run keeps the anneal pose the stored
        # copper still matches. Only parts the shove actually moved are
        # written — ``pcb_set_pose``'s per-axis ``fixed`` guard applies
        # either way, but x/y is all the shove changed.
        n_pose = (
            ctx.store.pcb_set_pose(
                pcb_ref_id,
                {
                    str(ir.instance_refdes[i]): (
                        float(best_pose[i, 0]),
                        float(best_pose[i, 1]),
                        float(ir.inst_rot[i]),
                    )
                    for i in range(len(td_route.parts))
                    if math.isfinite(float(best_pose[i, 0]))
                    and math.isfinite(float(best_pose[i, 1]))
                    and (
                        float(best_pose[i, 0]) != float(td_route.pose0[i, 0])
                        or float(best_pose[i, 1]) != float(td_route.pose0[i, 1])
                    )
                },
                conn=conn,
            )
            if shoved_kept and copper_rows
            else 0
        )
        n_copper = (
            ctx.store.pcb_copper_replace(int(board_id), copper_rows, conn=conn)
            if copper_rows
            else 0
        )
        n_routes = (
            ctx.store.pcb_routes_write(pcb_ref_id, int(board_id), rows, conn=conn)
            if copper_rows
            else (
                ctx.store.pcb_routes_write(
                    pcb_ref_id, int(board_id), failed_rows, conn=conn
                )
                if failed_rows
                else 0
            )
        )
        # The digest rides the same guard as the rows it summarizes —
        # stamping it over a run whose rows were skipped would make
        # route-status narrate a different run than its table shows.
        if copper_rows:
            ctx.store.pcb_last_route_merge(
                pcb_ref_id,
                {
                    "realized": n_realized,
                    "failed": n_failed,
                    "digest": digest,
                },
                conn=conn,
            )

    if shoved_kept and not copper_rows:
        # Nothing persisted — the board keeps the anneal pose its stored
        # copper matches, so ``ir`` restores pose0 too: the run record
        # stamped off it must describe what the board actually committed,
        # not a pose that was only probed.
        ir.inst_x[:] = pose0_x
        ir.inst_y[:] = pose0_y
    n_dangling = sum(
        1
        for r in rows.values()
        if (r.get("note") or "").startswith(DANGLING_NET_NOTE_PREFIX)
    )
    n_failed = sum(1 for r in rows.values() if r["status"] == "failed")

    # ── Visibility payload (Reto: "I just see ratsnest" — dump the plan
    # itself): per-net topology/status/problems from sketch_rows plus the
    # piece/via geometry, and the two-kernel routing masks at the
    # COMMITTED pose (the shoved pose if the loop kept one) — the "via
    # prohibited" / "pad blocking bars" debug layers, vector throughout.
    if out_files is not None:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union

        from precis.pcb.topoplace import masks as _tpmasks

        # plan pieces/vias carry the TOPO DESIGN's net index
        # (WirePath.net is a d.net_pins index); the IR's enumeration can
        # differ (the flex-pin stage renumbers) — sketch_rows joins the
        # two BY NAME via name_to_k, and so must this payload, or every
        # net's pieces/vias/corridor land under another net's name
        # (taz-leaf-local-55: S2_DRV's real R12.1→R13.1 plan displayed
        # as an S1_EXA_* piece, and a failed U2-region net's plan as
        # "S2_DRV" — the web view showed ghost-hunting routes while the
        # stored pcb_copper, named via realise_result's d.net_name, was
        # correct all along).
        net_names = [str(td_route.net_name[i]) for i in range(len(td_route.net_name))]
        pieces_by_net: dict[int, list[dict[str, Any]]] = {}
        for p in plan.pieces:
            pieces_by_net.setdefault(int(p.net), []).append(
                {
                    "branch": int(p.branch),
                    "layer": layers[p.layer] if 0 <= p.layer < len(layers) else p.layer,
                    "pts": [
                        [round(float(x), 4), round(float(y), 4)] for x, y in p.points
                    ],
                }
            )
        vias_by_net: dict[int, list[dict[str, Any]]] = {}
        for v in plan.vias:
            vias_by_net.setdefault(int(v.net), []).append(
                {
                    "x": round(float(v.x), 4),
                    "y": round(float(v.y), 4),
                    "branch": int(v.branch),
                }
            )
        wires_payload: dict[str, Any] = {"nets": {}}
        for name, row in rows.items():
            ni = net_names.index(name) if name in net_names else None
            wires_payload["nets"][name] = {
                **row,
                "pieces": pieces_by_net.get(ni, []) if ni is not None else [],
                "vias": vias_by_net.get(ni, []) if ni is not None else [],
            }
        out_files["wires.json"] = wires_payload

        board_masks = _route_masks(
            td_route, best_pose, ir.n_layers, net_rules, min_via_dia
        )
        out_files["masks.json"] = {
            "layer_names": layers,
            "layers": [
                {
                    "route_block": lm.route_block,
                    "spines": [
                        {
                            "pts": [
                                [round(float(x), 4), round(float(y), 4)]
                                for x, y in s.pts
                            ],
                            "width_mm": s.width_mm,
                            "refdes": s.refdes,
                            "layer": s.layer,
                        }
                        for s in lm.spines
                    ],
                    "route_block_free": lm.route_block_free,
                    "via_prevent_free": lm.via_prevent_free,
                }
                for lm in board_masks.layers
            ],
            "via_prevent": board_masks.via_prevent,
        }
        out_files["masks.svg"] = _tpmasks.masks_svg(
            board_masks, td_route.outline, tuple(layers)
        )

        # Per-net, per-layer corridor obstruction (the argmin inputs, made
        # visible BEFORE the layer chooser consumes them): thicken each
        # piece's centerline to the net's rule width against every layer's
        # mask set — worst blocked share across the net's pieces + total
        # spine crossings.  Read-only: no routing behavior changes here.
        rb_unions = []
        for lm in board_masks.layers:
            rb_unions.append(
                unary_union([Polygon(r) for r in lm.route_block])
                if lm.route_block
                else None
            )

        def _piece_width(name: str) -> float:
            # widths comes from widen_power keyed by IR net id; meet the
            # payload's (design-indexed) names by name, never by index.
            for k_ir, w in widths.items():
                if str(ir.net_name[k_ir]) == name:
                    return float(w)
            mins = [float(r.track_width_mm) for r in (net_rules or {}).values()]
            return min(mins) if mins else 0.15

        for name, payload in wires_payload["nets"].items():
            if not payload["pieces"]:
                continue
            pw = _piece_width(name)
            corridor: dict[str, Any] = {}
            for li, lm in enumerate(board_masks.layers):
                lname = layers[li] if 0 <= li < len(layers) else f"L{li}"
                worst_blocked = 0.0
                crossings = 0
                for pc in payload["pieces"]:
                    b, c = _tpmasks.corridor_score(
                        lm, pc["pts"], pw, rb_union=rb_unions[li]
                    )
                    worst_blocked = max(worst_blocked, b)
                    crossings += c
                corridor[lname] = {
                    "blocked_fraction": round(worst_blocked, 4),
                    "spine_crossings": crossings,
                }
            payload["corridor"] = corridor

    meta = {
        "nets": len(rows),
        "realized": n_realized,
        "failed": n_failed,
        "tracks": len(rres.tracks),
        "vias": len(rres.vias),
        "crossings": len(plan.crossings),
        "unreachable": len(plan.unreachable),
        "route_shove_rounds": route_shove_rounds,
        "route_shove_mm": round(route_shove_mm, 3),
        # instances whose committed x/y the kept shoved pose re-stamped
        # (0 when the kept plan was routed at pose0 or nothing persisted)
        "shove_committed": n_pose,
        # the corridor column is the REFUSAL count — a route that ran
        # had no over-capacity cut (a nonzero record raises instead)
        "corridor_overflow": 0,
        "widened": {str(k): v for k, v in widths.items()},
        # Whether the numbers above actually landed: an empty route
        # result skips BOTH writes, so pcb_routes/pcb_copper keep an
        # earlier run's rows — a reader comparing this meta against the
        # stored rows must not read the keep as this run's truth
        # (gr540907: meta said {realized: 7, tracks: 0} while the stored
        # rows held a previous attempt's {realized: 10, tracks: 3}).
        "persisted": bool(copper_rows),
        "digest": digest,
    }
    if not copper_rows:
        stored = ctx.store.pcb_route_status(pcb_ref_id)
        meta["stored"] = {
            "realized": sum(1 for r in stored if r["status"] == "realized"),
            "tracks": sum(
                1
                for c in ctx.store.pcb_copper_list(int(board_id))
                if c.get("ctype") == "track" and not c.get("fixed")
            ),
        }
    widened_line = ""
    if widths:
        widened_line = "; widened " + ", ".join(
            f"{ir.net_name[k]}→{w}mm" for k, w in sorted(widths.items())
        )
    text = (
        f"topo-route: {len(rows)} net(s) — {n_realized} realized "
        f"({n_dangling} dangling), {n_failed} failed, "
        f"{len(plan.crossings)} unresolved crossing(s), "
        f"{len(plan.unreachable)} unreachable net(s); wrote {n_copper} "
        f"copper row(s), {n_routes} route row(s)"
        + (f"; shoved pose committed for {n_pose} part(s)" if n_pose else "")
        + widened_line
        + (" — route produced nothing; stored copper kept" if not copper_rows else "")
        + "\n"
    )
    return text, meta


def _run_record_poses(ir: PcbIR) -> dict[str, dict[str, Any]]:
    """The committed pose map as ``{refdes: {x, y, rot, side}}`` — the
    D1 record's replayable payload (``side`` rides ``inst_bottom``;
    NaN-pose instances are omitted like :func:`pcb_session.positions`)."""
    return {
        str(ir.instance_refdes[i]): {
            "x": float(ir.inst_x[i]),
            "y": float(ir.inst_y[i]),
            "rot": float(ir.inst_rot[i]),
            "side": "bottom" if bool(ir.inst_bottom[i]) else "top",
        }
        for i in range(ir.n_instances)
        if math.isfinite(float(ir.inst_x[i])) and math.isfinite(float(ir.inst_y[i]))
    }


def _stamp_committed_record(
    ctx: DispatchContext,
    *,
    pcb_ref_id: int,
    placer: str,
    params: dict[str, Any],
    input_digest: str,
    ir: PcbIR,
    scores: dict[str, Any],
    ripped: set[int],
    placement_only: bool,
    elites: dict[str, Any] | None = None,
) -> None:
    """D1 (design-rerun-pull S1): the terminal ``pcb_place`` job carries
    its run record on ``meta.run_record`` — the pose map + scores a later
    resubmit replays verbatim. ``result_digest`` is the post-commit
    ``content_hash`` over a FRESH snapshot, so a resubmit on the board
    this run left behind hits on it (``run_record.lookup``).
    ``placement_only`` marks a run that had no route stage (gr512900
    fix 4) — replay says "placement replayed; route not replayed"."""
    after = pcb_session_store.load_snapshot(ctx.store, pcb_ref_id)
    result_digest = pcb_session.content_hash(
        after.graph, params, session_state=after.session_state()
    )
    ctx.set_meta(
        run_record=pcb_run_record.committed_record(
            op="place",
            placer=placer,
            params=params,
            input_digest=input_digest,
            result_digest=result_digest,
            footprint_digest=pcb_run_record.footprint_fingerprint(after.graph),
            poses=_run_record_poses(ir),
            scores=scores,
            ripped_fixed_ids=sorted(ripped),
            placement_only=placement_only,
            elites=elites,
        )
    )


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    pcb_ref_id = int(params["pcb_ref_id"])
    placer = str(params.get("placer") or "")
    # ``iters`` is the placer's budget either way — for 'topo' it is the
    # spec's eval-count effort (its own default 20000, mirroring
    # ``pcb.local.topo_place``), for the anneal ``PLACE_ITERS``.
    iters = int(params.get("iters") or (20000 if placer == "topo" else _DEFAULT_ITERS))
    seed = int(params.get("seed") or 0)
    snap = bool(params.get("snap") or False)
    # Route is a second opt-in on placer='topo' (GRIPE_FIX ruling) —
    # absent/False leaves the stored derived copper untouched.
    route = bool(params.get("route"))
    groups = bool(params.get("groups") or False)
    # The topo share vector, by preset name (default 'compact'). The
    # route-aware 'flow' preset prices the slice-5 proxies (via
    # estimate, backtrack, direction) for boards under active routing
    # iteration; crossings stay primary on every preset.
    preset = str(params.get("preset") or "compact")

    # The effective netlist (Slice 7): the latest resolved snapshot over
    # the authored graph, so placement's ratsnest is built against the
    # SAME netlist pcb_route realizes copper for. Read-only: only the
    # route job resolves and writes a snapshot.
    # One read of the graph and every surface the idem key hashes
    # (`load_snapshot`, the same call `_enqueue_op` digests); the resolved
    # snapshot is job-written, not hashed, so it stays a separate read.
    design = pcb_session_store.load_snapshot(ctx.store, pcb_ref_id)
    from precis.pcb.currents import require_net_currents

    require_net_currents(
        ((n["name"], n.get("est_current_a")) for n in design.graph.get("nets", [])),
        board_id=pcb_ref_id,
    )
    graph = pcb_resolved.apply(
        design.graph, ctx.store.pcb_netlist_snapshot_latest(pcb_ref_id)
    )
    if not graph.get("instances"):
        ctx.record_failure(
            f"pcb_place: design (ref_id={pcb_ref_id}) has no instances to place",
            failure_class="infra",
        )
        return

    # D1 (design-rerun-pull S1): the digest this run was enqueued under —
    # same recipe ``_enqueue_op`` hashes at submit (raw graph + minted
    # params + the snapshot surfaces), so a resubmit on the PRE-run state
    # (a stored-and-recovered board) hits this record's ``input_digest``.
    input_digest = pcb_session.content_hash(
        design.graph, params, session_state=design.session_state()
    )

    features = design.features
    board_id = (graph.get("board") or {}).get("board_id")
    ir = pcb_session.build_ir(
        graph,
        outline=pcb_session.outline_from_features(features),
        # Same hydration as pcb_route: the placer doesn't claim holes
        # itself, but the realize probe this job's auto-checks run does,
        # and an IR without them under-reports congestion near a corner.
        mounting_holes=pcb_session.mounting_holes_from_features(features),
        # Real per-pin positions where a footprint is cached/authored
        # (gripe 338983) — the anneal's cost terms (ratsnest length,
        # crossings, courtyard hulls) are all sub-instance geometry, so a
        # part placed against synthesized pin offsets is optimized against
        # a land pattern the board does not have.
        footprints_by_lcsc=ctx.store.pcb_footprints_for(pcb_ref_id),
        local_footprints_by_name=ctx.store.pcb_local_footprints_for(pcb_ref_id),
        # Authored copper (a generator's plaza vias) the placer must keep
        # every solder land off — without it a part is free to land on a
        # via that existed before placement ran.
        fixed_copper=design.fixed_copper if board_id is not None else None,
    )

    # Re-apply persisted plane assignments (authored `op='plane_net'` AND a
    # prior pcb_route run's derived write-back) onto the fresh IR BEFORE
    # placement runs — promote_plane() is L1 state the IR never persists on
    # its own (same discipline pcb_route.py already uses). This is read-only
    # context the placement anneal optimizes AROUND, never a move it can
    # propose: `_PLACE_ONLY_SCHEDULE` carries no PLANE_PROMOTE/DEMOTE weight,
    # so nothing here re-enables that move class. Without this, a human's
    # "GND is the plane on In1.Cu" declaration is invisible to placement,
    # which then treats a 200-pin GND net as an ordinary short-path signal
    # net to optimize around — the exact "one rule, two call sites, drifted"
    # shape the plane write-back bug (gr267526) already was. Because this
    # job cannot change a plane assignment (no PLANE_* move in its
    # schedule), it must never write back to pcb_planes — only the job that
    # can change a thing may persist it (pcb_route.py's job, not this one).
    # The stackup's own plane_net/fill declarations ride the same call
    # (pcb_session.apply_plane_assignments), so placement and routing agree
    # on which nets are planes.
    pcb_session.apply_plane_assignments(ir, design.planes)

    # Authored proximity/separation intent (put(args={'measures':[...]})),
    # resolved to the pair-distance cost terms `optimize.py`'s anneal can
    # actually enforce (precis-measures-help) — previously measured but
    # never enforced by this job (only the legacy `place.autoplace` quick
    # placer honoured them). `resolve_measures` silently drops anything
    # this engine can't express as a two-instance distance bound (a
    # `height`/connectivity metric, a role-based operand, a `gauge`
    # strength) — those stay eyes.py-evaluated only, same as before.
    measures = resolve_measures(design.measures, features=features)

    config = OptimizeConfig(
        iters=iters,
        seed=seed,
        schedule=_PLACE_ONLY_SCHEDULE,
        measures=measures,
        preferred_snap=snap,
        support_groups=groups,
    )
    # Pre-route DRC gate, measured TWICE: on the stored placement before
    # the anneal touches `ir`, and on the annealed one before anything is
    # written. The route job has had a never-commit-a-worse-board guard
    # since gr480010; this job had none, and on taz15 (gr489911, an
    # EasyEDA import whose source routing is 937 fixed-copper rows) it
    # annealed all 96 parts off the copper terminating on their pads and
    # wrote the poses with 186 placement errors where the stored board had
    # 37 — every later route then refused on 172 fixed-copper shorts no
    # router can remove. A worse count refuses the write unless
    # `force=True`; equal or better writes as before. Best-effort, same as
    # the old end-of-place gate: a stackup with no fab capability row (not
    # 2/4-layer) or a board not yet minted is not a placement error, so the
    # gate is skipped (both counts None), never failed — and a crash in
    # this geometry pass degrades to "no gate result", logged.
    force = bool(params.get("force", False))
    gate_board_id = (graph.get("board") or {}).get("board_id")
    capability = None
    footprints: dict[str, Any] | None = None
    try:
        if gate_board_id is not None:
            try:
                capability = pcb_session.stackup_capability(
                    ir.stackup, meta=(graph.get("board") or {}).get("meta")
                )
            except ValueError:
                capability = None
        if gate_board_id is not None:
            footprints = pcb_session.footprints_by_refdes(
                ir,
                ctx.store.pcb_footprints_for(pcb_ref_id),
                local_footprints_by_name=ctx.store.pcb_local_footprints_for(pcb_ref_id),
                local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(
                    graph
                ),
            )
    except Exception:
        log.exception("pcb_place: pre-route DRC gate inputs raised; gate skipped")
        capability = None
        footprints = None
    # gr491942: the per-net clearance floors the put judge and the handler's
    # seed legaliser apply (the coated hv floor on a parylene EWOD board).
    # Without them the anneal judged every land-to-authored-via pair at the
    # flat gap, and its legality gate let an HV sink slide back under the
    # floor its legalised seed had cleared. The anneal's existing hard
    # rejection (`_fixed_via_gap` inside its proposal legality) does the
    # rest. A board with no capability row keeps the flat gap, as before.
    # Resolved on its own, not via the gate's `capability`: a footprint
    # hydration failure that skips the gate must not drop the floors too.
    try:
        floor_capability = pcb_session.stackup_capability(
            ir.stackup, meta=(graph.get("board") or {}).get("meta")
        )
    except ValueError:
        floor_capability = None
    if floor_capability is not None:
        ir.net_clearance_floor_mm = pcb_session.net_clearance_floors(
            ctx.store.pcb_load(pcb_ref_id), floor_capability
        )
    # The "before" count is only a baseline when every part HAD a pose:
    # `placement_drc_findings` skips NaN-pose instances, so a never-placed
    # (or partly placed) board measures ~0 errors before the anneal and a
    # first placement with one residual error would read as "worse than
    # stored". There the guard has nothing honest to compare against.
    fully_placed_before = bool(
        ir.n_instances
        and not any(
            math.isnan(float(ir.inst_x[i])) or math.isnan(float(ir.inst_y[i]))
            for i in range(ir.n_instances)
        )
    )

    # gr489911 part 2 (Reto 2026-10-16): a part the anneal moves takes the
    # fixed copper on its pads with it — those branches are retired and
    # their connections become unrouted work for op='route'. The branches
    # are measured HERE, on the stored poses the copper was laid against;
    # after the anneal the pads no longer touch it. Copper between parts
    # that stay is kept. Nothing enters the anneal's objective.
    fixed_rows = design.fixed_copper
    branches = (
        pcb_session.fixed_copper_branches(
            ir,
            footprints,
            pcb_session.rippable_fixed_copper(
                fixed_rows, set(ctx.store.pcb_generators_for(pcb_ref_id))
            ),
        )
        if fixed_rows and footprints is not None
        else []
    )
    # Copper the stored board already carries as a pad short: broken
    # whoever moves, so it is ripped like copper of a moved part.
    shorted_before = (
        pcb_session.fixed_copper_pad_shorts(
            ir, footprints or {}, fixed_rows, capability
        )
        if branches
        else set()
    )
    pose_before = pcb_session.positions(ir)
    ripped: set[int] = set()
    kept_rows = fixed_rows

    def _placement_gate(
        copper: list[dict[str, Any]] | None = None,
        ripped_ids: set[int] | None = None,
    ) -> tuple[int, str | None] | tuple[None, None]:
        """``(n_errors, rendered report)`` over the CURRENT ``ir``, against
        the fixed copper the write would keep (``kept_rows``);
        ``(None, None)`` when the gate cannot run. ``copper``/``ripped_ids``
        override the commit's ``kept_rows``/``ripped`` for the topo
        candidate gate, which must judge a pose by the copper ITS OWN
        rip-up would leave (gr540612)."""
        if gate_board_id is None or capability is None:
            return None, None
        kept = kept_rows if copper is None else copper
        gone = ripped if ripped_ids is None else ripped_ids
        try:
            placement_findings = pcb_session.placement_drc_findings(
                ir,
                capability=capability,
                footprints=footprints,
                fixed_copper=kept,
                outline=pcb_session.outline_from_features(features),
            )
            # gr345859 §F — violated `strength='invariant'` measures join
            # the report (the route job's copy of this gate is the one that
            # refuses on them; here they count like any other error).
            layers = [str(layer.get("name")) for layer in ir.stackup]
            placement_findings += pcb_session.invariant_measure_findings(
                graph=graph,
                measures=design.measures,
                features=features,
                generators_rows=ctx.store.pcb_generators_for(pcb_ref_id),
                pads=pcb_realize.pads_for_ir(ir, layers, footprints),
                copper=[
                    {**row, "net": row.get("net") or ""}
                    if row.get("fixed")
                    else {**row, "derived": True}
                    for row in ctx.store.pcb_copper_list(int(gate_board_id))
                    if row.get("fixed_id") not in gone
                ],
                layers=layers,
            )
            return pcb_session.placement_drc_report(
                placement_findings,
                fixed_refdes=[
                    str(ir.instance_refdes[i])
                    for i in range(ir.n_instances)
                    if bool(ir.inst_fixed_xy[i])
                ],
            )
        except Exception:
            log.exception("pcb_place: pre-route DRC gate raised; placement kept")
            return None, None

    errors_before, _report_before = (
        _placement_gate() if fully_placed_before else (None, None)
    )
    side_changes: dict[str, str] = {}
    label_spots: dict[str, str] = {}
    topo_files: dict[str, Any] | None = None
    topo_text = ""
    topo_diverged = ""
    topo_result: Any = None
    result: Any = None
    if placer == "topo":
        ref = ctx.store.get_ref(kind="pcb", id=pcb_ref_id)
        # The stored board's sides, snapped before the first candidate
        # eval mutates ``ir`` — a candidate's side flips count toward
        # its moved set like the commit path's ``side_changes``.
        bottom_before = np.asarray(ir.inst_bottom, dtype=bool).copy()

        def _gate_topo_pose(cand: Any) -> int | None:
            """The placement-gate error count for one topo candidate
            pose — applies it to ``ir`` and re-runs the gate the commit
            faces, INCLUDING the fixed-copper rip-up: the real gate
            below runs after moved parts shed their branches, so a
            candidate judged on the full rows reads pad-on-copper shorts
            its own commit clears, and a good anneal silently fell back
            to the stored pose on imported boards (gr540612). The
            rip-up is only COMPUTED here — the DB retire still happens
            once, in the commit transaction."""
            _ir_pose(ir, cand)
            if not branches:
                n, _report = _placement_gate()
                return n
            moved = {
                r
                for r, p in pcb_session.positions(ir).items()
                if pcb_session.pose_moved(pose_before.get(r), p)
            } | {
                str(ir.instance_refdes[i])
                for i in range(ir.n_instances)
                if bool(ir.inst_bottom[i]) != bool(bottom_before[i])
            }
            cand_ripped = pcb_session.fixed_copper_ripup(
                ir,
                footprints or {},
                fixed_rows,
                branches,
                moved,
                capability,
                shorted_before,
            )
            n, _report = _placement_gate(
                copper=[r for r in fixed_rows if r["fixed_id"] not in cand_ripped],
                ripped_ids=cand_ripped,
            )
            return n

        topo_out = _topo_run(
            ctx,
            ir,
            graph,
            footprints,
            effort=iters,
            seed=seed,
            slug=(ref.slug if ref is not None and ref.slug else str(pcb_ref_id)),
            features=features,
            fixed_copper=design.fixed_copper,
            preset=preset,
            capability=capability,
            hole_margin_mm=hole_fab_clearance_mm(floor_capability),
            # ``force=True`` is the explicit "commit the anneal's pose
            # anyway" escape — the elite fallback must not preempt it.
            gate=_gate_topo_pose if not force else None,
            errors_before=errors_before,
        )
        if topo_out is None:  # PinConflict already recorded
            return
        (
            topo_result,
            topo_files,
            topo_text,
            side_changes,
            label_spots,
            topo_diverged,
        ) = topo_out
    else:
        result = optimize(ir, config)
    pose = pcb_session.positions(ir)
    if branches:
        moved_parts = {
            r for r, p in pose.items() if pcb_session.pose_moved(pose_before.get(r), p)
        } | set(side_changes)
        ripped = pcb_session.fixed_copper_ripup(
            ir,
            footprints or {},
            fixed_rows,
            branches,
            moved_parts,
            capability,
            shorted_before,
        )
        kept_rows = [r for r in fixed_rows if r["fixed_id"] not in ripped]
    n_placement_errors, placement_report = _placement_gate()
    worse = (
        errors_before is not None
        and n_placement_errors is not None
        and n_placement_errors > errors_before
    )
    if worse and not force:
        verdict = (
            "refusing to commit — the new placement has "
            f"{n_placement_errors} pre-route DRC error(s), the stored one has "
            f"{errors_before}; nothing written.\n{placement_report}\n"
            "Pin the parts that must not move (fixed='both', or keep the "
            "source placement of an imported board), try another seed, or "
            "pass force=True to write it anyway."
        )
        if topo_files is not None:
            # The run happened and its files are computed even though the
            # pose was refused — stamp them so view='topo' and bench
            # metrics can read a refused run (gr515107 loop).
            ctx.set_meta(topo_run={"files": topo_files})
        # A refused run is recorded too (evidence of what did NOT work —
        # S1): the resubmit on this exact input replays the refusal
        # instead of paying for the same anneal.
        ctx.set_meta(
            run_record=pcb_run_record.refused_record(
                op="place",
                placer=placer or "anneal",
                params=params,
                input_digest=input_digest,
                footprint_digest=pcb_run_record.footprint_fingerprint(design.graph),
                verdict=verdict,
                failure_class="non-convergence",
            )
        )
        ctx.record_failure(
            f"pcb_place: {verdict}",
            failure_class="non-convergence",
        )
        return
    if worse and force:
        placement_report = (
            f"pre-route DRC gate OVERRIDDEN (force=True): wrote a placement "
            f"with {n_placement_errors} error(s) over a stored one with "
            f"{errors_before}\n{placement_report}"
        )

    # One transaction: a pose write without its rip-up is the 172-short
    # board this exists to prevent. ``pcb_set_pose`` writes x/y/rot only —
    # a topo run's side flips ride ``pcb_move_instance(layer=)`` in the
    # same transaction.
    last_place = (
        {
            "placer": "topo",
            "evals": topo_result.evals,
            "crossings": topo_result.crossings,
            "cost_after": round(topo_result.best_cost, 4),
        }
        if placer == "topo"
        else {
            "iters": result.iters,
            "cost_before": round(result.cost_before, 4),
            "cost_after": round(result.cost_after, 4),
        }
    )
    with ctx.store.tx() as conn:
        moved = ctx.store.pcb_set_pose(
            pcb_ref_id,
            pose,
            meta={"last_place": last_place},
            conn=conn,
        )
        for refdes, layer in side_changes.items():
            ctx.store.pcb_move_instance(pcb_ref_id, refdes, layer=layer, conn=conn)
        # The placer's label pins ride the same transaction: a pose write
        # without its pins would leave the silk pass honoring spots the
        # new placement never preserved (or stale pins from the previous
        # placement parked on a moved part). Clear every instance's pin,
        # then write the fresh set — meta=None deletes the key
        # (pcb_move_instance's documented merge).
        for inst in graph.get("instances") or []:
            refdes = str(inst.get("refdes") or "")
            if refdes:
                ctx.store.pcb_move_instance(
                    pcb_ref_id, refdes, meta={"label_spot": None}, conn=conn
                )
        for refdes, spot in label_spots.items():
            ctx.store.pcb_move_instance(
                pcb_ref_id, refdes, meta={"label_spot": spot}, conn=conn
            )
        n_ripped = (
            ctx.store.pcb_fixed_copper_retire_ids(
                int(gate_board_id), sorted(ripped), conn=conn
            )
            if ripped and gate_board_id is not None
            else 0
        )

    # D1 (design-rerun-pull S1): the committed run's replayable record.
    # A topo run submitted with route=True records ONLY once its route
    # stage has succeeded — that stamp lands inside the topo block below
    # so a route failure is not a replayable hit (gr512900 fix 4). Every
    # other run has no route stage at all: record it now, flagged
    # placement-only, so replay can say "placement replayed; route not
    # replayed".
    if not (placer == "topo" and route):
        _stamp_committed_record(
            ctx,
            pcb_ref_id=pcb_ref_id,
            placer=placer or "anneal",
            params=params,
            input_digest=input_digest,
            ir=ir,
            scores=last_place,
            ripped=ripped,
            elites=(
                {"topo_run": "elites.json"}
                if placer == "topo" and topo_files is not None
                else None
            ),
            placement_only=True,
        )

    # The pre-route DRC report (gr451052: nothing used to run a geometric
    # check between placement and routing, so an illegal placement was
    # carried silently into the route job) now comes from the gate above,
    # measured on the settled positions before they were written; it is
    # named here in the job summary — the same NAMED, up-front flag the
    # route job raises (`pcb_session.placement_drc_findings`/
    # `placement_drc_report`, one definition, so the two jobs cannot drift).
    placement_summary = f"{placement_report}\n\n" if placement_report else ""
    if placer == "topo":
        # Slice-5 ruling: place and route are ONE job — but routing is
        # opt-in (route=True, GRIPE_FIX ruling). The pose is committed
        # above; when requested, route it now. Best-effort in the sense
        # that the committed pose stands either way, but a raise fails
        # the job LOUDLY (S1 ruling) — never "succeeded, 0 copper".
        route_text = ""
        route_meta: dict[str, Any] = {}
        route_error = ""
        if route:
            try:
                route_text, route_meta = _topo_route(
                    ctx,
                    ir,
                    graph,
                    footprints,
                    features=features,
                    fixed_copper=kept_rows,
                    capability=capability,
                    board_id=gate_board_id,
                    pcb_ref_id=pcb_ref_id,
                    label_spots=label_spots,
                    out_files=topo_files,
                )
            except Exception as exc:
                log.exception("pcb_place: topo route stage raised; placement kept")
                route_error = f"{type(exc).__name__}: {exc}"
                route_text = (
                    f"topo-route: raised {route_error} — the "
                    "placement above stands, no copper written\n"
                )
                route_meta = {"error": route_error}
                # a corridor overflow carries its cut count so the
                # bench's corridor column still reads a number (S6)
                from precis.pcb.topoplace.wires import CorridorOverflow

                if isinstance(exc, CorridorOverflow):
                    route_meta["corridor_overflow"] = exc.count
        # D1 (gr512900 fix 4): a route=True run is recorded only once its
        # route stage SUCCEEDED. The placement committed above still
        # stands and a raised route still fails the job below — but the
        # run is no replayable hit: a resubmit mints a fresh job rather
        # than replaying a placement the route half never proved.
        if route and not route_error:
            _stamp_committed_record(
                ctx,
                pcb_ref_id=pcb_ref_id,
                placer=placer or "anneal",
                params=params,
                input_digest=input_digest,
                ir=ir,
                scores=last_place,
                ripped=ripped,
                elites=(
                    {"topo_run": "elites.json"} if topo_files is not None else None
                ),
                placement_only=False,
            )
        # Decision 20: the whole run payload rides the job ref's meta for
        # ``view='topo'`` drill-down; the summary text is the same file.
        # ``route`` stays absent when the stage never ran.
        topo_run_meta: dict[str, Any] = {"files": topo_files}
        if route:
            topo_run_meta["route"] = route_meta
        ctx.set_meta(topo_run=topo_run_meta)
        flips = (
            "side flips: "
            + ", ".join(f"{r}->{s}" for r, s in sorted(side_changes.items()))
            + "\n\n"
            if side_changes
            else ""
        )
        ctx.append_chunk(
            "job_summary",
            f"{placement_summary}{topo_diverged}"
            f"topo-place: wrote {moved} instance(s) "
            f"(+{len(side_changes)} side flip(s)), "
            f"{topo_result.crossings} proxy crossing(s), airwire "
            f"{topo_result.length:.0f} mm, {topo_result.evals} evals, "
            f"{n_placement_errors or 0} pre-route DRC error(s)"
            + (
                f" (stored placement had {errors_before})"
                if errors_before is not None
                else ""
            )
            + "\nrun files: this job's meta.topo_run "
            "(get(kind='pcb', id=<slug>, view='topo'))\n\n"
            + pcb_session.ripup_note(n_ripped, fixed_rows, ripped)
            + flips
            + route_text
            + "\n"
            + topo_text,
        )
        if route_error:
            # AFTER the summary chunk: the failure event sits on top of
            # the placement report, so the job fails with the placement
            # described first — never "succeeded, 0 copper".
            ctx.record_failure(
                f"pcb_place: topo route stage raised {route_error}",
                failure_class="non-convergence",
            )
        return

    # A hard align the optimizer's snap pass closed is flagged on its row
    # (`meta.snapped`, shown as `detail` by view='measures'); cleared for
    # every align the snap did not close this run.
    if any(spec.metric == "align" for spec in measures):
        ctx.store.pcb_measures_mark_snapped(pcb_ref_id, result.snapped)

    # Preferred-number snap (op='place' snap=true): the pose write above
    # already persisted the snapped positions; this stamps WHY they moved
    # on each instance's meta so a later `view`/`get` can tell a snap from
    # an anneal landing. The stamp records the snapped pose — clearing it
    # is the author's call (`op='move'`), never this job's.
    for refdes, stamp in result.preferred_snapped.items():
        ctx.store.pcb_move_instance(pcb_ref_id, refdes, meta={"preferred_snap": stamp})

    ctx.append_chunk(
        "job_summary",
        f"{placement_summary}"
        f"placed {moved} instance(s), {result.iters} iters — "
        f"cost {result.cost_before:.4f} -> {result.cost_after:.4f}, "
        f"{n_placement_errors or 0} pre-route DRC error(s)"
        + (
            f" (stored placement had {errors_before})"
            if errors_before is not None
            else ""
        )
        + "\n\n"
        + pcb_session.ripup_note(n_ripped, fixed_rows, ripped)
        + _legalized_note(result.legalized)
        + _soft_pin_note(result.soft_pin_travel_mm)
        + _support_groups_note(result.support_groups)
        + _preferred_snap_note(result.preferred_snapped)
        + _on_fixed_vias_note(result.on_fixed_vias)
        + digest_toon(result),
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("pcb_place runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="pcb_place",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]

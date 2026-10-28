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
plane/pin-swap moves) — those are ``op='route'``'s job
(:mod:`precis.workers.job_types.pcb_route`), which also re-places jointly
with the sketch. Positions land via
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
) -> tuple[Any, dict[str, Any], str, dict[str, str]] | None:
    """The ``placer='topo'`` run (topo-place spec §1c, Decision 21):
    ``design_from_ir`` → the proxy placer → ``assign_pins`` → the run
    files, with the winning pose folded back into ``ir`` so the shared
    gate/ripup/``pcb_set_pose`` tail below measures and writes it like
    an anneal result. Returns ``(result, files, summary, side_changes)``
    — ``None`` after recording an unclassed ``PinConflict`` failure (the
    board script's error, not the job's). Lazy imports: the kernel pulls
    scipy, which this worker otherwise never loads."""
    from precis.pcb import topoplace as _tp
    from precis.pcb.topoplace import runstore as _runstore
    from precis.pcb.topoplace import summary as _tpsummary
    from precis_geom.pcb import PinConflict

    try:
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
        )
        _tp.check_slots(td)
        # gr520546: authored near()/separation/align measures steer the
        # anneal — the share is priced in only when the design carries
        # resolvable measures (an empty tuple would zero-divide its
        # reference magnitude to the 1.0 floor anyway, but the share
        # staying 0 keeps every measureless run's cost byte-identical).
        objective = (
            dataclasses.replace(_tp.PRESETS["compact"], measures=0.2)
            if td.measures
            else _tp.PRESETS["compact"]
        )
        result = _tp.topo_place(td, objective=objective, effort=effort, seed=seed)
        assignment = _tp.assign_pins(td, result.best_pose)
    except PinConflict as exc:
        ctx.record_failure(str(exc))
        return None
    pm = _tp.pair_map(td)

    bottom_before = np.asarray(ir.inst_bottom, dtype=bool).copy()
    for i in range(len(td.parts)):
        x, y, rot_q, side = (float(v) for v in result.best_pose[i])
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        ir.inst_x[i] = x
        ir.inst_y[i] = y
        ir.inst_rot[i] = rot_q * 90.0
        ir.inst_bottom[i] = side >= 0.5
    side_changes = {
        str(ir.instance_refdes[i]): ("bottom" if ir.inst_bottom[i] else "top")
        for i in range(len(td.parts))
        if bool(ir.inst_bottom[i]) != bool(bottom_before[i])
    }

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
    return result, files, summary_text, side_changes


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
) -> tuple[str, dict[str, Any]]:
    """The topo route half of a ``placer='topo'`` run (slice-5 ruling:
    place and route are ONE job — the pose commits above, then the same
    pose is routed and its copper + ``pcb_routes`` rows written). ``ir``
    already carries the placed pose (``_topo_run`` mutated it), so
    ``route_design``'s ``d.pose0`` default IS the routed pose.

    BEST-EFFORT: never fails the place. No board row or no fab
    capability row skips with a note; a raise is caught by the caller
    and lands as a summary line, because a placed-but-unrouted board is
    strictly better than a failed job that wrote nothing. Returns
    ``(summary_text, meta)`` — ``meta`` lands under
    ``meta.topo_run.route``."""
    from precis.pcb import DANGLING_NET_NOTE_PREFIX
    from precis.pcb import drc as pcb_drc
    from precis.pcb import ratsnest as pcb_ratsnest
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
    )
    layers = [str(layer.get("name")) for layer in ir.stackup]
    outline = pcb_session.outline_from_features(features or [])
    keepouts = pcb_session.keepouts_from_features(features or [])
    loaded = ctx.store.pcb_load(pcb_ref_id)
    net_rules = pcb_session.route_net_rules(loaded, capability)
    plan, rres = _tprealise.route_design(td_route, ir, net_rules=net_rules)

    # The widen oracle's own map — the FULL clearance resolution (per-net
    # class floors included), not the route map's signal-width variant.
    drc_net_rules = pcb_session.clearance_net_rules(loaded, capability)

    def _drc_errors(result: Any) -> int:
        # The widen stage's clearance oracle — the SAME model + rules the
        # view='drc' gate runs (pads + emitted copper + keepouts + per-net
        # clearance), so "clearance allows" is exactly what the gate
        # accepts — a class ``min_clearance_mm`` is an error tier the fab
        # floor alone would let a widened rail violate.
        model = pcb_session._copper_model(
            ir,
            pcb_realize.result_copper_rows(result, ir, layers, derived=True),
            footprints=footprints,
        )
        return sum(
            1
            for f in pcb_drc.run_geometric_drc(
                model,
                capability=capability,
                outline=outline,
                keepouts=keepouts,
                net_rules=drc_net_rules,
            )
            if f.severity == "error"
        )

    power_ids = {
        net_id
        for net_id in range(ir.n_nets)
        if str(ir.net_class[net_id] or "").strip().lower() in pcb_ratsnest.PLANE_CLASSES
        or str(ir.net_domain[net_id] or "") == "power"
        or (
            not math.isnan(float(ir.net_current_a[net_id]))
            and float(ir.net_current_a[net_id]) > 0.0
        )
        or int(ir.net_plane_layers[net_id]) != 0
    }
    rres, widths = _tprealise.widen_power(
        rres, sorted(power_ids), drc_errors=_drc_errors
    )
    rows = _tprealise.sketch_rows(plan, td_route, ir, layers)

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
    # ONE transaction: a raise between the copper replace and the route
    # rows must never strand copper without route status. And an empty
    # route result never wipes stored copper — both writes are skipped
    # together, so view='route-status' still matches the copper that
    # stays on the board.
    with ctx.store.tx() as conn:
        n_copper = (
            ctx.store.pcb_copper_replace(int(board_id), copper_rows, conn=conn)
            if copper_rows
            else 0
        )
        n_routes = (
            ctx.store.pcb_routes_write(pcb_ref_id, int(board_id), rows, conn=conn)
            if copper_rows
            else 0
        )

    n_realized = sum(1 for r in rows.values() if r["status"] == "realized")
    n_dangling = sum(
        1
        for r in rows.values()
        if (r.get("note") or "").startswith(DANGLING_NET_NOTE_PREFIX)
    )
    n_failed = sum(1 for r in rows.values() if r["status"] == "failed")
    meta = {
        "nets": len(rows),
        "realized": n_realized,
        "failed": n_failed,
        "tracks": len(rres.tracks),
        "vias": len(rres.vias),
        "crossings": len(plan.crossings),
        "unreachable": len(plan.unreachable),
        # the corridor column is the REFUSAL count — a route that ran
        # had no over-capacity cut (a nonzero record raises instead)
        "corridor_overflow": 0,
        "widened": {str(k): v for k, v in widths.items()},
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
        f"copper row(s), {n_routes} route row(s){widened_line}"
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

    # The effective netlist (Slice 7): the latest resolved snapshot over
    # the authored graph, so placement's ratsnest is built against the
    # SAME netlist pcb_route realizes copper for. Read-only: only the
    # route job resolves and writes a snapshot.
    # One read of the graph and every surface the idem key hashes
    # (`load_snapshot`, the same call `_enqueue_op` digests); the resolved
    # snapshot is job-written, not hashed, so it stays a separate read.
    design = pcb_session_store.load_snapshot(ctx.store, pcb_ref_id)
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

    def _placement_gate() -> tuple[int, str | None] | tuple[None, None]:
        """``(n_errors, rendered report)`` over the CURRENT ``ir``, against
        the fixed copper the write would keep (``kept_rows``);
        ``(None, None)`` when the gate cannot run."""
        if gate_board_id is None or capability is None:
            return None, None
        try:
            placement_findings = pcb_session.placement_drc_findings(
                ir,
                capability=capability,
                footprints=footprints,
                fixed_copper=kept_rows,
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
                    if row.get("fixed_id") not in ripped
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
    topo_files: dict[str, Any] | None = None
    topo_text = ""
    topo_result: Any = None
    result: Any = None
    if placer == "topo":
        ref = ctx.store.get_ref(kind="pcb", id=pcb_ref_id)
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
        )
        if topo_out is None:  # PinConflict already recorded
            return
        topo_result, topo_files, topo_text, side_changes = topo_out
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
            f"{placement_summary}"
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

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

import logging
import math
from typing import TYPE_CHECKING, Any

from precis.pcb import realize as pcb_realize
from precis.pcb import session as pcb_session
from precis.pcb.capabilities import capability_for
from precis.pcb.optimize import MoveKind as _MK
from precis.pcb.optimize import (
    OptimizeConfig,
    ScheduleStage,
    digest_toon,
    optimize,
    resolve_measures,
)
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
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

_DEFAULT_ITERS = 2000


def _process_for_stackup(stackup: list[dict[str, Any]]) -> str:
    """The capability-table process row for a board's layer COUNT —
    duplicated from :func:`precis.pcb.drc.process_for_stackup` for the same
    reason :mod:`precis.workers.job_types.pcb_route` duplicates it: a 4-line
    lookup should not force this worker to import shapely at module load.
    v1 checks 2- or 4-layer boards only, same limit as the DRC engine."""
    n = len(stackup)
    if n == 2:
        return "2layer"
    if n == 4:
        return "4layer"
    raise ValueError(
        f"no fab capability row for a {n}-layer stackup — v1 checks 2- or "
        "4-layer boards only"
    )


#: Single-stage, placement-only schedule — the whole run stays in
#: TRANSLATE/ROTATE/SWAP territory (mirrors DEFAULT_SCHEDULE's first
#: stage, just extended to through_fraction=1.0 instead of handing off to
#: topology/layer moves, which is ``pcb_route``'s job).
_PLACE_ONLY_SCHEDULE = (
    ScheduleStage(1.0, {_MK.TRANSLATE: 0.6, _MK.ROTATE: 0.15, _MK.SWAP: 0.25}),
)


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


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    pcb_ref_id = int(params["pcb_ref_id"])
    iters = int(params.get("iters") or _DEFAULT_ITERS)
    seed = int(params.get("seed") or 0)
    snap = bool(params.get("snap") or False)
    groups = bool(params.get("groups") or False)

    graph = ctx.store.pcb_graph(pcb_ref_id)
    if not graph.get("instances"):
        ctx.record_failure(
            f"pcb_place: design (ref_id={pcb_ref_id}) has no instances to place",
            failure_class="infra",
        )
        return

    features = ctx.store.pcb_features_list(pcb_ref_id)
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
        fixed_copper=(
            ctx.store.pcb_fixed_copper_list(int(board_id))
            if board_id is not None
            else None
        ),
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
    net_name_to_id = {str(ir.net_name[n]): n for n in range(ir.n_nets)}
    layer_name_to_idx = {
        str(layer.get("name")): i for i, layer in enumerate(ir.stackup)
    }
    for plane in ctx.store.pcb_planes_list(pcb_ref_id):
        net_id = net_name_to_id.get(plane["net"])
        layer_idx = layer_name_to_idx.get(plane["layer"])
        if net_id is not None and layer_idx is not None:
            ir.promote_plane(net_id, layer_idx)

    # Same discipline for a persisted pin swap (docs/backlog/
    # pcb-engine-plan.md "PIN_SWAP is not persisted"): PcbIR.swap_pins() is
    # L0 state the IR never persists on its own, so placement's ratsnest
    # must be built against the SAME netlist pcb_route will later realize
    # copper for -- otherwise the two jobs would optimize/route two
    # different boards. Read-only here too: `_PLACE_ONLY_SCHEDULE` has no
    # PIN_SWAP weight, so this job can never propose a new swap, and it
    # never calls pcb_pin_swaps_replace_derived -- only the job that can
    # change a thing may persist it.
    ir_pin_swaps = ctx.store.pcb_pin_swaps_list(pcb_ref_id)
    pcb_session.apply_pin_swap_overrides(ir, ir_pin_swaps)

    # Authored proximity/separation intent (put(args={'measures':[...]})),
    # resolved to the pair-distance cost terms `optimize.py`'s anneal can
    # actually enforce (precis-measures-help) — previously measured but
    # never enforced by this job (only the legacy `place.autoplace` quick
    # placer honoured them). `resolve_measures` silently drops anything
    # this engine can't express as a two-instance distance bound (a
    # `height`/connectivity metric, a role-based operand, a `gauge`
    # strength) — those stay eyes.py-evaluated only, same as before.
    measures = resolve_measures(
        ctx.store.pcb_measures_list(pcb_ref_id), features=features
    )

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
                capability = capability_for(_process_for_stackup(ir.stackup))
            except ValueError:
                capability = None
        if gate_board_id is not None and capability is not None:
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

    def _placement_gate() -> tuple[int, str | None] | tuple[None, None]:
        """``(n_errors, rendered report)`` over the CURRENT ``ir``;
        ``(None, None)`` when the gate cannot run."""
        if gate_board_id is None or capability is None:
            return None, None
        try:
            placement_findings = pcb_session.placement_drc_findings(
                ir,
                capability=capability,
                footprints=footprints,
                fixed_copper=ctx.store.pcb_fixed_copper_list(int(gate_board_id)),
                outline=pcb_session.outline_from_features(features),
            )
            # gr345859 §F — violated `strength='invariant'` measures join
            # the report (the route job's copy of this gate is the one that
            # refuses on them; here they count like any other error).
            layers = [str(layer.get("name")) for layer in ir.stackup]
            placement_findings += pcb_session.invariant_measure_findings(
                graph=graph,
                measures=ctx.store.pcb_measures_list(pcb_ref_id),
                features=features,
                generators_rows=ctx.store.pcb_generators_for(pcb_ref_id),
                pads=pcb_realize.pads_for_ir(ir, layers, footprints),
                copper=[
                    {**row, "net": row.get("net") or ""}
                    if row.get("fixed")
                    else {**row, "derived": True}
                    for row in ctx.store.pcb_copper_list(int(gate_board_id))
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
    result = optimize(ir, config)
    n_placement_errors, placement_report = _placement_gate()
    worse = (
        errors_before is not None
        and n_placement_errors is not None
        and n_placement_errors > errors_before
    )
    if worse and not force:
        ctx.record_failure(
            "pcb_place: refusing to commit — the annealed placement has "
            f"{n_placement_errors} pre-route DRC error(s), the stored one has "
            f"{errors_before}; nothing written.\n{placement_report}\n"
            "Pin the parts that must not move (fixed='both', or keep the "
            "source placement of an imported board), try another seed, or "
            "pass force=True to write it anyway.",
            failure_class="non-convergence",
        )
        return
    if worse and force:
        placement_report = (
            f"pre-route DRC gate OVERRIDDEN (force=True): wrote a placement "
            f"with {n_placement_errors} error(s) over a stored one with "
            f"{errors_before}\n{placement_report}"
        )

    pose = pcb_session.positions(ir)
    moved = ctx.store.pcb_set_pose(
        pcb_ref_id,
        pose,
        meta={
            "last_place": {
                "iters": result.iters,
                "cost_before": round(result.cost_before, 4),
                "cost_after": round(result.cost_after, 4),
            }
        },
    )

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

    # The pre-route DRC report (gr451052: nothing used to run a geometric
    # check between placement and routing, so an illegal placement was
    # carried silently into the route job) now comes from the gate above,
    # measured on the settled positions before they were written; it is
    # named here in the job summary — the same NAMED, up-front flag the
    # route job raises (`pcb_session.placement_drc_findings`/
    # `placement_drc_report`, one definition, so the two jobs cannot drift).
    placement_summary = f"{placement_report}\n\n" if placement_report else ""
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

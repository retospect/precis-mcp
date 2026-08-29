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
from typing import TYPE_CHECKING, Any

from precis.pcb import session as pcb_session
from precis.pcb.optimize import MoveKind as _MK
from precis.pcb.optimize import OptimizeConfig, ScheduleStage, digest_toon, optimize
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

#: Single-stage, placement-only schedule — the whole run stays in
#: TRANSLATE/ROTATE/SWAP territory (mirrors DEFAULT_SCHEDULE's first
#: stage, just extended to through_fraction=1.0 instead of handing off to
#: topology/layer moves, which is ``pcb_route``'s job).
_PLACE_ONLY_SCHEDULE = (
    ScheduleStage(1.0, {_MK.TRANSLATE: 0.6, _MK.ROTATE: 0.15, _MK.SWAP: 0.25}),
)


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    pcb_ref_id = int(params["pcb_ref_id"])
    iters = int(params.get("iters") or _DEFAULT_ITERS)
    seed = int(params.get("seed") or 0)

    graph = ctx.store.pcb_graph(pcb_ref_id)
    if not graph.get("instances"):
        ctx.record_failure(
            f"pcb_place: design (ref_id={pcb_ref_id}) has no instances to place",
            failure_class="infra",
        )
        return

    outline = pcb_session.outline_from_features(ctx.store.pcb_features_list(pcb_ref_id))
    ir = pcb_session.build_ir(graph, outline=outline)

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

    config = OptimizeConfig(iters=iters, seed=seed, schedule=_PLACE_ONLY_SCHEDULE)
    result = optimize(ir, config)

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
    ctx.append_chunk(
        "job_summary",
        f"placed {moved} instance(s), {result.iters} iters — "
        f"cost {result.cost_before:.4f} -> {result.cost_after:.4f}\n\n"
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

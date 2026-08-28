"""``pcb_route`` job_type — the enqueued, out-of-line half of ``op='route'``
(docs/backlog/pcb-guided-place-route.md, "Tool surface" + Slice 10).

Runs the JOINT place+sketch annealer (the full move schedule — placement,
layer assignment, side flips, plane role, pin swap), then checkpoints
through the realizer, then persists the settled sketch (``pcb_routes``)
and its derived copper (``pcb_copper``) — the same DELETE+INSERT cascade
discipline ``chunks``/``chunk_embeddings`` already uses. Never runs inline
in an MCP call (the thread-pool-starvation lesson, same as ``pcb_place``).

**A net's ``pcb_routes.status`` only ever reaches ``'realized'`` when it is
ACTUALLY clean** — no residual same-layer crossing on its segments (a
straight-line sweep at L3, :func:`precis.pcb.geom.segments_cross`, mirrors
``ir.same_layer_crossing_count``'s own admissibility direction: zero here
means zero routed crossings) and no over-capacity gap the realizer flagged
it in. Both failure modes name the blocking participants in
``pcb_routes.fail`` (backlog: "fail legibly") rather than just flipping a
bit — this is what lets ``route_complete`` (the gate evaluator) stay a
cheap status read instead of re-deriving any of this itself.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.pcb import realize as pcb_realize
from precis.pcb import session as pcb_session
from precis.pcb.geom import segments_cross
from precis.pcb.ir import UNSET_LAYER, segment_points
from precis.pcb.optimize import OptimizeConfig, digest_toon, optimize
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
    },
    "required": ["pcb_ref_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "Joint place+sketch anneal + realize checkpoint over a pcb design's "
    "current graph — the enqueued half of put(args={'op':'route'})."
)

_DEFAULT_ITERS = 3000

#: Placeholder trace width until net-class rules feed the realizer (a
#: later slice's job, not this one's — see the module docstring's scope).
_DEFAULT_TRACK_WIDTH_MM = 0.25


def _residual_crossings(
    ir: PcbIR, plane_net_ids: set[int]
) -> dict[str, list[dict[str, Any]]]:
    """Straight-line same-layer crossings, by offending net NAME — the
    geometric sweep :mod:`precis.pcb.ir`'s ``same_layer_crossing_count``
    also uses, done here pairwise so each crossing can name its OTHER
    participant (that module only returns a count)."""
    failing: dict[str, list[dict[str, Any]]] = {}
    for layer in pcb_session.signal_layers(ir):
        segs = [
            s
            for s in range(ir.n_segments)
            if int(ir.seg_layer[s]) == layer and int(ir.seg_net[s]) not in plane_net_ids
        ]
        pts = {s: segment_points(ir, s) for s in segs}
        segs = [s for s in segs if pts[s] is not None]
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                sa, sb = segs[i], segs[j]
                na, nb = int(ir.seg_net[sa]), int(ir.seg_net[sb])
                if na == nb:
                    continue
                (a1, a2), (b1, b2) = pts[sa], pts[sb]  # type: ignore[misc]
                if segments_cross(a1, a2, b1, b2):
                    name_a, name_b = str(ir.net_name[na]), str(ir.net_name[nb])
                    failing.setdefault(name_a, []).append(
                        {"kind": "same-layer-crossing", "layer": layer, "with": name_b}
                    )
                    failing.setdefault(name_b, []).append(
                        {"kind": "same-layer-crossing", "layer": layer, "with": name_a}
                    )
    return failing


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    pcb_ref_id = int(params["pcb_ref_id"])
    iters = int(params.get("iters") or _DEFAULT_ITERS)
    seed = int(params.get("seed") or 0)

    graph = ctx.store.pcb_graph(pcb_ref_id)
    if not graph.get("nets"):
        ctx.record_failure(
            f"pcb_route: design (ref_id={pcb_ref_id}) has no nets to route",
            failure_class="infra",
        )
        return
    board = graph.get("board") or {}
    board_id = board.get("board_id")
    if board_id is None:
        ctx.record_failure(
            f"pcb_route: design (ref_id={pcb_ref_id}) has no board",
            failure_class="infra",
        )
        return

    ir = pcb_session.build_ir(graph)
    routes_by_net = ctx.store.pcb_routes_get(pcb_ref_id)
    pcb_session.apply_route_overrides(ir, routes_by_net)

    # Re-apply authored plane assignments (op='plane_net' -> pcb_planes) —
    # promote_plane() is L1 state the IR never persists on its own, so a
    # fresh build must re-derive it every run, same discipline as the
    # topology/layer_assign re-application just above.
    net_name_to_id = {str(ir.net_name[n]): n for n in range(ir.n_nets)}
    layer_name_to_idx = {
        str(layer.get("name")): i for i, layer in enumerate(ir.stackup)
    }
    for plane in ctx.store.pcb_planes_list(pcb_ref_id):
        net_id = net_name_to_id.get(plane["net"])
        layer_idx = layer_name_to_idx.get(plane["layer"])
        if net_id is not None and layer_idx is not None:
            ir.promote_plane(net_id, layer_idx)

    config = OptimizeConfig(iters=iters, seed=seed)
    result = optimize(ir, config)

    pose = pcb_session.positions(ir)
    ctx.store.pcb_set_pose(pcb_ref_id, pose)

    rres = pcb_realize.realize(ir)
    plane_net_ids = {
        n for n in range(ir.n_nets) if int(ir.net_plane_layer[n]) != UNSET_LAYER
    }
    crossing_fail = _residual_crossings(ir, plane_net_ids)
    congestion_fail: dict[str, list[dict[str, Any]]] = {}
    for w in rres.warnings:
        detail = {
            "kind": "gap-capacity",
            "gap_mm": round(w.gap_mm, 4),
            "capacity": w.capacity,
            "usage": w.usage,
            "needed_mm": round(w.needed_mm, 4),
            "message": w.message(),
        }
        for net_name in w.nets:
            congestion_fail.setdefault(net_name, []).append(detail)

    sketch = pcb_session.extract_sketch(ir)
    routed_nets = {int(t.net_id) for t in rres.tracks}
    stackup = ir.stackup
    rows: dict[str, dict[str, Any]] = {}
    n_realized = n_failed = 0
    for net_id in range(ir.n_nets):
        net_name = str(ir.net_name[net_id])
        entry = sketch.get(net_name)
        if entry is None:
            continue  # dangling (<2-member) net — nothing to route, no row written
        problems = crossing_fail.get(net_name, []) + congestion_fail.get(net_name, [])
        if problems:
            status = "failed"
            n_failed += 1
        elif net_id in routed_nets or int(ir.net_plane_layer[net_id]) != UNSET_LAYER:
            status = "realized"
            n_realized += 1
        else:
            status = "sketched"  # topology decided but nothing placed to realize yet
        rows[net_name] = {
            **entry,
            "status": status,
            "fail": {"problems": problems} if problems else None,
        }
    ctx.store.pcb_routes_write(pcb_ref_id, int(board_id), rows)

    # pcb_copper.net_id is a real FK — resolve the IR-local net int back to
    # the DB net_id by name (net names are unique per design, the join key
    # every read/write in this module uses).
    db_net_ids = ctx.store.pcb_net_ids(pcb_ref_id)
    copper_rows: list[dict[str, Any]] = []
    for t in rres.tracks:
        db_id = db_net_ids.get(str(ir.net_name[t.net_id]))
        if db_id is None:
            continue
        layer_idx = t.layer if t.layer != UNSET_LAYER else 0
        layer_name = stackup[layer_idx]["name"] if layer_idx < len(stackup) else "F.Cu"
        copper_rows.append(
            {
                "ctype": "track",
                "layer": layer_name,
                "net_id": db_id,
                "route_id": None,
                "geom": {
                    "segments": list(t.segments),
                    "length_mm": round(t.length_mm, 4),
                    "width_mm": _DEFAULT_TRACK_WIDTH_MM,
                    "is_dogbone": t.is_dogbone,
                },
            }
        )
    ctx.store.pcb_copper_replace(int(board_id), copper_rows)

    ctx.store.pcb_set_pose(
        pcb_ref_id,
        {},  # positions already written above; this call is meta-only
        meta={
            "last_route": {
                "iters": result.iters,
                "realized": n_realized,
                "failed": n_failed,
                "warnings": [w.message() for w in rres.warnings][:20],
            }
        },
    )
    ctx.append_chunk(
        "job_summary",
        f"routed {len(rows)} net(s): {n_realized} realized, "
        f"{n_failed} failed, {len(rres.warnings)} congestion warning(s)\n\n"
        + digest_toon(result),
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("pcb_route runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="pcb_route",
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

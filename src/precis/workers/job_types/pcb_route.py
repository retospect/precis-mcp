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
it in — both PLACEMENT-time chord estimates, so both are applied only to a
net the maze router left without copper; a routed net's copper is the proof
(the gap warning still reaches the job summary). Both failure modes name the
blocking participants in
``pcb_routes.fail`` (backlog: "fail legibly") rather than just flipping a
bit — this is what lets ``route_complete`` (the gate evaluator) stay a
cheap status read instead of re-deriving any of this itself. A dangling
(<2-member) net is the one exception: it has no segments to route at all,
so it is written ``'realized'`` too (vacuously — nothing was left
unrouted), with ``pcb_routes.note`` naming why, rather than left with no
row (which reads as ``'unrouted'`` forever and wedges ``route_complete``).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from precis.pcb import pinswap as pcb_pinswap
from precis.pcb import realize as pcb_realize
from precis.pcb import session as pcb_session
from precis.pcb.capabilities import capability_for
from precis.pcb.geom import segments_cross
from precis.pcb.ir import (
    UNSET_LAYER,
    net_member_counts,
    plane_layers_of,
    segment_points,
)
from precis.pcb.optimize import (
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


def _process_for_stackup(stackup: list[dict[str, Any]]) -> str:
    """The capability-table process row for a board's layer COUNT —
    deliberately mirrors :func:`precis.pcb.drc.process_for_stackup`
    (duplicated, not imported: that module pulls in shapely, a dependency
    this worker has no other reason to load just for a 4-line lookup).
    v1 checks 2- or 4-layer boards only, same limit as the DRC engine's own
    copy."""
    n = len(stackup)
    if n == 2:
        return "2layer"
    if n == 4:
        return "4layer"
    raise ValueError(
        f"no fab capability row for a {n}-layer stackup — v1 checks 2- or 4-layer boards only"
    )


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
                    # `reason` too, not just `kind`: the per-net note builder
                    # only surfaces `reason` values, so kind-only entries used
                    # to fail a net with note=None — a failure with no WHY.
                    failing.setdefault(name_a, []).append(
                        {
                            "kind": "same-layer-crossing",
                            "reason": "same-layer-crossing",
                            "layer": layer,
                            "with": name_b,
                        }
                    )
                    failing.setdefault(name_b, []).append(
                        {
                            "kind": "same-layer-crossing",
                            "reason": "same-layer-crossing",
                            "layer": layer,
                            "with": name_a,
                        }
                    )
    return failing


def _resolve_pin_swap_groups(
    ir: PcbIR,
    graph: dict[str, Any],
    warnings: list[str],
) -> tuple[pcb_pinswap.PinSwapGroup, ...]:
    """The missing feed for ``OptimizeConfig.pin_swap_groups`` (docs/backlog
    /pcb-ewod-multitile.md "Rulings 2026-09-19" item 6): an instance's
    authored ``pin_swap_groups`` (``Store.pcb_graph``'s ``c.meta`` hoist —
    a generator's sink instance, or a hand-authored ``put(kind='pcb')``
    component) resolved into real :class:`precis.pcb.pinswap.PinSwapGroup`
    objects, with real per-pin offsets read off the IR's own placed pins
    (:func:`precis.pcb.pinswap.offsets_from_ir` — pin_map-named, rotated
    and mirrored, which the raw pad list is not). Never invents an admissible set of its
    own — an instance with nothing declared contributes nothing, the same
    "caller supplies, engine consumes" contract :mod:`precis.pcb.pinswap`'s
    module docstring states.

    :meth:`~precis.pcb.ir.PcbIR.swap_pins` requires equal rotation-CSR
    degree between the two pins it swaps (that method's own contract) — a
    declared group is normally homogeneous (every HV507 channel pin
    carries exactly one net, degree 1), but a member that genuinely isn't
    (a mis-declared pin name, or one this design left unconnected) is
    dropped from its OWN group (never the whole group, never the whole
    run) with a note appended to ``warnings`` for the job summary, keyed
    against the group's own majority degree.
    """
    groups: list[pcb_pinswap.PinSwapGroup] = []
    refdes_to_instance = {str(ir.instance_refdes[i]): i for i in range(ir.n_instances)}
    for inst_dict in graph.get("instances") or []:
        declared = inst_dict.get("pin_swap_groups")
        if not declared:
            continue
        refdes = str(inst_dict["refdes"])
        instance = refdes_to_instance.get(refdes)
        if instance is None:
            continue
        pin_by_label: dict[str, int] = {}
        for pin_id in range(ir.n_pins):
            if int(ir.pin_instance[pin_id]) == instance:
                pin_by_label[str(ir.pin_label[pin_id])] = pin_id
        for admissible in declared:
            pins = [pin_by_label[n] for n in admissible if n in pin_by_label]
            if len(pins) < 2:
                continue
            degree = {
                p: int(ir.rotation_index[p + 1]) - int(ir.rotation_index[p])
                for p in pins
            }
            ref_degree = Counter(degree.values()).most_common(1)[0][0]
            kept = tuple(p for p in pins if degree[p] == ref_degree)
            for p in pins:
                if degree[p] != ref_degree:
                    warnings.append(
                        f"pin_swap: {refdes}.{ir.pin_label[p]} dropped from its "
                        f"admissible set (degree {degree[p]} != the group's own "
                        f"{ref_degree})"
                    )
            if len(kept) < 2:
                continue
            # Offsets off the IR's own placed pins, not the raw pad list:
            # pin_map-named, rotated and mirrored (offsets_from_ir's
            # docstring — the pads path scored every real-part swap 0).
            groups.append(
                pcb_pinswap.PinSwapGroup(
                    instance=instance,
                    pins=kept,
                    offsets=pcb_pinswap.offsets_from_ir(ir, instance, kept),
                )
            )
    return tuple(groups)


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

    # The board outline, same as ``pcb_place`` already fetches it. This job
    # used to call ``build_ir(graph)`` with no outline at all, so its anneal
    # AND its realizer both ran against a board they could not see, while
    # ``_render_drc`` checked their output against the real one — 2
    # ``board_edge_clearance`` errors on a board that was otherwise clean,
    # and the place job's outline-aware placement quietly re-annealed away
    # here. One rule, two call sites, drifted: the same shape as every
    # other defect this build has produced.
    features = ctx.store.pcb_features_list(pcb_ref_id)
    ir = pcb_session.build_ir(
        graph,
        outline=pcb_session.outline_from_features(features),
        # Mounting holes ride the same hydration for the same reason as
        # the outline note above: a hole this IR doesn't carry is one the
        # realizer's occupancy grid never claims, and _render_drc then
        # checks the routed copper against holes the router couldn't see
        # (the npth_clearance family, round-3 review item 4).
        mounting_holes=pcb_session.mounting_holes_from_features(features),
        # Real per-pin positions where a footprint is cached/authored
        # (gripe 338983). Without this the maze router is asked to connect
        # SYNTHESIZED land-pattern coordinates while the gerber writer
        # flashes the real ones — copper that lands nowhere near its pad on
        # any part whose label no package family recognizes (every
        # generator-emitted component, e.g. an `ewod_pad_array`'s escape
        # vias).
        footprints_by_lcsc=ctx.store.pcb_footprints_for(pcb_ref_id),
        local_footprints_by_name=ctx.store.pcb_local_footprints_for(pcb_ref_id),
    )
    routes_by_net = ctx.store.pcb_routes_get(pcb_ref_id)
    pcb_session.apply_route_overrides(ir, routes_by_net)

    # ``ir.pin_net`` right after a fresh build IS ``pcb_netconns``'s
    # authored wiring — the baseline every persisted pin-swap override is
    # measured against below (docs/backlog/pcb-engine-plan.md "PIN_SWAP is
    # not persisted"). Captured BEFORE re-applying any existing override
    # (next block), since that re-application is exactly what would make
    # this copy indistinguishable from the post-optimize settled state.
    baseline_pin_net = ir.pin_net.copy()

    # Re-apply BOTH a (currently hypothetical -- no authoring verb exists
    # yet) authored pin-swap override and a prior pcb_route run's derived
    # write-back onto the fresh IR, same discipline as the route-overrides
    # and plane re-application just above/below: PcbIR.swap_pins() is L0
    # state the IR never persists on its own, so a fresh build must
    # re-derive it every run. Skipping this is worse than not persisting
    # at all -- the copper and the netlist would then disagree in the
    # OTHER direction (persisted swap says one thing, the freshly-built,
    # un-swapped IR realizes another).
    pin_swaps = ctx.store.pcb_pin_swaps_list(pcb_ref_id)
    pcb_session.apply_pin_swap_overrides(ir, pin_swaps)
    authored_pin_keys = {
        (row["refdes"], row["pin"])
        for row in pin_swaps
        if row.get("source", "authored") != "derived"
    }

    # Re-apply BOTH authored (op='plane_net') and optimizer-derived
    # (a prior pcb_route run's write-back, gr267526) plane assignments —
    # promote_plane() is L1 state the IR never persists on its own, so a
    # fresh build must re-derive it every run, same discipline as the
    # topology/layer_assign re-application just above. A derived row is a
    # reasonable warm start even though this run may move off it. An
    # AUTHORED row is a CONSTRAINT, not a hint, and must stay set through
    # the anneal (`OptimizeConfig.locked_plane_nets` below), never merely
    # "explored away from" — realization reads the anneal's POST-ANNEAL
    # IR, not the persisted DB row, so an authored net demoted mid-search
    # comes out as a plane in the database and a blank layer on the
    # board. Measured: this cost model has no term that wants a plane at
    # all (79 PLANE_PROMOTE proposals over 3000 iterations, all rejected
    # on cost), so an unlocked authored net's PLANE_DEMOTE is accepted
    # immediately and permanently, every run.
    net_name_to_id = {str(ir.net_name[n]): n for n in range(ir.n_nets)}
    layer_name_to_idx = {
        str(layer.get("name")): i for i, layer in enumerate(ir.stackup)
    }
    authored_net_names: set[str] = set()
    for plane in ctx.store.pcb_planes_list(pcb_ref_id):
        net_id = net_name_to_id.get(plane["net"])
        layer_idx = layer_name_to_idx.get(plane["layer"])
        if net_id is not None and layer_idx is not None:
            ir.promote_plane(net_id, layer_idx)
        if plane.get("source", "authored") != "derived":
            authored_net_names.add(plane["net"])

    # Only AUTHORED nets are locked (see the comment above) -- a derived
    # assignment (this run's own prior write-back) must stay fully
    # explorable, which is the whole distinction `meta.source` exists to
    # carry.
    locked_plane_nets = frozenset(
        net_name_to_id[name] for name in authored_net_names if name in net_name_to_id
    )
    # Same authored-measure enforcement as pcb_place's anneal — without
    # this, the route job's re-place pass would silently undo whatever a
    # proximity/separation measure had just pulled into shape (one rule,
    # one call site short).
    measures = resolve_measures(ctx.store.pcb_measures_list(pcb_ref_id))
    # part_lcsc -> Store.pcb_footprints_for (LCSC-keyed) -> refdes-keyed,
    # via PcbIR.instance_part_lcsc (the join pcb_graph/from_graph now
    # carry). Without it every pad on every routed board reads as a
    # land-pattern BOUND regardless of what is actually cached, and
    # gerber.export_fab therefore refuses EVERY routed board — including
    # one whose parts are all real. The two ends of this path both
    # existed; only the join key was missing, the same shape as the
    # write-only `inst_rot` defect.
    # Local (authored) footprints join the same dict: an instance with no
    # LCSC part at all — every `footprints[]`-authored component and every
    # generator-emitted one (pcb-ewod-multitile Slice 1/2) — otherwise
    # reserves a landpattern-BOUND pad on the occupancy grid instead of its
    # real electrode/via copper, and `export_fab` then refuses the board as
    # synthesized. Same two-source join `handlers/pcb.py::_drc_pads` and
    # `session.build_ir` already do; this call site was one source short.
    #
    # Computed HERE (rather than right before `realize()`, its other
    # consumer) because `_resolve_pin_swap_groups` below needs the exact
    # same cached pad geometry to give PIN_SWAP real footprint offsets —
    # one fetch, two consumers, not a second round-trip.
    footprints = pcb_session.footprints_by_refdes(
        ir,
        ctx.store.pcb_footprints_for(pcb_ref_id),
        local_footprints_by_name=ctx.store.pcb_local_footprints_for(pcb_ref_id),
        local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
    )
    pin_swap_warnings: list[str] = []
    pin_swap_groups = _resolve_pin_swap_groups(ir, graph, pin_swap_warnings)
    # Planar warm start BEFORE the anneal: each group's pins matched to
    # their far ends in cyclic angular order (pinswap.propose_radial_
    # assignment — the closed-form non-crossing assignment for a ring of
    # pads facing a field of vias, which the anneal's pairwise move
    # cannot reach from a crossed start). The anneal only improves on it,
    # and the settled result still goes through the same pin_swap_diff
    # write-back below, so persistence is unchanged.
    for group in pin_swap_groups:
        for pin_a, pin_b in pcb_pinswap.propose_radial_assignment(ir, group) or ():
            ir.swap_pins(pin_a, pin_b)
    config = OptimizeConfig(
        iters=iters,
        seed=seed,
        locked_plane_nets=locked_plane_nets,
        measures=measures,
        pin_swap_groups=pin_swap_groups,
    )
    result = optimize(ir, config)

    # Write back the anneal's settled plane decisions (gr267526: this used
    # to be dropped entirely — PLANE_PROMOTE/DEMOTE moves mutated
    # net_plane_layers freely during search, but nothing ever persisted the
    # result, so a promoted GND/VCC-class net reverted to a full-length
    # trace the moment the job ended). Only nets NOT covered by an
    # authored row are written — an authored net's persisted assignment
    # must stay exactly what the human asked for, never what this run's
    # search happened to leave it at.
    #
    # `pcb_planes_replace_derived`'s ``assignments`` is still ``{net_name:
    # layer_name}`` — ONE layer per net — because a derived (search-only)
    # net structurally never carries more than one bit:
    # `optimize._gen_plane_promote` only ever offers a bare (mask==0),
    # unlocked net a single new layer, and an authored net is excluded
    # from this dict entirely by the `authored_net_names` filter below. So
    # `plane_layers_of` yields at most one entry per net here, and this
    # dict comprehension stays a straight 1:1 mapping without needing a
    # store-side change (multi-layer fill for one net is carried entirely
    # by the AUTHORED path — see `handlers/pcb.py::_op_plane_net`, called
    # once per desired layer, each landing its own `pcb_planes` row).
    derived_assignments = {
        str(ir.net_name[n]): str(ir.stackup[layer]["name"])
        for n in range(ir.n_nets)
        if str(ir.net_name[n]) not in authored_net_names
        for layer in plane_layers_of(int(ir.net_plane_layers[n]))
    }
    ctx.store.pcb_planes_replace_derived(pcb_ref_id, int(board_id), derived_assignments)

    # Write back the anneal's settled pin-swap decisions (pcb-engine-plan
    # "PIN_SWAP is not persisted": previously `ir.swap_pins` genuinely
    # mutated pin->net during search and nothing ever wrote the result
    # back, so the stored netlist and the stored copper would describe two
    # different boards the moment this job ended). Diffed against the
    # RAW netconn baseline captured above (not the warm-started state), so
    # a pin already at its authored net writes nothing; only pins already
    # covered by an authored override are excluded from the write, same
    # "never overwrite an authored row" discipline as the plane path.
    pin_swap_overrides = [
        entry
        for entry in pcb_session.pin_swap_diff(ir, baseline_pin_net)
        if (entry["refdes"], entry["pin"]) not in authored_pin_keys
    ]
    ctx.store.pcb_pin_swaps_replace_derived(
        pcb_ref_id, int(board_id), pin_swap_overrides
    )

    pose = pcb_session.positions(ir)
    ctx.store.pcb_set_pose(pcb_ref_id, pose)

    try:
        fab_caps = capability_for(_process_for_stackup(ir.stackup))
    except ValueError as exc:
        ctx.record_failure(f"pcb_route: {exc}", failure_class="infra")
        return
    realize_config = pcb_realize.RealizeConfig(
        fab_caps=fab_caps, class_rules=graph.get("net_classes")
    )
    # `footprints` (the same refdes-keyed pad geometry PIN_SWAP's feed
    # above also used) was resolved earlier, before the anneal — see that
    # block's own comment for why.
    # Authored fixed copper (pcb-pre-place-route-blocks Slice 1, "Realize
    # seam"): claimed on the occupancy grid as real obstacles for every
    # OTHER net, and short-circuits any ratsnest segment its own two pins
    # already bridge (`RealizeResult.fixed_realized` below) — see
    # `realize()`'s own `fixed_copper` keyword docstring for both halves.
    fixed_copper = ctx.store.pcb_fixed_copper_list(int(board_id))
    rres = pcb_realize.realize(
        ir, config=realize_config, footprints=footprints, fixed_copper=fixed_copper
    )
    fixed_realized_net_ids = {int(ir.seg_net[s]) for s in rres.fixed_realized}
    plane_net_ids = {n for n in range(ir.n_nets) if int(ir.net_plane_layers[n]) != 0}
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

    # The router's own honest residue. `RealizeResult.unrouted` was not read
    # here at all: a maze run could return "I could not route 23 of these"
    # and every one of those nets was still written 'realized', so
    # route_complete read green over a board with holes in it. The realizer
    # reports it, the persister ignored it — one rule, two components, the
    # recurring shape. Worse for a plane-promoted net, which took the
    # unconditional 'realized' branch below whatever happened to it.
    #
    # `RealizeResult.unrouted_reasons` closes the sibling gap: total routing
    # failure used to carry no diagnostic at all (docs/backlog/
    # pcb-engine-plan.md "BOARD TWO" finding 1) -- every current-annotated
    # net failed 100% of the time with nothing distinguishing "the width
    # doesn't fit anywhere" from "lost a corridor race" from "walled in".
    # `reason_by_seg` is index-aligned by seg_id, not by list position (the
    # realizer's own docstring warning), so this is a dict lookup, not a
    # zip.
    reason_by_seg = {r.seg_id: r for r in rres.unrouted_reasons}
    unrouted_fail: dict[str, list[dict[str, Any]]] = {}
    for seg_id in rres.unrouted:
        net_name = str(ir.net_name[int(ir.seg_net[seg_id])])
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        reason = reason_by_seg.get(seg_id)
        unrouted_fail.setdefault(net_name, []).append(
            {
                "kind": "unrouted",
                "reason": reason.kind if reason else "unknown",
                "segment": pcb_session.segment_key(ir, seg_id),
                "message": (
                    reason.message
                    if reason
                    else (
                        f"no route found from pin {a} to pin {b} without crossing "
                        "another net's copper (no diagnostic available)"
                    )
                ),
            }
        )

    # `RealizeResult.unstitched` had NO reader anywhere outside the realizer
    # — the stitching pass computed an honest, specific "I could not bring
    # this plane to one piece, and here is why" and threw it away, leaving
    # only the `connectivity` DRC finding's bare piece count. That is the
    # same one-rule-two-components shape as the `unrouted` gap above, which
    # is exactly why that comment is worth re-reading here: a producer that
    # reports its own residue and a persister that ignores it read green
    # over a real defect. A fragmented plane is a routing failure — the
    # return path the plane exists to provide is not continuous — so it
    # lands in `problems` like any other, with the pass's own message rather
    # than a re-derivation of it.
    #
    # **Only an ELECTRICAL split fails the net.** `UnstitchedNet` counts
    # poured islands, and `bare_fragments` says how many of them hold none
    # of this net's own vias or traces (that field's own docstring has the
    # full distinction). A bare island is floating copper — undesirable,
    # but the net still reaches every pin, and `connectivity` rightly does
    # not report it. Failing the net for one would make a cosmetic pour
    # artefact indistinguishable from a board that genuinely cannot carry
    # its return current, which is the exact conflation this diagnostic
    # exists to avoid. It still gets said, as a note.
    unstitched_fail: dict[str, list[dict[str, Any]]] = {}
    unstitched_note: dict[str, str] = {}
    for u in rres.unstitched:
        if u.fragments - u.bare_fragments > 1:
            unstitched_fail.setdefault(u.net, []).append(
                {
                    "kind": "unstitched-plane",
                    "reason": "unstitched-plane",
                    "fragments": u.fragments,
                    "bare_fragments": u.bare_fragments,
                    "message": u.message,
                }
            )
        else:
            unstitched_note[u.net] = u.message

    sketch = pcb_session.extract_sketch(ir)
    # `fixed_realized_net_ids` folds in beside genuinely-routed nets for
    # BOTH readers below: the chord-crossing sweep (a net whose segments
    # are all realized by fixed copper has real copper, not a placement
    # chord, same as a routed net) and the status ladder (a net entirely
    # bridged by fixed copper needs no derived track to count as done).
    routed_nets = {int(t.net_id) for t in rres.tracks} | fixed_realized_net_ids
    member_counts = net_member_counts(ir)
    stackup = ir.stackup
    rows: dict[str, dict[str, Any]] = {}
    n_realized = n_failed = n_dangling = 0
    for net_id in range(ir.n_nets):
        net_name = str(ir.net_name[net_id])
        entry = sketch.get(net_name)
        if entry is None:
            # A dangling (<2-member) net has no segments, ever — it is
            # legal (test point / NC / mounting-hole net), not a routing
            # failure. Writing an explicit terminal 'realized' row here
            # (rather than leaving no row at all) is the fix for a real
            # bug: a silently-absent row read as 'unrouted' forever
            # (pcb_route_status's own documented default), permanently
            # wedging route_complete for any board with one of these. The
            # `note` names the reason for a later reader of
            # pcb_route_status, since a bare status alone can't.
            if member_counts.get(net_id, 0) < 2:
                rows[net_name] = {
                    "status": "realized",
                    "note": "dangling net (<2 members) — nothing to route",
                }
                n_dangling += 1
            continue
        # The crossing sweep is PLACEMENT-time geometry (segment_points is
        # instance-centroid chords, ir.py's own docstring) — meaningful only
        # for a net with no realized copper. A net the maze router actually
        # routed is crossing-free by construction (copper is claimed on a
        # shared occupancy grid before it is drawn, maze.py's guarantee), so
        # applying the chord check to it reported genuinely-finished nets as
        # "unrouted" — phantom failures that moved with every placement draw
        # (root-caused round 7; the regression test in
        # tests/workers/test_pcb_route.py pins this).
        chord_crossings = (
            [] if net_id in routed_nets else crossing_fail.get(net_name, [])
        )
        # Same rule for the gap-capacity warnings: `_gap_usage` is the
        # SAME placement-time chord geometry (instance-courtyard gaps,
        # side-blind — a bottom-mounted sink directly under a top-mounted
        # array reads as a 0 mm gap that "56 strands want through"), and
        # the maze router's own copper is the proof that the path exists.
        # Before this gate every fabric net on ewod-dogfood-2 that DID
        # route came back 'failed' on that one phantom warning
        # (gr346962's verification run). A net with no copper keeps the
        # warning as its reason; a routed net keeps it only in the job
        # summary's warning list.
        congestion = [] if net_id in routed_nets else congestion_fail.get(net_name, [])
        problems = (
            chord_crossings
            + congestion
            + unrouted_fail.get(net_name, [])
            + unstitched_fail.get(net_name, [])
        )
        note = None
        if problems:
            status = "failed"
            n_failed += 1
            # `_render_drc` (handlers/pcb.py) reads `pcb_routes.note` per NET
            # for its own `unrouted=` finding, one level coarser than the
            # per-segment `reason` above -- a caller who only looks at the
            # DRC view (never `pcb_routes.fail`) still sees WHY, not just
            # THAT, a net failed. An unstitched plane also carries the
            # stitching pass's OWN sentence, not just its kind: unlike a
            # routing failure, where the kind ("walled-in", "lost a race")
            # is the whole diagnosis, "this plane is in 2 pieces" says
            # nothing about which mechanism ran out -- and that message is
            # the only place the distinction survives.
            kinds = sorted({str(p["reason"]) for p in problems if p.get("reason")})
            if kinds:
                note = f"failed: {', '.join(kinds)}"
            said = [
                str(p.get("message", ""))
                for p in problems
                if p.get("reason") == "unstitched-plane"
            ]
            if said:
                note = f"{note or 'failed'} — {' '.join(said)}"
        elif net_id in routed_nets or int(ir.net_plane_layers[net_id]) != 0:
            status = "realized"
            n_realized += 1
        else:
            status = "sketched"  # topology decided but nothing placed to realize yet
        if note is None and status == "realized" and net_id in fixed_realized_net_ids:
            # Only ever set on the CLEAN path (`problems` empty) -- a net
            # that also failed elsewhere already carries its own `note`
            # from the branch above, and this is additive information, not
            # a correction of it.
            note = "realized by fixed copper"
        if note is None and net_name in unstitched_note:
            # Floating poured copper on an otherwise sound net. Deliberately
            # OUTSIDE the status ladder above — it is not a failure and must
            # not become one — but the note is the only place this is ever
            # said, so silence here would lose the single report of it.
            note = f"floating copper — {unstitched_note[net_name]}"
        rows[net_name] = {
            **entry,
            "status": status,
            "fail": {"problems": problems} if problems else None,
            "note": note,
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
                    # RealizedTrack.width_mm is already resolved per net
                    # (current-derived IPC-2221, class-rule override, or
                    # the fab floor) -- see precis.pcb.rules.
                    "width_mm": t.width_mm,
                    "is_dogbone": t.is_dogbone,
                },
            }
        )
    for v in rres.vias:
        db_id = db_net_ids.get(str(ir.net_name[v.net_id]))
        if db_id is None:
            continue
        lo = v.layer_lo if v.layer_lo < len(stackup) else 0
        hi = v.layer_hi if v.layer_hi < len(stackup) else 0
        copper_rows.append(
            {
                # pcb_copper.layer is NOT NULL, but a via's real layer
                # membership is its geom["span"] pair, not this column --
                # a via flashes on every layer it spans, never just one
                # (the exact prior "scalar layer" bug -- see realize.py's
                # RealizedVia docstring). This column holds the span's
                # LOWER layer purely to satisfy the schema; every reader
                # of via copper (precis.pcb.drc, precis.pcb.gerber) reads
                # geom["span"], never this column, for a via item.
                "ctype": "via",
                "layer": stackup[lo]["name"] if lo < len(stackup) else "F.Cu",
                "net_id": db_id,
                "route_id": None,
                "geom": {
                    "x": round(v.x, 4),
                    "y": round(v.y, 4),
                    # RealizedVia.dia_mm/drill_mm are already resolved via
                    # the same precis.pcb.rules resolver tracks use.
                    "dia_mm": v.dia_mm,
                    "drill_mm": v.drill_mm,
                    "span": [
                        stackup[lo]["name"] if lo < len(stackup) else "F.Cu",
                        stackup[hi]["name"] if hi < len(stackup) else "F.Cu",
                    ],
                },
            }
        )
    # Pours persist as copper too. They are the ONLY thing connecting a
    # plane-promoted net, so a route run that writes tracks and vias but
    # drops the pours leaves the DB describing a board whose planes are
    # empty — and view='drc' reads the DB, not the RealizeResult, so it
    # would report every promoted net as disconnected while the in-memory
    # realize was clean. Two representations of one board, drifted.
    for pour in rres.pours:
        db_id = db_net_ids.get(str(pour.get("net", "")))
        if db_id is None:
            continue
        geom = {"polygon": pour["polygon"]}
        if pour.get("holes"):
            geom["holes"] = pour["holes"]
        copper_rows.append(
            {
                "ctype": "pour",
                "layer": str(pour.get("layer", "")),
                "net_id": db_id,
                "route_id": None,
                "geom": geom,
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
                "dangling": n_dangling,
                "vias": len(rres.vias),
                "warnings": [w.message() for w in rres.warnings][:20],
            }
        },
    )
    pin_swap_summary = "\n" + "\n".join(pin_swap_warnings) if pin_swap_warnings else ""
    ctx.append_chunk(
        "job_summary",
        f"routed {len(rows)} net(s): {n_realized} realized, "
        f"{n_failed} failed, {n_dangling} dangling (<2-member, nothing to "
        f"route), {len(rres.vias)} via(s) placed, "
        f"{len(rres.warnings)} congestion warning(s), "
        f"{len(pin_swap_overrides)} pin swap(s) settled"
        f"{pin_swap_summary}\n\n" + digest_toon(result),
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

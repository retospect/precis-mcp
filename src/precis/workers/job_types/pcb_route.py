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
(the gap warning still reaches the job summary) — and its pads, fixed and
routed copper form ONE connected component
(:func:`precis.pcb.session.split_nets`, the same
:func:`precis.pcb.connectivity.net_islands` view='drc''s ``connectivity``
rule runs; gr449483), else it fails ``disconnected``. Every failure mode
names its blocking participants or witnesses in
``pcb_routes.fail`` (backlog: "fail legibly") rather than just flipping a
bit — this is what lets ``route_complete`` (the gate evaluator) stay a
cheap status read instead of re-deriving any of this itself. A dangling
(<2-member) net is the one exception: it has no segments to route at all,
so it is written ``'realized'`` too (vacuously — nothing was left
unrouted), with ``pcb_routes.note`` naming why, rather than left with no
row (which reads as ``'unrouted'`` forever and wedges ``route_complete``).

**A result worse than the incumbent board is never committed**
(gr480010): before the anneal moves anything, the job measures the stored
board's floor — nets not realized, and error-severity findings of the
routed-copper + placement rule sets over the stored copper at stored
poses (:func:`precis.pcb.session.stored_copper_drc_findings` /
:func:`~precis.pcb.session.placement_drc_findings`, one yardstick both
sides). After the post-route gate, an attempt with more unrealized nets
or more DRC errors writes nothing at all — no planes, pin swaps, poses,
statuses or copper — and the job fails ``non-convergence`` carrying the
attempt's summary. An equal or better result commits as usual.

**``loop=N`` (0 off, ≤ :data:`precis.pcb.optimize.LOOP_MAX_ROUNDS`) swaps
the single anneal+realize for** :func:`precis.pcb.optimize.anneal_route_loop`
— the budgeted route → penalise → anneal → route outer loop
(docs/backlog/pcb-anneal-closes-router-loop.md). ``realize()`` runs once
per round behind the same pre-route DRC/invariant gate (a refusal thrown
out of the round callable writes nothing, like the inline refusal), the
loop restores the IR to its best round, and that round's realized result
feeds the same post-route strip and never-worse ratchet. Authored
``meta.expansion_caps`` (``{outline_max_mm: [w, h], max_copper_layers: n,
max_fab_usd: usd}``) enables the priced expansion moves; absent caps
disable them. ``meta.last_route.loop`` records per-round counts and the
conflict digest the loop ends in.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from precis.pcb import DANGLING_NET_NOTE, format_warm_start
from precis.pcb import drc as pcb_drc
from precis.pcb import feedback as pcb_feedback
from precis.pcb import matching as pcb_matching
from precis.pcb import pinswap as pcb_pinswap
from precis.pcb import planes as pcb_planes
from precis.pcb import profiles as pcb_profiles
from precis.pcb import realize as pcb_realize
from precis.pcb import resolved as pcb_resolved
from precis.pcb import session as pcb_session
from precis.pcb.cost import COURTYARD_MIN_SEPARATION_MM, outline_bbox
from precis.pcb.geom import segments_cross
from precis.pcb.ir import (
    COURTYARD_CLEARANCE_MM,
    UNSET_LAYER,
    net_member_counts,
    pin_point,
    plane_layers_of,
    segment_points,
    world_courtyard_polygons,
)
from precis.pcb.optimize import (
    FIELD_POLISH_DEFAULT,
    LOOP_MAX_ROUNDS,
    ExpansionCaps,
    OptimizeConfig,
    RouteLoopConfig,
    RouteOutcome,
    _ir_state_restore,
    _ir_state_snapshot,
    anneal_route_loop,
    conflict_digest_text,
    digest_toon,
    optimize,
    resolve_measures,
)
from precis.pcb.rules import NetRules, resolve_net_rules
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.pcb.ir import PcbIR
    from precis.pcb.optimize import OptimizeResult, RouteLoopResult
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

# Part of the job's dedup key (`_enqueue_op` in handlers/pcb.py): bump it
# whenever a change alters what the route job produces for an unchanged
# board, or a pending/complete job keeps serving the old result.
# 2 = pin swaps restored before the layer sketch (5d50cd44a).
# 3 = finer clearance-derived occupancy grid cap (faithful replay net+1).
# 4 = restore global grid baseline after reference/fab seed regressions.
# 5 = connected-via distance assignment as the route warm start.
# 6 = a split net fails `disconnected` instead of reading `realized`.
# 7 = placement-DRC errors refuse the route before the router runs.
# 8 = a route result worse than the incumbent board (more unrealized nets
#     or more DRC errors) is not committed (gr480010).
# 9 = supply/client matching assigns pins before routing; PIN_SWAP anneal
#     move removed (pcb-lazy-netlist-and-checks Slice 6).
# 10 = stackup plane_net/fill pour without a pcb_planes row, derived power
#      regions, stackup direction/route_cost, bare islands dropped after
#      stitching (pcb-stackup-regions slices 3-4).
# 11 = route clean-up post-pass moves another net's blocking via (gr495397).
# 12 = the grid maze router (and its params) removed; vector is the only router.
CODE_VERSION = 12

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pcb_ref_id": {"type": "integer"},
        "iters": {"type": "integer", "minimum": 1},
        "seed": {"type": "integer"},
        "warm_start": {"type": "string", "enum": ["distance", "radial"]},
        # Which router — accepts only 'vector' (absent = vector); the grid
        # maze router was removed and any other value refuses the job.
        "router": {"type": "string"},
        # Removed grid-router knobs (negotiate/corridor/escape/layer_cost/
        # off_axis/cleanup) stay in the schema — callers may still send
        # them; they are collected into the run's `ignored` list, not
        # refused.
        "negotiate": {"type": "integer", "minimum": 0},
        "corridor": {"type": "number", "minimum": 0},
        "escape": {"type": "string"},
        "layer_cost": {
            "type": "object",
            "additionalProperties": {"type": "number", "minimum": 1},
        },
        "off_axis": {"type": "number", "minimum": 1},
        # The route -> penalise -> anneal -> route outer loop
        # (optimize.anneal_route_loop): 0 = the single anneal+realize
        # (default), 1..LOOP_MAX_ROUNDS = that many budgeted rounds.
        "loop": {"type": "integer", "minimum": 0, "maximum": LOOP_MAX_ROUNDS},
        # Authored ExpansionCaps forwarded verbatim from the design's
        # meta.expansion_caps by the handler (into params so the dedup
        # hash sees them); the job validates the shape itself.
        "expansion_caps": {"type": "object"},
        # Route despite pre-route placement DRC errors (recorded in the summary).
        "force": {"type": "boolean"},
        # Opt-in support-passive rows (optimize.OptimizeConfig.support_groups).
        "groups": {"type": "boolean"},
        # Removed with the grid router (route clean-up, gr495397):
        # accepted and ignored.
        "cleanup": {"type": "boolean"},
        # Route-only (gr495397): false = no anneal, poses and sketch kept.
        "place": {"type": "boolean"},
        # Opt-in field polish (optimize.FIELD_POLISH_DEFAULT, off until
        # the silk gate lands); absent = the config default.
        "field_polish": {"type": "boolean"},
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


def _fixed_copper_collisions(
    ir: PcbIR,
    footprints: dict[str, dict[str, Any]],
    fixed_copper: list[dict[str, Any]],
    fab_caps: Any,
) -> list[str]:
    """Every fab-illegal clearance between AUTHORED fixed copper and a pad
    (or other fixed copper) of another net, as one line each — empty when
    the board is clean.

    Reto (2026-10-01, ewod-dogfood-6: an ARR1 via through a pad of
    ARR1_SINK_O): "highly illegal and should prevent the router from
    starting". Fixed copper is frozen, so the router cannot fix such a
    collision; it can only route around a short. Same pad geometry
    (`pads_for_ir`) and the same rule (`drc.check_clearance`) view='drc'
    uses, restricted to error-severity findings that involve fixed copper —
    a pad-to-pad overlap is the placer's to fix and is not judged here."""
    return [
        f"{f.where}: {f.detail}"
        for f in pcb_session.fixed_copper_findings(
            ir, footprints, fixed_copper, fab_caps
        )
    ]


def _collision_reason(what: str, collisions: list[str]) -> str:
    shown = "\n".join(f"  {c}" for c in collisions[:10])
    more = f"\n  (+{len(collisions) - 10} more)" if len(collisions) > 10 else ""
    return (
        f"pcb_route: {what} — {len(collisions)} fixed-copper clearance "
        f"error(s) against another net's copper, which no routing can "
        f"remove:\n{shown}{more}\nMove the part off the fixed copper "
        "(op='move') or re-place, then route again; view='drc' lists them all."
    )


def _resolve_pin_swap_groups(
    ir: PcbIR,
    graph: dict[str, Any],
    warnings: list[str],
) -> tuple[pcb_pinswap.PinSwapGroup, ...]:
    """The radial warm start's feed (docs/backlog/pcb-ewod-multitile.md
    "Rulings 2026-09-19" item 6): an instance's ``supplies`` blocks or
    deprecated ``pin_swap_groups`` (``Store.pcb_graph``'s ``c.meta`` hoist —
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
        # Legacy `pin_swap_groups` plus every `supplies` block set, as
        # name lists (spare unwired pins have no IR pin and drop out below).
        declared = list(inst_dict.get("pin_swap_groups") or [])
        for sup in inst_dict.get("supplies") or []:
            declared.append([b[0] for b in sup["blocks"] if len(b) == 1])
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


def _apply_pin_swap_warm_start(
    ir: PcbIR,
    groups: tuple[pcb_pinswap.PinSwapGroup, ...],
    footprints: dict[str, dict[str, Any]],
    fixed_copper: list[dict[str, Any]],
    method: str = "distance",
) -> list[dict[str, Any]]:
    """Resolve connected via terminals and apply each admissible assignment."""
    if method not in ("distance", "radial"):
        raise ValueError("warm_start must be distance or radial")
    terminals = (
        pcb_realize._island_terminals_by_pin(ir, footprints, fixed_copper)
        if groups and method == "distance"
        else {}
    )
    reports: list[dict[str, Any]] = []
    for group in groups:
        far_by_pin = {}
        near_segments = pcb_pinswap._segments_near_pin(ir, group.instance)
        degree = Counter(near_segments.values())
        for seg, pin in near_segments.items():
            if degree[pin] != 1:
                continue
            a, b = int(ir.seg_pin_a[seg]), int(ir.seg_pin_b[seg])
            other = b if a == pin else a
            key = (
                str(ir.instance_refdes[int(ir.pin_instance[other])]),
                str(ir.pin_label[other]),
            )
            points = {t.point for t, _ in terminals.get(key, ()) if t.kind == "via"}
            if len(points) == 1:
                far_by_pin[pin] = next(iter(points))
        proposal = (
            pcb_pinswap.propose_distance_assignment(ir, group, far_by_pin)
            if method == "distance"
            else None
        )
        if proposal is None:
            pairs = pcb_pinswap.propose_radial_assignment(ir, group) or ()
            used, distance = "radial", None
        else:
            pairs, distance = proposal
            used = "distance"
        for a, b in pairs:
            ir.swap_pins(a, b)
        reports.append(
            {
                "refdes": str(ir.instance_refdes[group.instance]),
                "method": used,
                "distance_mm": distance,
            }
        )
    return reports


def _pins_of(c: pcb_matching.Client) -> tuple[str, ...]:
    """A client's member pins: the tuple, or its single pin."""
    return c.members if c.members is not None else (c.pin,)


def _client_of_member(m: dict[str, Any]) -> pcb_matching.Client | None:
    """The matcher client a graph member's ``client_of`` declares, or
    ``None``; a ``members`` list makes it a tuple client headed by the pin."""
    co = (m.get("meta") or {}).get("client_of")
    if not co:
        return None
    members = co.get("members")
    return pcb_matching.Client(
        instance=str(m["refdes"]),
        pin=str(m["pin"]),
        role=str(co["role"]),
        min_voltage_v=co.get("min_voltage_v"),
        members=tuple(str(x) for x in members) if members else None,
    )


def _matching_model(
    graph: dict[str, Any], warnings: list[str]
) -> tuple[
    list[pcb_matching.Client],
    list[pcb_matching.Supply],
    dict[str, pcb_matching.Role],
]:
    """The matcher's inputs from the stored graph: declared ``supplies``,
    ``client_of`` members (a ``members`` list = a tuple client) and ``roles``, plus the deprecated
    ``pin_swap_groups`` alias — each declared pin becomes a one-pin block
    of a per-group role (``pin_swap:<refdes>:<k>``) whose clients are the
    far ends of the 2-member nets on those pins. A pin whose net has any
    other member count is left out of the alias, with a warning."""
    roles = {
        str(name): pcb_matching.Role(
            name=str(name),
            voltage_v=spec.get("voltage_v"),
            fanout=spec.get("fanout", 1),
        )
        for name, spec in (graph.get("roles") or {}).items()
    }
    supplies: list[pcb_matching.Supply] = []
    clients: list[pcb_matching.Client] = []
    net_of: dict[tuple[str, str], dict[str, Any]] = {}
    for net in graph.get("nets") or []:
        for m in net.get("members") or []:
            net_of[(str(m["refdes"]), str(m["pin"]))] = net
            if client := _client_of_member(m):
                clients.append(client)
    # A client with no net yet (graph["unconnected"]) still demands a supplier.
    for m in graph.get("unconnected") or []:
        if client := _client_of_member(m):
            clients.append(client)
    client_keys = {(c.instance, c.pin) for c in clients}
    for inst in graph.get("instances") or []:
        refdes = str(inst["refdes"])
        for sup in inst.get("supplies") or []:
            supplies.append(
                pcb_matching.Supply(
                    instance=refdes,
                    role=str(sup["role"]),
                    blocks=tuple(tuple(str(p) for p in b) for b in sup["blocks"]),
                    voltage_v=sup.get("voltage_v"),
                    within_block_swappable=bool(sup.get("within_block_swappable")),
                )
            )
        declared = inst.get("pin_swap_groups") or []
        if not declared:
            continue
        warnings.append(
            f"pin_swap_groups on {refdes} is deprecated; declare supplies/client_of"
        )
        for k, names in enumerate(declared):
            role = f"pin_swap:{refdes}:{k}"
            blocks: list[tuple[str, ...]] = []
            for name in names:
                net = net_of.get((refdes, str(name)))
                if net is None:
                    continue  # unwired: no IR pin, nothing to swap
                members = net.get("members") or []
                if len(members) != 2:
                    warnings.append(
                        f"pin_swap: {refdes}.{name} left out of its group "
                        f"(net {net.get('name')} has {len(members)} members, not 2)"
                    )
                    continue
                other = next(
                    m
                    for m in members
                    if (str(m["refdes"]), str(m["pin"])) != (refdes, str(name))
                )
                okey = (str(other["refdes"]), str(other["pin"]))
                if okey in client_keys or okey[0] == refdes:
                    continue
                client_keys.add(okey)
                blocks.append((str(name),))
                clients.append(pcb_matching.Client(okey[0], okey[1], role))
            if blocks:
                supplies.append(pcb_matching.Supply(refdes, role, tuple(blocks)))
    return clients, supplies, roles


def _resolve_supply_matching(
    ir: PcbIR,
    authored: dict[str, Any],
    footprints: dict[str, dict[str, Any]],
    fixed_copper: list[dict[str, Any]],
    warnings: list[str],
    method: str = "distance",
) -> tuple[
    list[dict[str, Any]],
    list[str],
    dict[pcb_resolved.Pin, str | None] | None,
    dict[pcb_resolved.Pin, pcb_resolved.Pin],
]:
    """Supply/client matching before any routing (pcb-lazy-netlist-and-
    checks Slices 6+7): ``(report, unsupplied_findings, rebind, prior)``.

    Gate first: every client against every declared supply block; any
    unsupplied client returns its findings and nothing else (the caller
    fails the job ``input``). ``method='radial'`` keeps the legacy
    per-instance angular warm start, applied to ``ir`` in place.

    ``method='distance'`` solves on the AUTHORED netlist: a client whose
    every member pin has a declared net of its own (no second same-role
    client on it) is a candidate; a supply block is available when each
    of its pins is netless or declared on a candidate's net of that role.
    The min-cost Euclidean solve over those (``prior`` = the client ->
    supplier binding ``ir`` carries now, from the last snapshot) yields
    ``rebind``: every available supply pin of a solved role unbound, then
    each assigned one moved onto its client's declared net. A netless
    spare channel can take a client net this way (Reto 2026-10-16 ruling
    2a); the snapshot marks it ``solver_created``. A client with no
    declared net stays unbound, with a warning. A client is positioned at
    its unique fixed-copper via terminal, else at its own pin; every pin
    position comes from ``ir`` (netless pins are in it too). ``rebind`` is
    ``None`` when nothing was solved."""
    clients, supplies, roles = _matching_model(authored, warnings)
    if clients:
        try:
            gate = pcb_matching.solve(
                clients, supplies, roles, lambda _r, _p: (0.0, 0.0)
            )
        except ValueError as exc:
            return [], [str(exc)], None, {}
        if gate.unsupplied:
            return [], list(gate.findings), None, {}
        warnings.extend(gate.findings)
    if method == "radial":
        groups = _resolve_pin_swap_groups(ir, authored, warnings)
        return (
            _apply_pin_swap_warm_start(ir, groups, footprints, fixed_copper, "radial"),
            [],
            None,
            {},
        )
    if not clients:
        return [], [], None, {}

    declared = pcb_resolved.declared_pin_nets(authored)
    pin_id: dict[pcb_resolved.Pin, int] = {}
    effective: dict[pcb_resolved.Pin, str | None] = {}
    for p in range(ir.n_pins):
        key = (str(ir.instance_refdes[int(ir.pin_instance[p])]), str(ir.pin_label[p]))
        pin_id[key] = p
        n = int(ir.pin_net[p])
        effective[key] = None if n < 0 else str(ir.net_name[n])

    per_net = Counter(
        (declared.get((c.instance, p)), c.role) for c in clients for p in _pins_of(c)
    )
    candidates: list[pcb_matching.Client] = []
    netless: dict[str, list[str]] = {}
    for c in clients:
        nets = [declared.get((c.instance, p)) for p in _pins_of(c)]
        if any(n is None for n in nets):
            netless.setdefault(c.role, []).append(f"{c.instance}.{c.pin}")
        elif all(per_net[(n, c.role)] == 1 for n in nets):
            candidates.append(c)
        else:
            warnings.append(
                f"match: {c.instance}.{c.pin} shares its net with another "
                f"{c.role} client; left as last routed"
            )
    # The author names a client's net at design time; the solve never
    # mints one (Reto 2026-10-16).
    for role, names in sorted(netless.items()):
        warnings.extend(
            f"match: {role} client {name} has no net, left unbound; "
            "declare a net for it"
            for name in names
        )
    if not candidates:
        return [], [], None, {}
    cand_nets: dict[str, set[str]] = {}
    for c in candidates:
        for pn in _pins_of(c):
            net = declared[(c.instance, pn)]
            assert net is not None
            cand_nets.setdefault(c.role, set()).add(net)
    available: dict[str, set[pcb_resolved.Pin]] = {}
    for sup in supplies:
        nets_ok = cand_nets.get(sup.role, set())
        for block in sup.blocks:
            keys = [(sup.instance, p) for p in block]
            if all(
                k in declared and (declared[k] is None or declared[k] in nets_ok)
                for k in keys
            ):
                available.setdefault(sup.role, set()).update(keys)
    allowed = set().union(*available.values()) if available else set()
    prior = pcb_resolved.prior_assignment(candidates, supplies, effective)
    client_keys = {(c.instance, p) for c in candidates for p in _pins_of(c)}
    terminals = pcb_realize._island_terminals_by_pin(ir, footprints, fixed_copper)

    def position(refdes: str, pin: str) -> tuple[float, float] | None:
        key = (refdes, pin)
        pid = pin_id.get(key)
        if pid is None:
            return None
        if key in client_keys:
            points = {t.point for t, _ in terminals.get(key, ()) if t.kind == "via"}
            if len(points) == 1:
                return next(iter(points))
        return pin_point(ir, pid)

    def dist(a: pcb_resolved.Pin, b: pcb_resolved.Pin) -> float:
        pa, pb = position(*a), position(*b)
        if pa is None or pb is None:
            return 0.0
        return math.hypot(pa[0] - pb[0], pa[1] - pb[1])

    result = pcb_matching.solve(
        candidates, supplies, roles, position, prior=prior, allowed_suppliers=allowed
    )
    rebind: dict[pcb_resolved.Pin, str | None] = {}
    report: list[dict[str, Any]] = []
    for role in sorted({c.role for c in candidates}):
        rc = [
            (c.instance, p) for c in candidates if c.role == role for p in _pins_of(c)
        ]
        if any(k in result.unsupplied for k in rc):
            warnings.append(
                f"match: role {role} left as last routed (unsupplied clients)"
            )
            continue
        target: dict[pcb_resolved.Pin, str | None] = dict.fromkeys(
            available.get(role, ()), None
        )
        clash = False
        for k in rc:
            s_pin = result.assignment[k]
            if target.get(s_pin) not in (None, declared[k]):
                clash = True
            target[s_pin] = declared[k]
        if clash:
            warnings.append(
                f"match: role {role} left as last routed (a supplier pin was "
                "assigned clients on different nets)"
            )
            continue
        rebind.update(target)
        priced = [k for k in rc if k in prior]
        report.append(
            {
                "role": role,
                "method": "match",
                "clients": len(rc),
                "cost_mm": math.fsum(dist(k, result.assignment[k]) for k in rc),
                "prior_cost_mm": math.fsum(dist(k, prior[k]) for k in priced),
                "changed": sum(1 for k in rc if prior.get(k) != result.assignment[k]),
            }
        )
    return report, [], (rebind or None), prior


def _fixed_refdes(ir: PcbIR) -> list[str]:
    """Refdes of every instance the anneal may not shift (``fixed`` 'xy'
    or 'both') — the parts a placement refusal must not tell the user to
    "move" without saying they are pinned (gr489913)."""
    return [
        str(ir.instance_refdes[i])
        for i in range(ir.n_instances)
        if bool(ir.inst_fixed_xy[i])
    ]


def _moved_refdes(ir: PcbIR, before: tuple[Any, Any, Any]) -> list[str]:
    """Refdes of every instance moved off the ``(x, y, rot)`` arrays
    copied before the anneal ran; an unplaced (NaN) pose that stays NaN
    is not a move."""
    bx, by, brot = before
    moved: list[str] = []
    for i in range(ir.n_instances):
        pairs = (
            (bx[i], ir.inst_x[i]),
            (by[i], ir.inst_y[i]),
            (brot[i], ir.inst_rot[i]),
        )
        for a, b in pairs:
            a, b = float(a), float(b)
            if math.isnan(a) and math.isnan(b):
                continue
            if math.isnan(a) or math.isnan(b) or abs(a - b) > 1e-6:
                moved.append(str(ir.instance_refdes[i]))
                break
    return moved


def _refusal_reason(
    report: str, moved: Sequence[str], moved_violators: Sequence[str], n_parts: int
) -> str:
    """The pre-route gate's refusal text. When a violator is a part this
    job's anneal moved, the illegal placement is the anneal's output, not
    what the user stored — saying "the placement is already illegal"
    there read as a corrupt import (gr489913, taz15 jo484766). A moved
    part that is not a violator leaves the stored placement at fault."""
    if moved_violators:
        what = (
            f"the ANNEALED placement is illegal (this job's anneal moved "
            f"{len(moved)} of {n_parts} part(s) off the stored placement, "
            f"violators among them: {', '.join(moved_violators)}; their "
            "positions below are the anneal's, not the stored board's)"
        )
        fix = (
            "Pin the parts the anneal should not move or move them, re-run, "
            "or pass force=True to route anyway"
        )
    else:
        what = "the placement is already illegal"
        fix = (
            "Fix the placement (unpin a violator or move it), or pass "
            "force=True to route anyway"
        )
    return (
        f"pcb_route: refusing to route — {what}; nothing written.\n{report}\n"
        f"{fix}; the override is recorded in the job summary."
    )


class _LoopGateRefusal(Exception):
    """The pre-route gate's refusal, thrown out of the loop's route
    callable so it can cross ``anneal_route_loop`` — the same refusal the
    single-anneal path issues inline, with the same outcome: the job
    fails ``input`` and nothing is written."""


def _round_shape(ir: PcbIR, outcome: RouteOutcome) -> dict[str, Any]:
    """One loop round's persisted summary for ``meta.last_route.loop``:
    the honest per-NET counts (dangling = a <2-member net, the same
    definition the rows below count under) plus the board shape that
    round's route actually saw — outline bbox mm and copper layers, the
    two axes a priced expansion can move."""
    member_counts = net_member_counts(ir)
    dangling = sum(1 for n in range(ir.n_nets) if member_counts.get(n, 0) < 2)
    outline_mm: list[float] | None = None
    if ir.outline and len(ir.outline) >= 3:
        x0, y0, x1, y1 = outline_bbox(ir.outline)
        outline_mm = [round(x1 - x0, 3), round(y1 - y0, 3)]
    return {
        "routed": outcome.routed,
        "failed": outcome.total - outcome.routed,
        "dangling": dangling,
        "outline_mm": outline_mm,
        "layers": ir.n_layers,
    }


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    warm_start = str(params.get("warm_start", "distance"))
    if warm_start not in ("distance", "radial"):
        ctx.record_failure(
            "pcb_route: warm_start must be distance or radial", failure_class="input"
        )
        return
    router = str(params.get("router") or "vector")
    if router != "vector":
        ctx.record_failure(
            f"pcb_route: router={router!r} — the grid maze router was "
            "removed; only 'vector' exists",
            failure_class="input",
        )
        return
    # Removed grid-router knobs a caller may still send: honest, not a
    # refusal — each is reported in the route note, never silently eaten.
    ignored = [
        name
        for name in (
            "negotiate",
            "corridor",
            "escape",
            "layer_cost",
            "off_axis",
            "cleanup",
        )
        if params.get(name) not in (None, 0, 0.0, "", False, {})
    ]
    loop_rounds = int(params.get("loop") or 0)
    # Same arm as `negotiate` above: an out-of-range loop count refuses
    # before the board is ever read.
    if not 0 <= loop_rounds <= LOOP_MAX_ROUNDS:
        ctx.record_failure(
            f"pcb_route: loop={loop_rounds} is out of range; give 0 (off) "
            f"to {LOOP_MAX_ROUNDS} route rounds",
            failure_class="input",
        )
        return
    pcb_ref_id = int(params["pcb_ref_id"])
    iters = int(params.get("iters") or _DEFAULT_ITERS)
    seed = int(params.get("seed") or 0)
    groups = bool(params.get("groups") or False)

    # One read of the graph and every surface the idem key hashes
    # (`load_snapshot`, the same call `_enqueue_op` digests). No re-read
    # follows this job's own writes below: they are the last thing it does.
    snap = pcb_session.load_snapshot(ctx.store, pcb_ref_id)
    graph = snap.graph
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
    # Slice 7: the stored graph is the intent; the latest resolved
    # snapshot laid over it is the board as last routed, and this run's
    # prior. The solve below rebinds from the intent, never from a patch.
    authored = graph
    declared = pcb_resolved.declared_pin_nets(authored)
    graph = pcb_resolved.apply(
        authored, ctx.store.pcb_netlist_snapshot_latest(pcb_ref_id)
    )

    # The board outline, same as ``pcb_place`` already fetches it. This job
    # used to call ``build_ir(graph)`` with no outline at all, so its anneal
    # AND its realizer both ran against a board they could not see, while
    # ``_render_drc`` checked their output against the real one — 2
    # ``board_edge_clearance`` errors on a board that was otherwise clean,
    # and the place job's outline-aware placement quietly re-annealed away
    # here. One rule, two call sites, drifted: the same shape as every
    # other defect this build has produced.
    features = snap.features
    defect = pcb_session.outline_defect(pcb_session.outline_from_features(features))
    if defect is not None:
        ctx.record_failure(
            f"pcb_route: refusing to route design (ref_id={pcb_ref_id}) — {defect}. "
            f"Fix: {pcb_session.OUTLINE_FIX_HINT}",
            failure_class="input",
        )
        return

    def _build(g: dict[str, Any]) -> PcbIR:
        return pcb_session.build_ir(
            g,
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
            # This job RE-PLACES the board, so its anneal needs the authored
            # vias as obstacles exactly as ``pcb_place``'s does — one rule,
            # two call sites again; the realizer further down reads the same
            # rows for the router's grid.
            fixed_copper=snap.fixed_copper,
        )

    ir = _build(graph)
    routes_by_net = snap.routes

    fixed_copper = snap.fixed_copper

    def _footprints(ir: PcbIR) -> dict[str, dict[str, Any]]:
        return pcb_session.footprints_by_refdes(
            ir,
            ctx.store.pcb_footprints_for(pcb_ref_id),
            local_footprints_by_name=ctx.store.pcb_local_footprints_for(pcb_ref_id),
            local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
        )

    pin_swap_warnings: list[str] = []
    # Match supply/client pins before the optimizer or any routing, off the
    # board as last routed (the prior). Shorter paths through fixed-copper
    # corridors beat fewer airwire crossings on the replay. A changed
    # assignment rebinds the intent and rebuilds the IR from it, so every
    # step below (sketch, planes, anneal, router) sees one netlist.
    warm_start_report, unsupplied, rebind, match_prior = _resolve_supply_matching(
        ir, authored, _footprints(ir), fixed_copper, pin_swap_warnings, warm_start
    )
    if unsupplied:
        ctx.record_failure(
            "pcb_route: refusing to route - "
            + "; ".join(unsupplied)
            + ". Add suppliers (or fewer clients) for the role, then route again.",
            failure_class="input",
        )
        return
    if rebind is not None:
        # Over the board as last routed: a role the solve skipped keeps
        # its prior binding instead of falling back to the intent.
        rebound = pcb_resolved.rebind(graph, rebind)
        if pcb_resolved.declared_pin_nets(rebound) != pcb_resolved.declared_pin_nets(
            graph
        ):
            graph = rebound
            ir = _build(graph)
    unmatched_sketch = pcb_session.apply_route_overrides(ir, routes_by_net)

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
    #
    # The stackup's own `plane_net` and `fill` declarations are applied the
    # same way and locked like authored rows (pcb-stackup-regions slices
    # 3-4; gr495287: the default stackup's In1 GND was never read, so GND
    # routed as traces and In1 stayed empty).
    applied = pcb_session.apply_plane_assignments(ir, snap.planes)
    authored_net_names = set(applied.locked_names)
    # Only locked nets are (see the comment above) -- a derived assignment
    # (this run's own prior write-back) must stay fully explorable, which
    # is the whole distinction `meta.source` exists to carry.
    locked_plane_nets = applied.locked
    # A prior run's derived power regions are a warm start only: they are
    # re-derived for the settled placement right before realize.
    region_notes: list[str] = []

    def _apply_derived_regions(ir_: PcbIR) -> None:
        """Re-derive the power regions for ``ir_``'s current placement
        (:func:`precis.pcb.realize.derive_power_regions`; pcb-stackup-
        regions slice 4). Every unlocked net poured inside a region is
        demoted first, so a net whose region no longer fits routes as
        traces; deterministic in the placement, so deriving twice for one
        placement gives the same regions."""
        stale = {
            net
            for (net, _layer), (region, _prio) in ir_.plane_regions.items()
            if region is not None and net not in locked_plane_nets
        }
        for n in sorted(stale):
            ir_.demote_plane(n)
        regions, notes = pcb_realize.derive_power_regions(ir_, realize_config)
        for n, layer, region in regions:
            ir_.promote_plane(n, layer, region=region.region, priority=region.priority)
        region_notes[:] = notes

    # Same authored-measure enforcement as pcb_place's anneal — without
    # this, the route job's re-place pass would silently undo whatever a
    # proximity/separation measure had just pulled into shape (one rule,
    # one call site short). `raw_measures` also feeds the invariant gate
    # below (resolve_measures drops `strength='invariant'` rows).
    raw_measures = snap.measures
    measures = resolve_measures(raw_measures, features=features)
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
    footprints = _footprints(ir)
    try:
        profile = pcb_profiles.board_profile(board.get("meta"))
        fab_caps = pcb_session.board_capability(ir.stackup, profile)
    except ValueError as exc:
        ctx.record_failure(f"pcb_route: {exc}", failure_class="infra")
        return
    # Fixed copper through another net's pad is a short the router cannot
    # remove — refuse before spending the anneal, and write nothing.
    collisions = _fixed_copper_collisions(ir, footprints, fixed_copper, fab_caps)
    if collisions:
        ctx.record_failure(
            _collision_reason("refusing to route", collisions), failure_class="input"
        )
        return
    # gr489911 part 2: this job's anneal is a placer too, so a part it moves
    # takes the fixed copper on its pads with it (pcb_place's rule, one
    # definition in pcb_session). Branches are measured here, on the stored
    # poses; the single-anneal path below rips after `optimize`. The loop
    # path anneals between rounds and does not rip yet — its post-anneal
    # collision check still refuses a part parked on fixed copper.
    fixed_copper_stored = fixed_copper
    branches = (
        pcb_session.fixed_copper_branches(
            ir,
            footprints,
            pcb_session.rippable_fixed_copper(
                fixed_copper, set(ctx.store.pcb_generators_for(pcb_ref_id))
            ),
        )
        if fixed_copper
        else []
    )
    ripped: set[int] = set()

    # The incumbent board's own floor, measured BEFORE the anneal touches
    # `ir`'s poses — the never-commit-a-worse-board guard (gr480010; job
    # 479998 committed a 4→38 failed-net, 10→76 DRC-error result over a
    # still-good board) needs a BEFORE that is measured, not remembered:
    # a persisted view='drc' run could describe a stale board, so the same
    # rule sets judge both sides. Nets-not-realized reads the stored
    # pcb_routes statuses (an 'unrouted' net the board never attempted is
    # not 'failed', but it is not realized either — the comparison is of
    # how much of the board WORKS, so a first route can never refuse on
    # this axis and a re-route that only re-labels unrouted nets as failed
    # does not either). Fail-open, same discipline as the pre-route gate
    # below: a measurement that raises disables the DRC half of the
    # guard, never the route.
    prior_unrealized = sum(
        1 for r in routes_by_net.values() if r.get("status") != "realized"
    )
    prior_drc_errors: int | None = None
    try:
        prior_drc_errors = sum(
            1
            for f in (
                pcb_session.placement_drc_findings(
                    ir,
                    capability=fab_caps,
                    footprints=footprints,
                    fixed_copper=fixed_copper,
                    outline=pcb_session.outline_from_features(features),
                )
                + pcb_session.stored_copper_drc_findings(
                    ir,
                    ctx.store.pcb_copper_list(int(board_id)),
                    capability=fab_caps,
                    footprints=footprints,
                    outline=pcb_session.outline_from_features(features),
                    net_rules=_graph_net_rules(graph, fab_caps),
                    net_voltages=_graph_net_voltages(graph),
                )
            )
            if f.severity == "error"
        )
    except Exception:
        log.exception(
            "pcb_route: incumbent DRC measurement raised; the guard's "
            "error half is disabled for this run"
        )

    force = bool(params.get("force", False))
    expansion_caps: ExpansionCaps | None = None
    if loop_rounds:
        # Authored bounds for the loop's priced expansion moves, forwarded
        # verbatim from the design's meta.expansion_caps by the handler
        # (or supplied directly on a job put — the params contract admits
        # the key either way). Absent caps leave OUTLINE_GROW/LAYER_ADD
        # disabled exactly as the bare loop treats them; a malformed cap
        # is an input failure, never silently ignored.
        raw_caps = params.get("expansion_caps")
        if raw_caps is not None:
            try:
                outline_max = raw_caps["outline_max_mm"]
                expansion_caps = ExpansionCaps(
                    outline_max_mm=(float(outline_max[0]), float(outline_max[1])),
                    max_copper_layers=int(raw_caps["max_copper_layers"]),
                    max_fab_usd=float(raw_caps["max_fab_usd"]),
                )
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                ctx.record_failure(
                    f"pcb_route: expansion_caps is malformed ({exc}); "
                    "expected {'outline_max_mm': [w_mm, h_mm], "
                    "'max_copper_layers': n, 'max_fab_usd': usd}",
                    failure_class="input",
                )
                return
    # place=False: route-only. `hold_poses` skips every pose-moving pass of
    # `optimize`, so poses and the stored sketch reach the realizer as stored.
    route_only = params.get("place", True) is False
    if route_only and loop_rounds:
        ctx.record_failure(
            "pcb_route: place=False cannot combine with loop",
            failure_class="input",
        )
        return
    config = OptimizeConfig(
        iters=0 if route_only else iters,
        hold_poses=route_only,
        seed=seed,
        locked_plane_nets=locked_plane_nets,
        measures=measures,
        # None on the single-anneal path AND on a cap-less loop — the
        # priced generators refuse without authored bounds, and the
        # single path's schedule never draws them anyway.
        expansion=expansion_caps,
        support_groups=groups,
        # Absent param = FIELD_POLISH_DEFAULT, the one switch (off until
        # the silk gate lands).
        field_polish=bool(params.get("field_polish", FIELD_POLISH_DEFAULT)),
    )
    realize_config = pcb_realize.RealizeConfig(
        fab_caps=fab_caps,
        class_rules=graph.get("net_classes"),
        # The profile's soft cost for a via under a part's courtyard
        # (gr502369); None on prod ⇒ 0, today's behaviour exactly.
        via_body_cost_mm=pcb_profiles.profile_rules(profile).via_body_cost_mm or 0.0,
    )

    # `footprints` (the same refdes-keyed pad geometry PIN_SWAP's feed
    # above also used) was resolved earlier, before the anneal — see that
    # block's own comment for why.

    def _settled_writebacks() -> dict[str, Any]:
        """The anneal's settled plane decisions, diffed for
        write-back — read off the CURRENT ``ir``: the single path's
        post-anneal state, or the loop's restored best round."""
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
        #
        # A derived power region (pcb-stackup-regions slice 4) rides the
        # same row as its region and priority.
        derived: dict[str, Any] = {}
        for n in range(ir.n_nets):
            name = str(ir.net_name[n])
            if name in authored_net_names:
                continue
            for layer in plane_layers_of(int(ir.net_plane_layers[n])):
                layer_name = str(ir.stackup[layer]["name"])
                region, priority = ir.plane_regions.get((n, layer), (None, 0))
                derived[name] = (
                    {"layer": layer_name, "region": region, "priority": priority}
                    if region is not None or priority
                    else layer_name
                )
        return derived

    def _post_anneal_collision_check() -> bool:
        """The anneal moves unfrozen parts, so check again before any
        write: a placement that parks a pad on fixed copper is not
        persisted either. True when the job refused and must return."""
        collisions = _fixed_copper_collisions(ir, footprints, fixed_copper, fab_caps)
        if collisions:
            ctx.record_failure(
                _collision_reason("the anneal placed pads on fixed copper", collisions),
                failure_class="non-convergence",
            )
            return True
        return False

    # Pre-route DRC gate (the bug this closes): `drc.run_geometric_drc` had
    # exactly ONE caller — `view='drc'` — so a board could be placed, routed
    # and reported on without any geometric check ever running, and the
    # first time anyone learned the placement was illegal was when a human
    # asked for the DRC view (gr451052: nine `via_pad_keepout` errors
    # between authored escape vias and the HV507 driver's own lands — pad/
    # via geometry present here, BEFORE the maze router below draws a thing).
    # Run the placement-only subset now, off the SETTLED post-anneal
    # positions plus only the authored fixed copper, and surface any error
    # LOUDLY and by name in the job summary — "a check you did not RUN is
    # not a check that passed" (handlers/pcb.py doctrine), enforced at this
    # seam. This is scope (a) of the report: an error-severity finding now
    # REFUSES the route (below), before the router runs and before anything
    # is written; warnings do not block. It does not shift a part — (b)/(c),
    # a placement-illegality cost term and an escape hatch for a
    # `fixed='both'` violator, are the larger design question the report
    # parks; on ewod-dogfood-2 every part is `fixed='both'`, so the
    # refusal is a named complaint the user must act on by hand — or
    # `force=True` routes anyway, with the override recorded in the summary.
    # Belt-and-suspenders: the gate is a NEW geometry pass, so a crash in it
    # must never take down a route that would otherwise succeed — an added
    # check that fails closed on the whole job would be worse than the silent
    # gap it replaces. On error it degrades to "no gate result", logged, and
    # routing proceeds exactly as before.
    # gr489913: the poses before this job's anneal (set by each path just
    # before it runs) and what the last gate found against them, so a
    # refusal can say whether a violator is a part the anneal moved.
    pose_before: list[tuple[Any, Any, Any]] = []
    last_gate: dict[str, list[str]] = {"moved": [], "moved_violators": []}

    def _placement_gate() -> tuple[int, str | None]:
        """The pre-route placement-DRC + invariant-measure gate over the
        CURRENT ``ir`` — one block for every seam the router is about to
        run on: the single path's post-anneal poses, and every loop
        round's routed state inside ``_route_once``. Returns ``(n_errors,
        rendered report)``; a raised check degrades to (0, None), logged."""
        try:
            placement_findings = pcb_session.placement_drc_findings(
                ir,
                capability=fab_caps,
                footprints=footprints,
                fixed_copper=fixed_copper,
                outline=pcb_session.outline_from_features(features),
            )
            # gr345859 §F — violated `strength='invariant'` measures (the
            # generator-declared fabric rules) join the same gate: judged on
            # the post-anneal pads plus the whole copper store (stored router
            # copper a violation names yields as 'derived'), refusing the route
            # exactly like a placement DRC error unless force=True.
            layers = [str(layer.get("name")) for layer in ir.stackup]
            placement_findings += pcb_session.invariant_measure_findings(
                graph=graph,
                measures=raw_measures,
                features=features,
                generators_rows=ctx.store.pcb_generators_for(pcb_ref_id),
                pads=pcb_realize.pads_for_ir(ir, layers, footprints),
                copper=[
                    {**row, "net": row.get("net") or ""}
                    if row.get("fixed")
                    else {**row, "derived": True}
                    for row in ctx.store.pcb_copper_list(int(board_id))
                    if row.get("fixed_id") not in ripped
                ],
                layers=layers,
            )
            moved = _moved_refdes(ir, pose_before[0]) if pose_before else []
            moved_set = set(moved)
            violators = dict.fromkeys(
                r
                for f in placement_findings
                if f.severity == "error"
                for r in pcb_session.finding_refdes(f)
            )
            moved_violators = [r for r in violators if r in moved_set]
            last_gate.update(moved=moved, moved_violators=moved_violators)
            return pcb_session.placement_drc_report(
                placement_findings,
                fixed_refdes=_fixed_refdes(ir),
                subject="as annealed" if moved_violators else "as placed",
            )
        except Exception:
            log.exception("pcb_route: pre-route DRC gate raised; routing anyway")
            return 0, None

    result: OptimizeResult | None = None
    loop_result: RouteLoopResult | None = None
    rres: pcb_realize.RealizeResult | None = None
    n_placement_errors = 0
    placement_report: str | None = None
    round_shapes: list[dict[str, Any]] = []
    committed_round = 0
    refused_rounds: list[int] = []
    if loop_rounds:
        # route -> penalise -> anneal -> route (pcb-anneal-closes-router-
        # loop.md). `realize()` runs once per round inside `_route_once`,
        # behind the SAME pre-route gate the single path runs. Round 0
        # routes the stored board; a refusal there writes nothing, exactly
        # like the inline refusal below. A LATER round's refusal (the
        # anneal between rounds parked a part illegally) is recorded as a
        # zero-routed round instead of discarding the rounds already won.
        #
        # Which round commits is decided HERE, not by the loop's own
        # best-round rank: this job can only write poses, swaps, planes
        # and copper, never a grown outline or an added layer pair (the
        # stored features/stackup stay what the design authored), so a
        # round whose board shape differs from the stored board is not
        # committable — its copper would describe a board the store does
        # not hold, and the post-route DRC/ratchet below measure against
        # the stored outline. Such rounds are reported as PRICED PROPOSALS
        # in `meta.last_route.loop` (the digest already carries the
        # marginal prices); applying one is a design edit (outline
        # feature / stackup) followed by a re-run — the LLM-menu boundary
        # docs/backlog/pcb-global-plan-router.md draws. Round 0 is always
        # the stored shape, so an eligible round always exists.
        realized_by_round: list[pcb_realize.RealizeResult | None] = []
        round_gates: list[tuple[int, str | None]] = []
        round_states: list[dict[str, Any]] = []
        round_outcomes: list[RouteOutcome] = []
        n_nets_total = len({int(ir.seg_net[s]) for s in range(ir.n_segments)})

        def _route_once(ir_: PcbIR) -> RouteOutcome:
            _apply_derived_regions(ir_)
            n_err, report = _placement_gate()
            round_gates.append((n_err, report))
            round_states.append(_ir_state_snapshot(ir_))
            if n_err and not force:
                if not round_outcomes:
                    raise _LoopGateRefusal(report or "")
                refused_rounds.append(len(round_outcomes))
                realized_by_round.append(None)
                refused = RouteOutcome(
                    routed=0,
                    total=n_nets_total,
                    complete=False,
                    note="pre-route gate refused this round's placement",
                )
                round_outcomes.append(refused)
                round_shapes.append({**_round_shape(ir_, refused), "refused": True})
                return refused
            if n_err:
                log.warning(
                    "pcb_route: force=True — routing a placement with %d "
                    "pre-route DRC error(s)",
                    n_err,
                )
            rres_ = pcb_realize.realize(
                ir_,
                config=realize_config,
                footprints=footprints,
                fixed_copper=fixed_copper,
            )
            realized_by_round.append(rres_)
            outcome = pcb_feedback.outcome_from_realize(ir_, rres_)
            round_outcomes.append(outcome)
            round_shapes.append(_round_shape(ir_, outcome))
            return outcome

        try:
            pose_before.append((ir.inst_x.copy(), ir.inst_y.copy(), ir.inst_rot.copy()))
            loop_result = anneal_route_loop(
                ir,
                config,
                _route_once,
                loop=RouteLoopConfig(rounds=loop_rounds, seed=seed),
            )
        except _LoopGateRefusal as exc:
            # Round 0 routes before any anneal, but the loop's seed /
            # legalize pass may already have moved parts.
            ctx.record_failure(
                _refusal_reason(
                    str(exc),
                    last_gate["moved"],
                    last_gate["moved_violators"],
                    ir.n_instances,
                ),
                failure_class="input",
            )
            return
        stored_shape = (round_shapes[0]["outline_mm"], round_shapes[0]["layers"])
        eligible = [
            i
            for i, shape in enumerate(round_shapes)
            if i not in refused_rounds
            and (shape["outline_mm"], shape["layers"]) == stored_shape
        ]
        if loop_result.best_round in eligible:
            committed_round = loop_result.best_round
        else:
            # The loop's own rank, restricted to committable rounds:
            # fewest failures, then most routed, then the earliest.
            committed_round = min(
                eligible,
                key=lambda i: (
                    len(round_outcomes[i].failures),
                    -round_outcomes[i].routed,
                    i,
                ),
            )
            _ir_state_restore(ir, round_states[committed_round])
        rres = realized_by_round[committed_round]
        assert rres is not None  # an eligible round is never a refused one
        n_placement_errors, placement_report = round_gates[committed_round]
        if n_placement_errors:
            # Only reachable under force=True — a non-forced error count
            # already refused the whole run out of `_route_once`.
            placement_report = (
                f"pre-route DRC gate OVERRIDDEN (force=True): routing a placement "
                f"with {n_placement_errors} error(s)\n{placement_report}"
            )
        if _post_anneal_collision_check():
            return
        _apply_derived_regions(ir)
        derived_assignments = _settled_writebacks()
    else:
        pose_before.append((ir.inst_x.copy(), ir.inst_y.copy(), ir.inst_rot.copy()))
        result = optimize(ir, config)
        if branches:
            ripped = pcb_session.fixed_copper_ripup(
                ir,
                footprints,
                fixed_copper_stored,
                branches,
                set(_moved_refdes(ir, pose_before[0])),
                fab_caps,
            )
            # Every gate and the realize below read the kept copper.
            fixed_copper = [
                r for r in fixed_copper_stored if r["fixed_id"] not in ripped
            ]
        if _post_anneal_collision_check():
            return
        _apply_derived_regions(ir)
        derived_assignments = _settled_writebacks()
        n_placement_errors, placement_report = _placement_gate()
        if n_placement_errors and not force:
            ctx.record_failure(
                _refusal_reason(
                    placement_report or "",
                    last_gate["moved"],
                    last_gate["moved_violators"],
                    ir.n_instances,
                ),
                failure_class="input",
            )
            return
        if n_placement_errors:
            log.warning(
                "pcb_route: force=True — routing a placement with %d pre-route "
                "DRC error(s)",
                n_placement_errors,
            )
            placement_report = (
                f"pre-route DRC gate OVERRIDDEN (force=True): routing a placement "
                f"with {n_placement_errors} error(s)\n{placement_report}"
            )

        # Authored fixed copper (`fixed_copper`, read before the anneal for the
        # collision gate; pcb-pre-place-route-blocks Slice 1, "Realize seam"):
        # claimed on the occupancy grid as real obstacles for every OTHER net,
        # and short-circuits any ratsnest segment its own two pins already
        # bridge (`RealizeResult.fixed_realized` below) — see `realize()`'s own
        # `fixed_copper` keyword docstring for both halves.
        rres = pcb_realize.realize(
            ir,
            config=realize_config,
            footprints=footprints,
            fixed_copper=fixed_copper,
        )
    assert rres is not None  # every non-returning arm above sets it
    # Post-route legality gate. Legality is a hard gate and incompleteness the
    # only permitted failure (docs/backlog/pcb-always-valid-board-invariant.md):
    # router copper that breaks a geometric DRC rule is NOT stored — its net
    # is stripped and failed with the rule named. Everything below reads the
    # stripped `rres`. Unlike the advisory pre-route gate above this one fails
    # CLOSED: if the check itself raises, nothing from this realize is
    # written — not the copper (unchecked), and not the per-net statuses
    # either, which without that copper would call a plane net `realized`.
    drc_fail: dict[str, list[dict[str, Any]]] = {}
    try:
        routed_findings = pcb_session.routed_drc_findings(
            ir,
            rres,
            capability=fab_caps,
            footprints=footprints,
            fixed_copper=fixed_copper,
            outline=pcb_session.outline_from_features(features),
            net_rules=_graph_net_rules(graph, fab_caps),
            net_voltages=_graph_net_voltages(graph),
        )
        rres, drc_fail = pcb_session.strip_drc_violating_nets(ir, rres, routed_findings)
        # Connectivity over the copper that will actually be written (the
        # STRIPPED `rres` plus fixed copper and pads), by the same checker
        # view='drc' runs (gr449483: 13 nets read `realized` here while
        # that check found their copper in 2-3 pieces). Inside the
        # fail-closed block for the same reason as the legality gate: a
        # status this job cannot vouch for is not written.
        split = pcb_session.split_nets(
            ir, rres, footprints=footprints, fixed_copper=fixed_copper
        )
        # What the board will CARRY if this attempt commits: every
        # error-severity finding stripping did NOT resolve. A finding is
        # resolved when any router copper it names was stripped (the net
        # is then counted on the failed axis instead); one naming only
        # pads/authored copper, or only kept nets' copper, is still in the
        # stored board. The incumbent measured this same number on its
        # stored rows before the anneal.
        stripped_nets = set(drc_fail)
        committed_drc_errors = sum(
            1
            for f in routed_findings
            if f.severity == "error"
            and not (pcb_session.router_nets_of(f) & stripped_nets)
        )
    except Exception as exc:
        log.exception("pcb_route: post-route DRC gate raised; writing nothing")
        gate_failure = (
            f"pcb_route: post-route DRC gate raised ({exc}); nothing written — "
            "the board keeps its previous placement, copper and statuses"
        )
        ctx.append_chunk("job_summary", gate_failure)
        ctx.record_failure(gate_failure, failure_class="infra")
        return

    fixed_realized_net_ids = {int(ir.seg_net[s]) for s in rres.fixed_realized}
    plane_net_ids = {n for n in range(ir.n_nets) if int(ir.net_plane_layers[n]) != 0}
    crossing_fail = _residual_crossings(ir, plane_net_ids)
    congestion_fail: dict[str, list[dict[str, Any]]] = {}
    for w in rres.warnings:
        detail = {
            "kind": "gap-capacity",
            # `reason` too, not just `kind` — the per-net note builder only
            # surfaces `reason` values, so a gap-only failure used to land
            # 'failed' with note=None (gr495293: opaque route reasons).
            "reason": "congestion",
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
    pour_note = pcb_planes.pour_notes(rres.pour_quality)
    # Pour pieces nothing could reach, removed after stitching
    # (pcb-floating-pour-island: drop only what cannot be stitched, and say
    # so — silently deleted copper is invisible until someone measures the
    # gerbers).
    dropped_by_net: dict[str, list[str]] = {}
    for gone in rres.dropped_islands:
        dropped_by_net.setdefault(gone.net, []).append(gone.phrase())
    for net_name, pieces in dropped_by_net.items():
        clause = (
            f"dropped {len(pieces)} unreachable island(s): {'; '.join(pieces[:6])}"
            + (f" (+{len(pieces) - 6} more)" if len(pieces) > 6 else "")
        )
        pour_note[net_name] = (
            f"{pour_note[net_name]}; {clause}" if net_name in pour_note else clause
        )
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
    n_realized = n_failed = n_dangling = n_disconnected = 0
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
                    "note": DANGLING_NET_NOTE,
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
            + drc_fail.get(net_name, [])
        )
        # The last rung, and only for a net that would otherwise read
        # `realized`: its copper must be ONE piece. A net that already
        # failed for a named reason (unrouted, DRC-stripped) is split as a
        # CONSEQUENCE of that reason — saying "disconnected" too would report
        # one defect under two names (`net_islands`' own docstring).
        island = split.get(net_name)
        if (
            not problems
            and island is not None
            and (net_id in routed_nets or int(ir.net_plane_layers[net_id]) != 0)
        ):
            n_disconnected += 1
            problems = [
                {
                    "kind": "disconnected",
                    "reason": "disconnected",
                    "components": island.components,
                    "message": (
                        f"copper is in {island.components} disconnected pieces; "
                        "witnesses (one point per piece): "
                        + "; ".join(
                            f"({x:.3f},{y:.3f}) on {layer}"
                            for x, y, layer in island.witnesses[:6]
                        )
                    ),
                }
            ]
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
            # The note is the ONLY thing route-status and the check view's
            # `unrouted` finding ever show — bare kinds ('failed: width')
            # were gr495293's opaque-reason complaint. Each problem's own
            # evidence names the blocker: a crossing names the other net
            # and the layer, an unrouted segment names its endpoint pins
            # plus the realizer's diagnosis (needed width, corridor,
            # contested-by), a gap/drc/unstitched/disconnected problem
            # already carries its sentence.
            details: list[str] = []
            for p in problems:
                if p.get("reason") == "same-layer-crossing":
                    lname = pcb_realize.layer_names(ir, [int(p.get("layer", -1))])[0]
                    details.append(f"crosses {p.get('with')} on {lname}")
                elif p.get("kind") == "unrouted" and p.get("segment"):
                    details.append(f"{p['segment']}: {p.get('message') or ''}")
                elif p.get("message"):
                    details.append(str(p["message"]))
            note = f"failed: {', '.join(kinds)}" if kinds else "failed"
            if details:
                head = [d[:220] for d in details[:3]]
                note += " — " + "; ".join(head)
                if len(details) > 3:
                    note += f" (+{len(details) - 3} more)"
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
        if net_name in pour_note:
            # Pour quality (pcb-stackup-regions slice 1): additive, like
            # the floating-copper note, and never a status change.
            note = f"{note}; {pour_note[net_name]}" if note else pour_note[net_name]
        rows[net_name] = {
            **entry,
            "status": status,
            "fail": {"problems": problems} if problems else None,
            "note": note,
        }

    # ── Never commit a worse board (gr480010) ─────────────────────────
    # Scored BEFORE anything commits, against the incumbent measured
    # before the anneal: nets not realized ('failed' AND 'sketched' vs the
    # stored statuses — a net that loses realized copper is a regression
    # whether it reads failed or not) and DRC errors on the board this
    # attempt would store. A worse attempt writes NOTHING — the board
    # keeps its previous poses, planes, pin swaps, copper and statuses —
    # and the job reports the attempt instead of committing it.
    n_unrealized = sum(1 for r in rows.values() if r["status"] != "realized")
    n_new_drc_errors = n_placement_errors + committed_drc_errors
    worse: list[str] = []
    if n_unrealized > prior_unrealized:
        worse.append(
            f"{n_unrealized} net(s) unrealized vs the board's stored {prior_unrealized}"
        )
    if prior_drc_errors is not None and n_new_drc_errors > prior_drc_errors:
        worse.append(
            f"{n_new_drc_errors} DRC error(s) vs the board's stored {prior_drc_errors}"
        )

    # What the supply/client matching bought per role (gr491373): the
    # stored last_route.warm_start cost pair, shown — not just stored.
    match_summary = "".join(
        f"\n{line}" for line in format_warm_start(warm_start_report)
    )
    pin_swap_summary = "\n" + "\n".join(pin_swap_warnings) if pin_swap_warnings else ""
    if ignored:
        pin_swap_summary += "\nignored removed grid-router param(s): " + ", ".join(
            ignored
        )
    vector_line = rres.vector.line() if rres.vector is not None else None
    if vector_line:
        pin_swap_summary += "\n" + vector_line
    # One line per unrouted segment, naming the reason (gr503666): the
    # pcb_routes rows carry the full diagnosis, but the job summary is
    # what a reader sees first — "11 of 14 gridless" alone named nothing.
    unrouted_lines = [
        f"unrouted {pcb_session.segment_key(ir, seg_id)} "
        f"({ir.net_name[int(ir.seg_net[seg_id])]!s}): "
        f"{(reason_by_seg[seg_id].kind if seg_id in reason_by_seg else 'unknown')}"
        for seg_id in rres.unrouted
    ]
    if unrouted_lines:
        pin_swap_summary += "\n" + "\n".join(unrouted_lines[:12])
        if len(unrouted_lines) > 12:
            pin_swap_summary += (
                f"\n… and {len(unrouted_lines) - 12} more unrouted segments"
            )
    # A class naming a layer the stackup does not route on is narrowed, not
    # refused (gr472108): say so, or a "no gain" reads as a routing result.
    for drop in rres.class_layer_drops:
        pin_swap_summary += f"\nwarning: {drop}"
    # Plane assignments the stackup declares but cannot pour, and power
    # nets left without a derived region (pcb-stackup-regions slices 3-4).
    for note in applied.notes:
        pin_swap_summary += f"\nplanes: {note}"
    for note in region_notes:
        pin_swap_summary += f"\nregions: {note}"
    if rres.dropped_islands:
        pin_swap_summary += (
            f"\nplanes: dropped {len(rres.dropped_islands)} unreachable pour "
            "island(s) after stitching (see each net's note)"
        )
    if unmatched_sketch:
        pin_swap_summary += (
            f"\nwarning: {unmatched_sketch} stored layer/side sketch "
            "entr(ies) matched no segment and were not restored"
        )
    # The pre-route DRC gate's own account, ahead of the routing digest — an
    # illegal placement is the FIRST thing this summary should say, since
    # (gr451052) it is the thing that made the routing below meaningless.
    placement_summary = f"{placement_report}\n\n" if placement_report else ""
    if loop_result is not None:
        d = loop_result.digest
        loop_line = (
            f"\nroute-loop: {'complete' if d.complete else 'incomplete'} after "
            f"{d.rounds_run} round(s), best={loop_result.best_round}, "
            f"committed={committed_round}, "
            f"routed={[s['routed'] for s in round_shapes]}, "
            f"est_fab=${d.estimated_fab_usd:.2f}, "
            f"expansion={d.expansion_status}"
            + (", budget exhausted" if d.budget_exhausted else "")
            + (f", gate refused round(s) {refused_rounds}" if refused_rounds else "")
        )
        if committed_round != loop_result.best_round:
            best_shape = round_shapes[loop_result.best_round]
            loop_line += (
                f"\nroute-loop: best round {loop_result.best_round} needs a "
                f"{best_shape['outline_mm']} mm outline on {best_shape['layers']} "
                "layers — a board shape this job cannot write; its expansions "
                "are proposals in meta.last_route.loop.proposed_expansions "
                "(apply the outline/stackup edit, then re-run)"
            )
        digest_text = conflict_digest_text(loop_result)
    else:
        loop_line = ""
        assert result is not None
        digest_text = digest_toon(result)
    groups_line = ""
    if result is not None and result.support_groups:
        groups_line = f"support groups: {'; '.join(result.support_groups)}\n"
    snapshot_pins = pcb_resolved.snapshot_pins(ir, declared)
    n_rebound = sum(1 for e in snapshot_pins if e["net"] != e["declared"])
    # The profile's measured effect (docs/backlog/pcb-board-profile-
    # prototype.md item 4): realized vias under a part's courtyard,
    # counted on the same geometry the router's soft cost pays against.
    vias_under_part = pcb_drc.vias_in_courtyard_count(
        [(v.x, v.y) for v in rres.vias],
        world_courtyard_polygons(
            ir,
            clearance_mm=COURTYARD_CLEARANCE_MM,
            fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
        ),
    )
    attempt_summary = (
        f"{placement_summary}"
        f"routed {len(rows)} net(s): {n_realized} realized, "
        f"{n_failed} failed, {n_dangling} dangling (<2-member, nothing to "
        f"route), {len(rres.vias)} via(s) placed, "
        f"{len(rres.warnings)} congestion warning(s), "
        f"{n_rebound} pin(s) bound off their declared net, "
        f"{n_placement_errors} pre-route DRC error(s), "
        f"{len(drc_fail)} net(s) stripped by post-route DRC, "
        f"{n_disconnected} net(s) failed as disconnected copper"
        f"{match_summary}{pin_swap_summary}{loop_line}\n"
        f"vias_under_part: {vias_under_part}\n\n" + groups_line + digest_text
    )

    if worse:
        refusal = (
            "pcb_route: attempt left the board worse than it found it ("
            + "; ".join(worse)
            + ") — nothing committed; the board keeps its previous "
            "placement, copper and route statuses"
        )
        ctx.append_chunk("job_summary", f"{refusal}\n\n{attempt_summary}")
        ctx.record_failure(refusal, failure_class="non-convergence")
        return

    # The anneal's settled planes, netlist snapshot and poses are written only
    # now, after realize, the post-route gate AND the worse-board guard
    # have all passed: written earlier, a raise or a refusal left the
    # previous run's copper under parts that had already moved — a board
    # worse than the one the job started from. On any refusal above, the
    # stored board is untouched.
    ctx.store.pcb_planes_replace_derived(pcb_ref_id, int(board_id), derived_assignments)
    ctx.store.pcb_netlist_snapshot_write(
        int(board_id),
        content_hash=pcb_session.content_hash(authored, {}),
        pins=snapshot_pins,
        prior=pcb_resolved.encode_prior(match_prior),
        meta={"source": "pcb_route", "warm_start": warm_start},
    )
    # Rip before the pose write: a crash between the two then leaves the old
    # poses with that copper unrouted (legal), never moved parts on it.
    n_ripped = ctx.store.pcb_fixed_copper_retire_ids(int(board_id), sorted(ripped))
    ctx.store.pcb_set_pose(pcb_ref_id, pcb_session.positions(ir))
    if result is not None and any(spec.metric == "align" for spec in measures):
        ctx.store.pcb_measures_mark_snapped(pcb_ref_id, result.snapped)
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

    last_route: dict[str, Any] = {
        "warm_start": warm_start_report,
        "iters": iters,
        "realized": n_realized,
        "failed": n_failed,
        "dangling": n_dangling,
        "vias": len(rres.vias),
        "warnings": [w.message() for w in rres.warnings][:20],
        "vector": (rres.vector.line() if rres.vector else None),
        # Per-segment failure kinds (gr503666) — the machine-readable half
        # of the summary's unrouted lines; the full message lives on each
        # net's pcb_routes row.
        "unrouted_reasons": {
            pcb_session.segment_key(ir, r.seg_id): r.kind for r in rres.unrouted_reasons
        },
        "class_layer_drops": list(rres.class_layer_drops),
        "pre_route_drc": {
            "errors": n_placement_errors,
            "forced": force and n_placement_errors > 0,
        },
        # The worse-board guard's own scoreboard (gr480010): what
        # the incumbent measured before the anneal vs what this
        # attempt would have stored — the numbers a later reader
        # needs to see why the commit was allowed.
        "guard": {
            "unrealized_before": prior_unrealized,
            "unrealized_after": n_unrealized,
            "drc_errors_before": prior_drc_errors,
            "drc_errors_after": n_new_drc_errors,
        },
    }
    if loop_result is not None:
        d = loop_result.digest
        # The loop's accepted priced expansions, read as the per-round
        # delta of the two axes the moves act on (outline bbox area and
        # copper-layer count; undo only ever happens inside one anneal),
        # priced on the same fab-table basis the engine used. They are
        # PROPOSALS: this job never writes an outline or a stackup (see
        # the committed-round comment at the loop), so each carries the
        # round it first appeared in and the routed count that shape
        # bought — the evidence for the design edit that would apply it.
        proposed: list[dict[str, Any]] = []
        for i in range(1, len(round_shapes)):
            prev, cur = round_shapes[i - 1], round_shapes[i]
            if cur["layers"] > prev["layers"]:
                proposed.append(
                    {
                        "kind": "layer_add",
                        "round": i,
                        "routed": cur["routed"],
                        "layers": cur["layers"],
                        "fab_usd_delta": round(2.0 * float(config.cost.layer_usd), 4),
                    }
                )
            if prev["outline_mm"] and cur["outline_mm"]:
                area_delta = cur["outline_mm"][0] * cur["outline_mm"][1] - (
                    prev["outline_mm"][0] * prev["outline_mm"][1]
                )
                if area_delta > 1e-6:
                    proposed.append(
                        {
                            "kind": "outline_grow",
                            "round": i,
                            "routed": cur["routed"],
                            "outline_mm": cur["outline_mm"],
                            "fab_usd_delta": round(
                                area_delta * float(config.cost.board_area_usd_per_mm2),
                                4,
                            ),
                        }
                    )
        last_route["loop"] = {
            "rounds_run": d.rounds_run,
            "best_round": loop_result.best_round,
            "committed_round": committed_round,
            "refused_rounds": refused_rounds,
            "complete": d.complete,
            "expansion_status": d.expansion_status,
            "estimated_fab_usd": round(d.estimated_fab_usd, 4),
            "budget_exhausted": d.budget_exhausted,
            "unrouted_nets": list(d.unrouted_nets),
            "rounds": round_shapes,
            "proposed_expansions": proposed,
            "digest": conflict_digest_text(loop_result),
        }
    ctx.store.pcb_set_pose(
        pcb_ref_id,
        {},  # positions already written above; this call is meta-only
        meta={"last_route": last_route},
    )
    ctx.append_chunk(
        "job_summary",
        pcb_session.ripup_note(n_ripped, fixed_copper_stored, ripped) + attempt_summary,
    )
    # Reto's ruling (2026-10-01): "it's no good if the wires are not there.
    # You may show it to me but it is failed." So ANY failed net fails the
    # job — after the copper, statuses and summary above are written, so the
    # partial board stays inspectable (view='svg'/'drc'/'route-status').
    # Before this, a run that realized 8 of 89 nets ended SUCCEEDED.
    if n_failed:
        failed_names = sorted(n for n, r in rows.items() if r.get("status") == "failed")
        shown = ", ".join(failed_names[:10])
        more = f" (+{len(failed_names) - 10} more)" if len(failed_names) > 10 else ""
        ctx.record_failure(
            f"pcb_route: {n_failed} of {len(rows)} net(s) not realized: "
            f"{shown}{more} — partial copper is written; see "
            "view='route-status' for each net's reason",
            failure_class="non-convergence",
        )


def _graph_net_rules(graph: dict[str, Any], fab_caps: Any) -> dict[str, NetRules]:
    """Per-net resolved rules for the post-route clearance check — built the
    way ``handlers/pcb.py::_drc_run`` builds them (clearance is the only
    field ``check_clearance`` reads, so the layer flag is arbitrary)."""
    net_classes = graph.get("net_classes") or {}
    return {
        str(n["name"]): resolve_net_rules(
            str(n.get("net_class") or ""),
            layer_is_outer=True,
            fab_caps=fab_caps,
            overrides=net_classes.get(n.get("net_class") or ""),
            current_a=n.get("est_current_a"),
        )
        for n in graph.get("nets") or []
    }


def _graph_net_voltages(graph: dict[str, Any]) -> dict[str, float]:
    """Annotated nets only — a missing ``working_voltage_v`` is "not
    annotated", never 0 V (``_drc_run``'s same rule)."""
    return {
        str(n["name"]): float(n["working_voltage_v"])
        for n in graph.get("nets") or []
        if n.get("working_voltage_v") is not None
    }


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

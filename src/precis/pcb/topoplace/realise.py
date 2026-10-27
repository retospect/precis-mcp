"""Phase 4 — the topo realiser (spec §5).

Turns the 2b wires (edge sequences on the mesh, taut ``points`` per
branch) into copper: a per-branch layer assignment, via sites as
``(edge_id, t)`` positions, and ``RealizedTrack``/``RealizedVia`` rows
emitted through the normal :mod:`precis.pcb.realize` store path —
never a second copper shape.

The layer assignment is the minimal slice-5 form of phase-1's
colouring, not a separate engine: pad sides fix wire ends (Decision
6 — a surface pad's mount side stays the terminal layer, TH pads are
free), the class ``layers`` rule narrows candidates
(:attr:`TopoDesign.net_layer_force`), and same-layer crossings are
found off the 2b ``order`` lists exactly as Decision 3 defines them —
two wires on one layer cross iff their relative order differs between
two edges both cross. The yielder at a crossing follows the Decision-6
table (TH net, then an F↔B pair, then an SMD-only net): the piece of
its wire spanning the shared edges flips to the other outer layer and
two via sites — one at each end's ``(edge_id, t)`` crossing parameter —
bracket it. A crossing that survives the flip bound is reported on
:attr:`RoutePlan.crossings` for the caller to count as DRC, not hidden.

Via SITING is keepout-aware (topo S3, gr520901): a site is never minted
on a constrained ring edge or within a barrel's keep-out of an endpoint
vertex — the walk moves along the branch's crossed edges to the first
whose pad-channel record (:attr:`Wires.gap_free_mm`) leaves the via plus
its clearance on both outer layers, clears every pad polygon on those
layers by the barrel's keep-out (the channel record is trace-buffered —
a site "free" for a 0.15 mm wire can still drill a solder land), and
does not crowd an already placed via. A branch with no qualifying edge
mints no via; the plan records it on :attr:`RoutePlan.no_via` so the
caller reports the net ``failed: no via site`` instead of committing
keepout-breaking copper.

``net_rules`` keys net NAME → resolved :class:`~precis.pcb.rules.NetRules`
(the caller resolves per-net width/clearance the same way
``build_region_graph``'s consumers do); ``None`` takes the 2b default
pair, matching ``build_wires``' own fallback.

:func:`widen_power` is the ruling's post-route stage: GND/power tracks
widen where clearance allows, never below the net's rule width. The
clearance question is answered by an injected oracle — the caller's own
DRC over the emitted copper — so "allows" means exactly what the gate
accepts, not a second geometric estimate that could drift.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.pcb.rules import NetRules
from precis.pcb.topoplace.design import TopoDesign, _world_to_local
from precis.pcb.topoplace.mesh import TopoMesh
from precis.pcb.topoplace.wires import (
    CorridorOverflow,
    Unreachable,
    WirePath,
    Wires,
)

_DEFAULT = NetRules(track_width_mm=0.15, clearance_mm=0.15)

#: Fallback via geometry when no fab/net rule resolves one — the same
#: order of magnitude the fab floor uses; the store path always passes
#: resolved rules, so this only ever serves synthetic designs.
_VIA_DIA_MM = 0.6
_VIA_DRILL_MM = 0.3

#: Flip bound — a crossing the yielder can't resolve inside this many
#: passes is left on :attr:`RoutePlan.crossings` (bounded like shove:
#: a cap, not the mechanism).
_MAX_FLIPS = 200

#: The widen ladder, descending — the first step the DRC oracle accepts
#: wins. Uniform per net (a rail's tracks share one width, what the fab
#: and the reader both expect); 1.0 mm caps it — a rail wider than that
#: is pour territory, and the ruling is no pours.
_WIDEN_LADDER: tuple[float, ...] = (1.0, 0.8, 0.6, 0.5, 0.4, 0.35, 0.3, 0.25, 0.2)


@dataclass(frozen=True, slots=True)
class RoutePiece:
    """One branch's contiguous run on one layer — a track's polyline."""

    net: int  # net_pins index
    branch: int  # index into Wires.branches
    layer: int
    points: tuple[tuple[float, float], ...]
    #: The branch's endpoint pins (IR pin ids) — a ``pcb_routes`` sketch
    #  row keys a connection by its two pins, never by branch index.
    pin_a: int = -1
    pin_b: int = -1


@dataclass(frozen=True, slots=True)
class ViaSite:
    """A via at ``(edge, t)`` on the mesh — world ``(x, y)`` sits on an
    edge the wire crosses, at or near the taut wire's own crossing point
    (the siting walk may move it to a keepout-clean neighbour edge and
    clamps ``t`` off the endpoint vertices — gr520901)."""

    net: int
    branch: int
    edge: int
    t: float
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """Copper intent for one routed pose: layer-assigned pieces, via
    sites, and the diagnostics the summary/DRC read."""

    pieces: tuple[RoutePiece, ...]
    vias: tuple[ViaSite, ...]
    layers: tuple[int, ...]  # primary (first-piece) layer per branch
    crossings: tuple[tuple[int, int], ...] = ()  # unresolved same-layer pairs
    unreachable: tuple[int, ...] = ()  # net_pins indices with stranded comps
    #: The stranded components verbatim (:class:`Unreachable` records —
    #: stranded pins + walling owners), so a caller's failure note can
    #: name WHAT is cut off, not just the net. ``unreachable`` stays the
    #: net-level digest of this same list.
    stranded: tuple[Unreachable, ...] = ()
    #: Branches whose layer join found no keepout-clean via site —
    #: minted no via, emitted their pieces anyway (the failed net's
    #: honest geometry), and land on the caller's ``pcb_routes`` row as
    #: ``failed: no via site`` (gr520901).
    no_via: tuple[int, ...] = ()
    #: Corridor edges whose routed demand exceeded capacity (gr520904).
    #: Always 0 on a returned plan — a nonzero cut refuses the route in
    #: :func:`route_design` (:class:`CorridorOverflow`), so this is the
    #: bench's "the corridor did not overflow" number.
    corridor_overflow: int = 0

    @property
    def n_routed_nets(self) -> int:
        return len({p.net for p in self.pieces})


def _pin_forced(d: TopoDesign, pin: int, n_layers: int) -> int | None:
    """The outer layer a pad's mount side fixes (SMD), or ``None`` for a
    through-hole pad — present on every layer, it forces nothing."""
    side = int(d.pin_side[pin])
    if side == 0:
        return 0
    if side == 1:
        return n_layers - 1
    return None


def _initial_layers(d: TopoDesign, wires: Wires, n_layers: int) -> list[int]:
    """Per-branch start layer: both ends forcing the same side wins it;
    an F↔B pair takes pin_a's side (the free via lands mid-branch);
    anything free takes the lowest candidate the class rule allows."""
    out: list[int] = []
    outers = {0, n_layers - 1}
    for b in wires.branches:
        la = _pin_forced(d, b.pin_a, n_layers)
        lb = _pin_forced(d, b.pin_b, n_layers)
        cand = (
            set(wires.net_layers[b.net]) & outers
            if b.net < len(wires.net_layers)
            else set(outers)
        )
        if la is not None:
            out.append(la)
        elif lb is not None:
            out.append(lb)
        else:
            out.append(min(cand) if cand else 0)
    return out


def _crossing_pairs(wires: Wires) -> set[tuple[int, int]]:
    """Branch pairs whose relative order flips between two shared edges
    (Decision 3's list comparison). Same-net pairs are kept — the
    caller filters, a braid of the same net is not a short but two
    same-net wires can still cross on the board's geometry."""
    seen: dict[tuple[int, int], int] = {}
    crossed: set[tuple[int, int]] = set()
    for seq in wires.order:
        pos = {b: i for i, b in enumerate(seq)}
        items = sorted(pos)
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                key = (a, b)
                sign = pos[a] - pos[b]
                if key in seen and seen[key] * sign < 0:
                    crossed.add(key)
                else:
                    seen[key] = sign
    return crossed


def _yield_rank(d: TopoDesign, b: Any, n_layers: int) -> tuple[int, int]:
    """Decision 6's yielder order: a branch with a TH end (free — 0
    vias), then an F↔B SMD pair (moves its via — 1), then SMD-only
    (2 vias). Lower rank yields."""
    sides = (int(d.pin_side[b.pin_a]), int(d.pin_side[b.pin_b]))
    if 2 in sides:
        return (0, b.pin_a + b.pin_b)
    if sides[0] != sides[1]:
        return (1, b.pin_a + b.pin_b)
    return (2, b.pin_a + b.pin_b)


def assign_layers(
    d: TopoDesign, wires: Wires, n_layers: int
) -> tuple[list[int], tuple[tuple[int, int], ...]]:
    """Per-branch outer layer after crossing resolution: greedily flip
    the Decision-6 yielder to the other outer layer until no same-layer
    crossing remains, keeping the fewest-crossing state seen. Returns
    ``(layers, remaining crossing branch pairs)``."""
    branches = wires.branches
    layers = _initial_layers(d, wires, n_layers)
    cross = _crossing_pairs(wires)

    def live(ls: list[int]) -> set[tuple[int, int]]:
        return {
            (a, b)
            for a, b in cross
            if branches[a].net != branches[b].net and ls[a] == ls[b]
        }

    best, best_n = list(layers), len(live(layers))
    outers = (0, n_layers - 1)
    for _ in range(_MAX_FLIPS):
        cur = live(layers)
        if not cur:
            break
        a, b = min(cur)
        y = (
            a
            if _yield_rank(d, branches[a], n_layers)
            <= _yield_rank(d, branches[b], n_layers)
            else b
        )
        layers[y] = outers[1] if layers[y] == outers[0] else outers[0]
        n = len(live(layers))
        if n < best_n:
            best, best_n = list(layers), n
        if n >= len(cur):  # flip made things no better — oscillation
            break
    return best, tuple(sorted(live(best)))


def _edge_xy(mesh: TopoMesh, edge: int, t: float) -> tuple[float, float]:
    """World point at parameter ``t`` along mesh edge ``edge``."""
    u, v = mesh.edges[edge]
    a, b_ = mesh.verts[int(u)], mesh.verts[int(v)]
    return (float(a[0] + t * (b_[0] - a[0])), float(a[1] + t * (b_[1] - a[1])))


#: A candidate via site's per-outer-layer pad roster: ``(part index,
#: pin-local centre x/y, half w, half h)`` rows, read back into the part
#: frame per candidate (:func:`design._world_to_local` — the inverse of
#: the pose transform, so the rect distance is exact, mirror included).
_Pads = tuple[tuple[int, float, float, float, float], ...]


def _pad_clear(
    x: float, y: float, pads: _Pads, pose: np.ndarray, margin: float
) -> bool:
    """Whether the point keeps ``margin`` off every pad rect in ``pads``
    — the via barrel's keep-out against pad polygons, which
    ``gap_free_mm`` cannot see: its reach is buffered for a trace, not
    a barrel, and −1 ("no pad reaches the edge for a trace") still
    admits a site inside a pad's via keep-out (gr520901's esp32c3
    residual)."""
    for part, px, py, hw, hh in pads:
        lx, ly = _world_to_local(x, y, pose[part])
        dx = max(abs(lx - px) - hw, 0.0)
        dy = max(abs(ly - py) - hh, 0.0)
        if math.hypot(dx, dy) < margin:
            return False
    return True


def _site_via(
    mesh: TopoMesh,
    wires: Wires,
    b: WirePath,
    ei: int,
    n_layers: int,
    via_dia: float,
    clearance: float,
    placed: list[tuple[float, float, float]],
    pad_sets: tuple[_Pads, _Pads],
    pose: np.ndarray,
) -> tuple[int, float, float, float] | None:
    """One keepout-clean via site on the branch (topo S3, gr520901), or
    ``None`` when no crossed edge qualifies.

    Candidate edges are the branch's crossed edges nearest the intended
    join index ``ei`` first — an end via walks inward off its pad, a mid
    via off its mid edge, either way the smallest move that clears the
    keepout. An edge qualifies when it is not a constrained ring edge,
    its pad-channel record leaves ``via_dia + 2·clearance`` free on BOTH
    outer layers (``gap_free_mm`` −1 = no pad reaches the edge — open),
    and it is long enough to hold the barrel ``via_dia/2 + clearance``
    off both endpoint vertices (the wire's own ``t`` is clamped into
    that window — the graze-at-a-vertex case that minted vias at
    ``t <= eps``). The chosen point must then clear every pad rect on
    the two outer layers by ``via_dia/2 + clearance`` and sit
    ``(dia+dia)/2 + clearance`` off every already placed via; failing
    either, the walk continues."""
    outers = (0, n_layers - 1)
    need = via_dia + 2.0 * clearance
    margin = via_dia / 2.0 + clearance
    n = len(b.edges)
    for j in sorted(range(n), key=lambda k: (abs(k - ei), ei - k)):
        e = int(b.edges[j])
        if mesh.constrained[e]:
            continue
        free = wires.gap_free_mm[e]
        if any(0.0 <= float(free[l]) < need for l in outers):
            continue
        a, b_ = (mesh.verts[int(v)] for v in mesh.edges[e])
        edge_mm = math.dist((float(a[0]), float(a[1])), (float(b_[0]), float(b_[1])))
        if edge_mm <= 2.0 * margin:
            continue
        t = float(b.t[j]) if j < len(b.t) else 0.5
        t = min(max(t, margin / edge_mm), 1.0 - margin / edge_mm)
        xy = _edge_xy(mesh, e, t)
        if any(
            math.dist(xy, (px, py)) < (via_dia + pd) / 2.0 + clearance
            for px, py, pd in placed
        ):
            continue
        if not all(_pad_clear(xy[0], xy[1], s, pose, margin) for s in pad_sets):
            continue
        return e, t, xy[0], xy[1]
    return None


def plan_route(
    d: TopoDesign,
    mesh: TopoMesh,
    wires: Wires,
    n_layers: int,
    *,
    net_rules: Mapping[str, NetRules] | None = None,
    pose: Any = None,
) -> RoutePlan:
    """Layer assignment → pieces + via sites for one routed pose.

    Joins per branch: an F↔B SMD pair splits mid-branch onto its two
    forced layers (the free via, Decision 6); a forced end that disagrees
    with the assigned layer (a crossing yielded the branch to the other
    side) gets an end via at that end's first/last edge so the pad's
    mount side stays the terminal layer. Each join's site comes from
    :func:`_site_via`'s keepout walk — ``net_rules`` (net NAME →
    :class:`NetRules`, as :func:`realize_result` resolves them) sizes
    the barrel; a join with no qualifying edge mints no via and lands on
    :attr:`RoutePlan.no_via`."""
    layers, crossings = assign_layers(d, wires, n_layers)
    rules = net_rules or {}
    pose_a = np.asarray(d.pose0 if pose is None else pose)
    # pad rosters per outer layer: top-SMD pads live on layer 0, bottom
    # on n−1, TH (and anything unclassed) on both — the via barrel spans
    # the board, so both lists gate every site.
    pad_sets: list[list[tuple[int, float, float, float, float]]] = [[], []]
    if len(d.pin_wh):
        for p in range(len(d.pin_xy)):
            part = int(d.pin_part[p])
            if part >= len(pose_a) or not np.isfinite(pose_a[part, :2]).all():
                continue
            w, h = (float(v) for v in d.pin_wh[p])
            row = (
                part,
                float(d.pin_xy[p][0]),
                float(d.pin_xy[p][1]),
                w / 2.0,
                h / 2.0,
            )
            side = int(d.pin_side[p])
            if side == 0:
                pad_sets[0].append(row)
            elif side == 1:
                pad_sets[1].append(row)
            else:
                pad_sets[0].append(row)
                pad_sets[1].append(row)
    pieces: list[RoutePiece] = []
    vias: list[ViaSite] = []
    placed: list[tuple[float, float, float]] = []
    no_via: list[int] = []
    for bi, b in enumerate(wires.branches):
        L = layers[bi]
        la = _pin_forced(d, b.pin_a, n_layers)
        lb = _pin_forced(d, b.pin_b, n_layers)
        # (edge_seq_idx,) joins and the layer each resulting piece takes
        joins: list[int] = []
        run_layers = [L]
        if la is not None and lb is not None and la != lb and len(b.edges) >= 2:
            joins = [len(b.edges) // 2]
            run_layers = [la, lb]
        elif la is not None and la != L and len(b.edges) >= 1:
            joins = [0]
            run_layers = [la, L]
        elif lb is not None and lb != L and len(b.edges) >= 1:
            joins = [len(b.edges) - 1]
            run_layers = [L, lb]
        if not joins:
            pieces.append(
                RoutePiece(b.net, bi, run_layers[0], b.points, b.pin_a, b.pin_b)
            )
            continue
        name = str(d.net_name[b.net]) if b.net < len(d.net_name) else str(b.net)
        nr = rules.get(name, _DEFAULT)
        via_dia = float(nr.via_dia_mm or _VIA_DIA_MM)
        clearance = float(nr.clearance_mm)
        pts = list(b.points)
        cut_xy: list[tuple[float, float]] = []
        sited = True
        for ei in joins:
            site = _site_via(
                mesh,
                wires,
                b,
                ei,
                n_layers,
                via_dia,
                clearance,
                placed,
                (tuple(pad_sets[0]), tuple(pad_sets[1])),
                pose_a,
            )
            if site is None:
                sited = False
                # keep the split at the intended crossing: the pieces
                # still describe where the join was wanted — it is the
                # via that is not minted.
                e = int(b.edges[min(ei, len(b.edges) - 1)])
                t = float(b.t[ei]) if ei < len(b.t) else 0.5
                cut_xy.append(_edge_xy(mesh, e, t))
                continue
            e, t, x, y = site
            vias.append(ViaSite(b.net, bi, e, t, x, y))
            placed.append((x, y, via_dia))
            cut_xy.append((x, y))
        if not sited:
            no_via.append(bi)
        # split the polyline at the vertex nearest each via site — the
        # taut path crosses that edge at exactly the via point
        split = sorted(
            min(range(len(pts)), key=lambda i: math.dist(pts[i], xy)) for xy in cut_xy
        )
        bounds = [0, *split, len(pts) - 1]
        for pi in range(len(bounds) - 1):
            lo, hi = bounds[pi], bounds[pi + 1]
            run = list(pts[lo : hi + 1])
            if pi > 0:
                run.insert(0, cut_xy[pi - 1])
            if pi < len(cut_xy):
                run.append(cut_xy[pi])
            if len(run) >= 2:
                pieces.append(
                    RoutePiece(b.net, bi, run_layers[pi], tuple(run), b.pin_a, b.pin_b)
                )
    unrouted = tuple(sorted({u.net for u in wires.unreachable}))
    return RoutePlan(
        pieces=tuple(pieces),
        vias=tuple(vias),
        layers=tuple(layers),
        crossings=crossings,
        unreachable=unrouted,
        stranded=wires.unreachable,
        no_via=tuple(no_via),
    )


def _octilinear(
    points: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    """The piece's taut path re-drawn in multiples of 45° — the DRC's
    rectilinear rule admits no other angle (``every drawn wire must be
    a multiple of 45deg``). Each off-angle leg becomes an L bend
    (horizontal leg first); the homotopy class is the wire's, not the
    bend's, so a bend may sit tighter than the straight line — the
    corridor widths priced that margin."""
    out: list[tuple[float, float]] = [(float(points[0][0]), float(points[0][1]))]
    for p, q in itertools.pairwise(points):
        dx, dy = q[0] - p[0], q[1] - p[1]
        if dx == 0 or dy == 0 or abs(abs(dx) - abs(dy)) <= 1e-9:
            out.append((float(q[0]), float(q[1])))
        else:
            out.append((float(q[0]), float(p[1])))
            out.append((float(q[0]), float(q[1])))
    return tuple(out)


def realize_result(
    plan: RoutePlan,
    d: TopoDesign,
    ir: Any,
    n_layers: int,
    *,
    net_rules: Mapping[str, NetRules] | None = None,
) -> Any:
    """The plan as a :class:`precis.pcb.realize.RealizeResult` — the one
    shape the store path, ``result_copper_rows`` and the DRC gate all
    read. ``ir`` maps the design's ``net_name`` back to ir net ids."""
    from precis.pcb.realize import (
        RealizedTrack,
        RealizedVia,
        RealizeResult,
        through_span,
    )

    ir_net = {str(n): i for i, n in enumerate(ir.net_name)}
    tracks: list[RealizedTrack] = []
    vias: list[RealizedVia] = []
    rules = net_rules or {}
    for p in plan.pieces:
        name = str(d.net_name[p.net]) if p.net < len(d.net_name) else str(p.net)
        nr = rules.get(name, _DEFAULT)
        pts = _octilinear(p.points)
        segs = tuple(
            {
                "shape": "line",
                "start": [float(x0), float(y0)],
                "end": [float(x1), float(y1)],
            }
            for (x0, y0), (x1, y1) in itertools.pairwise(pts)
        )
        tracks.append(
            RealizedTrack(
                seg_id=p.branch,
                net_id=ir_net[name],
                layer=p.layer,
                segments=segs,
                length_mm=sum(
                    math.hypot(x1 - x0, y1 - y0)
                    for (x0, y0), (x1, y1) in itertools.pairwise(pts)
                ),
                blocked_by=None,
                width_mm=float(nr.track_width_mm),
            )
        )
    lo, hi = through_span(n_layers)
    for v in plan.vias:
        name = str(d.net_name[v.net]) if v.net < len(d.net_name) else str(v.net)
        nr = rules.get(name, _DEFAULT)
        vias.append(
            RealizedVia(
                seg_id=v.branch,
                net_id=ir_net.get(name, -1),
                x=v.x,
                y=v.y,
                dia_mm=float(nr.via_dia_mm or _VIA_DIA_MM),
                drill_mm=float(nr.via_drill_mm or _VIA_DRILL_MM),
                layer_lo=lo,
                layer_hi=hi,
                endpoint="a",
            )
        )
    return RealizeResult(tuple(tracks), tuple(vias), ())


def route_design(
    d: TopoDesign,
    ir: Any,
    *,
    pose: Any = None,
    net_rules: Mapping[str, NetRules] | None = None,
) -> tuple[RoutePlan, Any]:
    """Mesh → wires → plan → copper for one pose — the route half every
    caller (the ``placer='topo'`` job, ``pcb.local.topo_place``) shares.

    ``pose`` defaults to ``d.pose0``: a routing design built with
    ``design_from_ir(route_planes=True)`` over an IR whose poses were
    already settled reads it straight. A caller holding the anneal's
    ``best_pose`` (``pcb.local``) passes it — the mesh is rebuilt at the
    routed pose, not the authored one. ``net_rules`` feeds BOTH the wire
    corridor widths and the emitted track widths, per net name — pass
    the ROUTE-stage resolution
    (:func:`precis.pcb.session.route_net_rules`), whose current-derived
    IPC-2221 widths are dropped: those are ``widen_power`` targets, not
    routing demands.

    The emitted :class:`RealizeResult` DROPS every branch an unresolved
    same-layer crossing still names (:attr:`RoutePlan.crossings`): that
    copper is a real short, never committable — the plan keeps reporting
    it so the caller can fail the net, but no caller may persist or gate
    on it."""
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import build_wires

    mesh_pose = d.pose0 if pose is None else pose
    mesh = build_mesh(d, mesh_pose)
    rules = net_rules or {}
    rules_t = tuple(
        (
            float(rules[d.net_name[k]].track_width_mm),
            float(rules[d.net_name[k]].clearance_mm),
        )
        if k < len(d.net_name) and d.net_name[k] in rules
        else (_DEFAULT.track_width_mm, _DEFAULT.clearance_mm)
        for k in range(len(d.net_pins))
    )
    wires = build_wires(mesh, d, ir.n_layers, rules_t, pose=mesh_pose)
    if wires.overflow:
        # honest refusal (gr520904): a channel whose demand exceeds
        # capacity is named with its owners — never silently crammed.
        raise CorridorOverflow(wires.overflow)
    plan = plan_route(d, mesh, wires, ir.n_layers, net_rules=net_rules, pose=mesh_pose)
    emit = plan
    if plan.crossings:
        bad = {b for pair in plan.crossings for b in pair}
        emit = dataclasses.replace(
            plan,
            pieces=tuple(p for p in plan.pieces if p.branch not in bad),
            vias=tuple(v for v in plan.vias if v.branch not in bad),
        )
    return plan, realize_result(emit, d, ir, ir.n_layers, net_rules=net_rules)


def sketch_rows(
    plan: RoutePlan,
    d: TopoDesign,
    ir: Any,
    layer_names: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """The ``Store.pcb_routes_write`` rows for one routed plan — one row
    per IR net, keyed by net name, in the ``{tree, topology,
    layer_assign, status, fail, note}`` shape the deleted job wrote.
    ``tree``/``layer_assign`` entries key a connection by its two pins
    (``refdes.pin``, :func:`precis.pcb.session._pin_key`'s convention),
    never by the ephemeral branch index; ``topology`` stays empty — the
    topo placer's layer choice is a piece geometry fact, not a pinned
    side override for a later run to re-apply.

    The status ladder: a <2-member net is ``realized`` with the dangling
    note (legal, nothing to route — the wedge fix the deleted job
    documented); a net carrying a stranded component
    (:attr:`RoutePlan.stranded`) or a branch in an unresolved
    ``crossings`` pair is ``failed`` with ``unrouted`` /
    ``same-layer-crossing`` ``fail.problems`` — the same reason strings
    that job emitted, so ``note``-reading consumers (the check view's
    ``unrouted`` findings, ``view='route-status'``) parse it unchanged;
    a net with emitted pieces — or one a plane layer serves — is
    ``realized``; anything left is ``sketched``. A net absent from the
    route design's ``net_pins`` gets no row — under
    ``design_from_ir(route_planes=True)`` only <2-member nets drop, but
    this never guesses."""
    from precis.pcb import DANGLING_NET_NOTE
    from precis.pcb.ir import net_member_counts
    from precis.pcb.session import _pin_key

    def pin_key(pin_id: int) -> str:
        return _pin_key(
            str(ir.instance_refdes[int(ir.pin_instance[pin_id])]),
            str(ir.pin_label[pin_id]),
        )

    net_by_branch = {p.branch: p.net for p in plan.pieces}
    branch_pair: dict[int, tuple[int, int]] = {}
    for p in plan.pieces:
        branch_pair.setdefault(p.branch, (p.pin_a, p.pin_b))
    stranded_by_net = {u.net: u for u in plan.stranded}
    name_to_k = {str(n): k for k, n in enumerate(d.net_name)}
    member_counts = net_member_counts(ir)
    rows: dict[str, dict[str, Any]] = {}
    for net_id in range(ir.n_nets):
        name = str(ir.net_name[net_id])
        if member_counts.get(net_id, 0) < 2:
            rows[name] = {
                "tree": [],
                "topology": [],
                "layer_assign": [],
                "status": "realized",
                "fail": None,
                "note": DANGLING_NET_NOTE,
            }
            continue
        k = name_to_k.get(name)
        if k is None:
            continue
        branches_k = [b for b, n in net_by_branch.items() if n == k]
        problems: list[dict[str, Any]] = []
        u = stranded_by_net.get(k)
        if u is not None:
            pins = u.pins or (u.pin,)
            problems.append(
                {
                    "reason": "unrouted",
                    "message": "pins walled off: "
                    + ", ".join(pin_key(p) for p in pins),
                }
            )
        for a, b in plan.crossings:
            if net_by_branch.get(a) != k and net_by_branch.get(b) != k:
                continue
            other_branch = b if net_by_branch.get(a) == k else a
            other = str(d.net_name[net_by_branch[other_branch]])
            layer = int(plan.layers[a])
            lname = str(layer_names[layer]) if 0 <= layer < len(layer_names) else "?"
            problems.append(
                {
                    "reason": "same-layer-crossing",
                    "with": other,
                    "layer": layer,
                    "message": f"crosses {other} on {lname}",
                }
            )
        for b in plan.no_via:
            if net_by_branch.get(b) != k or b not in branch_pair:
                continue
            pa, pb = branch_pair[b]
            problems.append(
                {
                    "reason": "no via site",
                    "message": "no keepout-clean edge for a via between "
                    f"{pin_key(pa)} and {pin_key(pb)}",
                }
            )
        if problems:
            status = "failed"
            kinds = sorted({str(p["reason"]) for p in problems})
            details = list(dict.fromkeys(str(p["message"]) for p in problems))
            note = f"failed: {', '.join(kinds)} — " + "; ".join(details[:3])
            if len(details) > 3:
                note += f" (+{len(details) - 3} more)"
        elif branches_k or int(ir.net_plane_layers[net_id]) != 0:
            status, note = "realized", None
        else:
            status, note = "sketched", None
        tree: list[dict[str, str]] = []
        layer_assign: list[dict[str, Any]] = []
        seen: set[str] = set()
        for b in branches_k:
            pa, pb = branch_pair[b]
            ka, kb = pin_key(pa), pin_key(pb)
            key = "|".join(sorted((ka, kb)))
            if key in seen:
                continue
            seen.add(key)
            tree.append({"a": ka, "b": kb})
            layer_assign.append({"a": ka, "b": kb, "layer": int(plan.layers[b])})
        rows[name] = {
            "tree": tree,
            "topology": [],
            "layer_assign": layer_assign,
            "status": status,
            "fail": {"problems": problems} if problems else None,
            "note": note,
        }
    return rows


def widen_power(
    result: Any,
    power_net_ids: Iterable[int],
    *,
    drc_errors: Callable[[Any], int],
    ladder: Sequence[float] = _WIDEN_LADDER,
) -> tuple[Any, dict[int, float]]:
    """The post-route power stage (the ruling): widen GND/power tracks
    where clearance allows, never below the net's rule width.

    ``power_net_ids`` are IR net ids (``RealizedTrack.net_id``);
    ``result``/the oracle traffic in :class:`precis.pcb.realize.
    RealizeResult`. ``drc_errors`` counts error findings on the emitted
    copper — the caller's own DRC, so "clearance allows" is exactly what
    the gate accepts. Baseline is the result AS GIVEN: a widened step is
    kept when its error count does not exceed it (a board already
    carrying errors is not asked to heal them to widen a rail).

    Each net's tracks move together down the ladder — one rail, one
    width. Accepted widths carry into later nets' trials (the first rail
    widened is copper the next must clear). Returns ``(result,
    {net_id: width_mm})`` covering every power net that had tracks —
    widened or not — so the caller can report the delta; a net that
    never beat the oracle keeps its rule width verbatim."""
    base = drc_errors(result)
    tracks = list(result.tracks)
    widths: dict[int, float] = {}
    for net_id in sorted({int(n) for n in power_net_ids}):
        idx = {i for i, t in enumerate(tracks) if int(t.net_id) == net_id}
        if not idx:
            continue
        rule_w = float(tracks[next(iter(idx))].width_mm)
        widths[net_id] = rule_w
        for w in ladder:
            if w <= rule_w:
                break
            trial = tuple(
                dataclasses.replace(t, width_mm=float(w)) if i in idx else t
                for i, t in enumerate(tracks)
            )
            if drc_errors(dataclasses.replace(result, tracks=trial)) <= base:
                tracks, widths[net_id] = list(trial), float(w)
                break
    return dataclasses.replace(result, tracks=tuple(tracks)), widths


__all__ = [
    "RoutePiece",
    "RoutePlan",
    "ViaSite",
    "assign_layers",
    "plan_route",
    "realize_result",
    "route_design",
    "sketch_rows",
    "widen_power",
]

"""Slice 2b — wires as edge sequences on the 2a CDT mesh (spec §2).

A wire carries NO coordinates of its own (Decision 3): it is a sequence
of crossed mesh-edge ids — its homotopy class. What this module adds on
top is the taut representative (the funnel over the triangle corridor),
kept so each crossing sits at a measured parameter ``t`` along its edge.
The recorded corridor is re-derived by walking the taut polyline through
the mesh (:func:`_retrack`), so ``tris``/``edges``/``t`` describe the
geometry, not the search corridor that found it — courtyard and outline
corners are not homotopy obstacles (seam ruling R-a).

Routing passability is 2b's own per-layer rule, NOT the placement
``edge_side`` field (R-a — courtyards never block routing). Exemption
keys on the wire's NET, not the endpoint part (round-2 ruling): the
walls of a pin on the wire's own net are open, a pad of another net is
always a wall — even on the wire's own part:

- an SMD pad blocks its own face: the boundary edges of its vertex fan
  are closed on that outer layer to every wire of another net;
- a drilled pad (TH pin) blocks every layer the same way;
- a keepout/fixed-copper zone edge blocks the zone's ``layers`` — except
  fixed copper of the SAME net, which lets the wire through;
- the board edge never passes.

Per net the pins' tree is Prim over PATH distances (R-b): one persistent
multi-source search grows from the tree; the nearest unreached pin by
geodesic distance attaches next (a fresh attach re-seeds at cost 0, so
the heap key means distance-to-tree, not distance-to-root). A pin boxed
in on every candidate layer is left unplaced; the search then roots a
fresh component at the lowest unplaced pin — a caged root does not drop
the net — and every separated component is reported once on
``Wires.unreachable`` — the rest of the net still connects (the old
Euclidean-MST hub failure reported every downstream branch).

``order`` is Decision 3's per-edge ordered wire list. Crossing
parameters tie wherever taut paths wrap a shared vertex, so tied wires
are ordered by one scalar per (branch, vertex) — the wrap tightness —
so a shared run keeps a single order on every edge; ``t`` is geometry
only.
``capacity_mm`` is the slice-4 corridor input — the edge's free crossing
length after the pad extents at its endpoints are priced off.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass

import numpy as np

from precis.errors import BadInput
from precis.pcb.geom import segments_cross
from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.mesh import (
    OWNER_BOARD,
    OWNER_NONE,
    TopoMesh,
)

_EPS = 1e-9

#: Default design rules for the pad-gap passability check when the
#: caller resolves no per-net ``NetRules`` (synth fixtures, boards
#: without class rows): a 0.15 mm trace and 0.15 mm clearance.
_DEFAULT_TRACE_MM = 0.15
_DEFAULT_CLEAR_MM = 0.15

#: Occupancy pricing strength (gr520904): a channel edge at full
#: corridor capacity costs ``1 + _CONGESTION_ALPHA`` times its step
#: length, so late nets detour instead of piling into the same
#: corridor.
_CONGESTION_ALPHA = 4.0


@dataclass(frozen=True, slots=True)
class WirePath:
    """One pin→pin branch of a net's wire tree (a geodesic-Prim edge
    pulled taut). ``edges`` IS the wire (homotopy class); ``tris`` the
    corridor re-walked under the taut polyline; ``t``/``points`` the
    taut representative used for ordering and geometry."""

    net: int  # net_pins index
    pin_a: int
    pin_b: int
    tris: tuple[int, ...]
    edges: tuple[int, ...]
    t: tuple[float, ...]  # crossing parameter per edge, on edges[e]'s u→v
    points: tuple[tuple[float, float], ...]  # taut polyline, pads at ends

    @property
    def length_mm(self) -> float:
        return sum(math.dist(p, q) for p, q in zip(self.points, self.points[1:]))


@dataclass(frozen=True, slots=True)
class Unreachable:
    """A separated component of a net — no dual path links it to the
    net's main component on any candidate layer. ``pin`` names the
    component's root and ``pins`` every member, so a certificate can
    list a multi-pin cut-off group without re-flooding (round-3
    finding R3-4); ``owners`` names what walls it off, in the
    ``edge_owner`` id space (part id, OWNER_BOARD, or n_parts + zone):
    the colour pass reports it as a certificate input."""

    net: int
    pin: int
    owners: tuple[int, ...]
    pins: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Wires:
    """Every net's wire tree plus the shared-edge order structure the
    crossing graph (``layers.py``) reads. ``order[e]`` is the branch ids
    crossing edge ``e`` in edge-parameter order — ties at a wrapped
    vertex resolved by wrap tightness (:func:`_order`), not by id."""

    branches: tuple[WirePath, ...]
    order: tuple[tuple[int, ...], ...]
    capacity_mm: np.ndarray  # (E,) free crossing length per edge
    net_layers: tuple[frozenset[int], ...]  # candidate outer layers per net
    unreachable: tuple[Unreachable, ...]
    # slice-4 channel record (round-4): per edge and layer, the stretch
    # left uncovered by ringed pads buffered at the finest resolved rule
    # (−1 where no pad reaches the edge), and the TOTAL traces that
    # stretch takes — floor(free / (trace + clearance)) + 1 where a
    # centreline survives, 0 where the channel is closed (round-5:
    # the count is measured in the free width's own frame — a just-
    # passable gap records 1, not 0).
    gap_free_mm: np.ndarray  # (E, L) mm, -1 = not a pad-channel edge
    gap_cap: np.ndarray  # (E, L) int, -1 = not a pad-channel edge
    order_residual: int = 0  # tied-pair order flips _repair_order left over
    #: The demand-vs-capacity cut (spec phase 3, gr520904): edges whose
    #: routed demand exceeds what their corridor takes — the caller
    #: refuses on a non-empty record (``route_design`` raises
    #: :class:`CorridorOverflow`), never silently crams copper through.
    overflow: tuple[CorridorCut, ...] = ()


@dataclass(frozen=True, slots=True)
class CorridorCut:
    """One mesh edge whose routed demand exceeds its corridor capacity —
    the phase-3 cut condition (R11). ``owners`` names what bounds the
    corridor: the refdes of every part whose pads ring or endpoint the
    edge, the name of every contributing zone, ``"outline"`` for the
    board edge."""

    edge: int
    demand: float  # finest-rule trace slots
    cap: float  # total traces the corridor takes (all layers)
    owners: tuple[str, ...]
    x: float  # edge midpoint, world mm
    y: float


class CorridorOverflow(BadInput):
    """``failed: corridor overflow`` — a mesh edge's routed demand
    exceeds its capacity, so the route refuses rather than emitting
    copper that cannot fit (gr520904). Carries the full cut record;
    ``str()`` names the worst edge and its owners."""

    def __init__(self, cuts: tuple[CorridorCut, ...]) -> None:
        self.cuts = cuts
        self.count = len(cuts)
        worst = max(cuts, key=lambda c: c.demand - c.cap)
        super().__init__(
            f"corridor overflow: edge {worst.edge} at "
            f"({worst.x:.2f},{worst.y:.2f}) between {', '.join(worst.owners)} "
            f"— demand {worst.demand:g} wires > capacity {worst.cap:g}"
            + (f" (+{len(cuts) - 1} more edge(s))" if len(cuts) > 1 else ""),
            next=(
                "spread the bounding parts to widen the corridor, reroute "
                "a net around it, or relax the net's design rule — no "
                "copper was written"
            ),
        )


def _net_layers(d: TopoDesign, k: int, n_layers: int) -> frozenset[int]:
    """The net's candidate OUTER layers {0, n−1} — class rule
    (``net_layer_force``) ∩ pad sides (``net_pin_force``). An empty set
    is the inner-only clash the colour pass reports as a certificate;
    the path builder treats it as free so the wire exists to be
    diagnosed."""
    allowed = {0, n_layers - 1}
    forces = (
        int(d.net_layer_force[k]) if len(d.net_layer_force) > k else -1,
        int(d.net_pin_force[k]) if len(d.net_pin_force) > k else -1,
    )
    for force in forces:
        if force >= 0:
            allowed &= {force}
        elif force == -2:
            allowed = set()
    return frozenset(allowed)


@dataclass(slots=True)
class _Ctx:
    """Per-:func:`build_wires` mesh caches — everything the search and
    the re-walk read more than once."""

    n_layers: int
    all_mask: int  # bitmask of every layer
    tri_verts: list[tuple[int, int, int]]
    tri_set: list[frozenset[int]]
    v2t: list[list[int]]  # vertex -> incident triangle ids (ascending)
    fan_boundary: dict[int, frozenset[int]]  # pin -> boundary edges of its fan
    pad_contrib: dict[int, list[tuple[int, int]]]  # edge -> [(pin, mask)]
    zone_contrib: dict[int, list[tuple[int, int]]]  # edge -> [(zone idx, mask)]
    # Ringed-pad rectangles for the gap rule (round-4 rework): only pins
    # whose ``pin_wh`` ring the mesh actually built count — a ringless
    # pad's fan boundary is arbitrary CDT edges, so a box read from it
    # would invent gaps on synth fixtures. The fan boundary IS the pad
    # ring, so its endpoint bounding box is the pad's world rect — read
    # off the mesh, correct under whatever pose built it.
    pad_pin: np.ndarray  # (Rp,) global pin id per ringed pad
    pad_lo: np.ndarray  # (Rp, 2) world AABB lo
    pad_hi: np.ndarray  # (Rp, 2) world AABB hi
    pad_copper: np.ndarray  # (Rp,) int — layers the pad has copper on
    pin_pad: dict[int, int]  # pin -> ringed-pad index
    edge_lo: np.ndarray  # (E, 2) edge AABB lo
    edge_hi: np.ndarray  # (E, 2) edge AABB hi
    seg_u: np.ndarray  # (E, 2) edge u endpoint
    seg_v: np.ndarray  # (E, 2) edge v endpoint
    seg_len: np.ndarray  # (E,) edge length
    pin_seg: np.ndarray  # (E, n_pins) pin vertex -> segment distance
    pad_block_all: np.ndarray  # (E,) int — all pads' blocked-layer OR
    zone_block: dict[str, np.ndarray]  # net name -> (E,) zone-blocked OR
    gap_eval_cache: dict[
        float,
        tuple[np.ndarray, np.ndarray, dict[int, np.ndarray], dict[int, list[int]]],
    ]
    # gap_eval_cache[r] = (blocked (E,) int, free (E, L) mm, edge_pads,
    # pad_edges) — the all-pads coverage eval; _blocked_masks re-opens
    # a net's own pads on top of it.
    board: np.ndarray  # (E,) bool — board-outline edges
    vid_of: dict[tuple[float, float], int]  # rounded xy -> vertex id
    eu: list[tuple[float, float]]  # edge u endpoint xy (python floats)
    ev: list[tuple[float, float]]  # edge v endpoint xy
    edge_ends: list[tuple[int, int]]  # edge -> (u vid, v vid)
    edge_tris: list[tuple[int, int]]  # edge -> (tri, tri) ids, -1 off-mesh
    tri_xy: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]
    tri_edge_list: list[tuple[int, int, int]]  # tri -> its 3 edge ids
    tri_next: list[dict[int, tuple[tuple[int, int, float], ...]]]
    # tri_next[t][e] = ((e2, t2, |mid(e)-mid(e2)|) for the tri's OTHER edges


def _pin_mask(pin_side: int, n_layers: int) -> int:
    """Blocked-layer bitmask for one pad: SMD blocks its own face, a
    drilled (TH) pad blocks every layer."""
    if pin_side == 2:
        return (1 << n_layers) - 1
    return 1 << (n_layers - 1 if pin_side == 1 else 0)


def _make_ctx(mesh: TopoMesh, d: TopoDesign, n_layers: int) -> _Ctx:
    tri_verts = [(int(a), int(b), int(c)) for a, b, c in mesh.tris]
    v2t: list[list[int]] = [[] for _ in range(len(mesh.verts))]
    for t, tri in enumerate(tri_verts):
        for v in tri:
            v2t[v].append(t)
    # Pad fan boundary: the edges opposite the pad vertex in its fan
    # tris. Entering the fan = crossing one of these.
    fan_boundary: dict[int, frozenset[int]] = {}
    pad_contrib: dict[int, list[tuple[int, int]]] = {}
    for p in range(len(mesh.pin_vertex)):
        va = int(mesh.pin_vertex[p])
        mask = _pin_mask(int(d.pin_side[p]), n_layers)
        boundary = frozenset(
            int(e)
            for t in v2t[va]
            for e in mesh.tri_edges[t]
            if va not in mesh.edges[e]
        )
        fan_boundary[p] = boundary
        for e in boundary:
            pad_contrib.setdefault(e, []).append((p, mask))
    # Ringed-pad rectangles (round-4 rework): each pin the mesh built a
    # pin_wh ring for gets its world AABB off the ring corners — the
    # fan boundary's endpoints. Pads without a built ring (pin_wh
    # missing or zero) get NO box: their fan boundary is arbitrary CDT
    # edges and a box read from them invents false gaps on synth.
    pad_pin_l: list[int] = []
    pad_lo_l: list[np.ndarray] = []
    pad_hi_l: list[np.ndarray] = []
    pad_copper_l: list[int] = []
    pin_pad: dict[int, int] = {}
    for p, ring_edges in fan_boundary.items():
        if len(d.pin_wh) <= p:
            continue
        pw, ph = float(d.pin_wh[p, 0]), float(d.pin_wh[p, 1])
        if pw <= _EPS or ph <= _EPS:
            continue
        vids = np.unique(mesh.edges[sorted(ring_edges)].ravel())
        pts = mesh.verts[vids]
        pin_pad[p] = len(pad_pin_l)
        pad_pin_l.append(p)
        pad_lo_l.append(pts.min(axis=0))
        pad_hi_l.append(pts.max(axis=0))
        pad_copper_l.append(_pin_mask(int(d.pin_side[p]), n_layers))
    pad_lo = (
        np.asarray(pad_lo_l, dtype=np.float64).reshape(-1, 2)
        if pad_lo_l
        else np.zeros((0, 2))
    )
    pad_hi = (
        np.asarray(pad_hi_l, dtype=np.float64).reshape(-1, 2)
        if pad_hi_l
        else np.zeros((0, 2))
    )
    n_parts = len(d.parts)
    all_mask = (1 << n_layers) - 1
    zone_contrib: dict[int, list[tuple[int, int]]] = {}
    board = np.zeros(len(mesh.edges), dtype=bool)
    for e in range(len(mesh.edges)):
        for slot in range(2):
            owner = int(mesh.edge_owner[e, slot])
            if owner == OWNER_NONE:
                break
            if owner == OWNER_BOARD:
                board[e] = True
            elif owner >= n_parts:
                z = owner - n_parts
                layers = d.zones[z].layers
                zm = all_mask if not layers else _layer_mask(layers)
                zone_contrib.setdefault(e, []).append((z, zm))
    vid_of = {
        (round(float(x), 6), round(float(y), 6)): i
        for i, (x, y) in enumerate(mesh.verts)
    }
    eu = [(float(xy[0]), float(xy[1])) for xy in mesh.verts[mesh.edges[:, 0]]]
    ev = [(float(xy[0]), float(xy[1])) for xy in mesh.verts[mesh.edges[:, 1]]]
    eu_arr = mesh.verts[mesh.edges[:, 0]]
    ev_arr = mesh.verts[mesh.edges[:, 1]]
    edge_lo = np.minimum(eu_arr, ev_arr)
    edge_hi = np.maximum(eu_arr, ev_arr)
    edge_ends = [(int(a), int(b)) for a, b in mesh.edges]
    edge_tris = [(int(a), int(b)) for a, b in mesh.edge_tris]
    tri_xy = [
        (
            (float(a[0]), float(a[1])),
            (float(b[0]), float(b[1])),
            (float(c[0]), float(c[1])),
        )
        for a, b, c in mesh.verts[mesh.tris]
    ]
    tri_edge_list = [(int(a), int(b), int(c)) for a, b, c in mesh.tri_edges]
    mid_arr = 0.5 * (eu_arr + ev_arr)  # edge midpoints
    # Step cost through a tri (round-4 finding 4 + round-5 residual):
    # plain midpoint-to-midpoint distance everywhere EXCEPT the sliver
    # tris around a ringed pad's ring — the pad-channel tris are the
    # only place the midpoint metric lies (it priced a legal pad gap
    # above the wrap-around detour), and everywhere else the portal
    # metric's segment projection mis-ordered tied wires enough to push
    # the order residual from 6 to 42 on the n=100 ringless fixture. On
    # a marked tri a step is charged the symmetric mid→segment
    # projection — half the wedge altitude for a sliver — so a thin
    # channel costs what the taut path will actually pay.
    # Vectorized: pt→seg distance over the (E, 3) in/out pairs of the
    # per-tri edge lists.
    ecol = np.asarray(tri_edge_list, dtype=np.int64)  # (T, 3)
    mid_e = mid_arr[ecol]  # (T, 3, 2)

    def ptseg_mat(
        pts: np.ndarray, ei: np.ndarray
    ) -> np.ndarray:  # pts (...,2), ei (...,) -> (...,)
        su, sv = eu_arr[ei], ev_arr[ei]
        dd = sv - su
        ddd = np.maximum((dd * dd).sum(axis=-1), _EPS)
        tp = np.clip(((pts - su) * dd).sum(axis=-1) / ddd, 0.0, 1.0)
        return np.linalg.norm(pts - (su + tp[..., None] * dd), axis=-1)

    # wstep[t, i, j] = mid(e_i) -> seg(e_j) distance for the tri's edges
    T = len(ecol)
    pts_grid = np.broadcast_to(mid_e[:, :, None, :], (T, 3, 3, 2)).reshape(T, 9, 2)
    ei_grid = np.broadcast_to(ecol[:, None, :], (T, 3, 3)).reshape(T, 9)
    w_in = ptseg_mat(pts_grid, ei_grid).reshape(T, 3, 3)
    w_out = w_in.transpose(0, 2, 1)
    wstep_portal = 0.5 * (w_in + w_out)
    wstep_mid = np.linalg.norm(mid_e[:, :, None, :] - mid_e[:, None, :, :], axis=-1)
    # ring-neighbourhood tris of ringed pads only — tris sharing a
    # vertex with a fan-boundary edge: the sliver channel around a pad
    # ring, where the portal metric is needed; midpoints elsewhere
    # (round-5 residual)
    fan_tris = np.zeros(T, dtype=bool)
    for p in pin_pad:
        for e in fan_boundary[p]:
            for v in edge_ends[e]:
                for t2 in v2t[v]:
                    fan_tris[t2] = True
    wstep = np.where(fan_tris[:, None, None], wstep_portal, wstep_mid)
    tris_of = np.asarray(edge_tris, dtype=np.int64)  # (E, 2)
    tri_next: list[dict[int, tuple[tuple[int, int, float], ...]]] = []
    for t, es in enumerate(tri_edge_list):
        row: dict[int, tuple[tuple[int, int, float], ...]] = {}
        for i, e_in in enumerate(es):
            nxt = []
            for j, e2 in enumerate(es):
                if e2 == e_in:
                    continue
                t2a, t2b = tris_of[e2]
                t2 = t2a if t2a != t else t2b
                nxt.append((e2, int(t2), float(wstep[t, i, j])))
            row[e_in] = tuple(nxt)
        tri_next.append(row)
    # (E, n_pins) pin-vertex -> segment distance — every net's A*
    # heuristic and seed cost are column slices of this matrix (round-4:
    # rebuilding the rel/proj stack per net dominated _net_tree).
    n_pins_all = len(mesh.pin_vertex)
    if n_pins_all:
        pvv = mesh.verts[np.asarray(mesh.pin_vertex, dtype=np.int64)]
        dd_all = ev_arr - eu_arr
        ddd = np.maximum((dd_all * dd_all).sum(axis=1), _EPS)
        relv = pvv[None, :, :] - eu_arr[:, None, :]
        tpv = np.clip((relv * dd_all[:, None, :]).sum(axis=2) / ddd[:, None], 0.0, 1.0)
        projv = eu_arr[:, None, :] + tpv[:, :, None] * dd_all[:, None, :]
        pin_seg = np.linalg.norm(pvv[None, :, :] - projv, axis=2)
    else:
        pin_seg = np.zeros((len(mesh.edges), 0))
    pad_block_all = np.zeros(len(mesh.edges), dtype=np.int64)
    for e, lst in pad_contrib.items():
        for _pin, pm in lst:
            pad_block_all[e] |= pm
    zone_block: dict[str, np.ndarray] = {}
    return _Ctx(
        n_layers=n_layers,
        all_mask=all_mask,
        tri_verts=tri_verts,
        tri_set=[frozenset(t) for t in tri_verts],
        v2t=v2t,
        fan_boundary=fan_boundary,
        pad_contrib=pad_contrib,
        zone_contrib=zone_contrib,
        pad_pin=np.asarray(pad_pin_l, dtype=np.int64),
        pad_lo=pad_lo,
        pad_hi=pad_hi,
        pad_copper=np.asarray(pad_copper_l, dtype=np.int64),
        pin_pad=pin_pad,
        edge_lo=edge_lo,
        edge_hi=edge_hi,
        seg_u=eu_arr,
        seg_v=ev_arr,
        seg_len=np.hypot(*(ev_arr - eu_arr).T),
        pin_seg=pin_seg,
        pad_block_all=pad_block_all,
        zone_block=zone_block,
        gap_eval_cache={},
        board=board,
        vid_of=vid_of,
        eu=eu,
        ev=ev,
        edge_ends=edge_ends,
        edge_tris=edge_tris,
        tri_xy=tri_xy,
        tri_edge_list=tri_edge_list,
        tri_next=tri_next,
    )


def _layer_mask(layers) -> int:
    m = 0
    for ly in layers:
        m |= 1 << int(ly)
    return m


def _cand_mask(cand: frozenset[int], n_layers: int) -> int:
    """Candidate-layers bitmask; an empty candidate set is the inner-only
    clash the colour pass reports — the path builder treats it as free."""
    return _layer_mask(cand) if cand else (1 | (1 << (n_layers - 1)))


# A 2-vector: python-float pair (the hot paths) or an ndarray row.
_V2 = tuple[float, float] | np.ndarray


def _pt_seg(p: _V2, a: _V2, b: _V2) -> float:
    """Distance from ``p`` to segment ``a→b``."""
    ex, ey = b[0] - a[0], b[1] - a[1]
    dd = ex * ex + ey * ey
    if dd <= _EPS:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = min(1.0, max(0.0, ((p[0] - a[0]) * ex + (p[1] - a[1]) * ey) / dd))
    return math.hypot(p[0] - (a[0] + t * ex), p[1] - (a[1] + t * ey))


def _cross(u: _V2, v: _V2) -> float:
    return float(u[0] * v[1] - u[1] * v[0])


def _area2(a: _V2, b: _V2, c: _V2) -> float:
    """Mononen's triarea2 — positive when ``c`` is right of ``a→b``."""
    return (c[0] - a[0]) * (b[1] - a[1]) - (b[0] - a[0]) * (c[1] - a[1])


def _same(a: _V2, b: _V2) -> bool:
    return math.dist(a, b) <= 1e-9


def _funnel(
    portals: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[tuple[float, float]]:
    """The simple-stupid funnel over oriented ``(left, right)`` portal
    endpoints."""
    apex = portals[0][0]
    goal = portals[-1][0]
    left_v = right_v = apex
    apex_i = left_i = right_i = 0
    path = [apex]
    i = 1
    n = len(portals)
    while i < n:
        lp, rp = portals[i]
        if _area2(apex, right_v, rp) <= 0.0:
            if _same(apex, right_v) or _area2(apex, left_v, rp) > 0.0:
                right_v, right_i = rp, i
            else:
                path.append(left_v)
                apex, apex_i = left_v, left_i
                left_v = right_v = apex
                left_i = right_i = apex_i
                i = apex_i + 1
                continue
        if _area2(apex, left_v, lp) >= 0.0:
            if _same(apex, left_v) or _area2(apex, right_v, lp) < 0.0:
                left_v, left_i = lp, i
            else:
                path.append(right_v)
                apex, apex_i = right_v, right_i
                left_v = right_v = apex
                left_i = right_i = apex_i
                i = apex_i + 1
                continue
        i += 1
    if not _same(path[-1], goal):
        path.append(goal)
    return path


def _taut(
    ctx: _Ctx,
    mesh: TopoMesh,
    tris: list[int],
    edges: list[int],
    va: int,
    vb: int,
) -> list[tuple[float, float]]:
    """The funnel taut polyline over the search corridor."""
    a = (float(mesh.verts[va, 0]), float(mesh.verts[va, 1]))
    b = (float(mesh.verts[vb, 0]), float(mesh.verts[vb, 1]))
    if not edges:
        return [a, b]
    portals: list[tuple[tuple[float, float], tuple[float, float]]] = [(a, a)]
    for i, e in enumerate(edges):
        u, v = ctx.eu[e], ctx.ev[e]
        ends = ctx.edge_ends[e]
        apex = next(
            ctx.tri_xy[tris[i + 1]][j]
            for j, w in enumerate(ctx.tri_verts[tris[i + 1]])
            if w not in ends
        )
        portals.append((u, v) if _area2(u, v, apex) < 0.0 else (v, u))
    portals.append((b, b))
    pts = _funnel(portals)
    return [p for i, p in enumerate(pts) if i == 0 or not _same(p, pts[i - 1])]


def _wedge_tri(ctx: _Ctx, tris, w: int, dxy: tuple[float, float]) -> int | None:
    """The fan tri of ``w`` whose wedge contains direction ``dxy`` — the
    tri a ray from ``w`` along ``dxy`` enters first."""
    for t in sorted(int(x) for x in tris):
        tri = ctx.tri_verts[t]
        if w not in tri:
            continue
        i = tri.index(w)
        xy = ctx.tri_xy[t]
        nxt = (
            xy[(i + 1) % 3][0] - xy[i][0],
            xy[(i + 1) % 3][1] - xy[i][1],
        )
        prv = (
            xy[(i - 1) % 3][0] - xy[i][0],
            xy[(i - 1) % 3][1] - xy[i][1],
        )
        if _cross(nxt, dxy) >= -1e-12 and _cross(dxy, prv) >= -1e-12:
            return t
    return None


def _in_tri(ctx: _Ctx, t: int, p: tuple[float, float]) -> bool:
    a, b, c = ctx.tri_xy[t]
    return (
        _area2(a, b, p) >= -1e-9
        and _area2(b, c, p) >= -1e-9
        and _area2(c, a, p) >= -1e-9
    )


def _tri_exit(
    ctx: _Ctx,
    t: int,
    skip: int,
    p: tuple[float, float],
    q: tuple[float, float],
) -> tuple[str, int, float, float] | None:
    """Where the ray ``p→q`` leaves tri ``t``: ``('edge', e, s, r)`` for
    an interior crossing (s on the stored u→v, r along p→q), or
    ``('vertex', w, 0, r)`` when it exits exactly through vertex ``w``.
    The earliest exit wins; the just-crossed ``skip`` edge is ignored."""
    best: tuple[str, int, float, float] | None = None
    px, py = p
    d_sx, d_sy = q[0] - px, q[1] - py
    for e in ctx.tri_edge_list[t]:
        if e == skip:
            continue
        ux, uy = ctx.eu[e]
        vx, vy = ctx.ev[e]
        ex, ey = vx - ux, vy - uy
        denom = d_sx * ey - d_sy * ex
        if abs(denom) <= _EPS:
            continue
        wx, wy = ux - px, uy - py
        s = (wx * d_sy - wy * d_sx) / denom
        r = (wx * ey - wy * ex) / denom
        if r <= 1e-9 or s < -1e-9 or s > 1.0 + 1e-9:
            continue
        if best is not None and r >= best[3] - 1e-9:
            continue
        if s <= 1e-9:
            best = ("vertex", ctx.edge_ends[e][0], 0.0, r)
        elif s >= 1.0 - 1e-9:
            best = ("vertex", ctx.edge_ends[e][1], 0.0, r)
        else:
            best = ("edge", e, s, r)
    return best


def _fan_ring(ctx: _Ctx, w: int) -> list[tuple[int, int]]:
    """The cyclic fan around interior vertex ``w``: ``(tri, spoke_edge)``
    pairs where ``spoke_edge`` leads to the next fan tri. Deterministic —
    starts at the lowest tri id and takes the lowest valid spoke."""
    fan = ctx.v2t[w]
    if not fan:
        return []
    first = fan[0]
    ring: list[tuple[int, int]] = []
    cur, came = first, -1
    for _ in range(len(fan) + 2):
        nxt = None
        for e in sorted(ctx.tri_edge_list[cur]):
            if e == came or w not in ctx.edge_ends[e]:
                continue
            t2a, t2b = ctx.edge_tris[e]
            t2 = t2a if t2a != cur else t2b
            if t2 >= 0 and w in ctx.tri_set[t2]:
                nxt = (e, t2)
                break
        if nxt is None:
            break
        ring.append((cur, nxt[0]))
        if nxt[1] == first:
            break
        cur, came = nxt[1], nxt[0]
    return ring


def _fan_arc(
    ctx: _Ctx,
    w: int,
    t_in: int,
    d_in: tuple[float, float],
    d_out: tuple[float, float],
    passable=None,
) -> tuple[int, list[tuple[int, int]]]:
    """The fan-arc walk for a vertex-exact passage at ``w``: from entry
    tri ``t_in`` to the wedge holding ``d_out``, sweeping the arc on the
    OUTER side of the bend — the side the search corridor took. A taut
    bend wraps ``w`` away from the chord side; sweeping the turn
    (inner) side would route the wire through walls the corridor
    avoided. When a leg runs ALONG a spoke the wedge picks a side by
    triangle id — wrong half the time (round-2 finding 1): the picks
    are nudged to the outer side so the arc can never sweep back
    across the collinear spoke. A straight pass-through has no outer
    side — keep the corridor on the incoming tri's side and take the
    shorter arc. Returns ``(exit_tri, [(spoke_edge, entered_tri), ...])``."""
    ring = _fan_ring(ctx, w)
    pos_of = {tri: i for i, (tri, _e) in enumerate(ring)}
    if t_in not in pos_of:
        return t_in, []
    ring_tris = [tri for tri, _e in ring]

    def biased(
        d: tuple[float, float], toward: tuple[float, float]
    ) -> tuple[float, float]:
        """``d`` nudged toward ``toward``'s side — breaks a
        spoke-collinear wedge tie to the wanted side, leaves an
        interior direction on its own wedge."""
        l = math.hypot(d[0], d[1])
        if l <= _EPS:
            return d
        px, py = -d[1] / l, d[0] / l
        s = px * toward[0] + py * toward[1]
        if abs(s) <= 1e-12:
            return d
        if s < 0.0:
            px, py = -px, -py
        eps = 1e-9 * l
        return (d[0] + eps * px, d[1] + eps * py)

    # outward bisector: unit(d_in) - unit(d_out) points away from the
    # bend's interior; degenerate (~0) on a straight pass-through
    li = math.hypot(d_in[0], d_in[1])
    lo = math.hypot(d_out[0], d_out[1])
    away: tuple[float, float] | None = None
    if li > _EPS and lo > _EPS:
        ax, ay = d_in[0] / li - d_out[0] / lo, d_in[1] / li - d_out[1] / lo
        if ax * ax + ay * ay > 1e-12:
            away = (ax, ay)
    n = len(ring)

    def arc_to(i_c: int, fwd_dir: bool) -> list[tuple[int, int]]:
        """The swept ``(spoke, entered_tri)`` list from ``t_in`` to the
        wedge at ring index ``i_c``, walking in ``fwd_dir``."""
        i_in = pos_of[t_in]
        out: list[tuple[int, int]] = []
        i = i_in
        while i != i_c:
            if fwd_dir:
                e = ring[i][1]
                i = (i + 1) % n
            else:
                i = (i - 1) % n
                e = ring[i][1]
            out.append((e, ring[i][0]))
        return out

    if away is not None:
        # the entry wedge stays the walker's actual tri: rewriting it
        # to the away side would lie about the recorded corridor — a
        # corridor that flips sides of a collinear incoming spoke emits
        # that spoke once as a real vertex crossing via the arc below
        t_out = _wedge_tri(ctx, ring_tris, w, biased(d_out, away))
        if t_out is None or t_out == t_in:
            return t_in, []
        i_in, i_out = pos_of[t_in], pos_of[t_out]
        fwd = (i_out - i_in) % n
        go_fwd = fwd <= n - fwd
        # the outer arc sweeps directions from -d_in to d_out through
        # the away bisector — measured in angle space, because 'away'
        # can land inside an end wedge on a shallow bend and a wedge
        # probe then picks the inner side (round-2 finding 1)
        a_in = math.atan2(-d_in[1], -d_in[0])
        a_out = math.atan2(d_out[1], d_out[0])
        a_aw = math.atan2(away[1], away[0])
        tau = 2.0 * math.pi
        outer_ccw = (a_aw - a_in) % tau <= (a_out - a_in) % tau
        # ring orientation: the entry wedge's interior spans previous
        # spoke -> forward spoke; a <180 CCW span means forward is CCW
        i_w = ctx.tri_verts[t_in].index(w)
        wxy = ctx.tri_xy[t_in][i_w]

        def sdir(e: int) -> float:
            u2, _v2 = ctx.edge_ends[e]
            fx, fy = ctx.ev[e] if u2 == w else ctx.eu[e]
            return math.atan2(fy - wxy[1], fx - wxy[0])

        fwd_ccw = (sdir(ring[i_in][1]) - sdir(ring[(i_in - 1) % n][1])) % tau < math.pi
        go_fwd = outer_ccw == fwd_ccw
        swept = arc_to(i_out, go_fwd)
    else:
        # straight pass-through: no outer side. When the exit direction
        # runs ALONG a spoke both adjacent wedges hold the path — take
        # the one whose corridor (approached without crossing the
        # spoke) stays passable; otherwise hold the incoming tri's side
        i_w = ctx.tri_verts[t_in].index(w)
        wxy = ctx.tri_xy[t_in][i_w]
        others = [ctx.tri_xy[t_in][j] for j in range(3) if j != i_w]
        rel = (
            others[0][0] + others[1][0] - 2.0 * wxy[0],
            others[0][1] + others[1][1] - 2.0 * wxy[1],
        )
        i_spoke = None
        for i_r, (_t, e) in enumerate(ring):
            u2, _v2 = ctx.edge_ends[e]
            fx, fy = ctx.ev[e] if u2 == w else ctx.eu[e]
            sx, sy = fx - wxy[0], fy - wxy[1]
            if (
                lo > _EPS
                and abs(d_out[0] * sy - d_out[1] * sx) <= 1e-9 * lo * math.hypot(sx, sy)
                and d_out[0] * sx + d_out[1] * sy > 0.0
            ):
                i_spoke = i_r
                break
        if i_spoke is not None and passable is not None:
            scored = []
            # ring[i_spoke].tri is entered forward without crossing the
            # spoke; ring[i_spoke+1].tri backward — both sides offered
            for i_c, gd in ((i_spoke, True), ((i_spoke + 1) % n, False)):
                sw = arc_to(i_c, gd)
                bad = sum(1 for e, _t in sw if not passable(e))
                scored.append((bad, len(sw), i_c, sw))
            scored.sort(key=lambda o: (o[0], o[1], o[2]))
            bad, _ln, i_c, sw = scored[0]
            if bad == 0:
                return ring[i_c][0], sw
        t_out = _wedge_tri(ctx, ring_tris, w, biased(d_out, rel))
        if t_out is None or t_out == t_in:
            return t_in, []
        i_in, i_out = pos_of[t_in], pos_of[t_out]
        fwd = (i_out - i_in) % n
        go_fwd = fwd <= n - fwd
        swept = arc_to(i_out, go_fwd)
    # a side-ambiguous pick can sweep the arc through a wall; the other
    # side holds the same polyline, so take it when it is clean —
    # otherwise keep the primary arc and let the caller's validation
    # fall back to the search corridor
    if passable is not None and any(not passable(e) for e, _t in swept):
        alt = arc_to(i_out, not go_fwd)
        if all(passable(e) for e, _t in alt):
            swept = alt
    return ring[i_out][0], swept


def _retrack(
    ctx: _Ctx,
    mesh: TopoMesh,
    va: int,
    vb: int,
    pts: list[tuple[float, float]],
    passable=None,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[float, ...]]:
    """Re-derive the corridor by walking the taut polyline through the
    mesh: ``(tris, edges, t)`` where tris has len(edges)+1, edge i
    separates tris i and i+1, and ``t`` is the crossing parameter on the
    stored u→v of each edge. Vertex-exact passages emit the fan spokes
    swept on the turn side — the ties the order pass resolves by
    divergence."""
    start = _wedge_tri(
        ctx, ctx.v2t[va], va, (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
    )
    if start is None:
        raise ValueError("retrack: no start wedge")
    tris: list[int] = [start]
    edges: list[int] = []
    t_out: list[float] = []
    tri = start
    skip = -1
    pos = pts[0]
    guard = 0
    for i in range(1, len(pts)):
        q = pts[i]
        while True:
            guard += 1
            if guard > 8 * len(mesh.edges):
                raise ValueError("retrack: runaway walk")
            if _in_tri(ctx, tri, q):
                pos = q
                break
            hit = _tri_exit(ctx, tri, skip, pos, q)
            if hit is None:
                raise ValueError("retrack: stuck")
            kind, a_, _b_, r = hit
            if kind == "edge":
                e = a_
                # both callers reject a corridor touching a blocked
                # edge — stop here rather than walking the rest
                if passable is not None and not passable(e):
                    raise ValueError("retrack: blocked edge")
                edges.append(e)
                t_out.append(_b_)
                t2a, t2b = ctx.edge_tris[e]
                t2 = t2a if t2a != tri else t2b
                if t2 < 0:
                    raise ValueError("retrack: left the mesh")
                tri = t2
                tris.append(tri)
                skip = e
                pos = (pos[0] + r * (q[0] - pos[0]), pos[1] + r * (q[1] - pos[1]))
                continue
            w = a_
            wpos = (float(mesh.verts[w, 0]), float(mesh.verts[w, 1]))
            if _same(q, wpos) and i == len(pts) - 1:
                pos = q
                break  # the path ends at vertex vb — not a crossing
            # find where the path leaves w: the first later point off w
            d_out = None
            for j in range(i + 1, len(pts)):
                if not _same(pts[j], wpos):
                    d_out = (pts[j][0] - wpos[0], pts[j][1] - wpos[1])
                    break
            if d_out is None:
                d_out = (q[0] - wpos[0], q[1] - wpos[1])
            tri, swept = _fan_arc(
                ctx, w, tri, (q[0] - pos[0], q[1] - pos[1]), d_out, passable
            )
            for e, t2 in swept:
                if passable is not None and not passable(e):
                    raise ValueError("retrack: blocked edge")
                edges.append(e)
                t_out.append(_edge_param_at(ctx, e, wpos))
                tris.append(t2)
            skip = -1
            pos = wpos
            if _same(q, wpos):
                break  # bend exactly at w; next segment takes over
    # cancel back-to-back e,e pairs — a spoke swept then re-crossed is a
    # corridor spike, not a real crossing (round-2 finding 1): dropping
    # the pair and the middle tri keeps the word reduced
    i = 0
    while i < len(edges) - 1:
        if edges[i] == edges[i + 1]:
            if tris[i] != tris[i + 2]:
                raise ValueError("retrack: uncancellable edge repeat")
            del edges[i : i + 2]
            del t_out[i : i + 2]
            del tris[i + 1 : i + 3]
            i = max(0, i - 1)
            continue
        i += 1
    return tuple(tris), tuple(edges), tuple(t_out)


def _edge_param_at(ctx: _Ctx, e: int, p: tuple[float, float]) -> float:
    """The parameter on edge ``e``'s stored u→v of point ``p``."""
    ux, uy = ctx.eu[e]
    ex, ey = ctx.ev[e][0] - ux, ctx.ev[e][1] - uy
    dd = ex * ex + ey * ey
    if dd <= _EPS:
        return 0.0
    return min(1.0, max(0.0, ((p[0] - ux) * ex + (p[1] - uy) * ey) / dd))


def _shortcut(
    ctx: _Ctx,
    mesh: TopoMesh,
    va: int,
    vb: int,
    pts: list[tuple[float, float]],
    passable,
    pin_vids: frozenset[int],
) -> list[tuple[float, float]]:
    """Release wraps the search corridor forced around NON-obstacle
    vertices (courtyard corners, ring-crossing vertices): if removing a
    bend keeps the re-walked path on passable edges, the shorter class
    wins (seam finding 7). Pads, zone corners and outline corners are
    obstacles and keep their wraps."""
    incident_blocked: dict[int, bool] = {}

    def obstacle(w: int) -> bool:
        if w in pin_vids:
            return True
        cached = incident_blocked.get(w)
        if cached is not None:
            return cached
        obs = False
        for t in ctx.v2t[w]:
            for e in ctx.tri_edge_list[t]:
                if w in ctx.edge_ends[e] and (ctx.board[e] or e in ctx.zone_contrib):
                    obs = True
                    break
            if obs:
                break
        incident_blocked[w] = obs
        return obs

    out = list(pts)
    # a failed removal only becomes worth retrying when a neighbour
    # moved — key on the candidate triple so the restart scan skips
    # re-walks it already paid for (round-4 perf: the re-tries were
    # most of _shortcut's _retrack calls)
    failed: set[
        tuple[tuple[float, float], tuple[float, float], tuple[float, float]]
    ] = set()
    for _ in range(3):
        changed = False
        for i in range(1, len(out) - 1):
            w = _vertex_at(ctx, mesh, out[i])
            if w is None or obstacle(w):
                continue
            key = (out[i - 1], out[i], out[i + 1])
            if key in failed:
                continue
            cand = out[:i] + out[i + 1 :]
            try:
                _t2, edges2, _tt = _retrack(ctx, mesh, va, vb, cand, passable)
            except ValueError:
                failed.add(key)
                continue
            if all(passable(int(e)) for e in edges2):
                out = cand
                changed = True
                break
            failed.add(key)
        if not changed:
            return out
    return out


def _vertex_at(ctx: _Ctx, mesh: TopoMesh, p: _V2) -> int | None:
    v = ctx.vid_of.get((round(float(p[0]), 6), round(float(p[1]), 6)))
    if v is not None and float(((mesh.verts[v] - p) ** 2).sum()) <= 1e-12:
        return v
    d2 = (mesh.verts - np.asarray(p)) ** 2
    i = int(np.argmin(d2[:, 0] + d2[:, 1]))
    return i if float(d2[i, 0] + d2[i, 1]) <= 1e-12 else None


def _order(
    ctx: _Ctx, mesh: TopoMesh, branches: tuple[WirePath, ...]
) -> tuple[tuple[tuple[int, ...], ...], int]:
    """Decision 3's per-edge ordered wire lists: branches sorted by their
    taut crossing parameter. Ties sit at a wrapped vertex ``w`` (t is 0
    or 1); they sort by wrap TIGHTNESS — ``|u_in + u_out|`` over the
    unit leg directions at the bend. A sharper bend sweeps the wider
    arc, so its crossing sits nearer ``w``: first at the ``u`` end, last
    at the ``v`` end — the sign flip keeps one transverse order on every
    edge of a shared run (seam finding 1); ``t`` stays geometry only.
    Interior-t ties happen on a shared bend-to-bend run, where both
    wires left the same wrap vertex through the same last spoke: the
    key then propagates that wrap's signed tightness, so the order
    fixed at the run's first vertex holds across the whole run (seam
    round-2 finding 6). Where the heuristic still leaves a shared run
    with an inconsistent normalized order, a repair pass carries the
    sign the pair shows where it separates across the tied edges —
    the divergence order the crossing graph needs, made exact."""
    tri_cent = [
        (
            (xy[0][0] + xy[1][0] + xy[2][0]) / 3.0,
            (xy[0][1] + xy[1][1] + xy[2][1]) / 3.0,
        )
        for xy in ctx.tri_xy
    ]
    wrap: list[dict[int, float]] = []
    for br in branches:
        pts = br.points
        w_at: dict[int, float] = {}
        for j in range(1, len(pts) - 1):
            v = _vertex_at(ctx, mesh, np.asarray(pts[j]))
            if v is None:
                continue
            a = np.asarray(pts[j - 1]) - np.asarray(pts[j])
            b = np.asarray(pts[j + 1]) - np.asarray(pts[j])
            la = math.hypot(float(a[0]), float(a[1]))
            lb = math.hypot(float(b[0]), float(b[1]))
            if la <= _EPS or lb <= _EPS:
                continue
            w_at[v] = math.hypot(
                float(a[0]) / la + float(b[0]) / lb,
                float(a[1]) / la + float(b[1]) / lb,
            )
        wrap.append(w_at)
    # corridor-vs-edge direction sign per crossing position — the same
    # normalization the seam test applies (travel direction against the
    # stored u→v); a tied order that flips with it keeps the normalized
    # transverse side constant along a bending corridor
    cdir: list[list[float]] = []
    for br in branches:
        row = []
        for i, e in enumerate(br.edges):
            ca, cb = tri_cent[br.tris[i]], tri_cent[br.tris[i + 1]]
            ux, uy = ctx.eu[e]
            cr = (cb[0] - ca[0]) * (ctx.ev[e][1] - uy) - (cb[1] - ca[1]) * (
                ctx.ev[e][0] - ux
            )
            row.append(-1.0 if cr < -1e-12 else 1.0)
        cdir.append(row)
    order: list[list[tuple[float, float, int, int]]] = [
        [] for _ in range(len(mesh.edges))
    ]
    for bi, br in enumerate(branches):
        w_at = wrap[bi]
        # backward pass: the NEXT endpoint-tied (vertex, sign) after each
        # position — a run that starts before the wire's first wrap
        # (e.g. at a shared pad) still has an end vertex to inherit from
        nxt: list[tuple[int, float] | None] = [None] * len(br.edges)
        n: tuple[int, float] | None = None
        for i in range(len(br.edges) - 1, -1, -1):
            nxt[i] = n
            te_i = br.t[i]
            if te_i <= 1e-9:
                n = (ctx.edge_ends[br.edges[i]][0], -1.0)
            elif te_i >= 1.0 - 1e-9:
                n = (ctx.edge_ends[br.edges[i]][1], 1.0)
        last: tuple[int, float] | None = None  # last wrapped (vertex, sign)
        for i, (e, te) in enumerate(zip(br.edges, br.t)):
            if te <= 1e-9:
                last = (ctx.edge_ends[e][0], -1.0)
            elif te >= 1.0 - 1e-9:
                last = (ctx.edge_ends[e][1], 1.0)
            ref = last if last is not None else nxt[i]
            key = ref[1] * w_at.get(ref[0], 0.0) if ref is not None else 0.0
            order[e].append((float(te), cdir[bi][i] * key, int(cdir[bi][i]) * bi, bi))
    ids: list[list[int]] = [[bi for _t, _k, _c, bi in sorted(lst)] for lst in order]
    residual = _repair_order(ctx, branches, cdir, ids)
    return tuple(tuple(lst) for lst in ids), residual


def _repair_order(
    ctx: _Ctx,
    branches: tuple[WirePath, ...],
    cdir: list[list[float]],
    ids: list[list[int]],
) -> int:
    """Align tied pairs on a shared run to one normalized order.

    The normalized side of two wires on edge ``e`` is their position in
    ``ids[e]`` signed by the trailing wire's travel direction against
    the stored u→v. A real separation fixes that sign on untied edges;
    the same sign must hold on edges where the crossings tie exactly —
    otherwise the crossing graph reads a phantom order flip. For each
    run of shared edges we take the pair's sign at the first edge where
    they separate (the first edge, when the whole run is tied) and move
    the violating partner inside the equal-``t`` group to match. Every
    co-tied pair of a group contributes its constraint — a pair split
    by a third wire in the group is still reordered by the fix, so
    collecting only adjacent pairs would leave its want unspoken and
    let the reorder create the flip it was meant to remove. A want
    anchored on an untied edge outranks an all-tied run's keep-as-is
    want when the two cycle inside one group. Ties are exact
    (``te`` bitwise equal): a near-tie order is geometry, not ours to
    flip.
    """
    epos: list[dict[int, int]] = []
    lpos: list[dict[int, int]] = []
    for br in branches:
        m: dict[int, int] = {}
        for i, e in enumerate(br.edges):
            m.setdefault(e, i)
        epos.append(m)
        lpos.append({e: i for i, e in enumerate(br.edges)})

    def norm(e: int, a: int, b: int) -> int:
        lst = ids[e]
        s = 1 if lst.index(a) < lst.index(b) else -1
        return s * int(cdir[b][epos[b][e]])

    def runs_of(a: int, b: int) -> list[list[int]]:
        ea, eb_at = branches[a].edges, lpos[b]
        out: list[list[int]] = []
        i = 0
        while i < len(ea):
            if ea[i] in eb_at:
                j = i + 1
                while (
                    j < len(ea)
                    and ea[j] in eb_at
                    and abs(eb_at[ea[j]] - eb_at[ea[j - 1]]) == 1
                ):
                    j += 1
                if j - i >= 2:
                    out.append(list(ea[i:j]))
                i = j
            else:
                i += 1
        return out

    def tied(e: int, a: int, b: int) -> bool:
        return abs(branches[a].t[epos[a][e]] - branches[b].t[epos[b][e]]) <= 1e-9

    def groups(lst: list[int], e: int) -> list[tuple[int, int]]:
        """Contiguous equal-``t`` spans of the sorted list — the units a
        fix may reorder without touching real geometry."""
        out = []
        i = 0
        while i < len(lst):
            lo = hi = branches[lst[i]].t[epos[lst[i]][e]]
            j = i + 1
            while j < len(lst):
                te_j = branches[lst[j]].t[epos[lst[j]][e]]
                if max(hi, te_j) - min(lo, te_j) > 1e-9:
                    break
                lo, hi = min(lo, te_j), max(hi, te_j)
                j += 1
            out.append((i, j))
            i = j
        return out

    pair_runs: dict[tuple[int, int], list[list[int]]] = {}

    def shared_runs(a: int, b: int) -> list[list[int]]:
        key = (a, b) if a < b else (b, a)
        got = pair_runs.get(key)
        if got is None:
            got = runs_of(*key)
            pair_runs[key] = got
        return got

    for _round in range(4):
        # (lead, trail, anchored) per edge — anchored wants come from a
        # run with an untied edge; all-tied runs only ask to keep the
        # order they show at run[0] and yield on conflict
        fixes: dict[int, list[tuple[int, int, bool]]] = {}
        seen: set[tuple[int, int]] = set()
        for e, lst in enumerate(ids):
            for i, j in groups(lst, e):
                for a, b in itertools.combinations(lst[i:j], 2):
                    # one orientation per pair: a reversed run's norm is
                    # the same consistency condition (negated
                    # same-direction, identical opposite-direction)
                    key = (a, b) if a < b else (b, a)
                    if key in seen:
                        continue
                    seen.add(key)
                    if branches[key[0]].net == branches[key[1]].net:
                        continue
                    a, b = key
                    for run in shared_runs(a, b):
                        want = next(
                            (norm(e2, a, b) for e2 in run if not tied(e2, a, b)),
                            None,
                        )
                        anchored = want is not None
                        if want is None:
                            want = norm(run[0], a, b)
                        for e2 in run:
                            if tied(e2, a, b) and norm(e2, a, b) != want:
                                lead, trail = (
                                    (a, b)
                                    if want * cdir[b][epos[b][e2]] > 0
                                    else (b, a)
                                )
                                fixes.setdefault(e2, []).append((lead, trail, anchored))
        if not fixes:
            break
        for e2 in sorted(fixes):
            rel = fixes[e2]
            lst = ids[e2]
            for i, j in groups(lst, e2):
                grp = lst[i:j]
                members = set(grp)
                rel_g = sorted(
                    {
                        (c[0], c[1])
                        for c in rel
                        if c[0] in members and c[1] in members and c[2]
                    }
                ) + sorted(
                    {
                        (c[0], c[1])
                        for c in rel
                        if c[0] in members and c[1] in members and not c[2]
                    }
                )
                if rel_g:
                    lst[i:j] = _topo_group(grp, rel_g)
    # residual: tied-pair violations the rounds could not settle
    # (cycle-dropped constraints or the round cap) — 2c's parity check
    # wants this surfaced, not silent (round-3 finding R3-3)
    leftover = 0
    counted: set[tuple[int, int]] = set()
    for e, lst in enumerate(ids):
        for i, j in groups(lst, e):
            for a, b in itertools.combinations(lst[i:j], 2):
                key = (a, b) if a < b else (b, a)
                if key in counted:
                    continue
                counted.add(key)
                if branches[key[0]].net == branches[key[1]].net:
                    continue
                a, b = key
                for run in shared_runs(a, b):
                    want = next(
                        (norm(e2, a, b) for e2 in run if not tied(e2, a, b)),
                        norm(run[0], a, b),
                    )
                    leftover += sum(
                        1 for e2 in run if tied(e2, a, b) and norm(e2, a, b) != want
                    )
    return leftover


def _topo_group(grp: list[int], rel: list[tuple[int, int]]) -> list[int]:
    """The equal-``t`` group reordered to satisfy ``(lead, trail)``
    constraints — deterministic (current position breaks ties).
    Constraints apply in the given order; one that would close a cycle
    is dropped, so a conflicting round still yields a consistent order."""
    pos = {x: i for i, x in enumerate(grp)}
    after: dict[int, set[int]] = {x: set() for x in grp}

    def reaches(src: int, dst: int) -> bool:
        stack, seen = [src], {src}
        while stack:
            for y in after[stack.pop()]:
                if y == dst:
                    return True
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return False

    indeg: dict[int, int] = {x: 0 for x in grp}
    for lead, trail in rel:
        if trail in after[lead] or reaches(trail, lead):
            continue
        after[lead].add(trail)
        indeg[trail] += 1
    out: list[int] = []
    avail = sorted((x for x in grp if indeg[x] == 0), key=pos.__getitem__)
    while avail:
        x = avail.pop(0)
        out.append(x)
        for y in sorted(after[x], key=pos.__getitem__):
            indeg[y] -= 1
            if indeg[y] == 0:
                avail.append(y)
                avail.sort(key=pos.__getitem__)
    return out + [x for x in grp if x not in out]


def _edge_capacity_mm(
    ctx: _Ctx, mesh: TopoMesh, d: TopoDesign, pose: np.ndarray
) -> np.ndarray:
    """Per-edge free crossing length — the corridor-cut input slice 4
    prices demand against: edge length minus each endpoint pad's extent
    projected onto the edge direction, read through the part pose so a
    quarter-turn or a bottom-side mirror prices the pad's real world
    shadow (the per-layer split lands with slice 4's corridor model)."""
    vid_pin: dict[int, int] = {}
    for p in range(len(mesh.pin_vertex)):
        vid_pin[int(mesh.pin_vertex[p])] = p
    cap = np.zeros(len(mesh.edges), dtype=np.float64)
    for e in range(len(mesh.edges)):
        a, b = mesh.verts[mesh.edges[e, 0]], mesh.verts[mesh.edges[e, 1]]
        dvec = b - a
        length = float(np.hypot(dvec[0], dvec[1]))
        cap[e] = length
        if length <= _EPS:
            continue
        ux, uy = float(dvec[0]) / length, float(dvec[1]) / length
        for vid in (int(mesh.edges[e, 0]), int(mesh.edges[e, 1])):
            pin = vid_pin.get(vid)
            if pin is None or len(d.pin_wh) <= pin:
                continue
            part = int(d.pin_part[pin])
            rot_q = round(float(pose[part, 2])) % 4 if pose.shape[1] > 2 else 0
            side = float(pose[part, 3]) if pose.shape[1] > 3 else 0.0
            # world edge dir -> footprint-local frame: un-rotate, un-mirror
            if rot_q == 0:
                lx, ly = ux, uy
            elif rot_q == 1:
                lx, ly = -uy, ux
            elif rot_q == 2:
                lx, ly = -ux, -uy
            else:
                lx, ly = uy, -ux
            if side >= 0.5:
                lx = -lx
            w, h = float(d.pin_wh[pin, 0]), float(d.pin_wh[pin, 1])
            cap[e] -= (w * abs(lx) + h * abs(ly)) / 2.0
        if cap[e] < 0.0:
            cap[e] = 0.0
    return cap


def _flood_region(
    ctx: _Ctx,
    mesh: TopoMesh,
    pins: list[int],
    base_pass: np.ndarray,
) -> set[int]:
    """The triangles a search could expand through from these pins —
    plain reachability over ``base_pass``: under net-level exemption
    the mask already opens every own-net pin's walls, so what it
    cannot reach is genuinely walled off."""
    seen: set[int] = set()
    stack: list[int] = []
    for pin in pins:
        for t in ctx.v2t[int(mesh.pin_vertex[pin])]:
            if t not in seen:
                seen.add(t)
                stack.append(t)
    while stack:
        t = stack.pop()
        for e in (int(x) for x in mesh.tri_edges[t]):
            if not base_pass[e]:
                continue
            t2 = int(mesh.edge_tris[e, 0])
            t2 = int(mesh.edge_tris[e, 1]) if t2 == t else t2
            if t2 >= 0 and t2 not in seen:
                seen.add(t2)
                stack.append(t2)
    return seen


def _wall_owners(
    ctx: _Ctx,
    mesh: TopoMesh,
    d: TopoDesign,
    own: frozenset[int],
    region: set[int],
    tree_region: set[int],
    cand_mask: int,
    net_name: str,
    r: float,
) -> tuple[int, ...]:
    """What separates an unreachable component's region from the main
    tree's: owners of the blocked edges on the cut between the two
    flooded regions — part ids, ``n_parts + zone`` or OWNER_BOARD (the
    ``edge_owner`` id space). Falls back to the region's whole blocked
    boundary when the regions meet only at vertices."""
    n_parts = len(d.parts)
    _b, _f, gap_pads, _p = _pad_eval(ctx, r)
    cut: set[int] = set()
    boundary: set[int] = set()
    for t in region:
        for e in (int(x) for x in mesh.tri_edges[t]):
            t2 = int(mesh.edge_tris[e, 0])
            t2 = int(mesh.edge_tris[e, 1]) if t2 == t else t2
            if t2 in region:
                continue
            boundary.add(e)
            if t2 >= 0 and t2 in tree_region:
                cut.add(e)

    def blame(e: int, acc: set[int]) -> None:
        if ctx.board[e]:
            acc.add(OWNER_BOARD)
        for pin, pm in ctx.pad_contrib.get(e, ()):
            if pin not in own and pm & cand_mask:
                acc.add(int(d.pin_part[pin]))
        for z, zm in ctx.zone_contrib.get(e, ()):
            if zm & cand_mask and not (d.zones[z].net and d.zones[z].net == net_name):
                acc.add(n_parts + z)
        for pi_ in gap_pads.get(e, ()):
            pin = int(ctx.pad_pin[pi_])
            if pin in own:
                continue
            if _seg_pad_int(ctx, e, pi_, r) is not None:
                acc.add(int(d.pin_part[pin]))

    owners: set[int] = set()
    for e in cut or boundary:
        blame(e, owners)
    return tuple(sorted(owners))


def _seg_pad_int(ctx: _Ctx, e: int, pi: int, r: float) -> tuple[float, float] | None:
    """The parameter interval of edge ``e`` covered by pad ``pi``'s
    rectangle grown by ``r`` — a slab clip of the segment against the
    grown AABB. ``None`` when they don't meet (or touch in a
    zero-measure graze)."""
    px, py = float(ctx.seg_u[e, 0]), float(ctx.seg_u[e, 1])
    dx = float(ctx.seg_v[e, 0]) - px
    dy = float(ctx.seg_v[e, 1]) - py
    lo = ctx.pad_lo[pi] - r
    hi = ctx.pad_hi[pi] + r
    ent, ext = 0.0, 1.0
    for ax in range(2):
        dd = dx if ax == 0 else dy
        pp = px if ax == 0 else py
        if abs(dd) <= _EPS:
            if pp < lo[ax] - 1e-12 or pp > hi[ax] + 1e-12:
                return None
            continue
        t0 = (float(lo[ax]) - pp) / dd
        t1 = (float(hi[ax]) - pp) / dd
        ent = max(ent, min(t0, t1))
        ext = min(ext, max(t0, t1))
        if ent >= ext:
            return None
    return (ent, ext) if ext - ent > 1e-12 else None


def _merge_len(ints: list[tuple[float, float]]) -> float:
    """Covered measure of ``[0,1]`` under sorted-merge of intervals."""
    ints.sort()
    cov = 0.0
    ca = cb = -math.inf
    for a, b in ints:
        if a <= cb:
            cb = max(cb, b)
        else:
            cov += cb - ca if ca > -math.inf else 0.0
            ca, cb = a, b
    if ca > -math.inf:
        cov += cb - ca
    return min(cov, 1.0)


def _pad_eval(
    ctx: _Ctx, r: float
) -> tuple[np.ndarray, np.ndarray, dict[int, np.ndarray], dict[int, list[int]]]:
    """Coverage of every mesh edge by every ringed pad grown by ``r``
    (``trace/2 + clearance`` — the exclusion a centreline must keep).
    Returns ``(blocked, free, edge_pads, pad_edges)``: ``blocked[e]`` is
    the layer mask where no free stretch of ``e`` survives, ``free[e,
    l]`` the uncovered length in mm (−1 where no pad reaches ``e``), and
    the two maps index candidate pads per edge / candidate edges per
    pad for the per-net own-pad re-eval. Cached per ``r`` — nets sharing
    a rule share the eval (round-4 rework: the old endpoint-coincidence
    test missed every channel split by a non-pad vertex, and ran the
    O(ring²) pair scan per call)."""
    got = ctx.gap_eval_cache.get(r)
    if got is not None:
        return got
    n_e = len(ctx.board)
    n_l = ctx.n_layers
    free = np.full((n_e, n_l), -1.0)
    blocked = np.zeros(n_e, dtype=np.int64)
    edge_pads: dict[int, np.ndarray] = {}
    pad_edges: dict[int, list[int]] = {}
    out = (blocked, free, edge_pads, pad_edges)
    ctx.gap_eval_cache[r] = out
    if not len(ctx.pad_pin):
        return out
    # candidate (edge, pad) pairs: the grown pad AABB meets the edge's
    hit = np.logical_and(
        ctx.pad_lo[None] <= ctx.edge_hi[:, None] + r,
        ctx.pad_hi[None] >= ctx.edge_lo[:, None] - r,
    ).all(axis=2)
    ei, pi = np.nonzero(hit)
    if not len(ei):
        return out
    # slab clip of every pair's segment against its grown pad box
    pu = ctx.seg_u[ei]
    dd = ctx.seg_v[ei] - pu
    plo = ctx.pad_lo[pi] - r
    phi = ctx.pad_hi[pi] + r
    ent = np.zeros(len(ei))
    ext = np.ones(len(ei))
    feas = np.ones(len(ei), dtype=bool)
    for ax in range(2):
        dax = dd[:, ax]
        pax = pu[:, ax]
        big = np.abs(dax) > _EPS
        t0 = np.empty(len(ei))
        t1 = np.empty(len(ei))
        t0[big] = (plo[big, ax] - pax[big]) / dax[big]
        t1[big] = (phi[big, ax] - pax[big]) / dax[big]
        ent[big] = np.maximum(ent[big], np.minimum(t0[big], t1[big]))
        ext[big] = np.minimum(ext[big], np.maximum(t0[big], t1[big]))
        small = ~big
        feas[small] &= (pax[small] >= plo[small, ax] - 1e-12) & (
            pax[small] <= phi[small, ax] + 1e-12
        )
    valid = feas & (ext - ent > 1e-12)
    # per edge, per layer: merge the sorted covered intervals
    order = np.lexsort((ent, ei))
    ei_s, ent_s, ext_s = ei[order], ent[order], ext[order]
    val_s, lm_s = valid[order], ctx.pad_copper[pi][order]
    pi_s = pi[order]
    i = 0
    n = len(order)
    while i < n:
        e = int(ei_s[i])
        j = i
        while j < n and ei_s[j] == e:
            j += 1
        plist = pi_s[i:j]
        edge_pads[e] = np.asarray(sorted(set(int(x) for x in plist)))
        for p_ in edge_pads[e]:
            pad_edges.setdefault(int(p_), []).append(e)
        for ly in range(n_l):
            bit = 1 << ly
            ints = [
                (float(ent_s[k]), float(ext_s[k]))
                for k in range(i, j)
                if val_s[k] and (int(lm_s[k]) & bit)
            ]
            cov = _merge_len(ints) if ints else 0.0
            free[e, ly] = float(ctx.seg_len[e]) * (1.0 - cov)
            if free[e, ly] <= 1e-9:
                blocked[e] |= bit
        i = j
    return out


def _edge_free(
    ctx: _Ctx, e: int, r: float, drop: frozenset[int], edge_pads
) -> np.ndarray:
    """``free`` row for edge ``e`` re-merged without the net's own pads
    (``drop`` = pad indices) — the own-net exemption applied to the gap
    rule: a channel off the wire's own pad is measured against foreign
    copper only."""
    free = np.empty(ctx.n_layers)
    cands = [p for p in edge_pads.get(e, ()) if p not in drop]
    ints: list[tuple[float, float, int]] = []
    for pi_ in cands:
        iv = _seg_pad_int(ctx, e, int(pi_), r)
        if iv is not None:
            ints.append((iv[0], iv[1], int(ctx.pad_copper[pi_])))
    for ly in range(ctx.n_layers):
        bit = 1 << ly
        cov = _merge_len([(a, b) for a, b, m in ints if m & bit]) if ints else 0.0
        free[ly] = float(ctx.seg_len[e]) * (1.0 - cov)
    return free


def _blocked_masks(
    ctx: _Ctx,
    d: TopoDesign,
    cand_mask: int,
    net_name: str,
    own: frozenset[int],
    trace_mm: float,
    clearance_mm: float,
) -> np.ndarray:
    """The net's passable mask — an edge is open on a candidate layer
    unless a pin of ANOTHER net, a foreign-net zone, a too-narrow
    foreign pad-to-pad gap or the board edge blocks it there (R-a +
    round-2 ruling: exemption keys on the wire's net; a foreign-net pad
    on an endpoint part stays a wall. Reto 2026-10-25: a gap between
    other-net pads passes only where the geometry fits — each pad is
    buffered by ``trace/2 + clearance`` and an edge survives a layer
    only where a free stretch remains, so a channel closes exactly when
    ``gap < trace + 2·clearance``; the closure applies per LAYER — two
    top-side pads leave the bottom open — and the net's own pads are
    dropped from the coverage (own-flanked stays open)."""
    # base blocked layers: every pad wall, then the own-net exemption
    # re-merges only edges an own pin's fan boundary touches (round-4
    # perf — the per-net full contrib scan dominated _blocked_masks);
    # zones are cached per net name.
    full_blocked = ctx.pad_block_all.copy()
    for pin in own:
        for e in ctx.fan_boundary.get(pin, ()):
            m = 0
            for p2, pm in ctx.pad_contrib.get(e, ()):
                if p2 not in own:
                    m |= pm
            full_blocked[e] = m
    zb = ctx.zone_block.get(net_name)
    if zb is None:
        zb = np.zeros(len(ctx.board), dtype=np.int64)
        for e, lst in ctx.zone_contrib.items():
            m = 0
            for z, zm in lst:
                if not (d.zones[z].net and d.zones[z].net == net_name):
                    m |= zm
            if m:
                zb[e] = m
        ctx.zone_block[net_name] = zb
    full_blocked |= zb
    r = trace_mm / 2.0 + clearance_mm
    blocked_gap, _free, edge_pads, pad_edges = _pad_eval(ctx, r)
    if edge_pads:
        gap_mask = blocked_gap.copy()
        own_pads = frozenset(ctx.pin_pad[p] for p in own if p in ctx.pin_pad)
        if own_pads:
            seen_e: set[int] = set()
            for pi_ in own_pads:
                for e in pad_edges.get(pi_, ()):
                    if e in seen_e:
                        continue
                    seen_e.add(e)
                    free = _edge_free(ctx, e, r, own_pads, edge_pads)
                    m = 0
                    for ly in range(ctx.n_layers):
                        if free[ly] <= 1e-9:
                            m |= 1 << ly
                    gap_mask[e] = m
        full_blocked |= gap_mask
    return ((np.int64(cand_mask) & ~full_blocked) != 0) & ~ctx.board


def _net_tree(
    ctx: _Ctx,
    mesh: TopoMesh,
    d: TopoDesign,
    k: int,
    n_layers: int,
    trace_mm: float,
    clearance_mm: float,
    occ: np.ndarray,
) -> tuple[list[tuple[int, int, list[int], list[int]]], list[list[int]], np.ndarray]:
    """Geodesic Prim over one net's pins (R-b): per COMPONENT a
    persistent multi-source A* search — the heap carries relax states
    keyed on ``g + h`` (h = the midpoint distance to the nearest pin;
    consistent, so the first goal popped is optimal) plus lazy goal
    entries carrying exact totals, and a fresh attach re-seeds at cost
    0 so the heap key means distance-to-tree (Prim), not
    distance-to-root. A pin no path reaches under the net's passability
    stays unplaced; the search then roots a fresh component at the
    lowest unplaced pin — a caged root does not drop the net
    (round-2 finding 4). Under net-level exemption passability is a
    uniform per-net mask, so there is no goal-dependent rescue: a
    lost pin's flood region is exactly what it can reach. Returns
    ``(branches, components, base_pass)`` — a branch is
    ``(pin_a, pin_b, tris, edges)`` and a component is the member pin
    indices of one separated region (``build_wires`` reports every
    non-main component once on ``Wires.unreachable``)."""
    pins = [int(p) for p in d.net_pins[k]]
    if len(pins) < 2:
        return [], [[j] for j in range(len(pins))], np.zeros(len(ctx.board), bool)
    verts = [int(mesh.pin_vertex[p]) for p in pins]
    vpos = [mesh.verts[v] for v in verts]
    net_name = d.net_name[k] if k < len(d.net_name) else str(k)
    cand_mask = _cand_mask(_net_layers(d, k, n_layers), n_layers)
    base_pass = _blocked_masks(
        ctx, d, cand_mask, net_name, frozenset(pins), trace_mm, clearance_mm
    )

    # A* heuristic: distance from each pin to the NEAREST POINT of each
    # crossed edge (segment distance, not midpoint — round-4 finding 4:
    # sliver-fan midpoints sit millimetres off the channel axis, which
    # inflated h and g until a legal pad gap cost more than wrapping
    # the row). h(e) is the min over UNATTACHED pins, raised on each
    # attach (round-3 finding R3-2): an attached pin left in the min
    # keeps h ~0 around the growing tree and A* degenerates to Dijkstra
    # from the whole tree. Raising h leaves already-queued keys as
    # underestimates, so lazy entries stay correct; only edges whose
    # min was the attached pin are rescanned. ``pin_dist`` is a slice of
    # the ctx-wide pin→segment matrix (mutated by retire_pin, so a copy).
    pin_dist = ctx.pin_seg[:, pins].copy()
    hvec = pin_dist.min(axis=1)
    # the relax loop reads h per push — a memoryview gives plain-float
    # reads while retire_pin's numpy writes stay visible (round-4 perf)
    hview = memoryview(hvec)

    def retire_pin(j: int) -> None:
        affected = hvec == pin_dist[:, j]
        pin_dist[:, j] = math.inf
        if affected.any():
            hvec[affected] = pin_dist[affected].min(axis=1)

    def h(e: int) -> float:
        return hview[e]

    # which net pins sit in each triangle (goal check without scanning)
    tri_pins: dict[int, list[int]] = {}
    for j in range(len(pins)):
        for t in ctx.v2t[verts[j]]:
            tri_pins.setdefault(t, []).append(j)

    INF = math.inf
    span = len(mesh.tris) + 1  # state key: e * span + t
    unplaced = set(range(len(pins)))
    branches: list[tuple[int, int, list[int], list[int]]] = []
    components: list[list[int]] = []

    while unplaced:
        root = min(unplaced)
        unplaced.discard(root)
        retire_pin(root)
        dist: dict[int, float] = {}
        prev: dict[int, int] = {}
        src: dict[int, int] = {}
        # (key, tag, a, b, g): relax entries key on g + h and carry g so
        # a stale pop is detected against dist; goal entries key on the
        # exact total and carry it in b (encoded state) and g (unused).
        heap: list[tuple[float, int, int, int, float]] = []
        in_tree = {root}
        # attach[j] = (branch length, (src pin, chain) | None, src pin | None)
        attach: dict[
            int, tuple[float, tuple[int, tuple[int, ...]] | None, int | None]
        ] = {root: (0.0, None, None)}

        def push_state(
            e0: int,
            t2: int,
            cost: float,
            j: int,
            *,
            prev: dict[int, int] = prev,
            dist: dict[int, float] = dist,
            src: dict[int, int] = src,
            heap: list[tuple[float, int, int, int, float]] = heap,
        ) -> None:
            st = e0 * span + t2
            if cost >= dist.get(st, INF) - 1e-12:
                return
            if st in dist:
                # a cheaper re-seed roots the chain here: dropping the
                # old parent keeps prev a forest of edges new→old —
                # without it a descendant could re-parent a seeded
                # state and close a cycle in the walk (R4)
                prev.pop(st, None)
            dist[st] = cost
            src[st] = j
            heapq.heappush(heap, (cost + h(e0), 0, e0, t2, cost))

        def seed(j: int) -> None:
            # Prim: a fresh attach re-seeds at cost 0 — the heap key is
            # distance-to-tree, not distance-to-root (round-2 finding 3).
            # pin_dist[:, j] is already retired to inf (R3-2), so the
            # seed cost is read straight off the geometry.
            vj = vpos[j]
            for t0 in ctx.v2t[verts[j]]:
                for e0 in ctx.tri_edge_list[t0]:
                    if not base_pass[e0]:
                        continue
                    t2a, t2b = ctx.edge_tris[e0]
                    t2 = t2a if t2a != t0 else t2b
                    if t2 < 0:
                        continue
                    push_state(
                        e0,
                        t2,
                        _pt_seg(vj, ctx.seg_u[e0], ctx.seg_v[e0]) * occ[e0],
                        j,
                    )

        def push_goals(
            j: int,
            *,
            heap: list[tuple[float, int, int, int, float]] = heap,
        ) -> None:
            """Direct same-tri goals from a freshly attached pin."""
            vj = vpos[j]
            for t0 in ctx.v2t[verts[j]]:
                for j2 in tri_pins.get(t0, ()):
                    if j2 in unplaced:
                        total = math.dist(vj, vpos[j2])
                        heapq.heappush(heap, (total, 1, j2, -j - 1, total))

        seed(root)
        push_goals(root)
        while heap and unplaced:
            key, tag, a_, b_, g = heapq.heappop(heap)
            if tag == 1:
                j2 = a_
                if j2 not in unplaced:
                    continue
                unplaced.discard(j2)
                in_tree.add(j2)
                retire_pin(j2)
                if b_ >= 0:
                    # snapshot the winning chain NOW: later attaches
                    # re-seed at 0 and can relabel prev/src under a
                    # recorded goal state (a chain rerouted through the
                    # attached pin's own seed rebuilt a self-branch)
                    chain = []
                    seen: set[int] = set()
                    s = int(b_)
                    while s in prev and s not in seen:
                        seen.add(s)
                        chain.append(s)
                        s = prev[s]
                    chain.append(s)
                    chain.reverse()
                    attach[j2] = (key, (src[s], tuple(chain)), None)
                else:
                    attach[j2] = (key, None, -b_ - 1)
                seed(j2)
                push_goals(j2)
                continue
            e, t = a_, b_
            st = e * span + t
            if g > dist.get(st, INF) + 1e-9:
                continue
            for j2 in tri_pins.get(t, ()):
                if j2 in unplaced:
                    total = g + float(pin_dist[e, j2])
                    heapq.heappush(heap, (total, 1, j2, st, total))
            for e2, t2, wstep in ctx.tri_next[t].get(e, ()):
                if t2 < 0 or not base_pass[e2]:
                    continue
                nxt = e2 * span + t2
                nd = g + wstep * occ[e2]
                if nd < dist.get(nxt, INF) - 1e-12:
                    prev[nxt] = st
                    src[nxt] = src[st]
                    dist[nxt] = nd
                    heapq.heappush(heap, (nd + h(e2), 0, e2, t2, nd))

        members = sorted(in_tree)
        components.append(members)
        for j in members[1:]:
            _total, enc, src_j = attach[j]
            if enc is None:
                # direct same-tri goal: src_j is the tree pin index
                i = src_j if src_j is not None else root
                shared = next(
                    t for t in ctx.v2t[verts[i]] if verts[j] in ctx.tri_set[t]
                )
                branches.append((pins[i], pins[j], [shared], []))
                continue
            i, chain_t = enc
            chain = list(chain_t)
            edges = [s_ // span for s_ in chain]
            first_ta, first_tb = ctx.edge_tris[edges[0]]
            first_t = first_tb if first_ta == chain[0] % span else first_ta
            tris = [first_t] + [s_ % span for s_ in chain]
            branches.append((pins[i], pins[j], tris, edges))
    return branches, components, base_pass


def _make_passable(base_pass: np.ndarray):
    """The branch passable check for the shortcut pass and the re-walk
    validation — under net-level exemption it is simply the net's
    passable mask."""

    def passable(e: int) -> bool:
        return bool(base_pass[e])

    return passable


def _two_pointer_t(
    ctx: _Ctx,
    tris: list[int],
    edges: list[int],
    pts: list[tuple[float, float]],
) -> tuple[float, ...]:
    """Fallback crossing parameters on the SEARCH corridor when the
    re-walk fails — the two-pointer, restricted to its current segment
    (seam finding 8)."""
    t: list[float] = []
    seg = 0
    for e in edges:
        hit = None
        while seg < len(pts) - 1:
            hit = _portal_t(ctx, e, pts[seg], pts[seg + 1])
            if hit is not None:
                break
            seg += 1
        if hit is None:
            ux, uy = ctx.eu[e]
            ex, ey = ctx.ev[e][0] - ux, ctx.ev[e][1] - uy
            near = min(seg, len(pts) - 1)
            hit = min(
                1.0,
                max(
                    0.0,
                    ((pts[near][0] - ux) * ex + (pts[near][1] - uy) * ey)
                    / (ex * ex + ey * ey),
                ),
            )
        t.append(hit)
    return tuple(t)


def _portal_t(
    ctx: _Ctx, e: int, p: tuple[float, float], q: tuple[float, float]
) -> float | None:
    """The parameter along edge ``e``'s stored (u→v) where segment
    ``p→q`` meets it — a proper straddle's intersection, or an
    endpoint-on-segment's projection. ``None`` when they don't touch."""
    ux, uy = ctx.eu[e]
    vx, vy = ctx.ev[e]
    ex, ey = vx - ux, vy - uy
    dd = ex * ex + ey * ey
    sx, sy = q[0] - p[0], q[1] - p[1]
    denom = sx * ey - sy * ex
    if abs(denom) > _EPS:
        s = ((ux - p[0]) * sy - (uy - p[1]) * sx) / denom
        r = ((ux - p[0]) * ey - (uy - p[1]) * ex) / denom
        if -1e-9 <= r <= 1.0 + 1e-9 and -1e-9 <= s <= 1.0 + 1e-9:
            return min(1.0, max(0.0, s))
        return None
    for pt in (p, q):
        if abs(_area2(ctx.eu[e], ctx.ev[e], pt)) <= _EPS * max(1.0, dd):
            dot = (pt[0] - ux) * ex + (pt[1] - uy) * ey
            if -1e-9 <= dot <= dd + 1e-9:
                return min(1.0, max(0.0, dot / dd))
    if segments_cross(p, q, ctx.eu[e], ctx.ev[e]):
        dot = ((p[0] + q[0]) / 2.0 - ux) * ex + ((p[1] + q[1]) / 2.0 - uy) * ey
        return min(1.0, max(0.0, dot / dd))
    return None


def build_wires(
    mesh: TopoMesh,
    d: TopoDesign,
    n_layers: int,
    net_rules: tuple[tuple[float, float], ...] | None = None,
    pose: np.ndarray | None = None,
) -> Wires:
    """Every net's wire tree on the mesh — spec §2's wires. Returns a
    :class:`Wires`; a component of a net no path can link to the net's
    main component on any candidate layer is wired internally and
    named once on :attr:`Wires.unreachable` for the colour pass to
    report. ``net_rules`` is ``(trace_mm, clearance_mm)`` per net
    (resolved ``NetRules`` by the caller when the board has classes);
    ``None`` uses the default pair, under which foreign pad-to-pad gaps
    price open or closed by geometry (Reto 2026-10-25). ``pose`` is the
    pose the mesh was built at (defaults ``d.pose0``): the corridor
    capacity reads each endpoint pad's extent through it, so a caller
    routing a placed pose passes it — the authored pose prices a
    rotated or flipped pad's shadow wrong (gr520904 follow-up)."""
    ctx = _make_ctx(mesh, d, n_layers)
    pin_vids = frozenset(int(v) for v in mesh.pin_vertex)
    n_e = len(mesh.edges)
    branches: list[WirePath] = []
    net_layers: list[frozenset[int]] = [frozenset() for _ in d.net_pins]
    unreachable: list[Unreachable] = []
    rules_used: list[tuple[float, float]] = [
        (
            net_rules[k]
            if net_rules is not None and k < len(net_rules)
            else (_DEFAULT_TRACE_MM, _DEFAULT_CLEAR_MM)
        )
        for k in range(len(d.net_pins))
    ]
    # slice-4 channel record at the finest resolved rule — the most
    # slack a channel ever shows; coarser nets just see less free.
    # Priced BEFORE routing (gr520904): it feeds both the A* occupancy
    # term and the demand-vs-capacity cut on the routed wires.
    t_ref, c_ref = (
        min(rules_used, key=lambda rc: rc[0] + rc[1])
        if rules_used
        else (_DEFAULT_TRACE_MM, _DEFAULT_CLEAR_MM)
    )
    _b, free_ref, edge_pads_ref, _p = _pad_eval(ctx, t_ref / 2.0 + c_ref)
    gap_free = np.full((n_e, n_layers), -1.0)
    gap_cap = np.full((n_e, n_layers), -1, dtype=np.int64)
    for e in edge_pads_ref:
        f = free_ref[e]
        gap_free[e] = f
        # total traces the free stretch takes, in the free width's own
        # frame: floor(f / (t + c)) + 1 — a just-passable channel
        # records 1, a closed one 0 (round-5: the earlier
        # (f + c) / (t + c) form undercounted by up to one trace and
        # recorded cap 0 on passable gaps)
        gap_cap[e] = np.where(
            f > 1e-9, np.floor(f / (t_ref + c_ref)) + 1.0, 0.0
        ).astype(np.int64)
    capacity_mm = _edge_capacity_mm(
        ctx, mesh, d, np.asarray(d.pose0 if pose is None else pose)
    )
    # The corridor capacity the occupancy term and the cut share: a pad
    # channel takes gap_cap traces summed across EVERY layer a wire can
    # use (an inner signal layer is as good a crossing as an outer);
    # an edge no pad rings takes its free crossing length's worth on
    # each of the n_layers. Outer-layers-only pricing undercounted a
    # 4-layer channel by the inner share and refused boards that fit
    # (gr520904 follow-up, esp32c3 + nano probes).
    is_channel = (gap_cap >= 0).any(axis=1)
    cap_total = np.where(
        is_channel,
        np.clip(gap_cap, 0, None).sum(axis=1),
        (np.floor(capacity_mm / (t_ref + c_ref)) + 1.0) * n_layers,
    )
    inv_cap = np.zeros(n_e)
    np.divide(1.0, cap_total, out=inv_cap, where=cap_total > 0)
    # The pins bounding each edge — pads ringing it in the gap record
    # plus pins at its endpoints. A wire squeezing between pads of its
    # OWN net owes no foreign-pad clearance, so it contributes neither
    # occupancy nor demand (an own-net pin pair's edge is a legal
    # crossing even when its capacity reads 0).
    vid_pin = {int(mesh.pin_vertex[p]): p for p in range(len(mesh.pin_vertex))}
    edge_pins: list[frozenset[int]] = []
    for e in range(n_e):
        pins_e = {int(ctx.pad_pin[pi_]) for pi_ in edge_pads_ref.get(e, ())}
        pins_e.update(vid_pin[int(v)] for v in mesh.edges[e] if int(v) in vid_pin)
        edge_pins.append(frozenset(pins_e))
    own_pins = [
        frozenset(int(p) for p in d.net_pins[k]) for k in range(len(d.net_pins))
    ]
    weight = [(t + c) / (t_ref + c_ref) for t, c in rules_used]
    used = np.zeros(n_e)  # occupied finest-rule slots per edge

    def _net_order_key(k: int) -> tuple[bool, float, int]:
        # short and forced nets route first (gr520904): a single-
        # candidate-layer net and a short-spread net have the fewest
        # alternative corridors, so they claim capacity before the
        # free nets' occupancy term steers them elsewhere.
        cand = _net_layers(d, k, n_layers)
        pvs = [int(mesh.pin_vertex[p]) for p in d.net_pins[k]]
        span = (
            float(
                np.hypot(*(mesh.verts[pvs].max(axis=0) - mesh.verts[pvs].min(axis=0)))
            )
            if pvs
            else 0.0
        )
        return (len(cand) > 1, span, k)

    for k in sorted(range(len(d.net_pins)), key=_net_order_key):
        pins_arr = d.net_pins[k]
        net_name = d.net_name[k] if k < len(d.net_name) else str(k)
        cand = _net_layers(d, k, n_layers)
        net_layers[k] = cand
        trace_mm, clearance_mm = rules_used[k]
        occ = 1.0 + _CONGESTION_ALPHA * used * inv_cap
        found, comps, base_pass = _net_tree(
            ctx, mesh, d, k, n_layers, trace_mm, clearance_mm, occ
        )
        passable = _make_passable(base_pass)
        for pa, pb, tris, edges in found:
            va, vb = int(mesh.pin_vertex[pa]), int(mesh.pin_vertex[pb])
            if not edges:
                a, b = mesh.verts[va], mesh.verts[vb]
                points = (
                    (float(a[0]), float(a[1])),
                    (float(b[0]), float(b[1])),
                )
                branches.append(WirePath(k, pa, pb, (tris[0],), (), (), points))
                continue
            pts = _taut(ctx, mesh, tris, edges, va, vb)
            pts = _shortcut(ctx, mesh, va, vb, pts, passable, pin_vids)
            try:
                tris2, edges2, t2 = _retrack(ctx, mesh, va, vb, pts, passable)
                # the re-walked corridor must not cross a wall either —
                # a taut segment can run along a blocked mesh edge and
                # pick the wrong side (round-2 finding 1)
                if not all(passable(int(e)) for e in edges2):
                    raise ValueError("retrack: blocked edge")
            except ValueError:
                tris2, edges2, t2 = (
                    tuple(tris),
                    tuple(edges),
                    _two_pointer_t(ctx, tris, edges, pts),
                )
            branches.append(
                WirePath(
                    net=k,
                    pin_a=pa,
                    pin_b=pb,
                    tris=tuple(tris2),
                    edges=tuple(edges2),
                    t=tuple(t2),
                    points=tuple((float(p[0]), float(p[1])) for p in pts),
                )
            )
            # occupancy the next nets' A* prices (gr520904) — the
            # RETRACKED corridor is what capacity sees, so used counts
            # it, not the search edges.
            for e_ in edges2:
                if not edge_pins[int(e_)] <= own_pins[k]:
                    used[int(e_)] += weight[k]
        if len(comps) > 1:
            pins_l = [int(p) for p in pins_arr]
            own = frozenset(pins_l)
            cand_mask = _cand_mask(cand, n_layers)
            regions = [
                _flood_region(ctx, mesh, [pins_l[j] for j in comp], base_pass)
                for comp in comps
            ]
            # the main component is the one that sees most of the board —
            # a boxed-in root floods a few triangles, so member count
            # alone would report the FREE side as unreachable
            main_i = max(
                range(len(comps)),
                key=lambda ci: (len(regions[ci]), len(comps[ci])),
            )
            main_region = regions[main_i]
            for ci, comp in enumerate(comps):
                if ci == main_i:
                    continue
                owners = _wall_owners(
                    ctx,
                    mesh,
                    d,
                    own,
                    regions[ci],
                    main_region,
                    cand_mask,
                    net_name,
                    trace_mm / 2.0 + clearance_mm,
                )
                unreachable.append(
                    Unreachable(
                        net=k,
                        pin=pins_l[comp[0]],
                        owners=owners,
                        pins=tuple(pins_l[j] for j in comp),
                    )
                )
    order, residual = _order(ctx, mesh, tuple(branches))

    # The phase-3 cut on the ROUTED wires (gr520904): per edge the
    # slot-weighted count of crossing branches against the corridor
    # capacity — demand over capacity is recorded for the caller to
    # refuse on, never silently priced through.
    def _owners(e: int) -> tuple[str, ...]:
        names = {str(d.parts[int(d.pin_part[p])]) for p in edge_pins[e]}
        names.update(
            str(d.zones[z].name or f"zone{z}") for z, _zm in ctx.zone_contrib.get(e, ())
        )
        if ctx.board[e]:
            names.add("outline")
        return tuple(sorted(names)) or ("free space",)

    demand = np.zeros(n_e)
    demanders: dict[int, list[int]] = {}
    for e, seq in enumerate(order):
        for bi in seq:
            k = int(branches[bi].net)
            if not edge_pins[e] <= own_pins[k]:
                demand[e] += weight[k]
                demanders.setdefault(e, []).append(k)

    def _lone_fits(e: int, k: int) -> bool:
        """A LONE wire needs a point in the free stretch, not a full
        slot — the slot ledger reads a single coarser-rule wire as
        demand > 1 on a one-slot corridor even when its centreline
        fits, which refused a routed esp32c3 net pre-hardening
        (gr520904 follow-up). It cuts only when its own rule leaves it
        no room on any layer (a fat rail through a thin gap: genuine
        congestion, and exactly the cram the cut exists to name)."""
        t_k, c_k = rules_used[k]
        if is_channel[e]:
            # gap_free is measured under the ref rule's growth — the
            # wire fits where a point survives its own growth
            need = (t_k + 2 * c_k) - (t_ref + 2 * c_ref)
            return bool((gap_free[e] > need + 1e-9).any())
        return bool(capacity_mm[e] > t_k + 2 * c_k + 1e-9)

    overflow = tuple(
        CorridorCut(
            edge=int(e),
            demand=float(demand[e]),
            cap=float(cap_total[e]),
            owners=_owners(int(e)),
            x=float((ctx.eu[e][0] + ctx.ev[e][0]) / 2.0),
            y=float((ctx.eu[e][1] + ctx.ev[e][1]) / 2.0),
        )
        for e in np.flatnonzero(demand > cap_total + 1e-9)
        if len(demanders[int(e)]) > 1 or not _lone_fits(int(e), demanders[int(e)][0])
    )
    return Wires(
        branches=tuple(branches),
        order=order,
        capacity_mm=capacity_mm,
        net_layers=tuple(net_layers),
        unreachable=tuple(unreachable),
        gap_free_mm=gap_free,
        gap_cap=gap_cap,
        order_residual=residual,
        overflow=overflow,
    )

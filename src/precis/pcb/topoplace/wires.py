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

from precis.pcb.geom import segments_cross
from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.mesh import (
    OWNER_BOARD,
    OWNER_NONE,
    TopoMesh,
)

_EPS = 1e-9


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
    component's root; ``owners`` names what walls it off, in the
    ``edge_owner`` id space (part id, OWNER_BOARD, or n_parts + zone):
    the colour pass reports it as a certificate input."""

    net: int
    pin: int
    owners: tuple[int, ...]


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
    mid: list[tuple[float, float]]  # edge midpoint, python floats
    tri_verts: list[tuple[int, int, int]]
    tri_set: list[frozenset[int]]
    v2t: list[list[int]]  # vertex -> incident triangle ids (ascending)
    fan_boundary: dict[int, frozenset[int]]  # pin -> boundary edges of its fan
    pad_contrib: dict[int, list[tuple[int, int]]]  # edge -> [(pin, mask)]
    zone_contrib: dict[int, list[tuple[int, int]]]  # edge -> [(zone idx, mask)]
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
    mid = [
        (
            float(mesh.verts[mesh.edges[e, 0], 0] + mesh.verts[mesh.edges[e, 1], 0])
            / 2.0,
            float(mesh.verts[mesh.edges[e, 0], 1] + mesh.verts[mesh.edges[e, 1], 1])
            / 2.0,
        )
        for e in range(len(mesh.edges))
    ]
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
    mid_arr = np.asarray(mid, dtype=np.float64)
    tri_next: list[dict[int, tuple[tuple[int, int, float], ...]]] = []
    for t, es in enumerate(tri_edge_list):
        row: dict[int, tuple[tuple[int, int, float], ...]] = {}
        for e_in in es:
            mx, my = float(mid_arr[e_in, 0]), float(mid_arr[e_in, 1])
            nxt = []
            for e2 in es:
                if e2 == e_in:
                    continue
                t2a, t2b = edge_tris[e2]
                t2 = t2a if t2a != t else t2b
                nxt.append(
                    (
                        e2,
                        t2,
                        math.hypot(
                            mx - float(mid_arr[e2, 0]), my - float(mid_arr[e2, 1])
                        ),
                    )
                )
            row[e_in] = tuple(nxt)
        tri_next.append(row)
    return _Ctx(
        n_layers=n_layers,
        all_mask=all_mask,
        mid=mid,
        tri_verts=tri_verts,
        tri_set=[frozenset(t) for t in tri_verts],
        v2t=v2t,
        fan_boundary=fan_boundary,
        pad_contrib=pad_contrib,
        zone_contrib=zone_contrib,
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
    for _ in range(3):
        changed = False
        for i in range(1, len(out) - 1):
            w = _vertex_at(ctx, mesh, out[i])
            if w is None or obstacle(w):
                continue
            cand = out[:i] + out[i + 1 :]
            try:
                _t2, edges2, _tt = _retrack(ctx, mesh, va, vb, cand, passable)
            except ValueError:
                continue
            if all(passable(int(e)) for e in edges2):
                out = cand
                changed = True
                break
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
) -> tuple[tuple[int, ...], ...]:
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
    _repair_order(ctx, branches, cdir, ids)
    return tuple(tuple(lst) for lst in ids)


def _repair_order(
    ctx: _Ctx,
    branches: tuple[WirePath, ...],
    cdir: list[list[float]],
    ids: list[list[int]],
) -> None:
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
) -> tuple[int, ...]:
    """What separates an unreachable component's region from the main
    tree's: owners of the blocked edges on the cut between the two
    flooded regions — part ids, ``n_parts + zone`` or OWNER_BOARD (the
    ``edge_owner`` id space). Falls back to the region's whole blocked
    boundary when the regions meet only at vertices."""
    n_parts = len(d.parts)
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

    owners: set[int] = set()
    for e in cut or boundary:
        blame(e, owners)
    return tuple(sorted(owners))


def _blocked_masks(
    ctx: _Ctx, d: TopoDesign, cand_mask: int, net_name: str, own: frozenset[int]
) -> np.ndarray:
    """The net's passable mask — an edge is open on a candidate layer
    unless a pin of ANOTHER net, a foreign-net zone or the board edge
    blocks it there (R-a + round-2 ruling: exemption keys on the wire's
    net; a foreign-net pad on an endpoint part stays a wall)."""
    full_blocked = np.zeros(len(ctx.board), dtype=np.int64)
    for e, lst in ctx.pad_contrib.items():
        m = 0
        for pin, pm in lst:
            if pin not in own:
                m |= pm
        if m:
            full_blocked[e] = m
    for e, lst in ctx.zone_contrib.items():
        m = 0
        for z, zm in lst:
            if not (d.zones[z].net and d.zones[z].net == net_name):
                m |= zm
        if m:
            full_blocked[e] |= m
    return ((np.int64(cand_mask) & ~full_blocked) != 0) & ~ctx.board


def _net_tree(
    ctx: _Ctx,
    mesh: TopoMesh,
    d: TopoDesign,
    k: int,
    n_layers: int,
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
    base_pass = _blocked_masks(ctx, d, cand_mask, net_name, frozenset(pins))

    # A* heuristic: |mid(e) - pin| per edge per pin — h(e) is the min
    # over ALL pins, so the frontier chases the nearest goal instead of
    # growing a uniform disk. The full pin set never shrinks, which
    # keeps h a consistent underestimate at zero per-attach cost.
    ed_mid = np.asarray(ctx.mid, dtype=np.float64)
    pv = np.asarray(vpos, dtype=np.float64)
    pin_dist = np.hypot(ed_mid[:, 0, None] - pv[:, 0], ed_mid[:, 1, None] - pv[:, 1])
    hvec = pin_dist.min(axis=1)

    def h(e: int) -> float:
        return float(hvec[e])

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
        dist: dict[int, float] = {}
        prev: dict[int, int] = {}
        src: dict[int, int] = {}
        # (key, tag, a, b, g): relax entries key on g + h and carry g so
        # a stale pop is detected against dist; goal entries key on the
        # exact total and carry it in b (encoded state) and g (unused).
        heap: list[tuple[float, int, int, int, float]] = []
        in_tree = {root}
        # attach[j] = (branch length, encoded goal state | None, src pin | None)
        attach: dict[int, tuple[float, int | None, int | None]] = {
            root: (0.0, None, None)
        }

        def push_state(
            e0: int,
            t2: int,
            cost: float,
            j: int,
            *,
            dist: dict[int, float] = dist,
            src: dict[int, int] = src,
            heap: list[tuple[float, int, int, int, float]] = heap,
        ) -> None:
            st = e0 * span + t2
            if cost >= dist.get(st, INF) - 1e-12:
                return
            dist[st] = cost
            src[st] = j
            heapq.heappush(heap, (cost + h(e0), 0, e0, t2, cost))

        def seed(j: int) -> None:
            # Prim: a fresh attach re-seeds at cost 0 — the heap key is
            # distance-to-tree, not distance-to-root (round-2 finding 3).
            vj = vpos[j]
            for t0 in ctx.v2t[verts[j]]:
                for e0 in ctx.tri_edge_list[t0]:
                    if not base_pass[e0]:
                        continue
                    t2a, t2b = ctx.edge_tris[e0]
                    t2 = t2a if t2a != t0 else t2b
                    if t2 < 0:
                        continue
                    push_state(e0, t2, math.dist(vj, ctx.mid[e0]), j)

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
                attach[j2] = (
                    key,
                    b_ if b_ >= 0 else None,
                    -b_ - 1 if b_ < 0 else None,
                )
                seed(j2)
                push_goals(j2)
                continue
            e, t = a_, b_
            st = e * span + t
            if g > dist.get(st, INF) + 1e-9:
                continue
            for j2 in tri_pins.get(t, ()):
                if j2 in unplaced:
                    total = g + math.dist(ctx.mid[e], vpos[j2])
                    heapq.heappush(heap, (total, 1, j2, st, total))
            for e2, t2, wstep in ctx.tri_next[t].get(e, ()):
                if t2 < 0 or not base_pass[e2]:
                    continue
                nxt = e2 * span + t2
                nd = g + wstep
                if nd < dist.get(nxt, INF) - 1e-12:
                    dist[nxt] = nd
                    prev[nxt] = st
                    src[nxt] = src[st]
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
            chain: list[int] = []
            s = int(enc)
            while s in prev:
                chain.append(s)
                s = prev[s]
            chain.append(s)
            chain.reverse()
            i = src[chain[0]]
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


def build_wires(mesh: TopoMesh, d: TopoDesign, n_layers: int) -> Wires:
    """Every net's wire tree on the mesh — spec §2's wires. Returns a
    :class:`Wires`; a component of a net no path can link to the net's
    main component on any candidate layer is wired internally and
    named once on :attr:`Wires.unreachable` for the colour pass to
    report."""
    ctx = _make_ctx(mesh, d, n_layers)
    pin_vids = frozenset(int(v) for v in mesh.pin_vertex)
    branches: list[WirePath] = []
    net_layers: list[frozenset[int]] = []
    unreachable: list[Unreachable] = []
    for k, pins_arr in enumerate(d.net_pins):
        net_name = d.net_name[k] if k < len(d.net_name) else str(k)
        cand = _net_layers(d, k, n_layers)
        net_layers.append(cand)
        found, comps, base_pass = _net_tree(ctx, mesh, d, k, n_layers)
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
                    ctx, mesh, d, own, regions[ci], main_region, cand_mask, net_name
                )
                unreachable.append(
                    Unreachable(net=k, pin=pins_l[comp[0]], owners=owners)
                )
    return Wires(
        branches=tuple(branches),
        order=_order(ctx, mesh, tuple(branches)),
        capacity_mm=_edge_capacity_mm(ctx, mesh, d, np.asarray(d.pose0)),
        net_layers=tuple(net_layers),
        unreachable=tuple(unreachable),
    )

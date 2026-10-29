"""The slice-2 CDT mesh (spec Decision 1): a constrained triangulation
of pad centres plus footprint-courtyard and board-outline corners, with
courtyard/outline edges kept as constraints so no wire's dual path can
thread a part body. Wires (``wires.py``) are sequences of crossed mesh
edges; this module is only the topology they thread through.

Construction (Decision 2's fallback, chosen over shapely's CDT because
``constrained_delaunay_triangles`` takes polygon input only — it cannot
hold pad points inside hole-free footprint interiors): a scipy Delaunay
over every vertex, then constraint recovery per ring edge — Lawson-style
flips where the crossed edge's quadrilateral is strictly convex, and a
cavity re-triangulation (two simple polygons, ear-clipped) when no
crossed edge is flippable. Two constrained rings that properly CROSS are
not refused: the crossing point becomes a vertex both rings recover
through (real poses legitimately overlap — courtyard margins are
advisory, edge connectors hang over the outline). An unrecoverable
cavity or a runaway flip loop still raises :class:`MeshConflictError`.

Constraint ownership (:attr:`TopoMesh.edge_owner`) is what lets a wire
leave its own part: a ring edge knows WHICH part's courtyard it is (two
owners where rings coincide), and :attr:`TopoMesh.edge_side` which
layers it blocks — a bottom SMD's ring is no obstacle to a top-layer
wire; a through-hole part, the board edge and a drilled zone block
every layer. Overlaps are reported as DATA on :attr:`TopoMesh.conflicts`
(part-index pairs with the exact intersection area and mount-side
split, the same split ``check_courtyard_overlap`` uses — a same-side
pair is a violation, an opposite-side pair is legal facing), with
part-on-board-edge escapes separated onto ``outline_conflicts`` and
part-on-zone overlaps on ``zone_conflicts``.

A segment that passes exactly through a vertex splits into sub-segments
at the hit; each piece recovers (usually trivially — the piece is already
an edge) and the whole chain is marked constrained. Ear clipping can
orphan a vertex strictly inside a cavity (it keeps the id but no
triangle references it); a pad vertex in NO triangle (a pad outside the
board outline, or one the centroid filter stranded) is refused with a
fix hint rather than left for the wire builder to fall over, and the
Euler invariant is stated over REFERENCED vertices.

Vias are NOT mesh vertices (Reto ruling 2026-10-25): a via site is a
position ``(edge_id, t)`` inside a wire, so the mesh stays frozen and
edge ids stable for the order lists.

Delaunay-ness is not restored after recovery (no Lawson pass): the mesh
is a rubber-band channel structure, not a mesh-quality object — a
crossed-edge walk only needs the constraint edges present.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.spatial import Delaunay, QhullError

from precis.errors import BadInput
from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.proxy import zone_world_ring

#: Orientation epsilon (mm²). Courtyard coordinates carry transform dust
#: at ~1e-13; 1e-9 keeps true collinearities (shared pad rows) exact while
#: still calling hair-width non-crossings what they are.
_EPS = 1e-9
#: Hard bound on flip rounds per constraint segment — a bound rather than
#: a correctness argument: the loop below either converges in a few
#: rounds or the cavity path is unreachable through flips.
_MAX_FLIPS = 10000


class MeshConflictError(BadInput):
    """A constraint edge could not be recovered — a degenerate strip,
    a runaway flip loop, a recovered edge that vanished, a pad stranded
    outside every triangle. Internal defect or unplaceable pose, not a
    pose judgement: crossing rings are split at their intersection and
    reported on :attr:`TopoMesh.conflicts` instead. A :class:`BadInput`
    so the refusal can carry the ``next=`` fix hint."""


#: ``edge_owner`` sentinels. Board-outline edges carry ``OWNER_BOARD``;
#: the second owner slot of a singly-owned edge pads with
#: ``OWNER_NONE`` (distinct so a ring coincident with the board edge
#: still reads as two owners); a zone edge carries ``n_parts +
#: zone_index`` into :attr:`TopoDesign.zones`.
OWNER_BOARD = -1
OWNER_NONE = -2

#: ``edge_side`` block classes — which copper layers an owner blocks for
#: a wire (slice-2a seam item 2). ``SIDE_TOP``/``SIDE_BOTTOM`` block only
#: that outer layer (an SMD body lives on one face); ``SIDE_ALL`` blocks
#: every layer (TH part, board edge, drilled zone); ``SIDE_ZONE`` defers
#: to the zone's own ``layers`` list (a layer-scoped keepout).
SIDE_TOP = np.int8(0)
SIDE_BOTTOM = np.int8(1)
SIDE_ALL = np.int8(2)
SIDE_ZONE = np.int8(3)


def edge_blocks_layer(
    mesh: TopoMesh, d: TopoDesign, e: int, layer: int, n_layers: int
) -> bool:
    """Whether constrained edge ``e`` blocks a wire on ``layer`` — the
    ``edge_side`` classes resolved: SMD blocks its own outer face, TH/
    board/drilled-zone everything, a scoped keepout its ``layers``."""
    for slot in range(2):
        owner = int(mesh.edge_owner[e, slot])
        if owner == OWNER_NONE:
            break
        side = int(mesh.edge_side[e, slot])
        if (
            side == SIDE_ALL
            or (side == SIDE_TOP and layer == 0)
            or (side == SIDE_BOTTOM and layer == n_layers - 1)
        ):
            return True
        if side == SIDE_ZONE and (
            not d.zones[owner - len(d.parts)].layers
            or layer in d.zones[owner - len(d.parts)].layers
        ):
            return True
    return False


@dataclass(frozen=True, slots=True)
class MeshConflict:
    """One courtyard-overlap pair (slice-2a seam item 6): part indices
    ``a``/``b`` (``a`` < ``b``), the exact intersection area in mm², and
    ``side`` — 0 both top-mounted, 1 both bottom-mounted, 2 opposite
    sides. ``same_side`` is the violation half (the
    ``bottom_by_refdes`` split ``check_courtyard_overlap`` flags): an
    opposite-side overlap is two parts facing across the board, legal."""

    a: int
    b: int
    area_mm2: float
    side: int

    @property
    def same_side(self) -> bool:
        return self.side < 2


@dataclass(frozen=True, slots=True)
class TopoMesh:
    """The frozen mesh (spec §2). ``constrained`` marks courtyard,
    board-outline and zone edges; everything else is a free portal a
    wire's dual path may cross — and a constrained edge a wire's own
    part owns is passable for that wire (``edge_owner``). ``pad_vertex
    [(part, pin)]`` uses the GLOBAL pin id (index into the design's pin
    arrays)."""

    verts: np.ndarray  # (V, 2) float64 mm
    tris: np.ndarray  # (T, 3) int32, CCW
    edges: np.ndarray  # (E, 2) int32, sorted vertex pairs
    tri_edges: np.ndarray  # (T, 3) int32
    edge_tris: np.ndarray  # (E, 2) int32, -1 on the hull
    constrained: np.ndarray  # (E,) bool — courtyard / zone / board edge
    pad_vertex: Mapping[tuple[int, int], int]  # (part, pin id) → vertex
    pin_vertex: np.ndarray = field(
        default_factory=lambda: np.zeros(0, np.int32)
    )  # (P,) — ``pad_vertex`` flattened for the wire builder
    #: (E, 2) — who owns each constrained edge (``OWNER_BOARD`` board
    #: outline, ``n_parts + z`` a :attr:`TopoDesign.zones` entry, else a
    #: part id; ``OWNER_NONE`` pads a singly-owned edge's second slot).
    edge_owner: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), np.int32))
    #: (E, 2) — the ``SIDE_*`` block class per owner slot.
    edge_side: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), np.int8))
    #: Courtyard-overlap pairs — crossings, containment AND coincident
    #: rings, each with the exact intersection area and mount-side
    #: split. Sorted by (a, b).
    conflicts: tuple[MeshConflict, ...] = ()
    #: (part, area_mm²) — a part's courtyard area outside the board
    #: outline (an overhanging connector is legal; reported, never a
    #: same-side violation).
    outline_conflicts: tuple[tuple[int, float], ...] = ()
    #: (part, zone index, area_mm²) — a part's courtyard over a keepout,
    #: mounting hole or fixed copper zone.
    zone_conflicts: tuple[tuple[int, int, float], ...] = ()


# Vertex rows the hot predicates accept — a plain list indexes ~10x
# faster than ndarray rows, so the recovery/split loops hand down
# ``varr.tolist()`` snapshots (round-4 perf: _orient was the build cost).
_Verts = np.ndarray | list[list[float]]


def _orient(verts: _Verts, a: int, b: int, c: int) -> float:
    """Signed double area of triangle (a, b, c)."""
    ax, ay = verts[a]
    bx, by = verts[b]
    cx, cy = verts[c]
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _proper_cross(verts: _Verts, u: int, v: int, p: int, q: int) -> bool:
    """Segments (u,v) and (p,q) properly cross — the same proper-straddle
    predicate as ``geom.segments_cross``, indexed form (shared endpoints
    never count)."""
    if u in (p, q) or v in (p, q):
        return False
    d1, d2 = _orient(verts, u, v, p), _orient(verts, u, v, q)
    d3, d4 = _orient(verts, p, q, u), _orient(verts, p, q, v)
    return ((d1 > _EPS and d2 < -_EPS) or (d1 < -_EPS and d2 > _EPS)) and (
        (d3 > _EPS and d4 < -_EPS) or (d3 < -_EPS and d4 > _EPS)
    )


def _on_segment(verts: _Verts, a: int, w: int, b: int) -> bool:
    """``w`` collinear with (a, b) and strictly between them."""
    if abs(_orient(verts, a, b, w)) > _EPS:
        return False
    ax, ay = verts[a]
    bx, by = verts[b]
    wx, wy = verts[w]
    dot = (wx - ax) * (bx - ax) + (wy - ay) * (by - ay)
    return _EPS < dot < (bx - ax) ** 2 + (by - ay) ** 2 - _EPS


class _Soup:
    """The mutable triangulation under recovery — triangles by id plus
    the edge→triangles map every local op keeps current. Ids are stable
    across flips so a cavity op sees the same numbering its caller did."""

    def __init__(self, simplices: np.ndarray) -> None:
        self.tris: dict[int, tuple[int, int, int]] = {
            i: (int(t[0]), int(t[1]), int(t[2])) for i, t in enumerate(simplices)
        }
        self._next = len(self.tris)
        self.edge_tris: dict[tuple[int, int], list[int]] = {}
        self.vertex_tris: dict[int, set[int]] = {}
        for tid, tri in self.tris.items():
            self._link(tid, tri)

    @staticmethod
    def _ekey(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a < b else (b, a)

    def _link(self, tid: int, tri: tuple[int, int, int]) -> None:
        for k in range(3):
            key = self._ekey(tri[k], tri[(k + 1) % 3])
            self.edge_tris.setdefault(key, []).append(tid)
        for v in tri:
            self.vertex_tris.setdefault(v, set()).add(tid)

    def _unlink(self, tid: int, tri: tuple[int, int, int]) -> None:
        for k in range(3):
            key = self._ekey(tri[k], tri[(k + 1) % 3])
            lst = self.edge_tris[key]
            lst.remove(tid)
            if not lst:
                del self.edge_tris[key]
        for v in tri:
            fan = self.vertex_tris[v]
            fan.discard(tid)
            if not fan:
                del self.vertex_tris[v]

    def add(self, tri: tuple[int, int, int]) -> None:
        tid = self._next
        self._next += 1
        self.tris[tid] = tri
        self._link(tid, tri)

    def drop(self, tid: int) -> None:
        self._unlink(tid, self.tris.pop(tid))

    def crossed(self, verts: _Verts, a: int, b: int) -> list[tuple[int, int]]:
        return [e for e in self.edge_tris if _proper_cross(verts, a, b, e[0], e[1])]

    def crossed_walk(
        self, verts: _Verts, a: int, b: int
    ) -> list[tuple[int, int]] | None:
        """The edges properly crossed by (a,b), in path order, by walking
        the triangulation fan→edge→fan — O(path) instead of ``crossed``'s
        full edge scan (which dominated build time at ~10³ constraints).
        ``None`` when the walk hits a degenerate exit — caller falls back
        to the full scan. Assumes no vertex lies on the open segment
        (``recover`` splits those first)."""
        start = None
        for tid in self.vertex_tris.get(a, ()):
            tri = self.tris[tid]
            x, y = (v for v in tri if v != a)
            if _proper_cross(verts, a, b, x, y):
                start = (tid, self._ekey(x, y))
                break
        if start is None:
            return []
        tid, e = start
        out = []
        while True:
            out.append(e)
            nxt = [t for t in self.edge_tris.get(e, ()) if t != tid]
            if not nxt:
                return None  # walked off the hull — degenerate
            tid = nxt[0]
            tri = self.tris[tid]
            if b in tri:
                return out
            exit_e = None
            for k in range(3):
                u, v = tri[k], tri[(k + 1) % 3]
                ek = self._ekey(u, v)
                if ek != e and _proper_cross(verts, a, b, u, v):
                    exit_e = ek
                    break
            if exit_e is None:
                return None
            e = exit_e

    def flip(self, verts: _Verts, e: tuple[int, int]) -> bool:
        """Replace edge ``e`` by its quadrilateral's other diagonal when
        that diagonal lies strictly inside (strictly convex quad — the
        flip stays a triangulation). Returns False when not flippable."""
        tids = self.edge_tris.get(e)
        if not tids or len(tids) != 2:
            return False
        t1, t2 = (self.tris[t] for t in tids)
        p = next(v for v in t1 if v not in e)
        q = next(v for v in t2 if v not in e)
        if p == q or not _proper_cross(verts, e[0], e[1], p, q):
            return False
        # ``tids`` is the live ``edge_tris[e]`` list — unlinking inside
        # the loop mutates it mid-iteration, so iterate a frozen copy.
        for tid, tri in zip(tuple(tids), ((p, q, e[0]), (p, q, e[1]))):
            self._unlink(tid, self.tris[tid])
            self.tris[tid] = tri
            self._link(tid, tri)
        return True

    def t_junction(
        self, verts: _Verts, a: int, b: int
    ) -> tuple[tuple[int, int], int] | None:
        """An endpoint of (a, b) lying strictly on an existing edge —
        the T-junction the proper-crossing walk cannot see (an ear-clip
        sliver leaves its pruned collinear vertex exactly ON an edge,
        so a later constraint ending there finds a fan with no proper
        exit). Returns ``((p, q), w)`` or ``None``."""
        for p, q in self.edge_tris:
            if a not in (p, q) and _on_segment(verts, p, a, q):
                return (p, q), a
            if b not in (p, q) and _on_segment(verts, p, b, q):
                return (p, q), b
        return None

    def split_at_vertex(self, e: tuple[int, int], w: int) -> None:
        """Split edge ``e = (p, q)`` at vertex ``w`` strictly on it —
        each triangle on ``e`` becomes two, ending the T-junction. A
        zero-area sliver whose apex IS ``w`` (``w`` rode the edge, the
        ear-clip's way of keeping a pruned vertex covered) collapses
        away: its two leg edges are re-emitted by the other side's
        halves."""
        p, q = e
        for tid in tuple(self.edge_tris.get(e, ())):
            tri = self.tris[tid]
            x = next(v for v in tri if v not in e)
            if x == w:
                self.drop(tid)
                continue
            self._unlink(tid, tri)
            self.tris[tid] = (p, w, x)
            self._link(tid, self.tris[tid])
            self.add((w, q, x))

    def cavity(
        self,
        verts: _Verts,
        a: int,
        b: int,
        crossed: list[tuple[int, int]],
        protected: Any = (),
    ) -> None:
        """Ear-clip fallback when no crossed edge is flippable: the strip
        of triangles the segment passes through is removed and each of
        the two simple polygons it leaves — the chains above and below
        the segment — is retriangulated, giving (a, b) as an edge."""
        strip = {t for e in crossed for t in self.edge_tris.get(e, ())}
        interior: dict[tuple[int, int], int] = {}
        boundary: dict[tuple[int, int], int] = {}
        for tid in strip:
            for k in range(3):
                tri = self.tris[tid]
                key = self._ekey(tri[k], tri[(k + 1) % 3])
                both = len(self.edge_tris.get(key, ())) == 2 and all(
                    t in strip for t in self.edge_tris[key]
                )
                (interior if both else boundary)[key] = (
                    interior.get(key, 0) + 1 if both else 1
                )
        # An interior strip edge that is CONSTRAINED would be silently
        # lost by the removal — refuse instead.
        lost = [e for e in interior if e in protected]
        if lost:
            raise MeshConflictError(
                f"constraint strip for ({a},{b}) holds already-constrained "
                f"edge(s) {lost[:4]} — degenerate geometry"
            )
        # The boundary is one cycle through a and b; walk both arcs.
        adj: dict[int, list[int]] = {}
        for u, v in boundary:
            adj.setdefault(u, []).append(v)
            adj.setdefault(v, []).append(u)
        if a not in adj or b not in adj:
            raise MeshConflictError(
                f"constraint strip for ({a},{b}) is not closed — degenerate geometry"
            )
        arcs: list[list[int]] = []
        for first in adj[a]:
            arc = [a, first]
            while arc[-1] != b:
                nxt = [w for w in adj[arc[-1]] if w != arc[-2]]
                if len(nxt) != 1 or len(arc) > 2 * len(adj):
                    raise MeshConflictError(
                        f"constraint strip for ({a},{b}) is not a simple "
                        "cycle — degenerate geometry"
                    )
                arc.append(nxt[0])
            arcs.append(arc)
        if len(arcs) != 2 or set(arcs[0][1:-1]) & set(arcs[1][1:-1]):
            raise MeshConflictError(
                f"constraint strip for ({a},{b}) has crossing chains — "
                "degenerate geometry"
            )
        new_tris: list[tuple[int, int, int]] = []
        for arc in arcs:
            new_tris += _ear_clip(verts, arc)
        for tid in strip:
            self.drop(tid)
        for tri in new_tris:
            self.add(tri)


def _area2(verts: _Verts, poly: list[int]) -> float:
    s = 0.0
    for i, v in enumerate(poly):
        w = poly[(i + 1) % len(poly)]
        s += verts[v][0] * verts[w][1] - verts[w][0] * verts[v][1]
    return s


def _ear_clip(verts: _Verts, poly: list[int]) -> list[tuple[int, int, int]]:
    """Ear-clipped triangulation of a simple polygon (vertex ids, any
    winding — normalised to CCW first). Collinear-on-the-boundary verts
    are pruned, which is what keeps pad-row chains clip-able. Raises
    :class:`MeshConflictError` on a stalled clip rather than emitting a
    partial fill."""
    idx = list(poly)
    if _area2(verts, idx) < 0:
        idx.reverse()
    out: list[tuple[int, int, int]] = []
    changed = True
    while changed:
        changed = False
        for i, cur in enumerate(list(idx)):
            prev, nxt = idx[i - 1], idx[(i + 1) % len(idx)]
            if _on_segment(verts, prev, cur, nxt) or (
                abs(_orient(verts, prev, cur, nxt)) <= _EPS
                and (verts[nxt][0] - verts[cur][0]) * (verts[cur][0] - verts[prev][0])
                + (verts[nxt][1] - verts[cur][1]) * (verts[cur][1] - verts[prev][1])
                > 0
            ):
                # The (prev,nxt) edge the prune leaves behind spans cur —
                # emit the (possibly zero-area) sliver so the boundary
                # chain stays covered and no hole opens in the mesh.
                out.append((prev, cur, nxt))
                del idx[i]
                changed = True
                break
    if len(idx) == 3:
        return out + [(idx[0], idx[1], idx[2])]
    if len(idx) < 3:
        return out

    def inside(p: int, a: int, b: int, c: int) -> bool:
        return (
            _orient(verts, a, b, p) > _EPS
            and _orient(verts, b, c, p) > _EPS
            and _orient(verts, c, a, p) > _EPS
        )

    guard = 0
    while len(idx) > 3:
        guard += 1
        if guard > 4 * len(idx) * len(idx) + 64:
            raise MeshConflictError("ear clip stalled on a degenerate cavity")
        m = len(idx)
        for i in range(m):
            prev, cur, nxt = idx[i - 1], idx[i], idx[(i + 1) % m]
            if _orient(verts, prev, cur, nxt) <= _EPS:
                continue
            if any(inside(w, prev, cur, nxt) for w in idx if w not in (prev, cur, nxt)):
                continue
            out.append((prev, cur, nxt))
            del idx[i]
            break
        else:
            raise MeshConflictError("ear clip found no convex ear — degenerate cavity")
    out.append((idx[0], idx[1], idx[2]))
    return out


def _place(pts: np.ndarray, row: np.ndarray) -> np.ndarray:
    """Footprint-local points → world under a ``(x, y, rot_quarter,
    side)`` pose row — the same mirror-then-quarter-turn transform as
    :func:`precis.pcb.topoplace.proxy.pin_world`, on arbitrary points
    (courtyard rings, not only pins)."""
    x, y, rot_q, side = (float(v) for v in row[:4])
    mx = np.where(side >= 0.5, -pts[:, 0], pts[:, 0])
    ly = pts[:, 1]
    r = round(rot_q) % 4
    if r == 0:
        rx, ry = mx, ly
    elif r == 1:  # 90° cw-from-north: (x, y) -> (y, -x)
        rx, ry = ly, -mx
    elif r == 2:
        rx, ry = -mx, -ly
    else:  # r == 3
        rx, ry = -ly, mx
    return np.column_stack([x + rx, y + ry])


def _fallback_outline(d: TopoDesign) -> np.ndarray:
    """The placer's domain square as a closed ring — used when the
    design carries no board outline (synthetic designs)."""
    from precis.pcb.topoplace.placer import _domain

    lo, hi = _domain(d)
    return np.asarray([[lo[0], lo[1]], [hi[0], lo[1]], [hi[0], hi[1]], [lo[0], hi[1]]])


def _ring_vids(
    ring: np.ndarray,
    add_vert: Any,
) -> list[int]:
    """Ring (closed, first point repeated last or not) → vertex ids of
    its distinct corner points, without the closing duplicate."""
    pts = np.asarray(ring, dtype=np.float64)
    if len(pts) >= 2 and np.allclose(pts[0], pts[-1], atol=1e-9):
        pts = pts[:-1]
    return [add_vert(float(p[0]), float(p[1])) for p in pts]


def _seg_params(
    varr: _Verts, a: int, b: int, p: int, q: int
) -> tuple[float, float, tuple[float, float]] | None:
    """Proper crossing of (a,b) × (p,q) → ``(t, u, point)`` — ``t`` the
    fraction along a→b, ``u`` along p→q — else ``None``."""
    if not _proper_cross(varr, a, b, p, q):
        return None
    ax, ay = varr[a]
    bx, by = varr[b]
    px, py = varr[p]
    qx, qy = varr[q]
    dx, dy = bx - ax, by - ay
    ex, ey = qx - px, qy - py
    denom = dx * ey - dy * ex
    if abs(denom) <= _EPS:
        return None
    t = ((px - ax) * ey - (py - ay) * ex) / denom
    u = ((px - ax) * dy - (py - ay) * dx) / denom
    return t, u, (ax + t * dx, ay + t * dy)


def _split_ring_crossings(
    varr: _Verts,
    ring_segs: list[tuple[int, list[tuple[int, int]]]],
    add_vert: Any,
) -> list[tuple[int, int, int]]:
    """Where two rings' segments properly cross, mint the crossing point
    as a vertex and split both into sub-segments — afterwards no two
    constrained segments can properly cross, so recovery never meets a
    constrained wall. Returns the (sub-)segment constraints as
    ``(u, v, owner)`` triples; owners are part ids, ``OWNER_BOARD`` for
    the outline, or ``n_parts + zone_index``. Ring-pair bboxes prune
    the pair scan to actual overlaps."""
    flat: list[tuple[int, int, int]] = []
    ring_first: list[int] = []
    rbbox: list[tuple[float, float, float, float]] = []
    for owner, segs in ring_segs:
        xs = [varr[u][0] for u, _ in segs] + [varr[v][0] for _, v in segs]
        ys = [varr[u][1] for u, _ in segs] + [varr[v][1] for _, v in segs]
        ring_first.append(len(flat))
        rbbox.append((min(xs), min(ys), max(xs), max(ys)))
        flat += [(u, v, owner) for u, v in segs]

    splits: dict[int, list[tuple[float, tuple[float, float]]]] = {}
    for i in range(len(ring_segs)):
        ax0, ay0, ax1, ay1 = rbbox[i]
        segs_i = ring_segs[i][1]
        # Per-segment bboxes of ring i, hoisted out of the j loop — each
        # is reused against every overlapping ring below.
        i_x0 = [0.0] * len(segs_i)
        i_y0 = [0.0] * len(segs_i)
        i_x1 = [0.0] * len(segs_i)
        i_y1 = [0.0] * len(segs_i)
        for si, (a, b) in enumerate(segs_i):
            ax_a, ay_a = varr[a]
            bx_b, by_b = varr[b]
            i_x0[si] = ax_a if ax_a < bx_b else bx_b
            i_y0[si] = ay_a if ay_a < by_b else by_b
            i_x1[si] = ax_a if ax_a > bx_b else bx_b
            i_y1[si] = ay_a if ay_a > by_b else by_b
        for j in range(i + 1, len(ring_segs)):
            bx0, by0, bx1, by1 = rbbox[j]
            if (
                ax1 < bx0 - _EPS
                or bx1 < ax0 - _EPS
                or ay1 < by0 - _EPS
                or by1 < ay0 - _EPS
            ):
                continue
            segs_j = ring_segs[j][1]
            # Segment-level bbox prune: ring bboxes overlap coarse (the
            # board outline's bbox contains every part, so outline×part
            # pairs used to test EVERY segment pair — tens of millions of
            # _proper_cross calls per build). Disjoint segment boxes
            # cannot properly cross, so the mask rejects them exactly;
            # survivors go through the same _seg_params as before.
            j_x0 = [0.0] * len(segs_j)
            j_y0 = [0.0] * len(segs_j)
            j_x1 = [0.0] * len(segs_j)
            j_y1 = [0.0] * len(segs_j)
            for sj, (p, q) in enumerate(segs_j):
                px_p, py_p = varr[p]
                qx_q, qy_q = varr[q]
                j_x0[sj] = px_p if px_p < qx_q else qx_q
                j_y0[sj] = py_p if py_p < qy_q else qy_q
                j_x1[sj] = px_p if px_p > qx_q else qx_q
                j_y1[sj] = py_p if py_p > qy_q else qy_q
            for si in range(len(segs_i)):
                sax0, say0, sax1, say1 = i_x0[si], i_y0[si], i_x1[si], i_y1[si]
                a, b = flat[ring_first[i] + si][0], flat[ring_first[i] + si][1]
                for sj in range(len(segs_j)):
                    if (
                        j_x1[sj] < sax0 - _EPS
                        or sax1 < j_x0[sj] - _EPS
                        or j_y1[sj] < say0 - _EPS
                        or say1 < j_y0[sj] - _EPS
                    ):
                        continue
                    p, q = segs_j[sj]
                    hit = _seg_params(varr, a, b, p, q)
                    if hit is None:
                        continue
                    t, u, pt = hit
                    gi = ring_first[i] + si
                    gj = ring_first[j] + sj
                    splits.setdefault(gi, []).append((t, pt))
                    splits.setdefault(gj, []).append((u, pt))

    constraints: list[tuple[int, int, int]] = []
    for gi, (a, b, owner) in enumerate(flat):
        chain = [a]
        for _t, pt in sorted(splits.get(gi, ()), key=lambda s: s[0]):
            chain.append(add_vert(float(pt[0]), float(pt[1])))
        chain.append(b)
        for k in range(len(chain) - 1):
            if chain[k] != chain[k + 1]:
                constraints.append((chain[k], chain[k + 1], owner))
    return constraints


def _conflict_data(
    d: TopoDesign,
    world_rings: list[np.ndarray | None],
    pose: np.ndarray,
    outline: np.ndarray,
    world_bodies: list[np.ndarray | None],
) -> tuple[
    tuple[MeshConflict, ...],
    tuple[tuple[int, float], ...],
    tuple[tuple[int, int, float], ...],
]:
    """Courtyard overlaps, outline escapes and part-on-zone overlaps as
    DATA (slice-2a seam item 6): exact shapely intersection areas, not
    the segment-crossing flag — containment (a small part inside
    another's ring) and coincident rings count, and the mount-side
    split matches ``check_courtyard_overlap``'s ``bottom_by_refdes``:
    a same-side pair is the placer violation Reto priced (2026-10-25),
    an opposite-side pair is legal facing. Bbox masks prune the pair
    scan before any polygon work.

    The zone test (the ``courtyard_hole`` hard gate) measures the part
    BODY (``world_bodies``, the zero-clearance pad union) against the
    zone polygon — gr529963's ruling (Reto 2026-10-28): the hole's
    keep-out ring already carries its single margin claim in its radius,
    so the part's 0.4mm house courtyard margin must not stack on top of
    it. Part-part conflicts and outline escapes stay on the courtyard
    rings — the house margin IS between-parts breathing room."""
    from shapely.geometry import Polygon

    def _poly(ring: np.ndarray | None) -> Polygon | None:
        if ring is None or len(ring) < 4:
            return None
        g = Polygon(ring[:-1])
        return g if not g.is_empty and g.area > _EPS else None

    parts = [_poly(r) for r in world_rings]
    conflicts: list[MeshConflict] = []
    sides = pose[:, 3] >= 0.5
    for i, gi in enumerate(parts):
        if gi is None:
            continue
        x0, y0, x1, y1 = gi.bounds
        for j in range(i + 1, len(parts)):
            gj = parts[j]
            if gj is None:
                continue
            bx0, by0, bx1, by1 = gj.bounds
            if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                continue
            area = float(gi.intersection(gj).area)
            if area <= _EPS:
                continue
            side = (
                0
                if not sides[i] and not sides[j]
                else (1 if sides[i] and sides[j] else 2)
            )
            conflicts.append(MeshConflict(a=i, b=j, area_mm2=area, side=side))
    outline_conflicts: list[tuple[int, float]] = []
    zone_conflicts: list[tuple[int, int, float]] = []
    ogeom = _poly(np.vstack([outline, outline[:1]])) if len(outline) >= 3 else None
    # label_keepout zones (reserved refdes-label boxes) never conflict a
    # part — they exist only on the route design and the parts are
    # already placed; counting them here would mint a bogus
    # part-on-zone finding per label.
    zgeoms = [
        None if z.kind == "label_keepout" else _poly(zone_world_ring(d, z, pose))
        for z in d.zones
    ]
    bodies = [_poly(r) for r in world_bodies]
    for i, gi in enumerate(parts):
        if gi is None:
            continue
        if ogeom is not None:
            outside = float(gi.area - gi.intersection(ogeom).area)
            if outside > _EPS:
                outline_conflicts.append((i, outside))
    for i, bi in enumerate(bodies):
        if bi is None:
            continue
        for z, zg in enumerate(zgeoms):
            if zg is None:
                continue
            area = float(bi.intersection(zg).area)
            # the hole's owner covers it by design — courtyard_hole's
            # exemption, so the placement passes do not shove a
            # connector off its own standoff
            if d.zones[z].part == i:
                continue
            if area > _EPS:
                zone_conflicts.append((i, z, area))
    return (
        tuple(sorted(conflicts, key=lambda c: (c.a, c.b))),
        tuple(sorted(outline_conflicts)),
        tuple(sorted(zone_conflicts)),
    )


def build_mesh(d: TopoDesign, pose: np.ndarray) -> TopoMesh:
    """The CDT over pad centres + pad-outline corners + courtyard
    corners + outline corners at ``pose`` — spec Decision 1/2, with
    round-3 seam R3-1's pad polygons so a pad's wall is the pad itself.

    Every vertex set is deduplicated by 1e-9-rounded coordinate so a
    pad sitting exactly on a courtyard corner still lands on ONE vertex.
    Courtyard rings come from :attr:`TopoDesign.part_outline` (closed
    rings in the part frame); the board outline from
    :attr:`TopoDesign.outline`, or the placer's domain square when it is
    empty. Triangles outside the outline are dropped — footprint
    interiors stay triangulated (the spec's no-holes Euler invariant).
    """
    pose = np.asarray(pose, dtype=np.float64)
    n = len(d.parts)
    if pose.shape[0] != n:
        raise ValueError(f"pose has {pose.shape[0]} rows for {n} parts")
    bad = [
        d.parts[i]
        for i in range(n)
        if not (math.isfinite(pose[i, 0]) and math.isfinite(pose[i, 1]))
    ]
    if bad:
        raise ValueError(
            f"pose leaves {len(bad)} part(s) unplaced ({', '.join(bad[:5])}"
            f"{'…' if len(bad) > 5 else ''}) — scatter before meshing"
        )

    verts: list[tuple[float, float]] = []
    vid_of: dict[tuple[float, float], int] = {}

    def add_vert(x: float, y: float) -> int:
        key = (round(x, 9), round(y, 9))
        vid = vid_of.get(key)
        if vid is None:
            vid = len(verts)
            verts.append(key)
            vid_of[key] = vid
        return vid

    pin_vertex = np.full(len(d.pin_xy), -1, dtype=np.int32)
    pad_vertex: dict[tuple[int, int], int] = {}
    for pin in range(len(d.pin_xy)):
        part = int(d.pin_part[pin])
        wx, wy = _place(d.pin_xy[pin : pin + 1], pose[part])[0]
        vid = add_vert(float(wx), float(wy))
        pin_vertex[pin] = vid
        pad_vertex[(part, pin)] = vid

    ring_segs: list[tuple[int, list[tuple[int, int]]]] = []
    world_rings: list[np.ndarray | None] = [None] * n
    world_bodies: list[np.ndarray | None] = [None] * n
    for part in range(n):
        ring = d.part_outline[part] if part < len(d.part_outline) else ()
        pts = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
        if len(pts) < 4:
            continue
        # Rings place under the REAL pose — mirrored for a bottom-side
        # part, like the pins, so the ring provably contains its own
        # pads (the physical courtyard; slice-2a seam item 4).
        wpts = _place(pts, pose[part])
        world_rings[part] = wpts
        vids = _ring_vids(wpts, add_vert)
        m = len(vids)
        segs = [
            (vids[i], vids[(i + 1) % m])
            for i in range(m)
            if vids[i] != vids[(i + 1) % m]
        ]
        if segs:
            ring_segs.append((part, segs))
        # The pad-union BODY world ring (gr529963): the part-on-zone
        # conflict test measures this against the zone polygon — the
        # fab bar prices the body, the courtyard above stays the
        # between-parts reservation. A part with no body ring
        # (synthetic designs) falls back to its courtyard ring.
        body = d.part_body[part] if part < len(d.part_body) else ()
        bpts = np.asarray(body, dtype=np.float64).reshape(-1, 2)
        world_bodies[part] = _place(bpts, pose[part]) if len(bpts) >= 4 else wpts

    # Pad outline corners (round-3 seam R3-1): each pad's ``pin_wh``
    # rectangle becomes a constrained ring owned by its part. Without
    # it a pad's wall is wherever its CDT fan's skinny triangles reach
    # — a column of close pads leaves no fan-boundary edge a wire may
    # cross, so a pin with open copper beside it still reports caged.
    # With the ring constrained the pad vertex's fan is contained by
    # the pad polygon itself, and the wall is the pad.
    for pin in range(len(d.pin_xy)):
        if len(d.pin_wh) <= pin:
            continue
        pw, ph = float(d.pin_wh[pin, 0]), float(d.pin_wh[pin, 1])
        if pw <= _EPS or ph <= _EPS:
            continue
        part = int(d.pin_part[pin])
        cx, cy = float(d.pin_xy[pin, 0]), float(d.pin_xy[pin, 1])
        wpts = _place(
            np.asarray(
                [
                    [cx - pw / 2, cy - ph / 2],
                    [cx + pw / 2, cy - ph / 2],
                    [cx + pw / 2, cy + ph / 2],
                    [cx - pw / 2, cy + ph / 2],
                ]
            ),
            pose[part],
        )
        vids = _ring_vids(wpts, add_vert)
        m = len(vids)
        segs = [
            (vids[i], vids[(i + 1) % m])
            for i in range(m)
            if vids[i] != vids[(i + 1) % m]
        ]
        if segs:
            ring_segs.append((part, segs))

    # Non-part constrained regions (keepouts, mounting holes, fixed
    # copper) — world-frame already; owner ``n + zone_index``.
    # label_keepout zones (reserved refdes-label boxes) are VIA-only:
    # silk prints over traces freely, so wires may cross a label box;
    # only a via's annulus conflicts with the ink. The via-siting walk
    # reads these zones itself (realise.py's keepout list) — excluding
    # them here keeps the wire corridors open (taz-leaf widen fixtures
    # showed a 2 mm label box otherwise eating a rail's corridor).
    for z, zone in enumerate(d.zones):
        if zone.kind == "label_keepout":
            continue
        zpts = np.asarray(zone_world_ring(d, zone, pose), dtype=np.float64).reshape(
            -1, 2
        )
        if len(zpts) < 4:
            continue
        vids = _ring_vids(zpts, add_vert)
        m = len(vids)
        segs = [
            (vids[i], vids[(i + 1) % m])
            for i in range(m)
            if vids[i] != vids[(i + 1) % m]
        ]
        if segs:
            ring_segs.append((n + z, segs))

    outline = np.asarray(d.outline, dtype=np.float64).reshape(-1, 2)
    if len(outline) < 3:
        outline = _fallback_outline(d)
    ovids = _ring_vids(outline, add_vert)
    osegs = [
        (ovids[i], ovids[(i + 1) % len(ovids)])
        for i in range(len(ovids))
        if ovids[i] != ovids[(i + 1) % len(ovids)]
    ]
    if osegs:
        ring_segs.append((OWNER_BOARD, osegs))

    constraints = _split_ring_crossings(
        np.asarray(verts, dtype=np.float64).tolist(), ring_segs, add_vert
    )

    varr = np.asarray(verts, dtype=np.float64)
    vlist = varr.tolist()
    if len(varr) < 3:
        raise MeshConflictError("fewer than 3 mesh vertices — nothing to triangulate")
    try:
        soup = _Soup(Delaunay(varr).simplices)
    except QhullError as exc:
        raise MeshConflictError(
            f"degenerate vertex set — Qhull refused: {exc}"
        ) from exc

    # A part's side class for ``edge_side``: TH pins ride every layer, so
    # a part with any TH pad blocks all layers; an SMD-only part blocks
    # just the outer layer on its CURRENT mount side (the pose's, which
    # the mover can flip — not ``pin_side``'s authored side).
    has_th = np.zeros(n, dtype=bool)
    for pin in range(len(d.pin_xy)):
        if int(d.pin_side[pin]) == 2:
            has_th[int(d.pin_part[pin])] = True

    def _side_of(owner: int) -> int:
        if owner == OWNER_BOARD:
            return int(SIDE_ALL)
        if owner >= n:
            return int(SIDE_ZONE) if d.zones[owner - n].layers else int(SIDE_ALL)
        return int(SIDE_ALL) if has_th[owner] else int(pose[owner, 3] >= 0.5)

    def _owner_label(owner: int) -> str:
        if owner == OWNER_BOARD:
            return "board outline"
        if owner >= n:
            return d.zones[owner - n].name or f"zone {owner - n}"
        return f"{d.parts[owner]} courtyard"

    constrained_pairs: dict[tuple[int, int], int] = {}
    second_owner: dict[tuple[int, int], int] = {}

    def recover(a: int, b: int, owner: int) -> None:
        """Recover segment (a, b) — already-split at on-segment verts by
        the caller — then mark its whole chain constrained."""
        if a == b:
            return
        # A vertex exactly on the segment splits it: recover the pieces
        # (the near piece usually is already an edge). Vectorised: the
        # per-recover linear scan was the second build-time cost.
        ab = varr[b] - varr[a]
        orient = ab[0] * (varr[:, 1] - varr[a][1]) - ab[1] * (varr[:, 0] - varr[a][0])
        cand = np.flatnonzero(np.abs(orient) <= _EPS)
        cand = cand[(cand != a) & (cand != b)]
        if len(cand):
            dot = (varr[cand] - varr[a]) @ ab
            sel = cand[(dot > _EPS) & (dot < ab @ ab - _EPS)]
        else:
            sel = cand
        if len(sel):
            hit = int(sel[0])
            recover(a, hit, owner)
            recover(hit, b, owner)
            return
        stall = 0
        for _ in range(_MAX_FLIPS):
            crossed = soup.crossed_walk(vlist, a, b)
            if crossed is None:
                crossed = soup.crossed(vlist, a, b)
            if not crossed:
                if soup._ekey(a, b) in soup.edge_tris:
                    break
                # No proper crossing yet (a, b) is absent: a T-junction —
                # an endpoint sits strictly on a soup edge the crossing
                # predicates cannot see (an ear-clip sliver parks a pruned
                # collinear vertex exactly ON an edge; collinear pad rows
                # make this routine). Split the blocker at the endpoint,
                # carrying a constrained edge's owners onto both halves,
                # and resume the walk.
                tj = soup.t_junction(vlist, a, b)
                if tj is None:
                    raise MeshConflictError(
                        f"recovered edge ({a},{b}) of {_owner_label(owner)} "
                        "vanished — bug"
                    )
                (p, q), w = tj
                pk = soup._ekey(p, q)
                own = constrained_pairs.pop(pk, None)
                own2 = second_owner.pop(pk, None)
                soup.split_at_vertex(pk, w)
                if own is not None:
                    constrained_pairs[soup._ekey(p, w)] = own
                    constrained_pairs[soup._ekey(w, q)] = own
                    if own2 is not None:
                        second_owner[soup._ekey(p, w)] = own2
                        second_owner[soup._ekey(w, q)] = own2
                continue
            wall = next((e for e in crossed if e in constrained_pairs), None)
            if wall is not None:
                raise MeshConflictError(
                    f"{_owner_label(owner)} crosses a constrained edge of "
                    f"{_owner_label(constrained_pairs[wall])} — "
                    "overlapping keep-outs that never split at a vertex",
                    next="move the parts/zones so their rings cross "
                    "cleanly or not at all",
                )
            stall += 1
            # Flip-round cycling is real (a flip can reintroduce a crossed
            # edge a later flip undoes); once rounds outpace what honest
            # progress could need, ear-clip the whole strip instead.
            if stall > 4 * len(crossed) + 16:
                soup.cavity(vlist, a, b, crossed, protected=constrained_pairs)
                stall = 0
                continue
            for e in crossed:
                if soup.flip(vlist, e):
                    break
            else:
                soup.cavity(vlist, a, b, crossed, protected=constrained_pairs)
                stall = 0
        else:
            raise MeshConflictError(
                f"constraint recovery for {_owner_label(owner)} hit {_MAX_FLIPS} flips"
            )
        key = soup._ekey(a, b)
        if key not in soup.edge_tris:
            raise MeshConflictError(
                f"recovered edge ({a},{b}) of {_owner_label(owner)} vanished — bug"
            )
        prev = constrained_pairs.setdefault(key, owner)
        if prev != owner:
            # Coincident rings: one mesh edge carries two owners so a
            # wire may still pass its own part (the overlap itself is
            # already a ``conflicts`` entry via the area pass).
            second_owner[key] = owner

    for u, v, owner in constraints:
        recover(u, v, owner)

    # Drop triangles outside the outline (concave boards); footprint
    # interiors stay — a courtyard is a constrained ring, not a hole.
    inside_poly = [(float(varr[v][0]), float(varr[v][1])) for v in ovids]
    from precis.pcb.geom import point_in_polygon

    trows = np.asarray(list(soup.tris.values()))
    cents = varr[trows].mean(axis=1)
    kept = [
        tri
        for tri, c in zip(trows, cents)
        if point_in_polygon((float(c[0]), float(c[1])), inside_poly)
    ]
    tris = np.asarray(
        [t if _orient(vlist, *t) > 0 else (t[0], t[2], t[1]) for t in kept],
        dtype=np.int32,
    ).reshape(-1, 3)

    ekey: dict[tuple[int, int], list[int]] = {}
    for tid, tri in enumerate(tris):
        for k in range(3):
            u, v = int(tri[k]), int(tri[(k + 1) % 3])
            ekey.setdefault((u, v) if u < v else (v, u), []).append(tid)
    edges = np.asarray(sorted(ekey), dtype=np.int32)
    edge_id = {e: i for i, e in enumerate(sorted(ekey))}
    tri_edges = np.asarray(
        [
            [
                edge_id[
                    (
                        min(tri[k], tri[(k + 1) % 3]),
                        max(tri[k], tri[(k + 1) % 3]),
                    )
                ]
                for k in range(3)
            ]
            for tri in tris
        ],
        dtype=np.int32,
    )
    edge_tris = np.full((len(edges), 2), -1, dtype=np.int32)
    for i, e in enumerate(sorted(ekey)):
        for j, tid in enumerate(ekey[e][:2]):
            edge_tris[i, j] = tid
    constrained = np.asarray(
        [(int(e[0]), int(e[1])) in constrained_pairs for e in edges],
        dtype=bool,
    )
    edge_owner = np.full((len(edges), 2), OWNER_NONE, dtype=np.int32)
    edge_side = np.full((len(edges), 2), -1, dtype=np.int8)
    for i, ev in enumerate(edges):
        ekey_i = (int(ev[0]), int(ev[1]))
        own = constrained_pairs.get(ekey_i)
        if own is None:
            continue
        edge_owner[i, 0] = own
        edge_side[i, 0] = _side_of(own)
        second = second_owner.get(ekey_i)
        if second is not None:
            edge_owner[i, 1] = second
            edge_side[i, 1] = _side_of(second)

    # A pad vertex the centroid filter stranded (a pad outside the
    # outline — an overhanging connector) leaves its wire's dual path
    # with no triangle fan to start from (slice-2a seam item 5). Refuse
    # loudly, naming the pads, rather than shipping a mesh that
    # silently cannot route them.
    referenced = np.unique(tris.ravel()) if len(tris) else np.zeros(0, np.int32)
    ref_set = set(int(v) for v in referenced)
    orphans = [
        (d.parts[int(d.pin_part[p])], str(d.pin_label[p]))
        for p in range(len(pin_vertex))
        if int(pin_vertex[p]) not in ref_set
    ]
    if orphans:
        shown = ", ".join(f"{r}.{p}" for r, p in orphans[:8])
        raise MeshConflictError(
            f"{len(orphans)} pad(s) sit in no triangle — outside the "
            f"board outline ({shown}{'…' if len(orphans) > 8 else ''})",
            next="move the part(s) inside the outline, or widen the "
            "outline — the mesh drops every triangle outside it",
        )

    conflicts, outline_conflicts, zone_conflicts = _conflict_data(
        d, world_rings, pose, outline, world_bodies
    )

    return TopoMesh(
        verts=varr,
        tris=tris,
        edges=edges,
        tri_edges=tri_edges,
        edge_tris=edge_tris,
        constrained=constrained,
        pad_vertex=pad_vertex,
        pin_vertex=pin_vertex,
        edge_owner=edge_owner,
        edge_side=edge_side,
        conflicts=conflicts,
        outline_conflicts=outline_conflicts,
        zone_conflicts=zone_conflicts,
    )

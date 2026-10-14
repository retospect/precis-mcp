"""Openings: cut a hole in a flat sheet and stitch a tube end's rim into it.

The nanoreactor's reservoir joint (docs/backlog/hexfold-nanoreactor-tube.md,
slice H1): a flat sheet pierced by a tube whose end rim is an armchair-type
boundary -- dangling atoms in bonded pairs -- so the sheet's hole boundary
and the tube's rim can be zipped one-to-one, giving a valid all-sp2 seam
whose faces are the defect ring (hexagons where the lattices happen to
agree, pentagons/heptagons where they do not).  Unmatched boundary atoms
on either side keep their dangling valence and are left for termination
(``H-terminated where unbonded``).

Only the *rim* has to be armchair-class: a closed curve of one-valence
atoms, star-shaped about the opening centre.  The grammar's ``hex(k)``
hole fused to a ``(6(k+1), 6(k+1))`` tube is the special case this
generalises -- here the opening is cut to fit whatever rim arrives (a
``tube(n,n)`` end, or a rail tube's ribbed end), so the count match is
solved, not assumed.

``armchair_opening`` works on a flat :class:`hexfold.join.Block` (sheet in
its own xy plane) and a tube already placed with its rim dangling atoms
at the seat height past the sheet plane: atoms strictly inside the rim's
projected polygon are removed, single-bond leftovers pruned (a degree-1
spike into the mouth is frayed edge, not boundary), and the hole's rim
walk is recovered by half-edge traversal on the kept bonds -- the same
dart-orbit rule ``Patch.faces`` applies on the lattice, so the walk is a
real bonded cycle, never an angle sort.  A scale scan on the polygon
finds the cut whose bondable boundary count meets the rim's; ``_pair``
then aligns the two cyclic dangling lists (direction, phase and optional
skips by a small DP), the minted pair bonds are the seam, and each seam
face is the two rim arcs between consecutive pairs -- a walk arc on the
sheet side, a shortest bond path through the rim's boundary band on the
tube side, so an arc also crosses a rail tube's unbonded seam gap by way
of its seam atoms.

Analytic preview topology only: no grammar, no relaxation, no opinion on
whether the rings it mints are strained.  Units are Angstrom.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, replace

import numpy as np

from .build import Net
from .join import Block
from .lattice import Lattice

#: a bondable boundary atom is short of sp2 valence by one (degree 2);
#: anything below is pruned as frayed edge before it can confuse the walk
_BONDABLE_DEGREE = 2
#: the rim's dangling atoms seat one sigma past the sheet plane so a pair
#: bond lands near sigma at the seed (the fuse convention)
SEAT_SIGMA = 1.0
#: polygon-scale scan window around the rim outline
_SCALE_LO, _SCALE_HI, _SCALE_STEP = 0.55, 1.45, 0.01
#: skip cost in the pairing DP, in units of sigma^2: skipping a boundary
#: atom beats bonding it over more than ~2.5 sigma
_SKIP_SIGMA2 = 6.25


@dataclass(frozen=True)
class RimLoop:
    """A tube end's bondable boundary.

    ``atoms`` are the dangling (degree < 3) ordinals in cyclic order around
    the mouth; ``band`` is the atom set a seam-face arc may pass through
    (the dangling atoms plus their boundary neighbourhood); ``walk`` is
    the full cyclic rim walk when one exists (a grammar port), else None
    and arcs are found by path search through ``band``.
    """

    atoms: tuple[int, ...]
    band: frozenset[int]
    walk: tuple[int, ...] | None = None


@dataclass(frozen=True)
class Opening:
    """Bookkeeping for one cut-and-stitched opening.

    Sheet ordinals are into the *cut* sheet block, rim ordinals into the
    tube.  ``faces`` carry ``(side, ordinal)`` members -- ``"s"`` for the
    sheet and ``"t"`` for the tube -- so the caller offsets them when it
    assembles the composite.
    """

    #: sheet atoms removed for the mouth (in the *input* sheet's indexing)
    removed: tuple[int, ...]
    #: hole rim walk (cut-sheet ordinals), cyclic, bonded
    hole_walk: tuple[int, ...]
    #: bondable hole atoms in walk order
    hole_bondable: tuple[int, ...]
    #: fused pairs (cut-sheet ordinal, rim ordinal), in cyclic order
    fused: tuple[tuple[int, int], ...]
    #: minted seam faces, members tagged ("s"|"t", ordinal)
    faces: tuple[tuple[tuple[str, int], ...], ...]
    #: boundary atoms left dangling for termination
    unbonded_sheet: tuple[int, ...]
    unbonded_rim: tuple[int, ...]
    #: polygon scale that produced this cut
    scale: float


def _adjacency(bonds: tuple[tuple[int, int, int], ...], n: int) -> list[set[int]]:
    adj: list[set[int]] = [set() for _ in range(n)]
    for i, j, _ in bonds:
        adj[i].add(j)
        adj[j].add(i)
    return adj


def _degrees(bonds: tuple[tuple[int, int, int], ...], n: int) -> np.ndarray:
    deg = np.zeros(n, dtype=int)
    for i, j, _ in bonds:
        deg[i] += 1
        deg[j] += 1
    return deg


def _within(
    adj: list[set[int]], sources: frozenset[int] | set[int], hops: int
) -> set[int]:
    """Every atom within ``hops`` bonds of ``sources``."""
    seen = set(sources)
    frontier = set(sources)
    for _ in range(hops):
        nxt = set()
        for u in frontier:
            nxt |= adj[u]
        nxt -= seen
        seen |= nxt
        frontier = nxt
    return seen


def rim_loop_port(net: Net, name: str) -> RimLoop:
    """The dangling atoms of a grammar-built port rim, in rim-walk order.

    Only armchair rims are seats for an opening; anything else is refused
    by name so the caller reports which rim was asked for.
    """
    ports = dict(net.ports)
    if name not in ports:
        raise ValueError(
            f"rim_loop_port: net has no port {name!r}; ports: {sorted(ports)}"
        )
    port = ports[name]
    rt = port.rim_type
    if rt is None or rt[0] != "a":
        raise ValueError(
            f"opening solver seats armchair rims only; port {name!r} has "
            f"rim type {rt!r} (an (n,n) tube end is ('a', 2n))"
        )
    band = _within(_adjacency(net.bonds, len(net.atoms)), frozenset(port.dangling), 2)
    return RimLoop(
        atoms=tuple(a for a in port.atoms if a in set(port.dangling)),
        band=frozenset(band),
        walk=port.atoms,
    )


def rim_loop_end(
    coords: np.ndarray,
    bonds: tuple[tuple[int, int, int], ...],
    axis: np.ndarray,
    end: str,
) -> RimLoop:
    """The bondable boundary of an analytic tube's open end (a rail tube).

    ``end`` is ``"low"`` or ``"high"`` along ``axis``; the dangling atoms
    within one sigma of the extreme coordinate form the end boundary --
    deeper dangling atoms are free edges inside the tube (a rail tube's
    rib rims), not the mouth.  The end set is ordered cyclically about
    the axis by angle.  ``band`` is the two-bond neighbourhood of the
    dangling atoms inside the end slab, which is what lets a seam-face
    arc cross an unbonded stretch (a rail tube's seam gap) by way of its
    seam atoms.
    """
    coords = np.asarray(coords, dtype=float)
    axis = np.asarray(axis, dtype=float)
    axis /= np.linalg.norm(axis)
    n = len(coords)
    deg = _degrees(bonds, n)
    h = coords @ axis
    dangling = np.where(deg < 3)[0]
    if not len(dangling):
        raise ValueError("rim_loop_end: the tube end has no dangling atoms")
    sigma = Lattice().sigma_A
    # one lattice step inward is ~sqrt(3)/2 sigma; the window must stay
    # inside it so rib-edge atoms a site deep are excluded
    window = 0.6 * sigma
    if end == "low":
        sel = dangling[h[dangling] <= float(h[dangling].min()) + window]
    elif end == "high":
        sel = dangling[h[dangling] >= float(h[dangling].max()) - window]
    else:
        raise ValueError(f"rim_loop_end: end must be 'low' or 'high', got {end!r}")
    centre = coords[sel].mean(axis=0)
    plane_u = np.cross(axis, np.array([1.0, 0.0, 0.0]))
    if np.linalg.norm(plane_u) < 1e-9:
        plane_u = np.cross(axis, np.array([0.0, 1.0, 0.0]))
    plane_u /= np.linalg.norm(plane_u)
    plane_v = np.cross(axis, plane_u)
    rel = coords[sel] - centre
    order = np.argsort(np.arctan2(rel @ plane_v, rel @ plane_u), kind="stable")
    atoms = tuple(int(sel[i]) for i in order)
    slab = {
        a
        for a in range(n)
        if abs(float(h[a] - float(h[sel].mean()))) < 2.0 * Lattice().sigma_A
    }
    band = _within(_adjacency(bonds, n), frozenset(int(s) for s in sel), 2) & slab
    return RimLoop(atoms=atoms, band=frozenset(band))


def _dart_faces(
    bonds: tuple[tuple[int, int, int], ...], pos2d: np.ndarray
) -> list[tuple[int, ...]]:
    """Face orbits of a planar block by half-edge traversal: at each
    vertex the next dart is the one before the incoming dart in the
    direction-sorted neighbour list (the face stays on one side, as in
    ``Patch.faces``)."""
    darts: dict[int, list[tuple[float, int]]] = {}
    for i, j, _ in bonds:
        for x, y in ((i, j), (j, i)):
            dv = pos2d[y] - pos2d[x]
            darts.setdefault(x, []).append((math.atan2(dv[1], dv[0]), y))
    for v in darts:
        darts[v].sort(key=lambda t: t[0])
    visited: set[tuple[int, int]] = set()
    faces: list[tuple[int, ...]] = []
    for i, j, _ in bonds:
        for cur in ((i, j), (j, i)):
            if cur in visited:
                continue
            face: list[int] = []
            while cur not in visited:
                visited.add(cur)
                face.append(cur[0])
                a, b = cur
                idx = next(k for k, (_, vv) in enumerate(darts[b]) if vv == a)
                cur = (b, darts[b][(idx - 1) % len(darts[b])][1])
            faces.append(tuple(face))
    return faces


def _points_in_polygon(points: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Ray casting, vectorised over ``points``."""
    x, y = poly[:, 0], poly[:, 1]
    px, py = points[:, 0], points[:, 1]
    inside = np.zeros(len(points), dtype=bool)
    for i in range(len(poly)):
        j = (i + 1) % len(poly)
        cond = (y[i] > py) != (y[j] > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (x[j] - x[i]) * (py - y[i]) / (y[j] - y[i]) + x[i]
        inside ^= cond & (px < xint)
    return inside


def _cut(block: Block, polygon: np.ndarray) -> tuple[Block, tuple[int, ...]]:
    """Remove atoms strictly inside ``polygon`` (xy), then prune kept
    atoms that ended up below two bonds (frayed spikes), to convergence.
    Returns the cut block and the removed ordinals in *block* indexing."""
    pos = np.asarray(block.coords)
    inside = _points_in_polygon(pos[:, :2], polygon)
    removed = set(np.where(inside)[0].tolist())
    adj = _adjacency(block.bonds, len(pos))
    for _ in range(100):
        # prune kept atoms the cut left below two bonds -- only atoms that
        # actually lost a neighbour count (a _sheet's own outer-rim degree-1
        # corners are frayed by construction, not by this cut)
        drop = {
            a
            for a in range(len(pos))
            if a not in removed and adj[a] & removed and len(adj[a] - removed) < 2
        }
        if not drop:
            break
        removed |= drop
    else:
        raise ValueError("opening cut: frayed-edge pruning did not converge")
    keep = np.array([i for i in range(len(pos)) if i not in removed])
    remap = np.full(len(pos), -1, dtype=np.int64)
    remap[keep] = np.arange(len(keep))
    cut = replace(
        block,
        elements=tuple(block.elements[i] for i in keep),
        coords=pos[keep],
        bonds=tuple(
            (int(remap[i]), int(remap[j]), o)
            for i, j, o in block.bonds
            if i not in removed and j not in removed
        ),
        rings=tuple(
            tuple(int(remap[a]) for a in ring)
            for ring in block.rings
            if removed.isdisjoint(ring)
        ),
    )
    return cut, tuple(sorted(removed))


def _hole_walk(
    block: Block, centre: np.ndarray
) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """The rim walk of the hole around ``centre``: the smallest-area face
    orbit whose polygon encloses it (the outer rim's orbit encloses the
    centre too, so smallest wins).  Returns ``(walk, bondable)`` in
    cut-block ordinals, or None when nothing surrounds the cut."""
    deg = _degrees(block.bonds, len(block.elements))
    pos2 = np.asarray(block.coords)[:, :2]
    best: tuple[float, tuple[int, ...]] | None = None
    for face in _dart_faces(block.bonds, pos2):
        poly = pos2[list(face)]
        if not _points_in_polygon(centre[None], poly)[0]:
            continue
        area = 0.5 * abs(
            float(
                np.sum(
                    poly[:, 0] * np.roll(poly[:, 1], -1)
                    - np.roll(poly[:, 0], -1) * poly[:, 1]
                )
            )
        )
        if area < 1e-9:
            continue
        if best is None or area < best[0]:
            best = (area, face)
    if best is None:
        return None
    walk = best[1]
    bondable = tuple(a for a in walk if deg[a] <= _BONDABLE_DEGREE)
    return walk, bondable


def _arc(
    walk: tuple[int, ...] | None,
    band: frozenset[int] | set[int],
    adj: list[set[int]],
    a: int,
    b: int,
    avoid: set[int],
) -> tuple[int, ...] | None:
    """The boundary arc ``a -> b`` containing no other fused endpoint.

    On a real rim walk it is the walk segment; on an analytic rim (no
    bonded cycle across gaps) it is the shortest bond path through
    ``band``."""
    if walk is not None:
        pos = {v: i for i, v in enumerate(walk)}
        i, j = pos[a], pos[b]
        L = len(walk)
        fwd = [walk[(i + t) % L] for t in range((j - i) % L + 1)]
        if not any(x in avoid for x in fwd[1:-1]):
            return tuple(fwd)
        return tuple(walk[(i - t) % L] for t in range((i - j) % L + 1))
    if a == b:
        return (a,)
    prev: dict[int, int] = {}
    queue = deque([a])
    while queue:
        u = queue.popleft()
        for v in adj[u]:
            if v == a or v in prev or v in avoid or v not in band:
                continue
            prev[v] = u
            if v == b:
                path = [v]
                while path[-1] != a:
                    path.append(prev[path[-1]])
                return tuple(reversed(path))
            queue.append(v)
    return None


def _pair_cost(hp: np.ndarray, rp: np.ndarray) -> float:
    return float(np.sum(np.linalg.norm(hp - rp, axis=1) ** 2))


def _pair(
    hole_order: tuple[int, ...],
    rim_order: tuple[int, ...],
    hole_pos: np.ndarray,
    rim_pos: np.ndarray,
    sigma: float,
) -> tuple[tuple[tuple[int, int], ...], float]:
    """Align the two cyclic dangling lists into fused pairs.

    The shorter list maps onto a contiguous cyclic run of the longer one
    (equal counts included); direction and run start are chosen to
    minimise summed squared partner distance.  Atoms past the run on the
    longer side stay dangling for termination -- never double-bonded.
    Returns ``(pairs, cost)`` with pairs in the *hole* side's cyclic order.
    """
    nh, nr = len(hole_order), len(rim_order)
    if not nh or not nr:
        return (), 0.0
    hp = np.asarray(hole_pos)[list(hole_order)]
    rp = np.asarray(rim_pos)[list(rim_order)]
    long_is_hole = nh >= nr
    long_order = hole_order if long_is_hole else rim_order
    short_order = rim_order if long_is_hole else hole_order
    lp = hp if long_is_hole else rp
    sp = rp if long_is_hole else hp
    nl, ns = len(long_order), len(short_order)
    best: tuple[float, int, bool] | None = None
    for rev in (False, True):
        spp = sp[::-1] if rev else sp
        for phase in range(nl):
            perm = [(phase + i) % nl for i in range(ns)]
            cost = float(np.sum(np.linalg.norm(lp[perm] - spp, axis=1) ** 2))
            if best is None or cost < best[0]:
                best = (cost, phase, rev)
    assert best is not None
    cost, phase, rev = best
    so = short_order[::-1] if rev else short_order
    pairs = tuple(
        (
            (long_order[(phase + i) % nl], so[i])
            if long_is_hole
            else (so[i], long_order[(phase + i) % nl])
        )
        for i in range(ns)
    )
    if long_is_hole:
        # pairs are already in hole order (the run is contiguous)
        return pairs, cost
    # rim side is longer: reorder the pairs into the hole's cyclic order
    by_hole = {h: (h, r) for h, r in pairs}
    return tuple(by_hole[h] for h in hole_order if h in by_hole), cost


def stitch(
    sheet: Block,
    hole_walk: tuple[int, ...],
    hole_bondable: tuple[int, ...],
    tube: Block,
    rim: RimLoop,
    tube_coords: np.ndarray,
    sigma: float,
) -> tuple[
    tuple[tuple[int, int], ...],
    tuple[tuple[tuple[str, int], ...], ...],
    tuple[int, ...],
    tuple[int, ...],
]:
    """Pair bondable hole atoms to rim atoms in cyclic order and mint the
    seam faces.

    ``pairs`` are ``(cut-sheet ordinal, rim ordinal)`` -- the fused bond
    list; ``faces`` are ``(side, ordinal)`` members (``"s"`` cut sheet,
    ``"t"`` tube) so the caller offsets them at assembly.  A face whose
    arc cannot be grounded is skipped, not faked; the pair bond is minted
    regardless.
    """
    sheet_adj = _adjacency(sheet.bonds, len(sheet.elements))
    tube_adj = _adjacency(tube.bonds, len(tube.elements))
    hole_band = _within(sheet_adj, frozenset(hole_bondable), 2)
    pairs, _ = _pair(
        hole_bondable, rim.atoms, sheet.coords, np.asarray(tube_coords), sigma
    )
    fused_h = {h for h, _ in pairs}
    fused_r = {r for _, r in pairs}
    faces: list[tuple[tuple[str, int], ...]] = []
    for i, (h1, r1) in enumerate(pairs):
        h2, r2 = pairs[(i + 1) % len(pairs)]
        arc_h = _arc(hole_walk, hole_band, sheet_adj, h2, h1, fused_h - {h1, h2})
        arc_r = _arc(rim.walk, rim.band, tube_adj, r1, r2, fused_r - {r1, r2})
        if arc_h is None or arc_r is None:
            continue
        # face walk: h1 - r1 -(rim arc)- r2 - h2 -(hole arc back)- h1
        faces.append(
            (("s", h1), ("t", r1))
            + tuple(("t", a) for a in arc_r[1:])
            + (("s", h2),)
            + tuple(("s", a) for a in arc_h[1:-1])
        )
    unbonded_h = tuple(a for a in hole_bondable if a not in fused_h)
    unbonded_r = tuple(a for a in rim.atoms if a not in fused_r)
    return tuple(pairs), tuple(faces), unbonded_h, unbonded_r


def _boundary_count(
    pos: np.ndarray, adj: list[set[int]], polygon: np.ndarray
) -> tuple[set[int], list[int]]:
    """The scan's cheap pass over ``_cut``: removed atoms and the bondable
    (degree-2) boundary list, without rebuilding a block."""
    n = len(pos)
    removed = set(np.where(_points_in_polygon(pos[:, :2], polygon))[0].tolist())
    cand = {a for a in range(n) if adj[a] & removed}
    for _ in range(100):
        drop = {a for a in cand if a not in removed and len(adj[a] - removed) < 2}
        if not drop:
            break
        removed |= drop
        cand |= {nb for d in drop for nb in adj[d] if nb not in removed}
    bondable = [
        a
        for a in range(n)
        if a not in removed and adj[a] & removed and len(adj[a] - removed) == 2
    ]
    return removed, bondable


def twist_coords(
    coords: np.ndarray, deg: float, axis: np.ndarray, centre: np.ndarray
) -> np.ndarray:
    """Rigid rotation of ``coords`` by ``deg`` degrees about ``axis``
    through ``centre`` (Rodrigues)."""
    ax = np.asarray(axis, dtype=float)
    ax /= np.linalg.norm(ax)
    ca, sa = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    rot = (
        ca * np.eye(3)
        + (1 - ca) * np.outer(ax, ax)
        + sa
        * np.array([[0.0, -ax[2], ax[1]], [ax[2], 0.0, -ax[0]], [-ax[1], ax[0], 0.0]])
    )
    return (np.asarray(coords) - centre) @ rot.T + centre


def _rim_axis(rim_pos: np.ndarray) -> np.ndarray:
    """The rim dangling ring's least-variance axis (its own normal)."""
    _, _, vh = np.linalg.svd(rim_pos - rim_pos.mean(axis=0))
    return vh[2]


def scan_opening(
    sheet: Block, rim: RimLoop, tube_coords: np.ndarray
) -> tuple[float, float, np.ndarray] | None:
    """Best ``(cost, scale, polygon)`` cut for this rim at the given
    (already placed and twisted) tube coords, or None when no scale
    leaves a boundary cycle whose bondable count meets the rim's.

    ``cost`` is the summed squared partner distance of the winning cyclic
    pairing -- the caller compares ends or picks a shared twist by it.
    """
    rim_pos = np.asarray(tube_coords)[list(rim.atoms)]
    cen2 = rim_pos[:, :2].mean(axis=0)
    poly0 = rim_pos[:, :2] - cen2
    target = len(rim.atoms)
    spos = np.asarray(sheet.coords)
    adj = _adjacency(sheet.bonds, len(spos))
    best: tuple[float, float] | None = None
    s = _SCALE_LO
    while s <= _SCALE_HI + 1e-9:
        _removed, bondable = _boundary_count(spos, adj, poly0 * s + cen2)
        if len(bondable) == target:
            angs = np.arctan2(spos[bondable, 1] - cen2[1], spos[bondable, 0] - cen2[0])
            hp = spos[[bondable[i] for i in np.argsort(angs)]]
            cheapest = math.inf
            for rev in (False, True):
                rr = rim_pos[::-1] if rev else rim_pos
                for ph in range(target):
                    c = float(
                        np.sum(
                            np.linalg.norm(hp - np.roll(rr, ph, axis=0), axis=1) ** 2
                        )
                    )
                    if c < cheapest:
                        cheapest = c
            if best is None or cheapest < best[0]:
                best = (cheapest, s)
        s += _SCALE_STEP
    if best is None:
        return None
    return best[0], best[1], poly0 * best[1] + cen2


def apply_opening(
    sheet: Block,
    tube: Block,
    rim: RimLoop,
    tube_coords: np.ndarray,
    polygon: np.ndarray,
) -> tuple[Block, Opening]:
    """Cut ``sheet`` at ``polygon`` and stitch ``rim`` into it -- the
    back half of :func:`armchair_opening`, split so a driver that picked
    a shared twist itself can use the scan separately."""
    rim_pos = np.asarray(tube_coords)[list(rim.atoms)]
    cen2 = rim_pos[:, :2].mean(axis=0)
    cut, removed = _cut(sheet, polygon)
    got = _hole_walk(cut, cen2)
    if got is None:
        raise ValueError("armchair opening: winning cut lost its rim walk")
    walk, bondable = got
    pairs, faces, unbonded_h, unbonded_r = stitch(
        cut, walk, bondable, tube, rim, tube_coords, sheet.sigma
    )
    return cut, Opening(
        removed=removed,
        hole_walk=walk,
        hole_bondable=bondable,
        fused=pairs,
        faces=faces,
        unbonded_sheet=unbonded_h,
        unbonded_rim=unbonded_r,
        scale=0.0,
    )


def armchair_opening(
    sheet: Block,
    tube: Block,
    rim: RimLoop,
    tube_coords: np.ndarray,
    *,
    twists: tuple[float, ...] = tuple(range(0, 60, 2)),
) -> tuple[Block, np.ndarray, Opening]:
    """Cut the opening in ``sheet`` and stitch ``rim`` into it, scanning
    ``twists`` (degrees about the rim axis through the rim centroid)
    jointly with the polygon's scale -- the polygon is the rim's own
    outline, so rotating the tube changes which sheet atoms fall inside.
    Each candidate is scored by the best cyclic pairing cost.

    Returns ``(cut_sheet, placed_tube_coords, Opening)`` -- the tube
    coords carry the winning twist.
    """
    rim_pos = np.asarray(tube_coords)[list(rim.atoms)]
    cen3 = rim_pos.mean(axis=0)
    ax = _rim_axis(rim_pos)
    best: tuple[float, float, np.ndarray, np.ndarray] | None = None
    for tw in twists:
        tp = twist_coords(np.asarray(tube_coords), tw, ax, cen3)
        got = scan_opening(sheet, rim, tp)
        if got is None:
            continue
        cost, scale, polygon = got
        if best is None or cost < best[0]:
            best = (cost, tw, tp, polygon)
    if best is None:
        raise ValueError(
            f"armchair opening: no (twist, scale) left a boundary cycle of "
            f"{len(rim.atoms)} bondable atoms for this rim"
        )
    _cost, _tw, placed, polygon = best
    cut, opening = apply_opening(sheet, tube, rim, placed, polygon)
    return cut, placed, opening

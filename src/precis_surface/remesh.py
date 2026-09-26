"""precis_surface.remesh — degree-controlled isotropic remeshing loop
(docs/backlog/precis-surface-kernel.md "Slice 1 -- the dual route").

COLLAPSE + SPLIT + FLIP + SMOOTH + REPROJECT, in that order each
iteration. A flip changes vertex degrees by ``-1,-1,+1,+1`` and leaves
``V``, ``E``, ``F`` each unchanged -- it can only move valence around, never
remove or add a vertex, so it alone cannot relieve a degree-4 vertex in a
locked neighbourhood (no admissible re-wiring exists) or a degree-8/9
one either. Collapse and split close that gap, and are exact reverses of
each other: an edge collapse merges 2 vertices into 1 (``V-1, E-3, F-2``;
the 2 vertices opposite the collapsed edge each lose 1 degree), a vertex
split is the reverse -- it partitions one vertex's neighbour ring into 2
contiguous arcs, each keeping one new vertex connected by a fresh edge
(``V+1, E+3, F+2``; the 2 shared "pinch" vertices at the arcs' boundary
each gain 1 degree). Both leave the welded Euler characteristic
``chi = V - E + F`` invariant even though ``V``, ``E``, ``F`` individually
no longer are (only flip keeps those three literally fixed). Vertex
split, not a plain single-edge subdivision, is required here: subdividing
one edge never changes either of *its own* endpoints' degree (only the
2 apex vertices gain 1), so it can never by itself take a degree-8 or -9
vertex down -- and every admissibility filter in this module (flip's
included) already refuses to ever land any vertex on the forbidden
degree 8 as an intermediate result, so a single flip or collapse can't
walk a degree-9 vertex down through 8 either. Splitting the ring in two
is the only move that reduces a degree-``d`` vertex to 2 vertices of
degree ``arc_len + 2`` and ``d - arc_len + 2`` in one atomic step, with
neither ever forced through 8. Target degree is not a flat 6: each unit
of ``6 - degree`` concentrates ``pi/3`` of Gaussian curvature (angle
defect, :mod:`precis_surface.curvature`) at that vertex, so
``ideal_degree = clip(round(6 - defect / (pi/3)), 5, 7)``.

Works on the welded quotient (:func:`~precis_surface.periodic_mesh.weld_indices`)
for topology (degree, admissibility), but the raw
:attr:`~precis_surface.periodic_mesh.PeriodicMesh.verts`/``tris`` arrays for
geometry -- a canonical (welded) vertex's angle-defect is exactly the sum
of :func:`~precis_surface.curvature.gaussian_curvature`'s per-raw-vertex
defect over its wrap-identified raw copies: each copy sees only its own
side of the seam and so is individually flagged "boundary" (half turn,
``pi``), and two half turns sum to exactly the interior vertex's full
``2*pi`` turn once combined -- no separate boundary-aware curvature
routine is needed.

**Boundary vertices are frozen.** A wrap-identified raw vertex (a
low-face/high-face pair) must stay exactly on its cell face for ``wrap``
to remain valid; rather than solving that constrained-motion problem,
smoothing and reprojection here simply never move a boundary raw vertex
(the module docstring's stated "clamp" choice) -- only the interior
(singleton wrap-class) vertices are relaxed. A flip, collapse or split
that touches a wrap-crossing edge (whose two incident triangles reference
*different* raw copies of a shared endpoint) or any wrap-identified raw
vertex is likewise never applied -- collapse/split would otherwise need to
introduce a new wrap pairing, which this module never does; a vertex
split only ever runs from an interior (singleton wrap class) vertex with
both its chosen pinch vertices also interior, so the new vertex it mints
is always interior too.

Collapse's correctness guard is the **link condition**: collapsing edge
``(u, v)`` is only legal when the intersection of ``u``'s and ``v``'s
one-ring neighbour sets is exactly the two vertices opposite that edge
(the triangles' own third vertices) -- any other shared neighbour means
collapsing would either merge two already-distinct edges into one
(fine, that is the removed edge's own apexes) or silently create a
duplicate edge / non-manifold vertex (not fine). It is checked before
every collapse candidate is accepted, never assumed.

Pure functions, numpy only, no scipy (:mod:`precis_surface` house rules).
Determinism: every loop over a set of topology decisions iterates a
sorted list of keys, never a bare ``set()``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from precis_surface.curvature import gaussian_curvature, vertex_normals
from precis_surface.periodic_mesh import (
    GradientFn,
    LevelSetFn,
    PeriodicMesh,
    is_edge_manifold_closed,
    weld_indices,
    welded_euler,
)

#: Minimum interior angle (radians) below which a triangle is counted as
#: near-degenerate -- never an ``area == 0.0`` test (real slivers land
#: around ``1e-23``, never bit-exactly zero).
_DEGENERATE_ANGLE = np.radians(5.0)

#: Damping on the tangential Laplacian step (module docstring's SMOOTH
#: stage) -- a fixed constant, not exposed, to keep one iteration from
#: overshooting before REPROJECT pulls the vertex back onto the surface.
_SMOOTH_STEP = 0.5

#: Newton iterations for the REPROJECT stage, same scheme as
#: :func:`precis_surface.dual._project_to_zero_set`.
_REPROJECT_ITERS = 2

#: Soft (never hard-rejecting) preference against a flip whose new edge
#: joins two vertices that would both land at degree 7.
_SOFT_ADJACENT_7_PENALTY = 0.5


@dataclass(frozen=True)
class RemeshReport:
    """One :func:`remesh` run's bookkeeping.

    ``flips_applied``/``flips_rejected``, ``collapse_applied``/
    ``collapse_rejected`` and ``split_applied``/``split_rejected`` are all
    per-iteration counts, same length as ``iterations``. ``flips_rejected``
    counts every canonical edge considered in that iteration's flip pass
    that was *not* applied, whether from the hard admissibility filter or
    a non-positive cost change. ``collapse_rejected``/``split_rejected``
    are the residual count of degree<=4 / degree>=8 canonical edges still
    present when that iteration's collapse/split pass gave up (no more
    admissible, improving candidate found) -- not a per-candidate tally,
    since both passes apply greedily and re-derive their candidate pool
    from scratch after every application. ``degree_histogram`` and
    ``near_degenerate_triangle_count`` are measured on the final mesh.
    ``converged`` is true iff the last iteration applied zero flips,
    collapses and splits.
    """

    iterations: int
    collapse_applied: tuple[int, ...]
    collapse_rejected: tuple[int, ...]
    split_applied: tuple[int, ...]
    split_rejected: tuple[int, ...]
    flips_applied: tuple[int, ...]
    flips_rejected: tuple[int, ...]
    degree_histogram: dict[int, int]
    near_degenerate_triangle_count: int
    chi_before: int
    chi_after: int
    converged: bool


def _edge_keys(row: NDArray[np.int64]) -> list[tuple[int, int]]:
    """The 3 undirected (sorted) canonical edge keys of one triangle."""
    a, b, c = int(row[0]), int(row[1]), int(row[2])
    return [(x, y) if x < y else (y, x) for x, y in ((a, b), (b, c), (c, a))]


def _find_rot(row: NDArray[np.int64], a: int, b: int) -> int | None:
    """Rotation ``k`` such that ``row[k], row[k+1] == a, b`` (mod 3), or
    ``None`` if no rotation of ``row`` has ``a`` immediately followed by
    ``b``."""
    for k in range(3):
        if int(row[k]) == a and int(row[(k + 1) % 3]) == b:
            return k
    return None


def _orient(
    occ: list[int], uc: int, vc: int, canon: NDArray[np.int64], work: NDArray[np.int64]
) -> tuple[int, int, int, int] | None:
    """For the 2 triangles sharing canonical edge ``(uc, vc)``, find which
    one traverses it ``uc -> vc`` and which ``vc -> uc`` (a consistently
    oriented closed 2-manifold always has exactly one of each). Returns
    ``(t1, k1, t2, k2)`` with ``t1``'s rotation ``k1`` giving the
    ``uc -> vc`` traversal, or ``None`` if neither assignment works (a
    data inconsistency -- treated as a rejected candidate, never raised)."""
    for ta, tb in (occ, (occ[1], occ[0])):
        ka = _find_rot(canon[work[ta]], uc, vc)
        kb = _find_rot(canon[work[tb]], vc, uc)
        if ka is not None and kb is not None:
            return ta, ka, tb, kb
    return None


def _edge_discard(
    edge_tris: dict[tuple[int, int], list[int]], key: tuple[int, int], t: int
) -> None:
    lst = edge_tris.get(key)
    if lst is None:
        return
    if t in lst:
        lst.remove(t)
    if not lst:
        del edge_tris[key]


def _build_edge_tris(
    canon: NDArray[np.int64], tris: NDArray[np.int64]
) -> dict[tuple[int, int], list[int]]:
    """Canonical undirected edge -> owning triangle indices (exactly 2
    each, on a closed 2-manifold)."""
    edge_tris: dict[tuple[int, int], list[int]] = {}
    ctris = canon[tris]
    for t in range(len(tris)):
        for key in _edge_keys(ctris[t]):
            edge_tris.setdefault(key, []).append(t)
    return edge_tris


def _degrees_from_edges(
    edge_tris: dict[tuple[int, int], list[int]], n_canon: int
) -> tuple[list[set[int]], NDArray[np.int64]]:
    adj: list[set[int]] = [set() for _ in range(n_canon)]
    for a, b in edge_tris:
        adj[a].add(b)
        adj[b].add(a)
    degree = np.array([len(s) for s in adj], dtype=np.int64)
    return adj, degree


def _ideal_degrees(
    verts: NDArray[np.float64],
    tris: NDArray[np.int64],
    canon: NDArray[np.int64],
    n_canon: int,
) -> NDArray[np.int64]:
    """Curvature-derived target degree per canonical vertex (module
    docstring): a canonical vertex's angle defect is the sum, over its
    wrap-identified raw copies, of :func:`gaussian_curvature`'s per-raw
    defect."""
    defect_raw = gaussian_curvature(verts, tris)
    defect = np.zeros(n_canon, dtype=np.float64)
    np.add.at(defect, canon, defect_raw)
    ideal = np.round(6.0 - defect / (np.pi / 3.0))
    return np.clip(ideal, 5, 7).astype(np.int64)


_FlipCandidate = tuple[
    int, int, int, int, int, int, int, int, int, int, int, int, int, int, float
]


def _eval_candidate(
    uc: int,
    vc: int,
    occ: list[int],
    canon: NDArray[np.int64],
    work: NDArray[np.int64],
    edge_tris: dict[tuple[int, int], list[int]],
    degree: NDArray[np.int64],
    ideal: NDArray[np.int64],
) -> _FlipCandidate | None:
    """The hard admissibility filter plus cost-improvement score for
    flipping canonical edge ``(uc, vc)`` against the *current* (live)
    ``work``/``edge_tris``/``degree``, or ``None`` if inadmissible or not
    improving. Returns
    ``(t1, t2, u0, v0, c0, d0, uc, vc, cc, dc, nu, nv, ncc, nd, improvement)``."""
    oriented = _orient(occ, uc, vc, canon, work)
    if oriented is None:
        return None
    t1, k1, t2, k2 = oriented
    row1, row2 = work[t1], work[t2]
    u0, v0, c0 = int(row1[k1]), int(row1[(k1 + 1) % 3]), int(row1[(k1 + 2) % 3])
    v0b, u0b, d0 = int(row2[k2]), int(row2[(k2 + 1) % 3]), int(row2[(k2 + 2) % 3])
    if u0 != u0b or v0 != v0b:
        return None  # wrap-crossing edge -- out of scope (module docstring)
    cc, dc = int(canon[c0]), int(canon[d0])
    if cc == dc or cc in (uc, vc) or dc in (uc, vc):
        return None
    new_key = (cc, dc) if cc < dc else (dc, cc)
    if new_key in edge_tris:
        return None  # would create a duplicate edge
    nu, nv = int(degree[uc]) - 1, int(degree[vc]) - 1
    ncc, nd = int(degree[cc]) + 1, int(degree[dc]) + 1
    if min(nu, nv, ncc, nd) <= 4 or max(nu, nv, ncc, nd) >= 8:
        return None
    before = (
        abs(int(degree[uc]) - int(ideal[uc]))
        + abs(int(degree[vc]) - int(ideal[vc]))
        + abs(int(degree[cc]) - int(ideal[cc]))
        + abs(int(degree[dc]) - int(ideal[dc]))
    )
    after = (
        abs(nu - int(ideal[uc]))
        + abs(nv - int(ideal[vc]))
        + abs(ncc - int(ideal[cc]))
        + abs(nd - int(ideal[dc]))
    )
    improvement = float(before - after)
    if ncc == 7 and nd == 7:
        improvement -= _SOFT_ADJACENT_7_PENALTY
    if improvement <= 0:
        return None
    return (t1, t2, u0, v0, c0, d0, uc, vc, cc, dc, nu, nv, ncc, nd, improvement)


def _apply_candidate(
    cand: _FlipCandidate,
    canon: NDArray[np.int64],
    work: NDArray[np.int64],
    edge_tris: dict[tuple[int, int], list[int]],
    adj: list[set[int]],
    degree: NDArray[np.int64],
) -> None:
    t1, t2, u0, v0, c0, d0, uc, vc, cc, dc, nu, nv, ncc, nd, _improve = cand
    old_keys1, old_keys2 = _edge_keys(canon[work[t1]]), _edge_keys(canon[work[t2]])
    for key in old_keys1:
        _edge_discard(edge_tris, key, t1)
    for key in old_keys2:
        _edge_discard(edge_tris, key, t2)
    work[t1] = (u0, d0, c0)
    work[t2] = (d0, v0, c0)
    for key in _edge_keys(canon[work[t1]]):
        edge_tris.setdefault(key, []).append(t1)
    for key in _edge_keys(canon[work[t2]]):
        edge_tris.setdefault(key, []).append(t2)
    adj[uc].discard(vc)
    adj[vc].discard(uc)
    adj[cc].add(dc)
    adj[dc].add(cc)
    degree[uc], degree[vc], degree[cc], degree[dc] = nu, nv, ncc, nd


#: ``(edge_key, keep_raw, remove_raw, improvement)`` for one admissible,
#: improving edge-collapse candidate.
_CollapseCandidate = tuple[tuple[int, int], int, int, float]


def _eval_collapse_candidate(
    uc: int,
    vc: int,
    occ: list[int],
    canon: NDArray[np.int64],
    work: NDArray[np.int64],
    adj: list[set[int]],
    degree: NDArray[np.int64],
    ideal: NDArray[np.int64],
    boundary_raw: NDArray[np.bool_],
) -> _CollapseCandidate | None:
    """The hard admissibility filter (including the link condition -- see
    module docstring) plus cost-improvement score for collapsing canonical
    edge ``(uc, vc)``, one endpoint of which must currently be at degree
    <=4 (the caller only invokes this for such edges). Returns ``None`` if
    inadmissible or not improving."""
    oriented = _orient(occ, uc, vc, canon, work)
    if oriented is None:
        return None
    t1, k1, t2, k2 = oriented
    row1, row2 = work[t1], work[t2]
    u0, v0, c0 = int(row1[k1]), int(row1[(k1 + 1) % 3]), int(row1[(k1 + 2) % 3])
    v0b, u0b, d0 = int(row2[k2]), int(row2[(k2 + 1) % 3]), int(row2[(k2 + 2) % 3])
    if u0 != u0b or v0 != v0b:
        return None  # wrap-crossing edge -- refuse (module docstring)
    if boundary_raw[u0] or boundary_raw[v0] or boundary_raw[c0] or boundary_raw[d0]:
        return None  # seam-adjacent -- refuse (module docstring)
    cc, dc = int(canon[c0]), int(canon[d0])
    if cc == dc or cc in (uc, vc) or dc in (uc, vc):
        return None
    # Link condition (module docstring): collapsing (uc, vc) is only
    # manifold-safe if their one-ring neighbourhoods share *exactly* the
    # edge's own two apex vertices -- any other common neighbour would
    # either duplicate an edge or pinch the surface non-manifold.
    if (adj[uc] & adj[vc]) != {cc, dc}:
        return None
    du, dv = int(degree[uc]), int(degree[vc])
    if du > 4 and dv > 4:
        return None  # neither endpoint is this pass's degree<=4 target
    if du <= 4 and dv <= 4:
        keep = uc if uc < vc else vc  # deterministic tie-break
    elif du <= 4:
        keep = vc
    else:
        keep = uc
    keep_deg = du + dv - 4
    n_cc, n_dc = int(degree[cc]) - 1, int(degree[dc]) - 1
    if (
        keep_deg <= 4
        or keep_deg >= 8
        or n_cc <= 4
        or n_cc >= 8
        or n_dc <= 4
        or n_dc >= 8
    ):
        return None
    before = (
        abs(du - int(ideal[uc]))
        + abs(dv - int(ideal[vc]))
        + abs(int(degree[cc]) - int(ideal[cc]))
        + abs(int(degree[dc]) - int(ideal[dc]))
    )
    after = (
        abs(keep_deg - int(ideal[keep]))
        + abs(n_cc - int(ideal[cc]))
        + abs(n_dc - int(ideal[dc]))
    )
    improvement = float(before - after)
    if improvement <= 0:
        return None
    keep_raw = u0 if keep == uc else v0
    remove_raw = v0 if keep == uc else u0
    edge_key = (uc, vc) if uc < vc else (vc, uc)
    return (edge_key, keep_raw, remove_raw, improvement)


def _apply_collapse_raw(
    work: NDArray[np.int64], keep_raw: int, remove_raw: int
) -> NDArray[np.int64]:
    """Relabel every occurrence of ``remove_raw`` to ``keep_raw`` and drop
    the (exactly 2, on a closed 2-manifold) triangle rows this collapses
    into degenerate (repeated-vertex) rows -- the two triangles that used
    to share the collapsed edge."""
    relabeled = np.where(work == remove_raw, keep_raw, work)
    non_degenerate = (
        (relabeled[:, 0] != relabeled[:, 1])
        & (relabeled[:, 1] != relabeled[:, 2])
        & (relabeled[:, 2] != relabeled[:, 0])
    )
    return relabeled[non_degenerate]


def _collapse_pass(
    verts: NDArray[np.float64],
    tris: NDArray[np.int64],
    canon: NDArray[np.int64],
    n_canon: int,
    boundary_raw: NDArray[np.bool_],
) -> tuple[int, int, NDArray[np.int64]]:
    """Greedily collapse degree<=4 canonical vertices into a neighbour,
    highest-improvement-first, re-deriving the full candidate pool from
    the live (already-collapsed-this-pass) mesh before every application
    -- simpler and more robust than incremental bookkeeping, and cheap at
    this mesh size. Returns ``(applied, residual, new_tris)`` where
    ``residual`` is the count of degree<=4 canonical edges still present
    when no further admissible, improving candidate could be found."""
    ideal = _ideal_degrees(verts, tris, canon, n_canon)
    work = tris.copy()
    applied = 0
    residual = 0
    safety_cap = 4 * n_canon + 16
    for _round in range(safety_cap):
        edge_tris = _build_edge_tris(canon, work)
        adj, degree = _degrees_from_edges(edge_tris, n_canon)
        candidates: list[_CollapseCandidate] = []
        residual = 0
        for uc, vc in sorted(edge_tris.keys()):
            if min(int(degree[uc]), int(degree[vc])) > 4:
                continue
            residual += 1
            cand = _eval_collapse_candidate(
                uc,
                vc,
                edge_tris[(uc, vc)],
                canon,
                work,
                adj,
                degree,
                ideal,
                boundary_raw,
            )
            if cand is not None:
                candidates.append(cand)
        if not candidates:
            break
        candidates.sort(key=lambda c: (-c[-1], c[0]))
        _edge_key, keep_raw, remove_raw, _improve = candidates[0]
        work = _apply_collapse_raw(work, keep_raw, remove_raw)
        applied += 1
    return applied, residual, work


#: ``(neighbour_raw_id, triangle_row_index)`` per ring step, in cyclic
#: (CCW) order, one entry per triangle incident to the ring's owning
#: vertex.
_Ring = list[tuple[int, int]]


def _vertex_ring(w0: int, incident: list[int], work: NDArray[np.int64]) -> _Ring | None:
    """The cyclic (CCW) neighbour ring of raw vertex ``w0``, one entry per
    triangle in ``incident`` (every row of ``work`` containing ``w0``):
    for a triangle rotation ``(w0, a, b)``, the fan visits ``a`` then
    ``b``, so this records ``next[a] = (b, triangle)`` and walks it back
    to a single closed loop -- ``None`` if ``incident`` doesn't form one
    (never expected for an interior vertex on a closed 2-manifold, but a
    candidate is rejected rather than raising if it somehow doesn't)."""
    next_of: dict[int, tuple[int, int]] = {}
    for t in incident:
        row = work[t]
        k = int(np.nonzero(row == w0)[0][0])
        a, b = int(row[(k + 1) % 3]), int(row[(k + 2) % 3])
        if a in next_of:
            return None  # non-manifold fan -- reject rather than guess
        next_of[a] = (b, t)
    if not next_of:
        return None
    start = min(next_of)
    ring: _Ring = []
    cur = start
    for _ in range(len(next_of)):
        if cur not in next_of:
            return None
        nxt, t = next_of[cur]
        ring.append((cur, t))
        cur = nxt
    return ring if cur == start else None


#: ``(start_pos, arc_len)`` -- the two new vertices' ring partition (see
#: :func:`_eval_vertex_split`).
_SplitChoice = tuple[int, int]


def _eval_vertex_split(
    wc: int,
    ring: _Ring,
    canon: NDArray[np.int64],
    degree: NDArray[np.int64],
    ideal: NDArray[np.int64],
    boundary_raw: NDArray[np.bool_],
) -> tuple[_SplitChoice, float] | None:
    """Best admissible way to split canonical vertex ``wc`` (currently at
    degree ``d = len(ring)``, required >=8 by the caller): partition its
    ring into 2 contiguous arcs sharing 2 "pinch" endpoints (module
    docstring's ``(u, v)`` reverse-of-collapse), ``start_pos`` the ring
    index of one pinch and ``arc_len`` the number of *triangles* (not
    vertices) the first new vertex keeps -- resulting degrees
    ``arc_len + 2`` and ``d - arc_len + 2`` must both land in ``[5, 7]``
    (this is the only route to reducing a degree-8/9 vertex without ever
    passing through the forbidden degree 8 as an intermediate flip/collapse
    result -- module docstring), and both pinch vertices must be
    seam-free with degree <=6 pre-split (so <=7 after gaining 1). Returns
    ``(best_choice, score)`` or ``None`` if no partition is admissible."""
    d = len(ring)
    best: tuple[_SplitChoice, float] | None = None
    for arc_len in range(3, d - 2):
        d1, d2 = arc_len + 2, d - arc_len + 2
        if not (5 <= d1 <= 7 and 5 <= d2 <= 7):
            continue
        for start_pos in range(d):
            c_raw = ring[start_pos][0]
            e_raw = ring[(start_pos + arc_len) % d][0]
            if boundary_raw[c_raw] or boundary_raw[e_raw]:
                continue  # seam-adjacent -- refuse (module docstring)
            cc, ec = int(canon[c_raw]), int(canon[e_raw])
            if cc in (ec, wc) or ec == wc:
                continue
            nc, ne = int(degree[cc]) + 1, int(degree[ec]) + 1
            if nc >= 8 or ne >= 8:
                continue  # would push a pinch past the hard degree ceiling
            before = (
                abs(int(degree[wc]) - int(ideal[wc]))
                + abs(int(degree[cc]) - int(ideal[cc]))
                + abs(int(degree[ec]) - int(ideal[ec]))
            )
            # The 2 new vertices' own curvature-derived ideal isn't known
            # until next pass's `_ideal_degrees` recompute (module
            # docstring); `6` is this same-pass-only placeholder target.
            after = (
                abs(d1 - 6)
                + abs(d2 - 6)
                + abs(nc - int(ideal[cc]))
                + abs(ne - int(ideal[ec]))
            )
            score = float(before - after)
            if best is None or score > best[1]:
                best = ((start_pos, arc_len), score)
    return best


def _apply_vertex_split(
    verts: NDArray[np.float64],
    work: NDArray[np.int64],
    w0: int,
    ring: _Ring,
    choice: _SplitChoice,
) -> tuple[NDArray[np.float64], NDArray[np.int64], int]:
    """Split raw vertex ``w0`` per ``choice`` (see
    :func:`_eval_vertex_split`): ``w0`` is reused for the surviving
    "arc-1" vertex (its arc's triangles already reference it, untouched),
    a new raw vertex ``w2`` is appended for "arc-2" (its triangles get
    ``w0`` relabelled to ``w2``), and 2 new bridging triangles connect
    them at the 2 shared pinch vertices -- ``(V, E, F)`` bookkeeping
    ``+1, +3, +2``, the exact reverse of :func:`_apply_collapse_raw`. Both
    new vertices are repositioned to their own arc's neighbour centroid
    (not left literally coincident) so the 2 new bridging triangles are
    never momentarily zero-area."""
    d = len(ring)
    start_pos, arc_len = choice
    w2 = len(verts)
    arc1_vertex_pos = [(start_pos + k) % d for k in range(arc_len + 1)]
    arc2_vertex_pos = [(start_pos + arc_len + k) % d for k in range(d - arc_len + 1)]
    arc2_tri_pos = [(start_pos + arc_len + k) % d for k in range(d - arc_len)]
    pinch_c = ring[start_pos][0]
    pinch_d = ring[(start_pos + arc_len) % d][0]

    work = work.copy()
    for p in arc2_tri_pos:
        t = ring[p][1]
        row = work[t]
        row[row == w0] = w2
    bridge = np.array([[w0, w2, pinch_d], [w2, w0, pinch_c]], dtype=np.int64)
    work = np.concatenate([work, bridge], axis=0)

    arc1_members = np.array([ring[p][0] for p in arc1_vertex_pos], dtype=np.int64)
    arc2_members = np.array([ring[p][0] for p in arc2_vertex_pos], dtype=np.int64)
    verts = verts.copy()
    w1_pos = verts[arc1_members].mean(axis=0)
    w2_pos = verts[arc2_members].mean(axis=0)
    verts[w0] = w1_pos
    verts = np.concatenate([verts, w2_pos[None, :]], axis=0)
    return verts, work, w2


def _split_pass(
    verts: NDArray[np.float64],
    tris: NDArray[np.int64],
    canon: NDArray[np.int64],
    n_canon: int,
    boundary_raw: NDArray[np.bool_],
) -> tuple[
    NDArray[np.float64],
    NDArray[np.int64],
    NDArray[np.int64],
    NDArray[np.bool_],
    int,
    int,
    int,
]:
    """Greedily vertex-split degree>=8 canonical vertices, highest-score
    first, re-deriving the full candidate pool from the live
    (already-split-this-pass) mesh before every application, same style
    as :func:`_collapse_pass`. New vertices are appended to
    ``verts``/``canon``/``boundary_raw`` (always interior, singleton wrap
    class -- module docstring) and ``n_canon`` grows to match. Returns
    ``(verts, new_tris, canon, boundary_raw, n_canon, applied, residual)``
    where ``residual`` is the count of degree>=8 canonical vertices still
    present when no further admissible candidate could be found."""
    ideal = _ideal_degrees(verts, tris, canon, n_canon)
    work = tris.copy()
    applied = 0
    residual = 0
    edge_tris0 = _build_edge_tris(canon, work)
    _adj0, degree0 = _degrees_from_edges(edge_tris0, n_canon)
    safety_cap = int(np.sum(degree0 >= 8)) + 4
    for _round in range(safety_cap):
        edge_tris = _build_edge_tris(canon, work)
        _adj, degree = _degrees_from_edges(edge_tris, n_canon)
        best_overall: tuple[float, int, int, _Ring, _SplitChoice] | None = None
        residual = 0
        for wc in sorted(int(v) for v in np.nonzero(degree >= 8)[0]):
            residual += 1
            raws = np.nonzero(canon == wc)[0]
            if len(raws) != 1:
                continue  # seam-identified -- can never happen for wc>=8
            w0 = int(raws[0])
            if boundary_raw[w0]:
                continue
            incident = np.nonzero(np.any(work == w0, axis=1))[0].tolist()
            ring = _vertex_ring(w0, incident, work)
            if ring is None or len(ring) != int(degree[wc]):
                continue
            found = _eval_vertex_split(wc, ring, canon, degree, ideal, boundary_raw)
            if found is None:
                continue
            choice, score = found
            if best_overall is None or (-score, wc) < (
                -best_overall[0],
                best_overall[1],
            ):
                best_overall = (score, wc, w0, ring, choice)
        if best_overall is None:
            break
        _score, _wc, w0, ring, choice = best_overall
        verts, work, _w2 = _apply_vertex_split(verts, work, w0, ring, choice)
        canon = np.concatenate([canon, np.array([n_canon], dtype=np.int64)])
        boundary_raw = np.concatenate([boundary_raw, np.array([False])])
        ideal = np.concatenate([ideal, np.array([6], dtype=np.int64)])
        n_canon += 1
        applied += 1
    return verts, work, canon, boundary_raw, n_canon, applied, residual


def _flip_pass(
    verts: NDArray[np.float64],
    tris: NDArray[np.int64],
    canon: NDArray[np.int64],
    n_canon: int,
) -> tuple[int, int, NDArray[np.int64]]:
    """One pass of admissible, degree-improving edge flips: every
    canonical edge present at pass start is ranked by its improvement in
    total ``|degree - ideal|`` (ties broken by the edge's own sorted
    key, for determinism), then applied highest-improvement-first,
    re-validating each against the *live* (already-mutated-by-this-pass)
    state before applying. Returns ``(applied, rejected, new_tris)``."""
    ideal = _ideal_degrees(verts, tris, canon, n_canon)
    edge_tris = _build_edge_tris(canon, tris)
    adj, degree = _degrees_from_edges(edge_tris, n_canon)
    work = tris.copy()
    total_considered = len(edge_tris)

    ranked: list[tuple[float, tuple[int, int]]] = []
    for uc, vc in sorted(edge_tris.keys()):
        cand = _eval_candidate(
            uc, vc, edge_tris[(uc, vc)], canon, work, edge_tris, degree, ideal
        )
        if cand is not None:
            ranked.append((-cand[-1], (uc, vc)))
    ranked.sort()

    applied = 0
    for _neg_improve, (uc, vc) in ranked:
        occ = edge_tris.get((uc, vc))
        if occ is None or len(occ) != 2:
            continue
        cand = _eval_candidate(uc, vc, occ, canon, work, edge_tris, degree, ideal)
        if cand is None:
            continue
        _apply_candidate(cand, canon, work, edge_tris, adj, degree)
        applied += 1

    return applied, total_considered - applied, work


def _smooth(
    verts: NDArray[np.float64], tris: NDArray[np.int64], boundary_raw: NDArray[np.bool_]
) -> NDArray[np.float64]:
    """Tangential Laplacian: interior (non-boundary) raw vertices move
    toward the uniform-weight centroid of their raw-mesh neighbours,
    surface-normal component removed. Boundary raw vertices are frozen
    (module docstring)."""
    normals = vertex_normals(verts, tris)
    n = len(verts)
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    src = np.concatenate([e[:, 0], e[:, 1]])
    dst = np.concatenate([e[:, 1], e[:, 0]])
    sums = np.zeros_like(verts)
    counts = np.zeros(n, dtype=np.float64)
    np.add.at(sums, src, verts[dst])
    np.add.at(counts, src, 1.0)
    counts = np.where(counts > 0, counts, 1.0)
    disp = sums / counts[:, None] - verts
    disp -= np.einsum("ij,ij->i", disp, normals)[:, None] * normals
    out = verts.copy()
    interior = ~boundary_raw
    out[interior] = verts[interior] + _SMOOTH_STEP * disp[interior]
    return out


def _reproject(
    verts: NDArray[np.float64],
    boundary_raw: NDArray[np.bool_],
    f: LevelSetFn,
    grad: GradientFn,
) -> NDArray[np.float64]:
    """A couple of Newton steps back onto ``f``'s zero set along
    ``grad``, interior raw vertices only (same scheme as
    :func:`precis_surface.dual._project_to_zero_set`)."""
    out = verts.copy()
    interior = ~boundary_raw
    pts = out[interior]
    for _ in range(_REPROJECT_ITERS):
        val = np.asarray(f(pts), dtype=np.float64)
        g = np.asarray(grad(pts), dtype=np.float64)
        g2 = np.einsum("ij,ij->i", g, g)
        g2 = np.where(g2 < 1e-24, 1.0, g2)
        pts = pts - (val / g2)[:, None] * g
    out[interior] = pts
    return out


def _min_angles(
    verts: NDArray[np.float64], tris: NDArray[np.int64]
) -> NDArray[np.float64]:
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]

    def _angle(
        pa: NDArray[np.float64], pb: NDArray[np.float64], pc: NDArray[np.float64]
    ) -> NDArray[np.float64]:
        u, w = pb - pa, pc - pa
        return np.arctan2(
            np.linalg.norm(np.cross(u, w), axis=1), np.einsum("ij,ij->i", u, w)
        )

    a0, a1, a2 = _angle(v0, v1, v2), _angle(v1, v2, v0), _angle(v2, v0, v1)
    return np.minimum(np.minimum(a0, a1), a2)


def _degree_histogram(
    canon: NDArray[np.int64], tris: NDArray[np.int64], n_canon: int
) -> dict[int, int]:
    """Degree census over canonical vertices actually referenced by
    ``tris``. A collapsed-away canonical id's ``verts``/``canon`` slot is
    never physically removed (module docstring: cheaper to leave it
    orphaned than to renumber), so it must be excluded here by its
    (unambiguous) zero degree -- same convention as
    :func:`precis_surface.dual.dualise`'s ring walk."""
    ctris = canon[tris]
    adj: list[set[int]] = [set() for _ in range(n_canon)]
    for a, b, c in ctris.tolist():
        adj[a].update((b, c))
        adj[b].update((a, c))
        adj[c].update((a, b))
    hist: dict[int, int] = {}
    for s in adj:
        d = len(s)
        if d == 0:
            continue
        hist[d] = hist.get(d, 0) + 1
    return hist


def remesh(
    pm: PeriodicMesh, *, f: LevelSetFn, grad: GradientFn, iters: int = 20
) -> tuple[PeriodicMesh, RemeshReport]:
    """COLLAPSE + SPLIT + FLIP + SMOOTH + REPROJECT toward curvature-derived
    target degree (module docstring). ``V``, ``E`` and ``F`` now do change
    (collapse/split), but the welded Euler characteristic and ``pm.wrap``
    never do -- both operators refuse any wrap-crossing edge or
    wrap-identified raw vertex, same as flip. ``f``/``grad`` must be the
    same level-set pair ``pm`` was marched from.
    """
    verts = pm.verts.copy()
    tris = pm.tris.copy()
    raw_canon = weld_indices(len(verts), pm.wrap)
    class_size = np.bincount(raw_canon, minlength=len(verts))
    # weld_indices' ids are union-find *representative raw indices*, not a
    # dense 0..n_canon-1 range (most raw indices never occur as anyone's
    # representative) -- compact them, exactly as dual.py does, so every
    # array indexed by a canonical id is used in full, with no phantom
    # (degree-0) unused slots.
    _uniq, canon = np.unique(raw_canon, return_inverse=True)
    canon = canon.astype(np.int64)
    n_canon = len(_uniq)
    boundary_raw = class_size[raw_canon] > 1
    # `canon`/`n_canon`/`boundary_raw` grow across the loop below (a split
    # mints a new raw vertex, always interior/singleton-wrap-class by
    # construction -- module docstring); `verts` grows in lockstep. `tris`
    # both grows (split) and shrinks (collapse) row-count independently.
    # A class of >2 raw members (a cell-edge/corner point, identified
    # across 2+ wrap axes at once -- observed for the gyroid, never for
    # Schwarz P, at the resolutions this slice tests) is still frozen and
    # topologically tracked correctly here, but `_ideal_degrees`'s
    # per-raw-defect sum (module docstring) assumes exactly 2 halves
    # making one full turn; for such a vertex it over-counts by
    # `(members - 2) * pi`, degrading its *target* degree to whatever
    # `clip(..., 5, 7)` lands on -- bounded and harmless, never a
    # correctness break, just a known imprecision for those few vertices.

    def _view(v: NDArray[np.float64], t: NDArray[np.int64]) -> PeriodicMesh:
        return PeriodicMesh(verts=v, tris=t, wrap=pm.wrap, cell=pm.cell)

    _v0, _e0, _f0, chi_before = welded_euler(_view(verts, tris))

    collapse_applied: list[int] = []
    collapse_rejected: list[int] = []
    split_applied: list[int] = []
    split_rejected: list[int] = []
    flips_applied: list[int] = []
    flips_rejected: list[int] = []
    for _it in range(iters):
        c_applied, c_rejected, tris = _collapse_pass(
            verts, tris, canon, n_canon, boundary_raw
        )
        collapse_applied.append(c_applied)
        collapse_rejected.append(c_rejected)

        verts, tris, canon, boundary_raw, n_canon, s_applied, s_rejected = _split_pass(
            verts, tris, canon, n_canon, boundary_raw
        )
        split_applied.append(s_applied)
        split_rejected.append(s_rejected)

        applied, rejected, tris = _flip_pass(verts, tris, canon, n_canon)
        flips_applied.append(applied)
        flips_rejected.append(rejected)
        verts = _smooth(verts, tris, boundary_raw)
        verts = _reproject(verts, boundary_raw, f, grad)

        chi_it = welded_euler(_view(verts, tris))[3]
        if chi_it != chi_before:
            raise RuntimeError(
                f"remesh: welded chi changed from {chi_before} to {chi_it} -- "
                "a collapse/split/flip broke topology invariance"
            )
        if not is_edge_manifold_closed(_view(verts, tris)):
            raise RuntimeError(
                "remesh: welded quotient is no longer edge-manifold-closed"
            )

    chi_after = welded_euler(_view(verts, tris))[3]
    report = RemeshReport(
        iterations=iters,
        collapse_applied=tuple(collapse_applied),
        collapse_rejected=tuple(collapse_rejected),
        split_applied=tuple(split_applied),
        split_rejected=tuple(split_rejected),
        flips_applied=tuple(flips_applied),
        flips_rejected=tuple(flips_rejected),
        degree_histogram=_degree_histogram(canon, tris, n_canon),
        near_degenerate_triangle_count=int(
            np.sum(_min_angles(verts, tris) < _DEGENERATE_ANGLE)
        ),
        chi_before=int(chi_before),
        chi_after=int(chi_after),
        converged=bool(
            flips_applied
            and flips_applied[-1] == 0
            and collapse_applied[-1] == 0
            and split_applied[-1] == 0
        ),
    )
    out = PeriodicMesh(
        verts=verts,
        tris=tris,
        wrap=pm.wrap,
        cell=pm.cell,
        orientation_fallback_count=pm.orientation_fallback_count,
    )
    return out, report

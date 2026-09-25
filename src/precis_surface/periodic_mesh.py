"""precis_surface.periodic_mesh — marching cubes over one periodic TPMS
cell (docs/backlog/precis-surface-kernel.md "Slice 1 -- the dual route").

Meshes the zero set of a level-set function (e.g.
:mod:`precis_surface.level_set`) over one cubic cell ``[0, a]^3``,
producing the interface contract's bare ``Mesh = (verts, tris)`` tuple
plus the periodic bookkeeping the contract keeps separate: ``wrap``, the
index pairs identified across opposite cell faces, and ``cell``, the
lattice matrix. **Never merge or duplicate coordinates to express the
identification** -- both the low-face and high-face copy of an
identified point are kept as distinct vertex rows (they are physically
different embedded points, at ``x=0`` and ``x=a`` respectively, that
happen to be the *same point* once the cell repeats); ``wrap`` alone
records which pairs are that same point.

Reuses the standard 256-case marching-cubes tables
(:mod:`precis.cad._mc_tables`) directly -- the corner/edge conventions
and the ``TRI_TABLE`` triangle listing -- rather than
:func:`precis.cad.fieldmesh.field_mesh`. That function requires the zero
set strictly *inside* its padded box and raises on a non-watertight
result; neither holds for a periodic cell, whose level set crosses every
face of the box by construction, and whose raw single-cell triangulation
therefore has open boundary loops at those faces until :func:`wrap`'s
pairing is honoured (:func:`is_edge_manifold_closed` checks the *welded*
mesh, never the raw one). ``fieldmesh.py`` and ``_mc_tables.py`` are read
here for their table conventions, never modified.

**Orientation.** The standard table's per-case triangle winding is fixed
relative to the local "corner inside" bit pattern, but its *global* sign
(inward vs. outward) is only meaningful for a genuinely closed mesh --
which the raw per-cell output here is not. Orientation is therefore
decided per-triangle by the exact test: for a level set, the direction of
increasing field IS the outward normal direction, so each triangle is
oriented so its normal has positive dot product with ``grad(field)`` at
the triangle centroid (``grad`` is caller-supplied, or central differences
with a step tied to the grid pitch when omitted). This replaces an
earlier fixed-``eps`` finite-step probe, which was fragile exactly where
TPMS sheets pass close to a neighbouring sheet: a probe could step across
that neighbour and read its sign instead. Where ``|grad(field)|`` is near
zero the outward direction is undefined from the gradient alone; those
faces are resolved instead by breadth-first combinatorial propagation
over the face-adjacency graph (a TPMS is orientable, so flipping each
such face to agree with an already-oriented neighbour is consistent).

**The centring trap** (docs/backlog/precis-surface-kernel.md): body-
centring the cell swaps the P/D surface's two labyrinths and reports the
wrong Euler characteristic. This module never does that -- the cell is
always the plain simple-cubic ``[0, a]^3`` box with axis-aligned wrap
pairing, nothing folded or offset.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from precis.cad._mc_tables import CORNER_OFFSETS, EDGE_CORNERS, TRI_TABLE

#: A field function: ``(N, 3)`` world points -> ``(N,)`` values, zero on
#: the surface (need not be a true signed distance).
LevelSetFn = Callable[[NDArray[np.float64]], NDArray[np.float64]]

#: A gradient function: ``(N, 3)`` world points -> ``(N, 3)`` grad(field)
#: at those points, in the same world units as ``pts``.
GradientFn = Callable[[NDArray[np.float64]], NDArray[np.float64]]

#: The interface contract's bare mesh tuple: CCW outward.
Mesh = tuple[NDArray[np.float64], NDArray[np.int64]]

_CORNERS = np.array(CORNER_OFFSETS, dtype=np.int64)  # (8, 3)
_EDGE_MIN = np.array(
    [np.minimum(CORNER_OFFSETS[a], CORNER_OFFSETS[b]) for a, b in EDGE_CORNERS],
    dtype=np.int64,
)
_EDGE_AXIS = np.array(
    [
        int(np.nonzero(np.subtract(CORNER_OFFSETS[b], CORNER_OFFSETS[a]))[0][0])
        for a, b in EDGE_CORNERS
    ],
    dtype=np.int64,
)
_TRI = np.full((256, 15), -1, dtype=np.int64)
for _case, _row in enumerate(TRI_TABLE):
    _TRI[_case, : len(_row)] = _row
_NTRI = ((_TRI >= 0).sum(axis=1) // 3).astype(np.int64)


#: Field samples with ``|value| < _ZERO_TOL`` are treated as exactly on
#: the surface and nudged to barely-positive (see `periodic_mesh`'s inline
#: comment at the nudge site for why this must be a band, not a literal
#: ``== 0.0`` test).
_ZERO_TOL = 1e-8

#: Grad-magnitude faces below ``_GRAD_REL_TOL`` times the largest observed
#: gradient magnitude have an undefined outward direction from the
#: gradient test alone and fall back to combinatorial propagation.
_GRAD_REL_TOL = 1e-6


class PeriodicMeshError(RuntimeError):
    """The field has no zero crossing anywhere in the cell, produced a
    degenerate (triangle-free) case set, or its two opposite faces did
    not identify 1:1 (the field is not exactly periodic at this
    resolution)."""


@dataclass(frozen=True)
class PeriodicMesh:
    """One periodic cell's marching-cubes triangulation, welding-ready.

    ``verts``/``tris`` are the contract ``Mesh`` (CCW outward),
    ``wrap`` is ``(K, 2)`` int64 index pairs into ``verts`` that are
    identified once the cell repeats, and ``cell`` is the ``(3, 3)``
    lattice matrix (diagonal ``a`` for the plain cubic cell this module
    produces). Use :func:`welded_euler` / :func:`is_edge_manifold_closed`
    for the welded-quotient topology; ``verts``/``tris`` alone are the
    open, single-cell triangulation. ``orientation_fallback_count`` is the
    number of triangles whose outward direction ``grad(field)`` could not
    resolve (``|grad| ~ 0`` at the centroid) and were instead oriented by
    combinatorial propagation from an already-oriented neighbour.
    """

    verts: NDArray[np.float64]
    tris: NDArray[np.int64]
    wrap: NDArray[np.int64]
    cell: NDArray[np.float64]
    orientation_fallback_count: int = 0

    @property
    def mesh(self) -> Mesh:
        """The bare contract ``Mesh`` tuple."""
        return self.verts, self.tris


def periodic_mesh(
    field: LevelSetFn, a: float, n: int, grad: GradientFn | None = None
) -> PeriodicMesh:
    """Marching-cubes the zero set of ``field`` over the cubic periodic
    cell ``[0, a]^3``, sampled at ``n`` cells per axis.

    ``grad``, if given, returns ``(N, 3)`` ``grad(field)`` for an ``(N,
    3)`` point array and is used for the exact outward-orientation test
    (see :func:`_orient_ccw_outward`). When omitted, orientation falls
    back to central differences with a step tied to the grid pitch ``h``.

    Periodicity is structural, not numerical: ``field`` is sampled only
    on the half-open grid ``[0, a)`` (``n`` samples per axis, spacing
    ``h = a / n``) and every high-face lookup (grid index ``n``) reuses
    index ``0``'s sample via a ``% n`` wrap. The high face therefore *is*
    the low face's array, bit for bit -- the marching-cubes inside/outside
    sign test can never disagree between the two faces, which is what
    made Schwarz D and the gyroid fail here (float noise flipping the
    sign of a near-zero sample independently on each face) while Schwarz
    P happened to escape it (``cos(2*pi)`` is exactly ``1.0``). Vertex
    *positions* still use the unwrapped index (up to ``n``, i.e. world
    coordinate up to ``a``) so a wrapped edge's crossing point lands at
    its true location near ``x = a`` even though its field sample came
    from index 0 -- position and field-sample indexing are tracked
    separately throughout.

    Raises :class:`PeriodicMeshError` if the field never crosses zero in
    the cell, produces no triangles, or the low/high face crossings on
    some axis do not pair up 1:1 (a genuine periodicity mismatch --
    ``field``'s period isn't actually ``a``). ``field`` is evaluated
    exactly once per unique grid sample (``n**3`` points) plus once per
    output triangle (the orientation probe) -- no narrow-band budgeting,
    since one periodic cell is small by construction (contrast
    :mod:`precis.cad.fieldmesh`'s band-limited large-box export).
    """
    if not (a > 0.0 and np.isfinite(a)):
        raise ValueError(f"cell edge a must be a positive length, got {a}")
    if n < 2:
        raise ValueError(f"n must be >= 2 cells per axis, got {n}")
    h = a / n
    # `stride`/`nv` index the *unwrapped* (n+1)-per-axis key/position
    # space -- this is what keeps the low-face (index 0) and high-face
    # (index n) copies of a boundary point distinct rows, per the wrap
    # contract. `fstride` indexes the actual (n-per-axis) sample array.
    nv = n + 1
    stride = np.array([nv * nv, nv, 1], dtype=np.int64)
    fstride = np.array([n * n, n, 1], dtype=np.int64)

    ii, jj, kk = np.meshgrid(np.arange(n), np.arange(n), np.arange(n), indexing="ij")
    idx = np.stack([ii.ravel(), jj.ravel(), kk.ravel()], axis=1)
    grid_pts = idx.astype(np.float64) * h
    vals = np.asarray(field(grid_pts), dtype=np.float64)
    if vals.shape != (n**3,):
        raise ValueError(f"field() must return shape ({n**3},), got {vals.shape}")
    vals = vals.copy()
    # A sample on (or numerically indistinguishable from) the surface is
    # nudged to (barely) positive -- "outside" by this module's `field <=
    # 0` inside/`field > 0` outside convention -- so its crossing lands
    # exactly on the grid vertex instead of dividing 0/0ing. This must
    # catch not just literal `== 0.0` but the float-noise band around it:
    # a TPMS nodal form evaluated at a symmetric grid point (e.g. every
    # coordinate a multiple of pi/2) algebraically cancels to zero, but
    # floating-point trig lands it at O(1e-16), whose *sign* is
    # arbitrary rounding noise rather than the field's true local
    # behaviour. Left alone, two cell edges sharing that grid vertex can
    # independently resolve to opposite signs, or to a crossing parameter
    # ``t`` of ~0 from both sides, producing two distinct-but-coincident
    # vertices -- a degenerate (zero-area) triangle. `_ZERO_TOL` sits
    # many orders of magnitude above that noise floor (~1e-16) and many
    # orders below the smallest genuine non-symmetric sample observed at
    # these resolutions (~1e-3), so the tie-break never touches a real
    # crossing. Applied uniformly to every sample -- not just boundary
    # copies -- so the low-face and high-face (mod-``n``) lookups always
    # agree, preserving periodicity as well as closedness.
    vals[np.abs(vals) < _ZERO_TOL] = 1e-9 * h

    ci, cj, ck = np.meshgrid(np.arange(n), np.arange(n), np.arange(n), indexing="ij")
    cell_idx = np.stack([ci.ravel(), cj.ravel(), ck.ravel()], axis=1)  # (C, 3)
    cell_lin = cell_idx @ stride  # (C,) unwrapped key space
    # Corner *field* lookup wraps each corner's per-axis grid index mod
    # n, so the last cell's "high" corner reuses grid index 0's sample
    # bit for bit instead of a separately-evaluated (and possibly
    # sign-flipped) near-zero value.
    corner_idx = (cell_idx[:, None, :] + _CORNERS[None, :, :]) % n  # (C, 8, 3)
    corner_flin = corner_idx @ fstride  # (C, 8)
    cv = vals[corner_flin]
    case = np.zeros(len(cell_idx), dtype=np.int64)
    for m in range(8):
        case |= (cv[:, m] <= 0.0).astype(np.int64) << m
    active = (case != 0) & (case != 255)
    if not np.any(active):
        raise PeriodicMeshError(
            f"field has no zero crossing anywhere in the [0, {a}]^3 cell at "
            f"n={n} -- nothing to mesh"
        )
    a_lin = cell_lin[active]
    a_case = case[active]
    counts = _NTRI[a_case]
    total = int(counts.sum())
    if total == 0:
        raise PeriodicMeshError(
            f"active cells produced zero triangles at n={n} -- resolution "
            "too coarse for this field"
        )
    rep = np.repeat(np.arange(len(a_lin)), counts)
    slot = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
    edges = _TRI[a_case[rep]][
        np.arange(total)[:, None], (3 * slot)[:, None] + np.arange(3)[None, :]
    ]  # (T, 3) cell-edge ids
    # grid-edge key = 3 * (linear index of the edge's min vertex) + axis
    keys = (a_lin[rep][:, None] + (_EDGE_MIN[edges] @ stride)) * 3 + _EDGE_AXIS[edges]

    ekeys, inv = np.unique(keys.ravel(), return_inverse=True)
    tris = inv.reshape(-1, 3).astype(np.int64)

    axis = ekeys % 3
    e_lin = ekeys // 3  # unwrapped key space (0..n per axis component)
    e_ijk = np.stack(
        [e_lin // stride[0], (e_lin // stride[1]) % nv, e_lin % nv], axis=1
    )
    row = np.arange(len(e_ijk))
    # Field samples for interpolation come from the wrapped (mod n)
    # index -- the "high" corner along the edge's own axis may sit at
    # unwrapped coordinate n, which has no sample of its own and instead
    # reuses index 0's (see `fstride` above).
    lo_wrapped = e_ijk % n
    hi_ijk = e_ijk.copy()
    hi_ijk[row, axis] += 1
    hi_wrapped = hi_ijk % n
    va = vals[lo_wrapped @ fstride]
    vb = vals[hi_wrapped @ fstride]
    t = va / (va - vb)
    # Position uses the *unwrapped* index, so a crossing near the high
    # face lands at its true world coordinate near x = a, not at x = 0.
    verts_ijk = e_ijk.astype(np.float64)
    verts_ijk[row, axis] += t
    verts = verts_ijk * h

    # wrap must exist before orientation: the combinatorial fallback for
    # near-zero-gradient faces walks face adjacency on the *welded*
    # (periodic-identified) quotient, so a face pair that is only
    # adjacent across the wrap seam is still found.
    wrap = _wrap_pairs(e_lin, axis, nv, n, stride)
    tris, orientation_fallback_count = _orient_ccw_outward(
        verts, tris, field, h, wrap, grad
    )

    cell = np.eye(3, dtype=np.float64) * a
    return PeriodicMesh(
        verts=verts,
        tris=tris,
        wrap=wrap,
        cell=cell,
        orientation_fallback_count=orientation_fallback_count,
    )


def _central_diff_gradient(
    field: LevelSetFn, pts: NDArray[np.float64], eps: float
) -> NDArray[np.float64]:
    """Central-difference ``grad(field)`` at ``pts``, step ``eps`` on each
    axis independently. Fallback used by :func:`_orient_ccw_outward` when
    the caller supplies no analytic gradient."""
    grad = np.empty_like(pts)
    for axis in range(3):
        delta = np.zeros(3, dtype=np.float64)
        delta[axis] = eps
        f_plus = np.asarray(field(pts + delta), dtype=np.float64)
        f_minus = np.asarray(field(pts - delta), dtype=np.float64)
        grad[:, axis] = (f_plus - f_minus) / (2.0 * eps)
    return grad


def _orient_ccw_outward(
    verts: NDArray[np.float64],
    tris: NDArray[np.int64],
    field: LevelSetFn,
    h: float,
    wrap: NDArray[np.int64],
    grad: GradientFn | None,
) -> tuple[NDArray[np.int64], int]:
    """Orient each triangle so its normal has positive dot product with
    ``grad(field)`` at the triangle centroid -- the exact outward
    direction for a level set, replacing the earlier fixed-step probe
    (see module docstring). ``grad`` defaults to a central-difference
    approximation, step tied to ``h``. Faces where ``|grad|`` is near
    zero (relative to the largest gradient magnitude observed) have no
    defined direction from the gradient alone and are resolved by
    :func:`_propagate_orientation` instead; returns ``(tris,
    fallback_count)``."""
    a = verts[tris[:, 0]]
    b = verts[tris[:, 1]]
    c = verts[tris[:, 2]]
    normal = np.cross(b - a, c - a)
    centroid = (a + b + c) / 3.0

    if grad is None:
        eps = 1e-2 * h
        g = _central_diff_gradient(field, centroid, eps)
    else:
        g = np.asarray(grad(centroid), dtype=np.float64)
        if g.shape != centroid.shape:
            raise ValueError(
                f"grad() must return shape {centroid.shape}, got {g.shape}"
            )

    grad_mag = np.linalg.norm(g, axis=1)
    scale = float(np.max(grad_mag)) if grad_mag.size else 0.0
    defined = grad_mag > _GRAD_REL_TOL * scale
    fallback_count = int(np.sum(~defined))

    dot = np.einsum("ij,ij->i", normal, g)
    flip = np.zeros(len(tris), dtype=bool)
    flip[defined] = dot[defined] < 0.0
    out = tris.copy()
    out[flip] = out[flip][:, [0, 2, 1]]

    if fallback_count > 0:
        out = _propagate_orientation(verts, out, defined, wrap)

    return out, fallback_count


def _propagate_orientation(
    verts: NDArray[np.float64],
    tris: NDArray[np.int64],
    defined: NDArray[np.bool_],
    wrap: NDArray[np.int64],
) -> NDArray[np.int64]:
    """Resolve the orientation of every ``~defined`` (near-zero-gradient)
    triangle by breadth-first propagation over the face-adjacency graph
    of the *welded* (periodic-identified) quotient, starting from the
    already-correctly-oriented ``defined`` faces. A TPMS is orientable,
    so flipping an unresolved face to make its shared edge run opposite
    its resolved neighbour's is consistent and terminates once every
    reachable face is visited."""
    canon = weld_indices(len(verts), wrap)
    out = tris.copy()
    ctris = canon[out]
    n_tris = len(out)

    edge_owner: dict[tuple[int, int], list[int]] = {}
    for t in range(n_tris):
        v0, v1, v2 = int(ctris[t, 0]), int(ctris[t, 1]), int(ctris[t, 2])
        for u, w in ((v0, v1), (v1, v2), (v2, v0)):
            key = (u, w) if u < w else (w, u)
            edge_owner.setdefault(key, []).append(t)

    resolved = defined.copy()
    queue: deque[int] = deque(int(t) for t in np.nonzero(defined)[0])
    while queue:
        t = queue.popleft()
        v0, v1, v2 = int(ctris[t, 0]), int(ctris[t, 1]), int(ctris[t, 2])
        for u, w in ((v0, v1), (v1, v2), (v2, v0)):
            key = (u, w) if u < w else (w, u)
            for nbr in edge_owner.get(key, ()):
                if nbr == t or resolved[nbr]:
                    continue
                nv0, nv1, nv2 = (
                    int(ctris[nbr, 0]),
                    int(ctris[nbr, 1]),
                    int(ctris[nbr, 2]),
                )
                for a2, b2 in ((nv0, nv1), (nv1, nv2), (nv2, nv0)):
                    if {a2, b2} == {u, w}:
                        if (a2, b2) == (u, w):
                            # Same direction as this (already-resolved)
                            # triangle's traversal of the shared edge --
                            # a consistently-oriented 2-manifold requires
                            # opposite traversal, so this neighbour is
                            # mis-oriented and must flip.
                            out[nbr] = out[nbr][[0, 2, 1]]
                            ctris[nbr] = ctris[nbr][[0, 2, 1]]
                        break
                resolved[nbr] = True
                queue.append(nbr)

    unresolved = int(np.sum(~resolved))
    if unresolved > 0:
        raise PeriodicMeshError(
            f"{unresolved} triangle(s) with undefined gradient direction "
            "are not connected, via face adjacency, to any triangle whose "
            "outward direction the gradient test could resolve -- cannot "
            "orient consistently"
        )
    return out


def _wrap_pairs(
    e_lin: NDArray[np.int64],
    axis: NDArray[np.int64],
    nv: int,
    n: int,
    stride: NDArray[np.int64],
) -> NDArray[np.int64]:
    """Pair mesh-vertex indices (positions into the deduplicated crossing
    list, i.e. into :attr:`PeriodicMesh.verts`) across each pair of
    opposite cell faces, matched by the two non-wrap-axis grid
    coordinates plus the crossing edge's own axis -- exact because the
    field is periodic with period ``a`` by construction, so the low-face
    and high-face crossing patterns on a wrap axis are identical."""
    coords = np.stack(
        [e_lin // stride[0], (e_lin // stride[1]) % nv, e_lin % nv], axis=1
    )
    pairs: list[NDArray[np.int64]] = []
    for w in range(3):
        others = [ax for ax in range(3) if ax != w]
        low = np.nonzero((coords[:, w] == 0) & (axis != w))[0]
        high = np.nonzero((coords[:, w] == n) & (axis != w))[0]
        if len(low) != len(high):
            raise PeriodicMeshError(
                f"periodic identification failed on axis {w}: {len(low)} "
                f"low-face crossings vs {len(high)} high-face -- field is "
                "not exactly periodic with period a at this grid"
            )
        if len(low) == 0:
            continue

        def _key(
            sel: NDArray[np.int64], _others: list[int] = others
        ) -> NDArray[np.int64]:
            return (
                coords[sel][:, _others[0]] * (nv + 1) + coords[sel][:, _others[1]]
            ) * 3 + axis[sel]

        lk = _key(low)
        hk = _key(high)
        lo_order = np.argsort(lk)
        hi_order = np.argsort(hk)
        if not np.array_equal(lk[lo_order], hk[hi_order]):
            raise PeriodicMeshError(
                f"periodic identification failed on axis {w}: low/high-face "
                "crossing coordinates do not match 1:1 -- field is not "
                "exactly periodic with period a at this grid"
            )
        pairs.append(np.stack([low[lo_order], high[hi_order]], axis=1))
    if not pairs:
        return np.empty((0, 2), dtype=np.int64)
    return np.concatenate(pairs, axis=0).astype(np.int64)


def weld_indices(n_verts: int, wrap: NDArray[np.int64]) -> NDArray[np.int64]:
    """Union-find canonical id (``0..n_verts-1``) for each raw vertex
    index under the ``wrap`` identification -- the map from a
    :class:`PeriodicMesh` vertex to its class representative on the
    welded quotient."""
    parent = np.arange(n_verts, dtype=np.int64)

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = int(parent[root])
        while parent[x] != root:
            parent[x], x = root, int(parent[x])
        return root

    for u, v in wrap:
        ru, rv = find(int(u)), find(int(v))
        if ru != rv:
            parent[ru] = rv
    return np.array([find(i) for i in range(n_verts)], dtype=np.int64)


def welded_euler(pm: PeriodicMesh) -> tuple[int, int, int, int]:
    """``(V, E, F, chi)`` on the welded quotient -- ``wrap`` pairs merged
    into one vertex class before counting, ``chi = V - E + F``. Purely
    combinatorial: correct regardless of which of an identified pair's
    two coordinate copies a caller later chooses to keep for geometry."""
    canon = weld_indices(len(pm.verts), pm.wrap)
    ctris = canon[pm.tris]
    v = len(np.unique(ctris))
    edges = np.concatenate([ctris[:, [0, 1]], ctris[:, [1, 2]], ctris[:, [2, 0]]])
    edges = np.sort(edges, axis=1)
    ekeys = edges[:, 0].astype(np.int64) * len(pm.verts) + edges[:, 1]
    e = len(np.unique(ekeys))
    f = len(pm.tris)
    return v, e, f, v - e + f


def is_edge_manifold_closed(pm: PeriodicMesh) -> bool:
    """``True`` iff every undirected edge of the welded quotient (``wrap``
    pairs merged) is shared by exactly two triangles -- the welding
    actually closed the raw single-cell mesh into a boundary-free
    2-manifold."""
    canon = weld_indices(len(pm.verts), pm.wrap)
    ctris = canon[pm.tris]
    edges = np.concatenate([ctris[:, [0, 1]], ctris[:, [1, 2]], ctris[:, [2, 0]]])
    edges = np.sort(edges, axis=1)
    ekeys = edges[:, 0].astype(np.int64) * len(pm.verts) + edges[:, 1]
    _uniq, counts = np.unique(ekeys, return_counts=True)
    return bool(np.all(counts == 2))

"""precis_surface.dual — the dual route (docs/backlog/precis-surface-kernel.md
"Slice 1 -- the dual route"): the dual of a degree-controlled triangulation
*is* the hex tiling, so ``dualise`` turns a :class:`~precis_surface.periodic_mesh.PeriodicMesh`
into a hexfold-shaped net without ever building a direction field or an MIQ
solve. Triangle -> atom, degree-``n`` welded vertex -> ``n``-ring, and the
dual's 3-valence (every atom has exactly 3 bonds, one per edge of its
triangle) falls out for free -- no separate check is needed to make that
true, it is an algebraic consequence of "every triangle has 3 edges".

**Periodicity is carried the same way :mod:`precis_surface.periodic_mesh`
carries it**: the dual net lives on the welded quotient of one periodic
cell, and each bond additionally carries a ``(3,)`` integer lattice-image
shift (0 on an interior bond, one axis at ``+-1`` when the shared primal
edge crossed the cell's wrap identification). This makes the one-cell net
itself the periodic unit -- :func:`unroll` is the only place that
materialises a finite tiling, dropping any bond whose neighbour cell falls
outside the requested ``reps`` box (open valence at the supercell boundary,
exactly like the CNT/cone generators' open rim atoms in
:mod:`precis_se.atomic.generators.sp2`).

**Why there is no ``to_hexfold_net``.** :class:`hexfold.build.Net` is
frozen with ``spec: Spec``, ``lattice: Lattice`` and ``report: Report`` as
mandatory fields with no sensible default -- they are the ``.hx``-text
authoring contract's own objects (a parsed spec AST, hexfold's 2D
honeycomb lattice record, a build report), not general-purpose atom/bond/
ring containers. Synthesising a fake ``Spec``/``Lattice``/``Report`` just to
satisfy the dataclass would misrepresent provenance that was never
authored as hexfold DSL text -- exactly the kind of faked construction the
build task for this module was told not to do. So the bridge this slice
exports is the plain ``(coords, bonds)`` triple :func:`unroll` returns (plus
:class:`DualNet` itself, which already carries ``rings``) -- "handed to
hexfold across the existing precis -> hexfold direction" for now means "as
plain arrays a later slice's own ``.hx``-authoring boundary decides how to
lift into a ``Net``", not a direct dataclass construction here.

Pure functions, no store access, numpy only (:mod:`precis_surface`'s house
rules) -- ``precis_surface`` must never import ``precis_se``; the
dependency runs the other way (``precis_se.atomic.generators.tpms`` imports
this module).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from precis_surface.periodic_mesh import (
    GradientFn,
    LevelSetFn,
    PeriodicMesh,
    is_edge_manifold_closed,
    weld_indices,
)

#: Number of replicas along each of the 3 cell axes for :func:`unroll`.
Reps = tuple[int, int, int]

#: Newton iterations for the surface-projection step in :func:`dualise` --
#: a triangle centroid already sits close to the zero set (its 3 vertices
#: are exactly on it), so a couple of steps is ample; not exposed as a
#: knob because a caller that needs more precision should instead be
#: refining ``n`` in :func:`~precis_surface.periodic_mesh.periodic_mesh`.
_PROJECT_ITERS = 3


@dataclass(frozen=True)
class DualNet:
    """One periodic cell's triangle-dual net (welded quotient).

    ``atoms`` is ``(N, 3)`` float64, one row per triangle of the source
    :class:`~precis_surface.periodic_mesh.PeriodicMesh`, ``N == F`` (the
    welded triangle count). ``bonds`` is ``(B, 3)`` int64 rows
    ``(i, j, order)`` -- one per welded (canonical) mesh edge, ``B == E``
    (the welded edge count); ``order`` is always ``1`` today, a placeholder
    column matching :attr:`hexfold.build.Net.bonds`'s own ``(i, j, order)``
    shape rather than a chemistry decision made here (bond order is a
    :mod:`precis_se.atomic.generators` concern, assigned once atoms become
    carbon). ``shifts`` is the parallel ``(B, 3)`` int64 lattice-image
    shift: bond row ``k`` means atom ``bonds[k, 0]`` in cell ``c`` bonds to
    atom ``bonds[k, 1]`` in cell ``c + shifts[k]`` -- ``(0, 0, 0)`` for an
    interior bond, one axis at ``+-1`` for a bond that crosses the cell's
    periodic identification. ``rings`` is one entry per welded vertex,
    ``V`` of them, each ``(size, member_atoms)`` with ``size`` the vertex's
    degree (== the ring's atom count) and ``member_atoms`` the triangle
    (atom) indices around it in cyclic order. ``cell`` is the ``(3, 3)``
    lattice matrix, copied from the source mesh.
    """

    atoms: NDArray[np.float64]
    bonds: NDArray[np.int64]
    shifts: NDArray[np.int64]
    rings: tuple[tuple[int, tuple[int, ...]], ...]
    cell: NDArray[np.float64]

    def ring_histogram(self) -> dict[int, int]:
        """``{ring size: count}`` over :attr:`rings` -- the hexfold-style
        pentagon/hexagon/heptagon/... census
        (:func:`hexfold.defects.counting_residual`'s ``pn`` argument)."""
        hist: dict[int, int] = {}
        for size, _members in self.rings:
            hist[size] = hist.get(size, 0) + 1
        return hist


def _project_to_zero_set(
    pts: NDArray[np.float64], f: LevelSetFn, grad: GradientFn
) -> NDArray[np.float64]:
    """A few Newton steps of ``pts -= f(pts)/|grad(pts)|^2 * grad(pts)``,
    pulling each point onto ``f``'s zero set along the gradient direction.
    Started from a triangle centroid (already near the surface, since all
    3 vertices sit exactly on it), so a handful of steps is ample; the
    denominator is floored well above float64 noise so a rare
    near-critical-point centroid degrades to "barely moved" rather than a
    division blow-up."""
    out = pts.copy()
    for _ in range(_PROJECT_ITERS):
        val = np.asarray(f(out), dtype=np.float64)
        g = np.asarray(grad(out), dtype=np.float64)
        g2 = np.einsum("ij,ij->i", g, g)
        g2 = np.where(g2 < 1e-24, 1.0, g2)
        out = out - (val / g2)[:, None] * g
    return out


def dualise(
    pm: PeriodicMesh,
    *,
    f: LevelSetFn | None = None,
    grad: GradientFn | None = None,
) -> DualNet:
    """The triangle dual of ``pm``'s welded quotient (module docstring).

    ``f``/``grad``, if both given, project each triangle's plain centroid
    onto ``f``'s zero set along ``grad`` (:func:`_project_to_zero_set`);
    with neither, atoms sit at the plain (unprojected) centroid. Passing
    only one of the two is a caller error (there is no sensible partial
    projection).

    Raises ``ValueError`` if ``pm``'s welded quotient is not a closed
    2-manifold (every edge must be shared by exactly 2 triangles -- see
    :func:`~precis_surface.periodic_mesh.is_edge_manifold_closed`), or if a
    wrap-crossing edge's two endpoints disagree on which axis/direction
    they cross (a cell-corner edge running along the simultaneous
    intersection of two wrap faces -- not handled by this route; not
    observed for the Schwarz P/D/gyroid families at the resolutions this
    slice tests).
    """
    if (f is None) != (grad is None):
        raise ValueError("dualise: pass both f and grad, or neither")
    if not is_edge_manifold_closed(pm):
        raise ValueError(
            "dualise: pm's welded quotient is not a closed 2-manifold (some "
            "edge is not shared by exactly 2 triangles) -- the dual route "
            "needs a closed triangulation"
        )

    n_verts = len(pm.verts)
    raw_canon = weld_indices(n_verts, pm.wrap)
    uniq, canon = np.unique(raw_canon, return_inverse=True)
    n_canon = len(uniq)
    tris = pm.tris

    v0, v1, v2 = pm.verts[tris[:, 0]], pm.verts[tris[:, 1]], pm.verts[tris[:, 2]]
    centroids = (v0 + v1 + v2) / 3.0
    atoms = (
        _project_to_zero_set(centroids, f, grad)
        if f is not None and grad is not None
        else centroids
    )

    # Group every raw (triangle, edge) occurrence by its canonical
    # (welded) edge; a closed 2-manifold means each group has exactly 2
    # occurrences -- the dual bond between the two triangles that share it.
    edge_groups: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    for t in range(len(tris)):
        r0, r1, r2 = int(tris[t, 0]), int(tris[t, 1]), int(tris[t, 2])
        for ra, rb in ((r0, r1), (r1, r2), (r2, r0)):
            ca, cb = int(canon[ra]), int(canon[rb])
            key = (ca, cb) if ca < cb else (cb, ca)
            edge_groups.setdefault(key, []).append((t, ra, rb))

    bonds: list[tuple[int, int, int]] = []
    shifts: list[tuple[int, int, int]] = []
    # canonical vertex -> incident dual edges (as (atom_a, atom_b) pairs),
    # for the ring walk below.
    incident: dict[int, list[tuple[int, int]]] = {}

    for key, occ in edge_groups.items():
        if len(occ) != 2:
            raise ValueError(
                f"dualise: canonical edge {key} is shared by {len(occ)} "
                "triangle(s), expected exactly 2 (is_edge_manifold_closed "
                "should have caught this -- generator bug, file a gripe)"
            )
        (ta, ra0, ra1), (tb, rb0, rb1) = occ
        if {ra0, ra1} == {rb0, rb1}:
            i, j = (ta, tb) if ta < tb else (tb, ta)
            shift = (0, 0, 0)
        else:
            pairs = (
                ((ra0, rb0), (ra1, rb1))
                if canon[ra0] == canon[rb0]
                else ((ra0, rb1), (ra1, rb0))
            )
            axis: int | None = None
            sign = 0
            for x, y in pairs:
                if x == y:
                    continue
                diff = pm.verts[x] - pm.verts[y]
                ax = int(np.argmax(np.abs(diff)))
                sg = 1 if diff[ax] > 0 else -1
                if axis is None:
                    axis, sign = ax, sg
                elif (ax, sg) != (axis, sign):
                    raise ValueError(
                        f"dualise: wrap-crossing edge {key} disagrees on "
                        f"axis/direction between its two endpoints "
                        f"({(ax, sg)} vs {(axis, sign)}) -- likely a "
                        "cell-corner edge crossing two wrap axes at once, "
                        "not handled by this dual route"
                    )
            if axis is None:
                i, j = (ta, tb) if ta < tb else (tb, ta)
                shift = (0, 0, 0)
            else:
                shift_vec = [0, 0, 0]
                shift_vec[axis] = 1
                i, j = (ta, tb) if sign > 0 else (tb, ta)
                shift = (shift_vec[0], shift_vec[1], shift_vec[2])
        bonds.append((i, j, 1))
        shifts.append(shift)
        incident.setdefault(key[0], []).append((ta, tb))
        incident.setdefault(key[1], []).append((ta, tb))

    rings: list[tuple[int, tuple[int, ...]]] = []
    for v in range(n_canon):
        edges_v = incident.get(v, [])
        deg = len(edges_v)
        if deg == 0:
            continue
        local_adj: dict[int, list[int]] = {}
        for ta, tb in edges_v:
            local_adj.setdefault(ta, []).append(tb)
            local_adj.setdefault(tb, []).append(ta)
        start = edges_v[0][0]
        order = [start]
        prev: int | None = None
        cur = start
        for _ in range(deg - 1):
            nbrs = local_adj[cur]
            nxt = nbrs[0] if nbrs[0] != prev or len(nbrs) == 1 else nbrs[1]
            order.append(nxt)
            prev, cur = cur, nxt
        rings.append((deg, tuple(order)))

    return DualNet(
        atoms=atoms,
        bonds=np.array(bonds, dtype=np.int64).reshape(-1, 3),
        shifts=np.array(shifts, dtype=np.int64).reshape(-1, 3),
        rings=tuple(rings),
        cell=pm.cell,
    )


def unroll(
    dnet: DualNet, *, reps: Reps = (1, 1, 1)
) -> tuple[NDArray[np.float64], list[tuple[int, int]]]:
    """Tile ``dnet``'s one periodic cell into a finite ``reps`` supercell.

    Returns ``(coords, bonds)``: ``coords`` is ``(reps[0]*reps[1]*reps[2] *
    N, 3)`` float64 (cell ``(ci, cj, ck)``'s atoms occupy rows
    ``cell_lin(ci,cj,ck)*N .. +N``, in the same row order as
    ``dnet.atoms`` within each cell); ``bonds`` is a plain ``(i, j)`` index
    pair list into ``coords``. Every one of ``dnet``'s intra-cell bonds
    (``shift == (0,0,0)``) survives in every replica; a wrap-crossing bond
    survives only where its neighbour cell ``(ci,cj,ck) + shift`` still
    falls inside the ``reps`` box -- so an atom at the supercell boundary
    keeps fewer than 3 bonds, open valence exactly like the CNT/cone
    generators' rim atoms. ``reps=(1,1,1)`` is itself already a (unit)
    supercell with open boundaries on every axis that has any
    wrap-crossing bond -- it does not behave like an infinite periodic
    tiling.
    """
    rx, ry, rz = reps
    if rx < 1 or ry < 1 or rz < 1:
        raise ValueError(f"reps must each be >= 1, got {reps}")
    n_atoms = len(dnet.atoms)
    cell = dnet.cell

    def cell_lin(ci: int, cj: int, ck: int) -> int:
        return (ci * ry + cj) * rz + ck

    coords = np.empty((rx * ry * rz * n_atoms, 3), dtype=np.float64)
    for ci in range(rx):
        for cj in range(ry):
            for ck in range(rz):
                offset = np.array([ci, cj, ck], dtype=np.float64) @ cell
                base = cell_lin(ci, cj, ck) * n_atoms
                coords[base : base + n_atoms] = dnet.atoms + offset

    bonds: list[tuple[int, int]] = []
    for ci in range(rx):
        for cj in range(ry):
            for ck in range(rz):
                cbase = cell_lin(ci, cj, ck) * n_atoms
                for (i, j, _order), (sx, sy, sz) in zip(
                    dnet.bonds.tolist(), dnet.shifts.tolist(), strict=True
                ):
                    ni, nj, nk = ci + sx, cj + sy, ck + sz
                    if 0 <= ni < rx and 0 <= nj < ry and 0 <= nk < rz:
                        nbase = cell_lin(ni, nj, nk) * n_atoms
                        bonds.append((cbase + i, nbase + j))
    return coords, bonds

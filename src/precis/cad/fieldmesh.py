"""Folded signed-distance field → triangle mesh (narrow-band marching cubes).

The export backend for designs the analytic tessellate + manifold3d route
cannot represent: a rounded leaf (``rd``) or a blended union (``blend:``)
has no closed-form triangle mesh, but the kernel already holds its exact
signed distance (:func:`precis.cad.fold.expr_sdf_np`), so the mesh is
*extracted once* from that field at export and never edited afterwards
(the representation contract in
``docs/backlog/cad-sdf-rounding-and-field-export.md``). numpy only — no
skimage, no scipy.

Pipeline (:func:`field_mesh`):

1. **Grid.** A regular grid at the export ``pitch`` over the caller's
   AABB, padded by 1.5 pitch so every boundary vertex is outside the solid
   (a closed field ⇒ a closed mesh).
2. **Coarse sample.** The SDF at the *centres* of coarse cells (``F``
   fine cells per axis, ``F`` chosen so the coarse grid stays around
   :data:`COARSE_POINTS_TARGET` points). A coarse cell can contain the
   surface only if ``|d(centre)| <= L · half-diagonal`` (``L`` the field's
   Lipschitz constant: 1 for min/max folds of exact distances, < 1.5 with
   the smooth-min blend; :data:`BAND_SAFETY` covers it) — every other
   cell is skipped outright. This is the narrow band.
3. **Fine sample.** The SDF at the fine-grid vertices of band cells only,
   each vertex evaluated once (unique linear index), in
   :data:`EVAL_CHUNK`-row chunks so memory stays bounded.
4. **Marching cubes** on the band's fine cells with the standard 256-case
   tables (:mod:`precis.cad._mc_tables`). Crossing vertices are keyed by
   grid edge, so neighbouring cells share them exactly (no welding pass).
5. **Orientation** — :func:`precis.cad.tessellate._orient_outward` (signed
   volume > 0).
6. **Watertightness check** — every undirected edge in exactly two
   triangles and every directed edge exactly once; a violation raises
   :class:`FieldMeshError` naming the grid cell and its world position
   rather than writing a leaky file.

The band is budgeted: more than :data:`MAX_BAND_CELLS` fine cells raises
:class:`FieldMeshError` naming the count and the pitch that caused it —
the caller coarsens deliberately; this module never swaps the pitch
underneath it. Dual contouring / adaptive octrees are the named upgrade
when that refusal fires on real parts.

**The grid is public** (:func:`field_grid` → :class:`FieldGrid`,
:func:`sample_grid`, :func:`snap_lo`): a caller that needs the sampled
sign field itself — se's manufacture root labels its connected
components on it — samples the whole grid once, and a caller that wants
several meshes (or a stored :class:`~precis.cad.primitives.Field` leaf
and the mesh that reads it) to share one set of sample points snaps its
boxes onto one lattice. The mesher itself never takes precomputed or
edited values: every vertex it marches is the ``sdf`` evaluated there
(:func:`precis.cad.export.object_meshes` splits a design into objects by
meshing each object's own exact fold, not by masking a shared grid).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from precis.cad._mc_tables import CORNER_OFFSETS, EDGE_CORNERS, TRI_TABLE
from precis.cad.tessellate import Mesh, _orient_outward
from precis.cad.vec import Vec3, as_vec3

#: Refuse (rather than coarsen) when the narrow band holds more fine cells
#: than this. ~50 M cells is the point where the sampled field stops being
#: an export and starts being a memory event.
MAX_BAND_CELLS = 50_000_000

#: The coarse pass aims for about this many centre samples; the coarse
#: factor ``F`` (fine cells per coarse cell per axis) is derived from it.
COARSE_POINTS_TARGET = 2_000_000

#: Lipschitz margin on the band test (``|d(centre)| <= BAND_SAFETY ·
#: half-diagonal``). 1 would be exact for min/max folds of exact SDFs; the
#: smooth-min blend's gradient can reach ~1.5, so 2 keeps the band a
#: guaranteed superset of every cell the surface passes through.
BAND_SAFETY = 2.0

#: Rows per vectorised SDF evaluation (memory bound on the temporaries).
EVAL_CHUNK = 1 << 20

#: Band coarse cells per marching-cubes batch (memory bound on the
#: per-cell corner tables: ``_COARSE_BATCH · F³ · 8`` int64).
_COARSE_BATCH = 4096

#: Grid padding beyond the caller's AABB, in pitches. A non-integer pad
#: keeps grid planes off the sharp AABB faces most designs have at round
#: coordinates, so exact-zero samples (degenerate slivers) stay rare.
_PAD_PITCHES = 1.5

SdfFn = Callable[[NDArray[np.float64]], NDArray[np.float64]]


class FieldMeshError(RuntimeError):
    """The field could not be meshed within the contract (budget exceeded,
    empty field, or a non-watertight extraction)."""


def _eval_chunked(sdf: SdfFn, pts: NDArray[np.float64]) -> NDArray[np.float64]:
    out = np.empty(len(pts), dtype=np.float64)
    for start in range(0, len(pts), EVAL_CHUNK):
        stop = min(start + EVAL_CHUNK, len(pts))
        out[start:stop] = sdf(pts[start:stop])
    return out


def _coarse_factor(n: NDArray[np.int64]) -> int:
    cells = float(np.prod(n.astype(np.float64)))
    return max(2, math.ceil((cells / COARSE_POINTS_TARGET) ** (1.0 / 3.0)))


# The 12 cell edges as (min-corner offset, axis) — a grid edge's identity.
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
_CORNERS = np.array(CORNER_OFFSETS, dtype=np.int64)  # (8, 3)

# TRI_TABLE as a (256, 15) int64 array padded with -1, plus triangles/case.
_TRI = np.full((256, 15), -1, dtype=np.int64)
for _case, _row in enumerate(TRI_TABLE):
    _TRI[_case, : len(_row)] = _row
_NTRI = ((_TRI >= 0).sum(axis=1) // 3).astype(np.int64)


@dataclass(frozen=True)
class FieldGrid:
    """The fine vertex grid :func:`field_mesh` samples for one ``(lo, hi,
    pitch)``: vertex ``(i, j, k)`` sits at ``origin + (i, j, k) · pitch``;
    ``nv`` is the vertex count per axis (the coarse cells of ``factor``
    fine cells tile it exactly). Same units as the caller's box."""

    origin: Vec3
    nv: tuple[int, int, int]
    pitch: float
    factor: int

    @property
    def cells(self) -> int:
        """Vertices in the grid — what a full :func:`sample_grid` costs."""
        return self.nv[0] * self.nv[1] * self.nv[2]

    @property
    def stride(self) -> NDArray[np.int64]:
        return np.array([self.nv[1] * self.nv[2], self.nv[2], 1], dtype=np.int64)

    def points(self) -> NDArray[np.float64]:
        """Every vertex, ``(cells, 3)``, in C (``i``-major) order — the order
        :func:`sample_grid`'s array flattens to."""
        ii, jj, kk = np.meshgrid(*(np.arange(n) for n in self.nv), indexing="ij")
        idx = np.stack([ii.ravel(), jj.ravel(), kk.ravel()], axis=1).astype(float)
        return self.origin[None, :] + idx * self.pitch


def field_grid(lo: Vec3, hi: Vec3, pitch: float, *, name: str = "field") -> FieldGrid:
    """The grid :func:`field_mesh` will use for ``[lo, hi]`` at ``pitch``
    (module docstring step 1) — public so a caller can size, sample or
    align other grids to it before meshing. Raises :class:`FieldMeshError`
    for a non-positive pitch or a degenerate box."""
    lo = as_vec3(lo)
    hi = as_vec3(hi)
    if not (pitch > 0.0 and math.isfinite(pitch)):
        raise FieldMeshError(f"{name}: pitch must be a positive length, got {pitch}")
    if np.any(hi <= lo):
        raise FieldMeshError(f"{name}: degenerate AABB {lo.tolist()} .. {hi.tolist()}")
    origin = lo - _PAD_PITCHES * pitch
    span = hi + _PAD_PITCHES * pitch - origin
    n_fine = np.ceil(span / pitch).astype(np.int64) + 1  # cells per axis
    factor = _coarse_factor(n_fine)
    n_coarse = np.ceil(n_fine / factor).astype(np.int64)
    n_fine = n_coarse * factor  # extend so coarse cells tile exactly
    nv = n_fine + 1  # fine vertices per axis
    return FieldGrid(
        origin=origin,
        nv=(int(nv[0]), int(nv[1]), int(nv[2])),
        pitch=float(pitch),
        factor=int(factor),
    )


def sample_grid(sdf: SdfFn, grid: FieldGrid) -> NDArray[np.float64]:
    """``sdf`` at every vertex of ``grid`` → an ``nv``-shaped float64
    array (C order; ``.ravel()`` indexes like the mesher's linear vertex
    keys). Evaluated in :data:`EVAL_CHUNK` rows so memory stays bounded;
    the caller budgets ``grid.cells`` — this function never refuses."""
    out = np.empty(grid.nv, dtype=np.float64)
    flat = out.reshape(-1)
    nv = np.array(grid.nv, dtype=np.int64)
    stride = grid.stride
    total = grid.cells
    for start in range(0, total, EVAL_CHUNK):
        stop = min(start + EVAL_CHUNK, total)
        lin = np.arange(start, stop, dtype=np.int64)
        ijk = np.stack(
            [lin // stride[0], (lin // stride[1]) % nv[1], lin % nv[2]], axis=1
        ).astype(np.float64)
        flat[start:stop] = sdf(grid.origin + ijk * grid.pitch)
    return out


def snap_lo(lo: Vec3, origin: Vec3, pitch: float) -> Vec3:
    """The largest box corner ``<= lo`` for which :func:`field_grid`'s
    vertices land on the lattice ``origin + k · pitch`` — how several
    meshes (or a stored :class:`~precis.cad.primitives.Field` leaf and the
    mesh that reads it) share one set of sample points."""
    lo = as_vec3(lo)
    origin = as_vec3(origin)
    pad = _PAD_PITCHES * pitch
    return origin + pad + np.floor((lo - origin - pad) / pitch + 1e-9) * pitch


def field_mesh(
    sdf: SdfFn, lo: Vec3, hi: Vec3, pitch: float, *, name: str = "field"
) -> Mesh:
    """Extract the zero level set of ``sdf`` inside AABB ``[lo, hi]`` at
    ``pitch`` as an outward-oriented, watertight triangle mesh.

    ``sdf`` maps an ``(N, 3)`` world-point array to ``(N,)`` signed
    distances (negative inside) and must be Lipschitz with constant below
    :data:`BAND_SAFETY` (every kernel fold is). The zero set must lie
    inside ``[lo, hi]`` — the caller pads a blended union by its blend
    width. ``name`` labels error messages. Units are whatever ``lo``/
    ``hi``/``pitch`` are in.
    """
    grid = field_grid(lo, hi, pitch, name=name)
    origin = grid.origin
    factor = grid.factor
    nv = np.array(grid.nv, dtype=np.int64)
    n_coarse = (nv - 1) // factor
    stride = grid.stride

    # --- 2. coarse sample at cell centres -------------------------------
    cx, cy, cz = (np.arange(n_coarse[i], dtype=np.int64) for i in range(3))
    coarse_idx = np.stack(np.meshgrid(cx, cy, cz, indexing="ij"), axis=-1).reshape(
        -1, 3
    )
    centres = origin + (coarse_idx + 0.5) * (factor * pitch)
    d_coarse = _eval_chunked(sdf, centres)
    half_diag = 0.5 * math.sqrt(3.0) * factor * pitch
    band = coarse_idx[np.abs(d_coarse) <= BAND_SAFETY * half_diag]
    if len(band) == 0:
        state = "entirely outside" if np.all(d_coarse > 0.0) else "entirely inside"
        raise FieldMeshError(
            f"{name}: the field has no zero crossing in its bounding box "
            f"({state}) — nothing to mesh"
        )
    n_band_cells = len(band) * factor**3
    if n_band_cells > MAX_BAND_CELLS:
        raise FieldMeshError(
            f"{name}: narrow band would hold {n_band_cells:,} cells at pitch "
            f"{pitch:g} (budget {MAX_BAND_CELLS:,}) — coarsen the pitch "
            f"(≥ {pitch * (n_band_cells / MAX_BAND_CELLS) ** (1 / 2):.3g} "
            "should fit); the pitch is never swapped silently"
        )

    # --- 3. fine sample at the band's vertices --------------------------
    f1 = factor + 1
    v_off = np.stack(
        np.meshgrid(np.arange(f1), np.arange(f1), np.arange(f1), indexing="ij"),
        axis=-1,
    ).reshape(-1, 3)  # (f1³, 3) vertex offsets inside a coarse cell
    key_parts: list[NDArray[np.int64]] = []
    for start in range(0, len(band), _COARSE_BATCH):
        blk = band[start : start + _COARSE_BATCH] * factor  # (B, 3) min vertex
        vidx = (blk[:, None, :] + v_off[None, :, :]).reshape(-1, 3)
        key_parts.append(np.unique(vidx @ stride))
    vkeys = np.unique(np.concatenate(key_parts))
    vijk = np.stack(
        [vkeys // stride[0], (vkeys // stride[1]) % nv[1], vkeys % nv[2]], axis=1
    )
    vals = _eval_chunked(sdf, origin + vijk * pitch)
    # An exact zero sits on the surface; treat it as (barely) outside so
    # the crossing lands on the vertex instead of dividing 0/0.
    vals[vals == 0.0] = 1e-9 * pitch

    # --- 4. marching cubes over the band's fine cells -------------------
    c_off = np.stack(
        np.meshgrid(
            np.arange(factor), np.arange(factor), np.arange(factor), indexing="ij"
        ),
        axis=-1,
    ).reshape(-1, 3)  # (F³, 3) cell offsets inside a coarse cell
    corner_lin = _CORNERS @ stride  # (8,) linear offset of each corner
    tri_keys: list[NDArray[np.int64]] = []
    tri_cells: list[NDArray[np.int64]] = []
    for start in range(0, len(band), _COARSE_BATCH):
        blk = band[start : start + _COARSE_BATCH] * factor
        cells = (blk[:, None, :] + c_off[None, :, :]).reshape(-1, 3)  # (C, 3)
        cell_lin = cells @ stride  # (C,)
        corner_keys = cell_lin[:, None] + corner_lin[None, :]  # (C, 8)
        pos = np.searchsorted(vkeys, corner_keys)
        cv = vals[pos]  # (C, 8) corner values
        case = np.zeros(len(cells), dtype=np.int64)
        for m in range(8):
            case |= (cv[:, m] <= 0.0).astype(np.int64) << m
        active = (case != 0) & (case != 255)
        if not np.any(active):
            continue
        a_lin = cell_lin[active]
        a_case = case[active]
        counts = _NTRI[a_case]
        total = int(counts.sum())
        if total == 0:
            continue
        rep = np.repeat(np.arange(len(a_lin)), counts)
        slot = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
        edges = _TRI[a_case[rep]][
            np.arange(total)[:, None], (3 * slot)[:, None] + np.arange(3)[None, :]
        ]  # (T, 3) cell-edge ids
        # grid-edge key = 3·(linear index of the edge's min vertex) + axis
        keys = (a_lin[rep][:, None] + (_EDGE_MIN[edges] @ stride)) * 3 + _EDGE_AXIS[
            edges
        ]
        tri_keys.append(keys)
        tri_cells.append(a_lin[rep])
    if not tri_keys:
        raise FieldMeshError(f"{name}: no surface cells found inside the band")
    all_keys = np.concatenate(tri_keys)  # (T, 3)
    all_cells = np.concatenate(tri_cells)  # (T,)
    ekeys, inv = np.unique(all_keys.ravel(), return_inverse=True)
    tris = inv.reshape(-1, 3).astype(np.int64)

    # crossing point on each grid edge by linear interpolation
    axis = ekeys % 3
    a_lin = ekeys // 3
    b_lin = a_lin + stride[axis]
    va = vals[np.searchsorted(vkeys, a_lin)]
    vb = vals[np.searchsorted(vkeys, b_lin)]
    t = va / (va - vb)
    a_ijk = np.stack(
        [a_lin // stride[0], (a_lin // stride[1]) % nv[1], a_lin % nv[2]], axis=1
    ).astype(np.float64)
    a_ijk[np.arange(len(a_ijk)), axis] += t
    verts = origin + a_ijk * pitch

    # --- 5./6. orient, then prove it closed --------------------------------
    verts, tris = _orient_outward(verts, tris)
    _check_watertight(verts, tris, all_cells, stride, nv, origin, pitch, name)
    return verts, tris


def _check_watertight(
    verts: NDArray[np.float64],
    tris: NDArray[np.int64],
    tri_cells: NDArray[np.int64],
    stride: NDArray[np.int64],
    nv: NDArray[np.int64],
    origin: Vec3,
    pitch: float,
    name: str,
) -> None:
    """Every undirected edge in exactly two triangles, every directed edge
    exactly once — else :class:`FieldMeshError` naming an offending cell."""
    directed = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    owner = np.concatenate([tri_cells, tri_cells, tri_cells])
    nverts = len(verts)
    # one int64 key per edge — a 1-D unique is far cheaper than axis=0
    dkey = directed[:, 0] * nverts + directed[:, 1]
    und = np.sort(directed, axis=1)
    ukey = und[:, 0] * nverts + und[:, 1]
    _uniq, first, counts = np.unique(ukey, return_index=True, return_counts=True)
    bad = np.nonzero(counts != 2)[0]
    if len(bad) == 0:
        _d, dfirst, dcounts = np.unique(dkey, return_index=True, return_counts=True)
        if np.all(dcounts == 1):
            return
        cell_lin = int(owner[dfirst[np.nonzero(dcounts != 1)[0][0]]])
        kind = "inconsistently wound"
    else:
        cell_lin = int(owner[first[bad[0]]])
        kind = f"shared by {int(counts[bad[0]])} triangle(s), not 2"
    i, j, k = (
        cell_lin // int(stride[0]),
        (cell_lin // int(stride[1])) % int(nv[1]),
        cell_lin % int(nv[2]),
    )
    where = origin + np.array([i, j, k], dtype=np.float64) * pitch
    raise FieldMeshError(
        f"{name}: extracted mesh is not watertight — an edge is {kind} at "
        f"grid cell ({i}, {j}, {k}) near {where.tolist()} (pitch {pitch:g}); "
        f"{len(bad)} bad edge(s) of {len(_uniq)}"
    )

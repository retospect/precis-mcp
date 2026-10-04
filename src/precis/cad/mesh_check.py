"""Print-readiness checks and repairs on a finished triangle mesh.

Pure functions on ``(vertices float64 (N, 3), faces int (M, 3))`` in
**millimetres, build frame (z up, bed at z = 0)** — the mesh a slicer will
actually be handed. Nothing here knows about designs, se or the store; the
callers are :mod:`precis_se.printing` (report + export) and the tests.

Why it exists: marching cubes over a staircase field can leave a single
vertex a hair (0.02 mm) *below* every neighbour while sitting above the bed.
Sliced at the layer height that vertex is a tiny polygon with nothing under
it — Bambu Studio's SharpTail rule (a layer polygon overlapping neither the
layer below nor the one below that) flags it as a "floating region" even
though the shell is one clean closed manifold. The mesh-level overhang rule
and the voxel-grid SIMP rule both miss it, so:

- :func:`floating_islands` slices the shipped mesh the way a slicer does and
  reports every polygon with no support under it;
- :func:`lift_sharp_tails` removes the *sub-resolution* tails (a local
  z-minimum - a vertex or a plateau of equal-z vertices - less than ``tol``
  below its lowest outside neighbour) without touching topology, guarded
  against thin walls, flipped faces and volume drift
  (:func:`lift_rejection`); a real overhang is deeper than ``tol`` and
  stays a finding;
- :func:`slicer_cantilevers` mirrors Bambu Studio's "floating cantilever"
  rule (features under one line width are dropped, then the overhang
  beyond the slope limit is measured from what supports it);
- :func:`weld_and_drop_degenerate` / :func:`count_slivers` clean/count the
  zero-area triangles marching cubes emits;
- :func:`package_findings` validates a 3MF package against the core-spec
  essentials (members, content types, relationships, units, build items,
  index ranges, closed + consistently wound + positive-volume meshes).
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Any

import numpy as np

#: A layer polygon smaller than this (mm²) is numerical dust, not an island.
_ISLAND_MIN_AREA = 1e-9

#: ``lift_sharp_tails`` pass cap — a pass lifts every current floor up one
#: step, so a staircase of sub-tol steps (the real bracket's dimple is 8
#: deep) needs one pass per step; the ``tol`` cumulative cap bounds the total.
_MAX_LIFT_PASSES = 32

#: Vertices this close to the bed (mm) are the bed: never lifted, not counted.
_BED_SNAP = 1e-4
#: A lift may use at most this fraction of the material above a vertex.
_CLEARANCE_FRACTION = 0.5
#: A face touched by a lift keeps at least this fraction of its area.
_KEEP_AREA = 0.1

_CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_MODEL_REL_TYPE = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
_RELS_CT = "application/vnd.openxmlformats-package.relationships+xml"
_MODEL_CT = "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"


@dataclass(frozen=True)
class Island:
    """One layer polygon with nothing under it (see module docstring)."""

    z: float  #: slice height, mm
    x: float  #: centroid, mm
    y: float
    area: float  #: mm²


# ---------------------------------------------------------------------------
# floating regions
# ---------------------------------------------------------------------------


def _centroid(cross_section: Any) -> tuple[float, float]:
    """Area centroid of one connected cross-section (outer ring CCW, holes
    CW, so the signed shoelace sums subtract holes)."""
    a_sum = cx = cy = 0.0
    for ring in cross_section.to_polygons():
        p = np.asarray(ring, dtype=np.float64)
        if len(p) < 3:
            continue
        x, y = p[:, 0], p[:, 1]
        x2, y2 = np.roll(x, -1), np.roll(y, -1)
        w = x * y2 - x2 * y
        a_sum += 0.5 * float(w.sum())
        cx += float(((x + x2) * w).sum()) / 6.0
        cy += float(((y + y2) * w).sum()) / 6.0
    if abs(a_sum) < 1e-300:
        return 0.0, 0.0
    return cx / a_sum, cy / a_sum


def _supported(comp: Any, below: Any | None) -> bool:
    """``comp`` overlaps the layer ``below`` by more than dust — a point
    touch is not support."""
    return below is not None and float((comp ^ below).area()) > _ISLAND_MIN_AREA


def _manifold_of(verts: np.ndarray, tris: np.ndarray) -> Any:
    import manifold3d as m3d

    return m3d.Manifold(
        m3d.Mesh(
            vert_properties=np.asarray(verts, dtype=np.float64).astype(np.float32),
            tri_verts=np.asarray(tris).astype(np.uint32),
        )
    )


def floating_islands(
    vertices: np.ndarray,
    faces: np.ndarray,
    layer_height: float,
    *,
    bed_z: float = 0.0,
) -> list[Island]:
    """Every layer polygon that overlaps neither the layer below nor the one
    below that — Bambu's SharpTail rule. Slices at ``z_k = bed_z +
    (k + 0.5) * layer_height`` for every layer of the part. Layer 0 sits on
    the bed and is never floating; a layer whose *both* predecessors are
    empty (a shell hanging in mid-air) is. Overlap is the area of the
    cross-section intersection (manifold3d booleans), more than 1e-9 mm².

    Raises :class:`ValueError` when the mesh is not a manifold the slicer
    kernel accepts (run :func:`weld_and_drop_degenerate` first)."""
    if not layer_height > 0.0:
        raise ValueError(f"layer_height must be positive, got {layer_height}")
    verts = np.asarray(vertices, dtype=np.float64)
    tris = np.asarray(faces)
    if tris.size == 0:
        return []
    solid = _manifold_of(verts, tris)
    if solid.is_empty():
        raise ValueError(
            f"mesh is not a manifold the slicer kernel accepts: {solid.status()}"
        )
    top = float(verts[:, 2].max())
    n_layers = int(np.ceil((top - bed_z) / layer_height - 1e-9))
    islands: list[Island] = []
    below1: Any | None = None  # layer k-1
    below2: Any | None = None  # layer k-2
    for k in range(max(n_layers, 0)):
        z = bed_z + (k + 0.5) * layer_height
        layer = solid.slice(z)
        if layer.is_empty():
            layer = None
        if layer is not None and k >= 1:
            for comp in layer.decompose():
                area = float(comp.area())
                if area <= _ISLAND_MIN_AREA:
                    continue
                if _supported(comp, below1) or _supported(comp, below2):
                    continue
                cx, cy = _centroid(comp)
                islands.append(Island(z=z, x=cx, y=cy, area=area))
        below2, below1 = below1, layer
    return islands


# ---------------------------------------------------------------------------
# repairs / counts
# ---------------------------------------------------------------------------


def _plateau_labels(
    ea: np.ndarray, eb: np.ndarray, n: int, same: np.ndarray
) -> np.ndarray:
    """Connected components of the vertices joined by the ``same`` edges
    (min-label propagation with pointer jumping)."""
    labels = np.arange(n)
    pa, pb = ea[same], eb[same]
    for _ in range(10_000):
        m = np.minimum(labels[pa], labels[pb])
        new = labels.copy()
        np.minimum.at(new, pa, m)
        np.minimum.at(new, pb, m)
        new = new[new]
        if np.array_equal(new, labels):
            break
        labels = new
    return labels


def _face_normals(verts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    p0, p1, p2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    return np.cross(p1 - p0, p2 - p0)


def signed_volume(vertices: np.ndarray, faces: np.ndarray) -> float:
    """Signed volume of a closed triangle mesh (positive = outward wound)."""
    v = np.asarray(vertices, dtype=np.float64)
    t = np.asarray(faces)
    p0, p1, p2 = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
    return float(np.einsum("ij,ij->i", p0, np.cross(p1, p2)).sum() / 6.0)


class _Ceiling:
    """Vertical-ray clearance above chosen vertices: the distance up to the
    first mesh surface over each (the material thickness above a floor
    vertex). xy never changes during a lift, so the triangles' xy bounding
    boxes are computed once; z is read from the live vertices."""

    def __init__(self, verts: np.ndarray, tris: np.ndarray, slack: float) -> None:
        corners = verts[tris]  # (M, 3, 3)
        xy = corners[:, :, :2]
        self.lo = xy.min(axis=1)
        self.hi = xy.max(axis=1)
        a, b, c = xy[:, 0], xy[:, 1], xy[:, 2]
        d = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (
            a[:, 1] - c[:, 1]
        )
        self.ok = np.abs(d) > 1e-12  # vertical walls project to nothing
        self.tz_max = corners[:, :, 2].max(axis=1)
        self.slack = slack
        self.tris = tris

    def above(self, idx: np.ndarray, verts: np.ndarray) -> np.ndarray:
        pts = verts[idx]
        out = np.full(len(idx), np.inf)
        lo = pts[:, :2].min(axis=0) - 1e-9
        hi = pts[:, :2].max(axis=0) + 1e-9
        cand = np.flatnonzero(
            self.ok
            & (self.hi[:, 0] >= lo[0])
            & (self.lo[:, 0] <= hi[0])
            & (self.hi[:, 1] >= lo[1])
            & (self.lo[:, 1] <= hi[1])
            & (self.tz_max + self.slack > pts[:, 2].min())
        )
        if len(cand) == 0:
            return out
        a = verts[self.tris[cand, 0]]
        b = verts[self.tris[cand, 1]]
        c = verts[self.tris[cand, 2]]
        d = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (
            a[:, 1] - c[:, 1]
        )
        step = max(1, 3_000_000 // len(cand))
        for s in range(0, len(idx), step):
            px = pts[s : s + step, 0][:, None]
            py = pts[s : s + step, 1][:, None]
            pz = pts[s : s + step, 2][:, None]
            l1 = (
                (b[:, 1] - c[:, 1]) * (px - c[:, 0])
                + (c[:, 0] - b[:, 0]) * (py - c[:, 1])
            ) / d
            l2 = (
                (c[:, 1] - a[:, 1]) * (px - c[:, 0])
                + (a[:, 0] - c[:, 0]) * (py - c[:, 1])
            ) / d
            l3 = 1.0 - l1 - l2
            inside = (l1 >= -1e-9) & (l2 >= -1e-9) & (l3 >= -1e-9)
            dz = l1 * a[:, 2] + l2 * b[:, 2] + l3 * c[:, 2] - pz
            out[s : s + step] = np.where(inside & (dz > 1e-9), dz, np.inf).min(axis=1)
        return out


@dataclass(frozen=True)
class LiftResult:
    """What :func:`lift_sharp_tails_checked` did."""

    vertices: np.ndarray
    n_lifted: int = 0  #: distinct vertices raised
    n_kept_thin: int = 0  #: plateaus left alone: too little material above
    n_kept_flip: int = 0  #: plateaus left alone: a face would flip/collapse
    max_lift: float = 0.0  #: largest vertex rise, mm
    min_clearance: float | None = None  #: thinnest material above a lifted vertex


def lift_sharp_tails_checked(
    vertices: np.ndarray,
    faces: np.ndarray,
    *,
    bed_z: float = 0.0,
    tol: float,
) -> LiftResult:
    """Raise every sub-resolution tail: a local z-minimum above the bed — one
    vertex strictly below all its neighbours, or a connected plateau of
    equal-z vertices (within 1e-9) all of whose outside neighbours are
    higher — whose lowest outside neighbour is less than ``tol`` above it,
    up to that neighbour's z. (Marching cubes leaves the plateau form too:
    a one-vertex dip beside a vertex at the next height is a two-vertex
    floor once the first is lifted.) Topology is untouched. Repeats until
    none remain (at most :data:`_MAX_LIFT_PASSES` passes).

    Guards, per plateau, so a lift can never damage the part:

    - **bed**: vertices within 1e-4 mm of ``bed_z`` are the bed — never
      lifted, never counted;
    - **cap**: no vertex ends more than ``tol`` above where it started (a
      staircase of small steps cannot be filled in);
    - **clearance**: the cumulative rise of each vertex must stay under half
      the material above it at its original height (a vertical ray to the
      first surface over it) — a thin slab is left alone;
    - **no flip, no collapse**: every incident face keeps its normal
      direction and at least 10% of its original area.

    A plateau failing a guard is left untouched and counted. ``tol`` is the
    resolution of the mesh's source (half a layer, or half the voxel pitch
    of a field-derived mesh): a dip shallower than that is a meshing
    artefact; a deeper one is a real overhang and stays a finding."""
    orig = np.asarray(vertices, dtype=np.float64)
    verts = orig.copy()
    z0 = orig[:, 2]
    tris = np.asarray(faces)
    n = len(verts)
    if tris.size == 0 or n == 0:
        return LiftResult(verts)
    a = np.concatenate([tris[:, 0], tris[:, 1], tris[:, 2]]).astype(np.int64)
    b = np.concatenate([tris[:, 1], tris[:, 2], tris[:, 0]]).astype(np.int64)
    ukey = np.unique(np.minimum(a, b) * n + np.maximum(a, b))
    ua, ub = ukey // n, ukey % n
    ea, eb = np.concatenate([ua, ub]), np.concatenate([ub, ua])
    area0 = 0.5 * np.linalg.norm(_face_normals(orig, tris), axis=1)
    flat = tris.ravel()
    order = np.argsort(flat, kind="stable")
    starts = np.searchsorted(flat[order], np.arange(n + 1))
    ceiling: _Ceiling | None = None
    blocked = np.zeros(n, dtype=bool)
    touched = np.zeros(n, dtype=bool)
    n_thin = n_flip = 0
    min_clear = np.inf
    for _ in range(_MAX_LIFT_PASSES):
        z = verts[:, 2]
        active = z > bed_z + _BED_SNAP
        same = (np.abs(z[ea] - z[eb]) <= 1e-9) & active[ea] & active[eb]
        label = _plateau_labels(ea, eb, n, same)
        out_edge = active[ea] & (label[ea] != label[eb])
        la, zb = label[ea[out_edge]], z[eb[out_edge]]
        za = z[ea[out_edge]]
        has_lower = np.zeros(n, dtype=bool)
        has_lower[la[zb < za]] = True
        out_min = np.full(n, np.inf)
        np.minimum.at(out_min, la, zb)
        # the biggest rise any member of the plateau would take from its
        # ORIGINAL height (a vertex lifted in an earlier pass counts that)
        rise = np.zeros(n)
        np.maximum.at(rise, label, out_min[label] - z0)
        gap = out_min[label] - z
        bad = np.zeros(n, dtype=bool)
        bad[label[blocked]] = True
        tail = (
            active
            & ~has_lower[label]
            & ~bad[label]
            & (gap > 1e-9)
            & (gap < tol)
            & (rise[label] <= tol + 1e-12)
        )
        cand = np.flatnonzero(tail)
        if len(cand) == 0:
            break
        lab = label[cand]
        o = np.argsort(lab, kind="stable")
        cand, lab = cand[o], lab[o]
        for g in np.split(cand, np.flatnonzero(np.diff(lab)) + 1):
            new_z = float(out_min[label[g[0]]])
            if ceiling is None:
                ceiling = _Ceiling(verts, tris, slack=tol + 1e-6)
            # material above each vertex at its ORIGINAL height
            clear = ceiling.above(g, verts) + (verts[g, 2] - z0[g])
            if not np.all(new_z - z0[g] < _CLEARANCE_FRACTION * clear):
                blocked[g] = True
                n_thin += 1
                continue
            faces_of = np.unique(
                np.concatenate([order[starts[v] : starts[v + 1]] // 3 for v in g])
            )
            before_n = _face_normals(verts, tris[faces_of])
            old_z = verts[g, 2].copy()
            verts[g, 2] = new_z
            after_n = _face_normals(verts, tris[faces_of])
            live = (np.linalg.norm(before_n, axis=1) > 1e-12) & (
                area0[faces_of] > 1e-12
            )
            flips = np.einsum("ij,ij->i", before_n, after_n) <= 0.0
            shrinks = (
                0.5 * np.linalg.norm(after_n, axis=1) < _KEEP_AREA * area0[faces_of]
            )
            if np.any(live & (flips | shrinks)):
                verts[g, 2] = old_z
                blocked[g] = True
                n_flip += 1
                continue
            touched[g] = True
            min_clear = min(min_clear, float(clear.min()))
    return LiftResult(
        vertices=verts,
        n_lifted=int(touched.sum()),
        n_kept_thin=n_thin,
        n_kept_flip=n_flip,
        max_lift=float(np.abs(verts[:, 2] - z0).max()),
        min_clearance=None if not np.isfinite(min_clear) else min_clear,
    )


def lift_sharp_tails(
    vertices: np.ndarray,
    faces: np.ndarray,
    *,
    bed_z: float = 0.0,
    tol: float,
) -> tuple[np.ndarray, int]:
    """:func:`lift_sharp_tails_checked` reduced to ``(vertices, n_lifted)``."""
    res = lift_sharp_tails_checked(vertices, faces, bed_z=bed_z, tol=tol)
    return res.vertices, res.n_lifted


def lift_rejection(
    before: np.ndarray, after: np.ndarray, faces: np.ndarray, *, max_dv: float = 1e-3
) -> str | None:
    """Global backstop for a lift: why ``after`` (``before`` with its tails
    raised, same ``faces``) must not ship, or ``None`` when it may. It must
    still be accepted by manifold3d wherever ``before`` was, and move the
    volume by at most ``max_dv`` (a fraction; default 0.1%)."""
    v0, v1 = signed_volume(before, faces), signed_volume(after, faces)
    if abs(v1 - v0) > max_dv * abs(v0):
        return (
            f"it changed the volume by {abs(v1 - v0):.4g} mm³ "
            f"({abs(v1 - v0) / abs(v0):.3%} of {abs(v0):.4g}, limit {max_dv:.1%})"
        )
    if (
        not _manifold_of(before, faces).is_empty()
        and _manifold_of(after, faces).is_empty()
    ):
        return "manifold3d no longer accepts the lifted mesh"
    return None


def weld_and_drop_degenerate(
    vertices: np.ndarray, faces: np.ndarray, *, eps: float = 1e-9
) -> tuple[np.ndarray, np.ndarray, int]:
    """Weld vertices coincident within ``eps`` (grid-quantised, first
    occurrence keeps its exact coordinates), drop triangles that now repeat a
    vertex index, and compact away vertices nothing references. Returns
    ``(vertices, faces, n_dropped)``. Collinear-but-distinct slivers are not
    touched — :func:`count_slivers` reports them."""
    verts = np.asarray(vertices, dtype=np.float64)
    tris = np.asarray(faces, dtype=np.int64)
    if len(verts) == 0 or tris.size == 0:
        return verts.copy(), tris.copy(), 0
    keys = np.round(verts / eps).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    inverse = np.asarray(inverse).reshape(-1)
    welded = verts[first]
    new_tris = inverse[tris]
    keep = (
        (new_tris[:, 0] != new_tris[:, 1])
        & (new_tris[:, 1] != new_tris[:, 2])
        & (new_tris[:, 2] != new_tris[:, 0])
    )
    n_dropped = int((~keep).sum())
    new_tris = new_tris[keep]
    used, compact = np.unique(new_tris, return_inverse=True)
    return welded[used], np.asarray(compact).reshape(-1, 3), n_dropped


def count_slivers(
    vertices: np.ndarray, faces: np.ndarray, *, area_tol: float = 1e-9
) -> int:
    """Triangles with distinct indices but (numerically) zero area, mm²."""
    verts = np.asarray(vertices, dtype=np.float64)
    tris = np.asarray(faces)
    if tris.size == 0:
        return 0
    p0, p1, p2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(p1 - p0, p2 - p0), axis=1)
    return int((area < area_tol).sum())


# ---------------------------------------------------------------------------
# slicer cantilever (Bambu Studio's "floating cantilever")
# ---------------------------------------------------------------------------

#: Distance (mm) from the supported footprint past which Studio calls a
#: region a cantilever worth warning about; and the nearer one it records.
CANTILEVER_WARN_MM = 6.0
_CANTILEVER_RECORD_MM = 3.0


@dataclass(frozen=True)
class Cantilever:
    """One overhanging region of a layer that the slicer sees as unsupported
    (see :func:`slicer_cantilevers`)."""

    z: float  #: layer print_z (top of the layer), mm
    bottom_z: float  #: bottom of the layer, mm
    x: float  #: centroid of the region, mm
    y: float
    area: float  #: mm²
    distance: float  #: farthest point from what supports it, mm
    kind: str  #: ``floating-contour`` | ``boundary-dist``
    #: ``True`` when tree support cannot reach it: the layer starts within
    #: 0.2 mm of the first layer's top (Studio keeps its support interface
    #: that far off the bed), so supports=on adds nothing there.
    unreachable: bool


def _to_shapely(cs: Any) -> Any:
    """One connected cross-section as a shapely polygon (outer ring minus
    its holes, even-odd)."""
    from shapely.geometry import Polygon

    shape: Any | None = None
    for ring in cs.to_polygons():
        pts = np.asarray(ring, dtype=np.float64)
        if len(pts) < 3:
            continue
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        shape = poly if shape is None else shape.symmetric_difference(poly)
    return shape


def slicer_cantilevers(
    vertices: np.ndarray,
    faces: np.ndarray,
    layer_height: float,
    *,
    first_layer: float = 0.2,
    line_width: float = 0.42,
    threshold_deg: float = 30.0,
    bed_z: float = 0.0,
) -> list[Cantilever]:
    """Studio's "floating cantilever" rule on the sliced mesh
    (``TreeSupport::detect_overhangs`` / ``PrintObject::is_support_necessary``,
    v02.08), mirrored with manifold3d cross-section booleans.

    Layers: a ``first_layer`` thick bottom layer, then ``layer_height`` ones;
    each is sliced at its mid-height. Per layer ``k >= 1``:

    - ``ext_k = slice_k`` opened by ``line_width / 2`` — a feature narrower
      than one extrusion line does not print as a wall and is dropped (this
      is how a 0.24 mm sliver of a rounded fin foot makes the layer above it
      start in mid-air);
    - ``over_k = ext_k`` minus ``ext_{k-1}`` grown by ``layer_height /
      tan(threshold + 1 deg)`` — what hangs out further than the slope
      allows;
    - a piece of ``over_k`` is a cantilever when one of its contours has no
      point within ``line_width`` of ``ext_{k-1}`` (distance = its farthest
      point from it), or when its farthest point is more than 3 mm from the
      part of it that lies within ``max(line_width - grow, 0) + 0.1`` of
      the grown footprint (distance = that).

    Returns every such region (largest distance first); the caller applies
    :data:`CANTILEVER_WARN_MM`. A layer with nothing under it at all is a
    floating island (:func:`floating_islands`), not reported here."""
    import math

    import shapely

    if not layer_height > 0.0:
        raise ValueError(f"layer_height must be positive, got {layer_height}")
    verts = np.asarray(vertices, dtype=np.float64)
    tris = np.asarray(faces)
    if tris.size == 0:
        return []
    solid = _manifold_of(verts, tris)
    if solid.is_empty():
        raise ValueError(
            f"mesh is not a manifold the slicer kernel accepts: {solid.status()}"
        )
    zmax = float(verts[:, 2].max()) - bed_z
    grow = layer_height / math.tan(math.radians(min(threshold_deg + 1.0, 89.0)))
    half = line_width / 2.0
    min_area = half * half  # narrower than half a line is noise
    out: list[Cantilever] = []
    lower: Any | None = None
    lower2: Any | None = None
    pz = first_layer
    bottom = 0.0
    k = 0
    while bottom < zmax:
        h = first_layer if k == 0 else layer_height
        sl = solid.slice(bed_z + pz - h / 2.0)
        ext: Any | None = None
        if not sl.is_empty():
            ext = sl ^ sl.offset(-half).offset(half)
            if ext.is_empty():
                ext = None
        if k >= 1 and ext is not None and lower is not None:
            grown = lower.offset(grow)
            pieces: list[Any] = []
            for comp in ext.decompose():
                # a region resting on neither of the two layers below is a
                # floating island (floating_islands reports it): not ALSO a
                # cantilever
                if not (_supported(comp, lower) or _supported(comp, lower2)):
                    continue
                for piece in (comp - grown).decompose():
                    x0, y0, x1, y1 = piece.bounds()
                    if (
                        float(piece.area()) >= min_area
                        and math.hypot(x1 - x0, y1 - y0) > _CANTILEVER_RECORD_MM
                    ):
                        pieces.append(piece)
            if pieces:
                out.extend(
                    _cantilever_pieces(
                        pieces,
                        _to_shapely_multi(lower.offset(line_width)),
                        grown,
                        max(line_width - grow, 0.0) + 0.1,
                        pz,
                        pz - h,
                        first_layer,
                        shapely,
                    )
                )
        lower2, lower = lower, ext
        bottom = pz
        pz += layer_height
        k += 1
    out.sort(key=lambda c: -c.distance)
    return out


#: Spacing (mm) contour edges are densified to before their distance to
#: the support is sampled — a long straight edge anchored at both ends is
#: far from it in the middle, which its two corners alone would hide.
_CONTOUR_STEP_MM = 0.5


def _ring_points(shapely: Any, ring: Any) -> Any:
    """Points along a ring at most :data:`_CONTOUR_STEP_MM` apart."""
    dense = shapely.segmentize(
        shapely.linestrings(np.asarray(ring.coords)), _CONTOUR_STEP_MM
    )
    return shapely.points(shapely.get_coordinates(dense)[:-1])


def _cantilever_pieces(
    pieces: list[Any],
    near_geo: Any | None,
    grown: Any,
    reach: float,
    pz: float,
    bottom_z: float,
    first_layer: float,
    shapely: Any,
) -> list[Cantilever]:
    """The cantilevers among ``pieces`` (parts of one layer that hang past
    the grown footprint ``grown``); ``near_geo`` is the layer below grown by
    a line width (shapely), ``reach`` the extra margin that still counts as
    anchored."""
    found: list[Cantilever] = []
    if near_geo is None or near_geo.is_empty:
        return found  # nothing under it at all: an island, not a cantilever
    for piece in pieces:
        geom = _to_shapely(piece)
        if geom is None or geom.is_empty:
            continue
        polys = [g for g in getattr(geom, "geoms", [geom]) if not g.is_empty]
        rings = [r for g in polys for r in (g.exterior, *g.interiors)]
        dist = 0.0
        kind = ""
        for ring in rings:
            d = shapely.distance(_ring_points(shapely, ring), near_geo)
            if float(d.min()) > 1e-9:  # not one point within a line width
                dist, kind = float(d.max()), "floating-contour"
                break
        if not kind:
            cb = piece ^ grown.offset(reach)
            if cb.is_empty():
                continue
            bound = _to_shapely_multi(cb).boundary
            dist = max(
                float(shapely.distance(_ring_points(shapely, g.exterior), bound).max())
                for g in polys
            )
            if dist <= _CANTILEVER_RECORD_MM:
                continue
            kind = "boundary-dist"
        cx, cy = _centroid(piece)
        found.append(
            Cantilever(
                z=pz,
                bottom_z=bottom_z,
                x=cx,
                y=cy,
                area=float(piece.area()),
                distance=dist,
                kind=kind,
                unreachable=bottom_z - 0.2 < first_layer - 1e-9,
            )
        )
    return found


def _to_shapely_multi(cs: Any) -> Any:
    """A cross-section with several outer rings as one shapely geometry,
    ``None`` when it is empty."""
    from shapely.ops import unary_union

    if cs.is_empty():
        return None
    parts = [_to_shapely(c) for c in cs.decompose()]
    return unary_union([p for p in parts if p is not None])


# ---------------------------------------------------------------------------
# 3MF package validation
# ---------------------------------------------------------------------------


def _mesh_findings(label: str, verts: np.ndarray, tris: np.ndarray) -> list[str]:
    out: list[str] = []
    if len(verts) < 4:
        out.append(f"{label}: {len(verts)} vertices (a solid needs at least 4)")
    if len(tris) < 4:
        out.append(f"{label}: {len(tris)} triangles (a solid needs at least 4)")
    if len(tris) == 0:
        return out
    if tris.min() < 0 or tris.max() >= len(verts):
        out.append(
            f"{label}: triangle index out of range "
            f"(0..{len(verts) - 1} vertices, saw {int(tris.min())}..{int(tris.max())})"
        )
        return out
    n = len(verts)
    directed = np.concatenate(
        [
            tris[:, 0] * n + tris[:, 1],
            tris[:, 1] * n + tris[:, 2],
            tris[:, 2] * n + tris[:, 0],
        ]
    )
    a = np.concatenate([tris[:, 0], tris[:, 1], tris[:, 2]])
    b = np.concatenate([tris[:, 1], tris[:, 2], tris[:, 0]])
    undirected = np.minimum(a, b) * n + np.maximum(a, b)
    _, ucount = np.unique(undirected, return_counts=True)
    bad_edges = int((ucount != 2).sum())
    if bad_edges:
        out.append(
            f"{label}: mesh is not closed — {bad_edges} edge(s) not shared by "
            "exactly two triangles"
        )
    _, dcount = np.unique(directed, return_counts=True)
    same_dir = int((dcount > 1).sum())
    if same_dir and not bad_edges:
        out.append(
            f"{label}: inconsistent winding — {same_dir} edge(s) traversed twice "
            "in the same direction"
        )
    elif same_dir:
        out.append(
            f"{label}: inconsistent winding or non-manifold edges "
            f"({same_dir} directed edge(s) used more than once)"
        )
    if not bad_edges and not same_dir:
        p0, p1, p2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
        vol = float(np.einsum("ij,ij->i", p0, np.cross(p1, p2)).sum() / 6.0)
        if not vol > 0.0:
            out.append(f"{label}: signed volume {vol:.6g} mm³ is not positive")
    return out


def package_findings(data: bytes) -> list[str]:
    """Problems found in a 3MF package (``data`` = the zip bytes), checked
    against the core-spec essentials; an empty list means valid. The XML is
    our own writer's output or a file the user is about to slice, parsed with
    the stdlib (no external entities are resolved)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ["not a zip package"]
    out: list[str] = []
    names = set(zf.namelist())
    for member in ("[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"):
        if member not in names:
            out.append(f"missing required package member {member}")
    if "[Content_Types].xml" in names:
        out.extend(_content_type_findings(zf.read("[Content_Types].xml")))
    if "_rels/.rels" in names:
        out.extend(_rels_findings(zf.read("_rels/.rels")))
    if "3D/3dmodel.model" in names:
        out.extend(_model_findings(zf.read("3D/3dmodel.model")))
    return out


def _parse(xml: bytes, label: str) -> tuple[ET.Element | None, list[str]]:
    try:
        return ET.fromstring(xml), []
    except ET.ParseError as exc:
        return None, [f"{label} is not well-formed XML: {exc}"]


def _content_type_findings(xml: bytes) -> list[str]:
    root, out = _parse(xml, "[Content_Types].xml")
    if root is None:
        return out
    defaults = {
        el.get("Extension", "").lower(): el.get("ContentType")
        for el in root
        if el.tag.endswith("}Default") or el.tag == "Default"
    }
    overrides = {
        el.get("PartName"): el.get("ContentType")
        for el in root
        if el.tag.endswith("}Override") or el.tag == "Override"
    }
    if defaults.get("rels") != _RELS_CT:
        out.append("[Content_Types].xml has no relationships content type for .rels")
    if (
        defaults.get("model") != _MODEL_CT
        and overrides.get("/3D/3dmodel.model") != _MODEL_CT
    ):
        out.append(
            "[Content_Types].xml has no 3D model content type for the model part"
        )
    return out


def _rels_findings(xml: bytes) -> list[str]:
    root, out = _parse(xml, "_rels/.rels")
    if root is None:
        return out
    ok = any(
        el.get("Target") == "/3D/3dmodel.model" and el.get("Type") == _MODEL_REL_TYPE
        for el in root
    )
    if not ok:
        out.append(
            "_rels/.rels has no 3D model relationship targeting /3D/3dmodel.model"
        )
    return out


def _model_findings(xml: bytes) -> list[str]:
    root, out = _parse(xml, "3D/3dmodel.model")
    if root is None:
        return out
    ns = f"{{{_CORE_NS}}}"
    if root.tag != f"{ns}model":
        return [
            *out,
            f"3dmodel.model root is {root.tag!r}, not a core-namespace <model>",
        ]
    if not root.get("unit"):
        out.append("<model> has no unit attribute")
    objects: dict[str, ET.Element] = {}
    resources = root.find(f"{ns}resources")
    for obj in resources.findall(f"{ns}object") if resources is not None else []:
        oid = obj.get("id", "")
        if oid in objects:
            out.append(f"duplicate object id {oid!r}")
        objects[oid] = obj
    if not objects:
        out.append("model has no objects")
    build = root.find(f"{ns}build")
    items = build.findall(f"{ns}item") if build is not None else []
    if not items:
        out.append("model has no build items")
    for item in items:
        ref = item.get("objectid", "")
        target = objects.get(ref)
        if target is None:
            out.append(f"build item references missing object {ref!r}")
        elif target.get("type", "model") != "model":
            out.append(
                f"build item references object {ref!r} of type "
                f"{target.get('type')!r}, not 'model'"
            )
    for oid, obj in objects.items():
        label = f"object {oid}" + (f" ({obj.get('name')})" if obj.get("name") else "")
        mesh = obj.find(f"{ns}mesh")
        if mesh is None:
            if obj.find(f"{ns}components") is None:
                out.append(f"{label}: has neither a mesh nor components")
            continue
        vs = mesh.find(f"{ns}vertices")
        ts = mesh.find(f"{ns}triangles")
        if vs is None or ts is None:
            out.append(f"{label}: mesh lacks <vertices> or <triangles>")
            continue
        try:
            verts = np.array(
                [(v.get("x"), v.get("y"), v.get("z")) for v in vs], dtype=np.float64
            ).reshape(-1, 3)
            tris = np.array(
                [(t.get("v1"), t.get("v2"), t.get("v3")) for t in ts], dtype=np.int64
            ).reshape(-1, 3)
        except (TypeError, ValueError):
            out.append(f"{label}: non-numeric vertex or triangle attribute")
            continue
        out.extend(_mesh_findings(label, verts, tris))
    return out

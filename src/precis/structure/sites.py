"""Pure site enumeration and taxonomy over a posed atom cloud and its
scene — no DB, no handlers, so it ships in the ``precis-geom`` wheel.

Moved out of ``precis_se.atomic.sites`` (molecular-workspace-code slice
2), which keeps the ``view='sites'`` rendering and calls this. Every
carbon that can take a substituent is classified three ways:

- *ring taxonomy* — ``sp3`` (four-or-more-coordinated on single bonds, or
  generator-tagged: a graft junction, no free valence), ``substituted``
  (already carries a non-C non-H group), ``open-edge`` (under three
  carbon neighbours: a rim dangling bond), else the C–C rings (5–8) the
  atom sits on, joined ``+`` (``5+7-ring``), ``6-ring`` for a pure
  hexagon, ``non-ring`` past that;
- *face* — which side of the local surface a substituent would grow on,
  from the free direction against an axis (``out`` = away from it,
  ``in`` = toward it, ``axial`` = along it);
- *steric exposure* — the free fraction of a probe sphere one radius out
  along the free direction (deterministic Fibonacci lattice).

The cloud and the axis are structural (:class:`Cloud`, :class:`Axis`):
``precis_se.atomic.clearance.AtomCloud`` / ``SweepAxis`` satisfy them
unchanged, and ``precis_geom.mol.sites`` builds its own from a ``Geom``.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from precis.structure import Scene as StructScene


class Cloud(Protocol):
    """What the enumeration reads of a posed atom cloud."""

    name: str
    labels: list[str]
    elements: list[str]
    pts_A: NDArray[np.float64]


class Axis(Protocol):
    """A world-frame axis line: ``point_A`` on it, unit ``dir_A``."""

    point_A: NDArray[np.float64]
    dir_A: NDArray[np.float64]


def axis_basis(
    axis_dir: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """An orthonormal ``(v1, v2)`` spanning the plane normal to the axis —
    the θ=0 reference and its quarter-turn, deterministic off a fixed
    world axis (never the data, so the same pocket reports the same θ)."""
    ref = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(axis_dir, ref))) > 0.9:
        ref = np.array([1.0, 0.0, 0.0])
    v1 = ref - float(np.dot(ref, axis_dir)) * axis_dir
    v1 /= np.linalg.norm(v1)
    return v1, np.cross(axis_dir, v1)


#: Carbon–carbon bond cutoff used only when a bound structure carries no
#: declared bonds at all (generated scenes always do).
_CC_DERIVE_BOND_A = 1.9

#: A bond under this order counts as single for the sp³ test — aromatic
#: (~1.33) and double bonds do not, so an H-capped aromatic carbon with
#: four neighbours stays a ring site rather than reading as sp³.
_SINGLE_BOND_ORDER = 1.2

#: Ring search depth on the C–C graph — covers 5–8 membered rings;
#: anything longer reads as ``non-ring``.
_RING_MAX_DEPTH = 4

#: Ring sizes the taxonomy names — a carbon closing only 3/4-rings or
#: cycles past 8 reads ``non-ring``.
_RING_SIZES = frozenset({5, 6, 7, 8})

#: Free-direction vs axis-radial cosine thresholds for the face
#: classification: beyond ±0.3 the site faces clearly in or out; a
#: dangling bond along the tube axis reads ``axial``.
_FACE_COS = 0.3

#: A probe point inside the exposure sphere counts as free when no atom
#: sits this close — a ~vdW contact distance, not the full probe radius.
_EXPOSURE_CLEAR_A = 1.0

#: Deterministic sampling of the exposure sphere: Fibonacci directions ×
#: radial shells — reproducible, no RNG.
_EXPOSURE_DIRS = 40
_EXPOSURE_SHELLS = 5

#: Classes that ARE sites — a carbon can take a substituent; ``sp3`` and
#: ``substituted`` are counted in the summary but never listed. Compound
#: defect classes (``5+7-ring`` — an atom two defect rings share) are
#: site classes too but are not nameable ``ring:`` selectors: each
#: named size selects them by membership, so ``ring:5-ring`` and
#: ``ring:7-ring`` both reach a ``5+7-ring`` atom.
SITE_CLASSES = ("5-ring", "6-ring", "7-ring", "8-ring", "open-edge", "non-ring")
EXCLUDED_CLASSES = ("sp3", "substituted")


def _fib_dirs(n: int) -> NDArray[np.float64]:
    """``n`` Fibonacci-spiral unit directions — a deterministic, even
    sphere covering."""
    i = np.arange(n, dtype=float) + 0.5
    phi = np.pi * (1.0 + 5.0**0.5) * i
    z = 1.0 - 2.0 * i / n
    r = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    return np.stack([r * np.cos(phi), r * np.sin(phi), z], axis=1)


def _probe_lattice() -> NDArray[np.float64]:
    """Unit-sphere interior lattice: Fibonacci directions × shell radii
    (cube-root spacing for even volume fill), reused by every exposure
    probe."""
    shells = (np.arange(_EXPOSURE_SHELLS, dtype=float) + 0.5) / _EXPOSURE_SHELLS
    radii = shells ** (1.0 / 3.0)
    return (
        np.repeat(_fib_dirs(_EXPOSURE_DIRS), _EXPOSURE_SHELLS, axis=0)
        * np.tile(radii, _EXPOSURE_DIRS)[:, None]
    )


_LATTICE = _probe_lattice()


def bond_graph(
    scene: StructScene, labels: list[str], pts_A: NDArray[np.float64]
) -> tuple[list[list[int]], list[bool]]:
    """``(adjacency, unsaturated)`` over the cloud's atom indices —
    ALL-element declared scene bonds when present (hexfold scenes always
    carry them), a C–C distance derivation as the fallback.
    ``unsaturated[i]`` is set when any bond at ``i`` exceeds
    :data:`_SINGLE_BOND_ORDER` — the sp³ test needs it so an aromatic C–H
    (three aromatic C bonds + one H) doesn't read as four-coordinated."""
    index = {label: i for i, label in enumerate(labels)}
    adj: list[set[int]] = [set() for _ in labels]
    unsat = [False] * len(labels)
    n_bonds = 0
    for bond in scene.bonds:
        i, j = index.get(bond.i), index.get(bond.j)
        if i is None or j is None:
            continue
        adj[i].add(j)
        adj[j].add(i)
        if bond.order >= _SINGLE_BOND_ORDER:
            unsat[i] = unsat[j] = True
        n_bonds += 1
    if n_bonds == 0:
        from scipy.spatial import cKDTree

        carbon = [
            i for i, label in enumerate(labels) if scene.atoms[label].element == "C"
        ]
        pts = pts_A[carbon]
        for a, b in cKDTree(pts).query_pairs(_CC_DERIVE_BOND_A):
            adj[carbon[a]].add(carbon[b])
            adj[carbon[b]].add(carbon[a])
    return [sorted(s) for s in adj], unsat


def hyb_of(scene: StructScene, cloud: Cloud) -> list[str | None]:
    """The generator-authored hybridization tag per cloud atom
    (``sp2``/``sp3`` straight from hexfold's net), or ``None``."""
    return [scene.atoms[label].hybridization for label in cloud.labels]


def c_adjacency(adj: list[list[int]], elements: list[str]) -> list[list[int]]:
    """The C–C subgraph of a full bond graph — the ring-search graph."""
    return [[j for j in nbrs if elements[j] == "C"] for nbrs in adj]


def _ring_sizes(adj: list[list[int]], root: int) -> set[int]:
    """Every small-cycle size through ``root`` on the C–C graph — BFS to
    :data:`_RING_MAX_DEPTH`, tracking which root-neighbour subtree each
    visited node belongs to: an edge joining different subtrees closes a
    simple cycle through root of ``depth[u] + depth[v] + 1``. All closure
    sizes count, not just the smallest: a heptagon's carbon that also
    sits on hexagons must still read as a 7-ring site."""
    subtree: dict[int, int] = {}
    depth: dict[int, int] = {}
    queue: deque[int] = deque()
    sizes: set[int] = set()
    for nb in adj[root]:
        if nb in subtree:
            sizes.add(3)  # two root-neighbours bonded to each other
            continue
        subtree[nb] = nb
        depth[nb] = 1
        queue.append(nb)
    while queue:
        u = queue.popleft()
        for v in adj[u]:
            if v == root:
                continue
            if v not in subtree:
                subtree[v] = subtree[u]
                depth[v] = depth[u] + 1
                if depth[v] <= _RING_MAX_DEPTH:
                    queue.append(v)
            elif subtree[v] != subtree[u]:
                sizes.add(depth[u] + depth[v] + 1)
    return sizes


def _ring_label(sizes: set[int]) -> str:
    """The site's ring name: its defect (non-6) ring sizes joined ``+``
    (``5+7-ring`` for a vertex two defect rings share), ``6-ring`` for a
    pure-hexagon carbon, ``non-ring`` when it closes no named ring. The
    hexagon membership of a defect-ring atom is implicit — ``6-ring`` is
    reserved for carbons that close ONLY hexagons."""
    defects = sorted(sizes - {6})
    if defects:
        return "+".join(str(s) for s in defects) + "-ring"
    return "6-ring" if 6 in sizes else "non-ring"


def ring_classes(
    cloud: Cloud,
    adj: list[list[int]],
    unsat: list[bool],
    hyb: list[str | None] | None = None,
) -> list[str | None]:
    """Per-atom ring taxonomy parallel to ``cloud`` — ``None`` for
    non-carbons (never site candidates), ``substituted`` for a carbon
    carrying a non-C non-H group, ``sp3`` for a four-or-more-coordinated
    all-single-bond carbon (or one the generator tagged ``sp3`` — a graft
    junction has no free valence), ``open-edge`` under three C
    neighbours, else the C–C rings (5–8) the atom sits on
    (:func:`_ring_label`) or ``non-ring``."""
    cadj = c_adjacency(adj, cloud.elements)
    out: list[str | None] = []
    for i, el in enumerate(cloud.elements):
        if el != "C":
            out.append(None)
            continue
        if any(cloud.elements[j] not in ("C", "H") for j in adj[i]):
            out.append("substituted")
            continue
        if (hyb is not None and hyb[i] == "sp3") or (len(adj[i]) >= 4 and not unsat[i]):
            out.append("sp3")
            continue
        if len(cadj[i]) < 3:
            out.append("open-edge")
            continue
        out.append(_ring_label(_ring_sizes(cadj, i) & _RING_SIZES))
    return out


def free_dirs(
    i: int,
    pts: NDArray[np.float64],
    cadj: list[list[int]],
    adj: list[list[int]],
) -> list[NDArray[np.float64]]:
    """The directions a substituent could grow from atom ``i`` — the
    dangling-bond direction for open-edge carbons (away from the C
    neighbours' centroid, the all-neighbour centroid for a carbon with
    no C neighbours at all), both sides of the neighbour plane's normal
    for a saturated sp² wall carbon."""
    p = pts[i]
    if len(cadj[i]) < 3:
        ref = cadj[i] or adj[i]
        if not ref:
            return []
        d = p - pts[ref].mean(axis=0)
        n = float(np.linalg.norm(d))
        return [d / n] if n > 1e-9 else []
    centred = pts[cadj[i]] - p
    _u, _s, vt = np.linalg.svd(centred)
    normal = vt[-1]
    return [normal, -normal]


def exposure(
    atom_pos: NDArray[np.float64],
    free_dir: NDArray[np.float64],
    occluder_tree: Any,
    exposure_A: float,
) -> float:
    """Free fraction of the probe sphere centred ``exposure_A`` out along
    ``free_dir`` — the deterministic lattice's points whose nearest
    occluding atom is at least :data:`_EXPOSURE_CLEAR_A` away."""
    centre = atom_pos + free_dir * exposure_A
    dists, _ = occluder_tree.query(centre + _LATTICE * exposure_A)
    return float((dists >= _EXPOSURE_CLEAR_A).mean())


def cyl(p: NDArray[np.float64], axis: Axis) -> tuple[float, float, float]:
    """``(z, θ°, r)`` about the axis — z from the anchor along the axis,
    θ off the fixed world basis, r the axis distance."""
    v1, v2 = axis_basis(axis.dir_A)
    rel = p - axis.point_A
    z = float(np.dot(rel, axis.dir_A))
    theta = float(
        np.degrees(math.atan2(float(np.dot(rel, v2)), float(np.dot(rel, v1)))) % 360.0
    )
    r = float(np.linalg.norm(rel - z * axis.dir_A))
    return z, theta, r


def face_of(free_dir: NDArray[np.float64], radial: NDArray[np.float64]) -> str:
    norm = float(np.linalg.norm(radial))
    if norm < 1e-9:
        return "axial"
    cos = float(np.dot(free_dir, radial / norm))
    if cos > _FACE_COS:
        return "out"
    if cos < -_FACE_COS:
        return "in"
    return "axial"


@dataclass
class Site:
    """One (atom, face) site row: the carbon's cloud ordinal and label,
    ring class, face vs the envelope axis (``n/a`` when the block has no
    axis-bearing envelope), free direction, exposure, and cylindrical
    coordinates (``None`` without an axis)."""

    block: str
    ordinal: int
    label: str
    ring_class: str
    face: str
    normal: NDArray[np.float64]
    exposure: float
    z_A: float | None
    theta_deg: float | None
    r_A: float | None


def enumerate_sites(
    cloud: Cloud,
    classes: list[str | None],
    adj: list[list[int]],
    axis: Axis | None,
    exposure_A: float,
    occluder_pts: NDArray[np.float64] | None = None,
) -> list[Site]:
    """Every (carbon, free-face) site on ``cloud`` — only the real site
    classes (``sp3``/``substituted`` are excluded here and counted in the
    summary); the probe sphere sees the block's own atoms plus
    ``occluder_pts`` (the partner block, in the pair view). ``axis`` may
    be ``None`` — ring class, ``n/a`` face and exposure still render."""
    from scipy.spatial import cKDTree

    cadj = c_adjacency(adj, cloud.elements)
    occl = (
        np.concatenate([cloud.pts_A, occluder_pts])
        if occluder_pts is not None and len(occluder_pts)
        else cloud.pts_A
    )
    occluder_tree = cKDTree(occl)
    centroid = cloud.pts_A.mean(axis=0)
    sites: list[Site] = []
    for i, ring in enumerate(classes):
        if ring is None or ring in EXCLUDED_CLASSES:
            continue
        if axis is not None:
            rel = cloud.pts_A[i] - axis.point_A
            radial = rel - float(np.dot(rel, axis.dir_A)) * axis.dir_A
        for free_dir in free_dirs(i, cloud.pts_A, cadj, adj):
            face = face_of(free_dir, radial) if axis is not None else "n/a"
            if face == "axial" and len(cadj[i]) >= 3:
                # A wall normal along the axis — a cap carbon near the
                # pole. Its two faces are still a surface's two sides:
                # out is away from the block's centroid.
                face = (
                    "out"
                    if float(np.dot(free_dir, cloud.pts_A[i] - centroid)) > 0
                    else "in"
                )
            exp = exposure(cloud.pts_A[i], free_dir, occluder_tree, exposure_A)
            z, theta, r = (
                cyl(cloud.pts_A[i], axis)
                if axis is not None
                else (
                    None,
                    None,
                    None,
                )
            )
            sites.append(
                Site(
                    block=cloud.name,
                    ordinal=i,
                    label=cloud.labels[i],
                    ring_class=ring,
                    face=face,
                    normal=free_dir,
                    exposure=exp,
                    z_A=z,
                    theta_deg=theta,
                    r_A=r,
                )
            )
    sites.sort(key=lambda s: (-s.exposure, s.block, s.ordinal, s.face))
    return sites


def is_closed_cage(cloud: Cloud, classes: list[str | None]) -> bool:
    """A carbon net with no open edge — every carbon closes rings on all
    sides, so the surface is sealed and its ``in`` face is an interior no
    reagent reaches (a pillbox, a fullerene)."""
    carbons = [c for c in classes if c is not None]
    return bool(carbons) and "open-edge" not in carbons


def sites_block(
    cloud: Cloud,
    scene: StructScene,
    axis: Axis | None,
    exposure_A: float,
    occl: Any = None,
) -> tuple[list[Site], list[str | None]]:
    """``(sites, classes)`` — the bond graph is built once here and feeds
    the classifier AND the free-direction search (finding 5: no double
    walk of the scene's bonds). A closed cage's ``in`` sites read
    exposure 0: the probe sphere fits inside the cavity, but the cavity is
    sealed (:func:`is_closed_cage`)."""
    adj, unsat = bond_graph(scene, cloud.labels, cloud.pts_A)
    classes = ring_classes(cloud, adj, unsat, hyb_of(scene, cloud))
    sites = enumerate_sites(cloud, classes, adj, axis, exposure_A, occluder_pts=occl)
    if is_closed_cage(cloud, classes):
        for s in sites:
            if s.face == "in":
                s.exposure = 0.0
        sites.sort(key=lambda s: (-s.exposure, s.block, s.ordinal, s.face))
    return sites, classes

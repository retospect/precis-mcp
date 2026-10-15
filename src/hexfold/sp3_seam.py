"""Private analytic sp3 seam lines on the unrelaxed graphene metric.

Like :mod:`hexfold.y_junction` and :mod:`hexfold.fin`, a deterministic
analytic construction — finite sheets, minted seam atoms, no public ``.hx``
grammar, no cyclic Port.  Two rows of
``docs/backlog/hexfold-seam-type-catalogue.md``:

- ``k4-sp3-90-a`` — four armchair-rimmed sheets at 0/90/180/270 degrees
  around a straight seam line, one degree-four carbon per armchair period
  (``3*sigma``) bonded to one dangling atom of each sheet.  The four bonds
  alternate axial tilt, so each seam atom reads the catalogue's squashed
  tetrahedron: 104.48 x4 + 120 x2 degrees.
- ``k3-sp3-z`` — three zigzag-rimmed sheets at equal 120 degrees, and the
  fourth valence of each seam atom carried by a seam-chain bond between
  dimer pairs.  Seed lengths are the GFN2-xTB-relaxed values measured on
  the equal-120 sp2 Y and on the seam tube (tblite FIRE, 2026-10-13 —
  both seams dimerise into exactly this motif): chain bond 1.60 A,
  sheet bonds 1.51 A, inter-dimer gap 3.32 A.  Each seam atom then reads
  106.54 x3 + 112.24 x3 — as tetrahedral as the zigzag rims allow at
  those lengths — and each bonded rim atom reads 103.46/120/136.54 deg.
  The public pass pins seam atoms and their neighbours, so those tables
  are the held seed registration, not relaxed evidence.
- ``k3-sp3-a`` — three armchair-rimmed sheets at equal 120 degrees, two
  seam atoms per armchair period: ``a_i`` bonds the ``dangling_a`` atom
  of all three sheets, ``b_i`` the ``dangling_b`` atom of all three, and
  the ``sigma``-spaced ``(b_i, a_{i+1})`` cross-period pairs carry the
  chain bonds, so every rim atom is consumed and every interior seam
  atom is degree four.  Seed geometry is the 2026-10-14 GFN2-xTB model
  (sheet bonds 1.52 A, chain bonds 1.55 A): the seam atoms sit on the
  axis at 103.3 x3 + 115.1 x3 degrees at the authored registration; the
  terminal ``a_0``/``b_{n-1}`` stay degree-three boundary atoms and take
  the H cap.

``compose_sp3`` follows ``join.compose_k3``'s admission rules: integer
ordinals in range, closed faces, equal dangling counts, and typed
refusals — wrong rim class per row is ``port.mismatch``, unsupported
multiplicity/dihedrals ``fit.unsolvable``.  Both rows report degree-four
carbon seam atoms recorded ``sp3``, the six bond angles at each, a
per-sheet Euler residual of 0, and the seam ring census derived from the
built graph.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

import numpy as np

from .build import Atom, Net, SeamRecord
from .ids import AtomPath
from .join import Block, _adjacency, _segment_frame, _ZigzagSegment
from .lattice import Lattice, Site
from .report import Finding, Report, Severity
from .text import Spec
from .y_junction import _sheet


@dataclass(frozen=True)
class _ArmchairSegment:
    """Private straight open armchair rim: a DDSS-...-DDSS boundary walk.

    Each armchair period contributes a degree-2 dimer pair:
    ``dangling_a`` atoms whose missing bond tilts -x along the rim and
    ``dangling_b`` tilting +x; the intervening S sites have degree 3.
    Neither a cyclic Port nor a new authored port/rail grammar — the same
    private status as :class:`join._ZigzagSegment`.
    """

    name: str
    atoms: tuple[int, ...]
    dangling_a: tuple[int, ...]
    dangling_b: tuple[int, ...]
    endpoints: tuple[int, int]


@dataclass
class _Sp3Composite:
    """Local seam result, not a port-bearing/SE composite or a relaxed
    structure."""

    elements: tuple[str, ...]
    coords: np.ndarray
    bonds: tuple[tuple[int, int, int], ...]
    rings: tuple[tuple[int, ...], ...]
    hybridisation: tuple[str, ...]
    seam_atoms: tuple[int, ...]
    transforms: tuple[tuple[np.ndarray, np.ndarray], ...]
    sheets: tuple[tuple[str, tuple[int, ...]], ...]
    boundaries: tuple[tuple[int, ...], ...]
    findings: list[Finding]


def _sp3_refusal(code: str, message: str, **requested: Any) -> _Sp3Composite:
    return _Sp3Composite(
        (),
        np.empty((0, 3)),
        (),
        (),
        (),
        (),
        (),
        (),
        (),
        [
            Finding(
                code,
                Severity.ERROR,
                message,
                data=tuple(requested.items()),
                fix=(
                    "use the row's rim class and equal dihedrals: "
                    "armchair x4 at 90 deg for k4-sp3-90-a, zigzag x3 at "
                    "120 deg with an even period count for k3-sp3-z"
                ),
            )
        ],
    )


def _armchair_sheet(periods: int, levels: int) -> tuple[Block, _ArmchairSegment]:
    """Finite sheet with a straight armchair rim along +x, open elsewhere.

    Sites ``(k, i, s)``: level ``k`` in ``[0, levels)``, dimer cell ``i``
    in ``[-1, periods]`` (guard columns close the ends like
    ``y_junction._sheet``), sublattice ``s``.  Cell ``i`` on level ``k``
    sits at ``x = 3*sigma*i + 1.5*sigma*(k%2) + sigma*s``,
    ``y = k * sigma*sqrt(3)/2`` — the bottom edge is an armchair rim of
    period ``3*sigma`` whose per-cell dangling pair is ``(0,i,0)`` (tilts
    -x) and ``(0,i,1)`` (tilts +x).  Degree-1 stubs at the open ends are
    pruned iteratively until every surviving atom has at least two bonds.
    """
    sigma = Lattice().sigma_A
    cells = range(-1, periods + 1)
    grid = {(k, i, s) for k in range(levels) for i in cells for s in (0, 1)}

    def site_bonds(
        keep: set[tuple[int, int, int]],
    ) -> set[frozenset[tuple[int, int, int]]]:
        edges: set[frozenset[tuple[int, int, int]]] = set()
        for k, i, s in keep:
            if s != 0:
                continue
            shift = 1 if k % 2 == 0 else 0
            for q in ((k, i, 1), (k + 1, i - shift, 1), (k - 1, i - shift, 1)):
                if q in keep:
                    edges.add(frozenset(((k, i, 0), q)))
        return edges

    # Iteratively drop degree<2 stubs until the open ends are clean
    # boundaries (the guard columns would otherwise leave a degree-1
    # corner atom per level, and their termination caps overlap).
    alive = set(grid)
    while True:
        edges = site_bonds(alive)
        deg = {s: 0 for s in alive}
        for e in edges:
            a, b = sorted(e)
            deg[a] += 1
            deg[b] += 1
        dead = {s for s in alive if deg[s] < 2}
        if not dead:
            break
        alive -= dead
    sites = sorted(alive)
    ordinal = {site: n for n, site in enumerate(sites)}
    coords = np.asarray(
        [
            (
                3 * sigma * i + 1.5 * sigma * (k % 2) + sigma * s,
                k * sigma * math.sqrt(3) / 2,
                0.0,
            )
            for k, i, s in sites
        ]
    )
    bonds = tuple(
        (ordinal[a], ordinal[b], 1)
        for a, b in sorted(tuple(e) for e in site_bonds(alive))
    )
    adj = _adjacency(bonds)
    rings = tuple(_six_rings(adj))
    # The rim walk runs along the bottom boundary: per cell
    # A_i - B_i - S_i - W_i - A_{i+1}, minus pruned guard corners.
    rim_sites = [
        site
        for i in cells
        for site in ((0, i, 0), (0, i, 1), (1, i, 0), (1, i, 1))
        if site in alive
    ]
    walk = [ordinal[site] for site in rim_sites]
    segment = _ArmchairSegment(
        "rim",
        tuple(walk),
        tuple(ordinal[0, i, 0] for i in range(periods)),
        tuple(ordinal[0, i, 1] for i in range(periods)),
        (walk[0], walk[-1]),
    )
    return Block(("C",) * len(sites), coords, bonds, rings, {}, sigma), segment


def _six_rings(adj: dict[int, list[int]]) -> list[tuple[int, ...]]:
    """All simple 6-cycles of a bipartite bond graph, each once.

    In a honeycomb graph every simple 6-cycle is a hexagon face: a chord
    would split it into odd cycles.  Canonical orientation: the smallest
    vertex first, second vertex below the last.
    """
    out: list[tuple[int, ...]] = []
    nbrs = {a: sorted(b) for a, b in adj.items()}
    for s in sorted(nbrs):
        stack: list[tuple[int, tuple[int, ...]]] = [(s, (s,))]
        while stack:
            v, path = stack.pop()
            if len(path) == 6:
                if s in nbrs.get(v, ()) and path[1] < path[5]:
                    out.append(path)
                continue
            for w in nbrs.get(v, ()):
                if w > s and w not in path:
                    stack.append((w, path + (w,)))
    return sorted(out)


def _boundary_atoms(
    bonds: tuple[tuple[int, int, int], ...],
    rings: tuple[tuple[int, ...], ...],
) -> tuple[int, ...]:
    """Atoms on the open boundary of a sheet subgraph: endpoints of bonds
    that fewer than two of the sheet's rings use."""
    used: dict[frozenset[int], int] = {}
    for ring in rings:
        for a, b in zip(ring, ring[1:] + ring[:1]):
            key = frozenset((a, b))
            used[key] = used.get(key, 0) + 1
    out: set[int] = set()
    for a, b, _k in bonds:
        if used.get(frozenset((a, b)), 0) < 2:
            out.add(a)
            out.add(b)
    return tuple(sorted(out))


def _armchair_frame(block: Block, rim: _ArmchairSegment) -> np.ndarray | None:
    """Validate the DDSS walk and planar outward half-sheet, mirroring
    :func:`join._segment_frame`."""
    n = len(rim.dangling_a)
    walk = rim.atoms
    if (
        n < 2
        or len(rim.dangling_b) != n
        or len(walk) < 4 * n
        or len(set(walk)) != len(walk)
        or rim.endpoints != (walk[0], walk[-1])
        or any(type(i) is not int or not 0 <= i < len(block.elements) for i in walk)
    ):
        return None
    walk_pos = {a: p for p, a in enumerate(walk)}
    if any(d not in walk_pos for d in rim.dangling_a + rim.dangling_b) or any(
        walk_pos[rim.dangling_a[i]] + 1 != walk_pos[rim.dangling_b[i]] for i in range(n)
    ):
        return None
    pos = np.asarray(block.coords)
    if pos.shape != (len(block.elements), 3) or not np.isfinite(pos).all():
        return None
    if any(e != "C" for e in block.elements):
        return None
    if any(
        type(i) is not int
        or type(j) is not int
        or i == j
        or not (0 <= i < len(pos) and 0 <= j < len(pos))
        for i, j, _ in block.bonds
    ):
        return None
    adj = _adjacency(block.bonds)
    for ring in block.rings:
        if (
            len(ring) < 3
            or any(type(i) is not int or not 0 <= i < len(pos) for i in ring)
            or len(set(ring)) != len(ring)
            or any(b not in adj.get(a, ()) for a, b in zip(ring, ring[1:] + ring[:1]))
        ):
            return None
    if any(b not in adj.get(a, ()) for a, b in pairwise(walk)):
        return None
    if any(len(adj.get(d, ())) != 2 for d in rim.dangling_a + rim.dangling_b):
        return None
    # the two walk atoms right after each dangling pair (S/W sites of the
    # bonded cells) are interior atoms of degree 3
    if any(
        len(adj.get(walk[walk_pos[rim.dangling_b[i]] + m], ())) != 3
        for i in range(n)
        for m in (1, 2)
        if walk_pos[rim.dangling_b[i]] + m < len(walk)
    ):
        return None
    sigma = block.sigma
    if not math.isfinite(sigma) or sigma <= 0:
        return None
    dpos = pos[list(rim.dangling_a)]
    delta = np.diff(dpos, axis=0)
    period = 3.0 * sigma
    if not np.allclose(np.linalg.norm(delta, axis=1), period):
        return None
    tangent = delta[0] / np.linalg.norm(delta[0])
    if not np.allclose(delta, period * tangent):
        return None
    outward = pos.mean(axis=0) - dpos[0]
    outward -= outward.dot(tangent) * tangent
    length = np.linalg.norm(outward)
    if length == 0:
        return None
    outward /= length
    normal = np.cross(tangent, outward)
    local = (pos - dpos[0]) @ np.column_stack((tangent, outward, normal))
    if not np.allclose(local[:, 2], 0) or np.any(local[:, 1] < -1e-8):
        return None
    # each dangling atom's missing-bond direction must be the nominal
    # armchair one: in-plane, toward the rim, tilting -x for A, +x for B
    for sgn, ds in ((-1.0, rim.dangling_a), (1.0, rim.dangling_b)):
        for d in ds:
            u = (pos[list(adj[d])] - pos[d]) / sigma
            missing = -u.sum(axis=0)
            if not (
                np.isclose(missing @ tangent, sgn * 0.5, atol=1e-6)
                and np.isclose(missing @ outward, -math.sqrt(3) / 2, atol=1e-6)
                and abs(missing @ normal) < 1e-6
            ):
                return None
    return np.column_stack((tangent, outward, normal))


def _sheet_counting(
    sheets: tuple[tuple[str, tuple[int, ...]], ...],
    bonds: tuple[tuple[int, int, int], ...],
    rings: tuple[tuple[int, ...], ...],
) -> dict[str, tuple[int, int]]:
    """Per-sheet counting-law terms (SPEC 6.3) from the built graph:
    ``(chi, B_combinatorial)`` where ``B = 6*chi - sum(6-n)*P_n`` over the
    sheet's own subgraph — the same derivation ``build.py`` uses for a
    patch's boundary term."""
    out: dict[str, tuple[int, int]] = {}
    for name, ords in sheets:
        comp = set(ords)
        cn = sum(1 for i, j, _ in bonds if i in comp and j in comp)
        cpn: dict[int, int] = {}
        for r in rings:
            if set(r) <= comp:
                cpn[len(r)] = cpn.get(len(r), 0) + 1
        cchi = len(comp) - cn + sum(cpn.values())
        out[name] = (cchi, 6 * cchi - sum((6 - k) * c for k, c in cpn.items()))
    return out


def _sheet_residuals(
    sheets: tuple[tuple[str, tuple[int, ...]], ...],
    bonds: tuple[tuple[int, int, int], ...],
    rings: tuple[tuple[int, ...], ...],
) -> dict[str, int]:
    """Per-sheet residual against the declared flat-disc boundary term
    ``B_expected = 6`` (what a plain sheet spec registers): the check is
    that each sheet's own face census stayed hexagon-only — the minted
    seam faces contain seam atoms and belong to no sheet."""
    counting = _sheet_counting(sheets, bonds, rings)
    out: dict[str, int] = {}
    for name, ords in sheets:
        cchi, _b_comb = counting[name]
        comp = set(ords)
        cpn: dict[int, int] = {}
        for r in rings:
            if set(r) <= comp:
                cpn[len(r)] = cpn.get(len(r), 0) + 1
        out[name] = sum((6 - k) * c for k, c in cpn.items()) + 6 - 6 * cchi
    return out


def _seam_angles(coords: np.ndarray, bonds, seam: tuple[int, ...]) -> list[list[float]]:
    """The pairwise bond angles at each seam atom, ascending — six at a
    degree-four atom, three at a degree-three terminal atom."""
    adj = _adjacency(bonds)
    out: list[list[float]] = []
    for atom in seam:
        vec = coords[adj[atom]] - coords[atom]
        vec = vec / np.linalg.norm(vec, axis=1)[:, None]
        cos = np.clip(vec @ vec.T, -1.0, 1.0)
        ang = np.degrees(np.arccos(cos))
        d = len(adj[atom])
        out.append(sorted(float(ang[i, j]) for i in range(d) for j in range(i + 1, d)))
    return out


def _rim_angles(
    coords: np.ndarray, bonds, seam: tuple[int, ...]
) -> list[tuple[int, list[float]]]:
    """Pairwise bond angles at each seam atom's sheet-side neighbours —
    the bonded dangling rim atoms — as ``(ordinal, sorted angles)``.
    These atoms are pinned alongside the seam, so the table reports the
    held seed registration: for k3-sp3-z it carries the row's 16.54-deg
    tilt residual."""
    adj = _adjacency(bonds)
    seam_set = set(seam)
    rim = sorted({n for a in seam for n in adj[a] if n not in seam_set})
    out: list[tuple[int, list[float]]] = []
    for atom in rim:
        vec = coords[adj[atom]] - coords[atom]
        vec = vec / np.linalg.norm(vec, axis=1)[:, None]
        cos = np.clip(vec @ vec.T, -1.0, 1.0)
        ang = np.degrees(np.arccos(cos))
        d = len(adj[atom])
        out.append(
            (atom, sorted(float(ang[i, j]) for i in range(d) for j in range(i + 1, d)))
        )
    return out


def _place_k4(
    blocks: tuple[Block, ...],
    rims: tuple[_ArmchairSegment, ...],
    frames: list[np.ndarray],
) -> _Sp3Composite:
    """Four sheets at 0/90/180/270 deg around the x axis; one sp3 atom per
    armchair period bonded to one dangling atom of each sheet, the bonds
    alternating axial tilt (+x on sheets 0,2; -x on sheets 1,3)."""
    sigma = blocks[0].sigma
    n = len(rims[0].dangling_a)
    period = 3.0 * sigma
    rho = sigma * math.sqrt(3) / 2  # rim radius: dangling tilt 60 deg
    coords: list[np.ndarray] = []
    transforms: list[tuple[np.ndarray, np.ndarray]] = []
    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    sheets: list[tuple[str, tuple[int, ...]]] = []
    boundaries: list[tuple[int, ...]] = []
    dangling: list[tuple[int, ...]] = []  # the bonded atom of each sheet/period
    walks: list[tuple[int, ...]] = []
    elements: tuple[str, ...] = ()
    offset = 0
    for j, (block, rim, frame) in enumerate(zip(blocks, rims, frames)):
        angle = math.pi * j / 2
        radial = np.array([0.0, math.cos(angle), math.sin(angle)])
        axis = np.array([1.0, 0.0, 0.0])
        target = np.column_stack((axis, radial, np.cross(axis, radial)))
        rotation = target @ frame.T
        tilt = 0.5 * sigma if j % 2 == 0 else -0.5 * sigma
        bonded = rim.dangling_a if j % 2 == 0 else rim.dangling_b
        shift = radial * rho + axis * tilt - rotation @ block.coords[bonded[0]]
        coords.append(block.coords @ rotation.T + shift)
        transforms.append((rotation, shift))
        bonds.extend((a + offset, b + offset, k) for a, b, k in block.bonds)
        rings.extend(tuple(i + offset for i in ring) for ring in block.rings)
        walks.append(tuple(i + offset for i in rim.atoms))
        dangling.append(tuple(i + offset for i in bonded))
        elements += block.elements
        sheets.append((f"sheet{j}", tuple(range(offset, offset + len(block.elements)))))
        boundaries.append(
            tuple(i + offset for i in _boundary_atoms(block.bonds, block.rings))
        )
        offset += len(block.elements)
    seam = tuple(range(offset, offset + n))
    spos = np.zeros((n, 3))
    spos[:, 0] = np.arange(n) * period
    coords.append(spos)
    for i, atom in enumerate(seam):
        bonds.extend((dangling[j][i], atom, 1) for j in range(4))
    # Between each adjacent seam-atom pair, each of the four sheet-pair
    # channels closes a 12-cycle: five rim atoms per side. Faces are
    # minted from the built graph's actual bond structure.
    pos_walk = [{atom: idx for idx, atom in enumerate(walk)} for walk in walks]
    for a, b in ((0, 1), (1, 2), (2, 3), (3, 0)):
        for i in range(n - 1):
            wa, wb = walks[a], walks[b]
            seg_a = wa[
                pos_walk[a][dangling[a][i]] : pos_walk[a][dangling[a][i + 1]] + 1
            ]
            seg_b = wb[
                pos_walk[b][dangling[b][i]] : pos_walk[b][dangling[b][i + 1]] + 1
            ]
            rings.append((seam[i],) + seg_a + (seam[i + 1],) + seg_b[::-1])
    hybridisation = ("sp2",) * offset + ("sp3",) * n
    return _Sp3Composite(
        elements + ("C",) * n,
        np.vstack(coords),
        tuple(bonds),
        tuple(rings),
        hybridisation,
        seam,
        tuple(transforms),
        tuple(sheets),
        tuple(boundaries),
        [],
    )


def _place_k3z(
    blocks: tuple[Block, ...],
    rims: tuple[_ZigzagSegment, ...],
    frames: list[np.ndarray],
) -> _Sp3Composite:
    """Three zigzag sheets at equal 120 deg; every seam atom bonds into the
    three sheets and to its chain partner.  The chain pairs periods
    (0,1),(2,3),... at the xTB-measured dimer length 1.60 A and sheet-bond
    length 1.51 A, so seam atom i sits (a - 1.60)/2 off the axis foot and
    the six seam angles read 106.54 x3 + 112.24 x3 — near-tetrahedral,
    not exact — while each bonded rim atom carries a 16.54-degree
    in-plane tilt residual (its dangling bond offers only 90 deg from
    the axis); pinning holds that seed, the tilt is reported not relaxed
    away."""
    sigma = blocks[0].sigma
    n = len(rims[0].dangling)
    period = math.sqrt(3) * sigma
    # Analytic seed = the GFN2-xTB minimum the equal-120 sp2 Y seam and
    # the seam tube both relax into (tblite, FIRE fmax 0.05, 2026-10-13):
    # seam atoms pair along the axis at 1.60-1.64 A, sheet bonds
    # 1.50-1.53 A, inter-dimer gap ~3.3 A.
    hdx = (period - 1.60) / 2
    rho = math.sqrt(1.51**2 - hdx**2)
    coords: list[np.ndarray] = []
    transforms: list[tuple[np.ndarray, np.ndarray]] = []
    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    sheets: list[tuple[str, tuple[int, ...]]] = []
    boundaries: list[tuple[int, ...]] = []
    walks: list[tuple[int, ...]] = []
    dangling: list[tuple[int, ...]] = []
    elements: tuple[str, ...] = ()
    offset = 0
    for j, (block, rim, frame) in enumerate(zip(blocks, rims, frames)):
        angle = 2 * math.pi * j / 3
        radial = np.array([0.0, math.cos(angle), math.sin(angle)])
        axis = np.array([1.0, 0.0, 0.0])
        target = np.column_stack((axis, radial, np.cross(axis, radial)))
        rotation = target @ frame.T
        shift = rho * radial - rotation @ block.coords[rim.dangling[0]]
        coords.append(block.coords @ rotation.T + shift)
        transforms.append((rotation, shift))
        bonds.extend((a + offset, b + offset, k) for a, b, k in block.bonds)
        rings.extend(tuple(i + offset for i in ring) for ring in block.rings)
        walks.append(tuple(i + offset for i in rim.atoms))
        dangling.append(tuple(i + offset for i in rim.dangling))
        elements += block.elements
        sheets.append((f"sheet{j}", tuple(range(offset, offset + len(block.elements)))))
        boundaries.append(
            tuple(i + offset for i in _boundary_atoms(block.bonds, block.rings))
        )
        offset += len(block.elements)
    seam = tuple(range(offset, offset + n))
    spos = np.zeros((n, 3))
    spos[:, 0] = np.arange(n) * period + np.where(np.arange(n) % 2 == 0, hdx, -hdx)
    coords.append(spos)
    for i, atom in enumerate(seam):
        bonds.extend((dangling[j][i], atom, 1) for j in range(3))
        if i % 2 == 0:
            bonds.append((atom, seam[i + 1], 1))
    # Faces: over a bonded dimer gap, one pentagon per sheet; over an
    # unbonded gap, one octagon per sheet pair.
    for m in range(n // 2):
        i, k = 2 * m, 2 * m + 1
        for j in range(3):
            rings.append(
                (seam[i], dangling[j][i], walks[j][2 * i + 2], dangling[j][k], seam[k])
            )
    for m in range(n // 2 - 1):
        i, k = 2 * m + 1, 2 * m + 2
        for a, b in ((0, 1), (1, 2), (2, 0)):
            rings.append(
                (
                    seam[i],
                    dangling[a][i],
                    walks[a][2 * i + 2],
                    dangling[a][k],
                    seam[k],
                    dangling[b][k],
                    walks[b][2 * i + 2],
                    dangling[b][i],
                )
            )
    hybridisation = ("sp2",) * offset + ("sp3",) * n
    return _Sp3Composite(
        elements + ("C",) * n,
        np.vstack(coords),
        tuple(bonds),
        tuple(rings),
        hybridisation,
        seam,
        tuple(transforms),
        tuple(sheets),
        tuple(boundaries),
        [],
    )


def _place_k3a(
    blocks: tuple[Block, ...],
    rims: tuple[_ArmchairSegment, ...],
    frames: list[np.ndarray],
) -> _Sp3Composite:
    """Three armchair sheets at equal 120 deg; two seam atoms per armchair
    period, ``a_i`` bonded to the ``dangling_a_i`` of every sheet and
    ``b_i`` to the ``dangling_b_i``, with the ``(b_i, a_{i+1})`` pairs
    chained along the axis.  Seed: the 2026-10-14 GFN2-xTB model's
    relaxed lengths — sheet bonds 1.52 A, chain 1.55 A — placing the rim
    at rho ~ 1.38 A and each seam atom 0.645 A axially off its dangling
    atoms.  The terminal ``a_0``/``b_{n-1}`` have no chain partner and
    stay degree-three open-boundary atoms."""
    sigma = blocks[0].sigma
    n = len(rims[0].dangling_a)
    period = 3.0 * sigma
    sheet_bond = 1.52
    chain = 1.55
    off = (2.0 * sigma - chain) / 2.0
    rho = math.sqrt(sheet_bond**2 - off**2)
    coords: list[np.ndarray] = []
    transforms: list[tuple[np.ndarray, np.ndarray]] = []
    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    sheets: list[tuple[str, tuple[int, ...]]] = []
    boundaries: list[tuple[int, ...]] = []
    walks: list[tuple[int, ...]] = []
    dangling_a: list[tuple[int, ...]] = []
    dangling_b: list[tuple[int, ...]] = []
    elements: tuple[str, ...] = ()
    offset = 0
    for j, (block, rim, frame) in enumerate(zip(blocks, rims, frames)):
        angle = 2 * math.pi * j / 3
        radial = np.array([0.0, math.cos(angle), math.sin(angle)])
        axis = np.array([1.0, 0.0, 0.0])
        target = np.column_stack((axis, radial, np.cross(axis, radial)))
        rotation = target @ frame.T
        shift = rho * radial - rotation @ block.coords[rim.dangling_a[0]]
        coords.append(block.coords @ rotation.T + shift)
        transforms.append((rotation, shift))
        bonds.extend((a + offset, b + offset, k) for a, b, k in block.bonds)
        rings.extend(tuple(i + offset for i in ring) for ring in block.rings)
        walks.append(tuple(i + offset for i in rim.atoms))
        dangling_a.append(tuple(i + offset for i in rim.dangling_a))
        dangling_b.append(tuple(i + offset for i in rim.dangling_b))
        elements += block.elements
        sheets.append((f"sheet{j}", tuple(range(offset, offset + len(block.elements)))))
        boundaries.append(
            tuple(i + offset for i in _boundary_atoms(block.bonds, block.rings))
        )
        offset += len(block.elements)
    seam = tuple(range(offset, offset + 2 * n))
    spos = np.zeros((2 * n, 3))
    for i in range(n):
        spos[2 * i, 0] = period * i - off
        spos[2 * i + 1, 0] = period * i + sigma + off
    coords.append(spos)
    for i in range(n):
        a_i, b_i = seam[2 * i], seam[2 * i + 1]
        for j in range(3):
            bonds.append((dangling_a[j][i], a_i, 1))
            bonds.append((dangling_b[j][i], b_i, 1))
        if i + 1 < n:
            bonds.append((b_i, seam[2 * (i + 1)], 1))
    # Faces (i = 1..n-1): per sheet the hexagon
    # (a_i, A_i, W_{i-1}, S_{i-1}, B_{i-1}, b_{i-1}) through the
    # sigma-spaced chain bond, and per sheet pair the decagon that closes
    # the corridor between the two sheets' rim walks.  The walk order is
    # A_i, B_i, S_i, W_i per cell, cells starting at -1, so the rim
    # segment from B_{i-1} to A_i is walk[4i+1 : 4i+5].
    pos_walk = [{atom: idx for idx, atom in enumerate(walk)} for walk in walks]
    for j in range(3):
        walk = walks[j]
        for i in range(1, n):
            seg = walk[
                pos_walk[j][dangling_b[j][i - 1]] : pos_walk[j][dangling_a[j][i]] + 1
            ]
            rings.append((seam[2 * i],) + seg[::-1] + (seam[2 * (i - 1) + 1],))
    for a, b in ((0, 1), (1, 2), (2, 0)):
        wa, wb = walks[a], walks[b]
        for i in range(1, n):
            seg_a = wa[
                pos_walk[a][dangling_b[a][i - 1]] : pos_walk[a][dangling_a[a][i]] + 1
            ]
            seg_b = wb[
                pos_walk[b][dangling_b[b][i - 1]] : pos_walk[b][dangling_a[b][i]] + 1
            ]
            rings.append(
                (seam[2 * i],) + seg_a[::-1] + (seam[2 * (i - 1) + 1],) + seg_b
            )
    hybridisation = ("sp2",) * offset + ("sp3",) * (2 * n)
    return _Sp3Composite(
        elements + ("C",) * (2 * n),
        np.vstack(coords),
        tuple(bonds),
        tuple(rings),
        hybridisation,
        seam,
        tuple(transforms),
        tuple(sheets),
        tuple(boundaries),
        [],
    )


def compose_sp3(
    blocks: tuple[Block, ...],
    rims: tuple[_ZigzagSegment | _ArmchairSegment, ...],
    *,
    seam_type: str,
    dihedrals_deg: tuple[float, ...],
) -> _Sp3Composite:
    """Selected sp3 seam joins: private segments, deterministic, no relax.

    Refuse unsupported declarations before examining coordinates.  The
    rim class is part of the row: ``k4-sp3-90-a`` takes four armchair
    segments, ``k3-sp3-z`` three zigzag ones; a wrong rim class is a
    ``port.mismatch``, wrong multiplicity/dihedrals ``fit.unsolvable``.
    """
    specs = {
        "k4-sp3-90-a": (4, (90.0,) * 4, _ArmchairSegment),
        "k3-sp3-z": (3, (120.0,) * 3, _ZigzagSegment),
        "k3-sp3-a": (3, (120.0,) * 3, _ArmchairSegment),
    }
    spec = specs.get(seam_type)
    if (
        spec is None
        or len(blocks) != spec[0]
        or len(rims) != spec[0]
        or len(dihedrals_deg) != spec[0]
        or any(
            isinstance(a, bool) or not isinstance(a, int | float) or a != spec[1][0]
            for a in dihedrals_deg
        )
    ):
        return _sp3_refusal(
            "fit.unsolvable",
            "unsupported authored sp3 seam declaration",
            multiplicity=len(rims),
            seam_type=seam_type,
            dihedrals_deg=dihedrals_deg,
        )
    k, _dh, rim_type = spec
    armchair = [r for r in rims if isinstance(r, _ArmchairSegment)]
    zigzag = [r for r in rims if isinstance(r, _ZigzagSegment)]
    if rim_type is _ArmchairSegment and len(armchair) != len(rims):
        return _sp3_refusal(
            "port.mismatch",
            f"{seam_type} requires armchair segments on every sheet",
            seam_type=seam_type,
        )
    if rim_type is _ZigzagSegment and len(zigzag) != len(rims):
        return _sp3_refusal(
            "port.mismatch",
            "k3-sp3-z requires zigzag segments on every sheet",
            seam_type=seam_type,
        )
    sigma = blocks[0].sigma
    if (
        not math.isfinite(sigma)
        or sigma <= 0
        or any(not math.isclose(b.sigma, sigma, rel_tol=1e-12) for b in blocks)
    ):
        return _sp3_refusal(
            "fit.unsolvable", "segments require one finite positive sigma"
        )
    counts = (
        tuple(len(r.dangling_a) for r in armchair)
        if rim_type is _ArmchairSegment
        else tuple(len(r.dangling) for r in zigzag)
    )
    if len(set(counts)) != 1:
        return _sp3_refusal(
            "port.mismatch", "segment dangling counts differ", counts=counts
        )
    if seam_type == "k3-sp3-z" and counts[0] % 2 != 0:
        return _sp3_refusal(
            "fit.unsolvable",
            "k3-sp3-z pairs seam atoms along the chain; needs an even period count",
            counts=counts,
        )
    frames: list[np.ndarray] = []
    for block, rim in zip(blocks, rims):
        frame = (
            _armchair_frame(block, rim)
            if isinstance(rim, _ArmchairSegment)
            else _segment_frame(block, rim)
        )
        if frame is None:
            return _sp3_refusal(
                "port.mismatch",
                "invalid straight carbon segment/frame",
                rim=rim.name,
                endpoints=rim.endpoints,
            )
        frames.append(frame)
    if seam_type == "k4-sp3-90-a":
        return _place_k4(blocks, tuple(armchair), frames)
    if seam_type == "k3-sp3-a":
        return _place_k3a(blocks, tuple(armchair), frames)
    return _place_k3z(blocks, tuple(zigzag), frames)


def _seam_net(k: int, result: _Sp3Composite) -> Net:
    """Wrap a composite into a Net with per-sheet bookkeeping: sheet atoms
    partitioned by sheet, each sheet's open boundary registered as its
    rim with its combinatorial term derived from the built graph (as
    ``build.py`` does for a patch) and the flat-disc expectation
    ``B_expected = 6`` — the residual then checks the seam minting left
    every sheet a hexagon-only disc; seam atoms recorded sp3."""
    inst_of: dict[int, str] = {i: name for name, ords in result.sheets for i in ords}
    b_terms = list(_sheet_counting(result.sheets, result.bonds, result.rings).values())
    atoms = tuple(
        Atom(
            AtomPath(inst_of.get(i, "seam"), Site(i, 0, 0)),
            i,
            element,
            result.hybridisation[i],
            inst_of.get(i, "seam"),
        )
        for i, element in enumerate(result.elements)
    )
    return Net(
        atoms=atoms,
        bonds=result.bonds,
        rings=result.rings,
        ports=(),
        regions=result.sheets,
        report=Report(),
        spec=Spec(),
        lattice=Lattice(),
        seed3=tuple(
            (float(row[0]), float(row[1]), float(row[2])) for row in result.coords
        ),
        seed_kind="mixed",
        term_rims=tuple(
            (rim, b_comb, 6) for rim, (_chi, b_comb) in zip(result.boundaries, b_terms)
        ),
        seams=(
            SeamRecord(
                "seam", tuple(n for n, _ in result.sheets), k, result.seam_atoms
            ),
        ),
        sheet_atoms=result.sheets,
        sheets=tuple(
            (
                name,
                tuple(idx for idx, r in enumerate(result.rings) if set(r) <= set(ords)),
            )
            for name, ords in result.sheets
        ),
    )


def straight_k4(periods: int, levels: int) -> tuple[Net, _Sp3Composite]:
    """Four identical armchair-rimmed sheets at 90 deg; counts describe
    seam periods and atom levels. Public admission belongs to the SE
    adapter; this helper also rejects invalid counts before allocating.
    ``levels`` is the armchair sheet's atom-row count; under 3 the sheet
    is only its rim walk and carries no hexagon."""
    if any(type(n) is not int or not 2 <= n <= 30 for n in (periods, levels)):
        raise ValueError("straight k4 counts must be integers in [2,30]")
    if levels < 3:
        raise ValueError("straight k4 levels must be >= 3")
    block, rim = _armchair_sheet(periods, levels)
    result = compose_sp3(
        (block, block, block, block),
        (rim, rim, rim, rim),
        seam_type="k4-sp3-90-a",
        dihedrals_deg=(90, 90, 90, 90),
    )
    if result.findings:
        raise ValueError(Report(tuple(result.findings)).render())
    return _seam_net(4, result), result


def straight_k3z(periods: int, row_pairs: int) -> tuple[Net, _Sp3Composite]:
    """Three zigzag-rimmed sheets at 120 deg plus the seam chain; even
    periods only. Public admission belongs to the SE adapter."""
    if any(type(n) is not int or not 2 <= n <= 30 for n in (periods, row_pairs)):
        raise ValueError("straight k3z counts must be integers in [2,30]")
    block, rim = _sheet(periods, row_pairs)
    result = compose_sp3(
        (block, block, block),
        (rim, rim, rim),
        seam_type="k3-sp3-z",
        dihedrals_deg=(120, 120, 120),
    )
    if result.findings:
        raise ValueError(Report(tuple(result.findings)).render())
    return _seam_net(3, result), result


def straight_k3a(periods: int, levels: int) -> tuple[Net, _Sp3Composite]:
    """Three armchair-rimmed sheets at 120 deg; two seam atoms per period,
    chained across periods.  Public admission belongs to the SE adapter;
    ``levels`` follows :func:`straight_k4`."""
    if any(type(n) is not int or not 2 <= n <= 30 for n in (periods, levels)):
        raise ValueError("straight k3a counts must be integers in [2,30]")
    if levels < 3:
        raise ValueError("straight k3a levels must be >= 3")
    block, rim = _armchair_sheet(periods, levels)
    result = compose_sp3(
        (block, block, block),
        (rim, rim, rim),
        seam_type="k3-sp3-a",
        dihedrals_deg=(120, 120, 120),
    )
    if result.findings:
        raise ValueError(Report(tuple(result.findings)).render())
    return _seam_net(3, result), result

"""The read surface — exact, in-memory probes over the Scene (ADR 0043 §6).

These are *reads* (the Read category of §6): idempotent queries that return
numbers for the LLM, computed against the hydrated Scene with no DB round-trip.
v1 floor: toc · atom config · neighborhood · coordination · distance/angle ·
find · auto bond detection. Field/ensemble probes are vision (§6.7/§18).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import elements
from .cell import ImageOffset
from .scene import Bond, Scene


@dataclass
class NeighborHit:
    """One neighbour in a coordination shell."""

    label: str
    element: str
    distance: float
    image: ImageOffset


def coordination(scene: Scene, label: str, tolerance: float = 1.2) -> int:
    """Number of atoms within the covalent bond cutoff of ``label`` (MIC)."""
    a = scene.atoms[label]
    n = 0
    for other in scene.atoms.values():
        if other.label == label:
            continue
        dist, _ = scene.cell.mic(a.frac, other.frac)
        if dist <= elements.bond_cutoff(a.element, other.element, tolerance):
            n += 1
    return n


def neighborhood(scene: Scene, label: str, radius: float) -> list[NeighborHit]:
    """The coordination shell of ``label`` within ``radius`` Å, nearest first."""
    hits: list[NeighborHit] = []
    for other_label, image, dist in scene.neighbors(label, radius):
        hits.append(
            NeighborHit(
                label=other_label,
                element=scene.atoms[other_label].element,
                distance=dist,
                image=image,
            )
        )
    return hits


def distance(scene: Scene, a: str, b: str) -> float:
    """MIC distance (Å) between two atoms."""
    dist, _ = scene.cell.mic(scene.atoms[a].frac, scene.atoms[b].frac)
    return dist


def angle(scene: Scene, a: str, b: str, c: str) -> float:
    """Angle a–b–c in degrees, MIC-aware (b is the vertex)."""
    cell = scene.cell
    fb = scene.atoms[b].frac
    # nearest images of a and c relative to the vertex b
    _, ia = cell.mic(fb, scene.atoms[a].frac)
    _, ic = cell.mic(fb, scene.atoms[c].frac)
    va = cell.frac_to_cart(scene.atoms[a].frac + np.array(ia) - fb)
    vc = cell.frac_to_cart(scene.atoms[c].frac + np.array(ic) - fb)
    cosang = float(va @ vc / (np.linalg.norm(va) * np.linalg.norm(vc)))
    return float(np.degrees(np.arccos(np.clip(cosang, -1.0, 1.0))))


def detect_bonds(scene: Scene, tolerance: float = 1.2) -> list[Bond]:
    """Auto-detect bonds from geometry (covalent cutoff), marked ``inferred``.

    ADR 0043 Open-Q2: the LLM always sees the best image of reality — bonds are
    auto-detected, never withheld, and tagged ``inferred`` so they're marked, not
    hidden. Each unordered pair is emitted once with its MIC image offset.
    """
    out: list[Bond] = []
    labels = list(scene.atoms)
    for ai in range(len(labels)):
        a = scene.atoms[labels[ai]]
        for bj in range(ai + 1, len(labels)):
            b = scene.atoms[labels[bj]]
            dist, img = scene.cell.mic(a.frac, b.frac)
            if dist <= elements.bond_cutoff(a.element, b.element, tolerance):
                out.append(Bond(i=a.label, j=b.label, provenance="inferred", image=img))
    return out


def find(
    scene: Scene,
    *,
    element: str | None = None,
    undercoordinated: bool = False,
    tolerance: float = 1.2,
) -> list[str]:
    """Select atom labels by predicate (element and/or under-coordination)."""
    out: list[str] = []
    for label, atom in scene.atoms.items():
        if element is not None and atom.element != element:
            continue
        if undercoordinated:
            mv = elements.max_valence(atom.element)
            if mv is None or coordination(scene, label, tolerance) >= mv:
                continue
        out.append(label)
    return out


def dihedral(scene: Scene, a: str, b: str, c: str, d: str) -> float:
    """Dihedral angle a–b–c–d in degrees, MIC-aware (the torsion about b–c)."""
    cell = scene.cell
    fb = scene.atoms[b].frac
    fc = scene.atoms[c].frac
    # place every atom in the image nearest the b–c axis midpoint
    _, ia = cell.mic(fb, scene.atoms[a].frac)
    _, ic = cell.mic(fb, fc)
    _, id_ = cell.mic(fc, scene.atoms[d].frac)
    pa = cell.frac_to_cart(scene.atoms[a].frac + np.array(ia))
    pb = cell.frac_to_cart(fb)
    pc = cell.frac_to_cart(fc + np.array(ic))
    pd = cell.frac_to_cart(scene.atoms[d].frac + np.array(id_) + np.array(ic))
    b1, b2, b3 = pb - pa, pc - pb, pd - pc
    n1 = np.cross(b1, b2)
    n2 = np.cross(b2, b3)
    m1 = np.cross(n1, b2 / np.linalg.norm(b2))
    x = float(n1 @ n2)
    y = float(m1 @ n2)
    return float(np.degrees(np.arctan2(y, x)))


# -- spatial probes (the CAD ray / plane, retargeted to atoms, §6.2) ---------


@dataclass
class LineHit:
    """An atom near a probe ray, with its position along and offset from it."""

    label: str
    element: str
    along: float  # Å projected onto the ray direction
    offset: float  # Å perpendicular distance from the ray


def _cart(scene: Scene, label: str) -> np.ndarray:
    return scene.cell.frac_to_cart(scene.atoms[label].frac)


def line(
    scene: Scene,
    origin: np.ndarray,
    direction: np.ndarray,
    radius: float,
) -> list[LineHit]:
    """Atoms within ``radius`` Å of a Cartesian ray, ordered along it (§6.2).

    The instrument for channels / atom columns. v1 reads in-cell positions (no
    periodic image expansion) — adequate for an adsorbate column or a pore; a
    PBC-tiled ray is a later refinement.
    """
    o = np.asarray(origin, dtype=float)
    d = np.asarray(direction, dtype=float)
    dn = d / np.linalg.norm(d)
    hits: list[LineHit] = []
    for label, atom in scene.atoms.items():
        p = scene.cell.frac_to_cart(atom.frac) - o
        along = float(p @ dn)
        offset = float(np.linalg.norm(p - along * dn))
        if offset <= radius:
            hits.append(LineHit(label, atom.element, along, offset))
    hits.sort(key=lambda h: h.along)
    return hits


@dataclass
class PlaneHit:
    """An atom within a planar slab, with its in-plane 2D coordinates."""

    label: str
    element: str
    signed: float  # Å off the plane (sign = which side)
    u: float  # Å along the first in-plane axis
    v: float  # Å along the second in-plane axis


def _plane_basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = np.asarray(normal, dtype=float)
    n = n / np.linalg.norm(n)
    seed = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = seed - (seed @ n) * n
    u = u / np.linalg.norm(u)
    v = np.cross(n, u)
    return n, u, v


def plane(
    scene: Scene,
    point: np.ndarray,
    normal: np.ndarray,
    thickness: float,
) -> list[PlaneHit]:
    """Atoms within ``thickness`` Å of a plane, as a labelled 2D map (§6.2).

    The honest, token-cheap form of a layer slice: real in-plane coordinates +
    labels, never a raster. Returned sorted by (u, v).
    """
    p0 = np.asarray(point, dtype=float)
    n, u, v = _plane_basis(normal)
    hits: list[PlaneHit] = []
    for label, atom in scene.atoms.items():
        rel = scene.cell.frac_to_cart(atom.frac) - p0
        signed = float(rel @ n)
        if abs(signed) <= thickness:
            hits.append(
                PlaneHit(label, atom.element, signed, float(rel @ u), float(rel @ v))
            )
    hits.sort(key=lambda h: (round(h.u, 3), round(h.v, 3)))
    return hits


@dataclass
class CrossingBond:
    """A bond whose segment crosses a plane (§6.2 bonds_through_plane)."""

    i: str
    j: str
    order: float
    length: float
    angle_to_normal: float  # degrees


def bonds_through_plane(
    scene: Scene,
    point: np.ndarray,
    normal: np.ndarray,
) -> list[CrossingBond]:
    """Bonds whose segment crosses the plane (§6.2) — what stitches two layers.

    Image-aware: atom ``j`` is taken in the bond's declared periodic image, so a
    bond crossing *via* a cell wall is measured at its real geometry.
    """
    p0 = np.asarray(point, dtype=float)
    n, _, _ = _plane_basis(normal)
    out: list[CrossingBond] = []
    for b in scene.bonds:
        if b.i not in scene.atoms or b.j not in scene.atoms:
            continue
        pi = scene.cell.frac_to_cart(scene.atoms[b.i].frac)
        pj = scene.cell.frac_to_cart(scene.atoms[b.j].frac + np.array(b.image))
        si = float((pi - p0) @ n)
        sj = float((pj - p0) @ n)
        if si * sj < 0:  # endpoints on opposite sides
            vec = pj - pi
            length = float(np.linalg.norm(vec))
            cos = abs(float(vec @ n) / length) if length else 0.0
            ang = float(np.degrees(np.arccos(np.clip(cos, 0.0, 1.0))))
            out.append(CrossingBond(b.i, b.j, b.order, length, ang))
    return out


def bonds_in_sphere(
    scene: Scene,
    center: np.ndarray,
    radius: float,
) -> list[CrossingBond]:
    """Bonds inside or crossing a sphere (§6.2) — the local bonding environment.

    A bond counts if either endpoint is within ``radius`` or the segment passes
    within it. ``angle_to_normal`` is repurposed as the angle of the bond to the
    centre→midpoint direction (a cheap orientation cue)."""
    c = np.asarray(center, dtype=float)
    out: list[CrossingBond] = []
    for b in scene.bonds:
        if b.i not in scene.atoms or b.j not in scene.atoms:
            continue
        pi = scene.cell.frac_to_cart(scene.atoms[b.i].frac)
        pj = scene.cell.frac_to_cart(scene.atoms[b.j].frac + np.array(b.image))
        seg = pj - pi
        length = float(np.linalg.norm(seg))
        # closest point on the segment to the sphere centre
        t = 0.0 if length == 0 else float(np.clip((c - pi) @ seg / (length**2), 0, 1))
        closest = pi + t * seg
        if float(np.linalg.norm(closest - c)) <= radius:
            mid = (pi + pj) / 2 - c
            cos = (
                abs(float(seg @ mid) / (length * np.linalg.norm(mid)))
                if length and np.linalg.norm(mid)
                else 0.0
            )
            ang = float(np.degrees(np.arccos(np.clip(cos, 0.0, 1.0))))
            out.append(CrossingBond(b.i, b.j, b.order, length, ang))
    return out


# -- graph topology (path · rings · fragments, §6.1/§6.5) --------------------


def _adjacency(scene: Scene) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {label: set() for label in scene.atoms}
    for b in scene.bonds:
        if b.i in adj and b.j in adj:
            adj[b.i].add(b.j)
            adj[b.j].add(b.i)
    return adj


def path(scene: Scene, a: str, b: str) -> list[str] | None:
    """Shortest bond path a→b (BFS over the graph), or None if disconnected."""
    if a not in scene.atoms or b not in scene.atoms:
        return None
    adj = _adjacency(scene)
    prev: dict[str, str | None] = {a: None}
    queue = [a]
    while queue:
        cur = queue.pop(0)
        if cur == b:
            chain = [cur]
            while prev[chain[-1]] is not None:
                chain.append(prev[chain[-1]])  # type: ignore[arg-type]
            return list(reversed(chain))
        for nxt in adj[cur]:
            if nxt not in prev:
                prev[nxt] = cur
                queue.append(nxt)
    return None


def fragments(scene: Scene) -> list[list[str]]:
    """Connected components over the bond graph — the chemical fragments (§6.5).

    Returned largest-first; an isolated atom is its own one-atom fragment. The
    instrument for "did this edit fragment the structure?" and fragment-centric
    navigation.
    """
    adj = _adjacency(scene)
    seen: set[str] = set()
    comps: list[list[str]] = []
    for start in scene.atoms:
        if start in seen:
            continue
        comp: list[str] = []
        stack = [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        comps.append(sorted(comp))
    comps.sort(key=len, reverse=True)
    return comps


def rings(scene: Scene, max_size: int = 8) -> list[list[str]]:
    """Smallest bond cycles up to ``max_size`` atoms (§6.1 — find sp² 6-rings).

    For each bonded pair, drop the edge and find the shortest alternate path;
    its length+1 is the smallest ring through that bond. Rings are deduplicated
    by their atom set, returned smallest-first.
    """
    adj = _adjacency(scene)
    found: dict[frozenset[str], list[str]] = {}
    for b in scene.bonds:
        i, j = b.i, b.j
        if i not in adj or j not in adj:
            continue
        # BFS i→j without using the direct i–j edge
        prev: dict[str, str | None] = {i: None}
        queue = [i]
        hit = False
        while queue and not hit:
            cur = queue.pop(0)
            for nxt in adj[cur]:
                if cur == i and nxt == j:
                    continue  # the edge we're testing
                if nxt == j:
                    prev[j] = cur
                    hit = True
                    break
                if nxt not in prev:
                    prev[nxt] = cur
                    queue.append(nxt)
        if not hit:
            continue
        chain = [j]
        while prev.get(chain[-1]) is not None:
            chain.append(prev[chain[-1]])  # type: ignore[arg-type]
        ring = list(reversed(chain))
        if len(ring) <= max_size:
            found.setdefault(frozenset(ring), ring)
    return sorted(found.values(), key=len)


# -- diff (the most insightful single relaxation view, §6.3) -----------------


@dataclass
class StructureDiff:
    """Per-atom displacement + graph delta between two designs/versions."""

    rmsd: float
    max_disp: float
    moved: list[tuple[str, float]]  # (label, displacement Å), largest first
    bonds_broken: list[tuple[str, str]]
    bonds_formed: list[tuple[str, str]]
    atoms_added: list[str]
    atoms_removed: list[str]


def _bond_set(scene: Scene) -> set[frozenset[str]]:
    return {frozenset((b.i, b.j)) for b in scene.bonds}


def diff(before: Scene, after: Scene) -> StructureDiff:
    """Compare two scenes by atom label: displacement (MIC in ``after``'s cell),
    RMSD, and which bonds/atoms broke, formed, appeared, or vanished (§6.3)."""
    common = [la for la in before.atoms if la in after.atoms]
    moved: list[tuple[str, float]] = []
    sq = 0.0
    for la in common:
        d, _ = after.cell.mic(before.atoms[la].frac, after.atoms[la].frac)
        moved.append((la, d))
        sq += d * d
    moved.sort(key=lambda t: t[1], reverse=True)
    rmsd = float((sq / len(common)) ** 0.5) if common else 0.0
    max_disp = moved[0][1] if moved else 0.0
    b0, b1 = _bond_set(before), _bond_set(after)
    broken = [tuple(sorted(s)) for s in (b0 - b1)]
    formed = [tuple(sorted(s)) for s in (b1 - b0)]
    return StructureDiff(
        rmsd=rmsd,
        max_disp=max_disp,
        moved=moved,
        bonds_broken=[(x, y) for x, y in broken],
        bonds_formed=[(x, y) for x, y in formed],
        atoms_added=sorted(set(after.atoms) - set(before.atoms)),
        atoms_removed=sorted(set(before.atoms) - set(after.atoms)),
    )


# -- embodiment (the uniform point-of-view readout, §6.6, stateless v1) ------


@dataclass
class Pov:
    """One embodiment's uniform readout: i_am · i_include · i_touch (§6.6)."""

    i_am: str  # 'atom' | 'fragment'
    i_include: list[str]  # the support atoms
    i_touch: list[tuple[str, float]]  # (label, distance Å) outside the support


def pov(scene: Scene, support: list[str], reach: float = 3.0) -> Pov:
    """The §6.6 embodiment readout over an atom or a fragment support.

    Stateless v1: the support is given explicitly (a single atom or a set —
    e.g. a fragment from ``fragments()`` or a ring from ``rings()``). The
    *persisted, named* cursor with a bookmark stack is the stateful refinement
    (§6.8, vision). ``i_touch`` = atoms within ``reach`` Å of any support atom,
    excluding the support itself, nearest-first.
    """
    sset = set(support)
    touch: dict[str, float] = {}
    for member in support:
        if member not in scene.atoms:
            continue
        for other, _img, dist in scene.neighbors(member, reach):
            if other in sset:
                continue
            touch[other] = min(touch.get(other, dist), dist)
    ordered = sorted(touch.items(), key=lambda t: t[1])
    return Pov(
        i_am="atom" if len(support) == 1 else "fragment",
        i_include=list(support),
        i_touch=ordered,
    )


def toc(scene: Scene) -> dict[str, object]:
    """The structure summary: cell, pbc, composition, per-element counts.

    The symmetry-orbit collapse (spglib) is a later increment; v1 groups by
    element + the fixed flag, which is the cheap legible reduction.
    """
    comp = scene.composition()
    formula = "".join(f"{el}{comp[el]}" for el in sorted(comp))
    return {
        "formula": formula,
        "natoms": len(scene.atoms),
        "composition": comp,
        "pbc": scene.cell.pbc,
        "volume": scene.cell.volume,
        "nbonds": len(scene.bonds),
        "nfragments": len(fragments(scene)),
    }

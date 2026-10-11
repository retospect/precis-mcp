"""Systematic fragment names derived from a scene's bond graph.

A structure is titled with a trivial name (``azobenzene``) but agents search
by systematic fragments (``diazene phenyl``). The card text appends the
names of the functional groups / rings found in the graph, so lexical and
semantic search both reach the trivial-name card (gr334774). Pure topology:
no external lookup, no stored synonym table.
"""

from __future__ import annotations

import numpy as np

from precis.structure.elements import covalent_radius
from precis.structure.scene import Bond, Scene

_AROMATIC_ORDER = 1.5

_Adj = dict[str, dict[str, tuple[float, str]]]


def _is_double(order: float) -> bool:
    return abs(order - 2.0) < 0.25


#: Elements bonds are perceived between when a scene stores none. Metals are
#: excluded on purpose: slabs would give spurious bonds and O(N^2) cost.
_NONMETALS = frozenset(["H", "B", "C", "N", "O", "F", "Si", "P", "S", "Cl", "Br", "I"])
_BOND_FACTOR = 1.15
_MAX_PERCEIVED = 4000  # non-metal atoms; larger scenes are not named


def _infer_order(a: str, b: str, d: float) -> tuple[float, str]:
    """Bond order from the distance (A) alone, for the pairs the patterns use."""
    pair = {a, b}
    if pair == {"N"}:
        return (3.0 if d < 1.15 else 2.0 if d < 1.35 else 1.0), "pairwise"
    if pair == {"C", "N"}:
        if d < 1.20:
            return 3.0, "pairwise"
        return (2.0 if d < 1.30 else 1.0), "pairwise"
    if pair == {"C", "O"}:
        return (2.0 if d < 1.30 else 1.0), "pairwise"
    if pair == {"C"}:
        if d < 1.30:
            return 3.0 if d < 1.25 else 2.0, "pairwise"
        if d < 1.36:
            return 2.0, "pairwise"
        if d <= 1.46:
            return 1.5, "aromatic"
    return 1.0, "pairwise"


def _perceive_bonds(scene: Scene) -> list[Bond]:
    """Distance-perceived bonds among non-metal atoms (naming only; never
    stored). Minimum image per periodic axis, d < 1.15 * (r_i + r_j)."""
    atoms = [a for a in scene.atoms.values() if a.element in _NONMETALS]
    n = len(atoms)
    if n < 2 or n > _MAX_PERCEIVED:
        return []
    frac = np.array([a.frac for a in atoms], dtype=float)
    rad = np.array([covalent_radius(a.element) for a in atoms])
    pbc = np.array(scene.cell.pbc, dtype=bool)
    out: list[Bond] = []
    for i in range(n - 1):
        df = frac[i + 1 :] - frac[i]
        df[:, pbc] -= np.round(df[:, pbc])
        d = np.linalg.norm(df @ scene.cell.lattice, axis=1)
        close = np.nonzero((d > 0.4) & (d < _BOND_FACTOR * (rad[i] + rad[i + 1 :])))[0]
        for k in close:
            j = i + 1 + int(k)
            order, kind = _infer_order(atoms[i].element, atoms[j].element, float(d[k]))
            out.append(
                Bond(
                    atoms[i].label,
                    atoms[j].label,
                    order=order,
                    kind=kind,
                    provenance="inferred",
                )
            )
    return out


def _adjacency(bonds: list[Bond], scene: Scene) -> _Adj:
    adj: _Adj = {a: {} for a in scene.atoms}
    for b in bonds:
        if b.i in adj and b.j in adj and b.i != b.j:
            adj[b.i][b.j] = (b.order, b.kind)
            adj[b.j][b.i] = (b.order, b.kind)
    return adj


def _has_benzene_ring(scene: Scene, adj: _Adj) -> bool:
    """True when a six-carbon ring is aromatic (flagged, order 1.5, or a
    Kekule alternation of three double bonds)."""
    carbons = {a for a, at in scene.atoms.items() if at.element == "C"}
    found: set[frozenset[str]] = set()

    def walk(start: str, path: list[str]) -> None:
        for nxt in adj[path[-1]]:
            if nxt not in carbons:
                continue
            if len(path) == 6:
                if nxt == start:
                    found.add(frozenset(path))
            elif nxt not in path and nxt > start:
                walk(start, [*path, nxt])

    for c in carbons:
        walk(c, [c])
    for ring in found:
        edges = [adj[a][b] for a in ring for b in adj[a] if b in ring and a < b]
        if len(edges) != 6:
            continue
        if all(k == "aromatic" or abs(o - _AROMATIC_ORDER) < 0.1 for o, k in edges):
            return True
        if sum(1 for o, _ in edges if _is_double(o)) == 3:
            return True
    return False


def fragment_names(scene: Scene) -> list[str]:
    """Systematic names of the groups present, e.g.
    ``['benzene ring (phenyl, aryl, aromatic)', 'azo (diazene, N=N)']``.
    A scene with no stored bonds gets distance-perceived ones (naming only).
    Empty when there are no bonds either way."""
    bonds = scene.bonds or _perceive_bonds(scene)
    if not bonds:
        return []
    adj = _adjacency(bonds, scene)
    el = {a: at.element for a, at in scene.atoms.items()}
    out: list[str] = []
    if _has_benzene_ring(scene, adj):
        out.append("benzene ring (phenyl, aryl, aromatic)")
    kinds: set[str] = set()
    for a, nbrs in adj.items():
        for b, (order, _) in nbrs.items():
            if a >= b:
                continue
            pair = {el[a], el[b]}
            if pair == {"N"} and _is_double(order):
                kinds.add("azo (diazene, N=N)")
            elif pair == {"C", "N"} and order >= 2.75:
                kinds.add("nitrile (cyano, C#N)")
            elif pair == {"C", "O"} and _is_double(order):
                kinds.add("carbonyl (C=O)")
        if el[a] == "N" and sum(1 for b in nbrs if el[b] == "O") >= 2:
            kinds.add("nitro (NO2)")
        if el[a] == "O" and sorted(el[b] for b in nbrs) == ["C", "H"]:
            kinds.add("hydroxyl (OH)")
    return out + sorted(kinds)

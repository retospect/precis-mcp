"""Systematic fragment names derived from a scene's bond graph.

A structure is titled with a trivial name (``azobenzene``) but agents search
by systematic fragments (``diazene phenyl``). The card text appends the
names of the functional groups / rings found in the graph, so lexical and
semantic search both reach the trivial-name card (gr334774). Pure topology:
no external lookup, no stored synonym table.
"""

from __future__ import annotations

from precis.structure.scene import Scene

_AROMATIC_ORDER = 1.5

_Adj = dict[str, dict[str, tuple[float, str]]]


def _is_double(order: float) -> bool:
    return abs(order - 2.0) < 0.25


def _adjacency(scene: Scene) -> _Adj:
    adj: _Adj = {a: {} for a in scene.atoms}
    for b in scene.bonds:
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
    Empty when the scene carries no bonds."""
    if not scene.bonds:
        return []
    adj = _adjacency(scene)
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

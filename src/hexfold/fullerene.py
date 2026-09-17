"""C60 as a precomputed closed net: the truncated-icosahedron graph.

v1 ships C60 only (SPEC section 3); no Goldberg generality.  The graph is
derived once, deterministically, from the icosahedron: two atoms per edge,
bonds along the edge and around each vertex's pentagon.  ``coords`` gives
the closed-form truncated-icosahedron geometry used to score ``stick``.
"""

from __future__ import annotations

import math

import numpy as np

from .lattice import Site

_T = (1.0 + math.sqrt(5.0)) / 2.0


def _icosahedron() -> np.ndarray:
    raw = []
    for s1 in (-1.0, 1.0):
        for s2 in (-1.0, 1.0):
            raw.append([0.0, s1, s2 * _T])
            raw.append([s1, s2 * _T, 0.0])
            raw.append([s1 * _T, 0.0, s2])
    v: np.ndarray = np.array(raw)
    out: np.ndarray = v / np.linalg.norm(v[0])
    return out


def _edges(v: np.ndarray) -> list[tuple[int, int]]:
    d = np.linalg.norm(v[:, None] - v[None, :], axis=-1)
    dmin = d[d > 0].min()
    edges = set()
    for i in range(12):
        for j in range(i + 1, 12):
            if d[i, j] < dmin + 1e-9:
                edges.add((i, j))
    return sorted(edges)


def _faces(v: np.ndarray, edges: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    eset = {frozenset(e) for e in edges}
    out: set[tuple[int, int, int]] = set()
    for i, j in edges:
        for k in range(12):
            if k in (i, j):
                continue
            if frozenset((i, k)) in eset and frozenset((j, k)) in eset:
                a_, b_, c_ = sorted((i, j, k))
                out.add((a_, b_, c_))
    return sorted(out)


class C60Data:
    """Atoms, bonds and rings of the truncated icosahedron."""

    def __init__(self) -> None:
        v = _icosahedron()
        edges = _edges(v)
        faces = _faces(v, edges)
        # atoms: (edge_index, end) -> point at (2/3 nearer end + 1/3 far end)
        self.atom_pos: dict[tuple[int, int], np.ndarray] = {}
        order: list[tuple[int, int]] = []
        for ei, (i, j) in enumerate(edges):
            for end in (0, 1):
                a, b = (i, j) if end == 0 else (j, i)
                self.atom_pos[(ei, end)] = (2.0 * v[a] + v[b]) / 3.0
                order.append((ei, end))
        self.order = order
        # bonds along edges
        bonds: set[frozenset[tuple[int, int]]] = {
            frozenset(((e, 0), (e, 1))) for e in range(len(edges))
        }
        # pentagon cycles: at each vertex, cyclic order of incident edges
        self.pentagons: list[list[tuple[int, int]]] = []
        incident: dict[int, list[int]] = {i: [] for i in range(12)}
        for ei, (i, j) in enumerate(edges):
            incident[i].append(ei)
            incident[j].append(ei)
        edge_of = {e: idx for idx, e in enumerate(edges)}
        for vtx in range(12):
            eds = incident[vtx]
            # project edge directions onto plane perp to v, sort by angle
            n = v[vtx] / np.linalg.norm(v[vtx])
            ref = np.array([1.0, 0.0, 0.0])
            if abs(n @ ref) > 0.9:
                ref = np.array([0.0, 1.0, 0.0])
            e1 = ref - (ref @ n) * n
            e1 /= np.linalg.norm(e1)
            e2 = np.cross(n, e1)

            def key(
                ei: int,
                vtx: int = vtx,
                n: np.ndarray = n,
                e1: np.ndarray = e1,
                e2: np.ndarray = e2,
            ) -> float:
                i, j = edges[ei]
                other = j if i == vtx else i
                d = v[other] - v[vtx]
                d = d - (d @ n) * n
                return math.atan2(d @ e2, d @ e1)

            cyc = sorted(eds, key=key)
            ring = [(ei, 0 if edges[ei][0] == vtx else 1) for ei in cyc]
            self.pentagons.append(ring)
            for a in range(5):
                bonds.add(frozenset((ring[a], ring[(a + 1) % 5])))
        # hexagon rings: two atoms per face edge
        self.hexagons: list[list[tuple[int, int]]] = []
        for f in faces:
            ring = []
            for a, b in ((f[0], f[1]), (f[1], f[2]), (f[2], f[0])):
                ei = edge_of[(min(a, b), max(a, b))]
                end_a = 0 if edges[ei][0] == a else 1
                ring += [(ei, end_a), (ei, 1 - end_a)]
            self.hexagons.append(ring)
        pairs = [tuple(sorted(e)) for e in bonds]
        self.bonds: list[tuple[tuple[int, int], tuple[int, int]]] = sorted(
            (pr[0], pr[1]) for pr in pairs
        )

    def sites(self) -> dict[tuple[int, int], Site]:
        """Atom key -> Site(u=index in ``order``, v=0, s=sublattice)."""

        order = self.order
        color: dict[tuple[int, int], int] = {order[0]: 0}
        stack = [order[0]]
        adjacency: dict[tuple[int, int], list[tuple[int, int]]] = {a: [] for a in order}
        for e in self.bonds:
            a, b = tuple(e)
            adjacency[a].append(b)
            adjacency[b].append(a)
        while stack:
            a = stack.pop()
            for b in adjacency[a]:
                if b not in color:
                    color[b] = 1 - color[a]
                    stack.append(b)
        return {a: Site(i, 0, color[a]) for i, a in enumerate(order)}

    def bonds66(self) -> list[tuple[tuple[int, int], tuple[int, int]]]:
        """Bonds shared by two hexagons (the [2+2]-reactive 6-6 bonds)."""
        rings = [frozenset(r) for r in self.pentagons + self.hexagons]
        sizes = {frozenset(r): len(r) for r in self.pentagons + self.hexagons}
        out = []
        for b in self.bonds:
            e = frozenset(b)
            adj = [sizes[r] for r in rings if e <= r]
            if sorted(adj) == [6, 6]:
                out.append(b)
        return out

    def coords(self, bond: float = 1.42) -> np.ndarray:
        """(60, 3) closed-form positions scaled so bonds equal ``bond``."""
        pts: np.ndarray = np.array([self.atom_pos[a] for a in self.order])
        i = self.order.index(self.bonds[0][0])
        j = self.order.index(self.bonds[0][1])
        scale = bond / np.linalg.norm(pts[i] - pts[j])
        out: np.ndarray = pts * scale
        return out

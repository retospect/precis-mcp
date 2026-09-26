"""Ring perception + Taubin smoothing over a bare bond graph — the pure
geometry core behind gr450675's atomic↔smooth viewer slider.

No store, no web, no ``se``/``structure`` imports (the package docstring's
"no store, no I/O, no web" boundary): every function here takes plain
``coords``/``bonds`` arrays and returns plain arrays, so an ``se`` atomic
block, a bare ``structure`` cell, or a later ``cad`` view can all reuse it
identically — the same "adapter builds it, this module never reaches for
the store" split :mod:`.stickfig` already draws.

**Ring perception** (:func:`ring_faces`) is the "smallest ring containing
this edge" construction restricted to a sp² (max-degree-3) bond graph:
remove the edge, BFS the shortest alternate path between its two atoms,
and keep EVERY such minimal-length path (plural, deliberately — a 6:6
fullerene bond or an interior nanotube bond borders two faces of the SAME
size, so both must survive, not just whichever BFS visits first). This is
not general SSSR (it assumes degree ≤ 3, true of every sp² generator this
slice targets); a higher-valence graph would need a real ring-perception
library instead.

**Smoothing** (:func:`smooth_sheet`) is Taubin's λ|μ scheme (Taubin 1995),
not plain Laplacian averaging: a plain Laplacian smooth shrinks a closed
sheet (C60's shell would visibly collapse inward over 20 iterations)
because the "move toward the neighbour mean" step is a low-pass filter
with no gain compensation. Taubin alternates a shrinking pass (``λ > 0``)
with a compensating *inflating* pass (``μ < 0``, ``|μ| > λ``) chosen so
the net transfer function is ~1 at low frequencies (no shrink) while
still killing high-frequency noise — the standard mesh-fairing fix, ported
here onto a bond graph instead of a triangle mesh's edge graph (the
underlying Laplacian construction — neighbour mean minus self — is the
same operator either way).

**Deviation** (:func:`deviation`) is the per-atom aberration proxy this
slice needs: displacement from an atom's own position to where the
Taubin-smoothed sheet puts it. Docstring honesty, per the build brief:
the "idealized scaffold" here is the smoothed sheet through the *atoms
themselves* — a self-referential proxy, not the design's declared level
set. When a block carries a real scaffold surface (a tpms/hexfold smooth
section, built in a parallel slice) a later cut substitutes distance-to-
that-surface for distance-to-smoothed-self; :func:`deviation` takes plain
coordinate arrays precisely so that swap only touches its caller, not this
function's signature.

**Mesh** (:func:`sheet_mesh`) fan-triangulates each ring for rendering;
per-vertex colour (e.g. by :func:`deviation`) is the caller's job since
this module has no colour opinion (:mod:`.stickfig`'s same split).
"""

from __future__ import annotations

from collections import deque

import numpy as np
from numpy.typing import NDArray

__all__ = ["deviation", "ring_faces", "sheet_mesh", "smooth_sheet"]


def _adjacency(n_atoms: int, bonds: list[tuple[int, int]]) -> list[list[int]]:
    adj: list[list[int]] = [[] for _ in range(n_atoms)]
    for i, j in bonds:
        if j not in adj[i]:
            adj[i].append(j)
        if i not in adj[j]:
            adj[j].append(i)
    return adj


def _shortest_alt_paths(
    adj: list[list[int]], u: int, v: int, max_len: int
) -> list[list[int]]:
    """Every shortest ``u -> v`` path in ``adj`` that does NOT use the
    direct ``u-v`` edge as its first hop, capped at ``max_len - 1`` edges
    (so the resulting ring, path + the closing ``v-u`` edge, has at most
    ``max_len`` atoms). ``[]`` when no such path exists within the cap —
    an honest "no ring here" for a genuinely open edge (a nanotube rim),
    not a fabricated oversized ring."""
    dist: dict[int, int] = {u: 0}
    preds: dict[int, list[int]] = {}
    dq: deque[int] = deque([u])
    while dq:
        cur = dq.popleft()
        if dist[cur] >= max_len - 1:
            continue
        for nxt in adj[cur]:
            if cur == u and nxt == v:
                continue  # the direct edge itself never counts as the "alternate" path
            nd = dist[cur] + 1
            if nxt not in dist:
                dist[nxt] = nd
                preds[nxt] = [cur]
                dq.append(nxt)
            elif dist[nxt] == nd:
                preds[nxt].append(cur)
    if v not in dist:
        return []

    def backtrack(node: int) -> list[list[int]]:
        if node == u:
            return [[u]]
        out: list[list[int]] = []
        for p in preds.get(node, []):
            for sub in backtrack(p):
                out.append([*sub, node])
        return out

    return backtrack(v)


def _canonical_ring(ring: tuple[int, ...]) -> tuple[int, ...]:
    """The lexicographically smallest rotation of ``ring`` or its reverse —
    a ring found from two different bonds (or in either winding direction)
    canonicalizes to the same tuple, so :func:`ring_faces`'s dedup-by-value
    is deterministic regardless of discovery order."""
    n = len(ring)
    candidates = []
    for seq in (ring, tuple(reversed(ring))):
        for start in range(n):
            candidates.append(seq[start:] + seq[:start])
    return min(candidates)


def ring_faces(
    n_atoms: int, bonds: list[tuple[int, int]], max_ring: int = 8
) -> list[tuple[int, ...]]:
    """Every smallest ring (size ``<= max_ring``) bordering each bond of an
    sp²-like (max degree 3) bond graph, deduplicated, each ring's atoms in
    cyclic order (module docstring). Deterministic: the returned list is
    sorted by each ring's own canonical tuple."""
    adj = _adjacency(n_atoms, bonds)
    edges = sorted({(min(a, b), max(a, b)) for a, b in bonds})
    rings: dict[tuple[int, ...], tuple[int, ...]] = {}
    for u, v in edges:
        for path in _shortest_alt_paths(adj, u, v, max_ring):
            canon = _canonical_ring(tuple(path))
            rings[canon] = canon
    return sorted(rings.values())


def smooth_sheet(
    coords: NDArray[np.floating],
    bonds: list[tuple[int, int]],
    *,
    iters: int = 20,
    lam: float = 0.5,
    mu: float = -0.53,
) -> NDArray[np.float64]:
    """Taubin λ|μ smoothing (module docstring) of ``coords`` over the bond
    graph's neighbour-mean Laplacian, ``iters`` λ/μ pairs (``2 * iters``
    total passes). Taubin, not plain Laplacian averaging, so a closed sp²
    shell does not shrink."""
    xyz = np.asarray(coords, dtype=np.float64)
    n = len(xyz)
    adj = _adjacency(n, bonds)

    def _pass(x: NDArray[np.float64], factor: float) -> NDArray[np.float64]:
        out = x.copy()
        for i, nbrs in enumerate(adj):
            if not nbrs:
                continue
            mean = x[nbrs].mean(axis=0)
            out[i] = x[i] + factor * (mean - x[i])
        return out

    x = xyz.copy()
    for _ in range(iters):
        x = _pass(x, lam)
        x = _pass(x, mu)
    return x


def deviation(
    coords: NDArray[np.floating], coords_smooth: NDArray[np.floating]
) -> NDArray[np.float64]:
    """Per-atom displacement magnitude, ``coords`` -> ``coords_smooth`` (the
    "aberration" proxy, in ``coords``'s own units — module docstring)."""
    a = np.asarray(coords, dtype=np.float64)
    b = np.asarray(coords_smooth, dtype=np.float64)
    return np.linalg.norm(a - b, axis=1)


def sheet_mesh(
    coords: NDArray[np.floating], faces: list[tuple[int, ...]]
) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    """Fan-triangulate each ``faces`` polygon at ``coords`` — ``verts`` is
    ``coords`` unchanged (so a per-atom scalar, e.g. :func:`deviation`,
    indexes it directly; colouring it is the caller's job, module
    docstring), ``tris`` an ``(M, 3)`` index array into it."""
    verts = np.asarray(coords, dtype=np.float64)
    tris: list[tuple[int, int, int]] = []
    for ring in faces:
        v0 = ring[0]
        for k in range(1, len(ring) - 1):
            tris.append((v0, ring[k], ring[k + 1]))
    tri_arr = (
        np.array(tris, dtype=np.int64) if tris else np.zeros((0, 3), dtype=np.int64)
    )
    return verts, tri_arr

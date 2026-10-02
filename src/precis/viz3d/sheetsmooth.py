"""Ring perception + Taubin smoothing over a bare bond graph — the pure
geometry core behind gr450675's atomic↔smooth viewer slider.

No store, no web, no ``se``/``structure`` imports (the package docstring's
"no store, no I/O, no web" boundary): every function here takes plain
``coords``/``bonds`` arrays and returns plain arrays, so an ``se`` atomic
block, a bare ``structure`` cell, or a later ``cad`` view can all reuse it
identically — the same "adapter builds it, this module never reaches for
the store" split :mod:`.stickfig` already draws.

**Ring perception** (:func:`ring_faces`) is the "smallest ring through
this bond angle" construction on a sp² (max-degree-3) bond graph: for
every atom ``v`` and every pair of its neighbours ``u, w``, BFS the
shortest ``w -> u`` path that avoids ``v`` and close it through ``v``.
Every angle of a degree-3 net is a corner of exactly one face, so this
finds every face — including a heptagon whose every bond also borders a
smaller hexagon or pentagon (gr461146: the older "smallest ring per BOND"
rule dropped every heptagon of a smooth drum, leaving holes in the
viewer's smoothed surface). Ties keep EVERY minimal path (a 6:6 fullerene
bond borders two equal rings). Not general SSSR (it assumes degree ≤ 3,
true of every sp² generator this slice targets); a higher-valence graph
would need a real ring-perception library instead.

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


def _shortest_paths_avoiding(
    adj: list[list[int]], src: int, dst: int, avoid: int, max_edges: int
) -> list[list[int]]:
    """Every shortest ``src -> dst`` path in ``adj`` that never visits
    ``avoid``, at most ``max_edges`` edges long. ``[]`` when none exists
    within the cap — an honest "no ring here" for a genuinely open corner
    (a nanotube rim), not a fabricated oversized ring."""
    dist: dict[int, int] = {src: 0}
    preds: dict[int, list[int]] = {}
    dq: deque[int] = deque([src])
    while dq:
        cur = dq.popleft()
        if cur == dst or dist[cur] >= max_edges:
            continue
        for nxt in adj[cur]:
            if nxt == avoid:
                continue
            nd = dist[cur] + 1
            if nxt not in dist:
                dist[nxt] = nd
                preds[nxt] = [cur]
                dq.append(nxt)
            elif dist[nxt] == nd:
                preds[nxt].append(cur)
    if dst not in dist:
        return []

    def backtrack(node: int) -> list[list[int]]:
        if node == src:
            return [[src]]
        out: list[list[int]] = []
        for p in preds.get(node, []):
            for sub in backtrack(p):
                out.append([*sub, node])
        return out

    return backtrack(dst)


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
    """Every smallest ring (size ``<= max_ring``) through each bond angle
    of an sp²-like (max degree 3) bond graph, deduplicated, each ring's
    atoms in cyclic order (module docstring). Deterministic: the returned
    list is sorted by each ring's own canonical tuple."""
    adj = _adjacency(n_atoms, bonds)
    rings: dict[tuple[int, ...], tuple[int, ...]] = {}
    for v, nbrs in enumerate(adj):
        for a in range(len(nbrs)):
            for b in range(a + 1, len(nbrs)):
                u, w = nbrs[a], nbrs[b]
                for path in _shortest_paths_avoiding(adj, w, u, v, max_ring - 2):
                    canon = _canonical_ring((v, *path))
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

    # Neighbour structure built once: flat (atom, neighbour) edge lists over
    # the de-duplicated adjacency; an atom with no neighbour stays put.
    deg = np.fromiter((len(nb) for nb in adj), dtype=np.int64, count=n)
    src = np.repeat(np.arange(n, dtype=np.int64), deg)
    dst = np.fromiter(
        (j for nb in adj for j in nb), dtype=np.int64, count=int(deg.sum())
    )
    has_nbrs = deg > 0
    inv_deg = np.zeros(n, dtype=np.float64)
    inv_deg[has_nbrs] = 1.0 / deg[has_nbrs]

    def _pass(x: NDArray[np.float64], factor: float) -> NDArray[np.float64]:
        out = x.copy()
        if not len(dst):
            return out
        acc = np.zeros_like(x)
        np.add.at(acc, src, x[dst])
        mean = acc * inv_deg[:, None]
        out[has_nbrs] = x[has_nbrs] + factor * (mean[has_nbrs] - x[has_nbrs])
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

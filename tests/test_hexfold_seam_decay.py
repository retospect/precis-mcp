"""Seam decay length on the geo rung -- the number behind the hierarchical
resolved block (docs/backlog/diamondoid-pattern-language.md, "Three-zone
resolved block"): relax a part once, freeze its interior, re-relax only a
seam radius around each join.  That scheme holds iff the perturbation a
join makes on a part's geometry is localised to a few shells of atoms
around the fused rim.

Measured here on hexfold tubes with ``precis.structure.georelax.relax_graph``
(bond springs at the covalent-radius sum, VSEPR angle term, non-bond
repulsion; sp2 targets): the same instance ``a`` relaxed free and relaxed
fused tip-to-tip onto an identical ``b``, compared shell by shell in graph
distance from the fused rim.  Two rigid-motion-invariant measures --
per-bond length change and per-atom bond-angle change -- so a global
breathing/length mode of the longer composite (visible as a slow rise in
Kabsch-aligned displacement toward the far free end) does not masquerade
as seam strain.

Geo rung only: the spring model has no rim reconstruction and no bond
alternation, so the *amplitude* at the rim (0.013-0.03 A here) is a lower
bound on what an energy rung would show; the *decay length* is set by the
lattice's elastic screening, which the spring model does carry.  The
zigzag decay is a thin-shell edge mode and its length grows with tube
length until the two ends stop interacting: (12,0) measured 5 shells at
len=4 and 8 shells at len=6 (about 4 hexagon rows).  The short tubes used
here for runtime therefore pin *lower bounds*; the full tables live in the
diamondoid backlog item under "Seam decay, measured".

Lives in top-level ``tests/`` because it imports both ``hexfold`` and
``precis.structure`` (``tests/hexfold/`` stays hexfold-only).
"""

from __future__ import annotations

from collections import deque

import numpy as np
import pytest

from hexfold.build import Net, build
from hexfold.stick import stick
from precis.structure.georelax import relax_graph

pytestmark = pytest.mark.slow

_TOL = 1e-4
_ITERS = 4000
#: bond-length change (A) and bond-angle change (deg) below which a shell
#: counts as unperturbed; both are ~10x the converged relaxer's own noise
#: floor on the far, untouched rim.
_DL_A = 0.002
_DTHETA_DEG = 0.15


def _relaxed(net: Net) -> np.ndarray:
    coords = stick(net).astype(np.float64)
    elements = [a.element for a in net.atoms]
    bonds = [(i, j) for i, j, _ in net.bonds]
    trace = relax_graph(
        elements, coords, bonds, set(), hybridizations="sp2", iters=_ITERS, tol=_TOL
    )
    assert trace.converged, ("relaxer did not converge", trace.n_steps, trace.curve[-1])
    return coords


def _adjacency(net: Net) -> dict[int, list[int]]:
    adj: dict[int, list[int]] = {}
    for i, j, _ in net.bonds:
        adj.setdefault(i, []).append(j)
        adj.setdefault(j, []).append(i)
    return adj


def _shells_from(rim: set[int], adj: dict[int, list[int]]) -> dict[int, int]:
    dist = dict.fromkeys(rim, 0)
    queue = deque(rim)
    while queue:
        u = queue.popleft()
        for v in adj[u]:
            if v not in dist:
                dist[v] = dist[u] + 1
                queue.append(v)
    return dist


def _angles_at(c: np.ndarray, k: int, nb: list[int]) -> list[float]:
    out = []
    for a in range(len(nb)):
        for b in range(a + 1, len(nb)):
            u = c[nb[a]] - c[k]
            v = c[nb[b]] - c[k]
            cosang = u @ v / (np.linalg.norm(u) * np.linalg.norm(v))
            out.append(float(np.degrees(np.arccos(np.clip(cosang, -1.0, 1.0)))))
    return out


def _seam_decay_table(
    n: int, m: int, length: int
) -> dict[int, tuple[float, float, float]]:
    """shell -> (max |d| after Kabsch, max |dl|, max |dtheta|) over instance
    ``a``'s atoms/bonds at that graph distance from the fused rim."""
    from precis.structure.georelax import kabsch_align

    free = build(f"hexfold 0.2\na: tube({n},{m}, len={length})\n")
    fused = build(
        f"hexfold 0.2\na: tube({n},{m}, len={length})\n"
        f"b: tube({n},{m}, len={length})\na.out --fuse k=0--> b.in\n"
    )
    # hexfold assigns ordinals by (instance, path): instance a's atoms are
    # the same ordinals in both nets -- checked, not assumed
    free_a = {a.path: a.ord for a in free.atoms if a.instance == "a"}
    fused_a = {a.path: a.ord for a in fused.atoms if a.instance == "a"}
    assert free_a == fused_a
    ords = sorted(free_a.values())

    cf = _relaxed(free)
    cg = _relaxed(fused)
    adj = _adjacency(free)
    dist = _shells_from(set(dict(free.ports)["out"].atoms), adj)

    aligned, _rmsd = kabsch_align(cg[ords], cf[ords])
    disp = np.linalg.norm(aligned - cf[ords], axis=1)

    table: dict[int, list[float]] = {}
    for k, o in enumerate(ords):
        s = dist[o]
        row = table.setdefault(s, [0.0, 0.0, 0.0])
        row[0] = max(row[0], float(disp[k]))
        th_f = _angles_at(cf, o, adj[o])
        th_g = _angles_at(cg, o, adj[o])
        row[2] = max(row[2], max(abs(x - y) for x, y in zip(th_f, th_g, strict=True)))
    for i, j, _ in free.bonds:
        s = min(dist[i], dist[j])
        dl = abs(
            float(np.linalg.norm(cf[i] - cf[j])) - float(np.linalg.norm(cg[i] - cg[j]))
        )
        table[s][1] = max(table[s][1], dl)
    return {s: (r[0], r[1], r[2]) for s, r in table.items()}


def _assert_localised(
    table: dict[int, tuple[float, float, float]], seam_shell: int
) -> None:
    shells = sorted(table)
    # the join is felt: the rim shell's bond angles changed by more than the noise
    assert table[0][2] > 0.1, table[0]
    # ... and every shell beyond the seam radius is unperturbed on both
    # rigid-invariant measures (the far free rim included)
    beyond = [s for s in shells if s > seam_shell]
    assert beyond, "tube too short to have an interior beyond the seam shell"
    bad = {
        s: table[s]
        for s in beyond
        if table[s][1] >= _DL_A or table[s][2] >= _DTHETA_DEG
    }
    assert not bad, {s: tuple(round(x, 4) for x in v) for s, v in bad.items()}


def test_zigzag_seam_perturbation_dies_within_five_shells() -> None:
    """(8,0) zigzag at len=3: graph shells are half a hexagon row each along
    the axis, so five shells is about 2.5 rows (~5 A).  A lower bound -- the
    (12,0) len=6 measurement decays over 8 shells (module docstring)."""
    table = _seam_decay_table(8, 0, 3)
    _assert_localised(table, seam_shell=5)


def test_armchair_seam_perturbation_dies_within_two_shells() -> None:
    """(5,5) armchair: one graph shell per hexagon row; the perturbation is
    below threshold from the first interior shell on (measured 1; pinned
    at 2 for margin)."""
    table = _seam_decay_table(5, 5, 3)
    _assert_localised(table, seam_shell=2)

"""stick(net) -> (N, 3) float64 Angstrom preview geometry (SPEC section 11).

Relax seed comes from ``Net.seed3``: a closed-form embedding for fullerene
tables, an analytic cylinder/cone wrap for tube/cone patches, a flat lattice
embedding with a small fixed out-of-plane perturbation for sheets, and the
spectral seed (three adjacency eigenvectors below the Perron vector, signs
fixed largest-component-positive) when no lattice embedding exists.

Then a vectorised spring relaxation — bond springs to sigma, ring-ideal
angle springs (as chord springs between the two neighbours of each ring
vertex), soft non-bonded repulsion — as plain gradient descent with a fixed
step and a fixed iteration count.  Accumulation order is fixed via
``np.add.at`` over index arrays, so the same input gives the same bytes.
This is a preview, not physics.
"""

from __future__ import annotations

import math

import numpy as np

from .build import Net
from .lattice import ideal_angle_deg

_ITERS = 3000
_DT = 0.05
_K_BOND = 1.0
_K_ANGLE = 0.5
_K_REP = 0.1
_REP_CUT = 1.3
_REFRESH = 20  # repulsion candidate-pair rebuild period (deterministic)
_REP_MARGIN = 2.0  # candidate pairs within _REP_MARGIN*_REP_CUT*sigma


def _spectral_seed(net: Net) -> np.ndarray:
    n = len(net.atoms)
    a = np.zeros((n, n))
    for i, j, _ in net.bonds:
        a[i, j] = a[j, i] = 1.0
    _, vecs = np.linalg.eigh(a)
    # three eigenvectors below the Perron (largest) one
    idx = [n - 2, n - 3, n - 4] if n >= 4 else list(range(n - 2, -1, -1))
    seed = np.zeros((n, 3))
    for k, i in enumerate(idx):
        v = vecs[:, i].copy()
        j = int(np.argmax(np.abs(v)))
        if v[j] < 0:
            v = -v
        seed[:, k] = v
    # rescale median bond length to sigma
    bl = [np.linalg.norm(seed[i] - seed[j]) for i, j, _ in net.bonds]
    med = float(np.median(bl)) or 1.0
    return seed * (net.lattice.sigma_A / med)


def _angle_springs(net: Net) -> list[tuple[int, int, float]]:
    """Chord springs enforcing the ideal interior angle at each ring vertex
    (ring ideal for sp2, the sp3 tetrahedral angle at an sp3 vertex)."""
    sig = net.lattice.sigma_A
    hyb = {a.ord: a.hyb for a in net.atoms}
    out = []
    for ring in net.rings:
        n = len(ring)
        for i in range(n):
            v = ring[i]
            ideal = math.radians(ideal_angle_deg(n, hyb.get(v, "")))
            chord = 2.0 * sig * math.sin(ideal / 2.0)
            out.append((ring[(i - 1) % n], ring[(i + 1) % n], chord))
    return out


def _spring_forces(
    pos: np.ndarray, ii: np.ndarray, jj: np.ndarray, rest: np.ndarray, k: float
) -> np.ndarray:
    d = pos[jj] - pos[ii]
    r = np.linalg.norm(d, axis=1)
    r = np.where(r == 0.0, 1e-9, r)
    f = k * (r - rest)[:, None] * d / r[:, None]
    out = np.zeros_like(pos)
    np.add.at(out, ii, f)
    np.add.at(out, jj, -f)
    return out


def _rep_pairs(pos: np.ndarray, bonded: np.ndarray, lim: float) -> np.ndarray:
    """Candidate pairs for soft repulsion: closer than ``lim`` and unbonded."""
    d = pos[None, :, :] - pos[:, None, :]
    r = np.linalg.norm(d, axis=2)
    mask = (r < lim) & (r > 0.0) & ~bonded
    ii, jj = np.nonzero(np.triu(mask))
    return np.stack([ii, jj], axis=1)


def stick_info(net: Net) -> tuple[np.ndarray, float]:
    """Relaxed stick coordinates and the final max force magnitude."""
    if net.seed3 is not None:
        # primitives with a known embedding (closed-form, cylinder, cone,
        # flat lattice) seed from it; the spring stage is identical
        if len(net.seed3) != len(net.atoms):
            raise ValueError(
                f"seed3 has {len(net.seed3)} rows for {len(net.atoms)} atoms"
            )
        pos = np.array(net.seed3, dtype=np.float64)
    else:
        pos = _spectral_seed(net)
    sig = net.lattice.sigma_A
    n = len(net.atoms)

    bonds = np.array([(i, j) for i, j, _ in net.bonds], dtype=np.int64)
    # per-bond rest length: sigma_CH when an endpoint is a termination
    # atom (element outside the lattice pair), sigma otherwise
    lat_el = set(net.lattice.elements)
    brest = np.array(
        [
            sig
            if net.atoms[i].element in lat_el and net.atoms[j].element in lat_el
            else net.lattice.sigma_CH_A
            for i, j, _ in net.bonds
        ],
        dtype=np.float64,
    )
    springs = np.array(_angle_springs(net), dtype=np.float64)
    bonded = np.zeros((n, n), dtype=bool)
    bonded[bonds[:, 0], bonds[:, 1]] = True
    bonded[bonds[:, 1], bonds[:, 0]] = True
    si = springs[:, 0].astype(np.int64)
    sj = springs[:, 1].astype(np.int64)
    bonded[si, sj] = True
    bonded[sj, si] = True
    srest = springs[:, 2]

    pairs = _rep_pairs(pos, bonded, _REP_MARGIN * _REP_CUT * sig)
    f = np.zeros_like(pos)
    for it in range(_ITERS):
        if it % _REFRESH == 0:
            pairs = _rep_pairs(pos, bonded, _REP_MARGIN * _REP_CUT * sig)
        f = _spring_forces(pos, bonds[:, 0], bonds[:, 1], brest, _K_BOND)
        f += _spring_forces(pos, si, sj, srest, _K_ANGLE)
        if len(pairs):
            pi, pj = pairs[:, 0], pairs[:, 1]
            d = pos[pj] - pos[pi]
            r = np.linalg.norm(d, axis=1)
            near = r < _REP_CUT * sig
            pi, pj, d = pi[near], pj[near], d[near]
            r = r[near]
            r = np.where(r == 0.0, 1e-9, r)
            rf = -_K_REP * (_REP_CUT * sig - r)[:, None] * d / r[:, None]
            np.add.at(f, pi, rf)
            np.add.at(f, pj, -rf)
        pos += _DT * f
    max_force = float(np.linalg.norm(f, axis=1).max()) if len(f) else 0.0
    return pos, max_force


def stick(net: Net) -> np.ndarray:
    pos, _ = stick_info(net)
    return pos

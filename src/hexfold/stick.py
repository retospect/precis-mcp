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

The relaxation loop itself is :func:`stick_relax_pinned` (explicit
positions/bonds/rest-lengths/springs in, an optional per-atom 0/1 movable
mask), so :mod:`hexfold.join`'s seam re-relax can reuse it over a
composite with everything outside the seam radius pinned; ``stick_info``
is just its no-mask caller plumbing the seed/rest-length/angle-spring setup
from a ``Net``.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np

from .build import Net
from .lattice import ideal_angle_deg

_ITERS = 3000
_DT = 0.05
_K_BOND = 1.0
_K_ANGLE = 0.5
#: Soft non-bonded repulsion: force k*(cut - r) on pairs more than two bonds
#: apart closer than cut*sigma = 1.99 A at sigma 1.42.  The old 0.1 / 1.3
#: (1.85 A) left bud/host junction pairs at 1.2-1.75 A, which ``geom.clash``
#: reports and ``se structure validate`` counts as extra neighbours
#: (over_valence, gr459567).  At 2.0 / 1.4 the skill's tube(10,10) + C60
#: [9-6], nanobud_87/96 and the hexa-nanobud pillar have no pair under 1.8 A.
#: The price is a frustrated junction bond stretched further (worst bond
#: +0.29 -> +0.40 A, [8-7] +0.33 -> +0.54 A); angles, tube55 and the other
#: non-junction examples are unchanged.  Not the rest angles: capping each vertex's
#: ideal-angle sum at 360 deg made the junction worse (19 pairs, 83 deg).
#: Opening the pairs to 1.8 A needs about 0.1 A of bond stretch whatever the
#: constants (swept k 0.1-8, cut 1.3-1.7); stiffer or
#: wider settings only add strain (cut 1.7: worst bond +0.5 to +0.7 A).
_K_REP = 2.0
_REP_CUT = 1.4
_REFRESH = 20  # repulsion candidate-pair rebuild period (deterministic)
_REP_MARGIN = 2.0  # candidate pairs within _REP_MARGIN*_REP_CUT*sigma
_TETHER_EVERY = 25  # iterations between surface-foot refreshes

#: ``positions (N,3) -> (foot (N,3), unit normal (N,3))``: where each atom's
#: nearest point on a target surface is, and the surface normal there.
#: Supplied by the caller (e.g. ``precis_surface.deviation.surface_foot``);
#: hexfold never imports precis.
Tether = Callable[[np.ndarray], tuple[np.ndarray, np.ndarray]]


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


def angle_springs(net: Net) -> list[tuple[int, int, float]]:
    """Chord springs enforcing the ideal interior angle at each ring vertex
    (ring ideal for sp2, the sp3 tetrahedral angle at an sp3 vertex), and
    the sp2 angle at a one-bond lattice atom's neighbour."""
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
    # A lattice atom with one bond sits in no ring, so nothing above holds
    # its angle; once the repulsion stopped seeing 1-3 pairs (0.3.5) cone5's
    # dangling rim carbons folded to 41 deg, 0.99 A from their neighbour's
    # other neighbour (gr479847).  Spring each such atom to its neighbour's
    # other neighbours at the sp2 chord.
    lat_el = set(net.lattice.elements)
    nbrs: dict[int, list[int]] = {}
    for i, j, _ in net.bonds:
        nbrs.setdefault(i, []).append(j)
        nbrs.setdefault(j, []).append(i)
    chord_sp2 = 2.0 * sig * math.sin(math.radians(120.0) / 2.0)
    for a in sorted(nbrs):
        if len(nbrs[a]) != 1 or net.atoms[a].element not in lat_el:
            continue
        ctr = nbrs[a][0]
        out.extend((a, x, chord_sp2) for x in nbrs[ctr] if x != a)
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


def stick_relax_pinned(
    pos: np.ndarray,
    bonds: np.ndarray,
    brest: np.ndarray,
    springs: np.ndarray,
    sigma: float,
    *,
    iters: int = _ITERS,
    movable: np.ndarray | None = None,
    tether: Tether | None = None,
    k_tether: float = 0.0,
) -> tuple[np.ndarray, float]:
    """The vectorised spring relaxation loop, factored out of
    :func:`stick_info` so :mod:`hexfold.join` can re-relax only a seam
    radius of a composite (the rest of the plumbing -- seed choice,
    per-bond rest length, ring-chord angle springs -- stays with the
    caller, which already has the net or block to build them from).

    ``pos`` (N,3), ``bonds`` (M,2) int ordinal pairs, ``brest`` (M,) their
    rest lengths, ``springs`` (K,3) ``(i, j, rest)`` ring-chord angle
    springs, ``sigma`` the lattice spacing (repulsion cutoff scale).
    ``movable`` is an optional per-atom 0/1 float mask -- every force this
    atom receives is scaled by it each iteration, so 0 pins the atom
    exactly in place while it still exerts its own spring/repulsion force
    on its neighbours; ``None`` (:func:`stick_info`'s call) is the same as
    an all-ones mask and reproduces its output byte-for-byte.

    ``tether`` with ``k_tether > 0`` holds every atom to a fixed target
    surface by a normal-only spring (it slides along the surface, it does
    not leave it); the feet are re-read from ``tether`` every
    ``_TETHER_EVERY`` iterations, so the loop runs in chunks of that length.
    The surface is the caller's and never moves (docs/backlog/
    hexfold-ideal-surface-then-tile.md, S3). Off by default: without it the
    loop is the single unchunked call it always was.
    """
    pos = pos.copy()
    n = len(pos)
    mv = movable if movable is not None else np.ones(n, dtype=np.float64)
    bonded = np.zeros((n, n), dtype=bool)
    bonded[bonds[:, 0], bonds[:, 1]] = True
    bonded[bonds[:, 1], bonds[:, 0]] = True
    si = springs[:, 0].astype(np.int64)
    sj = springs[:, 1].astype(np.int64)
    bonded[si, sj] = True
    bonded[sj, si] = True
    # 1-3 pairs on a rim sit in no ring, so no chord spring excluded them
    # from the repulsion: two neighbours of one atom are never repelled
    nbrs: list[list[int]] = [[] for _ in range(n)]
    for a, b in bonds.tolist():
        nbrs[a].append(b)
        nbrs[b].append(a)
    for ns in nbrs:
        if len(ns) > 1:
            ix = np.array(ns)
            bonded[np.ix_(ix, ix)] = True
    srest = springs[:, 2]

    # lazy: keeps numba out of `import hexfold`
    from ._stick_kernel import relax_kernel

    pos = np.ascontiguousarray(pos, dtype=np.float64)  # already a copy above
    bonds_c = np.ascontiguousarray(bonds, dtype=np.int64)
    brest_c = np.ascontiguousarray(brest, dtype=np.float64)
    srest_c = np.ascontiguousarray(srest, dtype=np.float64)
    mv_c = np.ascontiguousarray(mv, dtype=np.float64)
    tethered = tether is not None and k_tether > 0.0
    anchor = np.zeros((n, 3))
    anorm = np.zeros((n, 3))
    chunks = (
        [_TETHER_EVERY] * (iters // _TETHER_EVERY)
        + ([iters % _TETHER_EVERY] if iters % _TETHER_EVERY else [])
        if tethered
        else [iters]
    )
    max_force = 0.0
    for chunk in chunks:
        if tethered:
            assert tether is not None
            foot, normal = tether(pos)
            anchor = np.ascontiguousarray(foot, dtype=np.float64)
            anorm = np.ascontiguousarray(normal, dtype=np.float64)
        max_force = float(
            relax_kernel(
                pos,
                bonds_c,
                brest_c,
                si,
                sj,
                srest_c,
                bonded,
                mv_c,
                float(sigma),
                int(chunk),
                _DT,
                _K_BOND,
                _K_ANGLE,
                _K_REP,
                _REP_CUT,
                _REFRESH,
                _REP_MARGIN,
                anchor,
                anorm,
                float(k_tether) if tethered else 0.0,
            )
        )
    return pos, max_force


def stick_info(
    net: Net, tether: Tether | None = None, k_tether: float = 0.0
) -> tuple[np.ndarray, float]:
    """Relaxed stick coordinates and the final max force magnitude.
    ``tether``/``k_tether``: see :func:`stick_relax_pinned`."""
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
    # `.reshape(-1, 3)` keeps :func:`stick_relax_pinned`'s documented
    # (K,3) contract when there are no angle springs at all: a net with no
    # rings gives `angle_springs` an empty list, and `np.array([])` is
    # shape (0,), so the `springs[:, 0]` unpack raised IndexError. A
    # one-period armchair tube is exactly that net — every atom sits on a
    # rim and no ring closes — so `tube(5,5,len=1)` with geometry on
    # reached the caller as "hexfold internal error while compiling the
    # spec", on a spec whose topology is fine (tests/hexfold/
    # test_len1_rims.py). Empty index arrays make the assignments below a
    # no-op, which is the right answer.
    springs = np.array(angle_springs(net), dtype=np.float64).reshape(-1, 3)
    return stick_relax_pinned(
        pos, bonds, brest, springs, sig, tether=tether, k_tether=k_tether
    )


def stick(net: Net, tether: Tether | None = None, k_tether: float = 0.0) -> np.ndarray:
    pos, _ = stick_info(net, tether=tether, k_tether=k_tether)
    return pos

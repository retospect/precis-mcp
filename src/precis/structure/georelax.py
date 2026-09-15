"""Graph-first geometry — the shared relax core behind the `geo` rung
(`relax.py`) and generators that build a structure from its bond graph
first, coordinates second (docs/backlog/se-nanobud-graph.md §1).

Promoted out of `precis_se.atomic.generators.sugars` (that generator's
`_relax`/`_angle_triples`/`_angle_theta_gradients`, the cyclodextrin
fallback path's post-build cleanup) — the physics is unchanged, only
generalized from a hard-coded sp3 angle target to a per-atom
hybridization-aware one (:func:`precis.structure.vsepr.ideal_angle`).
`sugars.py` now imports :func:`relax_graph` under its old private name so
its own behavior/tests stay byte-identical; the `geo` relax rung
(`relax.py::_relax_geo`) is the second, hybridization-aware consumer.

Three independent pieces, each usable on its own:

- :func:`relax_graph` — the bond-spring + non-bond-repulsion +
  VSEPR-angle-restoring cleanup pass, in place over a plain
  ``(elements, coords, bonds, pinned)`` tuple (no ``Scene`` dependency —
  callers translate their own representation in/out, same discipline the
  relax ladder's other rungs already follow for ASE).
- :func:`embed_from_graph` — seeds 3D coordinates from a bond graph ALONE
  (no coordinates needed at all), via the Manolopoulos-Fowler "topological
  coordinates" construction (three adjacency-matrix eigenvectors, skipping
  the trivial/Perron one) — the graph-surgery generators' escape from
  needing *any* starting geometry for a newly-stitched region.
- :func:`register` — post-relax canonical rigid framing (anchor at the
  origin, one bond along +x, a deterministic-sign plane normal along +z) —
  never used to pin anything mid-relax, purely a presentation-layer
  re-framing run once relax has converged.
- :func:`kabsch_align` (+ the private :func:`_kabsch_transform` it wraps) —
  the general rigid-alignment primitive both :func:`embed_from_graph`'s
  partial-embed continuity and any caller comparing two geometries up to
  rotation/translation need; no Kabsch helper existed in-tree before this.

Unit enclave (`precis.structure` package docstring): Å-native throughout,
like every other module in this package — no SI conversion happens here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from precis.structure.elements import covalent_radius
from precis.structure.vsepr import ideal_angle

#: Default hybridization for any atom the caller doesn't otherwise label —
#: `sugars.py`'s cyclodextrin family is all-sp3, so this default preserves
#: its exact original behavior when a caller passes no `hybridizations=` at
#: all (the common case, and the one the byte-identical-sugars contract
#: depends on).
DEFAULT_HYBRIDIZATION = "sp3"

#: Pass count / step sizes — `sugars.py`'s own tuned defaults (that
#: module's docstring has the empirical basis: small steps over many
#: iterations converges reliably, larger steps oscillate/diverge). Every
#: caller gets these unless it overrides; `sugars.py` never does, so its
#: numerics are unchanged by this move.
RELAX_ITERS = 800
RELAX_STEP = 0.25
RELAX_REPULSION_MARGIN = 1.05
RELAX_ANGLE_K = 2.0
RELAX_ANGLE_STEP = 0.02


def angle_theta_gradients(
    p_i: np.ndarray, p_k: np.ndarray, p_j: np.ndarray
) -> tuple[float, np.ndarray, np.ndarray, np.ndarray]:
    """The angle i-k-j (radians) and its gradient w.r.t. each of the three
    positions — the standard molecular-mechanics angle-bending gradient
    (``d(theta)/d(pos) = -1/sin(theta) . d(cos(theta))/d(pos)``, itself from
    the law-of-cosines derivative). Verified against a finite-difference
    check across several random configurations during this function's
    original development (in `sugars.py`, before this move) — never
    re-derived carelessly, a sign error here would silently push angles the
    WRONG way. ``sin(theta)`` is floored away from zero to avoid a blow-up
    at the (chemically meaningless, shouldn't occur) degenerate
    collinear/coincident case."""
    r_ki = p_i - p_k
    r_kj = p_j - p_k
    d_ki = float(np.linalg.norm(r_ki))
    d_kj = float(np.linalg.norm(r_kj))
    cos_t = float(np.clip(np.dot(r_ki, r_kj) / (d_ki * d_kj), -1.0, 1.0))
    theta = float(np.arccos(cos_t))
    sin_t = max(math.sin(theta), 1e-3)
    grad_i_cos = r_kj / (d_ki * d_kj) - cos_t * r_ki / (d_ki**2)
    grad_j_cos = r_ki / (d_ki * d_kj) - cos_t * r_kj / (d_kj**2)
    grad_k_cos = -(grad_i_cos + grad_j_cos)
    return theta, -grad_i_cos / sin_t, -grad_j_cos / sin_t, -grad_k_cos / sin_t


def _per_atom_hybridizations(n: int, hybridizations: list[str] | str) -> list[str]:
    """Normalize the `hybridizations=` argument every public function here
    accepts: a single string broadcasts to every atom (the `sugars.py`
    all-sp3 case, and the `relax.py` default), a per-atom list must match
    `n` exactly."""
    if isinstance(hybridizations, str):
        return [hybridizations] * n
    if len(hybridizations) != n:
        raise ValueError(
            f"hybridizations has {len(hybridizations)} entries, expected {n} "
            "(one per atom) or a single string broadcast to all atoms"
        )
    return list(hybridizations)


def angle_triples(
    elements: list[str],
    bonds: list[tuple[int, int]],
    hybridizations: list[str] | str = DEFAULT_HYBRIDIZATION,
) -> list[tuple[int, int, int, float]]:
    """Every ``(i, k, j, theta0_rad)`` bond-angle triple :func:`relax_graph`'s
    angle term restores toward — every vertex with >=2 bonded neighbours,
    every neighbour pair at that vertex, target =
    :func:`precis.structure.vsepr.ideal_angle` at that vertex's own
    hybridization (`sugars.py`'s original always passed ``"sp3"``; this is
    the generalization docs/backlog/se-nanobud-graph.md §1 asks for — an
    sp2 vertex now restores toward 120°, not 109.47°)."""
    n = len(elements)
    hybs = _per_atom_hybridizations(n, hybridizations)
    adj: dict[int, list[int]] = {i: [] for i in range(n)}
    for i, j in bonds:
        adj[i].append(j)
        adj[j].append(i)
    triples: list[tuple[int, int, int, float]] = []
    for k in range(n):
        neighbors = adj[k]
        if len(neighbors) < 2:
            continue
        ideal_deg = ideal_angle(elements[k], hybs[k])
        if ideal_deg is None:
            continue
        theta0 = math.radians(ideal_deg)
        for a in range(len(neighbors)):
            for b in range(a + 1, len(neighbors)):
                triples.append((neighbors[a], k, neighbors[b], theta0))
    return triples


@dataclass
class GeoRelaxTrace:
    """The bookkeeping :func:`relax_graph` hands back so a caller building a
    real ``RelaxResult`` (`relax.py`'s `geo` rung) can report an honest
    convergence envelope — `sugars.py` never looks at this (it discards the
    return value entirely, exactly as it discarded `_relax`'s old ``None``
    return), so adding it changes nothing about that call site's behavior
    or numerics."""

    converged: bool
    n_steps: int
    curve: list[float] = field(default_factory=list)


def relax_graph(
    elements: list[str],
    coords: np.ndarray,
    bonds: list[tuple[int, int]],
    pinned: set[int] | frozenset[int],
    *,
    hybridizations: list[str] | str = DEFAULT_HYBRIDIZATION,
    iters: int = RELAX_ITERS,
    step: float = RELAX_STEP,
    angle_step: float = RELAX_ANGLE_STEP,
    angle_k: float = RELAX_ANGLE_K,
    repulsion_margin: float = RELAX_REPULSION_MARGIN,
    tol: float | None = None,
) -> GeoRelaxTrace:
    """In-place bond-spring + non-bond-repulsion + angle-restoring cleanup —
    lifted verbatim from `sugars.py`'s original ``_relax`` (see that
    module's docstring for the physics narrative: covalent-radii-sum bond
    targets, a +5%-margin non-bond repulsion floor, and the VSEPR
    angle-bending gradient of :func:`angle_theta_gradients`).

    Two generalizations beyond the original, both no-ops at their default:

    - ``hybridizations`` (see :func:`angle_triples`) — a single string
      (default ``"sp3"``) reproduces `sugars.py`'s exact original
      behavior; a per-atom list lets an sp2 vertex (a fresh sp2-carbon
      graph-surgery region, the `geo` relax rung's motivating case)
      restore toward 120 degrees instead.
    - ``tol`` (default ``None``) — an early-stop threshold on the largest
      single-atom displacement this iteration. ``sugars.py`` never passes
      one, so it always runs the full ``iters`` count with no break, the
      exact original control flow; `relax.py`'s `geo` rung passes its own
      ``tol=`` so a converged geometry stops early rather than always
      burning the full iteration budget.

    ``pinned`` atoms never move (every force term multiplies by a 0/1
    per-atom mask) — whole-atom only, no partial per-axis freedom (unlike
    the ``Scene``-level ``fixed`` bitmask other relax rungs honor); a
    caller needing genuine per-axis constraints wants ``fidelity='clean'``
    or an energy rung instead.

    Returns a :class:`GeoRelaxTrace` (`sugars.py` discards it, matching the
    original ``None`` return there) rather than mutating nothing — `coords`
    itself is still mutated in place, exactly as before.
    """
    n = len(elements)
    bonded = {frozenset(b) for b in bonds}
    movable = np.array([0.0 if i in pinned else 1.0 for i in range(n)])
    triples = angle_triples(elements, bonds, hybridizations)
    curve: list[float] = []
    converged = False
    step_count = 0
    for step_count in range(1, iters + 1):
        disp = np.zeros_like(coords)
        for i, j in bonds:
            target = covalent_radius(elements[i]) + covalent_radius(elements[j])
            d = coords[j] - coords[i]
            dist = float(np.linalg.norm(d))
            if dist < 1e-9:
                continue
            f = step * (dist - target) * (d / dist)
            disp[i] += f * movable[i]
            disp[j] -= f * movable[j]
        for i in range(n):
            for j in range(i + 1, n):
                if frozenset((i, j)) in bonded:
                    continue
                cutoff = (
                    1.2
                    * (covalent_radius(elements[i]) + covalent_radius(elements[j]))
                    * repulsion_margin
                )
                d = coords[j] - coords[i]
                dist = float(np.linalg.norm(d))
                if dist >= cutoff or dist < 1e-9:
                    continue
                f = step * (cutoff - dist) * (d / dist)
                disp[i] -= f * movable[i]
                disp[j] += f * movable[j]
        for i, k, j, theta0 in triples:
            theta, grad_i, grad_j, grad_k = angle_theta_gradients(
                coords[i], coords[k], coords[j]
            )
            f = angle_step * angle_k * (theta - theta0)
            disp[i] -= f * grad_i * movable[i]
            disp[j] -= f * grad_j * movable[j]
            disp[k] -= f * grad_k * movable[k]
        coords += disp
        max_disp = float(np.max(np.linalg.norm(disp, axis=1))) if n else 0.0
        curve.append(round(max_disp, 4))
        if tol is not None and max_disp < tol:
            converged = True
            break
    return GeoRelaxTrace(converged=converged, n_steps=step_count, curve=curve)


def _kabsch_transform(
    mobile: np.ndarray, target: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """The rotation ``R`` (3x3, proper — determinant +1, never a reflection)
    and translation ``t`` (3,) minimizing
    ``sum(||(R @ mobile[i] + t) - target[i]||^2)`` — the standard Kabsch
    algorithm. ``mobile``/``target`` must be the same size and index-paired
    (``mobile[i]`` corresponds to ``target[i]``); no correspondence search
    happens here."""
    mobile_mean = mobile.mean(axis=0)
    target_mean = target.mean(axis=0)
    mobile_c = mobile - mobile_mean
    target_c = target - target_mean
    h = mobile_c.T @ target_c
    u, _s, vt = np.linalg.svd(h)
    d = float(np.sign(np.linalg.det(vt.T @ u.T)))
    if d == 0.0:  # pragma: no cover - degenerate det, treat as proper
        d = 1.0
    corr = np.diag([1.0, 1.0, d])
    r = vt.T @ corr @ u.T
    t = target_mean - r @ mobile_mean
    return r, t


def kabsch_align(mobile: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, float]:
    """Rigid-align ``mobile`` onto ``target`` (both ``(N, 3)``, index-paired)
    — :func:`_kabsch_transform` applied, plus the resulting RMSD. No Kabsch
    helper existed anywhere in-tree before this (checked first per the task
    instruction) — used both internally (:func:`embed_from_graph`'s partial-
    embed continuity) and by any caller comparing two geometries up to
    rotation/translation (e.g. an embed-from-bond-graph self-test against a
    closed-form reference)."""
    r, t = _kabsch_transform(mobile, target)
    aligned = mobile @ r.T + t
    rmsd = float(np.sqrt(np.mean(np.sum((aligned - target) ** 2, axis=1))))
    return aligned, rmsd


def embed_from_graph(
    n_atoms: int,
    bonds: Sequence[tuple[int, int]],
    *,
    seed_coords: np.ndarray | None = None,
    pinned: frozenset[int] = frozenset(),
    bond_length: float = 1.45,
) -> np.ndarray:
    """Seed 3D coordinates from a bond graph ALONE — no starting geometry
    needed at all (docs/backlog/se-nanobud-graph.md §1's "embed-from-graph"
    item; the graph-surgery generators' escape from needing coordinates for
    a freshly-stitched region).

    The Manolopoulos-Fowler "topological coordinates" construction: build
    the ``(n_atoms, n_atoms)`` 0/1 adjacency matrix, take its
    :func:`numpy.linalg.eigh` decomposition (dense — no scipy, a core-deps
    constraint; fine to a few-thousand atoms), and use the THREE
    eigenvectors with the largest eigenvalues BELOW the Perron (trivial,
    largest-of-all) one as the seeded x/y/z columns. For a connected graph
    the Perron eigenvector is simple (Perron-Frobenius) and all-one-sign —
    no shape information, always skipped; the next three carry the
    graph's own 3D "shape" (this is the standard fullerene-coordinate
    trick from Fowler & Manolopoulos's *An Atlas of Fullerenes*, generalized
    here to any bonded graph).

    Each chosen eigenvector has a free overall sign (``eigh`` returns
    *some* valid eigenvector, not a canonical one) — resolved
    deterministically by forcing the largest-magnitude component of each
    to be positive, so the same graph always embeds to the same seed
    (checked by this module's own determinism test) regardless of which
    LAPACK build produced the raw eigendecomposition.

    Coordinates are then uniformly rescaled so the MEDIAN bonded-pair
    distance lands on ``bond_length`` (default 1.45 Å, a typical
    carbon-carbon covalent bond — parametrize for a different element).

    ``seed_coords``/``pinned`` together do a **partial embed**: when a
    subset of atoms already has known (parent-structure) coordinates,
    those rows of ``seed_coords`` are treated as ground truth and kept
    exactly — the freshly spectral-seeded coordinates for the REST of the
    graph are rigidly aligned (:func:`_kabsch_transform`, >=3 pinned atoms
    needed for a unique rotation; a translation-only fallback applies for
    1-2) onto that known subset first, so the new region continues
    smoothly from the parent rather than landing in the spectral seed's
    own arbitrary frame. With fewer than 3 pinned atoms there is no unique
    rotation to solve for — only the pinned centroid is matched, and the
    caller should expect to still need a relax pass to reconcile
    orientation.
    """
    if n_atoms <= 0:
        return np.zeros((0, 3))
    adjacency = np.zeros((n_atoms, n_atoms))
    for i, j in bonds:
        adjacency[i, j] = 1.0
        adjacency[j, i] = 1.0

    eigvals, eigvecs = np.linalg.eigh(adjacency)
    order = np.argsort(eigvals)[::-1]  # descending: index 0 = Perron (trivial)
    n_axes = min(3, n_atoms - 1)
    chosen = order[1 : 1 + n_axes]

    coords = np.zeros((n_atoms, 3))
    for axis, idx in enumerate(chosen):
        v = eigvecs[:, idx].copy()
        largest = int(np.argmax(np.abs(v)))
        if v[largest] < 0.0:
            v = -v
        coords[:, axis] = v

    if bonds:
        dists = np.array(
            [float(np.linalg.norm(coords[i] - coords[j])) for i, j in bonds]
        )
        median = float(np.median(dists))
        if median > 1e-9:
            coords *= bond_length / median

    if seed_coords is not None and pinned:
        seed = np.asarray(seed_coords, dtype=float)
        pinned_idx = sorted(pinned)
        if len(pinned_idx) >= 3:
            r, t = _kabsch_transform(coords[pinned_idx], seed[pinned_idx])
            coords = coords @ r.T + t
        else:
            coords = (
                coords - coords[pinned_idx].mean(axis=0) + seed[pinned_idx].mean(axis=0)
            )
        coords[pinned_idx] = seed[pinned_idx]
    return coords


def register(
    coords: np.ndarray,
    *,
    anchor: int,
    x_neighbor: int,
    anchor_neighbors: Sequence[int] | None = None,
    plane_ref: str | np.ndarray = "inward",
) -> np.ndarray:
    """Canonical rigid registration (docs/backlog/se-nanobud-graph.md §1,
    item 4) — a post-relax, presentation-layer re-framing, NEVER used to
    pin anything during relax, and molecular scenes only (a periodic scene
    keeps its own cell conventions instead). Guarantees camera params stay
    meaningful across relax reruns and a generated feature (e.g. a nanobud)
    always pops out the same way.

    Places ``anchor`` at the origin, the ``anchor``-``x_neighbor`` bond
    direction along ``+x``, and completes a right-handed frame whose
    ``+z`` is:

    - ``plane_ref="inward"`` (the default): the best-fit plane normal
      through ``anchor``'s own bonded neighbors (``anchor_neighbors``,
      >=2 required — an SVD best-fit plane through their positions, the
      least-variance eigenvector of their covariance, same construction
      `sugars._align_to_axis` uses for its ring plane), signed to point
      TOWARD the centroid of the WHOLE structure — the deterministic rule
      resolving the plane normal's otherwise +/- ambiguity ("the bud
      always pops the same way").
    - an explicit ``(3,)`` array: used directly as the target ``+z``
      direction (any nonzero vector; re-orthogonalized against ``+x``
      below, so it need not already be exactly perpendicular).

    ``y`` is then forced by right-handedness (``y = z x x``) — there is no
    free sign left anywhere in the frame once ``anchor``/``x_neighbor``/the
    ``+z`` choice are fixed, so the whole transform is a deterministic pure
    function of ``coords`` and these arguments. Idempotent: registering an
    already-registered structure with the same anchor/neighbors is the
    identity transform up to floating point (the anchor is already at the
    origin, ``x_neighbor`` already exactly on ``+x``, and the plane-fit
    ``+z`` — already orthogonal to ``+x`` — re-derives to the same
    direction).
    """
    coords = np.asarray(coords, dtype=float)
    c = coords - coords[anchor]

    x_vec = c[x_neighbor]
    x_norm = float(np.linalg.norm(x_vec))
    if x_norm < 1e-9:
        raise ValueError(
            f"register: anchor {anchor} and x_neighbor {x_neighbor} coincide "
            "(zero-length bond) -- cannot orient +x"
        )
    x_hat = x_vec / x_norm

    if isinstance(plane_ref, str):
        if plane_ref != "inward":
            raise ValueError(
                f"register: unknown plane_ref {plane_ref!r} (use 'inward' or "
                "an explicit (3,) direction array)"
            )
        if anchor_neighbors is None or len(anchor_neighbors) < 2:
            raise ValueError(
                "register: plane_ref='inward' needs >=2 anchor_neighbors to fit a plane"
            )
        nbr_pts = c[list(anchor_neighbors)]
        centered = nbr_pts - nbr_pts.mean(axis=0)
        cov = centered.T @ centered
        _eigvals, eigvecs = np.linalg.eigh(cov)
        z_raw = eigvecs[:, 0]  # least-variance direction = the local plane normal
        whole_centroid = c.mean(axis=0)
        if float(np.dot(z_raw, whole_centroid)) < 0.0:
            z_raw = -z_raw
    else:
        z_raw = np.asarray(plane_ref, dtype=float)
        z_norm = float(np.linalg.norm(z_raw))
        if z_norm < 1e-9:
            raise ValueError("register: explicit plane_ref must be a nonzero vector")
        z_raw = z_raw / z_norm

    # Gram-Schmidt z against x -- exact orthogonality even when the plane
    # fit isn't perfectly perpendicular to the anchor->x_neighbor bond --
    # then complete a right-handed frame.
    z_perp = z_raw - float(np.dot(z_raw, x_hat)) * x_hat
    z_perp_norm = float(np.linalg.norm(z_perp))
    if z_perp_norm < 1e-9:
        raise ValueError(
            "register: the chosen +z direction is parallel to +x -- the "
            "frame is degenerate (pick a different x_neighbor/plane_ref)"
        )
    z_hat = z_perp / z_perp_norm
    y_hat = np.cross(z_hat, x_hat)  # right-handed: z x x = y

    rot = np.array([x_hat, y_hat, z_hat])
    return c @ rot.T

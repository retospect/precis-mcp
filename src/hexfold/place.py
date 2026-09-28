"""``place_graph``: joint rigid placement of instances across a part-graph
*cycle* -- the seed-placement residual left open by ``build._place_seeds``
(docs/backlog/hexfold-integration.md "Root-caused 2026-09-28" item 2 and
"Seed placement residuals ... fixed 2026-09-28"'s "Still open" note).

``_place_seeds``' two-phase BFS assigns each non-root instance exactly one
rigid transform, from the first fuse/seam edge that reaches it; any other
edge onto an already-placed instance (a part-graph cycle, SPEC 12.2) is
silently dropped.  For a *tree* part graph that edge simply doesn't exist,
so nothing is lost.  For a cycle, dropping it means the two chains that
meet back up (e.g. ``flanged_doughnut.hx``'s ``top``-``wall``-``bottom``
real-fuse chain vs its ``top``-``bottom`` seam edge) can disagree by a
rotation no single per-edge transform reconciles -- ``check_registry``'s
integer-phase closure reports residual 0 (the symmetry indices line up),
but that is a discrete claim about *which* lattice sites bond, not a
continuous guarantee that the two chains' independently-composed
orientations agree.

``place_graph`` is a small alternating-Kabsch / Gauss-Seidel relaxation
over the whole set of pairwise correspondences an edge implies (SPEC
12.2's part-graph edges, restated as explicit atom-atom pairs): it
initialises every instance via the same one-edge-per-node BFS idea
(:func:`_place_seeds`'s own algorithm, generalised over arbitrary point
correspondences instead of the rim-normal-and-twist fit), then repeatedly
refits each non-root instance's rigid transform against *all* of its
neighbours' current placements at once, until the whole net stops moving
or a sweep budget runs out.  A tree part graph converges after the first
sweep confirms nothing moved (each instance has exactly one constraint,
already satisfied by its BFS transform); a cycle spreads its
irreconcilable residual over every instance the cycle touches instead of
concentrating it on whichever edge the old BFS happened to drop.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: One part-graph edge: the two instance names, the atom-index pairs the
#: edge implies (indices are *local* -- positions into ``rims[u]`` /
#: ``rims[v]``, not global atom ordinals), and a per-side unit normal
#: ``(n_u, n_v)`` -- each expressed in its own instance's local frame --
#: used to build the sigma-offset target when that side is the currently
#: -known one (see :func:`_target`).  ``u == v`` (a self-fused instance)
#: is accepted but contributes nothing: there is no second instance to
#: place relative to it.
Edge = tuple[str, str, list[tuple[int, int]], tuple[np.ndarray, np.ndarray]]


@dataclass(frozen=True)
class PlaceResult:
    """``place_graph``'s solution: per-instance ``(R, t)`` (local ->
    global, ``global = local @ R.T + t``), the RMS pair residual per
    edge (same order as the input ``edges``, sigma-offset convention
    fixed via each edge's ``u`` side and ``n_u`` -- see :func:`place_graph`
    ), and the sweep count actually run."""

    transforms: dict[str, tuple[np.ndarray, np.ndarray]]
    residuals: tuple[float, ...]
    sweeps: int


def _kabsch(src: np.ndarray, tgt: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Best rigid fit (no reflection, ``det(R) == 1``) mapping ``src``
    onto ``tgt`` -- the same SVD Kabsch solve as
    :func:`hexfold.build._fuse_transform_kabsch`, generalised to an
    arbitrary point count.  Fewer than 3 points cannot pin a 3D rotation
    (a single bond pair, or two collinear ones): falls back to identity
    rotation, translation-only -- exact for a genuine bond_link's single
    pair (no twist to resolve), an approximation for exactly two pairs.
    """
    n = len(src)
    if n == 0:
        return np.eye(3), np.zeros(3)
    if n < 3:
        return np.eye(3), tgt.mean(axis=0) - src.mean(axis=0)
    src_c = src.mean(axis=0)
    tgt_c = tgt.mean(axis=0)
    h = (src - src_c).T @ (tgt - tgt_c)
    u, _s, vt = np.linalg.svd(h)
    d = float(np.sign(np.linalg.det(vt.T @ u.T))) or 1.0
    r = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    t = tgt_c - r @ src_c
    return r, t


def place_graph(
    rims: dict[str, np.ndarray],
    edges: list[Edge],
    root: str,
    *,
    sigma: float,
    max_sweeps: int = 50,
    tol: float = 1e-4,
) -> PlaceResult:
    """Joint least-squares placement of every instance ``edges`` touches.

    ``rims[inst]`` is instance ``inst``'s own local seed coordinates
    (``(n_atoms, 3)``, the same untransformed per-instance array
    ``_place_seeds`` starts from) -- an edge's ``pairs`` index into it, so
    passing just the atoms an edge actually needs is enough (a caller
    that already has the whole instance's array, as ``_place_seeds``
    does, may pass that instead; only the paired positions are ever
    read).  ``root`` is held at the identity transform; every other
    instance edges reach gets solved.

    Target convention (shared with :func:`hexfold.build._fuse_transform_
    kabsch`): for edge ``(u, v, pairs, (n_u, n_v))``, ``v``'s paired
    atoms are fit to ``u``'s paired atoms offset by ``sigma`` along
    ``u``'s own local normal ``n_u`` (rotated into the current global
    frame) -- a rim-to-rim fuse's or seam's own registration, or a menu
    attach's six-point host-side offset.  Solving in the other direction
    (``u`` unplaced, ``v`` known) uses the same rule with the roles
    swapped -- ``n_v`` instead of ``n_u`` -- since the offset must always
    come from whichever side is *already* placed (using the side being
    solved for's own normal would need its own not-yet-known rotation).

    Algorithm: BFS from ``root`` for an initial guess (one Kabsch fit per
    newly reached instance, against whichever edge reached it first --
    :func:`_place_seeds`'s own scheme, generalised); then alternating
    sweeps, each refitting every non-root instance's ``(R, t)`` by one
    combined Kabsch against *all* its neighbours' current placements at
    once (Gauss-Seidel: a neighbour refit earlier in the same sweep is
    already visible).  Stops when the largest atom displacement across
    the whole net falls under ``tol``, or after ``max_sweeps``.

    A tree part graph (every instance has exactly one constraint) has its
    BFS initial guess already at zero residual for every edge, so the
    first sweep finds nothing moved and returns after 1 sweep. A cycle
    has at least one instance with two or more constraints that disagree;
    each sweep's combined Kabsch is the minimiser of the sum of squared
    pair residuals over that instance's edges *for the neighbours' current
    placements*, so repeating it over every instance the cycle touches
    is a block-coordinate-descent minimisation of the total sum over all
    edges (never a single global least-squares solve in one shot, but
    monotonically spreading -- never concentrating -- the irreconcilable
    residual as it converges).
    """

    def _target(
        cur: str, ei: int, self_is_u: bool, r_c: np.ndarray, t_c: np.ndarray
    ) -> tuple[np.ndarray, list[int]]:
        u, v, pairs, (n_u, n_v) = edges[ei]
        if self_is_u:
            known_idx = [p[0] for p in pairs]
            other_idx = [p[1] for p in pairs]
            normal_local = n_u
        else:
            known_idx = [p[1] for p in pairs]
            other_idx = [p[0] for p in pairs]
            normal_local = n_v
        norm = float(np.linalg.norm(normal_local)) or 1.0
        normal_local = normal_local / norm
        known_global = rims[cur][known_idx] @ r_c.T + t_c
        normal_global = r_c @ normal_local
        return known_global + sigma * normal_global, other_idx

    nodes = sorted({u for u, v, _, _ in edges} | {v for u, v, _, _ in edges} | {root})
    adj: dict[str, list[tuple[str, int, bool]]] = {}
    for ei, (u, v, _pairs, _normals) in enumerate(edges):
        if u == v:
            continue
        adj.setdefault(u, []).append((v, ei, True))
        adj.setdefault(v, []).append((u, ei, False))

    placed: dict[str, tuple[np.ndarray, np.ndarray]] = {root: (np.eye(3), np.zeros(3))}
    queue = [root]
    while queue:
        cur = queue.pop(0)
        r_c, t_c = placed[cur]
        for nbr, ei, self_is_u in sorted(adj.get(cur, ()), key=lambda x: (x[0], x[1])):
            if nbr in placed:
                continue
            target, other_idx = _target(cur, ei, self_is_u, r_c, t_c)
            src = rims[nbr][other_idx]
            placed[nbr] = _kabsch(src, target)
            queue.append(nbr)
    for n in nodes:
        placed.setdefault(n, (np.eye(3), np.zeros(3)))

    sweeps = 0
    for sweep in range(1, max_sweeps + 1):
        sweeps = sweep
        max_move = 0.0
        for node in nodes:
            if node == root or node not in adj:
                continue
            src_parts: list[np.ndarray] = []
            tgt_parts: list[np.ndarray] = []
            for nbr, ei, self_is_u in adj[node]:
                r_nb, t_nb = placed[nbr]
                target, other_idx = _target(nbr, ei, not self_is_u, r_nb, t_nb)
                tgt_parts.append(target)
                src_parts.append(rims[node][other_idx])
            src = np.concatenate(src_parts, axis=0)
            tgt = np.concatenate(tgt_parts, axis=0)
            r_new, t_new = _kabsch(src, tgt)
            r_old, t_old = placed[node]
            whole = rims[node]
            move = (
                float(
                    np.abs((whole @ r_new.T + t_new) - (whole @ r_old.T + t_old)).max()
                )
                if whole.size
                else 0.0
            )
            max_move = max(max_move, move)
            placed[node] = (r_new, t_new)
        if max_move < tol:
            break

    residuals: list[float] = []
    for u, v, pairs, (n_u, _n_v) in edges:
        if u == v or not pairs:
            residuals.append(0.0)
            continue
        r_u, t_u = placed[u]
        r_v, t_v = placed[v]
        a_idx = [p[0] for p in pairs]
        b_idx = [p[1] for p in pairs]
        a_glob = rims[u][a_idx] @ r_u.T + t_u
        b_glob = rims[v][b_idx] @ r_v.T + t_v
        norm = float(np.linalg.norm(n_u)) or 1.0
        target = a_glob + sigma * (r_u @ (n_u / norm))
        d = np.linalg.norm(b_glob - target, axis=1)
        residuals.append(float(np.sqrt(np.mean(d**2))))

    return PlaceResult(transforms=placed, residuals=tuple(residuals), sweeps=sweeps)

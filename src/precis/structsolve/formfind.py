"""Force-density form-finding (Schek's linear method) — the shape a
pin-jointed axial network takes in equilibrium, given per-member force
densities and anchored coordinates.

The method: with force density ``q_k = t_k / L_k`` fixed per member, node
equilibrium ``Σ_k q_k (x_other − x_i) + p_i = 0`` is *linear* in the
coordinates — one solve of the force-density Laplacian
``D = Cᵀ diag(q) C`` per axis, partitioned into prescribed and free
coordinates, returns the equilibrium geometry directly (no iteration, no
initial geometry for the free nodes — only the anchors and ``q`` matter).
Member forces follow as ``t = q · L``.

Sign convention matches :mod:`precis_se.stability`: **tension-positive**
— a cable-like member has ``q > 0``, a strut-like member ``q < 0``.

Prescription is **per coordinate**, not per node: ``fixed[i, k]`` pins
node ``i``'s axis-``k`` coordinate at its input value, so a node may ride
a rail (one axis free) as well as be a full anchor. A solve is refused
loudly (:class:`FormFindError`) when the reduced system is singular —
no anchored coordinate on an axis, a free node group disconnected from
every anchor, or strut/cable densities cancelling — because a least-
squares "answer" there would be a confident wrong shape.

Unit-agnostic, pure over passed-in arrays, no store access (the package
docstring's house rules). Callers translate their vocabulary (blocks,
connects, ``objectives.fixed``) into the arrays here —
:mod:`precis_se.formfind` is the se bridge.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Relative singular-value cutoff below which the reduced Laplacian is
#: treated as singular. Looser than machine epsilon for the same reason
#: as stability's rank cutoff: a solve that only "works" through 1e-12
#: leakage is reporting noise, not a shape.
_SINGULAR_RTOL = 1e-10


class FormFindError(ValueError):
    """A form-finding problem that cannot be solved honestly — bad
    shapes/values, or a singular reduced system. The message names the
    axis and the likely structural cause."""


@dataclass
class FormFindResult:
    """The equilibrium geometry and the member state it implies."""

    #: (j, 3) node coordinates — prescribed entries kept verbatim.
    coords: np.ndarray
    #: (b,) member lengths at the solved geometry.
    lengths: np.ndarray
    #: (b,) member forces ``t = q · L`` — tension-positive.
    forces: np.ndarray
    #: max |residual force| over the free coordinates (should be ~0 —
    #: reported so a caller can assert the solve is an equilibrium
    #: rather than trust it).
    residual: float
    #: (j,) True where at least one coordinate was solved (not fully
    #: prescribed) — the caller's write-back set.
    solved: np.ndarray


def _laplacian(j: int, members: np.ndarray, q: np.ndarray) -> np.ndarray:
    d = np.zeros((j, j))
    for k in range(members.shape[0]):
        ia, ib = int(members[k, 0]), int(members[k, 1])
        d[ia, ia] += q[k]
        d[ib, ib] += q[k]
        d[ia, ib] -= q[k]
        d[ib, ia] -= q[k]
    return d


def form_find(
    coords: np.ndarray,
    members: np.ndarray,
    q: np.ndarray,
    fixed: np.ndarray,
    loads: np.ndarray | None = None,
) -> FormFindResult:
    """Solve the force-density system. ``coords`` (j, 3) supplies the
    prescribed coordinates (free entries are ignored — the method needs
    no initial guess); ``members`` (b, 2) integer node indices; ``q``
    (b,) per-member force densities, tension-positive, all nonzero;
    ``fixed`` (j, 3) bool, True = prescribed; ``loads`` (j, 3) external
    force per node in the caller's force unit, default zero."""
    coords = np.asarray(coords, dtype=float)
    members = np.asarray(members, dtype=int)
    q = np.asarray(q, dtype=float)
    fixed = np.asarray(fixed, dtype=bool)
    if coords.ndim != 2 or coords.shape[1] != 3:
        raise FormFindError(f"coords must be (j, 3), got {coords.shape}")
    j = coords.shape[0]
    if members.ndim != 2 or members.shape[1] != 2:
        raise FormFindError(f"members must be (b, 2), got {members.shape}")
    b = members.shape[0]
    if b == 0 or j < 2:
        raise FormFindError("nothing to solve: need at least 2 nodes and 1 member")
    if q.shape != (b,):
        raise FormFindError(f"q must be ({b},) — one force density per member")
    if fixed.shape != (j, 3):
        raise FormFindError(f"fixed must be ({j}, 3) bool, got {fixed.shape}")
    if loads is None:
        loads = np.zeros((j, 3))
    else:
        loads = np.asarray(loads, dtype=float)
        if loads.shape != (j, 3):
            raise FormFindError(f"loads must be ({j}, 3), got {loads.shape}")
        if not np.all(np.isfinite(loads)):
            raise FormFindError("loads must be finite")
    if not np.all(np.isfinite(coords[fixed])):
        raise FormFindError("prescribed coordinates must be finite")
    if not np.all(np.isfinite(q)) or np.any(q == 0.0):
        raise FormFindError(
            "every force density must be a finite nonzero number "
            "(tension-positive; a q of 0 removes the member — drop it "
            "instead)"
        )
    if np.any(members < 0) or np.any(members >= j):
        raise FormFindError(f"member node indices must lie in [0, {j})")
    if np.any(members[:, 0] == members[:, 1]):
        raise FormFindError(
            "a member may not join a node to itself — drop self-loops before solving"
        )

    d = _laplacian(j, members, q)
    out = coords.copy()
    for axis in range(3):
        free = ~fixed[:, axis]
        n_free = int(free.sum())
        if n_free == 0:
            continue
        d_ff = d[np.ix_(free, free)]
        rhs = loads[free, axis] - d[np.ix_(free, ~free)] @ coords[~free, axis]
        svals = np.linalg.svd(d_ff, compute_uv=False)
        smax = float(svals[0]) if svals.size else 0.0
        if smax == 0.0 or float(svals[-1]) <= _SINGULAR_RTOL * smax:
            raise FormFindError(
                f"the reduced system is singular along axis "
                f"{'xyz'[axis]} — likely no anchored coordinate on that "
                "axis, a group of free nodes disconnected from every "
                "anchor, or strut (q < 0) densities cancelling the "
                "cables'; anchor more coordinates or adjust the q ratios"
            )
        out[free, axis] = np.linalg.solve(d_ff, rhs)

    lengths = np.empty(b)
    for k in range(b):
        lengths[k] = float(
            np.linalg.norm(out[int(members[k, 1])] - out[int(members[k, 0])])
        )
    forces = q * lengths
    free_mask = ~fixed
    residual_field = loads - np.stack([d @ out[:, axis] for axis in range(3)], axis=1)
    residual = (
        float(np.max(np.abs(residual_field[free_mask]))) if free_mask.any() else 0.0
    )
    solved = free_mask.any(axis=1)
    return FormFindResult(
        coords=out,
        lengths=lengths,
        forces=forces,
        residual=residual,
        solved=solved,
    )

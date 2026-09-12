"""Active-set complementarity solve for unilateral pin-jointed networks
(docs/backlog/complementarity-solver.md slice 1 — CORE only): given
node coordinates, member incidence, per-member axial rate and free
length (hence a prestress ``k(L₀ − L)`` at the input geometry), a
per-member sign idiom, per-coordinate supports and nodal loads, find
the small-displacement equilibrium in which every member obeys its
declared one-sidedness — never both a gap and a force.

**The unilateral family, one formulation.** Per member, either it
participates (its length constraint is active — a taut cable, a
bearing strut, a seated stop) and carries a legally-signed force, or it
does not (slack, separated, unseated) and carries none. Which members
participate is not knowable in advance — a cable going slack changes
which linear system describes the structure — so this is an
active-set iteration over trial member sets, never a single solve.
Four idioms (spec §4.2.1): ``tension_only`` (cable/tie — slack under
compression), ``compression_only`` (strut/contact — separates under
tension), ``bidirectional`` (rod — always participates, either sign
legal) and ``must_contact`` (a hard stop — physically the same
one-sided law as ``compression_only``, but a stop that ends up
*not* engaged is reported ``unseated`` rather than the benign
``separated``, because a hard stop that fails to seat is a design
failure, not a quiet non-event).

**Model.** One linear elastic axial (pin-ended, two-force) member per
row of ``members``, tension-positive — matching
:mod:`precis.structsolve.formfind` and :mod:`precis_se.stability`.
Small-displacement, linearized about the *given* geometry (``coords``
is the as-built/as-designed shape, not a free guess to be reshaped —
contrast :func:`~precis.structsolve.formfind.form_find`, which returns
a new shape from force densities alone). The tangent stiffness has two
additive parts, standard for prestressed cable/strut networks
(Buchholdt's cable-net method; the geometric part is exactly
Pellegrino & Calladine's product-force matrix, already in-tree as
:func:`precis_se.stability._stress_matrix`, duplicated here rather than
imported — the package boundary forbids a structsolve→precis_se
dependency):

- **Elastic**: ``rate_k · u_k u_kᵀ`` per active member — ordinary axial
  spring stiffness, direction-only (a member is stiff along its own
  axis and contributes nothing transverse).
- **Geometric** (a.k.a. stress or geometric-stiffness matrix): the
  *existing* force density ``q_k = t0_k / L_k`` of each active member,
  assembled as the isotropic force-density Laplacian ``Ω`` Kronecker
  the 3×3 identity. This is what lets a first-order mechanism become
  stiff once correctly prestressed — the textbook reason tensegrities
  need form-finding, not just statics — and it is why the acceptance
  fixture (d), the classic 3-strut/9-cable prism at its exact
  equilibrium twist, only solves once its prestress is declared: with
  the elastic term alone the reduced stiffness there is singular (one
  internal mechanism), and it is the geometric term, at the correct
  self-stress ratios, that removes it.

**Algorithm (documented choice — this is the "iterative active-set
over linear solves" branch flagged as open in the spec, not the
LCP/QP-via-scipy or Lemke-pivot branch).** Start with every member
active. Solve the linear tangent-stiffness system for the free-DOF
displacement increment. Any active member whose resulting force
violates its idiom's legal sign is dropped (its force forced to zero,
excluded from the next solve's stiffness and its own equilibrium
contribution); any *inactive* member whose trial force — computed from
the same displacement solution, using its own rate/length as if it had
stayed engaged — has "re-entered" its legal sign is reinstated. Both
checks run over every member each pass (not one at a time), then the
system is re-solved; the fixed point is a self-consistent active set: no
active member's force is illegal, and no inactive member's trial force
wants to reactivate — exactly the complementarity condition (either the
gap is zero and there is legally-signed force, or there is a gap and no
force). This is the standard "member removal/reinstatement" scheme for
one-sided cable/strut elements; it is simple, needs no LCP/QP machinery,
and its only failure modes are enumerable: (a) the active-set signature
repeats — a cycle — refused rather than accepted at whichever iteration
happened to stop there; (b) the reduced stiffness of the current active
set is singular — the load has no path through the members still
willing to carry it, i.e. no complementary equilibrium exists in the
small-displacement model. Both refuse loudly
(:class:`ComplementarityError`, the :class:`~precis.structsolve.formfind.FormFindError`
posture) rather than answer with a member set that does not actually
balance.

Unit-agnostic and pure over passed-in arrays, no store access (the
package docstring's house rules): every tolerance here is *relative*
(to the largest singular value for the rank/singularity checks, to the
largest force magnitude for the sign checks), never an absolute
epsilon — the same problem in metres/newtons or ångström/nanonewtons
takes the same number of iterations and reaches the same per-member
status.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: The four sign idioms a member may declare. `must_contact` is
#: physically identical to `compression_only` (see module docstring);
#: the two are kept distinct only so the status labels can tell a
#: benign non-event (`separated`) from a failed hard stop (`unseated`).
IDIOMS = ("tension_only", "compression_only", "bidirectional", "must_contact")

#: Relative singular-value cutoff for the reduced tangent stiffness —
#: same rationale as formfind's and stability's rank cutoffs: a solve
#: that only "works" through 1e-10 leakage is noise, not a structure.
_SINGULAR_RTOL = 1e-10

#: Relative tolerance (against the largest trial force magnitude seen
#: in a pass) for deciding a member's sign is actually violated /
#: actually re-entered, rather than floating-point noise at the
#: boundary. Scale-relative so unit choice never changes the answer.
_SIGN_RTOL = 1e-9


class ComplementarityError(ValueError):
    """No complementary equilibrium exists for this problem in the
    small-displacement model — a cycling active-set, or an active set
    whose reduced stiffness is singular (the load has no legal path
    through the members still willing to carry it). The message names
    the cause; callers must not fall back to a least-squares answer."""


@dataclass
class ComplementarityResult:
    """The converged active-set equilibrium."""

    #: (j, 3) displacement increment from the input `coords`; exactly
    #: zero at every prescribed (`fixed`) coordinate.
    displacements: np.ndarray
    #: (b,) member force, tension-positive; 0 for every inactive
    #: (slack / separated / unseated) member.
    forces: np.ndarray
    #: (b,) True where the member is part of the converged active set.
    active: np.ndarray
    #: (b,) dtype=object — one of 'taut' / 'slack' / 'bearing' /
    #: 'separated' / 'seated' / 'unseated' per member (see module
    #: docstring for which idiom produces which pair).
    status: np.ndarray
    #: max |imbalance| over the free coordinates of the converged
    #: active set's tangent-stiffness system — should be ~0; reported so
    #: a caller can assert the linear solve is trustworthy rather than
    #: trust it (a numerical check, not an independent physics
    #: cross-check — see the module docstring's geometric-stiffness
    #: note for why `A @ forces` alone is not the right quantity here).
    residual: float
    #: active-set iterations to convergence (>= 1).
    iterations: int


def _status_for(idiom: str, active: bool, force: float) -> str:
    if idiom == "tension_only":
        return "taut" if active else "slack"
    if idiom == "compression_only":
        return "bearing" if active else "separated"
    if idiom == "must_contact":
        return "seated" if active else "unseated"
    return "taut" if force >= 0.0 else "bearing"  # bidirectional


def solve_complementarity(
    coords: np.ndarray,
    members: np.ndarray,
    rate: np.ndarray,
    free_length: np.ndarray,
    idiom: np.ndarray,
    fixed: np.ndarray,
    loads: np.ndarray | None = None,
    *,
    max_iterations: int | None = None,
) -> ComplementarityResult:
    """Solve the unilateral active-set problem. ``coords`` (j, 3) is
    the as-given geometry (the linearization point — not reshaped, in
    contrast to :func:`~precis.structsolve.formfind.form_find`);
    ``members`` (b, 2) integer node indices; ``rate`` (b,) per-member
    axial stiffness (force/length), finite and > 0; ``free_length``
    (b,) per-member unstressed length, finite and > 0 — together with
    the member's length at ``coords`` these give each member's
    prestress ``t0 = rate · (L − free_length)``, tension-positive;
    ``idiom`` (b,) each entry one of :data:`IDIOMS`; ``fixed`` (j, 3)
    bool, True = prescribed (the ``form_find`` per-coordinate
    convention — a node may ride a rail); ``loads`` (j, 3) external
    force per node, default zero."""
    coords = np.asarray(coords, dtype=float)
    members = np.asarray(members, dtype=int)
    rate = np.asarray(rate, dtype=float)
    free_length = np.asarray(free_length, dtype=float)
    idiom_arr = np.asarray(idiom, dtype=object)
    fixed = np.asarray(fixed, dtype=bool)

    if coords.ndim != 2 or coords.shape[1] != 3:
        raise ComplementarityError(f"coords must be (j, 3), got {coords.shape}")
    j = coords.shape[0]
    if members.ndim != 2 or members.shape[1] != 2:
        raise ComplementarityError(f"members must be (b, 2), got {members.shape}")
    b = members.shape[0]
    if b == 0 or j < 2:
        raise ComplementarityError(
            "nothing to solve: need at least 2 nodes and 1 member"
        )
    if rate.shape != (b,):
        raise ComplementarityError(f"rate must be ({b},) — one axial rate per member")
    if free_length.shape != (b,):
        raise ComplementarityError(
            f"free_length must be ({b},) — one free length per member"
        )
    if idiom_arr.shape != (b,):
        raise ComplementarityError(f"idiom must be ({b},) — one idiom per member")
    if fixed.shape != (j, 3):
        raise ComplementarityError(f"fixed must be ({j}, 3) bool, got {fixed.shape}")
    if loads is None:
        loads = np.zeros((j, 3))
    else:
        loads = np.asarray(loads, dtype=float)
        if loads.shape != (j, 3):
            raise ComplementarityError(f"loads must be ({j}, 3), got {loads.shape}")
        if not np.all(np.isfinite(loads)):
            raise ComplementarityError("loads must be finite")
    if not np.all(np.isfinite(coords[fixed])):
        raise ComplementarityError("prescribed coordinates must be finite")
    if not np.all(np.isfinite(rate)) or np.any(rate <= 0.0):
        raise ComplementarityError(
            "every rate must be a finite positive number (a non-positive "
            "axial stiffness is not a member)"
        )
    if not np.all(np.isfinite(free_length)) or np.any(free_length <= 0.0):
        raise ComplementarityError("every free_length must be a finite positive number")
    if np.any(members < 0) or np.any(members >= j):
        raise ComplementarityError(f"member node indices must lie in [0, {j})")
    if np.any(members[:, 0] == members[:, 1]):
        raise ComplementarityError(
            "a member may not join a node to itself — drop self-loops before solving"
        )
    # key=str: a mixed-type idiom array (e.g. a stray int) makes the set
    # difference contain incomparable types — sort by string form so a
    # bad idiom is a clean ComplementarityError, never a bare TypeError.
    bad_idioms = sorted(set(idiom_arr.tolist()) - set(IDIOMS), key=str)
    if bad_idioms:
        raise ComplementarityError(
            f"unknown idiom(s) {bad_idioms} — must be one of {IDIOMS}"
        )

    # Base geometry: unit direction + length per member, and the
    # tension-positive equilibrium matrix (+u at the a-node, -u at the
    # b-node), matching precis_se.stability's convention exactly.
    lengths = np.empty(b)
    equilibrium = np.zeros((3 * j, b))
    for k in range(b):
        ia, ib = int(members[k, 0]), int(members[k, 1])
        u = coords[ib] - coords[ia]
        length = float(np.linalg.norm(u))
        if length <= 0.0:
            raise ComplementarityError(
                f"member {k} ({ia}, {ib}) has zero length — no line of action"
            )
        lengths[k] = length
        u = u / length
        equilibrium[3 * ia : 3 * ia + 3, k] = u
        equilibrium[3 * ib : 3 * ib + 3, k] = -u

    t0 = rate * (lengths - free_length)  # prestress at the input geometry
    fixed_flat = fixed.reshape(-1)
    free = ~fixed_flat
    loads_flat = loads.reshape(-1)

    active = np.ones(b, dtype=bool)
    visited: set[frozenset[int]] = set()
    n_free = int(free.sum())
    max_iter = max_iterations if max_iterations is not None else max(50, 4 * b)

    forces_all = t0.copy()
    d_flat = np.zeros(3 * j)
    iteration = 0
    while True:
        iteration += 1
        if iteration > max_iter:
            raise ComplementarityError(
                f"no complementary equilibrium found within {max_iter} "
                "active-set iterations without converging or repeating a "
                "set — pass max_iterations explicitly if this problem "
                "genuinely needs more"
            )
        signature = frozenset(np.flatnonzero(active).tolist())
        if signature in visited:
            raise ComplementarityError(
                f"the active-set iteration is cycling (iteration {iteration} "
                "repeats an earlier member set) — no complementary "
                "equilibrium exists for this problem in the small-"
                "displacement model"
            )
        visited.add(signature)

        idx = np.flatnonzero(active)
        k_total = np.zeros((3 * j, 3 * j))
        rhs_full = np.zeros(3 * j)
        if n_free == 0:
            d_free = np.empty(0)
        else:
            # idx may be empty (every member inactive) — the matrix
            # products below then come out all-zero, and the singular
            # check a few lines down catches it exactly like any other
            # active set with no stiffness to offer: nothing left to
            # carry a load is refused the same way as any other.
            a_active = equilibrium[:, idx]
            elastic = a_active @ np.diag(rate[idx]) @ a_active.T
            omega = np.zeros((j, j))
            q_active = t0[idx] / lengths[idx]
            for pos, midx in enumerate(idx):
                ia, ib = int(members[midx, 0]), int(members[midx, 1])
                omega[ia, ia] += q_active[pos]
                omega[ib, ib] += q_active[pos]
                omega[ia, ib] -= q_active[pos]
                omega[ib, ia] -= q_active[pos]
            geometric = np.kron(omega, np.eye(3))
            k_total = elastic + geometric
            k_free = k_total[np.ix_(free, free)]
            # Nodal equilibrium at a free node is `(A @ T) + P = 0`
            # (Schek's force-density balance Σ q·(x_other − x_i) + p_i = 0,
            # the same identity form_find.form_find solves) — so the
            # tangent-stiffness system A @ (T0 − rate·Δe) = −P, with
            # Δe = −(Aᵀ@d) (the true elongation, positive when the
            # member lengthens), rearranges to `K @ d = P + A @ T0`.
            rhs_full = loads_flat + a_active @ t0[idx]
            rhs = rhs_full[free]

            svals = (
                np.linalg.svd(k_free, compute_uv=False) if k_free.size else np.array([])
            )
            smax = float(svals[0]) if svals.size else 0.0
            if smax == 0.0 or float(svals[-1]) <= _SINGULAR_RTOL * smax:
                inactive_names = (
                    ", ".join(str(k) for k in range(b) if not active[k]) or "none"
                )
                raise ComplementarityError(
                    f"iteration {iteration}: the active set's reduced "
                    "stiffness is singular — the load has no legal path "
                    "through the members still willing to carry it "
                    f"(inactive members: {inactive_names}); no "
                    "complementary equilibrium exists for this load in "
                    "the small-displacement model"
                )
            d_free = np.linalg.solve(k_free, rhs)

        d_flat = np.zeros(3 * j)
        d_flat[free] = d_free
        # True elongation is +u·(d_ib − d_ia); the equilibrium column at
        # ib is −u, so that is −(equilibriumᵀ @ d), not +.
        elongation = -(equilibrium.T @ d_flat)  # (b,) — every member, active or not
        forces_all = t0 + rate * elongation

        scale = float(np.max(np.abs(forces_all))) if forces_all.size else 0.0
        tol = _SIGN_RTOL * scale
        new_active = active.copy()
        for k in range(b):
            idm = idiom_arr[k]
            f = float(forces_all[k])
            if idm == "bidirectional":
                continue
            illegal_active = (idm == "tension_only" and f < -tol) or (
                idm in ("compression_only", "must_contact") and f > tol
            )
            wants_reactivate = (idm == "tension_only" and f > tol) or (
                idm in ("compression_only", "must_contact") and f < -tol
            )
            if active[k] and illegal_active:
                new_active[k] = False
            elif not active[k] and wants_reactivate:
                new_active[k] = True

        if np.array_equal(new_active, active):
            break
        active = new_active

    forces = np.where(active, forces_all, 0.0)
    status = np.array(
        [
            _status_for(str(idiom_arr[k]), bool(active[k]), float(forces[k]))
            for k in range(b)
        ],
        dtype=object,
    )
    # Solve-quality check, not an independent physics cross-check (same
    # posture as form_find's `residual`): re-evaluate the exact tangent-
    # stiffness system just solved, `k_total @ d == rhs_full`, at the
    # converged active set. The per-member `forces` above are the axial
    # (elastic) force only — the geometric-stiffness term has no scalar
    # per-member force of its own (it is a transverse, direction-change
    # effect from the *existing* prestress), so checking `A @ forces`
    # against the load would show a leftover proportional to how much
    # geometric stiffness did the stabilizing, not a defect in the
    # solve — this checks what was actually solved instead.
    residual_field = rhs_full - k_total @ d_flat
    residual_field[fixed_flat] = 0.0
    residual = float(np.max(np.abs(residual_field))) if residual_field.size else 0.0

    return ComplementarityResult(
        displacements=d_flat.reshape(j, 3),
        forces=forces,
        active=active,
        status=status,
        residual=residual,
        iterations=iteration,
    )

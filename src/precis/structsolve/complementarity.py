"""Active-set complementarity solve for unilateral pin-jointed networks
(docs/backlog/complementarity-solver.md slices 1 and 3 — CORE only, no
se/nm bridge): given node coordinates, member incidence, per-member
axial rate and free length (hence a prestress ``k(L₀ − L)`` at the
input geometry), a per-member sign idiom, per-coordinate supports and
nodal loads, find the small-displacement equilibrium in which every
member obeys its declared one-sidedness — never both a gap and a
force.

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

**Slice 3 — :func:`probe_bistability`, the two-equilibria/bistability
probe.** Given one topology and two candidate free-length assignments
(in practice a photoswitch's ``{trans, cis}`` Δ(end-to-end) on one
member — everything else in ``free_length_a``/``free_length_b`` is
identical), run :func:`solve_complementarity` for each and additionally
check that its converged active set is *second-order* stable, not
merely solvable: mirroring the discipline of
:func:`precis_se.stability.classify`'s second-order test (eigenvalues
of a stress-matrix quadratic form against a scale-relative tolerance,
duplicated rather than imported for the same package-boundary reason as
the geometric-stiffness term above), the reduced tangent stiffness over
the free DOFs must be positive *definite*, checked with
``eigvalsh``, not merely non-singular, checked with ``svd`` as
:func:`solve_complementarity`'s own iteration does. The distinction has
teeth: a compression member's geometric stiffness can drive a
transverse direction negative (the discrete analogue of buckling)
while the reduced system stays perfectly invertible — solvable, but not
a stable equilibrium (see the "non-singular but indefinite" test
fixture). Two solved-and-stable candidates report ``bistable=True``; a
monostable assignment (one candidate fails to solve, or solves but
fails the second-order test) reports exactly one stable equilibrium and
never fabricates a barrier for the other.

**The barrier estimate — the spec's open question, resolved.** Energy
along a *linear interpolation* of free length AND of the two solved
equilibria's nodal displacement (each affine in the interpolation
parameter ``t``), evaluated with the same per-member unilateral-spring
potential the sign idiom implies (zero while a tension-only member is
slack or a compression-only/must-contact member is separated, the
ordinary ``½·rate·stretch²`` while engaged) — sampled, not
re-equilibrated. This is deliberately *not* continuation: no
intermediate active-set solve is run, so a member's engagement along
the path is read off the sign of its own interpolated stretch, never
redistributed onto its neighbours. Two honesty limits this module will
not paper over: (1) because both the displacement path and the free
length path are affine in ``t``, and each unilateral term is a convex
function of an affine argument, the *sum* is provably convex in ``t`` —
an interior energy maximum strictly above both endpoints cannot occur
in this small-displacement linear model, so the interior samples add
*nothing* beyond the two endpoints: the number reported,
``max(sampled energy) − min(endpoint energy)``, is always exactly the
two states' own energy difference, ``|E_a − E_b|`` — never a
snap-through hump; (2) a genuine geometrically-nonlinear snap-through
saddle needs large-rotation kinematics this solver does not model at
all. The barrier is therefore an *estimate* for screening actuation
energy scale, never a certified saddle-point energy — every
:class:`BistabilityResult` that reports one carries the method and
these limits in its ``notes``, the same posture
:mod:`precis.structsolve.simp` uses for its advisory-tier numbers.
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

#: Relative eigenvalue floor (against the largest |eigenvalue| of the
#: reduced tangent stiffness) for :func:`probe_bistability`'s
#: second-order stability check — the same scale-relative discipline as
#: precis_se.stability's own second-order test (`eigvals > 1e-9 * scale`),
#: not `_SINGULAR_RTOL`: singularity is a magnitude test (svd), stability
#: is a sign test (eigvalsh) — a reduced stiffness can be comfortably
#: non-singular and still indefinite (see the module docstring).
_STABILITY_RTOL = 1e-9


class ComplementarityError(ValueError):
    """No complementary equilibrium exists for this problem in the
    small-displacement model — a cycling active-set, or an active set
    whose reduced stiffness is singular (the load has no legal path
    through the members still willing to carry it). The message names
    the cause; callers must not fall back to a least-squares answer."""


class ComplementarityInputError(ComplementarityError):
    """The request itself is malformed — a bad array shape, a
    non-finite/non-positive rate or free length, an unknown idiom, a
    self-loop, a zero-length member, or an emptyish problem (no nodes
    or no members). Distinct from :class:`ComplementarityError`'s other
    raise sites (active-set cycling, a singular reduced stiffness, the
    iteration budget) which mean "no complementary equilibrium exists
    for this otherwise well-formed problem" — a caller bug versus a
    genuine physics refusal. :func:`probe_bistability` re-raises this
    subclass rather than absorbing it into a ``solved=False``
    :class:`EquilibriumStabilityResult`; a subclass of
    :class:`ComplementarityError`, so existing ``except
    ComplementarityError`` call sites keep catching it unchanged."""


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


def _geometry(coords: np.ndarray, members: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-member length and the tension-positive equilibrium matrix (+u
    at the a-node, -u at the b-node) at ``coords`` — matching
    ``precis_se.stability``'s convention exactly. Shared by
    :func:`solve_complementarity`'s initial assembly and
    :func:`probe_bistability`'s post-hoc stability/barrier checks so the
    two never assemble a different geometry for the same input. Assumes
    ``coords``/``members`` are already the validated, coerced arrays."""
    j = coords.shape[0]
    b = members.shape[0]
    lengths = np.empty(b)
    equilibrium = np.zeros((3 * j, b))
    for k in range(b):
        ia, ib = int(members[k, 0]), int(members[k, 1])
        u = coords[ib] - coords[ia]
        length = float(np.linalg.norm(u))
        if length <= 0.0:
            raise ComplementarityInputError(
                f"member {k} ({ia}, {ib}) has zero length — no line of action"
            )
        lengths[k] = length
        u = u / length
        equilibrium[3 * ia : 3 * ia + 3, k] = u
        equilibrium[3 * ib : 3 * ib + 3, k] = -u
    return lengths, equilibrium


def _tangent_stiffness(
    j: int,
    members: np.ndarray,
    rate: np.ndarray,
    t0: np.ndarray,
    lengths: np.ndarray,
    equilibrium: np.ndarray,
    active: np.ndarray,
) -> np.ndarray:
    """The ``(3j, 3j)`` elastic + geometric tangent stiffness of the
    ``active`` member set, evaluated at the *existing* prestress ``t0``
    (never re-linearized at a trial force — see the module docstring's
    algorithm section). Shared by :func:`solve_complementarity`'s
    active-set iteration and by :func:`probe_bistability`'s second-order
    check, so the two can never disagree about what "the tangent
    stiffness" means for the same converged active set."""
    idx = np.flatnonzero(active)
    if idx.size == 0:
        # nothing active offers stiffness — the caller's own singularity
        # check (or, for the second-order check, its positive-definite
        # check) rejects this the same way as any other under-supported
        # active set.
        return np.zeros((3 * j, 3 * j))
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
    return elastic + geometric


def _second_order_stable(
    j: int,
    members: np.ndarray,
    rate: np.ndarray,
    t0: np.ndarray,
    lengths: np.ndarray,
    equilibrium: np.ndarray,
    active: np.ndarray,
    free: np.ndarray,
) -> tuple[bool, float]:
    """Is the converged active set's reduced tangent stiffness positive
    *definite* over the free DOFs — a genuine second-order-stable
    equilibrium, not merely the non-singular (magnitude-only) condition
    :func:`solve_complementarity`'s own iteration checks? Mirrors
    ``precis_se.stability``'s second-order test: ``eigvalsh``, not
    ``svd``, against a scale-relative tolerance (see the module
    docstring for why the distinction has teeth — a compression member's
    geometric stiffness can drive a transverse eigenvalue negative while
    the reduced system stays perfectly invertible). Returns ``(stable,
    worst eigenvalue relative to the largest |eigenvalue|)``."""
    k_total = _tangent_stiffness(j, members, rate, t0, lengths, equilibrium, active)
    k_free = k_total[np.ix_(free, free)]
    if k_free.size == 0:
        return True, 0.0  # no free dof at all — nothing that can go unstable
    eigvals = np.linalg.eigvalsh(k_free)
    scale = float(np.max(np.abs(eigvals))) or 1.0
    worst = float(np.min(eigvals))
    return bool(worst > _STABILITY_RTOL * scale), worst / scale


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
        raise ComplementarityInputError(f"coords must be (j, 3), got {coords.shape}")
    j = coords.shape[0]
    if members.ndim != 2 or members.shape[1] != 2:
        raise ComplementarityInputError(f"members must be (b, 2), got {members.shape}")
    b = members.shape[0]
    if b == 0 or j < 2:
        raise ComplementarityInputError(
            "nothing to solve: need at least 2 nodes and 1 member"
        )
    if rate.shape != (b,):
        raise ComplementarityInputError(
            f"rate must be ({b},) — one axial rate per member"
        )
    if free_length.shape != (b,):
        raise ComplementarityInputError(
            f"free_length must be ({b},) — one free length per member"
        )
    if idiom_arr.shape != (b,):
        raise ComplementarityInputError(f"idiom must be ({b},) — one idiom per member")
    if fixed.shape != (j, 3):
        raise ComplementarityInputError(
            f"fixed must be ({j}, 3) bool, got {fixed.shape}"
        )
    if loads is None:
        loads = np.zeros((j, 3))
    else:
        loads = np.asarray(loads, dtype=float)
        if loads.shape != (j, 3):
            raise ComplementarityInputError(
                f"loads must be ({j}, 3), got {loads.shape}"
            )
        if not np.all(np.isfinite(loads)):
            raise ComplementarityInputError("loads must be finite")
    if not np.all(np.isfinite(coords[fixed])):
        raise ComplementarityInputError("prescribed coordinates must be finite")
    if not np.all(np.isfinite(rate)) or np.any(rate <= 0.0):
        raise ComplementarityInputError(
            "every rate must be a finite positive number (a non-positive "
            "axial stiffness is not a member)"
        )
    if not np.all(np.isfinite(free_length)) or np.any(free_length <= 0.0):
        raise ComplementarityInputError(
            "every free_length must be a finite positive number"
        )
    if np.any(members < 0) or np.any(members >= j):
        raise ComplementarityInputError(f"member node indices must lie in [0, {j})")
    if np.any(members[:, 0] == members[:, 1]):
        raise ComplementarityInputError(
            "a member may not join a node to itself — drop self-loops before solving"
        )
    # key=str: a mixed-type idiom array (e.g. a stray int) makes the set
    # difference contain incomparable types — sort by string form so a
    # bad idiom is a clean ComplementarityInputError, never a bare TypeError.
    bad_idioms = sorted(set(idiom_arr.tolist()) - set(IDIOMS), key=str)
    if bad_idioms:
        raise ComplementarityInputError(
            f"unknown idiom(s) {bad_idioms} — must be one of {IDIOMS}"
        )

    # Base geometry: unit direction + length per member, and the
    # tension-positive equilibrium matrix (+u at the a-node, -u at the
    # b-node), matching precis_se.stability's convention exactly.
    lengths, equilibrium = _geometry(coords, members)

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
            # idx may be empty (every member inactive) — _tangent_stiffness
            # then comes back all-zero, and the singular check a few lines
            # down catches it exactly like any other active set with no
            # stiffness to offer: nothing left to carry a load is refused
            # the same way as any other.
            a_active = equilibrium[:, idx]
            k_total = _tangent_stiffness(
                j, members, rate, t0, lengths, equilibrium, active
            )
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


# ── slice 3: two-equilibria / bistability probe ──────────────────────────
# (docs/backlog/complementarity-solver.md slice 3 — see the module
# docstring for the second-order-stability and barrier-method rationale)


#: The barrier-method note, kept as one constant (the same discipline as
#: simp.py's ``_HONESTY_NOTES``) so the wording cannot drift between call
#: sites. ``{n}`` is filled in with the sample count actually used.
_BARRIER_METHOD_NOTE = (
    "barrier method: energy along a LINEAR interpolation of free length "
    "AND of the two solved equilibria's nodal displacement ({n} samples) "
    "— NOT a re-equilibrated continuation path; each member's engagement "
    "along the path is read off the sign of its own interpolated stretch "
    "(the same idiom rule the solve itself uses), never redistributed "
    "onto its neighbours by an intermediate active-set solve"
)
_BARRIER_LIMITS_NOTE = (
    "barrier is an ESTIMATE, not a certified saddle-point energy: both "
    "interpolated paths are affine in the path parameter and every "
    "unilateral-spring term is a convex function of an affine argument, "
    "so their sum is provably convex in this small-displacement linear "
    "model — an interior energy maximum strictly above both endpoints "
    "cannot occur, which means the sampled interior points add NOTHING "
    "beyond the two endpoints: the reported number is always exactly "
    "|E_a - E_b| (the two states' own energy difference), never a "
    "snap-through hump; a genuine geometrically-nonlinear snap-through "
    "saddle needs large-rotation kinematics this solver does not model "
    "— use this number to screen actuation energy scale, never to "
    "certify a transition"
)


@dataclass
class EquilibriumStabilityResult:
    """One candidate free-length assignment's fate under
    :func:`probe_bistability`: did :func:`solve_complementarity` converge
    for it, and — if so — is the converged active set second-order
    stable (see the module docstring)?"""

    #: The candidate free-length array this result is for.
    free_length: np.ndarray
    #: True when :func:`solve_complementarity` converged (found a
    #: sign-consistent active set) for this ``free_length``.
    solved: bool
    #: True only when ``solved`` and the converged active set's reduced
    #: tangent stiffness is positive definite over the free DOFs — a
    #: genuine stable equilibrium, not merely a solvable one.
    stable: bool
    #: The solve's own result, when ``solved`` is True.
    result: ComplementarityResult | None = None
    #: :class:`ComplementarityError`'s message, when ``solved`` is False.
    error: str | None = None
    #: Why ``stable`` is False despite ``solved`` being True, or why this
    #: state could not be solved at all — empty when ``stable`` is True.
    notes: tuple[str, ...] = ()


@dataclass
class BistabilityResult:
    """Slice-3 two-equilibria/bistability probe result
    (docs/backlog/complementarity-solver.md). ``bistable`` is True only
    when *both* candidates solved and are second-order stable; a
    monostable (or worse) assignment reports each state's own honest fate
    and never fabricates a barrier for a state that is not itself a
    stable equilibrium."""

    #: ``(state for free_length_a, state for free_length_b)``.
    states: tuple[EquilibriumStabilityResult, EquilibriumStabilityResult]
    #: True iff both states solved and are second-order stable.
    bistable: bool
    #: The energy-barrier estimate — see the module docstring's "barrier
    #: estimate" section — or None when ``bistable`` is False (no
    #: invented barrier for a non-equilibrium state).
    barrier: float | None
    #: The sampled energy along the interpolation path (see ``notes`` for
    #: the method), one entry per sample; None alongside ``barrier``.
    barrier_samples: tuple[float, ...] | None
    #: Always explains what was (or was not) computed and why — the
    #: barrier method + its limits when ``bistable``, or which state(s)
    #: failed and how when not.
    notes: tuple[str, ...] = ()


def _unilateral_energy(
    rate: np.ndarray, idiom_arr: np.ndarray, stretch: np.ndarray
) -> float:
    """Per-member unilateral-spring potential energy implied by each
    member's idiom, at the given ``stretch`` (length beyond free length,
    tension-positive): zero while a tension-only member is slack
    (``stretch <= 0``) or a compression-only/must-contact member is
    separated (``stretch >= 0``); the ordinary ``½·rate·stretch²``
    otherwise. This is the same sign rule :func:`solve_complementarity`
    uses to decide activation, evaluated pointwise rather than solved —
    see the module docstring's barrier-estimate section."""
    total = 0.0
    for k in range(stretch.size):
        idm = idiom_arr[k]
        s = float(stretch[k])
        r = float(rate[k])
        if idm == "tension_only":
            total += 0.5 * r * max(0.0, s) ** 2
        elif idm in ("compression_only", "must_contact"):
            total += 0.5 * r * max(0.0, -s) ** 2
        else:
            total += 0.5 * r * s * s
    return total


def _barrier_estimate(
    rate: np.ndarray,
    idiom_arr: np.ndarray,
    lengths: np.ndarray,
    equilibrium: np.ndarray,
    free_length_a: np.ndarray,
    free_length_b: np.ndarray,
    d_a_flat: np.ndarray,
    d_b_flat: np.ndarray,
    samples: int,
) -> tuple[float, tuple[float, ...]]:
    """Sample the unilateral-spring energy along a linear interpolation
    of free length and of nodal displacement between two solved
    equilibria (see the module docstring's barrier-estimate section for
    the method and its limits). Returns ``(barrier, sampled energies)``."""
    energies: list[float] = []
    for t in np.linspace(0.0, 1.0, samples):
        d_flat = (1.0 - t) * d_a_flat + t * d_b_flat
        free_length_t = (1.0 - t) * free_length_a + t * free_length_b
        stretch = lengths - free_length_t - (equilibrium.T @ d_flat)
        energies.append(_unilateral_energy(rate, idiom_arr, stretch))
    energies_t = tuple(energies)
    barrier = max(energies_t) - min(energies_t[0], energies_t[-1])
    return barrier, energies_t


def probe_bistability(
    coords: np.ndarray,
    members: np.ndarray,
    rate: np.ndarray,
    free_length_a: np.ndarray,
    free_length_b: np.ndarray,
    idiom: np.ndarray,
    fixed: np.ndarray,
    loads: np.ndarray | None = None,
    *,
    barrier_samples: int = 21,
    max_iterations: int | None = None,
) -> BistabilityResult:
    """Verify two candidate free-length assignments over the *same*
    topology (``coords``/``members``/``idiom``/``fixed`` shared — in
    practice a photoswitch's ``{trans, cis}`` Δ(end-to-end) on one
    member) each reach a stable complementary equilibrium, and — only
    when both do — report an energy-barrier estimate between them. See
    the module docstring for the second-order-stability check and the
    barrier method's derivation and honesty limits. ``barrier_samples``
    (>= 2) is the number of points sampled along the interpolation
    path."""
    if barrier_samples < 2:
        raise ComplementarityInputError(
            f"barrier_samples must be >= 2 (need at least both endpoints), "
            f"got {barrier_samples}"
        )

    idiom_arr = np.asarray(idiom, dtype=object)
    rate_arr = np.asarray(rate, dtype=float)

    lengths: np.ndarray | None = None
    equilibrium: np.ndarray | None = None
    free: np.ndarray | None = None
    states: list[EquilibriumStabilityResult] = []
    for free_length in (free_length_a, free_length_b):
        try:
            res = solve_complementarity(
                coords,
                members,
                rate,
                free_length,
                idiom,
                fixed,
                loads,
                max_iterations=max_iterations,
            )
        except ComplementarityInputError:
            # A malformed request (bad shape, non-positive rate, unknown
            # idiom, self-loop, ...) is a caller bug, not a physics
            # refusal — propagate it loudly rather than reporting a
            # normal-looking BistabilityResult(bistable=False, ...) for
            # it (main-loop ruling, review 2026-09-12).
            raise
        except ComplementarityError as exc:
            states.append(
                EquilibriumStabilityResult(
                    free_length=np.asarray(free_length, dtype=float),
                    solved=False,
                    stable=False,
                    error=str(exc),
                    notes=(
                        "the active-set solve did not converge for this "
                        "free-length assignment — no equilibrium to report "
                        f"(cause: {exc})",
                    ),
                )
            )
            continue

        if lengths is None:
            # Lazily assembled only once at least one candidate solved
            # (so we know the shared shape inputs were valid) — shared
            # across both states since the topology does not depend on
            # which free_length is being tried.
            coords_arr = np.asarray(coords, dtype=float)
            members_arr = np.asarray(members, dtype=int)
            fixed_arr = np.asarray(fixed, dtype=bool)
            lengths, equilibrium = _geometry(coords_arr, members_arr)
            free = ~fixed_arr.reshape(-1)

        assert equilibrium is not None and free is not None  # set above, same pass
        fl = np.asarray(free_length, dtype=float)
        t0 = rate_arr * (lengths - fl)
        stable, worst = _second_order_stable(
            coords_arr.shape[0],
            members_arr,
            rate_arr,
            t0,
            lengths,
            equilibrium,
            res.active,
            free,
        )
        notes: tuple[str, ...] = ()
        if not stable:
            notes = (
                f"the solve converged (residual {res.residual:.3g}) but the "
                "reduced tangent stiffness is not positive definite over "
                f"the free coordinates (worst eigenvalue {worst:.3g} of its "
                "own scale) — a member's geometric (prestress) stiffness "
                "outweighs the elastic stiffness available to resist it in "
                "some direction (the discrete analogue of buckling); this "
                "is a solvable but not a stable equilibrium",
            )
        states.append(
            EquilibriumStabilityResult(
                free_length=fl,
                solved=True,
                stable=stable,
                result=res,
                notes=notes,
            )
        )

    state_a, state_b = states
    bistable = state_a.solved and state_a.stable and state_b.solved and state_b.stable

    barrier: float | None = None
    barrier_samples_out: tuple[float, ...] | None = None
    notes_out: list[str] = []
    if not bistable:
        failed = []
        for label, state in (("A", state_a), ("B", state_b)):
            if state.solved and state.stable:
                continue
            if not state.solved:
                failed.append(f"state {label}: did not solve ({state.error})")
            else:
                failed.append(f"state {label}: solved but not second-order stable")
        notes_out.append(
            "not bistable: " + "; ".join(failed) + " — no barrier estimate is "
            "reported (an energy comparison against a state that is not "
            "itself a stable equilibrium would not mean anything)"
        )
    else:
        assert state_a.result is not None and state_b.result is not None
        assert lengths is not None and equilibrium is not None
        barrier, barrier_samples_out = _barrier_estimate(
            rate_arr,
            idiom_arr,
            lengths,
            equilibrium,
            state_a.free_length,
            state_b.free_length,
            state_a.result.displacements.reshape(-1),
            state_b.result.displacements.reshape(-1),
            barrier_samples,
        )
        notes_out.append(_BARRIER_METHOD_NOTE.format(n=barrier_samples))
        notes_out.append(_BARRIER_LIMITS_NOTE)

    return BistabilityResult(
        states=(state_a, state_b),
        bistable=bistable,
        barrier=barrier,
        barrier_samples=barrier_samples_out,
        notes=tuple(notes_out),
    )

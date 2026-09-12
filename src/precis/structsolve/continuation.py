"""Geometrically nonlinear axial-network continuation — snap-through /
limit-point tracing and an honest energy-barrier estimate along the
traced equilibrium branch. This is
docs/backlog/structural-solution-space.md slice 5's CORE, and it is the
deferral target both :mod:`precis.structsolve.complementarity`'s barrier
estimate and that doc itself name explicitly: "a real snap-through
barrier needs the geometrically nonlinear sweep."

**Why this module exists — what the linear solver cannot do.**
:func:`~precis.structsolve.complementarity.probe_bistability`'s barrier
estimate samples energy along a *linear interpolation* of free length AND
of the two solved equilibria's nodal displacement, each affine in the
path parameter ``t``; every unilateral-spring term is a convex function
of an affine argument, so the summed energy is *provably* convex in
``t`` — no interior maximum strictly above both endpoints can occur, and
the number it reports collapses to exactly ``|E_a - E_b|``, always
(see that module's docstring for the proof). This module removes the
linearization that forces that collapse: member unit vectors ``n(u)``
are re-evaluated at the *current*, displaced geometry at every Newton
iteration — directions rotate with displacement — which is precisely the
nonlinearity a snap-through needs. The elastic + geometric tangent
stiffness here is the *consistent* tangent of that same current
geometry, standard for a total-Lagrangian two-force (pin-jointed) truss
element:

- **elastic**: ``rate_k · n(u) n(u)ᵀ`` — axial stiffness along the
  *current* member direction.
- **geometric**: ``(t_k / L_k(u)) · (I₃ − n(u) n(u)ᵀ)`` — the existing
  member force resists *transverse* motion away from its own current
  axis; this is the standard truss geometric-stiffness matrix (see e.g.
  Crisfield, *Nonlinear Finite Element Analysis of Solids and
  Structures*, the total-Lagrangian truss element).

Contrast :mod:`~precis.structsolve.complementarity`'s tangent stiffness,
which assembles the geometric term as the *isotropic* force-density
Laplacian ``Ω ⊗ I₃`` (every spatial direction, including the member's
own axis, gets the same ``q = t₀/L`` contribution). That is the
Buchholdt/Schek "cable-net" linearization at the *reference* geometry: it
holds ``n`` fixed at ``n₀`` and, critically, evaluates the geometric term
using the FULL identity rather than the transverse projector
``I₃ − n₀n₀ᵀ`` — an approximation valid, and standard, exactly when
displacements stay small enough that ``n(u) ≈ n₀``. The two forms
provably agree in that small-displacement limit (``n(u) → n₀`` collapses
``(I₃ − nnᵀ)`` and the extra axial term the isotropic form adds becomes
negligible next to the elastic term, which already dominates the axial
direction) and provably disagree once a member's direction rotates
enough to matter — which is exactly the regime a snap-through lives in.
This module is that disagreement, made computable.

**Model.** Same axial-member vocabulary as
:mod:`~precis.structsolve.complementarity`, reused rather than
reinvented: tension-positive member force ``t = rate · (L(u) − L₀)``,
the four sign idioms in :data:`~precis.structsolve.complementarity.IDIOMS`
(``tension_only`` / ``compression_only`` / ``bidirectional`` /
``must_contact``), the same per-coordinate ``fixed`` prescription
convention. ``coords0`` is the reference (undeformed, or as-given)
geometry; ``members``/``rate``/``idiom``/``fixed`` are shared across the
whole path. What is *swept* over the continuation parameter ``t ∈ [0,
1]`` is, affinely: each member's free length (``free_length_start`` →
``free_length_end`` — a photoswitch's ``{trans, cis}`` Δ(end-to-end) on
one member, everything else identical, is the flagship use of this),
the *value* held at any prescribed coordinate (``coords0`` → an optional
``coords_end`` — displacement control, the natural generalization of the
``fixed`` mask's existing "value" to something that can itself move,
needed by the von Mises truss fixture below), and the external load
(``loads_start`` → ``loads_end``, default zero). All three may be swept
independently or together; a caller doing pure free-length continuation
(the photoswitch/tensegrity case) leaves ``coords_end``/``loads_end`` at
their defaults and only ``free_length_end`` differs from
``free_length_start``.

**Unilateral consistency per step (task 3).** At each continuation step,
the active set is re-derived at the *current* geometry by mirroring
:func:`~precis.structsolve.complementarity.solve_complementarity`'s own
member-removal/reinstatement scheme exactly (same sign tolerance, same
cycle detection, same per-idiom legality rule) — nested one level deeper
than that function, because each active-set trial here requires a full
Newton solve (not one linear solve) to find the equilibrium the sign
check is evaluated against. A cable going slack (or a contact
separating) mid-continuation therefore changes which members carry load
exactly as it does in the linear solver, just re-derived at a geometry
that has actually rotated.

**Continuation algorithm (documented choice — task 2's "plain
load-stepping first" branch, not arc-length).** March ``t`` from 0 to 1
in ``1/steps`` increments; at each increment, run the nested Newton +
active-set solve above, warm-started from the previous step's converged
state (predictor = last converged displacement/active set, the cheapest
predictor and the standard one for load/parameter-stepping). On failure
to converge, halve the increment (up to ``max_halving`` times) and retry
from the last converged point — "Newton at each step with step-halving,"
literally. Two distinct things can make a step fail, and both are
reported as a :class:`LimitPoint` when halving is exhausted or, for the
displacement-driven case, detected without any solve failure at all:

- **Newton failure** (``kind='newton_failure'``): the reduced tangent
  stiffness at the current active set is genuinely singular, or Newton
  simply does not converge — the signature of a *force-controlled* fold
  (a limit point in the classical sense: past it, no equilibrium exists
  for a monotonically increasing free-length/load parameter on this
  branch). This is the mode :func:`~precis.structsolve.complementarity`
  users will hit continuing a switched member's free length: per the
  spec, a detected limit point with its energy IS the barrier
  deliverable, even without an arc-length traversal onto the unstable
  branch beyond it.
- **Reaction extremum** (``kind='reaction_extremum'``): for a
  *displacement-controlled* sweep (a moving prescribed coordinate,
  ``coords_end != coords0`` somewhere), the swept coordinate's value is
  never actually unsolvable — Newton keeps converging right through the
  fold, because nothing is asked to jump. What changes is the
  *generalized reaction* the prescription must supply (the force needed
  to keep that coordinate at its prescribed value) — this module's
  answer to "add arc-length only if the fixture needs it to traverse the
  fold": the von Mises two-bar truss fixture below needs to see PAST its
  limit point to report a limit *load*, and prescribing the apex's
  displacement directly (rather than driving the apex by a load
  parameter) gets there with no solvability singularity at all — the
  standard alternative to arc-length for exactly this class of problem,
  and the same method the truss's own closed-form solution is derived
  with. The reaction is read off the residual at every prescribed
  coordinate that actually moves between ``coords0`` and ``coords_end``;
  a sign change in its increment along the branch is the fold. Grid-
  limited accuracy, v1, no auto-refinement: unlike ``newton_failure``
  (refined by this module's own step-halving), the reaction-extremum
  scan reads the sign change directly off the sampled grid — its
  reported location and energy are only as accurate as ``steps``, the
  default (``20``) a coarse screen, not a converged number.

Documented limitation: this is generalized-displacement / plain
load-stepping, not arc-length (Riks/Crisfield) — a branch that folds
*and* is being force- (not displacement-) driven at every relevant
degree of freedom stops at the fold rather than traversing onto the
descending branch. That is an accepted, disclosed gap (task 2's
"honest detection + report" branch), not a hidden one: every
:class:`ContinuationResult` reports which detection fired (or that
neither did — a monostable branch).

**Barrier result (task 4) — a barrier is not a well-depth difference.**
:func:`~precis.structsolve.complementarity.probe_bistability`'s number
is provably always ``|E_a − E_b|`` — a well-depth DIFFERENCE, read off
two independently-solved endpoints, and it is proven (see above) never
to see an interior maximum because its sampling path is affine. Calling
that a "barrier" is only ever correct by coincidence: a barrier and a
well-depth difference are independent quantities that happen to be equal
in the special case of a perfectly symmetric double well and otherwise
routinely disagree, in either direction, depending on how asymmetric the
two wells are — an early framing of this module's acceptance criteria
assumed the disagreement would always run one way (traced barrier
*larger* than the linear ``|ΔE|``); the corrected finding, from this
module's own fixtures, is that it can equally run the other way (a
short climb to the fold from a shallow well, a long drop from a deep
one) — see the near-symmetric and asymmetric test fixtures for both
directions demonstrated side by side.

Energy along the branch is the same per-member unilateral-spring
potential :func:`~precis.structsolve.complementarity._unilateral_energy`
computes for the linear solver, reused directly (not reinvented) — but
evaluated at the *exact* current stretch ``L(u) − free_length(t)`` at
each traced step, not an affine approximation. Because the displacement
path is now the actual nonlinear equilibrium trajectory (not a straight
line between two endpoints) and the unilateral-spring energy is being
sampled along a *curved* path in configuration space, the sum is no
longer provably convex in ``t`` — an interior maximum strictly above
both endpoints is exactly what a genuine snap-through looks like, and
this module can report one, in either of two ways a branch can show it:
a detected :class:`LimitPoint` (Newton genuinely fails, or a driven
coordinate's reaction peaks), or — with no fold anywhere — a *soft*
hump: an interior sample strictly greater than both the branch's own
start and end energy, which a curved (nonlinear) equilibrium path can
produce with every step along the way perfectly solvable (no
singularity, no reaction extremum at all). The second case matters on
its own: it is exactly what
:func:`~precis.structsolve.complementarity.probe_bistability`'s affine
interpolation can *never* produce (see its convexity proof above), so a
fixture built to contrast the two modules needs it, not just the fold
case.

Once a peak ``E*`` is found (by either detection), :class:`ContinuationResult`
reports it as TWO directed numbers, not one, because "the barrier" is
meaningless without saying which side you are climbing from:
``forward_barrier = E* − E_start`` and ``reverse_barrier = E* − E_end``
— typically ``0.0`` for a Newton-failure limit point (the branch simply
stops at the fold, with no far state to climb back from), but NOT
guaranteed to be: the peak search is a genuine ``argmax`` over the
traced energies, so if the branch has an interior maximum strictly
before the fold (a smaller hump on the way to the bigger one),
``reverse_barrier`` honestly reports that positive climb rather than
silently reporting zero. ``escape_barrier = min(forward_barrier,
reverse_barrier)`` is the
number that actually matters for a practical-bistability screen — a
system held in a double well escapes over its LOWER side first, so the
harder-to-leave side is irrelevant to whether it escapes at all.
``delta_e = E_end − E_start`` (signed) is reported alongside, explicitly
labeled NOT a barrier, so a caller can put it next to the linear probe's
own ``|ΔE|`` and see the two numbers are answering different questions.
All four populate together, or (monostable: no fold, no interior
maximum) all report ``None`` together — the same discipline
:func:`~precis.structsolve.complementarity.probe_bistability` uses for a
state that fails its own stability check, never inventing a number from
whatever the endpoint happens to be.

The peak search is deliberately capped at the FIRST detected fold rather
than run over the whole traced branch: a force-controlled branch already
stops there (the two coincide automatically), but a displacement-
controlled one (the von Mises truss fixture) keeps tracing past it —
capping the search is what makes ``forward_barrier`` mean "the climb
needed to reach the nearest obstacle" (the standard snap-through-energy
reading) rather than "however much energy a forced sweep piles up if you
keep driving it well past the obstacle," which would silently inflate
past the physically meaningful number. A consequence, disclosed rather
than hidden: a branch with more than one fold only ever reports the
first one, so on a genuinely multi-fold branch ``reverse_barrier`` is
the climb back up to THAT peak specifically, not necessarily the nearest
obstacle to wherever the branch ends.

:func:`barrier_over_kT` divides a caller-supplied energy (typically
``escape_barrier``, the practical-bistability number — see above) by a
caller-supplied ``kT`` in the same energy units — this module never
picks a temperature or a unit; it stays exactly as unit-agnostic as
:mod:`~precis.structsolve.complementarity`. The molecular-bistability
rule of thumb is tens of ``kT`` for practical room-temperature
bistability (a barrier much below that thermally cycles on its own; one
far above it may never switch under practical drive) — quoted here only
as a rule of thumb, not a threshold this module enforces: this solver
reports *statics* (an equilibrium energy landscape), never rates,
attempt frequencies, or transition-state prefactors, so it cannot itself
say whether a given barrier/kT actually produces bistable behavior at
some timescale — that is an honest gap named, not a computation invented
to paper over it.

Unit-agnostic and pure over passed-in arrays, no store access (the
package docstring's house rules) — same relative-tolerance discipline as
:mod:`~precis.structsolve.complementarity` throughout (reused constants,
not re-derived).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from precis.structsolve.complementarity import (
    _SIGN_RTOL,
    _SINGULAR_RTOL,
    IDIOMS,
    ComplementarityError,
    ComplementarityInputError,
    _geometry,
    _status_for,
    _unilateral_energy,
)

#: Inner Newton line-search halvings (per Newton iteration, distinct from
#: the continuation parameter's own outer step-halving) before giving up
#: on a trial displacement increment improving the residual.
_MAX_LINE_SEARCH_HALVING = 8

#: Same-value discipline as complementarity: relative, never absolute —
#: the same problem at any length/force scale takes the same iteration
#: count and reaches the same limit point.
_NEWTON_RTOL_DEFAULT = 1e-9

#: Relative threshold (against the problem's own member-length scale)
#: for deciding a prescribed coordinate actually moves between
#: ``coords0`` and ``coords_end`` — see ``driven_mask``'s construction
#: in :func:`trace_equilibrium_branch`. Scale-relative, no absolute
#: floor: `np.isclose`'s default `atol=1e-8` would silently treat a
#: genuinely-driven sweep at nm scale (coordinates ~1e-9) as unchanged.
_DRIVEN_RTOL = 1e-9

#: Machine-readable cause of a failed
#: :func:`_solve_step_active_set` attempt — which of its three failure
#: sites actually fired. `kind='newton_failure'` stays the single
#: :class:`LimitPoint` kind for all three (compat with existing
#: `kind`-based branching); `cause` is the new, string-parsing-free way
#: to tell them apart.
_FailureCause = Literal["newton", "active_set_cycle", "budget"]


class ContinuationError(ComplementarityError):
    """No continuation result could be produced at all — the very first
    (``t=0``) equilibrium solve fails, or the active-set iteration cycles
    at some step in a way step-halving cannot resolve. Subclasses
    :class:`~precis.structsolve.complementarity.ComplementarityError` so
    existing ``except ComplementarityError`` call sites keep catching it.
    A branch that starts fine and later hits a genuine limit point is NOT
    an error — that is :class:`LimitPoint`, the expected outcome of a
    snap-through sweep, reported inside a normal
    :class:`ContinuationResult`."""


@dataclass
class ContinuationStep:
    """One converged point on the traced equilibrium branch."""

    #: Continuation parameter, 0..1.
    t: float
    #: (j, 3) displacement from `coords0` at this step.
    displacements: np.ndarray
    #: (b,) member force, tension-positive; 0 for every inactive member.
    forces: np.ndarray
    #: (b,) True where the member is part of this step's active set.
    active: np.ndarray
    #: (b,) dtype=object per-member status, same vocabulary as
    #: :class:`~precis.structsolve.complementarity.ComplementarityResult`.
    status: np.ndarray
    #: Total unilateral-spring energy of all members at this step's
    #: exact (nonlinear) stretch — see the module docstring.
    energy: float
    #: (j, 3) generalized reaction — the force that must be supplied at
    #: every coordinate (meaningful at `fixed` coordinates; ~0 at free
    #: ones by construction of the solve) to hold this equilibrium,
    #: `loads(t) - internal force(u)`.
    reaction: np.ndarray
    #: Newton iterations the converged solve at this step needed.
    newton_iterations: int


@dataclass
class LimitPoint:
    """A detected fold on the traced branch — see the module docstring's
    "Continuation algorithm" section for the two detection modes."""

    #: Continuation parameter at (or just before) the fold.
    t: float
    #: Branch energy at this point.
    energy: float
    #: `'newton_failure'` (force-controlled: no further equilibrium found
    #: continuing the parameter monotonically) or `'reaction_extremum'`
    #: (displacement-controlled: the branch is fully traced, but the
    #: generalized reaction at a driven coordinate peaks here).
    kind: str
    #: Machine-readable reason a `kind='newton_failure'` was raised —
    #: `'newton'` (the Newton solve itself failed to converge, the
    #: classic force-controlled limit-point signature),
    #: `'active_set_cycle'` (the active-set member-removal/
    #: reinstatement loop repeated a member set) or `'budget'` (the
    #: active-set iteration cap was hit). All three still report
    #: `kind='newton_failure'` (compat — existing `kind`-based branching
    #: keeps working); `cause` is the string-parsing-free way to tell
    #: them apart, rather than grepping `notes`. `None` for
    #: `kind='reaction_extremum'` (no failure occurred there at all).
    cause: _FailureCause | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class ContinuationResult:
    """The traced equilibrium branch plus its barrier verdict.

    A barrier and a well-depth difference are DIFFERENT, INDEPENDENT
    quantities — the whole reason this module exists next to
    :func:`~precis.structsolve.complementarity.probe_bistability`, whose
    own number is provably always ``|E_a - E_b|`` (a well-depth
    difference) and never a barrier. Reporting only one directed number
    here (from the start toward the peak) would silently assume the
    well-depth asymmetry is small; it usually isn't, so this reports
    both directions from the SAME detected peak:

    - ``forward_barrier`` (``E* - E_start``): the climb FROM the
      starting equilibrium up to the peak.
    - ``reverse_barrier`` (``E* - E_end``): the climb FROM wherever the
      branch currently ends back up to that SAME peak — typically
      ``0.0`` for a force-controlled Newton-failure limit point (the
      branch stops at the fold, no far state reached to climb back
      from), but NOT a guarantee: the peak search is a genuine
      ``argmax`` over the traced branch, so a branch with an interior
      maximum strictly before the fold reports that positive climb
      honestly rather than forcing zero.
    - ``escape_barrier`` (``min(forward_barrier, reverse_barrier)``):
      the operative number for a practical-bistability-vs-kT screen —
      a system escapes over its LOWER side first, so this, not either
      directed number alone, is what :func:`barrier_over_kT` is usually
      called with.
    - ``delta_e`` (``E_end - E_start``, signed): the well-depth
      difference alone — NOT a barrier, reported so a caller can
      compare it directly against the linear probe's own
      (unsigned) ``|ΔE|`` number and see that the two are unrelated
      quantities (they agree, disagree, or one can be ~0 while the
      other is large, depending on well asymmetry — see the module
      docstring's near-symmetric fixture).

    All four are populated together (a genuine peak was found) or all
    `None` together (monostable: task 4's "reports no barrier rather
    than inventing one"). A branch with more than one fold only ever
    reports the FIRST one encountered from the start (see
    :class:`LimitPoint`) — ``reverse_barrier`` is the climb back up to
    THAT peak specifically, not necessarily the nearest obstacle to
    wherever the branch ends, on a genuinely multi-fold branch.

    Grid-limited accuracy (v1 — no auto-refinement): a
    ``kind='newton_failure'`` fold's location is refined by this
    module's own step-halving (accurate to `max_halving` binary digits
    of `t`), but a ``kind='reaction_extremum'`` fold's location/energy
    is only as accurate as the requested ``steps`` — the reaction-sign-
    change scan reads directly off the sampled grid with no follow-up
    refinement pass. The default ``steps=20`` is a coarse screen;
    tighten it for a published number (the von Mises fixture below uses
    ``steps=4000`` to match its closed form to ~1e-3 relative)."""

    #: Every converged step, `t=0` first.
    branch: tuple[ContinuationStep, ...]
    #: The detected fold, or `None` for a monostable branch (task 4:
    #: "monostable case reports no barrier rather than inventing one").
    limit_point: LimitPoint | None
    #: `E* - branch[0].energy` — the climb from the start to the peak.
    forward_barrier: float | None
    #: `E* - branch[-1].energy` — the climb from wherever the branch
    #: currently ends back to the SAME peak; typically `0.0` when the
    #: branch stopped exactly at the peak (no far state reached to climb
    #: back from), but a genuine `argmax` result, not an enforced
    #: zero — a positive value here means the traced branch had an
    #: interior energy peak before the fold.
    reverse_barrier: float | None
    #: `min(forward_barrier, reverse_barrier)` — the escape-prone side;
    #: the number to compare against kT for a practical-bistability
    #: screen (see :func:`barrier_over_kT`). `None` alongside the two
    #: directed barriers.
    escape_barrier: float | None
    #: `branch[-1].energy - branch[0].energy`, SIGNED — the well-depth
    #: difference ALONE, not a barrier; comparable to the linear probe's
    #: own (unsigned) `|ΔE|`. `None` alongside the barriers above.
    delta_e: float | None
    #: `True` iff the branch reached `t=1` (no limit point stopped it
    #: early — still `True` for a `reaction_extremum` limit point, since
    #: that detection never stops tracing).
    reached_end: bool
    #: Always explains method + what was (or was not) certified.
    notes: tuple[str, ...] = field(default_factory=tuple)


def barrier_over_kT(barrier_energy: float, kT: float) -> float:
    """`barrier_energy / kT`, both in the caller's own energy unit —
    see the module docstring's honesty note: this module reports statics
    only (an equilibrium energy landscape), never rates or prefactors, so
    this ratio is a screening number against the "tens of kT for
    practical bistability" rule of thumb, never a certified switching
    criterion. ``barrier_energy`` is typically a
    :class:`ContinuationResult`'s ``escape_barrier`` (the lower of the
    two directed climbs — a system escapes over its easier side first),
    not ``forward_barrier`` alone, which only answers "how hard is it to
    leave the state I started in" and can understate the true escape
    risk on an asymmetric double well."""
    kT = float(kT)
    if not np.isfinite(kT) or kT <= 0.0:
        raise ComplementarityInputError(
            f"kT must be a finite positive number, got {kT}"
        )
    return float(barrier_energy) / kT


def _internal_force(
    j: int, equilibrium: np.ndarray, forces: np.ndarray, active: np.ndarray
) -> np.ndarray:
    idx = np.flatnonzero(active)
    if idx.size == 0:
        return np.zeros(3 * j)
    return equilibrium[:, idx] @ forces[idx]


def _tangent_stiffness_nonlinear(
    j: int,
    members: np.ndarray,
    rate: np.ndarray,
    forces: np.ndarray,
    lengths: np.ndarray,
    equilibrium: np.ndarray,
    active: np.ndarray,
) -> np.ndarray:
    """The consistent (total-Lagrangian) elastic + geometric tangent of
    the ``active`` member set at the CURRENT geometry implied by
    ``equilibrium``/``lengths`` — see the module docstring for the
    derivation and the contrast with
    :mod:`~precis.structsolve.complementarity`'s isotropic, reference-
    geometry ``Ω ⊗ I₃`` term. Assembled per-member as a dense 3x3 block
    (member counts in this module's fixtures are small; no sparsity
    exploited, matching the linear solver's own posture)."""
    k_total = np.zeros((3 * j, 3 * j))
    eye3 = np.eye(3)
    for k in np.flatnonzero(active):
        ia, ib = int(members[k, 0]), int(members[k, 1])
        n = equilibrium[3 * ia : 3 * ia + 3, k]  # current unit vector a -> b
        nn = np.outer(n, n)
        q = forces[k] / lengths[k]
        k_member = rate[k] * nn + q * (eye3 - nn)
        k_total[3 * ia : 3 * ia + 3, 3 * ia : 3 * ia + 3] += k_member
        k_total[3 * ib : 3 * ib + 3, 3 * ib : 3 * ib + 3] += k_member
        k_total[3 * ia : 3 * ia + 3, 3 * ib : 3 * ib + 3] -= k_member
        k_total[3 * ib : 3 * ib + 3, 3 * ia : 3 * ia + 3] -= k_member
    return k_total


@dataclass
class _NewtonOutcome:
    u: np.ndarray
    converged: bool
    iterations: int
    lengths: np.ndarray
    equilibrium: np.ndarray
    forces: np.ndarray


def _newton_solve(
    coords0: np.ndarray,
    members: np.ndarray,
    rate: np.ndarray,
    free_length: np.ndarray,
    active: np.ndarray,
    free: np.ndarray,
    fixed_flat: np.ndarray,
    prescribed_coords_flat: np.ndarray,
    loads_flat: np.ndarray,
    u_start: np.ndarray,
    tol_rel: float,
    max_newton: int,
) -> _NewtonOutcome:
    """Newton-Raphson, with inner line-search step-halving, for the
    equilibrium of a FIXED active set at the current continuation
    parameter's free length / prescribed values / loads. Distinct from
    the continuation driver's own (coarser) step-halving over the
    continuation parameter itself. ``prescribed_coords_flat`` is an
    ABSOLUTE coordinate target (the current ``_at(t)`` interpolation of
    ``coords0`` -> ``coords_end``) — the displacement enforced at fixed
    dof is the difference from ``coords0``, not the target itself."""
    j = coords0.shape[0]
    coords0_flat = coords0.reshape(-1)
    u = u_start.copy()
    u[fixed_flat] = (prescribed_coords_flat - coords0_flat)[fixed_flat]
    n_free = int(free.sum())

    def _eval(
        u_vec: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        coords_cur = coords0 + u_vec.reshape(j, 3)
        lengths, equilibrium = _geometry(coords_cur, members)
        forces = rate * (lengths - free_length)
        f_int = _internal_force(j, equilibrium, forces, active)
        return lengths, equilibrium, forces, f_int

    lengths, equilibrium, forces, f_int = _eval(u)
    if n_free == 0:
        # Every coordinate is prescribed -- nothing to solve for. The
        # geometry/forces above ARE the answer; this is what makes the
        # von Mises truss fixture's displacement-controlled sweep exact
        # with zero Newton iterations at each step.
        return _NewtonOutcome(u, True, 0, lengths, equilibrium, forces)

    for iteration in range(1, max_newton + 1):
        # Nodal equilibrium at a free coordinate is `f_int(u) + loads = 0`
        # (Schek's balance, the same identity `_geometry`'s callers all
        # share — see the module docstring's derivation), so THIS,
        # not `loads - f_int`, is the residual driven to zero. Paired
        # with `_tangent_stiffness_nonlinear` (which assembles
        # `K = -d(f_int)/du`, verified against finite differences during
        # review), the Newton correction is `du = solve(K, residual)`
        # with no extra sign flip — get either half wrong and Newton
        # walks AWAY from equilibrium instead of toward it.
        residual_free = (loads_flat + f_int)[free]
        # Scale-relative, never an absolute floor (unit-agnosticism — the
        # package's own house rule, tested explicitly at metre vs
        # 1e-9 scale): `rate * length` is a force-scale quantity that is
        # always well-defined (rate > 0, length > 0 are both validated),
        # unlike a hardcoded `1.0` floor, which at a very different force
        # scale (all loads/forces tiny) would silently turn the relative
        # tolerance into an enormous one and mask real non-convergence.
        scale = max(
            float(np.max(np.abs(loads_flat))) if loads_flat.size else 0.0,
            float(np.max(np.abs(f_int))) if f_int.size else 0.0,
            float(np.max(rate) * np.max(lengths)),
        )
        if float(np.max(np.abs(residual_free))) <= tol_rel * scale:
            return _NewtonOutcome(u, True, iteration, lengths, equilibrium, forces)

        k_total = _tangent_stiffness_nonlinear(
            j, members, rate, forces, lengths, equilibrium, active
        )
        k_free = k_total[np.ix_(free, free)]
        svals = np.linalg.svd(k_free, compute_uv=False) if k_free.size else np.array([])
        smax = float(svals[0]) if svals.size else 0.0
        if smax == 0.0 or float(svals[-1]) <= _SINGULAR_RTOL * smax:
            # Singular tangent -- the classic force-controlled limit-
            # point signature; report Newton failure, not a bogus solve.
            return _NewtonOutcome(u, False, iteration, lengths, equilibrium, forces)

        du_free = np.linalg.solve(k_free, residual_free)
        base_norm = float(np.max(np.abs(residual_free)))
        step_scale = 1.0
        u_trial = u.copy()
        improved = False
        for _ in range(_MAX_LINE_SEARCH_HALVING + 1):
            u_trial[free] = u[free] + step_scale * du_free
            _, _, _, f_int_trial = _eval(u_trial)
            trial_norm = float(np.max(np.abs((loads_flat + f_int_trial)[free])))
            if trial_norm < base_norm:
                improved = True
                break
            if step_scale <= 2.0**-_MAX_LINE_SEARCH_HALVING:
                break
            step_scale *= 0.5
        u = u_trial
        lengths, equilibrium, forces, f_int = _eval(u)
        if not improved:
            return _NewtonOutcome(u, False, iteration, lengths, equilibrium, forces)

    return _NewtonOutcome(u, False, max_newton, lengths, equilibrium, forces)


def _solve_step_active_set(
    coords0: np.ndarray,
    members: np.ndarray,
    rate: np.ndarray,
    free_length: np.ndarray,
    idiom_arr: np.ndarray,
    free: np.ndarray,
    fixed_flat: np.ndarray,
    prescribed_flat: np.ndarray,
    loads_flat: np.ndarray,
    u_guess: np.ndarray,
    active_guess: np.ndarray,
    tol_rel: float,
    max_newton: int,
    max_active_iterations: int,
) -> tuple[tuple[np.ndarray, _NewtonOutcome] | None, str | None, _FailureCause | None]:
    """Mirrors
    :func:`~precis.structsolve.complementarity.solve_complementarity`'s
    active-set member-removal/reinstatement loop exactly (same sign
    tolerance, same cycle detection), nested around a full Newton solve
    per trial active set rather than one linear solve. Returns
    ``((active, outcome), None, None)`` on success or
    ``(None, reason, cause)`` on failure — never raises, so the
    continuation driver can decide whether to halve the continuation
    step or treat this as a limit point. ``cause`` is the machine-
    readable counterpart of the human-readable ``reason`` string, one of
    :data:`_FailureCause`."""
    b = members.shape[0]
    active = active_guess.copy()
    visited: set[frozenset[int]] = set()
    iteration = 0
    while True:
        iteration += 1
        if iteration > max_active_iterations:
            return (
                None,
                "active-set iteration budget exhausted at this continuation step",
                "budget",
            )
        signature = frozenset(np.flatnonzero(active).tolist())
        if signature in visited:
            return (
                None,
                "the active set is cycling at this continuation step",
                "active_set_cycle",
            )
        visited.add(signature)

        outcome = _newton_solve(
            coords0,
            members,
            rate,
            free_length,
            active,
            free,
            fixed_flat,
            prescribed_flat,
            loads_flat,
            u_guess,
            tol_rel,
            max_newton,
        )
        if not outcome.converged:
            return None, "Newton did not converge for this active set", "newton"

        forces_all = outcome.forces
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
            return (active, outcome), None, None
        active = new_active
        u_guess = outcome.u


def trace_equilibrium_branch(
    coords0: np.ndarray,
    members: np.ndarray,
    rate: np.ndarray,
    free_length_start: np.ndarray,
    free_length_end: np.ndarray,
    idiom: np.ndarray,
    fixed: np.ndarray,
    coords_end: np.ndarray | None = None,
    loads_start: np.ndarray | None = None,
    loads_end: np.ndarray | None = None,
    *,
    steps: int = 20,
    max_halving: int = 6,
    newton_tol: float = _NEWTON_RTOL_DEFAULT,
    max_newton: int = 50,
    max_active_iterations: int | None = None,
) -> ContinuationResult:
    """Trace the geometrically nonlinear equilibrium branch as the
    continuation parameter ``t`` sweeps 0 -> 1, affinely interpolating
    ``free_length`` (``free_length_start`` -> ``free_length_end``, the
    "switched member's free length" case — everything else in the two
    arrays identical for a single-member photoswitch actuation), the
    prescribed-coordinate VALUES held at ``fixed`` positions (``coords0``
    -> ``coords_end``, default unchanged — displacement control, needed
    by problems like the von Mises truss where the driven quantity is a
    node position rather than a free length), and external load
    (``loads_start`` -> ``loads_end``, default zero). See the module
    docstring for the algorithm, the two limit-point detection modes, and
    the barrier convention.

    ``coords0`` (j, 3); ``members`` (b, 2) integer node indices; ``rate``
    (b,) per-member axial stiffness, finite and > 0; ``free_length_start``
    / ``free_length_end`` (b,) finite and > 0; ``idiom`` (b,) one of
    :data:`~precis.structsolve.complementarity.IDIOMS`; ``fixed`` (j, 3)
    bool, True = prescribed. Raises
    :class:`~precis.structsolve.complementarity.ComplementarityInputError`
    for a malformed request, :class:`ContinuationError` when even the
    starting (``t=0``) state has no equilibrium."""
    coords0 = np.asarray(coords0, dtype=float)
    members = np.asarray(members, dtype=int)
    rate = np.asarray(rate, dtype=float)
    free_length_start = np.asarray(free_length_start, dtype=float)
    free_length_end = np.asarray(free_length_end, dtype=float)
    idiom_arr = np.asarray(idiom, dtype=object)
    fixed = np.asarray(fixed, dtype=bool)

    if coords0.ndim != 2 or coords0.shape[1] != 3:
        raise ComplementarityInputError(f"coords0 must be (j, 3), got {coords0.shape}")
    j = coords0.shape[0]
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
    if free_length_start.shape != (b,):
        raise ComplementarityInputError(f"free_length_start must be ({b},)")
    if free_length_end.shape != (b,):
        raise ComplementarityInputError(f"free_length_end must be ({b},)")
    if idiom_arr.shape != (b,):
        raise ComplementarityInputError(f"idiom must be ({b},) — one idiom per member")
    if fixed.shape != (j, 3):
        raise ComplementarityInputError(
            f"fixed must be ({j}, 3) bool, got {fixed.shape}"
        )
    if not np.all(np.isfinite(rate)) or np.any(rate <= 0.0):
        raise ComplementarityInputError(
            "every rate must be a finite positive number (a non-positive axial stiffness is not a member)"
        )
    if not np.all(np.isfinite(free_length_start)) or np.any(free_length_start <= 0.0):
        raise ComplementarityInputError(
            "every free_length_start must be a finite positive number"
        )
    if not np.all(np.isfinite(free_length_end)) or np.any(free_length_end <= 0.0):
        raise ComplementarityInputError(
            "every free_length_end must be a finite positive number"
        )
    if np.any(members < 0) or np.any(members >= j):
        raise ComplementarityInputError(f"member node indices must lie in [0, {j})")
    if np.any(members[:, 0] == members[:, 1]):
        raise ComplementarityInputError(
            "a member may not join a node to itself — drop self-loops before solving"
        )
    bad_idioms = sorted(set(idiom_arr.tolist()) - set(IDIOMS), key=str)
    if bad_idioms:
        raise ComplementarityInputError(
            f"unknown idiom(s) {bad_idioms} — must be one of {IDIOMS}"
        )
    if steps < 1:
        raise ComplementarityInputError(f"steps must be >= 1, got {steps}")
    if max_halving < 0:
        raise ComplementarityInputError(f"max_halving must be >= 0, got {max_halving}")
    if not np.all(np.isfinite(coords0[fixed])):
        raise ComplementarityInputError("prescribed coordinates must be finite")

    coords_end_arr = (
        coords0.copy() if coords_end is None else np.asarray(coords_end, dtype=float)
    )
    if coords_end_arr.shape != (j, 3):
        raise ComplementarityInputError(
            f"coords_end must be ({j}, 3), got {coords_end_arr.shape}"
        )
    if not np.all(np.isfinite(coords_end_arr[fixed])):
        raise ComplementarityInputError("prescribed coordinates must be finite")

    loads_start_arr = (
        np.zeros((j, 3))
        if loads_start is None
        else np.asarray(loads_start, dtype=float)
    )
    if loads_start_arr.shape != (j, 3):
        raise ComplementarityInputError(
            f"loads_start must be ({j}, 3), got {loads_start_arr.shape}"
        )
    loads_end_arr = (
        loads_start_arr.copy()
        if loads_end is None
        else np.asarray(loads_end, dtype=float)
    )
    if loads_end_arr.shape != (j, 3):
        raise ComplementarityInputError(
            f"loads_end must be ({j}, 3), got {loads_end_arr.shape}"
        )
    if not np.all(np.isfinite(loads_start_arr)) or not np.all(
        np.isfinite(loads_end_arr)
    ):
        raise ComplementarityInputError("loads must be finite")

    fixed_flat = fixed.reshape(-1)
    free = ~fixed_flat
    prescribed_start_flat = coords0.reshape(-1)
    prescribed_end_flat = coords_end_arr.reshape(-1)
    loads_start_flat = loads_start_arr.reshape(-1)
    loads_end_flat = loads_end_arr.reshape(-1)
    max_active_iter = (
        max_active_iterations if max_active_iterations is not None else max(50, 4 * b)
    )
    # Which prescribed coordinates actually MOVE between `coords0` and
    # `coords_end` (the "driven" dof the reaction-extremum scan below
    # tracks). Scale-relative to the problem's own member-length scale,
    # no absolute floor — `np.isclose`'s default `atol=1e-8` silently
    # treats a genuinely-driven sweep at nm scale (coordinates ~1e-9) as
    # unchanged, which is exactly wrong: `2e-9` apart is NOT noise there,
    # it is the whole sweep. `_geometry` also validates `members` here
    # (zero-length check), a few lines earlier than the t=0 solve would
    # anyway.
    lengths0_for_scale, _ = _geometry(coords0, members)
    length_scale = float(np.max(lengths0_for_scale))
    coord_diff = prescribed_end_flat - prescribed_start_flat
    driven_mask = fixed_flat & (np.abs(coord_diff) > _DRIVEN_RTOL * length_scale)

    def _at(t: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        fl = (1.0 - t) * free_length_start + t * free_length_end
        pf = (1.0 - t) * prescribed_start_flat + t * prescribed_end_flat
        lf = (1.0 - t) * loads_start_flat + t * loads_end_flat
        return fl, pf, lf

    def _record(
        t: float, active: np.ndarray, outcome: _NewtonOutcome
    ) -> ContinuationStep:
        fl, _pf, lf = _at(t)
        status = np.array(
            [
                _status_for(
                    str(idiom_arr[k]), bool(active[k]), float(outcome.forces[k])
                )
                for k in range(b)
            ],
            dtype=object,
        )
        energy = _unilateral_energy(rate, idiom_arr, outcome.lengths - fl)
        # The support/reaction force needed at a prescribed coordinate,
        # given the SAME equilibrium identity as the Newton residual
        # above (`f_int(u) + loads = 0`): whatever `loads` does not
        # already supply there, the reaction must — `-(loads + f_int)`.
        reaction = -(
            lf + _internal_force(j, outcome.equilibrium, outcome.forces, active)
        ).reshape(j, 3)
        return ContinuationStep(
            t=t,
            displacements=outcome.u.reshape(j, 3).copy(),
            forces=np.where(active, outcome.forces, 0.0),
            active=active.copy(),
            status=status,
            energy=energy,
            reaction=reaction,
            newton_iterations=outcome.iterations,
        )

    fl0, pf0, lf0 = _at(0.0)
    active0 = np.ones(b, dtype=bool)
    result0, reason0, _cause0 = _solve_step_active_set(
        coords0,
        members,
        rate,
        fl0,
        idiom_arr,
        free,
        fixed_flat,
        pf0,
        lf0,
        np.zeros(3 * j),
        active0,
        newton_tol,
        max_newton,
        max_active_iter,
    )
    if result0 is None:
        raise ContinuationError(
            f"no equilibrium at the starting state (t=0): {reason0}"
        )
    active, outcome0 = result0
    branch: list[ContinuationStep] = [_record(0.0, active, outcome0)]
    u_guess = outcome0.u

    t = 0.0
    dt = 1.0 / steps
    halving = 0
    limit_point: LimitPoint | None = None
    while t < 1.0 - 1e-12:
        t_target = min(t + dt, 1.0)
        fl, pf, lf = _at(t_target)
        result, reason, cause = _solve_step_active_set(
            coords0,
            members,
            rate,
            fl,
            idiom_arr,
            free,
            fixed_flat,
            pf,
            lf,
            u_guess,
            active,
            newton_tol,
            max_newton,
            max_active_iter,
        )
        if result is not None:
            active, outcome = result
            branch.append(_record(t_target, active, outcome))
            u_guess = outcome.u
            t = t_target
            halving = 0
            dt = 1.0 / steps
        else:
            halving += 1
            if halving > max_halving:
                limit_point = LimitPoint(
                    t=t,
                    energy=branch[-1].energy,
                    kind="newton_failure",
                    cause=cause,
                    notes=(
                        f"the active-set Newton solve failed to converge continuing "
                        f"past t={t:.6g} even after {max_halving} step-halvings "
                        f"(cause: {reason}) — read as a force-controlled limit point: "
                        "no further equilibrium was found continuing this parameter "
                        "monotonically; arc-length continuation was not needed/used "
                        "(see module docstring) so the branch stops here",
                    ),
                )
                break
            dt /= 2.0

    limit_point_index = len(branch) - 1  # newton_failure: branch already stops there
    if limit_point is None and np.any(driven_mask):
        reaction_sum = [
            float(np.sum(step.reaction.reshape(-1)[driven_mask])) for step in branch
        ]
        increments = np.diff(reaction_sum)
        for i in range(1, len(increments)):
            if increments[i - 1] == 0.0 or increments[i] == 0.0:
                continue
            if np.sign(increments[i]) != np.sign(increments[i - 1]):
                limit_point_index = i
                limit_point = LimitPoint(
                    t=branch[i].t,
                    energy=branch[i].energy,
                    kind="reaction_extremum",
                    notes=(
                        f"the generalized reaction at the driven (prescribed-value) "
                        f"coordinate(s) peaks near t={branch[i].t:.6g} — the classic "
                        "force-controlled limit load, read off a displacement-"
                        "controlled sweep that has no solvability singularity there "
                        "(the standard alternative to arc-length for this class of "
                        "problem — see module docstring)",
                    ),
                )
                break

    energies = [s.energy for s in branch]
    start_energy = energies[0]
    end_energy = energies[-1]

    notes: list[str] = [
        f"method: load/parameter-stepping continuation, {len(branch) - 1} accepted "
        f"step(s) (requested steps={steps}, max_halving={max_halving}) — the "
        "geometrically nonlinear tangent of the module docstring, NOT arc-length; "
        "certifies branch-following energy along one traced equilibrium path, not a "
        "certified global saddle-point search over all possible paths",
    ]
    # Restrict the peak search to the branch UP TO the first detected
    # fold when there is one: past it, a displacement-controlled sweep
    # keeps tracing but is no longer climbing toward an obstacle — the
    # classical "snap-through energy" is the climb TO the fold, not
    # whatever a continued forced sweep piles up beyond it (see module
    # docstring). With no fold, the whole traced branch is the window.
    window = energies[: limit_point_index + 1] if limit_point is not None else energies
    peak_idx = int(np.argmax(window))
    peak_energy = window[peak_idx]
    # A genuine hump doesn't require a solvability failure or a reaction
    # extremum to be real: an interior energy maximum strictly above
    # both the window's own start and end IS the snap-through signature
    # — a curved (nonlinear) equilibrium path can climb over one and
    # come back down with every step along the way perfectly solvable
    # (no fold at all), which is exactly the case
    # `~precis.structsolve.complementarity.probe_bistability`'s affine
    # path can never produce (see module docstring's convexity proof).
    is_interior_peak = 0 < peak_idx < len(window) - 1

    if limit_point is not None or is_interior_peak:
        forward_value = peak_energy - start_energy
        reverse_value = peak_energy - end_energy
        forward_barrier: float | None = forward_value
        reverse_barrier: float | None = reverse_value
        escape_barrier: float | None = min(forward_value, reverse_value)
        delta_e: float | None = end_energy - start_energy
        if limit_point is not None:
            notes.append(
                f"limit point detected ({limit_point.kind}) at t={limit_point.t:.6g}, "
                f"energy {limit_point.energy:.6g} — forward_barrier = this peak minus "
                "the starting equilibrium's energy, reverse_barrier = this peak minus "
                "wherever the branch currently ends (0.0 if the branch stopped exactly "
                "at the fold); NEITHER is `delta_e` (branch[-1] - branch[0], signed, "
                "not a barrier) or the linear probe's own |ΔE| — barrier and well-"
                "depth difference are independent quantities (see the dataclass "
                "docstring)"
            )
            notes.extend(limit_point.notes)
        else:
            notes.append(
                f"interior energy maximum at t={branch[peak_idx].t:.6g} "
                f"(energy {peak_energy:.6g}), strictly above both the branch's start "
                f"({start_energy:.6g}) and end ({window[-1]:.6g}) — a genuine "
                "snap-through hump with no solvability failure anywhere along the "
                "traced path (no limit_point to report); forward_barrier/"
                "reverse_barrier are this maximum minus the start/end energy"
            )
        notes.append(
            f"forward_barrier={forward_barrier:.6g}, reverse_barrier={reverse_barrier:.6g}, "
            f"escape_barrier={escape_barrier:.6g} (the lower of the two — the "
            f"practical-bistability-vs-kT number), delta_e={delta_e:.6g} (signed "
            "well-depth difference, NOT a barrier)"
        )
    else:
        forward_barrier = None
        reverse_barrier = None
        escape_barrier = None
        delta_e = None
        notes.append(
            "monostable: no limit point was detected tracing this parameter to its "
            "endpoint, and the branch energy has no interior maximum strictly above "
            "both endpoints — so no snap-through barrier is reported (inventing one "
            "from the endpoint energy would misrepresent ordinary monotonic loading "
            "as a bistable transition)"
        )

    return ContinuationResult(
        branch=tuple(branch),
        limit_point=limit_point,
        forward_barrier=forward_barrier,
        reverse_barrier=reverse_barrier,
        escape_barrier=escape_barrier,
        delta_e=delta_e,
        reached_end=(t >= 1.0 - 1e-9),
        notes=tuple(notes),
    )

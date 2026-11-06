"""Configuration-space sweep — exact collision windows for one swept joint.

``view='sweep'`` (:meth:`precis.handlers.cad.CadHandler._render_sweep`) used
to sample a joint's travel at ``args.n`` evenly spaced states and check the
full contact graph at each — which can skip a collision thinner than the
sample spacing, costs ``n`` gap evaluations even for a pair nowhere near
contact, and only ever says "hit at sample i", never where the interference
starts and stops.

This module is conservative advancement (CCD) over the one scalar degree of
freedom a sweep actually varies (the swept joint, plus anything a gear/belt
couples to it is just another point on the same curve — still one DOF): a
pair with signed gap ``g`` at joint value ``q`` cannot touch within
``|dq| <= g / v``, where ``v`` is a caller-supplied bound on how fast any
point of the moving material can move per unit ``q``. Stepping by that bound
never skips a sign change (clear → interfering or back), so it brackets every
collision; bisecting each bracket to a caller-given tolerance then gives the
window edges to that precision. Far from an obstacle the bound lets the step
grow large (``g`` is large); near one it shrinks — but never below a floor,
which is also what keeps a pair held in permanent near-zero contact (a
joint's own hinge-pin-and-its-bore partners, who touch at every state by
construction) from stalling the walk: see ``persistent_contact`` below.

The speed bound ``v`` and the gap function are both the caller's concern —
this module is pure cspace-walk arithmetic, agnostic of what "gap" or "the
design at q" even mean. :meth:`CadHandler._render_sweep` supplies both: the
existing SDF clearance (:func:`precis.cad.relate._clearance`, unchanged —
speeding that up is a separate, out-of-scope effort) and a speed bound built
from the joint's own axis and the moving body's AABB.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

#: A gap sample: signed distance (negative = interference) and the query's
#: own "can't tell clear from touching" band (:data:`precis.cad.relate.
#: CONTACT_TOL_REL` of its governing length) — never an unqualified sign.
GapFn = Callable[[float], tuple[float, float]]

#: Bisection's own iteration ceiling, independent of ``tol``: halving even
#: a half-turn bracket (the largest ``min_step`` can be, at the smallest
#: legal ``eval_cap``) this many times already lands under 2° / 2 mm — far
#: past what a report needs, and a small, fixed cost per crossing regardless
#: of how tight the caller's ``tol`` asks to go (reaching the exact
#: ``CONTACT_TOL_REL`` tolerance a pair's governing length implies can cost
#: far more evaluations than the walk itself did to find the bracket).
_BISECT_MAX_ITERS = 4


@dataclass(frozen=True)
class Interval:
    """One collision window ``[lo, hi]``, in the swept joint's own q units
    (radians for an angle-valued joint, metres for prismatic)."""

    lo: float
    hi: float


@dataclass(frozen=True)
class PairSweep:
    """One pair's cspace walk over one joint's full travel.

    ``persistent_contact`` is the joint's own hinge-pin-and-its-bore case: the
    pair never reads clearly clear (every sample's ``|gap| <= resolution``)
    and never reads clearly interfering beyond that resolution band either —
    it is touching throughout by construction, not a collision to report as
    one. ``capped`` means the evaluation cap was hit before the walk reached
    ``hi`` — the caller must refuse rather than report a silent partial
    answer; ``intervals`` is then meaningless and left empty.
    """

    intervals: tuple[Interval, ...]
    persistent_contact: bool
    evaluations: int
    capped: bool


def sweep_pair(
    gap_at: GapFn,
    lo: float,
    hi: float,
    speed_bound: float,
    *,
    tol: float,
    eval_cap: int,
) -> PairSweep:
    """Walk ``[lo, hi]`` once, bracketing and bisecting every sign change of
    ``gap_at(q)`` against its own resolution band.

    ``speed_bound`` bounds ``|d(gap)/dq|`` everywhere on the range (0 is
    legal — a pair that cannot possibly move relative to each other under
    this sweep — and falls back to one full-range step). ``tol`` is the
    bisection's stopping width in ``q`` units (the caller converts a length
    tolerance to ``q`` via ``tol_length / speed_bound``). ``eval_cap`` bounds
    the **walk's own** ``gap_at`` calls — the ones that cover ``[lo, hi]`` —
    reached before ``hi`` is covered, the walk gives up (``capped=True``)
    rather than guess at the uncovered remainder. Bisection calls, which
    only ever refine a bracket the walk already found, are not counted
    against it (:data:`_BISECT_MAX_ITERS` bounds those instead) — covering
    the full range is the thing that must never be a silent partial
    answer; polishing an edge already found is a strictly bounded, separate
    cost. Both kinds of call are counted in the returned ``evaluations``.
    """
    evaluations = 0
    walk_evaluations = 0

    def walk_sample(q: float) -> tuple[float, float]:
        nonlocal evaluations, walk_evaluations
        evaluations += 1
        walk_evaluations += 1
        return gap_at(q)

    def bisect_sample(q: float) -> tuple[float, float]:
        nonlocal evaluations
        evaluations += 1
        return gap_at(q)

    span = hi - lo
    if span <= 0.0:
        g, res = walk_sample(lo)
        interfering = g < -res
        return PairSweep(
            (Interval(lo, hi),) if interfering else (),
            not interfering and not (g > res),
            evaluations,
            False,
        )

    # The floor is what stops a near-zero gap (about to cross, or a
    # permanently-touching pair) from collapsing the step to nothing — the
    # same granularity the old uniform sampling used, so a pair that needs
    # it costs no more than before.
    min_step = max(span / max(1, eval_cap - 1), tol)

    g_prev, resolution = walk_sample(lo)
    q_prev = lo
    in_collision = g_prev < -resolution
    ever_clear = g_prev > resolution
    start: float | None = lo if in_collision else None
    intervals: list[Interval] = []
    q = lo
    capped = False
    while q < hi:
        if walk_evaluations >= eval_cap:
            capped = True
            break
        step = min_step
        if speed_bound > 0.0:
            step = max(min_step, abs(g_prev) / speed_bound)
        q = min(q_prev + step, hi)
        g, resolution = walk_sample(q)
        ever_clear = ever_clear or g > resolution
        now_collision = g < -resolution
        if now_collision != in_collision:
            edge = _bisect(bisect_sample, q_prev, in_collision, q, tol)
            if in_collision:
                assert start is not None
                intervals.append(Interval(start, edge))
                start = None
            else:
                start = edge
            in_collision = now_collision
        q_prev, g_prev = q, g
    if capped:
        return PairSweep((), False, evaluations, True)
    if in_collision:
        assert start is not None
        intervals.append(Interval(start, hi))
    persistent = not intervals and not ever_clear
    return PairSweep(tuple(intervals), persistent, evaluations, False)


def _bisect(
    sample: Callable[[float], tuple[float, float]],
    q_lo: float,
    lo_interfering: bool,
    q_hi: float,
    tol: float,
) -> float:
    """Narrow ``[q_lo, q_hi]`` — known to straddle a clear/interfering sign
    change, with ``q_lo``'s side already classified as ``lo_interfering`` —
    to ``tol``, and return its midpoint."""
    for _ in range(_BISECT_MAX_ITERS):
        if q_hi - q_lo <= tol:
            break
        mid = 0.5 * (q_lo + q_hi)
        g_mid, res_mid = sample(mid)
        if (g_mid < -res_mid) == lo_interfering:
            q_lo = mid
        else:
            q_hi = mid
    return 0.5 * (q_lo + q_hi)

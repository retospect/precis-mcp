"""Quest Pareto frontier — the non-dominated candidate materials.

Slice 4b of the quest layer (``quest-layer`` (git-only) §Materials are
`structure` servers). Every candidate a quest tries is a `structure` that
``serves`` it, carrying its relax **measures** (energy, max force, …). "Do
better" = push the **Pareto frontier** of those measures against the quest's
objective vector (its rubric). This module is the read-time computation of that
frontier: the non-dominated set is *the current best*, the dominated set is
*explored-and-beaten*, the **provisional** set is *measured-but-unconfirmed*
(an untrusted barrier, or a barrier with no converged relax yet — see
:class:`ProvisionalCandidate`), and the un-evaluated set is *awaiting a sim*
(never measured at all).

The objective vector (which measures, minimise or maximise) is the machine
reading of the quest's prose rubric — an open question (docs, slice-4 Q3). For
now it defaults to **minimise energy** and can be overridden per quest via
``meta.rubric_objectives = [{"key": "energy", "sense": "min"}, …]``.

A quest may additionally declare a **composite** objective — a weighted sum of
other measures, human-set at seed time (the potential-lever rubric): ``meta.rubric_composite = {"key": "score", "weights": {"barrier":
1.0, "U_L_abs": 0.5}}``. :func:`_apply_rubric_composite` computes it onto each
candidate at frontier-assembly time (only when every weighted component is
present — no partial sums) so ``rubric_objectives`` can reference the
composite's ``key`` like any other measure.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Energy-degeneracy tripwire tolerance (eV) — sub-DFT-noise convergence gap,
#: see :func:`_flag_energy_twins`.
_ENERGY_TWIN_EPS = 0.002

#: Default objective when a quest declares no rubric: the lowest-energy
#: (most stable) converged candidate wins.
DEFAULT_OBJECTIVES: tuple[tuple[str, str], ...] = (("energy", "min"),)

_VALID_SENSES = frozenset({"min", "max"})


@dataclass(frozen=True)
class Candidate:
    """A candidate material + the measures of its best converged relax."""

    ref_id: int
    handle: str
    name: str
    measures: dict[str, float]
    converged: bool
    #: The candidate's point in the quest's named param space (``meta.params``,
    #: §7.8). Rides along for a later optimizer advisor; never a ranking measure.
    params: dict[str, Any] = field(default_factory=dict)
    #: Non-ranking diagnostic flags stamped by harvest (e.g. the pathway
    #: quality verdict — ``barrier_trusted``/``barrier_neb_failed``/
    #: ``barrier_desorbed``, see :func:`precis.quest.compute._pathway_quality`).
    #: An untrusted barrier is excluded from ``measures`` entirely (it must
    #: not rank or dominate — "noise should be excluded from ranking"); its
    #: raw value survives here as ``barrier_untrusted_value`` purely for
    #: display (:func:`leaderboard`). Rides along for the leaderboard's
    #: quality column and :func:`precis.quest.graduate.graduate_frontier`'s
    #: belt-and-suspenders gate; never a measure.
    flags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProvisionalCandidate:
    """A candidate with at least one measured-but-unconfirmed objective value.

    Product decision (prod audit of quest 164903 — 26 "awaiting a sim"
    candidates that had actually been measured): an untrusted barrier, or a
    barrier harvested with no converged relax, must not render
    indistinguishable from "never tried" — it becomes visible here, clearly
    marked, without touching the strict trust semantics
    :mod:`precis.quest.graduate` gates on (still reads
    ``candidate.flags['barrier_trusted'] is True`` unchanged).

    Wraps the underlying :class:`Candidate` **unmodified** — ``candidate.
    measures``/``candidate.flags`` are exactly what :func:`pareto_split` saw
    (an untrusted barrier is still absent from ``candidate.measures``, still
    excluded from any real ranking). ``measures`` here is a *separate* merged
    view built only for provisional display/ranking: trusted values where
    present, backfilled with the untrusted values :func:`_candidate_from_structure`
    stashed onto ``flags[f"{key}_untrusted_value"]`` — ``untrusted_keys`` names
    which of those came from that backfill rather than a trusted source.
    ``reasons`` is a short human-readable list of why the candidate isn't
    confirmed (derived from the same flags/warnings, no new trust logic).
    ``on_frontier`` marks membership in the *provisional* Pareto frontier
    (:func:`_provisional_split`) — a separate, non-authoritative split
    computed over confirmed + provisional candidates on the same objectives;
    the confirmed ``FrontierResult.frontier`` is unaffected either way.
    """

    candidate: Candidate
    measures: dict[str, float]
    untrusted_keys: frozenset[str]
    reasons: list[str]
    on_frontier: bool = False


@dataclass(frozen=True)
class FrontierResult:
    objectives: list[tuple[str, str]]
    frontier: list[Candidate] = field(default_factory=list)  # non-dominated
    dominated: list[Candidate] = field(default_factory=list)  # explored + beaten
    #: Measured-but-unconfirmed (untrusted value, or no converged relax) —
    #: additive field (§Cycle — untrusted-barrier visibility): pulled OUT of
    #: ``unevaluated`` so "awaiting a sim" means genuinely never measured.
    #: Every existing caller that only reads ``objectives``/``frontier``/
    #: ``dominated``/``unevaluated`` keeps working unchanged; a caller that
    #: wants the newly-surfaced candidates reads this list too.
    provisional: list[ProvisionalCandidate] = field(default_factory=list)
    unevaluated: list[Candidate] = field(default_factory=list)  # no measures yet


#: Quest hub v2 / Cycle C J4 — the Pareto-scatter axis choice. A starter pick
#: (Reto, 2026-07-25). Kinetics cutover: this pair is now only the
#: **fallback** a quest falls back to when it declares fewer than two
#: rubric objectives (:func:`plot_axes_for`) — a quest with >= 2 declared
#: objectives plots its own first two instead, so a catalyst quest's
#: scatter shows its real headline trade-off (``log_tof`` vs ``atom_cost``)
#: rather than this starter pick.
#:
#: X = "highest barrier" → the autocatpath rate-limiting barrier
#: :func:`compute._autocatpath_measures_from_job` stamps onto a candidate's own
#: ``meta`` (harvested by :func:`_candidate_from_structure` above as the
#: ``"barrier"`` measure) — an exact match, no substitution needed.
#:
#: Y = "highest intermediate energy" → no separate "intermediate" concept
#: exists on a candidate yet (a quest's candidates ARE the structures, one
#: `structure` server per candidate material/intermediate — see the module
#: docstring), so the closest available measure is the candidate's own
#: relaxed ``"energy"`` (from ``struct_runs``, the default quest objective —
#: see :data:`DEFAULT_OBJECTIVES`): a candidate's relax energy literally IS
#: that intermediate's energy.
PARETO_X_MEASURE = "barrier"
PARETO_X_LABEL = "Barrier (eV)"
PARETO_Y_MEASURE = "energy"
PARETO_Y_LABEL = "Relaxed energy (eV)"

#: Human-readable axis labels for measures the scatter/PNG twin might plot —
#: read by :func:`plot_axes_for` for a quest's own declared axes (an unknown
#: key falls back to the bare key itself, so a future measure never crashes
#: the label lookup, just shows unpretty).
_AXIS_LABELS: dict[str, str] = {
    "log_tof": "log₁₀ TOF (site⁻¹ s⁻¹)",
    "atom_cost": "log₁₀ atom cost ($/kg)",
    "barrier": "Barrier (eV)",
    "energy": "Relaxed energy (eV)",
    "selectivity_margin": "Selectivity margin (eV)",
    "poison_margin": "Poison margin (eV)",
}


def axis_label_for(key: str) -> str:
    return _AXIS_LABELS.get(key, key)


def plot_axes_for(
    quest_meta: dict[str, Any] | None, objectives: list[tuple[str, str]]
) -> tuple[str, str, str, str]:
    """``(x_key, y_key, x_label, y_label)`` — the quest's own Pareto-scatter axes.

    Kinetics cutover: a quest that declares >= 2 ``rubric_objectives`` plots
    its own first two (a catalyst quest orders ``log_tof``/``atom_cost``
    first — the headline activity/cost trade-off) instead of the fixed hub-v2
    starter pick (:data:`PARETO_X_MEASURE`/:data:`PARETO_Y_MEASURE`), which
    remains the fallback for every quest that declares fewer than two (the
    single-objective default, or a hand-built quest with no
    ``rubric_objectives`` at all) — unaffected by this change.

    ``quest_meta`` (the quest ref's raw ``meta``, not required to be
    parsed) is consulted alongside the already-resolved ``objectives``
    (:func:`_objectives_for`'s output) so a quest whose raw
    ``rubric_objectives`` names >= 2 axes but had one dropped by that
    function's defensive parse (a malformed key/sense) still falls back
    cleanly rather than indexing past a shorter resolved list.
    """
    raw = (quest_meta or {}).get("rubric_objectives")
    declared_two_plus = isinstance(raw, list) and len(raw) >= 2
    if declared_two_plus and len(objectives) >= 2:
        x_key, y_key = objectives[0][0], objectives[1][0]
    else:
        x_key, y_key = PARETO_X_MEASURE, PARETO_Y_MEASURE
    return x_key, y_key, axis_label_for(x_key), axis_label_for(y_key)


#: Minimum plottable candidates (both axis measures present) before the
#: scatter is worth drawing — below this a two-point line/point cloud with
#: no real shape isn't more legible than the text frontier already below it.
_SCATTER_MIN_POINTS = 2

#: Default SVG geometry (px) — a `viewBox` this size, `pad` reserved on every
#: edge for axis labels/breathing room around the outermost points.
_SVG_WIDTH = 480.0
_SVG_HEIGHT = 260.0
_SVG_PAD = 36.0

#: Fraction of the data span added as padding on each side of the axis range,
#: so the extreme points never sit flush against the plot border.
_RANGE_PAD_FRACTION = 0.1

#: Tick count the nice-step search aims for on each axis — the actual count
#: varies (round steps rarely divide the range evenly) but stays close.
_AXIS_TICK_TARGET = 5


def _nice_ticks(lo: float, hi: float, target: int = _AXIS_TICK_TARGET) -> list[float]:
    """Round tick values inside ``[lo, hi]`` on a 1/2/5×10ᵏ step.

    The classic nice-numbers walk: pick the smallest step from the
    {1, 2, 5}·10ᵏ ladder that yields at most ``target`` intervals over the
    span, then emit every multiple of it inside the range. A degenerate span
    (``hi <= lo``) returns the single value — the flat-line case
    :func:`build_frontier_scatter` already guards its scaling for.
    """
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / max(target - 1, 1)
    mag = 10.0 ** math.floor(math.log10(raw))
    step = 10.0 * mag
    for mult in (1.0, 2.0, 5.0):
        if mult * mag >= raw:
            step = mult * mag
            break
    first = math.ceil(lo / step) * step
    ticks: list[float] = []
    v = first
    while v <= hi + step * 1e-9:
        ticks.append(round(v, 10))
        v += step
    return ticks


@dataclass(frozen=True)
class FrontierScatter:
    """A plottable Pareto scatter — geometry pre-computed, template-ready.

    ``points`` are plain dicts (not a dataclass) since the caller may stamp
    an ``open_url`` onto each before handing them to Jinja; every point
    already carries pixel-space ``cx``/``cy`` so the template does no math.
    ``x_ticks``/``y_ticks`` are likewise pre-projected axis ticks
    (``{"value", "pos", "label"}`` with ``pos`` in pixel space — an x for
    x-ticks, a y for y-ticks), and ``pad`` is the plot-area inset the
    template draws the axis lines on.
    """

    points: list[dict[str, Any]]
    x_label: str
    y_label: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    width: float = _SVG_WIDTH
    height: float = _SVG_HEIGHT
    pad: float = _SVG_PAD
    x_ticks: list[dict[str, Any]] = field(default_factory=list)
    y_ticks: list[dict[str, Any]] = field(default_factory=list)


def _rate_readout(measures: dict[str, float]) -> str | None:
    """``atom_cost - log_tof`` (log10 $ per unit TOF) when both measures are
    present, else ``None`` (never a fabricated partial). Read as: 100x more
    active (higher ``log_tof``) buys 100x less catalyst mass for the same
    dollar spend — the single number that answers "is dear-but-active worth
    it". Shared by :func:`leaderboard` (the ``$/rate`` column) and
    :func:`build_frontier_scatter` (the hover tooltip)."""
    ac = measures.get("atom_cost")
    lt = measures.get("log_tof")
    if not isinstance(ac, (int, float)) or not isinstance(lt, (int, float)):
        return None
    return f"{ac - lt:g}"


def _extra_objective_measures(
    measures: dict[str, float],
    objectives: Sequence[tuple[str, str]],
    plotted: tuple[str, str],
) -> list[dict[str, str]]:
    """The candidate's value for every declared objective OTHER than the
    two currently plotted — ``{"key", "label", "value"}`` per entry, in
    declared order, skipping any objective this candidate has no value
    for (never a fabricated ``"—"`` — the tooltip just omits it)."""
    out: list[dict[str, str]] = []
    for key, _sense in objectives:
        if key in plotted:
            continue
        v = measures.get(key)
        if isinstance(v, (int, float)):
            out.append({"key": key, "label": axis_label_for(key), "value": f"{v:g}"})
    return out


def _union_viewport(
    lo: float,
    hi: float,
    viewport: dict[str, tuple[float, float]] | None,
    measure: str,
) -> tuple[float, float]:
    """``[lo, hi]`` unioned with ``viewport[measure]`` when present + well-
    formed, else ``(lo, hi)`` unchanged — see :func:`build_frontier_scatter`'s
    ``viewport`` param."""
    if not viewport:
        return lo, hi
    entry = viewport.get(measure)
    if not isinstance(entry, (tuple, list)) or len(entry) != 2:
        return lo, hi
    try:
        vlo, vhi = float(entry[0]), float(entry[1])
    except (TypeError, ValueError):
        return lo, hi
    if not (math.isfinite(vlo) and math.isfinite(vhi)) or vlo > vhi:
        return lo, hi
    return min(lo, vlo), max(hi, vhi)


def build_frontier_scatter(
    candidates: Sequence[Candidate],
    *,
    provisional: Sequence[ProvisionalCandidate] = (),
    x_measure: str = PARETO_X_MEASURE,
    y_measure: str = PARETO_Y_MEASURE,
    x_label: str = PARETO_X_LABEL,
    y_label: str = PARETO_Y_LABEL,
    open_url_for: Callable[[Candidate], str] | None = None,
    frontier_ref_ids: frozenset[int] | set[int] | None = None,
    width: float = _SVG_WIDTH,
    height: float = _SVG_HEIGHT,
    pad: float = _SVG_PAD,
    viewport: dict[str, tuple[float, float]] | None = None,
    objectives: Sequence[tuple[str, str]] = (),
) -> FrontierScatter | None:
    """Extract + scale an (x, y) scatter over ``candidates``, or ``None``.

    Pure geometry: no store, no Jinja. A candidate is plottable only when
    *both* axis measures are present (``_dominates``'s own "missing a measure
    ⇒ not comparable" rule, mirrored here as "not comparable ⇒ not
    plottable"); fewer than :data:`_SCATTER_MIN_POINTS` plottable candidates
    (confirmed + provisional combined) returns ``None`` so the caller falls
    back to the text-only frontier. An all-equal axis (every point shares one
    x or y) would otherwise divide by zero scaling to the viewBox — guarded
    by substituting a span of ``1.0`` so the points simply plot along a flat
    line instead.

    ``provisional`` (default empty — every pre-existing caller unaffected)
    plots :class:`ProvisionalCandidate`'s merged (trusted + recovered-
    untrusted) values alongside the confirmed points on the SAME shared axis
    range, each stamped ``band='provisional'`` (vs. ``'confirmed'`` for the
    rest) plus ``untrusted``/``on_frontier`` so the template can render them
    visually distinct (hollow/dashed, a frontier star) without a second
    geometry pass.

    ``frontier_ref_ids`` (optional) stamps ``on_frontier`` on the *confirmed*
    points too, so the template's marker grammar — shape = frontier
    membership (star/circle), fill = trust, colour = band — covers both
    bands with one vocabulary.

    ``viewport`` (optional — ``{measure: (lo, hi)}``, read from
    ``quest.meta.frontier_viewport`` by the caller) pins a wider axis range
    than the current data alone would produce: when an entry names the
    chosen ``x_measure``/``y_measure``, the plotted range becomes the union
    of the data-derived range and the stored one (:func:`_union_viewport`)
    — so the axis doesn't keep re-scaling tick-to-tick as new points land
    inside a range a human already widened. A missing/malformed entry (not
    a 2-tuple, non-numeric, ``lo > hi``, or no ``viewport`` at all) leaves
    the data-derived range untouched.

    ``objectives`` (optional — the quest's full declared objective vector,
    not just the two plotted axes) stamps each point's ``extra_measures``
    (the OTHER declared objectives it carries a value for, each
    ``{"key", "label", "value"}``) plus a ``rate_readout`` ($/rate,
    :func:`_rate_readout` — ``None`` when not computable), so the template's
    hover tooltip can show the full objective vector, not just the plotted
    pair, without a second lookup. Default empty — every pre-existing
    caller gets no extra fields, unchanged.
    """
    plottable = [
        c
        for c in candidates
        if c.measures.get(x_measure) is not None
        and c.measures.get(y_measure) is not None
    ]
    plottable_provisional = [
        pc
        for pc in provisional
        if pc.measures.get(x_measure) is not None
        and pc.measures.get(y_measure) is not None
    ]
    if len(plottable) + len(plottable_provisional) < _SCATTER_MIN_POINTS:
        return None

    xs = [c.measures[x_measure] for c in plottable] + [
        pc.measures[x_measure] for pc in plottable_provisional
    ]
    ys = [c.measures[y_measure] for c in plottable] + [
        pc.measures[y_measure] for pc in plottable_provisional
    ]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    x_span = (x_max - x_min) or 1.0
    y_span = (y_max - y_min) or 1.0
    x_lo = x_min - x_span * _RANGE_PAD_FRACTION
    x_hi = x_max + x_span * _RANGE_PAD_FRACTION
    y_lo = y_min - y_span * _RANGE_PAD_FRACTION
    y_hi = y_max + y_span * _RANGE_PAD_FRACTION
    x_lo, x_hi = _union_viewport(x_lo, x_hi, viewport, x_measure)
    y_lo, y_hi = _union_viewport(y_lo, y_hi, viewport, y_measure)
    x_range = (x_hi - x_lo) or 1.0
    y_range = (y_hi - y_lo) or 1.0

    plot_w = width - 2 * pad
    plot_h = height - 2 * pad

    def _cx(v: float) -> float:
        return pad + (v - x_lo) / x_range * plot_w

    def _cy(v: float) -> float:
        # SVG y grows downward; flip so the higher value plots higher up.
        return pad + (1.0 - (v - y_lo) / y_range) * plot_h

    plotted = (x_measure, y_measure)
    points: list[dict[str, Any]] = []
    for c in plottable:
        x = c.measures[x_measure]
        y = c.measures[y_measure]
        point: dict[str, Any] = {
            "ref_id": c.ref_id,
            "handle": c.handle,
            "name": c.name,
            "x": x,
            "y": y,
            "converged": c.converged,
            "band": "confirmed",
            "on_frontier": bool(frontier_ref_ids and c.ref_id in frontier_ref_ids),
            "cx": round(_cx(x), 2),
            "cy": round(_cy(y), 2),
            "extra_measures": _extra_objective_measures(
                c.measures, objectives, plotted
            ),
            "rate_readout": _rate_readout(c.measures),
        }
        if open_url_for is not None:
            point["open_url"] = open_url_for(c)
        points.append(point)

    for pc in plottable_provisional:
        c = pc.candidate
        x = pc.measures[x_measure]
        y = pc.measures[y_measure]
        ppoint: dict[str, Any] = {
            "ref_id": c.ref_id,
            "handle": c.handle,
            "name": c.name,
            "x": x,
            "y": y,
            "converged": c.converged,
            "band": "provisional",
            "untrusted": x_measure in pc.untrusted_keys
            or y_measure in pc.untrusted_keys,
            "on_frontier": pc.on_frontier,
            "reasons": list(pc.reasons),
            "cx": round(_cx(x), 2),
            "cy": round(_cy(y), 2),
            "extra_measures": _extra_objective_measures(
                pc.measures, objectives, plotted
            ),
            "rate_readout": _rate_readout(pc.measures),
        }
        if open_url_for is not None:
            ppoint["open_url"] = open_url_for(c)
        points.append(ppoint)

    x_ticks = [
        {"value": v, "pos": round(_cx(v), 2), "label": f"{v:g}"}
        for v in _nice_ticks(x_lo, x_hi)
    ]
    y_ticks = [
        {"value": v, "pos": round(_cy(v), 2), "label": f"{v:g}"}
        for v in _nice_ticks(y_lo, y_hi)
    ]

    return FrontierScatter(
        points=points,
        x_label=x_label,
        y_label=y_label,
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
        width=width,
        height=height,
        pad=pad,
        x_ticks=x_ticks,
        y_ticks=y_ticks,
    )


def _dominates_measures(
    a: dict[str, float], b: dict[str, float], objectives: list[tuple[str, str]]
) -> bool:
    """True when measures dict ``a`` Pareto-dominates ``b`` over ``objectives``.

    ``a`` dominates ``b`` iff it is no worse on every objective and strictly
    better on at least one. Missing a measure on either side → not comparable
    (returns False), so a partially-measured point never dominates. Shared by
    :func:`_dominates` (real ``Candidate.measures``) and
    :func:`_provisional_split` (a provisional candidate's merged trusted +
    recovered-untrusted view) — same rule, two measure sources.
    """
    strictly_better = False
    for key, sense in objectives:
        av = a.get(key)
        bv = b.get(key)
        if av is None or bv is None:
            return False
        if sense == "min":
            if av > bv:
                return False
            if av < bv:
                strictly_better = True
        else:  # max
            if av < bv:
                return False
            if av > bv:
                strictly_better = True
    return strictly_better


def _dominates(a: Candidate, b: Candidate, objectives: list[tuple[str, str]]) -> bool:
    """True when ``a`` Pareto-dominates ``b`` over ``objectives`` (see
    :func:`_dominates_measures` for the rule)."""
    return _dominates_measures(a.measures, b.measures, objectives)


def pareto_split(
    candidates: list[Candidate], objectives: list[tuple[str, str]]
) -> FrontierResult:
    """Partition ``candidates`` into frontier / dominated / unevaluated."""
    keys = [k for k, _ in objectives]
    evaluated = [
        c
        for c in candidates
        if c.converged and all(c.measures.get(k) is not None for k in keys)
    ]
    unevaluated = [c for c in candidates if c not in evaluated]

    frontier: list[Candidate] = []
    dominated: list[Candidate] = []
    for c in evaluated:
        if any(_dominates(o, c, objectives) for o in evaluated if o.ref_id != c.ref_id):
            dominated.append(c)
        else:
            frontier.append(c)
    return FrontierResult(
        objectives=objectives,
        frontier=frontier,
        dominated=dominated,
        unevaluated=unevaluated,
    )


def _objectives_for(store: Store, quest_id: int) -> list[tuple[str, str]]:
    ref = store.get_ref(kind="quest", id=quest_id)
    raw = (ref.meta or {}).get("rubric_objectives") if ref else None
    out: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            sense = str(item.get("sense") or "min").strip().lower()
            if key and sense in _VALID_SENSES:
                out.append((key, sense))
    return out or list(DEFAULT_OBJECTIVES)


def _rubric_composite_for(store: Store, quest_id: int) -> dict[str, Any] | None:
    """The quest's declared composite objective, or ``None`` (feature off).

    ``meta.rubric_composite = {"key": "<name>", "weights": {"<measure>":
    <float>, ...}}`` — a **human-set rubric field** (decided: "the agent may not tune its
    own objective"). Written only by :func:`precis.quest.catalyst_seed.
    seed_catalyst_quest` at seed time; no quest-tick or LLM code path writes
    or modifies it (verified: the tick's only quest-meta writes are
    ``ticks_since_experiment`` and the weave-body marker — neither touches
    this key). Malformed/empty shapes (no ``key``, no usable numeric
    weights) are treated as absent rather than raising, matching
    :func:`_objectives_for`'s defensive parse.
    """
    ref = store.get_ref(kind="quest", id=quest_id)
    raw = (ref.meta or {}).get("rubric_composite") if ref else None
    if not isinstance(raw, dict):
        return None
    key = str(raw.get("key") or "").strip()
    raw_weights = raw.get("weights")
    if not key or not isinstance(raw_weights, dict):
        return None
    weights = {k: v for k, v in ((k, _numeric(v)) for k, v in raw_weights.items())}
    weights = {k: v for k, v in weights.items() if v is not None}
    if not weights:
        return None
    return {"key": key, "weights": weights}


def _apply_rubric_composite(
    candidates: Sequence[Candidate], composite: dict[str, Any] | None
) -> None:
    """Stamp the quest's declared composite measure onto each candidate that
    has EVERY weighted component — in place, mutating ``Candidate.measures``
    (a plain dict field on an otherwise frozen dataclass) so the composite is
    referenceable from ``rubric_objectives`` like any other measure.

    A candidate missing any one component gets no partial sum and no
    fabricated value — it simply has no ``key`` measure, so it ranks
    ``unevaluated`` on any objective that names it, exactly like a missing
    measure today (:func:`pareto_split`'s existing "missing a declared
    objective" path).
    """
    if not composite:
        return
    key = composite["key"]
    weights: dict[str, float] = composite["weights"]
    for c in candidates:
        total = 0.0
        for mkey, w in weights.items():
            v = c.measures.get(mkey)
            if v is None:
                break
            total += w * v
        else:
            c.measures[key] = total


#: struct_runs columns that are bookkeeping, not measures — never rank on these
#: (``converged`` is a bool and ``status``/``fidelity``/``model``/``created_at``
#: are non-numeric, so ``_numeric`` already filters them; these are the numeric
#: ones we must exclude by name).
_RUN_NON_MEASURE: frozenset[str] = frozenset({"id", "ref_id", "on_version"})

#: structure.meta bookkeeping keys that are never a ranking measure —
#: ``version``/``label_hi`` are ``structure_save``'s own housekeeping and
#: ``quest_harvested_upto``/``quest_autocatpath_harvested_upto`` are the
#: idempotency bookmarks ``harvest_measures`` (compute.py) stamps onto the
#: candidate. ``label_hi``/``lattice``/``pbc`` are non-numeric already
#: (``_numeric`` filters them), so only the numeric ones need listing here.
#: ``barrier_neb_failed``/``barrier_desorbed``/``barrier_wrong_site`` are the
#: pathway-quality warning counts harvest_measures also stamps (``barrier_trusted``/
#: ``barrier_low_confidence`` are bools, already excluded by ``_numeric``) —
#: diagnostics for :func:`leaderboard`'s quality flag, never a rank measure.
#: ``adsorption_barrier`` is the tether's reseat barrier: a trust/annotation
#: signal (activated-adsorption sniff), likewise not a Pareto objective.
#: ``barrier_screen`` is the tier-ladder's superseded (parked/neb-tier)
#: barrier once a higher-fidelity (verify/coadsorbed-tier) one lands
#: (:func:`precis.quest.compute._canonicalize_barrier`) — calibration data
#: (the screen→verify delta), never a ranking axis.
_META_NON_MEASURE: frozenset[str] = frozenset(
    {
        "version",
        "quest_harvested_upto",
        "quest_autocatpath_harvested_upto",
        "barrier_neb_failed",
        "barrier_desorbed",
        "barrier_wrong_site",
        "adsorption_barrier",
        "barrier_screen",
    }
)


def _numeric(v: Any) -> float | None:
    """A measure value, or None. ``bool`` is an ``int`` subclass but never a measure."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _candidate_from_structure(store: Store, s: Any) -> Candidate:
    """Build a :class:`Candidate` from a structure ref + its measures.

    Measures are gathered **generically** (not a fixed column list) so the
    frontier can rank on any named objective a quest declares (barrier,
    formation-energy, selectivity, …). Two sources:

    1. every numeric field of the best converged ``struct_runs`` row — today
       ``energy`` / ``max_force`` / ``max_disp`` / ``n_steps``, plus any future
       run scalar, with no code change here;
    2. every numeric top-level key of ``structure.meta`` — the escape hatch a
       synthesis/harvest pass stamps computed measures onto. This is how the
       reaction **barrier** reaches the frontier: a autocatpath run over the
       candidate is harvested onto the candidate's own ``meta`` (Slice 3), so
       the frontier reads a plain scalar — no autocatpath import, no graph
       recompute. Fill-only: a stamped measure never clobbers a real relax
       measure of the same name.

    ``meta.params`` (the candidate's point in the quest param space, §7.8) rides
    along for a later optimizer advisor; it is not a measure.
    """
    from precis.utils import handle_registry

    handle = handle_registry.try_format("structure", s.id) or f"structure:{s.id}"
    name = (s.title or "").splitlines()[0] if s.title else handle
    runs = store.structure_runs(s.id)
    # Best = the most recent converged run (structure_runs is newest-first).
    best = next((r for r in runs if r.get("converged")), None)
    converged = best is not None

    measures: dict[str, float] = {}
    if best is not None:
        for k, v in best.items():
            if k in _RUN_NON_MEASURE:
                continue
            fv = _numeric(v)
            if fv is not None:
                measures[k] = fv

    meta = getattr(s, "meta", None) or {}
    for k, v in meta.items():
        if k == "params" or k in _META_NON_MEASURE:
            continue
        # A directly-stamped untrusted-value stash (e.g. `log_tof_
        # untrusted_value`, kinetics contract) is flag-only display context
        # (see below), never a literal measure of its own — excluded here
        # so it doesn't ALSO land in `measures` under its raw stash name.
        if isinstance(k, str) and k.endswith(_UNTRUSTED_VALUE_SUFFIX):
            continue
        fv = _numeric(v)
        if fv is not None:
            measures.setdefault(k, fv)  # runs win on collision

    raw_params = meta.get("params")
    params = dict(raw_params) if isinstance(raw_params, dict) else {}

    flags: dict[str, Any] = {}
    if "barrier_trusted" in meta:
        flags["barrier_trusted"] = bool(meta.get("barrier_trusted"))
    if "barrier_neb_failed" in meta:
        flags["barrier_neb_failed"] = meta.get("barrier_neb_failed")
    if "barrier_desorbed" in meta:
        flags["barrier_desorbed"] = meta.get("barrier_desorbed")
    if "barrier_wrong_site" in meta:
        flags["barrier_wrong_site"] = meta.get("barrier_wrong_site")
    if "adsorption_barrier" in meta:
        flags["adsorption_barrier"] = meta.get("adsorption_barrier")
    if "barrier_screen" in meta:
        flags["barrier_screen"] = meta.get("barrier_screen")
    # Kinetics cutover: the trust gate + naming context the kinetics model
    # harvest stamps (same shape as `barrier_trusted` above) — a kinetics
    # run that fails or comes back untrustworthy sets `kinetics_trusted`
    # False + a `kinetics_note` reason, so `log_tof` never lands as a
    # ranking measure but still surfaces the candidate as `provisional`
    # (via the `*_untrusted_value` stash mechanism below) rather than
    # reading as never-tried. `drc_top` (the degree-of-rate-control-leading
    # elementary step) and `atom_cost_dearest` (the priciest element driving
    # `atom_cost`) are display-only naming context, never a measure.
    if "kinetics_trusted" in meta:
        flags["kinetics_trusted"] = bool(meta.get("kinetics_trusted"))
    for k in ("kinetics_note", "drc_top", "atom_cost_dearest"):
        if k in meta:
            flags[k] = meta.get(k)
    # Selectivity/poisoning naming context (catpath >= 0.5.2 harvests —
    # compute._AUTOCATPATH_SELECTIVITY_CONTEXT_KEYS): the most competitive
    # side product, the deepest kinetic-trap state, per-species poison
    # verdicts, and (>= 0.6.0) the scorecard's limiting axis + one-line
    # worst-problem statement. Strings/dicts, so the `_numeric` filter
    # already keeps them out of `measures`; surfaced as flags for the
    # leaderboard + tick prompt.
    for k in (
        "side_worst",
        "trap_worst",
        "poison_verdicts",
        "limiting_factor",
        "worst_problem",
    ):
        if k in meta:
            flags[k] = meta.get(k)
    # A kinetics-untrusted stash — a numeric value directly stamped onto
    # `meta` under the SAME `_UNTRUSTED_VALUE_SUFFIX` convention the
    # barrier-exclusion block below derives at read time (e.g.
    # `log_tof_untrusted_value`, kinetics contract). Copied into `flags`
    # (never `measures`) so :func:`_merge_provisional_measures` recovers it
    # for the provisional band exactly like a popped barrier value — and
    # excluded from the generic meta harvest below (`_META_NON_MEASURE`
    # can't list it by name since the base measure varies) so it never
    # ALSO lands as a bogus literal-named measure.
    for k, v in meta.items():
        if (
            isinstance(k, str)
            and k.endswith(_UNTRUSTED_VALUE_SUFFIX)
            and k not in flags
        ):
            fv = _numeric(v)
            if fv is not None:
                flags[k] = fv
    # The tier-ladder rung the candidate's CURRENT canonical `barrier` came
    # from (:func:`precis.quest.compute._canonicalize_barrier`) — read by
    # :mod:`precis.quest.graduate`'s verify-only gate; absent on a candidate
    # with no autocatpath barrier harvested yet.
    if "barrier_tier" in meta:
        flags["barrier_tier"] = meta.get("barrier_tier")
    # The candidate's OWN tier-ladder rung (highest tier with a completed
    # run, :func:`precis.quest.compute._bump_tier_stamp` — distinct from
    # ``barrier_tier`` above, which tracks the ranked barrier specifically).
    # Display-only: the leaderboard's glyph column reads this
    # (:func:`leaderboard`); never a ranking measure (already excluded from
    # ``measures`` — ``_META_NON_MEASURE``/``_numeric`` filter the string).
    if "tier" in meta:
        flags["tier"] = meta.get("tier")

    # An untrusted barrier (its pathway had non-converged NEB edges / desorbed
    # or mis-bound adsorbates) is noise, not a measurement — exclude it (and
    # span, plus the CHE electro scalars U_L/U_L_abs/U_opt/span_at_Uopt/
    # P_side — all measured over the SAME pathway, see
    # compute._AUTOCATPATH_ELECTRO_KEYS) from ranking entirely so none of
    # them can dominate or be dominated; each falls to `unevaluated` via the
    # existing "missing a declared objective" path. The raw values survive in
    # `flags` so the leaderboard can still show what was measured, just
    # marked excluded.
    if flags.get("barrier_trusted") is False:
        excluded_barrier = measures.pop("barrier", None)
        measures.pop("span", None)
        if excluded_barrier is not None:
            flags["barrier_untrusted_value"] = excluded_barrier
        for k in (
            "U_L",
            "U_L_abs",
            "U_opt",
            "span_at_Uopt",
            "P_side",
            # selectivity/poisoning scalars — measured over the same
            # untrusted pathway, so they are excluded with it
            "selectivity_margin",
            "trap_margin",
            "poison_margin",
            # legacy 0.5.2-era keys — may still sit on candidates measured
            # pre-swap, before the engine-scorecard rename
            "side_span_margin",
            "trap_depth",
        ):
            excluded = measures.pop(k, None)
            if excluded is not None:
                flags[f"{k}_untrusted_value"] = excluded

    return Candidate(
        ref_id=s.id,
        handle=handle,
        name=name[:70],
        measures=measures,
        converged=converged,
        params=params,
        flags=flags,
    )


#: Suffix :func:`_candidate_from_structure` stamps onto ``flags`` for every
#: measure it popped out of ``measures`` on an untrusted pathway (e.g.
#: ``flags['barrier_untrusted_value']``) — the raw value survives there
#: purely for display. :func:`_merge_provisional_measures` reads the same
#: suffix back off to build the provisional bucket's merged view.
_UNTRUSTED_VALUE_SUFFIX = "_untrusted_value"


def _merge_provisional_measures(
    c: Candidate,
) -> tuple[dict[str, float], frozenset[str]]:
    """A candidate's trusted measures, backfilled with any untrusted values
    :func:`_candidate_from_structure` stashed onto ``flags``.

    Returns ``(merged, untrusted_keys)`` — ``merged`` never overrides a
    trusted value (a key present in ``c.measures`` always wins), and
    ``untrusted_keys`` names exactly the keys that came from the backfill,
    so a caller (:func:`_provisional_split`, :func:`build_frontier_scatter`)
    can mark them "⚠untrusted" instead of presenting them as confirmed.
    """
    merged = dict(c.measures)
    untrusted_keys: set[str] = set()
    for flag_key, value in c.flags.items():
        if not isinstance(flag_key, str) or not flag_key.endswith(
            _UNTRUSTED_VALUE_SUFFIX
        ):
            continue
        base = flag_key[: -len(_UNTRUSTED_VALUE_SUFFIX)]
        if base in merged:
            continue  # a trusted value always wins
        fv = _numeric(value)
        if fv is None:
            continue
        merged[base] = fv
        untrusted_keys.add(base)
    return merged, frozenset(untrusted_keys)


def _provisional_reasons(
    c: Candidate, objective_keys: Sequence[str], merged: dict[str, float]
) -> list[str]:
    """Human-readable reasons a candidate isn't confirmed, derived from the
    SAME flags/warnings :func:`_candidate_from_structure` already stamped —
    no new trust logic, just prose over the existing verdict."""
    reasons: list[str] = []
    if c.flags.get("barrier_trusted") is False:
        n_neb = c.flags.get("barrier_neb_failed") or 0
        n_desorbed = c.flags.get("barrier_desorbed") or 0
        n_wrong_site = c.flags.get("barrier_wrong_site") or 0
        if n_neb:
            reasons.append("barrier untrusted: NEB not converged")
        if n_desorbed:
            reasons.append("barrier untrusted: adsorbate detached")
        if n_wrong_site:
            reasons.append("barrier untrusted: wrong binding site")
        if not reasons:
            reasons.append("barrier untrusted")
    if not c.converged:
        reasons.append("no converged relax")
    missing = [k for k in objective_keys if k not in merged]
    if missing:
        reasons.append(f"missing {', '.join(missing)}")
    return reasons


def _provisional_split(
    confirmed: Sequence[Candidate],
    unevaluated: Sequence[Candidate],
    objectives: list[tuple[str, str]],
) -> tuple[list[ProvisionalCandidate], list[Candidate]]:
    """Split ``unevaluated`` into the provisional bucket + the truly-
    unevaluated remainder, then mark which provisional candidates sit on the
    provisional Pareto frontier.

    A candidate qualifies as provisional when its merged view (trusted
    measures + untrusted values recovered from ``flags`` —
    :func:`_merge_provisional_measures`) carries a value for at least one
    declared objective; everything else (never measured at all, on this
    objective vector) stays truly unevaluated. The provisional frontier is
    then computed by re-running the SAME domination rule
    (:func:`_dominates_measures`) over ``confirmed`` (the real
    frontier+dominated candidates, unchanged) union the provisional
    candidates' merged views — so a confirmed, trustworthy measurement can
    still knock out a provisional one, and provisional candidates compete
    among themselves too. ``confirmed`` itself is never mutated or reordered;
    this is a second, non-authoritative split layered on top.
    """
    obj_keys = [k for k, _ in objectives]
    provisional: list[ProvisionalCandidate] = []
    still_unevaluated: list[Candidate] = []
    for c in unevaluated:
        merged, untrusted_keys = _merge_provisional_measures(c)
        if not any(k in merged for k in obj_keys):
            still_unevaluated.append(c)
            continue
        provisional.append(
            ProvisionalCandidate(
                candidate=c,
                measures=merged,
                untrusted_keys=untrusted_keys,
                reasons=_provisional_reasons(c, obj_keys, merged),
            )
        )

    pool: list[tuple[int, dict[str, float]]] = [
        (c.ref_id, c.measures) for c in confirmed
    ]
    pool += [(pc.candidate.ref_id, pc.measures) for pc in provisional]

    def _is_dominated(ref_id: int, measures: dict[str, float]) -> bool:
        return any(
            other_id != ref_id
            and _dominates_measures(other_measures, measures, objectives)
            for other_id, other_measures in pool
        )

    final: list[ProvisionalCandidate] = [
        replace(pc, on_frontier=not _is_dominated(pc.candidate.ref_id, pc.measures))
        for pc in provisional
    ]
    return final, still_unevaluated


#: Tier-ladder glyph column (tier-ladder UX item 4) — one character per rung,
#: read off ``Candidate.flags['tier']`` (the candidate's OWN highest-attained
#: tier, not ``barrier_tier``). A candidate with no tier stamp at all (a
#: pre-ladder quest, or one that opted out — ``tier_ladder=False``) gets no
#: glyph rather than a fabricated one. The word each glyph stands for isn't
#: repeatable in a plain TOON cell (no title-attribute equivalent), so
#: ``QuestHandler._render_leaderboard`` prints the legend once in the
#: header instead of on every row.
TIER_GLYPH: dict[str, str] = {"screening": "○", "neb": "◐", "verify": "●"}


def leaderboard(
    fr: FrontierResult, *, graduated: frozenset[int] | set[int] = frozenset()
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows + TOON schema for the **by-total** design leaderboard (§7.3).

    One row per candidate design: identity, the objective vector, its Pareto
    ``band`` (``frontier`` / ``dominated`` / ``provisional`` / ``awaiting``),
    and a graduation flag. Ordered frontier → dominated → provisional →
    awaiting, and within each band sorted by the primary objective (best
    first). A provisional row shows its merged measures with ``≈`` on each
    unconfirmed value and its exclusion reasons in ``quality`` — measured but
    not confirmed, never ranked against the confirmed bands. A ``$/rate``
    column (:func:`_rate_readout` — ``atom_cost - log_tof``, log10 $ per
    unit TOF) is computed HERE at read time whenever a candidate carries
    both components — never stored — so "100x more active buys 100x less
    catalyst" reads as one number without a spreadsheet; ``—`` when either
    component is absent. Pure over a :class:`FrontierResult` so it is
    trivially testable; the handler renders it via ``toon.dump``. This is
    the striving's authoritative leaderboard — autocatpath's own
    ``compare`` view is a compute-side diagnostic over sibling pathways,
    not this.
    """
    obj_keys = [k for k, _ in fr.objectives]
    primary = fr.objectives[0] if fr.objectives else None

    def _sort_key(c: Candidate) -> float:
        if primary is None:
            return 0.0
        key, sense = primary
        v = c.measures.get(key)
        if v is None:
            return float("inf")  # unmeasured sinks to the bottom of its band
        return v if sense == "min" else -v

    def _rows(cands: list[Candidate], band: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in sorted(cands, key=_sort_key):
            row: dict[str, Any] = {
                "design": c.handle,
                "name": c.name,
                "tier": TIER_GLYPH.get(str(c.flags.get("tier")), ""),
                "band": band,
            }
            for key in obj_keys:
                v = c.measures.get(key)
                row[key] = f"{v:g}" if isinstance(v, (int, float)) else "—"
            row["$/rate"] = _rate_readout(c.measures) or "—"
            row["graduated"] = "★" if c.ref_id in graduated else ""
            quality = (
                "⚠ non-converged" if c.flags.get("barrier_trusted") is False else ""
            )
            # A geometry that duplicates an earlier candidate (:func:`_flag_geom_duplicates`)
            # — flagged only, not excluded, so it still ranks; the leaderboard just
            # marks it "dup" alongside any other quality note.
            if c.flags.get("duplicate_of"):
                quality = f"{quality} dup".strip()
            # An energy-degeneracy tripwire hit (:func:`_flag_energy_twins`) —
            # also flagged-only, mirroring `dup` above.
            if c.flags.get("energy_twin_of"):
                quality = f"{quality} etwin".strip()
            row["quality"] = quality
            out.append(row)
        return out

    def _prov_sort_key(pc: ProvisionalCandidate) -> float:
        if primary is None:
            return 0.0
        key, sense = primary
        v = pc.measures.get(key)
        if v is None:
            return float("inf")
        return v if sense == "min" else -v

    def _prov_rows(provs: Sequence[ProvisionalCandidate]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for pc in sorted(provs, key=_prov_sort_key):
            c = pc.candidate
            row: dict[str, Any] = {
                "design": c.handle,
                "name": c.name,
                "tier": TIER_GLYPH.get(str(c.flags.get("tier")), ""),
                "band": "provisional",
            }
            for key in obj_keys:
                v = pc.measures.get(key)
                if not isinstance(v, (int, float)):
                    row[key] = "—"
                elif key in pc.untrusted_keys:
                    row[key] = f"≈{v:g}"
                else:
                    row[key] = f"{v:g}"
            rate = _rate_readout(pc.measures)
            if rate is None:
                row["$/rate"] = "—"
            else:
                approx = bool(pc.untrusted_keys & {"atom_cost", "log_tof"})
                row["$/rate"] = f"≈{rate}" if approx else rate
            row["graduated"] = ""  # a provisional row can never graduate
            quality = "; ".join(pc.reasons) or "unconfirmed"
            if pc.on_frontier:
                quality = f"★ would lead — {quality}"
            row["quality"] = f"⚠ {quality}"
            out.append(row)
        return out

    rows = (
        _rows(fr.frontier, "frontier")
        + _rows(fr.dominated, "dominated")
        + _prov_rows(fr.provisional)
        + _rows(fr.unevaluated, "awaiting")
    )
    schema = [
        "design",
        "name",
        "tier",
        *obj_keys,
        "$/rate",
        "band",
        "graduated",
        "quality",
    ]
    return rows, schema


def _lazy_geom_hash_c(store: Store, s: Any) -> str | None:
    """Backfill ``meta.geom_hash_c`` for a candidate structure minted before
    the periodic-symmetry canonical hash existed: load its scene, compute
    the hash (:func:`precis.structure.canonical.geom_hash_c`), and stamp it
    back (:meth:`Store.stamp_ref_meta`) so the backfill happens once per
    structure — every later read (this one's own in-memory ``s.meta``
    included) sees the stamped value with no further scene load. Never
    raises: a load/hash/stamp failure logs at debug and falls back to
    ``None`` (the caller drops to the legacy absolute-position
    ``geom_hash``).
    """
    from precis.structure.canonical import geom_hash_c

    try:
        scene, _handles = store.structure_load(s.id)
        chc = geom_hash_c(scene)
    except Exception:
        log.debug(
            "_flag_geom_duplicates: geom_hash_c backfill failed for %s",
            s.id,
            exc_info=True,
        )
        return None
    try:
        store.stamp_ref_meta(s.id, {"geom_hash_c": chc})
    except Exception:
        log.debug(
            "_flag_geom_duplicates: geom_hash_c stamp failed for %s",
            s.id,
            exc_info=True,
        )
    meta = getattr(s, "meta", None)
    if isinstance(meta, dict):
        meta["geom_hash_c"] = chc  # so _flag_energy_twins sees it too, same read
    return chc


def _flag_geom_duplicates(
    store: Store, candidates: Sequence[Candidate], structures: Sequence[Any]
) -> None:
    """Flag a later-created candidate that shares its geometry hash with an
    earlier one — a proposer re-discovering the same material under a new
    name. Prefers the periodic-symmetry-invariant ``meta.geom_hash_c``
    (:func:`precis.structure.canonical.geom_hash_c`, stamped at
    candidate-creation time — :func:`precis.quest.compute._ensure_candidate_detail`)
    over the legacy absolute-position ``meta.geom_hash``
    (:func:`precis.quest.compute._geom_hash`) — a translation/rotation/mirror
    twin of an earlier candidate now groups with it even though its raw
    coordinates differ. A structure minted before the canonical hash existed
    gets it lazily backfilled here (:func:`_lazy_geom_hash_c`), falling back
    to the legacy hash only when the backfill itself fails.
    **Non-exclusionary**: ``flags['duplicate_of']`` is display-only (the
    earlier candidate's handle); the flagged candidate still ranks normally.
    Mutates ``candidates`` in place (``Candidate.flags`` is a plain dict, so
    this is safe on an otherwise-frozen dataclass).
    """
    by_id = {c.ref_id: c for c in candidates}
    seen: dict[str, str] = {}  # hash -> first-seen handle
    for s in sorted(structures, key=lambda s: s.id):
        meta = getattr(s, "meta", None) or {}
        gh = meta.get("geom_hash_c")
        if not isinstance(gh, str) or not gh:
            gh = _lazy_geom_hash_c(store, s)
        if not isinstance(gh, str) or not gh:
            gh = meta.get("geom_hash")
        if not isinstance(gh, str) or not gh:
            continue
        c = by_id.get(s.id)
        if c is None:
            continue
        first = seen.get(gh)
        if first is None:
            seen[gh] = c.handle
        else:
            c.flags["duplicate_of"] = first


def _flag_energy_twins(
    candidates: Sequence[Candidate], structures: Sequence[Any]
) -> None:
    """Flag a later candidate whose relaxed energy lands within
    :data:`_ENERGY_TWIN_EPS` of an earlier one of the SAME composition but a
    DIFFERENT ``geom_hash_c`` — two nominally-distinct starting geometries
    that converged to the same (or a symmetry-equivalent) minimum, a
    degeneracy the geometry hash alone can't see (it only catches duplicates
    at the *input* geometry, not post-relax convergence).

    Composition is never re-materialised here — no per-candidate scene load
    in a loop. ``atom_cost`` (stamped on ``meta`` at candidate-creation time
    from the composition, :func:`precis.quest.compute._stamp_atom_cost`) is
    a pure function of element counts alone and already rides on every
    :class:`Candidate` via ``measures`` — an exact ``atom_cost`` match is a
    cheap, reliable composition proxy over data already loaded for the
    frontier. (The composition dict itself isn't stamped anywhere
    :func:`_candidate_from_structure` reads, so grouping on it directly would
    need a fresh scene load per candidate — the thing this is built to
    avoid; a candidate missing ``atom_cost`` is simply not grouped.)

    Display-only: sets ``flags['energy_twin_of']`` on the later candidate
    (the earlier one's handle); ranking/dominance/geom-dup flagging are
    untouched. Call this AFTER :func:`_flag_geom_duplicates` (same call
    sites) so any lazily-backfilled ``geom_hash_c`` is already in ``meta``.
    """
    hash_by_id = {
        s.id: (getattr(s, "meta", None) or {}).get("geom_hash_c") for s in structures
    }
    # `Candidate.measures` is already `dict[str, float]` (`_numeric` filtered
    # at `_candidate_from_structure` time, bools included) — a plain
    # ``is not None`` is enough here.
    by_cost: dict[float, list[Candidate]] = {}
    for c in candidates:
        cost = c.measures.get("atom_cost")
        if cost is None:
            continue
        by_cost.setdefault(round(cost, 6), []).append(c)

    for group in by_cost.values():
        seen: list[Candidate] = []
        for c in sorted(group, key=lambda c: c.ref_id):
            energy = c.measures.get("energy")
            if energy is not None:
                gh = hash_by_id.get(c.ref_id)
                for prior in seen:
                    prior_energy = prior.measures.get("energy")
                    prior_gh = hash_by_id.get(prior.ref_id)
                    # Both hashes must be KNOWN and different — a missing
                    # stamp (backfill failure) is "unknown geometry", not
                    # "different geometry", and must not fire the flag.
                    if (
                        prior_energy is not None
                        and abs(energy - prior_energy) <= _ENERGY_TWIN_EPS
                        and gh is not None
                        and prior_gh is not None
                        and gh != prior_gh
                    ):
                        c.flags["energy_twin_of"] = prior.handle
                        break
            seen.append(c)


def _candidate_lineage_markers(store: Store, c: Candidate) -> list[str]:
    """Trust/ruled-out/dup markers for one candidate's frontier-tree line."""
    markers: list[str] = []
    ruled = next(
        (str(t) for t in store.tags_for(c.ref_id) if str(t).startswith("ruled-out:")),
        None,
    )
    if ruled is not None:
        markers.append(ruled)
    if c.flags.get("barrier_trusted") is False:
        markers.append("untrusted")
    dup_of = c.flags.get("duplicate_of")
    if dup_of:
        markers.append(f"dup-of {dup_of}")
    twin_of = c.flags.get("energy_twin_of")
    if twin_of:
        markers.append(f"energy-twin {twin_of}")
    if not c.converged and not ruled:
        markers.append("unconverged")
    return markers


def _candidate_key_measure(c: Candidate) -> str:
    """The candidate's headline measure for the frontier-tree line — the
    quest hub's own axis pick (:data:`PARETO_X_MEASURE`, the barrier) when
    present, else the default objective (:data:`PARETO_Y_MEASURE`, energy).

    Tier-ladder UX item 4: when the candidate's CANONICAL barrier came from a
    verify-tier pathway that superseded an earlier parked(neb)-tier one
    (``flags['barrier_tier'] == 'verify'`` + a kept ``barrier_screen`` —
    :func:`precis.quest.compute._canonicalize_barrier`), show the
    screen→verify delta itself (``"screen 0.84 → verified 0.96"``) in place
    of the single barrier figure — the headline calibration signal, not just
    the latest number.
    """
    screen = c.flags.get("barrier_screen")
    verified = c.measures.get(PARETO_X_MEASURE)
    if (
        c.flags.get("barrier_tier") == "verify"
        and screen is not None
        and verified is not None
    ):
        return f"screen {screen:g} → verified {verified:g}"
    v = c.measures.get(PARETO_X_MEASURE)
    label = PARETO_X_LABEL
    if v is None:
        v = c.measures.get(PARETO_Y_MEASURE)
        label = PARETO_Y_LABEL
    if v is None:
        return "no measure yet"
    return f"{label}={v:g}"


def render_frontier_tree(store: Store, quest_id: int) -> str:
    """Render the quest's candidate lineage as an indented markdown tree —
    the CODE-regenerated ``meta.pinned='frontier-tree'`` dossier chunk
    (:func:`precis.quest.dossier.update_frontier_tree`).

    Roots are candidates with no resolved ``derived-from`` parent among this
    quest's own candidate set (a link to something outside it — e.g. an
    ancestor design from a prior quest — does not count as in-tree lineage);
    each child nests one level under its parent. One line per candidate:
    name/handle, its key measure (:func:`_candidate_key_measure`), and any
    trust/ruled-out/dup markers (:func:`_candidate_lineage_markers`). Pure
    read over data :func:`quest_frontier` already loads — no new query shape,
    just a different assembly (a tree instead of a Pareto split).
    """
    from precis.quest.gaps import _live_servers

    structures = [s for s in _live_servers(store, quest_id) if s.kind == "structure"]
    if not structures:
        return "_(No candidates yet.)_\n"

    candidates = {s.id: _candidate_from_structure(store, s) for s in structures}
    _flag_geom_duplicates(store, list(candidates.values()), structures)
    _flag_energy_twins(list(candidates.values()), structures)
    _apply_rubric_composite(
        list(candidates.values()), _rubric_composite_for(store, quest_id)
    )

    # child ref_id -> parent ref_id, restricted to this quest's own candidates
    # (`derived-from` is child -> parent, the same relation
    # StructureHandler.derive uses).
    parent_of: dict[int, int] = {}
    for sid in candidates:
        for link in store.links_for(sid, direction="out", relation="derived-from"):
            if link.dst_ref_id in candidates:
                parent_of[sid] = int(link.dst_ref_id)
                break
    children: dict[int, list[int]] = {}
    for child, parent in parent_of.items():
        children.setdefault(parent, []).append(child)
    roots = sorted(sid for sid in candidates if sid not in parent_of)

    lines: list[str] = []

    def _render(sid: int, depth: int) -> None:
        c = candidates[sid]
        markers = _candidate_lineage_markers(store, c)
        marker_s = f" [{', '.join(markers)}]" if markers else ""
        lines.append(
            f"{'  ' * depth}- {c.name} [{c.handle}]: "
            f"{_candidate_key_measure(c)}{marker_s}"
        )
        for child_id in sorted(children.get(sid, [])):
            _render(child_id, depth + 1)

    for rid in roots:
        _render(rid, 0)
    return "\n".join(lines) + "\n"


def quest_frontier(
    store: Store,
    quest_id: int,
    *,
    objectives: list[tuple[str, str]] | None = None,
) -> FrontierResult:
    """The Pareto frontier over the quest's candidate `structure` servers.

    ``pareto_split`` itself is untouched (still the strict confirmed split —
    :mod:`precis.quest.graduate`'s belt-and-suspenders gate and the generic
    (non-quest) reuse in :mod:`precis.utils.llm.policy` both read exactly
    that). This function additionally lifts any measured-but-unconfirmed
    candidate out of ``pareto_split``'s ``unevaluated`` into
    ``FrontierResult.provisional`` (:func:`_provisional_split`) — quest-
    specific (it reads the ``barrier_trusted``/``*_untrusted_value`` flags
    :func:`_candidate_from_structure` stamps), so it lives here rather than
    in the generic splitter.
    """
    from precis.quest.gaps import _live_servers

    objs = objectives or _objectives_for(store, quest_id)
    structures = [s for s in _live_servers(store, quest_id) if s.kind == "structure"]
    candidates = [_candidate_from_structure(store, s) for s in structures]
    _flag_geom_duplicates(store, candidates, structures)
    _flag_energy_twins(candidates, structures)
    _apply_rubric_composite(candidates, _rubric_composite_for(store, quest_id))
    result = pareto_split(candidates, objs)
    provisional, still_unevaluated = _provisional_split(
        [*result.frontier, *result.dominated], result.unevaluated, objs
    )
    return replace(result, provisional=provisional, unevaluated=still_unevaluated)


__all__ = [
    "DEFAULT_OBJECTIVES",
    "PARETO_X_LABEL",
    "PARETO_X_MEASURE",
    "PARETO_Y_LABEL",
    "PARETO_Y_MEASURE",
    "TIER_GLYPH",
    "Candidate",
    "FrontierResult",
    "FrontierScatter",
    "ProvisionalCandidate",
    "axis_label_for",
    "build_frontier_scatter",
    "leaderboard",
    "pareto_split",
    "plot_axes_for",
    "quest_frontier",
    "render_frontier_tree",
]

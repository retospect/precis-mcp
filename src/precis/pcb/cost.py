"""ONE cost function, refined by level-dependent estimators, hardened by a
schedule-dependent penalty shape. See docs/backlog/pcb-guided-place-route.md
§"The cost function" — this module is written out in full because
everything downstream (the joint optimizer, the digest, the LLM's
between-round judgment) depends on it being right.

**Three things this file keeps deliberately separate** (conflating them is
the single easiest way to make an optimizer thrash):

- **the cost function** — :func:`evaluate_cost`, constant across levels
  AND across the schedule; it never gets a different formula for "early"
  vs "late" or "coarse" vs "fine".
- **estimator fidelity** — each :class:`TermSpec.estimate` looks at more
  of the IR as ``level`` increases (an L1 call may not touch L3
  positions even though they exist on the object; an L4 call may). This
  is the *only* thing ``level`` controls.
- **constraint hardness** — :data:`CostConfig.schedule`, which sharpens
  the margin penalty shape (:func:`hardened_penalty`) from exploratory
  (quadratic) toward a barrier. This is the *only* thing ``schedule``
  controls.

**Admissibility is two-sided (revised 2026-08-28).** Every estimator must
be optimistic in its OWN declared direction — never the reverse — but
"optimistic" means different things for different terms, so each
:class:`TermSpec` now carries an explicit :class:`BoundDirection`:

- **LOWER** (the original rule, and every term registered before
  ``crossings``): the estimate may understate cost or overstate
  feasibility, never the reverse (A* admissibility). Payoff: a state that
  looks bad at a coarse level really is bad and can be pruned without
  discarding a good solution — coarse ``raw`` <= fine ``raw``.
- **UPPER** (``crossings`` only, see that term's docstring): the estimate
  may overstate cost, never understate it — coarse ``raw`` >= fine
  ``raw``. Payoff mirrors LOWER exactly: a state that looks GOOD (a small
  or zero upper bound) really is good, so driving it to zero is a real,
  trustworthy guarantee, which is exactly what a LOWER bound cannot give
  you (a LOWER bound of zero says nothing about the truth).

Both directions are sound; what matters is that each term's direction is
DECLARED and TESTED, not that every term points the same way. Tested as a
property in ``tests/test_pcb_cost.py`` — generate random IR states,
evaluate every registered term at a coarse and a fine level, and assert
EACH term's own declared direction holds, per-term, not one global
``coarse.total <= fine.total`` inequality (which stopped being true the
moment ``crossings`` needed the opposite direction).

**Discrete vs. continuous, and why every term must be move-reachable.**
A cost term over a CONTINUOUS variable that rewards an exact coincidence
(round to a 25 mm grid, an alignment match) is measure-zero: a continuous
move reaches the rewarded state with probability zero, so the term never
fires — indistinguishable from a working term by any other test. The
original ``crossings`` estimator was the degenerate case of this same
defect: the Euler-bound backing it shipped with was provably always zero
on any real, star-decomposed board (a forest satisfies the bound
unconditionally — see ``ir.same_layer_crossing_bound``'s docstring), so
random states/moves alone could never have produced two distinct values
either. A term over a DISCRETE variable (which side a label sits on, a
small candidate set) has no such trap — a plain cost term is fine. Tested
as a registry-driven property in ``tests/test_pcb_cost.py``: for every
registered term, generate randomized IR states, apply every available
``optimize.MoveKind``, and assert the term's own aggregate takes at least
two distinct values across that exploration — a term that can't vary is
either dead or measure-zero, and the registry (not a per-term test
someone has to remember to write) is what demands the check.

**Undefined != zero.** A term with nothing to measure yet at this level
(gap capacity before L4) must still return a nonzero, *admissible* bound —
never literally 0, which would tell the optimizer congestion is free and
produce states that look excellent at L1 and are unroutable at L4. Every
estimator below that has a coarse/fine split says explicitly, in its
docstring, what the coarse bound assumes and why it's still ≤ the truth.

**Two families, aggregated differently.** Money terms (board area, layer
count, via count, part fees) normalize to USD and **sum** — money is
fungible and additive. Margin terms (clearance, loop inductance,
coupling, thermal rise, same-layer crossings) normalize to *fraction of
that term's own budget* and aggregate by **max** (or a soft p-norm) — a
sum would let 500 nets at
5% of budget drown the one net actually at 99%, which is exactly the net
that matters. See :func:`aggregate_margin`.

**Convexity IS the hardening schedule — one mechanism, not two.**
:func:`hardened_penalty` is superlinear in budget fraction always
(quadratic at ``schedule=0``, sharpening toward a steep barrier at
``schedule=1``), so a state at 95% of every budget costs far more than
one at 50%/40% even though both "sum to the same" under a naive linear
model — the first has no room for manufacturing variance.

**One dial.** :data:`CostConfig.risk_to_money` is the sole
risk<->money exchange rate. Everything else a term needs derives from a
small :class:`Criticality` enum (consequence of violation, not a
per-term tuning knob) plus the term's own physical budget. Only relative
weights matter; sweep the dial for a Pareto front rather than guessing
a single "right" value (backlog, verbatim).

**No wirelength term.** Deliberately absent — length enters through
resistance, inductance and delay where those actually matter (see
:mod:`precis.pcb.objectives`) and is correctly ignored where a net cares
about none of them. Do not add one back; that is the commonest way a
placer produces a tidy-looking, electrically mediocre board.

**Calibration is unvalidated.** Every dollar figure, every physical
constant below is order-of-magnitude and explicitly not fit to real
fabrication or bench data (backlog: "structure is sound, numbers are
not"). The ranking harness (reference designs vs. deliberately perturbed
negatives) is the cheap entry point for catching *gross* errors; it does
not discriminate near-optimal designs, which is where these numbers would
actually need tuning. Say so here rather than dressing a guess as a
derivation.

**``courtyard_overlap`` and ``board_edge_clearance`` close a real gap
(gr267456): nothing in this file used to give the optimizer a spatial-
exclusion signal at all.** ``drc.py`` treats an overlapping courtyard pair
or an edge-clearance violation as a categorical hard error — correct for
a final check, useless as an optimizer signal (a binary term is a
plateau, not a slope) — so both terms below report the SAME violation
*gradedly*, as a fraction of the SAME physical threshold DRC checks
against, imported rather than re-declared:
:data:`precis.pcb.drc.DEFAULT_COURTYARD_RADIUS_MM` for the former,
:class:`precis.pcb.capabilities.CapabilityRow`'s
``board_edge_clearance_vcut_mm`` field (already threaded through this
module as :data:`CostConfig.fab_caps`, same as ``thermal_rise`` and
``via_count`` use) for the latter. The ``drc.py`` import is a deliberate,
one-directional ``cost.py -> drc.py`` edge: ``drc.py`` imports nothing
from ``cost.py``/``optimize.py`` (no cycle), and only a plain float
constant crosses the edge, never any of ``drc.py``'s shapely/STRtree
machinery. Flagged here anyway because ``drc.py``'s own docstring frames
itself as "the final check, not the main event", downstream of realized
(L5) geometry — an always-on, L0-L4 module reaching into a downstream
terminal-stage module for a constant is an unusual direction, and
re-declaring the number instead (the "two components implementing one
rule" defect this task exists to close, see ``docs/backlog/
pcb-residual-defects-0828.md``) was judged the worse of the two options.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from precis.pcb import objectives as obj
from precis.pcb.capabilities import CapabilityRow, capability_for
from precis.pcb.drc import DEFAULT_COURTYARD_RADIUS_MM
from precis.pcb.ir import (
    UNSET_LAYER,
    Level,
    PcbIR,
    pin_point,
    same_layer_crossing_count,
)
from precis.pcb.rules import (
    implied_via_count,
    ipc2221_capacity_a,
    net_current_a_or_none,
    resolve_net_rules,
)


class Family(Enum):
    """How a term's raw value is normalized and aggregated. See the module
    docstring — this split, and only this split, decides sum vs. max."""

    MONEY = "money"  # normalized to USD; SUM
    MARGIN = "margin"  # normalized to fraction-of-budget; MAX / soft-max


class BoundDirection(Enum):
    """Which side of the truth a term's estimate is allowed to err on —
    see the module docstring's "admissibility is two-sided" section. Every
    term registered before ``crossings`` is ``LOWER``; ``crossings`` is
    the first (and, as of this writing, only) ``UPPER`` term. A term
    declaring the wrong direction is exactly the failure mode the
    per-term admissibility property test in ``tests/test_pcb_cost.py``
    exists to catch."""

    LOWER = "lower"  # coarse raw <= fine raw (may understate, never overstate)
    UPPER = "upper"  # coarse raw >= fine raw (may overstate, never understate)


class Criticality(Enum):
    """Consequence of violating a constraint *type*, assigned once and
    justified by physics/manufacturing risk — not a per-term tuning knob.
    Only used to weight the MARGIN family; money terms record a
    criticality for documentation but it plays no role in the sum."""

    CATASTROPHIC = "catastrophic"  # board is non-functional or damaged (short, plane split feeding a load)
    FUNCTIONAL = "functional"  # a feature degrades or fails intermittently (timing violation, EMI failure)
    MARGINAL = "marginal"  # yield/robustness risk under normal manufacturing variance
    COSMETIC = "cosmetic"  # avoidable cost with no functional consequence


_CRITICALITY_WEIGHT: dict[Criticality, float] = {
    Criticality.CATASTROPHIC: 8.0,
    Criticality.FUNCTIONAL: 4.0,
    Criticality.MARGINAL: 2.0,
    Criticality.COSMETIC: 1.0,
}


@dataclass(frozen=True, slots=True)
class TermValue:
    """One term's evaluated contribution at one region — kept un-collapsed
    so the digest can say "peak congestion in region C3, driven by these
    six nets" instead of a single scalar (backlog: legibility is a
    requirement, not a hope)."""

    name: str
    family: Family
    region: str  # a net name, an instance refdes, a via id, a pair key, or "board"
    raw: float  # MONEY: USD. MARGIN: fraction of that term's own budget (0 = none consumed).
    justification: str
    is_bound: bool = False  # True iff this value is an admissible bound, not a measurement — never hide this from the digest


@dataclass(frozen=True, slots=True)
class CostConfig:
    """Every tunable this module needs, in one place. ``risk_to_money`` is
    the one irreducible dial (see module docstring); everything else is a
    physical/fabrication constant, explicitly uncalibrated."""

    # -- the one dial --------------------------------------------------
    risk_to_money: float = 100.0  # USD per unit of criticality-weighted margin risk
    schedule: float = (
        0.0  # 0 = exploratory .. 1 = hardened barrier (see hardened_penalty)
    )
    p_norm: float | None = None  # None = exact max; a float = smoother p-norm soft-max

    # -- money rates (USD) ----------------------------------------------
    board_area_usd_per_mm2: float = 0.002
    layer_usd: float = 5.0
    via_usd: float = 0.02
    extended_part_fee_usd: float = 3.0
    default_instance_area_mm2: float = (
        2.0  # coarse per-instance area assumption before L3 positions exist
    )

    # -- margin budgets ---------------------------------------------------
    default_pitch_mm: float = 0.3  # trace width + clearance, generic class fallback
    assumed_max_gap_mm: float = (
        80.0  # generous board-scale gap assumption for the pre-L4 bound
    )
    inductance_budget_nh: float = 5.0
    inductance_nh_per_mm: float = 1.0  # crude partial-inductance-of-a-loop proxy
    min_loop_mm: float = 0.3  # smallest physically possible loop (via diameter scale) for the pre-L3 bound
    coupling_budget: float = 1.0
    coupling_decay_mm: float = 5.0  # e-folding distance for the proximity factor
    # Admissible pre-placement proximity factor. Unlike gap_capacity (every
    # trace guaranteed to consume *some* nonzero fraction of *some* gap),
    # two specific nets' physical distance is genuinely unconstrained
    # before L3 — they may land adjacent or on opposite corners — so 0.0
    # is the tightest value that can never overstate risk. This is NOT
    # the "undefined == zero" trap: that trap is about hiding a cost that
    # is definitely present; here the cost is conditional on a placement
    # choice that hasn't been made yet, and the candidate filter
    # (`_wants_coupling`) already keeps the *class* of risk visible via
    # the other margin terms.
    coupling_bound_k: float = 0.0
    # `crossings`' margin BUDGET is zero (a same-layer crossing is a
    # violation, never a quantity to trade -- see crossings_term_for_layer)
    # -- this is not a budget, it is the raw Euler-bound crossing COUNT at
    # which the fraction reaches "at budget" (1.0). Fixed, deliberately NOT
    # schedule-dependent: schedule-driven softening/hardening comes ONLY
    # from hardened_penalty's own convexity dial (reused, never duplicated
    # -- see that function's and crossings_term_for_layer's docstrings).
    crossings_tolerance: float = 1.0
    thermal_budget_fraction: dict[str, float] = field(
        default_factory=lambda: {"power": 0.4, "ground": 0.3}
    )  # net_class fallback ONLY for a net with no current annotation -- see _thermal_rise
    #: The fab this design realizes against — same field, same default, as
    #: :class:`precis.pcb.realize.RealizeConfig.fab_caps`; ``thermal_rise``
    #: reads it to resolve the SAME per-net width :mod:`precis.pcb.realize`
    #: will actually draw (:mod:`precis.pcb.rules`).
    fab_caps: CapabilityRow = field(default_factory=lambda: capability_for("4layer"))
    #: ``pcb_net_classes.rules`` overrides, keyed by net_class name — the
    #: same dict :class:`precis.pcb.realize.RealizeConfig.class_rules`
    #: takes, so a class-rule-authored width is what BOTH the realizer and
    #: this cost term reason about.
    class_rules: dict[str, dict[str, Any]] | None = None
    thermal_temp_rise_c: float = 10.0  # IPC-2221 target rise, thermal_rise term
    thermal_copper_oz: float = 1.0

    # -- catalog / annotation side-channels (not IR fields; optional) ----
    net_annotations: dict[int, obj.NetAnnotation] = field(default_factory=dict)
    extended_parts: frozenset[int] | None = (
        None  # override ir.inst_extended_part if supplied
    )


@dataclass(frozen=True, slots=True)
class CostResult:
    total: float
    money: float
    risk: float
    terms: list[TermValue]


#: One estimator: (ir, level, config) -> the term's TermValue(s), one per
#: region, at whatever fidelity `level` allows it to consult.
TermFn = Callable[[PcbIR, Level, CostConfig], list[TermValue]]


@dataclass(frozen=True, slots=True)
class TermSpec:
    """A registered cost term. ``justification`` is a **required field**,
    not a comment: a term whose one-line physics/manufacturing reason
    can't be written down is suspect and should not exist (backlog,
    verbatim — this is how a defunct convention like "penalize
    wirelength" gets caught at the point it would enter)."""

    name: str
    family: Family
    criticality: Criticality
    justification: str
    estimate: TermFn
    direction: BoundDirection = BoundDirection.LOWER

    def __post_init__(self) -> None:
        if not self.justification or not self.justification.strip():
            raise ValueError(f"cost term {self.name!r} has no justification")


def hardened_penalty(fraction: float, schedule: float) -> float:
    """The margin penalty shape — superlinear in ``fraction`` (budget
    consumed / budget) always, sharpening from quadratic (``schedule=0``)
    toward a steep barrier hugging ``fraction=1`` (``schedule=1``) as the
    schedule advances. **This is the entire hardening mechanism** —
    convexity IS the schedule, not a second thing layered on top.

    Two properties any barrier-shaping formula here must have, and the
    reason this isn't simply ``fraction ** (2 + k*schedule)``: raising a
    number *less than 1* to a *higher* power makes it **smaller**, so a
    naive growing exponent would make the SAME sub-budget fraction look
    *cheaper* as the schedule hardens — backwards. Instead the exponent
    stays fixed (quadratic core) and a schedule-weighted extra term is
    *added*, one that grows with both ``fraction`` and ``schedule`` — a
    comfortably low fraction (say 0.2) stays cheap at any schedule, while
    a fraction near the budget gets increasingly punished as the
    schedule advances, which is the actual "barrier tightens" behaviour
    the hardening schedule is supposed to produce.

    Monotonic non-decreasing in ``fraction`` for any fixed ``schedule``
    (needed for :func:`evaluate_cost`'s admissibility: a coarse, smaller
    fraction never produces a larger penalty than the fine, truer one)
    **and** non-decreasing in ``schedule`` for any fixed fraction (needed
    for the schedule to actually harden anything). Continuous at
    ``fraction == 1``.
    """
    if fraction <= 0.0:
        return 0.0
    schedule = max(0.0, min(1.0, schedule))
    if fraction < 1.0:
        return fraction**2 * (1.0 + schedule * 4.0 * fraction**6)
    # Beyond the budget: continuous with the branch above at fraction==1
    # (which evaluates to 1 + 4*schedule there), then steepens the
    # overage slope with schedule so an actual violation must shrink
    # back to zero as the schedule hardens, rather than being tradeable
    # away by a cheap enough alternative.
    at_budget = 1.0 + 4.0 * schedule
    return at_budget + (fraction - 1.0) * (10.0 + 90.0 * schedule)


def money_total(terms: list[TermValue]) -> float:
    return sum(t.raw for t in terms if t.family is Family.MONEY)


def margin_penalties(
    terms: list[TermValue], specs: dict[str, TermSpec], schedule: float
) -> list[float]:
    """Criticality-weighted, hardened penalty for every MARGIN term value
    — one entry per (term, region), not collapsed, so the caller can still
    trace which one is peaking."""
    out = []
    for t in terms:
        if t.family is not Family.MARGIN:
            continue
        w = _CRITICALITY_WEIGHT[specs[t.name].criticality]
        out.append(w * hardened_penalty(t.raw, schedule))
    return out


def aggregate_margin(penalties: list[float], *, p_norm: float | None = None) -> float:
    """**Max, not sum** — the whole point of splitting the two families
    (module docstring). ``p_norm`` swaps in a smoother p-norm soft-max
    (still dominated by the largest term as ``p`` grows) for callers that
    want a gradient-friendlier signal; ``None`` (the default) is the
    exact max, which is what the "1 net at 99% vs. 500 at 5%" case in the
    tests pins down explicitly."""
    if not penalties:
        return 0.0
    if p_norm is None:
        return max(penalties)
    m = max(penalties)
    if m <= 0.0:
        return 0.0
    return m * (sum((p / m) ** p_norm for p in penalties)) ** (1.0 / p_norm)


def evaluate_cost(
    ir: PcbIR, level: Level, config: CostConfig = CostConfig()
) -> CostResult:
    """The ONE cost function. Evaluates every registered term's estimator
    at ``level`` (fidelity only — see module docstring), sums the money
    family, max-aggregates the criticality-weighted margin family, and
    combines them through the single ``risk_to_money`` dial."""
    terms: list[TermValue] = []
    for spec in TERMS:
        terms.extend(spec.estimate(ir, level, config))
    money = money_total(terms)
    penalties = margin_penalties(terms, _BY_NAME, config.schedule)
    risk = aggregate_margin(penalties, p_norm=config.p_norm)
    return CostResult(
        total=money + config.risk_to_money * risk, money=money, risk=risk, terms=terms
    )


# ── money terms ──────────────────────────────────────────────────────
def board_area_term(ir: PcbIR, level: Level, config: CostConfig) -> TermValue:
    """The single ``board_area`` :class:`TermValue` — extracted out of
    :func:`_board_area`'s list-returning wrapper so
    :mod:`precis.pcb.optimize` has one call it can re-run after a move
    without going through the full-registry :func:`evaluate_cost`. This is
    the one registered term that stays honestly whole-board (a bounding
    box is not decomposable into per-segment contributions) — the
    optimizer recomputes it via a cheap vectorized numpy min/max over
    instance positions rather than this Python loop; see that module's
    docstring for why that's still within the locality budget in
    practice.

    Coarse (< L3): sum of a small constant per-instance footprint — an
    admissible lower bound because the true board must be at least large
    enough to hold every component without overlap, and a generously
    *small* per-instance constant only makes that sum smaller still
    (backlog's own worked example: "courtyard sum <= area"). Fine (>= L3):
    the bounding box of placed instances, which is always >= that same
    sum for any legal (non-overlapping) placement."""
    n = ir.n_instances
    if level < Level.L3:
        area = n * config.default_instance_area_mm2
        return TermValue(
            "board_area",
            Family.MONEY,
            "board",
            area * config.board_area_usd_per_mm2,
            "fab price scales with panel area; the sum of minimum component footprints "
            "is the tightest lower bound obtainable before placement exists",
            is_bound=True,
        )
    xs = [ir.inst_x[i] for i in range(n) if not math.isnan(ir.inst_x[i])]
    ys = [ir.inst_y[i] for i in range(n) if not math.isnan(ir.inst_y[i])]
    if len(xs) < 1:
        area = n * config.default_instance_area_mm2
    else:
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        area = max(w * h, n * config.default_instance_area_mm2)
    return TermValue(
        "board_area",
        Family.MONEY,
        "board",
        area * config.board_area_usd_per_mm2,
        "fab price scales with panel area; the placed bounding box is the tightest "
        "estimate available once positions exist",
    )


def _board_area(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    return [board_area_term(ir, level, config)]


def layer_count_term(ir: PcbIR, level: Level, config: CostConfig) -> TermValue:
    """The single ``layer_count`` :class:`TermValue` — extracted the same
    way :func:`board_area_term` was (module docstring's per-item pattern)
    so :mod:`precis.pcb.optimize` can recompute it after a LAYER_ASSIGN or
    plane promote/demote move without re-running the full registry. Unlike
    ``board_area``, this is genuinely cheap to recompute in full (a set
    over segment/net layer fields, not a geometry re-derivation) — slice
    7's own docstring anticipated exactly this: "layer_count... currently
    cached-once... slice 7's layer/topology moves will need to dirty them
    again.\""""
    if level < Level.L1:
        used = 1  # a board needs at least one layer; nothing assigned yet
        bound = True
    else:
        layers = {
            int(ir.seg_layer[s])
            for s in range(ir.n_segments)
            if int(ir.seg_layer[s]) != UNSET_LAYER
        }
        layers |= {
            int(ir.net_plane_layer[n])
            for n in range(ir.n_nets)
            if int(ir.net_plane_layer[n]) != UNSET_LAYER
        }
        used = max(1, len(layers))
        bound = False
    return TermValue(
        "layer_count",
        Family.MONEY,
        "board",
        used * config.layer_usd,
        "each additional copper layer is a discrete lamination+drill fab step, not a continuous cost",
        is_bound=bound,
    )


def _layer_count(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    return [layer_count_term(ir, level, config)]


def _via_count(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    # Zero at L0 is a genuine (not swallowed-signal) lower bound: the via
    # concept doesn't exist before L1, and 0 is the trivially correct
    # minimum for a count-based MONEY term (unlike a MARGIN term, a 0
    # here can't hide risk from a max-aggregation — it just doesn't add
    # to a sum yet, same as any other not-yet-decided money term).
    #
    # **Derived from segment layer assignments, never `ir.n_vias`**
    # (2026-08-28 fix). `ir.n_vias` only grows via `PcbIR.add_via`, which
    # has ZERO production callers anywhere in this package -- nothing ever
    # created an IR via, so this term was structurally always zero,
    # letting the optimizer pay nothing for a layer change while
    # `realize.py` independently emitted real vias wherever a track's
    # layer differed from `realize.PAD_LAYER`. `implied_via_count`
    # (`rules.py`) is now the ONE place that predicate lives -- this term
    # sums it over every segment; `realize._vias_for_track` calls the
    # exact same function for the geometry it actually draws, so the two
    # can never drift apart again (see that function's docstring for the
    # full story, and `tests/test_pcb_cost.py::
    # test_via_count_matches_realized_vias` for the anti-drift pin).
    n = (
        sum(
            implied_via_count(
                ir,
                s,
                fab_caps=config.fab_caps,
                class_rules=config.class_rules,
                temp_rise_c=config.thermal_temp_rise_c,
                copper_oz=config.thermal_copper_oz,
            )
            for s in range(ir.n_segments)
        )
        if level >= Level.L1
        else 0
    )
    return [
        TermValue(
            "via_count",
            Family.MONEY,
            "board",
            n * config.via_usd,
            "each via is a separately drilled and plated hole with its own per-hole fab cost",
            is_bound=level < Level.L1,
        )
    ]


def _extended_part_fees(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    if config.extended_parts is not None:
        n = len(config.extended_parts)
    else:
        n = int(ir.inst_extended_part.sum())
    return [
        TermValue(
            "extended_part_fees",
            Family.MONEY,
            "board",
            n * config.extended_part_fee_usd,
            "JLC charges a flat per-line surcharge for Extended-library parts (manual pick-and-place setup)",
        )
    ]


# ── margin terms ─────────────────────────────────────────────────────
def _pitch_for(ir: PcbIR, net_id: int, config: CostConfig) -> float:
    # v1 has no per-class-rules table wired into the IR yet (pcb_net_classes
    # is a store concern, out of this slice's scope) — a single generic
    # pitch stands in, documented as such.
    return config.default_pitch_mm


def gap_capacity_term(
    ir: PcbIR, seg_id: int, level: Level, config: CostConfig
) -> TermValue:
    """One segment's ``gap_capacity`` :class:`TermValue` — the body
    :func:`_gap_capacity` loops over every segment to build; extracted so
    :mod:`precis.pcb.optimize` can recompute exactly this term for one
    moved segment (its per-move delta) without re-scanning the board, the
    same math either way.

    **The flagship undefined-!=-zero example.** Before L4, no gap width
    is known at all — reporting a fraction of 0 (as if the gap were
    infinitely wide) would erase this term from the margin max whenever
    something else is nonzero, exactly the failure mode the backlog
    warns about. Instead the coarse fraction assumes a generously wide
    (but finite, configured) gap: ``pitch / assumed_max_gap`` is small
    but never zero, and is provably <= the true fraction as long as the
    real gap never exceeds ``assumed_max_gap`` (true for any board this
    architecture targets). Once L4 populates ``seg_gap_capacity`` (a
    strand count), the fraction becomes the real ``demand / capacity``
    (one segment == one strand of demand in v1)."""
    net_id = int(ir.seg_net[seg_id])
    pitch = _pitch_for(ir, net_id, config)
    net_name = str(ir.net_name[net_id])
    if int(ir.net_plane_layer[net_id]) != UNSET_LAYER:
        # Plane-served nets excluded from the routing objective (backlog,
        # verbatim, for the crossing metric — this is the nearest analog
        # this slice's registered terms have to it): a plane-promoted net
        # dog-bones a short stub to its via instead of threading a
        # full-length trace through shared gap capacity. This is a
        # genuine measured near-zero, not an "undefined == zero" trap —
        # the physical situation really is good once a net is
        # plane-promoted, so 0.0 doesn't hide anything here the way it
        # would for a segment whose gap is simply not yet known.
        return TermValue(
            "gap_capacity",
            Family.MARGIN,
            net_name,
            0.0,
            "a plane-promoted net dog-bones a short stub to its via, not a full-length "
            "trace competing for the same routed-gap capacity",
        )
    if level < Level.L4 or math.isnan(ir.seg_gap_capacity[seg_id]):
        bound_capacity = max(1.0, config.assumed_max_gap_mm / pitch)
        return TermValue(
            "gap_capacity",
            Family.MARGIN,
            net_name,
            1.0 / bound_capacity,
            "a trace cannot occupy more of a gap than its width allows; this bound assumes "
            "the most generous physically plausible gap so it can never overstate congestion",
            is_bound=True,
        )
    capacity = float(ir.seg_gap_capacity[seg_id])
    fraction = (
        1.0 / capacity if capacity > 0 else 10.0
    )  # no room at all: far over budget, not undefined
    return TermValue(
        "gap_capacity",
        Family.MARGIN,
        net_name,
        fraction,
        "a trace cannot occupy more of a gap than its width allows; measured against the "
        "actual nearest-obstacle gap once placement exists",
    )


def _gap_capacity(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    return [gap_capacity_term(ir, s, level, config) for s in range(ir.n_segments)]


def _annotation(net_id: int, ir: PcbIR, config: CostConfig) -> obj.NetAnnotation:
    if net_id in config.net_annotations:
        return config.net_annotations[net_id]
    return obj.annotation_for(None)


def loop_inductance_term(
    ir: PcbIR, seg_id: int, level: Level, config: CostConfig
) -> TermValue | None:
    """One segment's ``loop_inductance`` :class:`TermValue`, or ``None``
    when this segment's connection carries no loop-inductance objective
    (most logic nets) — the per-segment body :func:`_loop_inductance`
    loops over the board to build, extracted so :mod:`precis.pcb.optimize`
    can recompute exactly this term for one moved segment. Purely local
    by construction: unlike ``gap_capacity``, this only ever reads
    ``seg_id``'s own two endpoints, never another instance's position, so
    a move only ever needs to touch segments incident to it.

    Only meaningful for connections whose objective vector names a
    ``return_net`` (power/ground loop connections — see
    :mod:`precis.pcb.objectives`). Before L3, the coarse bound assumes
    the physically smallest possible loop (about a via's own diameter);
    that is always <= any real placement's loop, so it never overstates
    how much margin is consumed."""
    net_id = int(ir.seg_net[seg_id])
    net_class = str(ir.net_class[net_id])
    # `return_net=net_id` is a v1 placeholder: no PWR/GND pairing table
    # is wired into the IR yet (out of this slice's scope), so a
    # segment's own net id stands in for "this class DOES have a
    # return path" — objectives_for_connection only sets it non-None
    # for power/ground classes, and this term only checks `is None`,
    # never the value. Real pairing arrives with net_class rules data.
    vector, _reason = obj.objectives_for_connection(
        net_class, str(ir.net_domain[net_id]), return_net=net_id
    )
    if vector.return_net is None or vector.low_impedance <= 0.0:
        return None
    net_name = str(ir.net_name[net_id])
    if level < Level.L3:
        length_mm = config.min_loop_mm
        bound = True
    else:
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        # PAD positions, not part centres: two nets leaving one part are
        # not the same point, and pretending they are made `crossings`
        # a measurement over a degenerate graph (ir.pin_point).
        _pa, _pb = pin_point(ir, a), pin_point(ir, b)
        xa, ya = _pa if _pa is not None else (math.nan, math.nan)
        xb, yb = _pb if _pb is not None else (math.nan, math.nan)
        if math.isnan(xa) or math.isnan(xb):
            length_mm = config.min_loop_mm
            bound = True
        else:
            length_mm = max(config.min_loop_mm, math.hypot(xa - xb, ya - yb))
            bound = False
    nh = length_mm * config.inductance_nh_per_mm * vector.low_impedance
    return TermValue(
        "loop_inductance",
        Family.MARGIN,
        net_name,
        nh / config.inductance_budget_nh,
        "return-path loop inductance grows with pin-to-return separation; this is the "
        "exact quantity the '2 mm decap folklore' approximates",
        is_bound=bound,
    )


def _loop_inductance(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    out: list[TermValue] = []
    for s in range(ir.n_segments):
        t = loop_inductance_term(ir, s, level, config)
        if t is not None:
            out.append(t)
    return out


def coupling_candidates(ir: PcbIR, config: CostConfig) -> list[int]:
    """The segment ids the ``coupling`` term considers at all — position-
    independent (depends only on net class/annotation, never on
    placement), so this list is stable across every move in an anneal.
    :mod:`precis.pcb.optimize` computes it once and reuses it: a move
    only ever changes *which pairs of these* are close, never the list
    itself. "Most nets are neither strong aggressors nor sensitive
    victims" (backlog) — this is what keeps the list itself short."""
    return [s for s in range(ir.n_segments) if _wants_coupling(ir, s, config)]


def coupling_pair_term(
    ir: PcbIR, sa: int, sb: int, level: Level, config: CostConfig
) -> TermValue | None:
    """One candidate pair's ``coupling`` :class:`TermValue`, or ``None``
    when both segments happen to share a net (not a coupling pair) — the
    per-pair body :func:`_coupling` loops over every candidate pair to
    build, extracted so :mod:`precis.pcb.optimize` can recompute exactly
    the pairs touched by a move (any pair naming a moved segment) instead
    of the full O(candidates^2) sweep.

    Before L3, the proximity factor is ``coupling_bound_k`` (0.0 by
    default) — unlike gap_capacity, two specific nets' distance is
    genuinely unconstrained pre-placement (they may land adjacent or on
    opposite corners), so no nonzero value could be a safe admissible
    floor; the risk *class* stays visible to the optimizer through the
    other margin terms instead (see :data:`CostConfig.coupling_bound_k`).
    """
    net_a, net_b = int(ir.seg_net[sa]), int(ir.seg_net[sb])
    if net_a == net_b:
        return None
    ann_a, ann_b = _annotation(net_a, ir, config), _annotation(net_b, ir, config)
    if level < Level.L3:
        k = config.coupling_bound_k
        bound = True
    else:
        dist_mm = _segment_distance_mm(ir, sa, sb)
        if dist_mm is None:
            k = config.coupling_bound_k
            bound = True
        else:
            k = math.exp(-dist_mm / config.coupling_decay_mm)
            bound = False
    value = obj.coupling(ann_a, ann_b, k) + obj.coupling(ann_b, ann_a, k)
    region = f"{ir.net_name[net_a]}~{ir.net_name[net_b]}"
    return TermValue(
        "coupling",
        Family.MARGIN,
        region,
        value / config.coupling_budget,
        "coupled noise scales with aggressor strength, victim susceptibility and spatial "
        "proximity together (backlog coupling formula)",
        is_bound=bound,
    )


def _coupling(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    """Pairwise, but cheap in practice: only connections whose objective
    vector names a nonzero ``low_coupling`` weight (a real aggressor or a
    real victim) enter the candidate list at all — "most nets are neither
    strong aggressors nor sensitive victims" (backlog)."""
    candidates = coupling_candidates(ir, config)
    out: list[TermValue] = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            t = coupling_pair_term(ir, candidates[i], candidates[j], level, config)
            if t is not None:
                out.append(t)
    return out


def _wants_coupling(ir: PcbIR, seg_id: int, config: CostConfig) -> bool:
    net_id = int(ir.seg_net[seg_id])
    net_class = str(ir.net_class[net_id])
    vector, _ = obj.objectives_for_connection(net_class, str(ir.net_domain[net_id]))
    if vector.low_coupling > 0.0:
        return True
    ann = _annotation(net_id, ir, config)
    return obj.aggressor_strength(ann) > 0.0


def _segment_distance_mm(ir: PcbIR, sa: int, sb: int) -> float | None:
    def _mid(seg_id: int) -> tuple[float, float] | None:
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        # PAD positions, not part centres: two nets leaving one part are
        # not the same point, and pretending they are made `crossings`
        # a measurement over a degenerate graph (ir.pin_point).
        _pa, _pb = pin_point(ir, a), pin_point(ir, b)
        xa, ya = _pa if _pa is not None else (math.nan, math.nan)
        xb, yb = _pb if _pb is not None else (math.nan, math.nan)
        if math.isnan(xa) or math.isnan(xb):
            return None
        return (xa + xb) / 2.0, (ya + yb) / 2.0

    ma, mb = _mid(sa), _mid(sb)
    if ma is None or mb is None:
        return None
    return math.hypot(ma[0] - mb[0], ma[1] - mb[1])


def _net_layer_is_outer(ir: PcbIR, net_id: int, level: Level) -> tuple[bool, bool]:
    """Whether ``net_id``'s realized copper lands on an OUTER stackup layer
    — ``(layer_is_outer, is_bound)``. Before L1 (no layer assignment has
    been decided for anything, regardless of what ``ir.seg_layer`` happens
    to already hold — an L1-fidelity-or-coarser caller must not peek, same
    discipline :func:`layer_count_term` already follows), the OPTIMISTIC
    assumption is outer (the higher-ampacity, lower-required-width side of
    IPC-2221's split): this is what keeps the term admissible (LOWER
    bound — coarse must never overstate cost) since assuming outer can
    only ever UNDERSTATE the true required width relative to whatever the
    net's real layer turns out to be. Once L1 exists, the WORST (any
    non-outer) assigned layer among the net's own segments (or its plane
    layer) wins — thermal risk is bounded by whichever segment actually
    carries the current through the tightest copper."""
    n_layers = len(ir.stackup)
    if level < Level.L1 or n_layers == 0:
        return True, True
    layers = [
        int(ir.seg_layer[s])
        for s in range(ir.n_segments)
        if int(ir.seg_net[s]) == net_id and int(ir.seg_layer[s]) != UNSET_LAYER
    ]
    plane_layer = int(ir.net_plane_layer[net_id])
    if plane_layer != UNSET_LAYER:
        layers.append(plane_layer)
    if not layers:
        return True, True
    all_outer = all(layer_i <= 0 or layer_i >= n_layers - 1 for layer_i in layers)
    return all_outer, False


def _thermal_rise(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    """Per-connection ampacity margin — **not** the board-level
    component-to-component heat-spreading term the backlog explicitly
    excludes (open items: "no board-level thermal term... known-absent
    by decision"). This is narrower and local: how much of a net's ACTUAL
    current draw exceeds the ampacity of the width :mod:`precis.pcb.rules`
    will actually resolve for it — the SAME resolver
    :mod:`precis.pcb.realize` uses to draw the copper, closing the exact
    bug this term exists to catch (an optimizer reasoning about current
    while the geometry it scores ignores it).

    A net with NO current annotation (``ir.net_current_a`` is ``nan`` —
    "keep today's behaviour", not an invented figure) falls back to the
    original class-fraction placeholder
    (:data:`CostConfig.thermal_budget_fraction`), unchanged."""
    out: list[TermValue] = []
    for net_id in range(ir.n_nets):
        net_class = str(ir.net_class[net_id]).strip().lower()
        current_a = net_current_a_or_none(float(ir.net_current_a[net_id]))
        if current_a is None or current_a <= 0.0:
            fraction = config.thermal_budget_fraction.get(net_class)
            if not fraction:
                continue
            out.append(
                TermValue(
                    "thermal_rise",
                    Family.MARGIN,
                    str(ir.net_name[net_id]),
                    fraction,
                    "IPC-2221-style temperature rise vs. a generic-width ampacity budget for "
                    "current-carrying rails; no current annotation yet, so this net falls back "
                    "to a class-assumed load fraction rather than inventing a current figure",
                    is_bound=True,
                )
            )
            continue

        layer_is_outer, is_bound = _net_layer_is_outer(ir, net_id, level)
        overrides = (config.class_rules or {}).get(net_class)
        resolved = resolve_net_rules(
            net_class,
            layer_is_outer=layer_is_outer,
            fab_caps=config.fab_caps,
            overrides=overrides,
            current_a=current_a,
            temp_rise_c=config.thermal_temp_rise_c,
            copper_oz=config.thermal_copper_oz,
        )
        capacity_a = ipc2221_capacity_a(
            resolved.track_width_mm,
            layer_is_outer=layer_is_outer,
            temp_rise_c=config.thermal_temp_rise_c,
            copper_oz=config.thermal_copper_oz,
        )
        fraction = current_a / capacity_a if capacity_a > 0.0 else 10.0
        out.append(
            TermValue(
                "thermal_rise",
                Family.MARGIN,
                str(ir.net_name[net_id]),
                fraction,
                "IPC-2221-style temperature rise scored against the SAME resolved trace "
                "width realize.py will actually draw for this net -- current / ampacity "
                "of the real copper, not a generic-width placeholder",
                is_bound=is_bound,
            )
        )
    return out


def crossings_term_for_layer(
    ir: PcbIR, layer: int, level: Level, config: CostConfig
) -> TermValue:
    """One layer's ``crossings`` :class:`TermValue` — extracted the same
    way :func:`board_area_term`/:func:`layer_count_term` were (module
    docstring's per-item pattern) so :mod:`precis.pcb.optimize` can
    recompute exactly this term for the (at most two) layers a
    ``LAYER_ASSIGN`` move touches, never the whole board.

    **Backed by** :func:`precis.pcb.ir.same_layer_crossing_count` (found
    on contact 2026-08-28, replacing the Euler-bound backing this term
    shipped with — see :func:`precis.pcb.ir.same_layer_crossing_bound`'s
    docstring for the forest proof of why that bound is provably always
    zero on a real, star-decomposed board and therefore cannot be a cost
    signal at all): a sweep-line count of ACTUAL straight-line segment
    intersections at L3, ``O(n log n + k)``.

    **``BoundDirection.UPPER`` — the direction flip this fix required.**
    The geometric count is an upper bound on eventually-REALIZED
    crossings (realize.py's router can sometimes route around a
    straight-line crossing), never a lower one — the opposite of every
    other registered term. Fidelity increases as ``level`` rises the same
    way it does elsewhere, but for an UPPER-bound term "coarser" must
    mean "more pessimistic" (never understate), the mirror image of every
    LOWER-bound term's "undefined != zero" rule:
    - ``level < Level.L1`` (no layer assignment decided yet): the
      worst-case placeholder is "every segment on the board could, in the
      end, land on this one layer" — ``C(n_segments, 2)`` same-layer
      pairs, all of which *might* cross. Loose, but always true regardless
      of how assignment eventually resolves.
    - ``Level.L1 <= level < Level.L3`` (layer known, no positions yet):
      tighter — ``C(m, 2)`` where ``m`` is the segment count ACTUALLY
      assigned to this layer, since every same-layer pair might cross in
      the worst case (this always dominates the true count: it doesn't
      even exclude same-net pairs, which can never cross, making it
      looser still but simpler and still valid).
    - ``level >= Level.L3``: the real geometric sweep-line count.
    Each tier is provably >= the next (``C(n_segments,2) >= C(m,2) >=``
    the true geometric count, since the geometric count only counts a
    SUBSET of same-layer pairs), so the per-term admissibility property
    holds by construction. ``is_bound=True`` at every level, including
    L3+: even the exact geometric count is still only a bound on the
    REALIZED crossing count, never a claim to already BE it (contrast
    ``gap_capacity``, which does become a genuine measurement at L4).

    **Margin family, budget ZERO** (backlog, verbatim: a same-layer
    crossing is a manufacturing/topology violation — it cannot be
    realized without a via or a reroute — not a quantity to trade against
    money the way real per-net gap headroom is). So there is no "how much
    of a real physical budget is this" question; instead
    ``config.crossings_tolerance`` (a small FIXED constant) sets the raw
    crossing-count scale at which the fraction reaches "at budget" (1.0),
    and ALL of the schedule-driven softening (early, exploratory) /
    hardening (late, barrier) behaviour comes from
    :func:`hardened_penalty`'s own convexity dial, reused exactly as
    every other margin term reuses it — not a second hardening mechanism.
    The backlog's "tolerance shrinks over the schedule" describes exactly
    this effect in plain language: the SAME raw fraction gets punished
    increasingly harshly as ``config.schedule`` advances (both the
    quadratic-core term below budget and the steepening overage slope
    above it grow with schedule — see :func:`hardened_penalty`), which is
    what makes early passes able to walk through a crossing state and
    late passes unable to — not a literally shrinking denominator, which
    would duplicate the mechanism :mod:`precis.pcb.cost` already owns.

    **Known, unchanged gap: plane-served nets are NOT excluded here.**
    The architecture section's "signal-net crossings (plane-served nets
    excluded)" describes the eventual objective; this term (both before
    and after this fix) does not yet read ``net_plane_layer`` — carried
    over unchanged rather than folded into this fix's scope, exactly the
    same honest-deferral shape as ``SIDE_FLIP``'s known inertness in
    :mod:`precis.pcb.optimize`.
    """
    if level >= Level.L3:
        count = same_layer_crossing_count(ir, layer)
    else:
        # Pre-L3 UPPER-bound placeholder: no positions exist yet, so the
        # safe (never-understating) count is "every same-layer pair might
        # cross" -- see the docstring's per-tier breakdown.
        m = ir.n_segments if level < Level.L1 else int((ir.seg_layer == layer).sum())
        count = m * (m - 1) // 2
    fraction = count / config.crossings_tolerance
    return TermValue(
        "crossings",
        Family.MARGIN,
        f"layer{layer}",
        fraction,
        "a same-layer crossing is an unresolved airwire conflict that cannot be realized "
        "without a via or a reroute -- exactly what the layered ratsnest's layer "
        "assignment and side choice exist to resolve",
        is_bound=True,
    )


def _crossings(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    """Every layer's ``crossings`` value — see
    :func:`crossings_term_for_layer` for the full per-level formula
    (an UPPER-bound placeholder before L3 positions exist, the real
    geometric sweep-line count once they do)."""
    return [
        crossings_term_for_layer(ir, layer, level, config)
        for layer in range(ir.n_layers)
    ]


# ── courtyard overlap (gr267456) ─────────────────────────────────────────

#: Two instances' courtyard circles first touch when their centre-to-
#: centre distance drops to this — the SAME threshold ``drc.
#: check_courtyard_overlap`` uses for its own two circles (each of radius
#: :data:`~precis.pcb.drc.DEFAULT_COURTYARD_RADIUS_MM`), imported rather
#: than re-declared (module docstring).
COURTYARD_MIN_SEPARATION_MM = 2.0 * DEFAULT_COURTYARD_RADIUS_MM

_COURTYARD_JUSTIFICATION = (
    "two components' courtyards cannot physically overlap on a manufactured board -- "
    "the same rule drc.check_courtyard_overlap enforces as a hard error, graded here "
    "as overlap depth (fraction of the shared minimum separation) so the optimizer "
    "has a slope to descend rather than a plateau"
)


def courtyard_overlap_pair_term(
    ir: PcbIR, ia: int, ib: int, level: Level, config: CostConfig
) -> TermValue:
    """One instance PAIR's ``courtyard_overlap`` :class:`TermValue` — the
    optimizer-visible, GRADED counterpart to ``drc.check_courtyard_
    overlap``'s hard, binary "error". DRC treats any overlap as
    categorical, but an optimizer needs a slope to descend, not a
    plateau, so this reports how DEEPLY the two courtyards overlap, as a
    fraction of :data:`COURTYARD_MIN_SEPARATION_MM` (the same threshold
    DRC's two circles collide at) — 0.0 the instant the circles stop
    touching, 1.0 at perfect coincidence, and (like every other fraction
    in this module) unbounded above 1.0 has no meaning here since a
    centre-to-centre distance can't go negative.

    Before L3 (no committed position for either instance), two SPECIFIC
    instances' eventual placement is exactly as unconstrained as
    :func:`coupling_pair_term`'s own pre-placement case — they may land
    adjacent or on opposite corners of the eventual board — so 0.0 is the
    tightest LOWER-admissible bound (see :data:`CostConfig.
    coupling_bound_k`'s docstring for the identical argument): this is
    NOT the "undefined == zero" trap, because no risk is being hidden —
    it is genuinely undetermined by a placement choice not yet made."""
    refdes_a = str(ir.instance_refdes[ia])
    refdes_b = str(ir.instance_refdes[ib])
    region = f"{refdes_a}~{refdes_b}"
    if level < Level.L3:
        return TermValue(
            "courtyard_overlap",
            Family.MARGIN,
            region,
            0.0,
            _COURTYARD_JUSTIFICATION,
            is_bound=True,
        )
    # INSTANCE centroids, deliberately — a courtyard is the part's BODY,
    # not its pads. This is the one geometric term that must NOT move to
    # pin_point: pads sit inside the courtyard, so measuring body overlap
    # from pad positions would understate it.
    xa, ya = float(ir.inst_x[ia]), float(ir.inst_y[ia])
    xb, yb = float(ir.inst_x[ib]), float(ir.inst_y[ib])
    if math.isnan(xa) or math.isnan(ya) or math.isnan(xb) or math.isnan(yb):
        return TermValue(
            "courtyard_overlap",
            Family.MARGIN,
            region,
            0.0,
            _COURTYARD_JUSTIFICATION,
            is_bound=True,
        )
    dist_mm = math.hypot(xa - xb, ya - yb)
    overlap_mm = max(0.0, COURTYARD_MIN_SEPARATION_MM - dist_mm)
    fraction = overlap_mm / COURTYARD_MIN_SEPARATION_MM
    return TermValue(
        "courtyard_overlap",
        Family.MARGIN,
        region,
        fraction,
        _COURTYARD_JUSTIFICATION,
        is_bound=False,
    )


def _courtyard_overlap(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    """Every instance PAIR's ``courtyard_overlap`` value — genuinely
    O(n_instances^2), same carve-out as ``coupling``'s own full double
    loop (module docstring's "what is NOT claimed local" section):
    :mod:`precis.pcb.optimize` never calls this full-board version per
    move, it maintains its own grid-bucketed incremental cache instead
    (see that module's docstring)."""
    n = ir.n_instances
    return [
        courtyard_overlap_pair_term(ir, ia, ib, level, config)
        for ia in range(n)
        for ib in range(ia + 1, n)
    ]


# ── board-edge clearance (gr267456 addendum) ─────────────────────────────

#: The SAME field ``drc.check_board_edge_clearance`` reads when the caller
#: doesn't know the panel type yet (V-cut, the conservative tier — module
#: docstring there: "when the caller doesn't know the panel type yet, use
#: the V-cut figure"). This module has no panel-type concept at placement
#: time either, so it always reads this one field — never the routed-edge
#: field, and never a re-declared clearance figure of its own.
_BOARD_EDGE_FIELD = "board_edge_clearance_vcut_mm"


def outline_bbox(
    outline: list[tuple[float, float]],
) -> tuple[float, float, float, float]:
    """The axis-aligned bounding box (``x0, y0, x1, y1``) of a polygon
    ring — shared, not duplicated, between this module's
    ``board_edge_clearance`` term and :mod:`precis.pcb.optimize`'s
    TRANSLATE-move clamp (both approximate ``ir.outline`` the same way;
    see :func:`board_edge_clearance_term`'s docstring)."""
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    return min(xs), min(ys), max(xs), max(ys)


def _signed_inside_depth_mm(
    x: float, y: float, bbox: tuple[float, float, float, float]
) -> float:
    """How far ``(x, y)`` sits inside ``bbox``'s nearest edge — POSITIVE
    when inside (the distance to the closest of the four edges), NEGATIVE
    when outside (the Euclidean distance back to the nearest point of the
    rectangle, negated). A signed, single-number "clearance available"
    that :func:`board_edge_clearance_term` compares directly against the
    required clearance, so a part drifting further and further outside
    the board keeps getting a worse (more negative) number rather than
    plateauing the moment it crosses the edge."""
    x0, y0, x1, y1 = bbox
    dx_out = max(x0 - x, x - x1, 0.0)
    dy_out = max(y0 - y, y - y1, 0.0)
    if dx_out > 0.0 or dy_out > 0.0:
        return -math.hypot(dx_out, dy_out)
    return min(x - x0, x1 - x, y - y0, y1 - y)


_BOARD_EDGE_JUSTIFICATION = (
    "copper must clear the panel edge by the fab's own margin -- the same rule "
    "drc.check_board_edge_clearance enforces as a hard/soft two-tier finding, "
    "graded here as intrusion-or-overshoot depth (fraction of the required "
    "clearance) so the optimizer has a slope to descend rather than a plateau"
)


def board_edge_clearance_term(
    ir: PcbIR, inst_id: int, level: Level, config: CostConfig
) -> TermValue | None:
    """One instance's ``board_edge_clearance`` :class:`TermValue`, or
    ``None`` when no board outline is known yet (``ir.outline`` is
    ``None``/too short to bound anything, or the fab capability row
    publishes no figure for this field at all) — the same "nothing to
    check" rule ``drc.check_board_edge_clearance`` itself applies, not an
    invented boundary or an invented clearance figure.

    **Approximates the outline by its axis-aligned bounding box**
    (:func:`outline_bbox`), not the exact polygon boundary ``drc.
    check_board_edge_clearance`` measures against REALIZED copper — this
    term only ever sees an instance CENTROID (no footprint/copper geometry
    exists yet at L3), and every reference board this build targets is
    rectangular, so the approximation is exact for the common case and
    still a same-direction (never risk-hiding) proxy elsewhere. Mirrors
    :mod:`precis.pcb.optimize`'s own TRANSLATE-clamp simplification — one
    approximation of the outline, not two independently invented ones.

    Reads the SAME two-tier clearance figure ``drc.check_board_edge_
    clearance`` does — :data:`_BOARD_EDGE_FIELD` off the shared, already-
    resolved :class:`~precis.pcb.capabilities.CapabilityRow`
    (:data:`CostConfig.fab_caps`, the same row ``thermal_rise``/
    ``via_count`` already use) — never a re-declared figure. Prefers the
    house-default tier (the deliberate-margin target) as the term's
    BUDGET, falling back to the JLC-min floor only when no house default
    is published for this field.

    Before L3 (no committed position), or for an instance with no
    position yet, this is exactly :func:`courtyard_overlap_pair_term`'s
    own pre-placement argument: 0.0 is the tightest LOWER-admissible
    bound, not a hidden risk (no move has decided where relative to the
    board edge this instance eventually lands)."""
    if not ir.outline or len(ir.outline) < 3:
        return None
    required_mm = config.fab_caps.house_default.get(
        _BOARD_EDGE_FIELD
    ) or config.fab_caps.jlc_min.get(_BOARD_EDGE_FIELD)
    if not required_mm:
        return None
    region = str(ir.instance_refdes[inst_id])
    if level < Level.L3:
        return TermValue(
            "board_edge_clearance",
            Family.MARGIN,
            region,
            0.0,
            _BOARD_EDGE_JUSTIFICATION,
            is_bound=True,
        )
    x, y = float(ir.inst_x[inst_id]), float(ir.inst_y[inst_id])
    if math.isnan(x) or math.isnan(y):
        return TermValue(
            "board_edge_clearance",
            Family.MARGIN,
            region,
            0.0,
            _BOARD_EDGE_JUSTIFICATION,
            is_bound=True,
        )
    bbox = outline_bbox(ir.outline)
    depth_mm = _signed_inside_depth_mm(x, y, bbox)
    violation_mm = max(0.0, required_mm - depth_mm)
    fraction = violation_mm / required_mm
    return TermValue(
        "board_edge_clearance",
        Family.MARGIN,
        region,
        fraction,
        _BOARD_EDGE_JUSTIFICATION,
        is_bound=False,
    )


def _board_edge_clearance(
    ir: PcbIR, level: Level, config: CostConfig
) -> list[TermValue]:
    out: list[TermValue] = []
    for i in range(ir.n_instances):
        t = board_edge_clearance_term(ir, i, level, config)
        if t is not None:
            out.append(t)
    return out


TERMS: list[TermSpec] = [
    TermSpec(
        "board_area",
        Family.MONEY,
        Criticality.FUNCTIONAL,
        "fab price scales with panel area",
        _board_area,
        direction=BoundDirection.LOWER,
    ),
    TermSpec(
        "layer_count",
        Family.MONEY,
        Criticality.FUNCTIONAL,
        "each copper layer is a discrete lamination/drill fab step",
        _layer_count,
        direction=BoundDirection.LOWER,
    ),
    TermSpec(
        "via_count",
        Family.MONEY,
        Criticality.MARGINAL,
        "each via is a separately drilled/plated hole",
        _via_count,
        direction=BoundDirection.LOWER,
    ),
    TermSpec(
        "extended_part_fees",
        Family.MONEY,
        Criticality.COSMETIC,
        "JLC's flat per-line surcharge for Extended-library parts",
        _extended_part_fees,
        direction=BoundDirection.LOWER,
    ),
    TermSpec(
        "gap_capacity",
        Family.MARGIN,
        Criticality.CATASTROPHIC,
        "a trace physically cannot exceed the width a gap allows — a manufacturing impossibility, not a preference",
        _gap_capacity,
        direction=BoundDirection.LOWER,
    ),
    TermSpec(
        "loop_inductance",
        Family.MARGIN,
        Criticality.FUNCTIONAL,
        "return-path loop inductance grows with pin-to-return separation and degrades decoupling/EMI",
        _loop_inductance,
        direction=BoundDirection.LOWER,
    ),
    TermSpec(
        "coupling",
        Family.MARGIN,
        Criticality.FUNCTIONAL,
        "aggressor edge rate x victim susceptibility x proximity is the physical coupled-noise mechanism",
        _coupling,
        direction=BoundDirection.LOWER,
    ),
    TermSpec(
        "thermal_rise",
        Family.MARGIN,
        Criticality.MARGINAL,
        "current-carrying traces heat per IPC-2221; exceeding the class budget risks yield/reliability",
        _thermal_rise,
        direction=BoundDirection.LOWER,
    ),
    TermSpec(
        "crossings",
        Family.MARGIN,
        Criticality.CATASTROPHIC,
        "a same-layer crossing cannot be realized as sketched without spending a via or a "
        "reroute -- the exact thing the layered ratsnest exists to make legible and resolvable",
        _crossings,
        direction=BoundDirection.UPPER,
    ),
    TermSpec(
        "courtyard_overlap",
        Family.MARGIN,
        Criticality.CATASTROPHIC,
        _COURTYARD_JUSTIFICATION,
        _courtyard_overlap,
        direction=BoundDirection.LOWER,
    ),
    TermSpec(
        "board_edge_clearance",
        Family.MARGIN,
        Criticality.CATASTROPHIC,
        _BOARD_EDGE_JUSTIFICATION,
        _board_edge_clearance,
        direction=BoundDirection.LOWER,
    ),
]

_BY_NAME: dict[str, TermSpec] = {t.name: t for t in TERMS}

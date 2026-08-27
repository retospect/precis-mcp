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

**Admissibility.** Every estimator must be optimistic: it may understate
cost or overstate feasibility, never the reverse (A* admissibility). The
payoff: a state that looks bad at a coarse level really is bad and can be
pruned without discarding a good solution. Tested as a property in
``tests/test_pcb_cost.py`` — generate random IR states, evaluate the same
object at a coarse and a fine level, assert coarse ``total`` never
exceeds fine ``total``.

**Undefined != zero.** A term with nothing to measure yet at this level
(gap capacity before L4) must still return a nonzero, *admissible* bound —
never literally 0, which would tell the optimizer congestion is free and
produce states that look excellent at L1 and are unroutable at L4. Every
estimator below that has a coarse/fine split says explicitly, in its
docstring, what the coarse bound assumes and why it's still ≤ the truth.

**Two families, aggregated differently.** Money terms (board area, layer
count, via count, part fees) normalize to USD and **sum** — money is
fungible and additive. Margin terms (clearance, loop inductance,
coupling, thermal rise) normalize to *fraction of that term's own budget*
and aggregate by **max** (or a soft p-norm) — a sum would let 500 nets at
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
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from precis.pcb import objectives as obj
from precis.pcb.ir import UNSET_LAYER, Level, PcbIR


class Family(Enum):
    """How a term's raw value is normalized and aggregated. See the module
    docstring — this split, and only this split, decides sum vs. max."""

    MONEY = "money"  # normalized to USD; SUM
    MARGIN = "margin"  # normalized to fraction-of-budget; MAX / soft-max


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
    schedule: float = 0.0  # 0 = exploratory .. 1 = hardened barrier (see hardened_penalty)
    p_norm: float | None = None  # None = exact max; a float = smoother p-norm soft-max

    # -- money rates (USD) ----------------------------------------------
    board_area_usd_per_mm2: float = 0.002
    layer_usd: float = 5.0
    via_usd: float = 0.02
    extended_part_fee_usd: float = 3.0
    default_instance_area_mm2: float = 2.0  # coarse per-instance area assumption before L3 positions exist

    # -- margin budgets ---------------------------------------------------
    default_pitch_mm: float = 0.3  # trace width + clearance, generic class fallback
    assumed_max_gap_mm: float = 80.0  # generous board-scale gap assumption for the pre-L4 bound
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
    thermal_budget_fraction: dict[str, float] = field(
        default_factory=lambda: {"power": 0.4, "ground": 0.3}
    )  # net_class -> assumed loading fraction of a generic-width ampacity budget; 0 for other classes

    # -- catalog / annotation side-channels (not IR fields; optional) ----
    net_annotations: dict[int, obj.NetAnnotation] = field(default_factory=dict)
    extended_parts: frozenset[int] | None = None  # override ir.inst_extended_part if supplied


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

    def __post_init__(self) -> None:
        if not self.justification or not self.justification.strip():
            raise ValueError(f"cost term {self.name!r} has no justification")


def hardened_penalty(fraction: float, schedule: float) -> float:
    """The margin penalty shape — superlinear in ``fraction`` (budget
    consumed / budget) always, sharpening from quadratic
    (``schedule=0``) toward a steep barrier (``schedule=1``) as the
    schedule advances. **This is the entire hardening mechanism** —
    convexity IS the schedule, not a second thing layered on top.
    Monotonic non-decreasing in ``fraction`` for any fixed ``schedule``,
    which is exactly the property :func:`evaluate_cost`'s admissibility
    needs: a coarse (smaller) fraction never produces a *larger*
    penalty than the fine (larger, truer) one.
    """
    if fraction <= 0.0:
        return 0.0
    schedule = max(0.0, min(1.0, schedule))
    exponent = 2.0 + 6.0 * schedule  # quadratic .. octic
    if fraction < 1.0:
        return fraction**exponent
    # Beyond the budget: continuous with the branch above at fraction==1,
    # steepening with schedule so an actual violation must shrink back to
    # zero by the time the schedule hardens, rather than being tradeable
    # away by a cheap enough alternative.
    return 1.0 + (fraction - 1.0) * (10.0 + 90.0 * schedule)


def money_total(terms: list[TermValue]) -> float:
    return sum(t.raw for t in terms if t.family is Family.MONEY)


def margin_penalties(terms: list[TermValue], specs: dict[str, TermSpec], schedule: float) -> list[float]:
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


def evaluate_cost(ir: PcbIR, level: Level, config: CostConfig = CostConfig()) -> CostResult:
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
    return CostResult(total=money + config.risk_to_money * risk, money=money, risk=risk, terms=terms)


# ── money terms ──────────────────────────────────────────────────────
def _board_area(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    """Coarse (< L3): sum of a small constant per-instance footprint — an
    admissible lower bound because the true board must be at least large
    enough to hold every component without overlap, and a generously
    *small* per-instance constant only makes that sum smaller still
    (backlog's own worked example: "courtyard sum <= area"). Fine (>= L3):
    the bounding box of placed instances, which is always >= that same
    sum for any legal (non-overlapping) placement."""
    n = ir.n_instances
    if level < Level.L3:
        area = n * config.default_instance_area_mm2
        return [
            TermValue(
                "board_area",
                Family.MONEY,
                "board",
                area * config.board_area_usd_per_mm2,
                "fab price scales with panel area; the sum of minimum component footprints "
                "is the tightest lower bound obtainable before placement exists",
                is_bound=True,
            )
        ]
    xs = [ir.inst_x[i] for i in range(n) if not math.isnan(ir.inst_x[i])]
    ys = [ir.inst_y[i] for i in range(n) if not math.isnan(ir.inst_y[i])]
    if len(xs) < 1:
        area = n * config.default_instance_area_mm2
    else:
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        area = max(w * h, n * config.default_instance_area_mm2)
    return [
        TermValue(
            "board_area",
            Family.MONEY,
            "board",
            area * config.board_area_usd_per_mm2,
            "fab price scales with panel area; the placed bounding box is the tightest "
            "estimate available once positions exist",
        )
    ]


def _layer_count(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    if level < Level.L1:
        used = 1  # a board needs at least one layer; nothing assigned yet
        bound = True
    else:
        layers = {int(ir.seg_layer[s]) for s in range(ir.n_segments) if int(ir.seg_layer[s]) != UNSET_LAYER}
        layers |= {int(ir.net_plane_layer[n]) for n in range(ir.n_nets) if int(ir.net_plane_layer[n]) != UNSET_LAYER}
        used = max(1, len(layers))
        bound = False
    return [
        TermValue(
            "layer_count",
            Family.MONEY,
            "board",
            used * config.layer_usd,
            "each additional copper layer is a discrete lamination+drill fab step, not a continuous cost",
            is_bound=bound,
        )
    ]


def _via_count(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    # Zero at L0 is a genuine (not swallowed-signal) lower bound: the via
    # concept doesn't exist before L1, and 0 is the trivially correct
    # minimum for a count-based MONEY term (unlike a MARGIN term, a 0
    # here can't hide risk from a max-aggregation — it just doesn't add
    # to a sum yet, same as any other not-yet-decided money term).
    n = ir.n_vias if level >= Level.L1 else 0
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


def _gap_capacity(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    """**The flagship undefined-!=-zero example.** Before L4, no gap width
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
    out: list[TermValue] = []
    for s in range(ir.n_segments):
        net_id = int(ir.seg_net[s])
        pitch = _pitch_for(ir, net_id, config)
        net_name = str(ir.net_name[net_id])
        if level < Level.L4 or math.isnan(ir.seg_gap_capacity[s]):
            bound_capacity = max(1.0, config.assumed_max_gap_mm / pitch)
            out.append(
                TermValue(
                    "gap_capacity",
                    Family.MARGIN,
                    net_name,
                    1.0 / bound_capacity,
                    "a trace cannot occupy more of a gap than its width allows; this bound assumes "
                    "the most generous physically plausible gap so it can never overstate congestion",
                    is_bound=True,
                )
            )
            continue
        capacity = float(ir.seg_gap_capacity[s])
        fraction = 1.0 / capacity if capacity > 0 else 10.0  # no room at all: far over budget, not undefined
        out.append(
            TermValue(
                "gap_capacity",
                Family.MARGIN,
                net_name,
                fraction,
                "a trace cannot occupy more of a gap than its width allows; measured against the "
                "actual nearest-obstacle gap once placement exists",
            )
        )
    return out


def _annotation(net_id: int, ir: PcbIR, config: CostConfig) -> obj.NetAnnotation:
    if net_id in config.net_annotations:
        return config.net_annotations[net_id]
    return obj.annotation_for(None)


def _loop_inductance(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    """Only meaningful for connections whose objective vector names a
    ``return_net`` (power/ground loop connections — see
    :mod:`precis.pcb.objectives`). Before L3, the coarse bound assumes
    the physically smallest possible loop (about a via's own diameter);
    that is always <= any real placement's loop, so it never overstates
    how much margin is consumed."""
    out: list[TermValue] = []
    for s in range(ir.n_segments):
        net_id = int(ir.seg_net[s])
        net_class = str(ir.net_class[net_id])
        # `return_net=net_id` is a v1 placeholder: no PWR/GND pairing table
        # is wired into the IR yet (out of this slice's scope), so a
        # segment's own net id stands in for "this class DOES have a
        # return path" — objectives_for_connection only sets it non-None
        # for power/ground classes, and this term only checks `is None`,
        # never the value. Real pairing arrives with net_class rules data.
        vector, _reason = obj.objectives_for_connection(net_class, str(ir.net_domain[net_id]), return_net=net_id)
        if vector.return_net is None or vector.low_impedance <= 0.0:
            continue
        net_name = str(ir.net_name[net_id])
        if level < Level.L3:
            length_mm = config.min_loop_mm
            bound = True
        else:
            a, b = int(ir.seg_pin_a[s]), int(ir.seg_pin_b[s])
            ia, ib = int(ir.pin_instance[a]), int(ir.pin_instance[b])
            xa, ya, xb, yb = ir.inst_x[ia], ir.inst_y[ia], ir.inst_x[ib], ir.inst_y[ib]
            if math.isnan(xa) or math.isnan(xb):
                length_mm = config.min_loop_mm
                bound = True
            else:
                length_mm = max(config.min_loop_mm, math.hypot(xa - xb, ya - yb))
                bound = False
        nh = length_mm * config.inductance_nh_per_mm * vector.low_impedance
        out.append(
            TermValue(
                "loop_inductance",
                Family.MARGIN,
                net_name,
                nh / config.inductance_budget_nh,
                "return-path loop inductance grows with pin-to-return separation; this is the "
                "exact quantity the '2 mm decap folklore' approximates",
                is_bound=bound,
            )
        )
    return out


def _coupling(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    """Pairwise, but cheap in practice: only connections whose objective
    vector names a nonzero ``low_coupling`` weight (a real aggressor or a
    real victim) enter the candidate list at all — "most nets are neither
    strong aggressors nor sensitive victims" (backlog). Before L3, the
    proximity factor is ``coupling_bound_k`` (0.0 by default) — unlike
    gap_capacity, two specific nets' distance is genuinely unconstrained
    pre-placement (they may land adjacent or on opposite corners), so no
    nonzero value could be a safe admissible floor; the risk *class*
    stays visible to the optimizer through the other margin terms
    instead (see :data:`CostConfig.coupling_bound_k`)."""
    candidates = [s for s in range(ir.n_segments) if _wants_coupling(ir, s, config)]
    out: list[TermValue] = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            sa, sb = candidates[i], candidates[j]
            net_a, net_b = int(ir.seg_net[sa]), int(ir.seg_net[sb])
            if net_a == net_b:
                continue
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
            out.append(
                TermValue(
                    "coupling",
                    Family.MARGIN,
                    region,
                    value / config.coupling_budget,
                    "coupled noise scales with aggressor strength, victim susceptibility and spatial "
                    "proximity together (backlog coupling formula)",
                    is_bound=bound,
                )
            )
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
        ia, ib = int(ir.pin_instance[a]), int(ir.pin_instance[b])
        xa, ya, xb, yb = ir.inst_x[ia], ir.inst_y[ia], ir.inst_x[ib], ir.inst_y[ib]
        if math.isnan(xa) or math.isnan(xb):
            return None
        return (xa + xb) / 2.0, (ya + yb) / 2.0

    ma, mb = _mid(sa), _mid(sb)
    if ma is None or mb is None:
        return None
    return math.hypot(ma[0] - mb[0], ma[1] - mb[1])


def _thermal_rise(ir: PcbIR, level: Level, config: CostConfig) -> list[TermValue]:
    """Per-connection ampacity margin — **not** the board-level
    component-to-component heat-spreading term the backlog explicitly
    excludes (open items: "no board-level thermal term... known-absent
    by decision"). This is narrower and local: how much of a class's
    assumed current-carrying budget a power/ground connection draws.
    Current and trace width aren't first-class IR fields yet (width is
    assigned by the tiling pass, a later slice), so this term is
    deliberately **level-invariant** — it has nothing further to learn
    from L3/L4 in this slice, and a constant estimate trivially satisfies
    admissibility (coarse == fine)."""
    out: list[TermValue] = []
    for net_id in range(ir.n_nets):
        net_class = str(ir.net_class[net_id]).strip().lower()
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
                "current-carrying rails; refines once realize.py assigns real trace widths",
                is_bound=True,
            )
        )
    return out


TERMS: list[TermSpec] = [
    TermSpec("board_area", Family.MONEY, Criticality.FUNCTIONAL, "fab price scales with panel area", _board_area),
    TermSpec(
        "layer_count",
        Family.MONEY,
        Criticality.FUNCTIONAL,
        "each copper layer is a discrete lamination/drill fab step",
        _layer_count,
    ),
    TermSpec("via_count", Family.MONEY, Criticality.MARGINAL, "each via is a separately drilled/plated hole", _via_count),
    TermSpec(
        "extended_part_fees",
        Family.MONEY,
        Criticality.COSMETIC,
        "JLC's flat per-line surcharge for Extended-library parts",
        _extended_part_fees,
    ),
    TermSpec(
        "gap_capacity",
        Family.MARGIN,
        Criticality.CATASTROPHIC,
        "a trace physically cannot exceed the width a gap allows — a manufacturing impossibility, not a preference",
        _gap_capacity,
    ),
    TermSpec(
        "loop_inductance",
        Family.MARGIN,
        Criticality.FUNCTIONAL,
        "return-path loop inductance grows with pin-to-return separation and degrades decoupling/EMI",
        _loop_inductance,
    ),
    TermSpec(
        "coupling",
        Family.MARGIN,
        Criticality.FUNCTIONAL,
        "aggressor edge rate x victim susceptibility x proximity is the physical coupled-noise mechanism",
        _coupling,
    ),
    TermSpec(
        "thermal_rise",
        Family.MARGIN,
        Criticality.MARGINAL,
        "current-carrying traces heat per IPC-2221; exceeding the class budget risks yield/reliability",
        _thermal_rise,
    ),
]

_BY_NAME: dict[str, TermSpec] = {t.name: t for t in TERMS}

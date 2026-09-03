"""Joint place+route optimizer: one engine, one (placement, sketch) state,
per docs/backlog/pcb-guided-place-route.md §"Joint place+route optimizer".
Slice 6 restricts the move set to placement (translate, rotate 90°,
swap-pair); slice 7 adds topology/layer/pin-swap moves as new
:data:`MOVE_GENERATORS` entries — the state, cost plumbing and SA loop
below don't change shape for that.

One engine, not place-then-route, because under :mod:`precis.pcb.ir`'s
rubber-band sketch topology (L0-L2) is invariant to a placement (L3)
perturbation — a move dirties L3/L4/L5 only (see ``PcbIR.move_instance``).

**Hard locality constraint**: every cost term must decompose into a
bounded per-move delta; a term needing board-wide re-evaluation is
disqualified regardless of cost. This module never calls
:func:`precis.pcb.cost.evaluate_cost` per move. Per-term locality:

- ``layer_count``, ``via_count``, ``extended_part_fees``,
  ``thermal_rise``: invariant under slice-6 moves (read only
  ``seg_layer``/``net_plane_layers``/``inst_extended_part``/``net_class``,
  none touched by translate/rotate/swap) — evaluated once at
  construction. Slice 7 dirties both: :meth:`OptimizeEngine.
  _refresh_layer_count` (O(n_segments) full rescan — a whole-board
  aggregate, same carve-out as ``board_area``); :meth:`OptimizeEngine.
  _refresh_via_count_for_segment` (O(1) bounded per-segment delta — a via
  count depends only on its own segment's net/layer, see
  :func:`precis.pcb.rules.implied_via_count`).
- ``board_area``: cannot decompose locally (a bbox is a whole-board
  aggregate) — recomputed via :func:`~precis.pcb.cost.board_area_term`,
  O(n_instances), every move. Flagged rather than silently claimed
  compliant with the locality rule.
- ``gap_capacity``: the nearest-other-instance search
  (:func:`precis.pcb.ir.nearest_other_instance`) is O(n_instances) per
  segment, no spatial index. ``_seg_nearest_instance`` caches the last
  answer per segment; a moved instance ``M`` only invalidates segment
  ``s`` if (a) ``s`` is incident to ``M``, (b) ``s``'s cached nearest
  *was* ``M``, or (c) ``M``'s new position beats ``s``'s cached distance
  (one vectorized numpy comparison over all segments). Only flagged
  segments get a real re-search — exact, not approximate (see
  ``tests/test_pcb_optimize.py``'s delta-correctness property test).
- ``loop_inductance``: depends only on a segment's own endpoints —
  recompute only segments incident to the moved instance(s).
- ``coupling``: the candidate list
  (:func:`~precis.pcb.cost.coupling_candidates`) is position-independent,
  computed once; only pairwise proximity is re-scored, only for pairs
  naming a moved segment — bounded by candidate-list size ("dozens, not
  thousands"), never by segment count.
- ``crossings`` (:func:`precis.pcb.cost.crossings_term_for_layer`, backed
  by :func:`precis.pcb.ir.same_layer_crossing_count`'s geometric
  sweep-line count): ``_segments_by_layer`` (segment-membership index per
  layer) + ``_seg_crossing_partners`` (symmetric same-layer-crossing
  adjacency cache per segment) + ``_layer_crossing_count`` (running int,
  ``sum(len(partners)) / 2``, never recomputed from scratch).
  :meth:`OptimizeEngine._recompute_seg_crossings` is the bounded delta:
  for one touched segment, discard its cached partnerships (O(old
  partner count)), retest against every other currently-same-layer
  segment (O(layer size), never O(board)). Called once per segment
  incident to a moved instance for TRANSLATE/ROTATE/SWAP (inside
  :meth:`_rescan_after_move`) and once for a ``LAYER_ASSIGN`` move's
  segment. SIDE_FLIP and PIN_SWAP never move an instance centroid, so
  neither calls this — provably cost-neutral for ``crossings``.
- ``courtyard_overlap`` (gr267456): a uniform spatial grid
  (``_courtyard_grid``) buckets instance ids by ``floor(x / cell),
  floor(y / cell)`` with ``cell == _courtyard_cell_mm`` (``2*max(keep-out
  radius) + routing corridor``, floored at the nominal
  ``cost.COURTYARD_MIN_SEPARATION_MM``) — cell size at least the maximum
  pair interaction distance makes the 3x3-neighbourhood query
  (:meth:`OptimizeEngine._courtyard_candidates_near`) exact. The graded
  term reads the SAME pose-keyed world polygons ``_placement_is_legal``
  tests (passed through ``courtyard_overlap_pair_term``'s ``poly_a``/
  ``poly_b``), so steering and legality share one geometry.
  :meth:`OptimizeEngine._refresh_courtyard` is the bounded delta (discard
  cached partnerships, relocate in grid, retest 3x3 neighbourhood) —
  call-order-independent for a multi-instance move (SWAP). Only
  overlapping pairs are cached.
- ``alignment`` (2026-09-01, the first SUMMED "preference field" —
  ``docs/backlog/pcb-global-codesign-north-star.md`` invariant 3):
  piggybacks courtyard_overlap's OWN grid/3x3-neighbourhood query rather
  than a second spatial index — :func:`~precis.pcb.cost.
  alignment_pair_term`'s "nearby" gate is the SAME ``_courtyard_cell_mm``
  radius (see that function's and :func:`~precis.pcb.cost.
  _alignment_neighbourhood_mm`'s docstrings for why the two are an EXACT
  match, not merely overlapping). :meth:`OptimizeEngine.
  _refresh_alignment` mirrors :meth:`_refresh_courtyard`'s discard-then-
  rebuild shape exactly, called immediately after it at every call site
  (:meth:`_init_alignment_state`, :meth:`_rescan_after_move`) so it
  always sees ``inst``'s already-relocated grid cell — but keeps a
  RUNNING DOLLAR TOTAL (``_money_static_by_name["alignment"]``, the same
  shape :meth:`_refresh_via_count_for_segment` already uses for
  ``via_count``) rather than a per-pair margin survivor, since a MONEY
  term sums instead of max-aggregating.
- ``board_edge_clearance`` (gr267456): depends only on the moved instance
  + ``ir.outline`` — O(1) delta, no grid: :meth:`OptimizeEngine.
  _refresh_board_edge` re-evaluates
  :func:`~precis.pcb.cost.board_edge_clearance_term` for that instance.
  When ``ir.outline`` exists, ``_placement_bounds`` (via
  :func:`~precis.pcb.cost.outline_bbox`, the same approximation the cost
  term uses) is the seed extent padded by ``board_side``, CLIPPED to the
  outline bbox inset by :data:`_EDGE_MARGIN_MM` — the outline caps the
  domain, it never expands it. Handing the anneal the full bbox of an
  oversized/placeholder outline was tried twice and reverted twice: it
  once quadrupled DRC errors on the ESP32-C3 reference fixture, and on
  2026-08-31 (paired with a centred seed) it flipped two of that
  fixture's five seeds to ``no_path`` — the cooling schedule
  (``board_side``-derived, not outline-aware, see
  :meth:`OptimizeEngine.__init__`) lets a component drift too far to
  walk back once hardened. The "parts cluster in one corner quadrant"
  visual finding is solved downstream instead: :func:`recentre_in_outline`
  rigidly translates the FINISHED placement to the outline's centre
  (routing is translation-invariant); a spread-to-FILL pressure, if ever
  wanted, belongs in the cost function, not in this domain.
- ``measures`` (2026-09-03, author-supplied placement intent —
  ``put(args={'measures':[...]})``, precis-measures-help): a
  ``proximity``/``separation`` measure names exactly two instances and a
  distance bound, so unlike ``alignment``'s spatial-grid-discovered
  neighbours the pair is FIXED at construction (:meth:`OptimizeEngine.
  __init__` resolves ``config.measures`` once, into
  ``_measures_by_inst[inst]`` — the small, static list of measures naming
  ``inst``) and needs no candidate search at all: :meth:`OptimizeEngine.
  _refresh_measures` just recomputes those, O(measures on this instance),
  every move. A SUMMED (``Family.MONEY``-shaped, though not a registered
  ``cost.py`` :class:`~precis.pcb.cost.TermSpec` — see
  :data:`_MEASURE_SOFT_USD_PER_MM`'s docstring for why) running total,
  the same running-dollar shape ``alignment`` uses, kept in its own
  ``_money_measures`` channel. Never a legality rejection: the penalty is
  linear in violation distance with no ceiling, so a badly-seeded pair
  always has a gradient walking it toward its goal, at every schedule
  stage.

**Not claimed local**: :func:`precis.pcb.cost.aggregate_margin`'s max is a
linear scan over cached per-item penalties, O(cached entries) not
O(touched) — fine at this slice's scale (hundreds of entries); a
lazy-deletion max-heap is the production-scale fix, not built.

**Constraint hardening is the schedule**: this engine drives
:func:`precis.pcb.cost.hardened_penalty`'s ``schedule`` linearly 0
(exploratory) → 1 (barrier) over the anneal's iteration count — no second
hardening mechanism.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from precis.format import toon
from precis.pcb import pinswap
from precis.pcb.cost import (
    _BY_NAME,
    _CRITICALITY_WEIGHT,
    COURTYARD_MIN_SEPARATION_MM,
    CostConfig,
    Family,
    TermValue,
    alignment_pair_term,
    board_area_term,
    board_edge_clearance_term,
    coupling_candidates,
    coupling_pair_term,
    courtyard_overlap_pair_term,
    evaluate_cost,
    gap_capacity_term,
    hardened_penalty,
    layer_count_term,
    loop_inductance_term,
    outline_bbox,
)
from precis.pcb.eyes import measure_bound
from precis.pcb.geom import (
    convex_polygons_overlap,
    convex_polygons_signed_separation,
    segments_cross,
)
from precis.pcb.ir import (
    COURTYARD_CLEARANCE_MM,
    UNSET_LAYER,
    Level,
    MountingHole,
    PcbIR,
    courtyard_bound_radius_mm,
    instance_courtyard_polygons,
    nearest_other_instance,
    plane_layers_of,
    pourable_layers,
    routable_layers,
    segment_points,
)
from precis.pcb.landpattern import place_points, rotate_offset
from precis.pcb.pinswap import PinSwapGroup
from precis.pcb.rules import implied_via_count

# ── move set (slice 6: placement only) ──────────────────────────────────


class MoveKind(Enum):
    """The move set. Slice 6 shipped the first three (placement only);
    slice 7 adds the rest — topology (side flip), layer assignment, plane
    role, and pin swap — registering their generators in
    :data:`MOVE_GENERATORS`. The engine (state, cost plumbing, SA loop)
    is unchanged in shape; :class:`Move` grows a few kind-specific
    optional fields (see its docstring) rather than the module being
    restructured."""

    TRANSLATE = "translate"
    ROTATE = "rotate"
    SWAP = "swap"
    LAYER_ASSIGN = "layer_assign"
    SIDE_FLIP = "side_flip"
    PLANE_PROMOTE = "plane_promote"
    PLANE_DEMOTE = "plane_demote"
    PIN_SWAP = "pin_swap"


@dataclass(frozen=True, slots=True)
class Move:
    """One proposed move. A pure data record — :meth:`OptimizeEngine.
    apply_move` / :meth:`OptimizeEngine.undo_move` are what actually
    mutate state, so a rejected move costs exactly one more application of
    the same (exact, bounded) update pipeline, never a special-cased
    rollback path.

    Different :class:`MoveKind`\\ s populate different fields — a tagged
    union via optional defaults, not one shape stretched to fit
    everything:

    - ``TRANSLATE``/``ROTATE``/``SWAP`` (slice 6): ``instances`` + the
      ``(x, y, rot)`` triples in ``old``/``new``. ``instances`` is a whole
      rigid body when the picked instance names a ``"group"``/``"pattern"``
      (:meth:`OptimizeEngine._rigid_members`) — every member moves in the
      SAME move, never a follow-up one, which is what keeps apply/undo
      atomic for a group. **SWAP is restricted for a grouped instance**:
      two ungrouped instances swap freely (unchanged since slice 6); two
      DIFFERENT groups of the SAME pattern (congruent tiles) may swap
      anchors; every other combination (grouped<->ungrouped, two
      unrelated groups, or a group with itself) is refused by
      :func:`_gen_swap` before it ever reaches legality — a mixed swap
      isn't merely unlikely to fit, this design doesn't admit it at all.
    - ``LAYER_ASSIGN``/``SIDE_FLIP``: ``segments`` (one segment id) +
      ``old_int``/``new_int`` (one layer index / side value).
    - ``PLANE_PROMOTE``/``PLANE_DEMOTE``: ``net`` + ``old_int``/``new_int``
      (the vacated/assigned plane layer — empty tuple when there was/is
      none, i.e. a promote's ``old_int`` and a demote's ``new_int``).
    - ``PIN_SWAP``: ``pin_pairs`` — a sequence of
      :meth:`precis.pcb.ir.PcbIR.swap_pins` calls (a fixed-pivot cycle
      decomposition, see :mod:`precis.pcb.pinswap`) that together realize
      the proposed reassignment; applying the same pairs in REVERSE order
      undoes it (each individual swap is self-inverse; reversing a
      composed sequence of invertible ops always undoes it, regardless of
      whether the ops commute).
    """

    kind: MoveKind
    instances: tuple[int, ...] = ()
    old: tuple[tuple[float, float, float], ...] = ()
    new: tuple[tuple[float, float, float], ...] = ()
    segments: tuple[int, ...] = ()
    old_int: tuple[int, ...] = ()
    new_int: tuple[int, ...] = ()
    net: int | None = None
    pin_pairs: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class ScheduleStage:
    """One stretch of the anneal with a fixed move-kind weight mix — "move
    mix is a schedule, not an architecture" (backlog). Active while the
    fraction of iterations completed so far is ``<= through_fraction``.
    Slice 6 ships one stage covering the whole run; slice 7 appends more
    stages (e.g. topology moves entering mid-schedule) without touching
    the SA loop that reads this."""

    through_fraction: float
    weights: dict[MoveKind, float]


#: **Move mix is a schedule** (backlog, verbatim): placement-dominant
#: early (there is no sketch to refine yet), layer assignment entering
#: mid-schedule (NOT at move zero — while placement is still making large
#: moves, an assigned layer's gap/congestion story is stale before the
#: component has even settled near its final neighbourhood), topology
#: polish (side flips, plane role, pin swap) late, once placement is
#: near-frozen and the remaining wins are combinatorial. Three stages,
#: `through_fraction` gating which is "active":
DEFAULT_SCHEDULE: tuple[ScheduleStage, ...] = (
    # 0% - 50%: placement only, same mix slice 6 shipped. No LAYER_ASSIGN/
    # SIDE_FLIP/PLANE_*/PIN_SWAP entries at all in this stage -- the
    # schedule enforces "not at move zero" by simple absence, not a
    # near-zero weight that could still fire early by chance.
    ScheduleStage(
        0.5,
        {MoveKind.TRANSLATE: 0.6, MoveKind.ROTATE: 0.15, MoveKind.SWAP: 0.25},
    ),
    # 50% - 85%: layer assignment enters; placement still carries most of
    # the weight (components are still refining position) but topology
    # (side flip, plane role) starts contributing too.
    ScheduleStage(
        0.85,
        {
            MoveKind.TRANSLATE: 0.35,
            MoveKind.ROTATE: 0.05,
            MoveKind.SWAP: 0.15,
            MoveKind.LAYER_ASSIGN: 0.2,
            MoveKind.SIDE_FLIP: 0.15,
            MoveKind.PLANE_PROMOTE: 0.05,
            MoveKind.PLANE_DEMOTE: 0.05,
        },
    ),
    # 85% - 100%: topology-dominant polish. Placement moves shrink to a
    # minority (fine-tuning only); pin swap -- the most expensive-to-
    # evaluate move (a per-instance min-cost matching) and the one whose
    # payoff depends on a nearly-settled placement to even be measured
    # meaningfully -- joins only here.
    ScheduleStage(
        1.0,
        {
            MoveKind.TRANSLATE: 0.1,
            MoveKind.SIDE_FLIP: 0.3,
            MoveKind.LAYER_ASSIGN: 0.25,
            MoveKind.PIN_SWAP: 0.2,
            MoveKind.PLANE_PROMOTE: 0.075,
            MoveKind.PLANE_DEMOTE: 0.075,
        },
    ),
)

#: **ROTATE is cost-neutral, provably** — no term in ``cost.py`` reads
#: ``inst_rot`` (component-centroid granularity, same limitation
#: ``place.py`` documents), so its ``total()`` delta is a true zero. Still
#: exercised (dirty cascade honoured, `fixed='rot'` respected) so the
#: registry/schedule/delta plumbing is ready for a future per-pin-offset
#: term (the data :func:`precis.pcb.pinswap.offsets_from_pads` already
#: wires through for PIN_SWAP).
#:
#: **SIDE_FLIP is cost-neutral too**, for the same reason: ``crossings``
#: (:func:`precis.pcb.ir.same_layer_crossing_count`) reads segment
#: endpoints at INSTANCE-centroid granularity, and ``seg_side`` (which
#: side of an obstacle a connection routes — needed by the realizer's
#: arcs/tangents) never perturbs an instance's own (x, y). Blind until
#: sub-instance geometry or realize.py's face tracing exists. Still
#: exercised (dirty cascade honoured) for the same future-readiness
#: reason.
#:
#: LAYER_ASSIGN and PLANE_PROMOTE/DEMOTE are NOT cost-neutral: LAYER_
#: ASSIGN's ``layer_count`` money term AND ``crossings`` margin term
#: respond to it; PLANE_PROMOTE/DEMOTE's ``layer_count`` (via
#: ``net_plane_layers``) AND ``gap_capacity`` (the plane-exclusion branch)
#: respond to those moves — see :meth:`OptimizeEngine._refresh_layer_count`,
#: :meth:`OptimizeEngine._recompute_seg_crossings` and
#: :func:`precis.pcb.cost.gap_capacity_term`. PIN_SWAP's effect is real
#: but measured entirely by :mod:`precis.pcb.pinswap`'s own crossing
#: evaluator (over real per-pin geometry via
#: :func:`precis.pcb.pinswap.offsets_from_pads` when supplied), never by
#: ``total()`` — no ``cost.py`` term reads pin identity or sub-instance
#: pad position.


#: Linear USD-per-mm-of-violation scale for a `soft` measure — a gradient
#: toward the authored goal, NOT a fab-cost figure. Deliberately NOT a
#: registered `cost.py` :class:`~precis.pcb.cost.TermSpec` (unlike
#: `alignment`, this module's other MONEY "preference field"): an authored
#: `pcb_measures` row is per-DESIGN intent that lives outside `PcbIR`/
#: `CostConfig`'s board-physics catalogue entirely (it isn't even data the
#: IR carries), so it rides its OWN small money channel here
#: (:attr:`OptimizeEngine._money_measures`, folded into :meth:`OptimizeEngine.
#: money` directly) rather than joining `_money_static_by_name`'s
#: `_BY_NAME`-keyed terms — see :meth:`OptimizeEngine.digest`'s own
#: hand-built `TermSummary` for this term for the same reason. Set an
#: order of magnitude above `CostConfig.alignment_usd_per_pair` (0.002, the
#: deliberately-weakest possible cosmetic tie-breaker) since an AUTHORED
#: measure is a real stated intent, not a tie-breaker — but still well
#: below a single via (`via_usd` 0.02) or a layer (`layer_usd` 5.0) at a
#: typical few-mm violation, so a `soft` measure trades off against real
#: fab cost rather than steamrolling it.
#: Measured empirically (a two-free-instance anneal, no other cost term in
#: play, `tests/test_pcb_optimize.py`'s own measure tests): the schedule's
#: cooling temperature starts at `board_side / 2` (`OptimizeEngine.anneal`)
#: and a per-pair-unit scale near `CostConfig.alignment_usd_per_pair`
#: (0.002) left the acceptance-probability gradient too weak against that
#: temperature to reliably close more than a few mm per run — this value
#: reliably lands a soft-measured pair within a couple mm of `goal_mm`.
_MEASURE_SOFT_USD_PER_MM = 1.0

#: `hard` measures get a DECISIVELY larger per-mm scale than `soft` — 40x —
#: so a hard violation dominates any real money term this engine ever
#: prices (one mm of hard violation already outweighs eight whole extra
#: layers), without ever being a legality REJECTION (task requirement,
#: verbatim): the anneal always has a slope to walk a violating seed back
#: in, it is just very rarely willing to accept a move that walks further
#: out.
_MEASURE_HARD_USD_PER_MM = 40.0

#: :meth:`OptimizeEngine.digest`'s hand-built justification for the
#: "measures" row — there is no `cost.py` :class:`~precis.pcb.cost.TermSpec`
#: to read one from (:data:`_MEASURE_SOFT_USD_PER_MM`'s own docstring), so
#: this is the one place that text lives.
_MEASURES_JUSTIFICATION = (
    "author-stated placement intent (put(args={'measures':[...]})) — a "
    "proximity/separation goal between two named instances, priced as a "
    "linear-in-violation-mm penalty so the anneal steers toward it; "
    "'hard' measures are priced decisively above 'soft' ones, never as a "
    "legality rejection (precis-measures-help)"
)


@dataclass(frozen=True, slots=True)
class MeasureSpec:
    """One placement-drivable `pcb_measures` row, pre-resolved to the
    shape this engine can enforce as a bounded pair-distance cost term —
    NOT the raw ``{'metric', 'operands', ...}`` dict
    ``store.pcb_measures_list`` returns (:func:`resolve_measures` converts
    one into the other). Only `proximity`/`separation` measures over
    exactly two ``{'instance': refdes}`` operands resolve to one of these:
    `height` measures are `eyes.py`-evaluated only (not a pair-distance
    bound), and role-based (``{'role': ...}``) operands can't resolve here
    at all — bare :class:`~precis.pcb.ir.PcbIR` carries no role data (that
    lives only on the graph dict `store.pcb_graph` hands `eyes.py`/
    `place.py`, never on the IR this engine anneals), so a role-based
    measure is still evaluated read-only (``get(view='measures')``) but
    does not steer this module's placement — a documented gap, not a
    silent one.

    ``refdes_a``/``refdes_b`` are resolved to instance ids at
    :meth:`OptimizeEngine.__init__` time (a design's refdes are stable
    strings; instance ids are engine-internal), so this dataclass itself
    stays engine-agnostic and easy to unit-test standalone."""

    refdes_a: str
    refdes_b: str
    bound: str  # "lower" | "upper" | "target" — precis.pcb.eyes.measure_bound
    goal_mm: float
    hard: bool
    weight: float


def resolve_measures(measures: list[dict[str, Any]] | None) -> tuple[MeasureSpec, ...]:
    """Raw `pcb_measures` rows (`store.pcb_measures_list`'s own shape) ->
    the pair-distance bounds this engine can enforce. Mirrors
    :func:`precis.pcb.place._measure_specs`'s own filtering — skip
    `strength='gauge'` (report-only, never drives placement per
    precis-measures-help, verbatim); skip a metric other than proximity/
    separation; skip a missing goal — but resolves ONLY
    ``{'instance': refdes}`` operand pairs (see :class:`MeasureSpec`'s
    docstring for why role operands can't resolve at this layer) and keeps
    EXACTLY two distinct refdes — a measure naming more or fewer operands
    doesn't name a pair-distance bound this engine's move-local delta
    machinery can express."""
    out: list[MeasureSpec] = []
    for m in measures or []:
        strength = str(m.get("strength") or "gauge").strip().lower()
        if strength == "gauge":
            continue
        metric = str(m.get("metric") or "").strip().lower()
        if metric not in ("proximity", "separation"):
            continue
        goal = m.get("goal")
        if goal is None:
            continue
        refs = [
            str(op["instance"])
            for op in (m.get("operands") or [])
            if isinstance(op, dict) and "instance" in op
        ]
        if len(refs) != 2 or refs[0] == refs[1]:
            continue
        weight = 1.0 if m.get("weight") is None else float(m["weight"])
        out.append(
            MeasureSpec(
                refdes_a=refs[0],
                refdes_b=refs[1],
                bound=measure_bound(m.get("direction"), metric),
                goal_mm=float(goal),
                hard=(strength == "hard"),
                weight=weight,
            )
        )
    return tuple(out)


def _measure_pair_usd(ir: PcbIR, ia: int, ib: int, spec: MeasureSpec) -> float:
    """One measure's current USD contribution — 0.0 when its bound is
    satisfied, growing LINEARLY with the violation distance (the same
    linear-in-fraction shape :func:`~precis.pcb.cost.alignment_pair_term`
    uses for this module's other summed preference term), scaled by
    :data:`_MEASURE_HARD_USD_PER_MM`/:data:`_MEASURE_SOFT_USD_PER_MM` and
    the measure's own author-supplied ``weight`` (``weight: 0`` records a
    measure without letting it steer placement, same convention
    :func:`precis.pcb.place._measure_specs` documents). Never a legality
    rejection — a violating pair always keeps a nonzero gradient pulling
    it back toward ``goal_mm``, at every distance, so a badly-seeded pair
    can always walk in."""
    xa, ya = float(ir.inst_x[ia]), float(ir.inst_y[ia])
    xb, yb = float(ir.inst_x[ib]), float(ir.inst_y[ib])
    if math.isnan(xa) or math.isnan(ya) or math.isnan(xb) or math.isnan(yb):
        return 0.0
    dist_mm = math.hypot(xa - xb, ya - yb)
    if spec.bound == "lower":  # separation: keep apart, penalise being too close
        violation_mm = max(0.0, spec.goal_mm - dist_mm)
    elif spec.bound == "upper":  # proximity: keep close, penalise being too far
        violation_mm = max(0.0, dist_mm - spec.goal_mm)
    else:  # target: aim at goal_mm from either side
        violation_mm = abs(dist_mm - spec.goal_mm)
    if violation_mm <= 0.0:
        return 0.0
    scale = _MEASURE_HARD_USD_PER_MM if spec.hard else _MEASURE_SOFT_USD_PER_MM
    return spec.weight * scale * violation_mm


@dataclass(frozen=True, slots=True)
class OptimizeConfig:
    """Every tunable this module needs. ``cost`` is passed straight to the
    per-item ``cost.py`` evaluators; its own ``schedule`` field is ignored
    here — this engine drives hardening itself (see module docstring) —
    and its ``p_norm`` must stay ``None`` (exact max), since a soft p-norm
    is not decomposable into a bounded per-move delta the way a plain max
    is (a p-norm changes with EVERY cached value's relative weight, not
    just the peak)."""

    cost: CostConfig = field(default_factory=CostConfig)
    iters: int = 2000
    seed: int = 0
    t0: float | None = None  # None => derived from board scale
    cooling: float = 0.995
    translate_step_mm: float = 6.0
    gap_pitch_mm: float = 0.3
    region_cell_mm: float = 5.0
    max_cluster_size: int = 6
    seed_pitch_mm: float = 8.0
    schedule: tuple[ScheduleStage, ...] = DEFAULT_SCHEDULE
    #: Caller-supplied pin-swap admissible sets (:mod:`precis.pcb.pinswap`).
    #: Empty by default — "where the equivalence data doesn't exist yet,
    #: degrade cleanly (no swaps)" (task instruction, verbatim): with no
    #: groups, :func:`_gen_pin_swap` always returns ``None`` and PIN_SWAP
    #: never fires, regardless of its schedule weight. This module never
    #: invents an equivalence class from a part number or footprint shape.
    pin_swap_groups: tuple[PinSwapGroup, ...] = ()
    #: Net ids whose plane assignment a human AUTHORED (``ir.promote_plane``
    #: called by something other than this search) rather than one this
    #: search derived. A declaration is a constraint, not a hint: when a
    #: caller says "GND is the plane on In1.Cu" it is supplying exactly the
    #: domain judgment the search cannot derive on its own, so
    #: :func:`_gen_plane_demote` must never offer a locked net and
    #: :func:`_gen_plane_promote` must never move one to a different layer
    #: or treat its own plane layer as free for another net.
    #:
    #: Without this, every authored plane assignment was silently reversed:
    #: this cost model has already been measured to dislike planes (79
    #: ``PLANE_PROMOTE`` proposals over 3000 iterations, all rejected on
    #: cost — see the module docstring's PLANE_PROMOTE/DEMOTE note), so the
    #: mirror fact was inevitable — every ``PLANE_DEMOTE`` on an authored
    #: net was accepted immediately and permanently, because nothing
    #: distinguished "the search's own exploration" from "the one thing the
    #: caller told it not to explore away from". Realization runs on the
    #: post-anneal IR, not the persisted authored row, so that demotion was
    #: never a mere excursion — it was the final, shipped answer. Empty by
    #: default: an optimizer-DERIVED plane assignment stays fully
    #: explorable, only a human's stays fixed.
    locked_plane_nets: frozenset[int] = frozenset()
    #: Author-supplied placement measures (``put(args={'measures':[...]})``,
    #: see precis-measures-help) as pre-resolved :class:`MeasureSpec`\\ s —
    #: :func:`resolve_measures` is the caller-side conversion from
    #: ``store.pcb_measures_list``'s raw dicts, mirroring
    #: :attr:`pin_swap_groups`'s own "caller resolves, engine consumes"
    #: shape. Empty by default: with no measures, :meth:`OptimizeEngine.
    #: __init__` resolves an empty pair list, ``_money_measures`` stays 0.0
    #: for the WHOLE anneal, and every move/accept/reject decision is
    #: bit-for-bit identical to a run built before this field existed — the
    #: measure machinery adds a cost term, never a new code path an
    #: unrelated design's anneal has to pass through.
    measures: tuple[MeasureSpec, ...] = ()

    def __post_init__(self) -> None:
        if self.cost.p_norm is not None:
            raise ValueError(
                "OptimizeConfig.cost.p_norm must be None (exact max) — a "
                "soft p-norm margin aggregate is not locally decomposable "
                "(see module docstring)"
            )


# ── digest shapes ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TermSummary:
    name: str
    family: str  # "money" | "margin"
    value: float  # money: summed raw USD. margin: peak criticality-weighted, hardened penalty.
    peak_region: str | None  # margin terms with a spatial home only
    justification: str


@dataclass(frozen=True, slots=True)
class RegionEntry:
    """One spatial grid cell's worst-binding margin story — the "peak
    congestion in region C3, driven by these six nets, blocked by two
    locked parts" digest the backlog requires."""

    region: str
    peak_term: str
    peak_penalty: float
    nets: tuple[str, ...]
    locked_instances: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MoveRecord:
    iteration: int
    kind: str
    instances: tuple[str, ...]
    accepted: bool
    delta: float


@dataclass(frozen=True, slots=True)
class Digest:
    total: float
    money: float
    risk: float
    terms: tuple[TermSummary, ...]
    regions: tuple[RegionEntry, ...]  # worst-first
    move_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class OptimizeResult:
    seed: int
    iters: int
    cost_before: float
    cost_after: float
    digest: Digest
    moves: tuple[MoveRecord, ...]
    positions: dict[str, tuple[float, float, float]]  # refdes -> (x, y, rot)


# ── constructive seed: connectivity clustering + cluster drop ───────────


def _cluster_instances(ir: PcbIR, *, max_cluster_size: int) -> list[list[int]]:
    """Greedy edge-weight clustering (a simplified Kruskal): sort every
    instance-pair's shared-segment count descending, union-find-merge the
    heaviest edges first, refusing a merge that would exceed
    ``max_cluster_size``. O(E log E) — a one-time seed cost, not part of
    the per-move locality budget above (the seed runs once before the SA
    loop starts, same as any constructive-placement seed)."""
    n = ir.n_instances
    parent = list(range(n))
    size = [1] * n

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edge_weight: dict[tuple[int, int], int] = {}
    for s in range(ir.n_segments):
        a = int(ir.pin_instance[int(ir.seg_pin_a[s])])
        b = int(ir.pin_instance[int(ir.seg_pin_b[s])])
        if a == b:
            continue
        key = (a, b) if a < b else (b, a)
        edge_weight[key] = edge_weight.get(key, 0) + 1

    edges = sorted(edge_weight.items(), key=lambda kv: (-kv[1], kv[0]))
    for (a, b), _w in edges:
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        if size[ra] + size[rb] > max_cluster_size:
            continue
        parent[rb] = ra
        size[ra] += size[rb]

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    return list(clusters.values())


def _merge_pattern_clusters(ir: PcbIR, clusters: list[list[int]]) -> list[list[int]]:
    """Union-merge any :func:`_cluster_instances` clusters that share a
    PATTERN-group member — a real, measured defect
    (docs/backlog/pcb-review-round4-0901.md's nano fixture repro, 2026-09):
    connectivity clustering has no notion of "pattern" membership, and a
    tile's genuine netlist edges (say, a diode-to-transistor-to-connector
    star) can lose the GREEDY heaviest-edge-first union to an unrelated,
    busier hub elsewhere on the board — measured on the nano fixture's own
    four-part "channel" pattern, whose members landed in THREE different
    clusters (one absorbed into a 6-part power cluster around a header,
    one alone, one in a passives cluster), tens of millimetres apart.

    That is fatal once the anneal starts: :meth:`OptimizeEngine.
    _rigid_members` moves every member of a group in ONE proposal sharing
    ONE small delta (:func:`~precis.pcb.optimize._gen_translate`'s own
    "the SAME delta for every member" contract), so a tile spanning tens
    of millimetres can never find a legal move at all — every proposal
    either leaves the far member stranded outside `bounds_for` or
    collides with whatever the shelf pack put in its way, so the group is
    frozen at whatever mismatched, useless clusters happened to seed it
    at (observed: several tiles seeded 25-40mm past the board's own
    outline, permanently — DRC's `outline_containment` tally alone
    reached 78 on that run).

    Merging clusters is deliberately NOT the same as widening
    :data:`_cluster_instances`'s own ``max_cluster_size`` cap: a pattern
    member unconditionally drags its WHOLE cluster along regardless of
    that cap (a rigid group is a hard requirement, not a size preference
    the connectivity clusterer is free to trade off), while every OTHER
    clustering decision on the board — for a design with no patterns at
    all, or for the members that ARE NOT in a pattern — is completely
    unaffected: this is a no-op whenever no two returned clusters share a
    pattern-group member."""
    if not ir.n_groups:
        return clusters
    cluster_of_inst: dict[int, int] = {}
    for ci, members in enumerate(clusters):
        for inst in members:
            cluster_of_inst[inst] = ci

    parent = list(range(len(clusters)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pattern_members: dict[int, list[int]] = {}
    for i in range(ir.n_instances):
        gid = int(ir.inst_group[i])
        if gid >= 0 and ir.group_pattern[gid] is not None:
            pattern_members.setdefault(gid, []).append(i)

    for members in pattern_members.values():
        cis = [cluster_of_inst[m] for m in members]
        root = find(cis[0])
        for ci in cis[1:]:
            other = find(ci)
            if other != root:
                parent[other] = root

    merged: dict[int, list[int]] = {}
    for ci, members in enumerate(clusters):
        merged.setdefault(find(ci), []).extend(members)
    return list(merged.values())


def seed_placement(
    ir: PcbIR,
    rng: random.Random,
    *,
    max_cluster_size: int = 6,
    pitch_mm: float = 8.0,
    force: bool = False,
) -> None:
    """The constructive seed: connectivity clustering (above), then
    **shelf packing by each part's own size** — clusters are laid out in
    order (biggest cluster first, so adjacency survives), each instance
    taking exactly the room its land pattern needs, wrapping to a new
    shelf at ``row_width``. Adjacency-aware, unlike a uniform random
    scatter: instances that share nets start out near each other, which
    is what makes the SA refiner's job tractable.

    **Why not a fixed ``pitch_mm`` grid.** It was one, and the grid
    spacing has to be sized for the LARGEST part or that part overlaps
    its neighbours. On a typical board — one module and 28 passives —
    that means every 0.6mm capacitor is allotted the same cell as an
    18mm module: this fixture's 29 instances seeded across 148mm of board
    for ~30mm of actual parts. That is not merely untidy. The router
    grids the pad bounding box under a fixed cell budget, so a seed 5x too
    large makes the routing grid 5x too coarse to resolve the 0.65mm pad
    pitch it has to escape from, and the anneal cannot walk it back
    inside a fixed iteration budget. ``pitch_mm`` is now the *minimum*
    footprint an instance is allotted, not the spacing.

    The packing is legal by construction under
    :meth:`OptimizeEngine._placement_is_legal`: within a shelf, adjacent
    centres are exactly ``r_i + r_j`` apart, and successive shelves are
    separated by the taller shelf's full height.

    **Corner-anchored on purpose — centering happens AFTER the anneal.**
    A centred seed was tried (2026-08-31) and reverted the same day: the
    anneal is not translation-invariant (its domain is clipped to the
    outline, so a corner-anchored seed anneals against a wall a centred
    one does not have), and moving the seed changed two of the esp32c3
    reference fixture's five seeds enough that a net came back
    ``no_path``. The board still ends up visually centred:
    :func:`recentre_in_outline` translates the FINISHED placement as one
    rigid block, which routing — translation-invariant, grid origin
    derived from the pad bbox — never notices.

    ``force=False`` (the default, "re-anneal from current state" — the
    LLM's lever per the backlog) only seeds instances with no position yet
    (``nan``); already-placed instances keep their coordinates.
    ``force=True`` reseeds everyone. Either way, every mutation goes
    through :meth:`PcbIR.move_instance` — never a direct array write —
    honouring the "only sanctioned way to change state" contract
    ``ir.py`` documents.

    **Mounting holes are NOT avoided here.** This function is "legal by
    construction" only against OTHER instances (the docstring above); it
    does not steer the shelf pack around a hole the way it steers around
    a neighbour. Chosen deliberately over the alternative (folding holes
    into the packer as more obstacles to route the shelf around) because
    legality is already a hard backstop — :meth:`OptimizeEngine.
    _placement_is_legal` rejects any TRANSLATE/ROTATE/SWAP that would put
    a courtyard ON a hole — and the graded ``courtyard_overlap`` pressure
    :meth:`OptimizeEngine._refresh_courtyard` now folds in for a hole
    gives the anneal a real slope to walk a badly-seeded part off one,
    the same way it already resolves an ordinary seed-time instance
    overlap. A part that happens to seed on a hole is a transient, cost-
    priced state for a few early iterations, not a stuck one.

    **Rigid groups seed as ONE unit.** An AUTHORED group (``"group"``/
    ``"group_offset"`` — see :attr:`~precis.pcb.ir.PcbIR.inst_group`'s own
    docstring) packs as a single shelf entry sized to circumscribe every
    member's own courtyard at its authored offset, then every member is
    placed at ``anchor + rotate(offset, anchor_rot)`` — never seeded
    individually. A PATTERN group (auto-derived, no authored offset)
    seeds its members individually, exactly like an ungrouped instance,
    and only afterwards has its internal layout forced to match
    pattern_instance 0's (:func:`_stamp_pattern_tiles`, called at the end
    of this function) — see that function's own docstring for why a
    pattern can't be seeded from an anchor the way an authored group is.
    """
    clusters = _cluster_instances(ir, max_cluster_size=max_cluster_size)
    clusters = _merge_pattern_clusters(ir, clusters)
    clusters.sort(key=len, reverse=True)
    # The SAME radius `_placement_is_legal`'s broad phase uses, derived
    # from the same courtyard polygons — a shelf pack needs one scalar
    # footprint per part, and it must be the conservative circumscribed
    # one. Seeding to a tighter figure than legality enforces produces a
    # seed the annealer can never repair: `bounds_for` clamps the
    # TRANSLATE that would rescue a crowded part while
    # `_placement_is_legal` rejects the crowding, so it stays illegal for
    # the whole run (this function's own docstring, "legal by
    # construction").
    radii = np.maximum(
        courtyard_bound_radius_mm(
            instance_courtyard_polygons(
                ir,
                clearance_mm=COURTYARD_CLEARANCE_MM,
                fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
            )
        ),
        max(COURTYARD_MIN_SEPARATION_MM / 2.0, pitch_mm / 8.0),
    )
    min_radius = max(COURTYARD_MIN_SEPARATION_MM / 2.0, pitch_mm / 8.0)

    # ── unit resolution: an AUTHORED group's members pack as ONE shelf
    # entry (below); everyone else (ungrouped instances, and a PATTERN
    # group's members — see this function's own docstring) packs
    # individually, exactly as before groups existed at all.
    # Pre-sized by `ir.n_groups` rather than grown ad hoc: every DECLARED
    # group id gets an entry (even the pathological empty one), which is
    # what lets the loops below key off `group_members.items()` without a
    # separate "does this id even exist" check.
    group_members: dict[int, list[int]] = {g: [] for g in range(ir.n_groups)}
    for i in range(ir.n_instances):
        gid = int(ir.inst_group[i])
        if gid >= 0:
            group_members[gid].append(i)

    def _is_authored_group(gid: int) -> bool:
        return ir.group_pattern[gid] is None

    unit_of_instance: dict[int, int | tuple[str, int]] = {}
    unit_radius: dict[int | tuple[str, int], float] = {}
    for i in range(ir.n_instances):
        gid = int(ir.inst_group[i])
        if gid >= 0 and _is_authored_group(gid):
            unit_of_instance[i] = ("group", gid)
        else:
            unit_of_instance[i] = i
            unit_radius[i] = float(radii[i])
    #: gid -> (local_cx, local_cy), the group's own OFFSET-SPACE centroid
    #: (the mean of every member's authored ``group_offset``) — the point
    #: :attr:`unit_radius`'s circumscribed figure is measured FROM, and
    #: the point the placement pass below maps onto the packed WORLD
    #: position, mirroring exactly how a plain instance's own
    #: ``courtyard_bound_radius_mm`` is a circle centred on ITS OWN
    #: centroid (never on an arbitrary corner of its footprint).
    group_local_centroid: dict[int, tuple[float, float]] = {}
    for gid, members in group_members.items():
        if not _is_authored_group(gid):
            continue
        offsets = []
        for m in members:
            dx = float(ir.inst_group_offset_dx[m])
            dy = float(ir.inst_group_offset_dy[m])
            dx = 0.0 if math.isnan(dx) else dx
            dy = 0.0 if math.isnan(dy) else dy
            offsets.append((m, dx, dy))
        local_cx = sum(dx for _m, dx, _dy in offsets) / len(offsets)
        local_cy = sum(dy for _m, _dx, dy in offsets) / len(offsets)
        group_local_centroid[gid] = (local_cx, local_cy)
        # The group's own circumscribed radius, from its own CENTROID —
        # every member's own keep-out radius plus its authored distance
        # from THAT centroid, worst case over members. Measured from the
        # centroid rather than the anchor (offset (0, 0)) on purpose: an
        # anchor sits at one END of an elongated multi-member assembly
        # (the nano fixture's own two 15-pin headers, J1 at the anchor,
        # J2 15.24mm off it), so a circle centred there has to reach all
        # the way across BOTH members' own extents to cover them —
        # measured on that exact fixture, an anchor-centred radius came
        # out to 34mm (bigger than the WHOLE 62mm-wide board), which
        # broke the row-width clamp below outright (nothing is `>`
        # ``2 * max_unit_radius`` once one unit alone is that big) and
        # produced a shelf pack that placed the group tens of millimetres
        # past the outline — the "J1 crosses the top edge" defect. The
        # SAME assembly measured from its own centroid comes out to
        # ~26mm: still large (a two-15-pin-header assembly IS large), but
        # small enough that the clamp fires and the group packs inside
        # the board it can (in principle) fit on.
        r = min_radius
        for m, dx, dy in offsets:
            r = max(r, math.hypot(dx - local_cx, dy - local_cy) + float(radii[m]))
        unit_radius[("group", gid)] = r

    order_units: list[int | tuple[str, int]] = []
    seen_units: set[int | tuple[str, int]] = set()
    for members in clusters:
        for inst in members:
            unit = unit_of_instance[inst]
            if unit in seen_units:
                continue
            seen_units.add(unit)
            order_units.append(unit)

    # A square-ish shelf region: total footprint area, with slack for the
    # wrap waste an unsorted (adjacency-ordered, not size-ordered) shelf
    # pack leaves behind.
    max_unit_radius = max(unit_radius.values(), default=0.0)
    total_area = float(sum((2.0 * unit_radius[u]) ** 2 for u in order_units))
    row_width = max(2.0 * max_unit_radius, math.sqrt(total_area) * 1.2)

    # Pack inside the BOARD when there is one, anchored at the outline's
    # own min corner rather than a synthetic origin: on any outline
    # narrower than the shelf's natural row width, packing from the
    # origin puts parts straight off the edge — and once a part is
    # outside, ``bounds_for`` clamps every TRANSLATE that could rescue it
    # while ``_placement_is_legal`` rejects the crowding that bringing it
    # back would cause, so it stays outside for the whole anneal.
    #
    # Clamping the row width does NOT make an over-full board fit; parts
    # then wrap down past the bottom edge instead of off the right one.
    # That is the honest outcome and ``drc.check_outline_containment``
    # reports it. The point here is to stop the seed from putting parts
    # outside a board they would have fitted in.
    origin_x = origin_y = _EDGE_MARGIN_MM
    outline_bounds: tuple[float, float, float, float] | None = None
    if ir.outline and len(ir.outline) >= 3:
        outline_bounds = outline_bbox(ir.outline)
        ox0, oy0, ox1, oy1 = outline_bounds
        origin_x, origin_y = ox0 + _EDGE_MARGIN_MM, oy0 + _EDGE_MARGIN_MM
        usable = (ox1 - _EDGE_MARGIN_MM) - origin_x
        if usable > 2.0 * max_unit_radius:
            row_width = min(row_width, usable)

    shelf_x = origin_x
    shelf_y = origin_y
    shelf_h = 0.0
    unit_positions: dict[int | tuple[str, int], tuple[float, float]] = {}
    for unit in order_units:
        r = unit_radius[unit]
        diameter = 2.0 * r
        if shelf_x > origin_x and shelf_x + diameter > origin_x + row_width:
            shelf_x, shelf_y, shelf_h = origin_x, shelf_y + shelf_h, 0.0
        unit_positions[unit] = (
            shelf_x + r + _SEED_EPSILON_MM,
            shelf_y + r + _SEED_EPSILON_MM,
        )
        shelf_x += diameter + _SEED_EPSILON_MM
        shelf_h = max(shelf_h, diameter + _SEED_EPSILON_MM)

    # ── individually-packed instances (ungrouped + pattern members) ------
    freshly_seeded: set[int] = set()
    for members in clusters:
        for inst in members:
            unit = unit_of_instance[inst]
            if isinstance(unit, tuple):
                continue  # an authored-group member -- placed below, as one body
            x, y = unit_positions[unit]
            rot: float | None = None
            if not bool(ir.inst_fixed_rot[inst]) and (
                force or math.isnan(float(ir.inst_rot[inst]))
            ):
                rot = float(rng.choice((0.0, 90.0, 180.0, 270.0)))
            needs_xy = force or math.isnan(float(ir.inst_x[inst]))
            if bool(ir.inst_fixed_xy[inst]):
                # A locked instance still needs *some* coordinate to
                # participate in cost terms (module docstring: "locked
                # parts still contribute cost") — only fill it in if it
                # genuinely has none yet; never relocate a locked part.
                if math.isnan(float(ir.inst_x[inst])):
                    ir.move_instance(inst, x=x, y=y, rot=rot)
                elif rot is not None:
                    ir.move_instance(inst, rot=rot)
                continue
            if needs_xy:
                ir.move_instance(inst, x=x, y=y, rot=rot)
                freshly_seeded.add(inst)
            elif rot is not None:
                ir.move_instance(inst, rot=rot)

    # ── authored groups: packed by CENTROID, every member placed rigidly
    # relative to it (never from the anchor directly -- see
    # `group_local_centroid`'s own docstring for why that distinction is
    # load-bearing, not cosmetic).
    for gid, members in group_members.items():
        if not _is_authored_group(gid):
            continue
        # Treat the WHOLE group as needing a fresh seed the moment ANY
        # member does (a re-anneal call with everyone already placed
        # leaves it alone, same "don't reseed the settled" rule every
        # other instance here follows) -- an authored group is placed as
        # one atomic act, there is no meaningful "half seeded" state for
        # it.
        if not (force or any(math.isnan(float(ir.inst_x[m])) for m in members)):
            continue
        cx, cy = unit_positions[("group", gid)]
        local_cx, local_cy = group_local_centroid[gid]
        # A member with its OWN rotation locked pins the whole anchor's
        # rotation too -- a rigid body can't rotate to satisfy one
        # member's random draw while contradicting another's fixed one.
        arot = 0.0
        if not any(bool(ir.inst_fixed_rot[m]) for m in members):
            arot = float(rng.choice((0.0, 90.0, 180.0, 270.0)))
        for m in members:
            if bool(ir.inst_fixed_xy[m]):
                continue
            dx = float(ir.inst_group_offset_dx[m])
            dy = float(ir.inst_group_offset_dy[m])
            drot = float(ir.inst_group_offset_rot[m])
            dx = 0.0 if math.isnan(dx) else dx
            dy = 0.0 if math.isnan(dy) else dy
            drot = 0.0 if math.isnan(drot) else drot
            # This member's offset FROM THE GROUP CENTROID (not from the
            # anchor), rotated by the group's own draw and added to the
            # packed WORLD centroid position -- algebraically identical
            # to placing from the anchor (``anchor + rotate(offset,
            # arot)``) since the anchor is just ``offset (0, 0)``, but
            # expressed in the SAME centroid-relative frame
            # ``unit_radius``/``unit_positions`` already committed to
            # above, so the two never drift into disagreeing about where
            # "the group's own position" is.
            rdx, rdy = rotate_offset(dx - local_cx, dy - local_cy, arot)
            mrot = None if bool(ir.inst_fixed_rot[m]) else (arot + drot) % 360.0
            ir.move_instance(m, x=cx + rdx, y=cy + rdy, rot=mrot)

    # ── pattern leaders: compact each leader tile's own members into a
    # tight row before the stamp below copies that layout onto every
    # follower -- see `_compact_pattern_leader`'s own docstring for why
    # this exists (the SAME clustering mismatch `_merge_pattern_clusters`
    # closes at the cluster level, closed again here at the individual-
    # seed level: a merged cluster keeps a tile's members in the same
    # NEIGHBOURHOOD, not necessarily adjacent on the shelf).
    _compact_pattern_leaders(ir, radii, freshly_seeded=freshly_seeded)

    # ── pattern tiling: stamp the leader tile's internal layout onto
    # every congruent follower (feature 3) -- see that function's own
    # docstring for why this only touches groups this call just seeded.
    _stamp_pattern_tiles(ir, radii, freshly_seeded=freshly_seeded)


def _compact_pattern_leaders(
    ir: PcbIR, radii: np.ndarray, *, freshly_seeded: set[int]
) -> None:
    """Re-pack pattern_instance 0 (the LEADER) of every pattern's own
    members into a tight row, anchored at the LARGEST-radius member's own
    individually-seeded position — closes the SAME defect
    :func:`_merge_pattern_clusters` closes at the cluster level, one
    level down: even after that merge keeps a tile's members in one
    cluster (so the shelf pack visits them in the same neighbourhood),
    they are not necessarily CONSECUTIVE shelf slots (other, unrelated
    cluster members can land between them), so the leader's own members
    can still end up several millimetres apart — measured on the nano
    fixture, the "channel" leader tile's own D1/J4/Q1/R1 span shrank from
    ~85mm (three unrelated clusters, pre-``_merge_pattern_clusters``) to
    a still-real few millimetres post-merge, not the "adjacent centres at
    ``r_i + r_j``" a genuinely rigid tile needs.

    **Anchored at the LARGEST member, not an arbitrary one.**
    :meth:`OptimizeEngine.bounds_for` shrinks the shared board domain by
    EACH instance's own keep-out radius, so two members with different
    radii need different clearance from the board edge — anchoring at a
    SMALL member's position (whatever it happens to be) can seed a
    BIGGER sibling closer to the edge than ITS OWN radius ever tolerates,
    which makes the tile illegal from the moment it exists and, because
    every member shares one TRANSLATE delta, unable to self-correct
    until whichever draw happens to pick the oversized member specifically.
    The main shelf pack already gave every INDIVIDUALLY-seeded instance
    exactly its own required margin (``unit_positions[unit] = shelf_pos +
    r + eps``), so the largest member's own pre-compaction slot is
    already a position every SMALLER sibling can safely share.

    Legal-by-construction WITHIN the tile only (the same guarantee the
    main shelf pack makes for the whole board): adjacent members sit
    exactly ``r_i + r_j`` apart. It may freshly overlap a THIRD-PARTY
    instance the same way the main pack never avoids a mounting hole
    (:func:`seed_placement`'s own docstring) or a hole itself — the
    anneal's legality backstop is what resolves that, same precedent.

    Only :func:`_stamp_pattern_tiles`'s LEADER tiles need this: a
    follower's own internal layout is entirely OVERWRITTEN by that stamp
    regardless of how it seeded, so compacting a follower here would be
    wasted work immediately discarded. The row itself is built in
    radius-descending order (not sorted-instance-id order) — harmless,
    since the stamp reads whatever FINAL positions exist and re-derives
    each member's offset from the tile's own centroid fresh, it never
    assumes a particular on-row ordering.
    """
    members_by_group: dict[int, list[int]] = {}
    for i in range(ir.n_instances):
        gid = int(ir.inst_group[i])
        if gid >= 0:
            members_by_group.setdefault(gid, []).append(i)

    for gid, unordered_members in members_by_group.items():
        if ir.group_pattern[gid] is None:
            continue  # an authored group, not a pattern -- not this pass's job
        if int(ir.group_pattern_index[gid]) != 0:
            continue  # only the LEADER; a follower is overwritten by the stamp
        if len(unordered_members) < 2:
            continue
        if not all(m in freshly_seeded for m in unordered_members):
            continue
        if any(bool(ir.inst_fixed_xy[m]) for m in unordered_members):
            continue  # a locked member -- a rigid teleport would move it
        members = sorted(unordered_members, key=lambda m: -float(radii[m]))
        anchor_x = float(ir.inst_x[members[0]])
        anchor_y = float(ir.inst_y[members[0]])
        x = anchor_x
        prev_r = 0.0
        for k, m in enumerate(members):
            r = float(radii[m])
            if k == 0:
                x = anchor_x
            else:
                x += prev_r + r + _SEED_EPSILON_MM
                ir.move_instance(m, x=x, y=anchor_y)
            prev_r = r


def _stamp_pattern_tiles(
    ir: PcbIR, radii: np.ndarray, *, freshly_seeded: set[int]
) -> None:
    """The tiling stamp: force every PATTERN instance's internal layout
    to match pattern_instance 0's (the "leader") exactly — same member
    offsets from each tile's own centroid, same member rotations — so N
    congruent tiles are identical by construction rather than by
    coincidence of independent seeding. Runs at the end of
    :func:`seed_placement`, after every pattern member has already been
    seeded individually there (a pattern group carries no authored
    ``group_offset`` to seed FROM — see :attr:`~precis.pcb.ir.PcbIR.
    inst_group`'s own docstring for the group/pattern split).

    **Positional correspondence, not label matching.** Tile K's Nth
    member (sorted by instance id — the same order the graph declared
    components in) is taken to BE the leader tile's Nth member's
    counterpart. The simplest rule that needs no extra authored data, on
    the documented assumption that a fixture author lists every tile's
    components in the same structural order (see ``tests/fixtures/pcb/
    nano_oc_switch.json``'s four "channel" tiles: connector, transistor,
    resistor, diode, every time).

    **Only touches groups this call itself just seeded** —
    ``freshly_seeded`` (built by :func:`seed_placement`'s own per-
    instance loop) — never a tile that was already placed (by a prior
    ``seed_placement`` call, then possibly moved by an anneal since). A
    re-anneal call (``force=False``, everyone already positioned) must
    leave a settled tile's accumulated ROTATE/TRANSLATE progress alone;
    stamping it again here would silently overwrite every accepted move
    the anneal made to that tile with the leader's ORIGINAL layout,
    every single time :func:`optimize` is called again. The leader's
    layout is still read fresh from the IR (not cached across calls), so
    a tile added to an already-running pattern later inherits whatever
    shape the leader currently has, not its shape at pattern-instance-0's
    own original seed.
    """
    members_by_group: dict[int, list[int]] = {g: [] for g in range(ir.n_groups)}
    for i in range(ir.n_instances):
        gid = int(ir.inst_group[i])
        if gid >= 0:
            members_by_group[gid].append(i)

    tiles_by_pattern: dict[str, list[tuple[int, list[int]]]] = {}
    for gid, members in members_by_group.items():
        pattern = ir.group_pattern[gid]
        if not pattern:
            continue
        idx = int(ir.group_pattern_index[gid])
        tiles_by_pattern.setdefault(str(pattern), []).append((idx, sorted(members)))

    for tiles in tiles_by_pattern.values():
        tiles.sort(key=lambda t: t[0])
        leader_idx, leader_members = tiles[0]
        if leader_idx != 0:
            continue  # no declared pattern_instance 0 -- nothing to stamp from
        if any(math.isnan(float(ir.inst_x[m])) for m in leader_members):
            continue  # the leader itself never got placed
        lx = sum(float(ir.inst_x[m]) for m in leader_members) / len(leader_members)
        ly = sum(float(ir.inst_y[m]) for m in leader_members) / len(leader_members)
        leader_layout = [
            (float(ir.inst_x[m]) - lx, float(ir.inst_y[m]) - ly, float(ir.inst_rot[m]))
            for m in leader_members
        ]
        for idx, members in tiles[1:]:
            if len(members) != len(leader_members):
                continue  # an uneven/malformed tile -- left as individually seeded
            if not all(m in freshly_seeded for m in members):
                continue  # a settled tile from a prior call -- leave it alone
            if any(bool(ir.inst_fixed_xy[m]) for m in members):
                continue  # a locked member -- a rigid teleport would move it
            # Anchored at the LARGEST-radius member's own individually-
            # seeded position -- the SAME reasoning
            # :func:`_compact_pattern_leaders` uses for the leader tile,
            # for the SAME reason: a follower's own members can still be
            # scattered pre-stamp (`_merge_pattern_clusters` keeps them
            # in one cluster, not necessarily adjacent shelf slots), and
            # the ARITHMETIC MEAN of scattered positions is no more
            # trustworthy a placement than any one of them -- it can
            # itself land off-board even after the stamp fixes the
            # tile's INTERNAL shape. The biggest member's own slot is the
            # one position in the tile the whole-board shelf pack already
            # gave proper edge/neighbour clearance to.
            anchor_member = max(members, key=lambda m: float(radii[m]))
            fx = float(ir.inst_x[anchor_member])
            fy = float(ir.inst_y[anchor_member])
            for m, (ox, oy, rot) in zip(members, leader_layout):
                mrot = None if bool(ir.inst_fixed_rot[m]) else rot
                ir.move_instance(m, x=fx + ox, y=fy + oy, rot=mrot)


#: :func:`recentre_in_outline` fires only when the outline bbox area is at
#: most this multiple of the finished pack's own bbox area. Above it the
#: outline is a placeholder canvas (the esp32c3 fixture ships 300x300 for
#: a ~35mm design, ratio ~70), where centring is meaningless AND the shift
#: is large enough to perturb routing through the absolute-coordinate
#: board-edge interactions the function's own guard comment documents.
#: 9.0 = up to 3x the pack per axis: the motor reference (70x50 around a
#: ~28x30 pack, ratio ~4.2) is a REAL authored board that must centre —
#: 4.0 was measured to exclude it — while the placeholder canvas stays
#: two orders of magnitude beyond either figure.
_RECENTRE_MAX_AREA_RATIO = 9.0


def recentre_in_outline(ir: PcbIR) -> tuple[float, float]:
    """Translate the FINISHED placement as one rigid block so its bounding
    box (by part edge: centre ± courtyard-circumscribed radius) shares a
    centre with the authored outline's bbox. Returns the ``(dx, dy)``
    actually applied — ``(0, 0)`` when there is no outline or nothing is
    placed.

    Runs after the anneal, never in the seed: the anneal is NOT
    translation-invariant (its domain is clipped to the outline, so a
    corner-anchored seed anneals against a wall a centred one does not
    have — measured 2026-08-31, seeding centred flipped two of the esp32c3
    reference fixture's five seeds to ``no_path``), while ROUTING is
    translation-invariant (the grid origin derives from the pad bbox). So
    shifting the finished placement is visually free, and it is what makes
    a board use the middle of its authored outline instead of one corner
    quadrant (user review 2026-08-31, the motor board's lower-left
    cluster). The shift is clamped so it never pushes a part that already
    cleared :data:`_EDGE_MARGIN_MM` back out past it; a pack too big for
    the outline keeps its corner anchor (the honest overflow
    :func:`seed_placement`'s row-width clamp already permits). Locked
    (``fixed_xy``) instances pin the board: any lock means no shift — a
    rigid translation that moved a deliberately-placed part would violate
    the lock, and translating everyone EXCEPT the locked part would tear
    the placement apart.

    **Mounting holes can veto the shift.** Every OTHER instance stays
    exactly where it was relative to its neighbours (a rigid translation
    doesn't perturb that), but a mounting hole is fixed to the BOARD, not
    the pack — sliding the whole placement can newly park a part that
    was clear on top of a hole it never touched before, and this
    function runs AFTER the anneal, so nothing downstream would notice or
    fix it. Rather than duplicate the anneal's own polygon legality test
    here, this is a conservative circle-vs-circle broad-phase check: any
    hole overlap under it (an over-estimate, never an under-estimate —
    the same direction :func:`_hole_polygon`'s own circumscribing bound
    already leans) cancels the shift outright, keeping the pre-recentre
    corner-anchored layout instead of a corrected one — the same "honest
    overflow beats a silent violation" call ``row_width``'s clamp already
    makes."""
    if not ir.outline or len(ir.outline) < 3:
        return (0.0, 0.0)
    placed = [
        i
        for i in range(ir.n_instances)
        if math.isfinite(float(ir.inst_x[i])) and math.isfinite(float(ir.inst_y[i]))
    ]
    if not placed:
        return (0.0, 0.0)
    if any(bool(ir.inst_fixed_xy[i]) for i in placed):
        return (0.0, 0.0)
    radii = courtyard_bound_radius_mm(
        instance_courtyard_polygons(
            ir,
            clearance_mm=COURTYARD_CLEARANCE_MM,
            fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
        )
    )
    pack_x0 = min(float(ir.inst_x[i]) - float(radii[i]) for i in placed)
    pack_x1 = max(float(ir.inst_x[i]) + float(radii[i]) for i in placed)
    pack_y0 = min(float(ir.inst_y[i]) - float(radii[i]) for i in placed)
    pack_y1 = max(float(ir.inst_y[i]) + float(radii[i]) for i in placed)
    ox0, oy0, ox1, oy1 = outline_bbox(ir.outline)
    # Placeholder-canvas guard: routing is NOT perfectly translation-
    # invariant — `_outline_clip` and the pour's edge inset are absolute,
    # so a big shift changes which fan-out candidates the board edge
    # clips, and the routing draw with it (measured 2026-08-31: recentring
    # inside the esp32c3 fixture's 300x300 placeholder canvas flipped
    # reference seeds and cost seed 4 four silk placements). Centring a
    # design inside a canvas an order of magnitude larger than itself is
    # not a layout decision worth that perturbation; an outline
    # commensurate with its design (every real authored board) shifts by
    # little and keeps its routing character.
    pack_area = (pack_x1 - pack_x0) * (pack_y1 - pack_y0)
    outline_area = (ox1 - ox0) * (oy1 - oy0)
    if pack_area <= 0.0 or outline_area > _RECENTRE_MAX_AREA_RATIO * pack_area:
        return (0.0, 0.0)
    target_x0, target_x1 = ox0 + _EDGE_MARGIN_MM, ox1 - _EDGE_MARGIN_MM
    target_y0, target_y1 = oy0 + _EDGE_MARGIN_MM, oy1 - _EDGE_MARGIN_MM
    shift_x = (target_x0 + target_x1) / 2.0 - (pack_x0 + pack_x1) / 2.0
    shift_y = (target_y0 + target_y1) / 2.0 - (pack_y0 + pack_y1) / 2.0
    lo_x, hi_x = target_x0 - pack_x0, target_x1 - pack_x1
    lo_y, hi_y = target_y0 - pack_y0, target_y1 - pack_y1
    shift_x = max(lo_x, min(hi_x, shift_x)) if lo_x <= hi_x else lo_x
    shift_y = max(lo_y, min(hi_y, shift_y)) if lo_y <= hi_y else lo_y
    # Only translate when the pack was NOT already at the margin target:
    # a shift the clamp reduced to noise is not worth dirtying state for.
    if abs(shift_x) < 1e-9 and abs(shift_y) < 1e-9:
        return (0.0, 0.0)
    if ir.mounting_holes:
        for i in placed:
            nx, ny = float(ir.inst_x[i]) + shift_x, float(ir.inst_y[i]) + shift_y
            for hole in ir.mounting_holes:
                sep = float(radii[i]) + _hole_keepout_radius_mm(hole)
                if (nx - hole.x) ** 2 + (ny - hole.y) ** 2 < sep * sep:
                    return (0.0, 0.0)
    for i in placed:
        ir.move_instance(
            i, x=float(ir.inst_x[i]) + shift_x, y=float(ir.inst_y[i]) + shift_y
        )
    return (shift_x, shift_y)


# ── the incremental engine ───────────────────────────────────────────────

MoveGeneratorFn = Callable[["OptimizeEngine", random.Random, float], "Move | None"]

#: The margin cache key shape: a segment id (gap_capacity/loop_inductance),
#: a sorted segment-id pair (coupling's segment ids, or courtyard_overlap's
#: INSTANCE ids), a net name (thermal_rise), a layer id (crossings), or a
#: single instance id (board_edge_clearance).
MarginKey = int | tuple[int, int] | str


def _pair_key(a: int, b: int) -> tuple[int, int]:
    """A sorted pair key, canonical regardless of argument order — the same
    convention ``coupling``'s ``(sa, sb)`` cache key already uses, reused
    here for ``courtyard_overlap``'s instance-id pairs."""
    return (a, b) if a < b else (b, a)


#: A mounting hole's ``courtyard_overlap`` margin entry is keyed as a
#: ``(inst, _HOLE_KEY_BASE + hole_idx)`` pair through :func:`_pair_key` —
#: same ``tuple[int, int]`` shape :data:`MarginKey` already promises for
#: this term, so ``risk()``/``digest()`` need no special case to fold a
#: hole's penalty into the SAME ``_BY_NAME["courtyard_overlap"]``
#: criticality/justification an instance-instance overlap already uses
#: (registering a brand new term name would need a ``cost.py`` change
#: this slice doesn't make). ``_HOLE_KEY_BASE`` only has to clear the
#: largest realistic instance count (thousands, not tens of millions) to
#: stay unambiguous — :meth:`OptimizeEngine._region_for_key` is the one
#: place that decodes it back into "a real instance" vs. "a hole".
_HOLE_KEY_BASE = 10_000_000

#: How many sides approximate a mounting hole's circular keep-out for the
#: SAT overlap test :func:`~precis.pcb.geom.convex_polygons_overlap` (and
#: its graded sibling) already run on every other courtyard pair — a
#: circle has no SAT primitive of its own, and one is not worth adding to
#: ``geom.py`` for a shape this module is the only user of.
_HOLE_POLY_SIDES = 16


def _hole_polygon(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    """A regular :data:`_HOLE_POLY_SIDES`-gon that CIRCUMSCRIBES (never
    under-covers) a circle of radius ``r`` centred at ``(cx, cy)`` —
    vertices pushed out to ``r / cos(pi/sides)`` so every edge stays
    tangent to (or outside) the true circle. Over-covering a keep-out is
    always the safe direction (the same argument
    :func:`~precis.pcb.ir.instance_courtyard_polygon`'s own bounding-
    square pad approximation already makes for a round pad)."""
    sides = _HOLE_POLY_SIDES
    vr = r / math.cos(math.pi / sides)
    pts = [
        (
            cx + vr * math.cos(2.0 * math.pi * k / sides),
            cy + vr * math.sin(2.0 * math.pi * k / sides),
        )
        for k in range(sides)
    ]
    pts.append(pts[0])
    return pts


def _hole_keepout_radius_mm(hole: MountingHole) -> float:
    """A mounting hole's placement keep-out radius: half its widest
    feature — the plated annulus, the bare drill, or the authored
    hardware envelope (:attr:`~precis.pcb.ir.MountingHole.head_dia_mm`,
    the screw head / solder-nut flange / washer sitting above the board,
    which can be wider than the copper) — PLUS the same courtyard
    clearance an instance's own pads get against a neighbour
    (:data:`~precis.pcb.ir.COURTYARD_CLEARANCE_MM`) — the router needs
    the identical breathing room around a hole's copper (or bare NPTH
    edge, or physical hardware) that it needs around any other pad."""
    widest = max(hole.drill_mm, hole.ring_dia_mm, hole.head_dia_mm)
    return widest / 2.0 + COURTYARD_CLEARANCE_MM


#: How far inside the board outline a component's OUTERMOST PAD must sit
#: — the component centre must therefore stay this far in PLUS its own
#: :attr:`OptimizeEngine._keepout_r`. A single centre-inset was tried and
#: is wrong for the same reason a single courtyard radius is: a module
#: whose pads reach 8.9mm from its centre, placed at a 2mm centre-inset,
#: puts copper 6.9mm off the board. (That is not a hypothetical — it is
#: the 2 residual ``board_edge_clearance`` errors that survived every
#: other fix here.) A graded ``board_edge_clearance`` cost term already
#: existed and was settled through regardless: a part hanging off the
#: board is not a margin to trade, so this is a domain boundary.
_EDGE_MARGIN_MM = 0.5

#: Slack added between shelf-packed seed positions so a placement that is
#: exactly at the keep-out limit lands strictly inside it — the legality
#: test is a strict ``<``, and float arithmetic on the boundary is not
#: something to rely on.
_SEED_EPSILON_MM = 1e-3


class OptimizeEngine:
    """Wraps a :class:`PcbIR`, maintains an incrementally-updated cost
    decomposition (money sum + margin max, per term/region), and proposes
    /accepts/rejects :class:`Move`\\ s via simulated annealing. See the
    module docstring for exactly which terms are touched-only vs. the one
    honestly-whole-board exception (``board_area``).

    Construction assumes every instance already has an L3 position (call
    :func:`seed_placement` first, or use the :func:`optimize` convenience
    wrapper) — this engine is the refiner, not the seeder.
    """

    def __init__(self, ir: PcbIR, config: OptimizeConfig) -> None:
        self.ir = ir
        self.config = config
        self.level = (
            Level.L4
        )  # placement moves need L4 fidelity to see congestion at all
        self.schedule = 0.0  # constraint-hardening dial this engine drives itself
        self.moves: list[MoveRecord] = []

        n = ir.n_instances
        #: gid -> every member instance id, sorted ascending — the SAME
        #: correspondence order :func:`seed_placement`'s pattern-tiling
        #: stamp uses, which is what makes a SWAP between two congruent
        #: pattern tiles (:func:`_gen_swap`) a simple index-aligned
        #: exchange. Static for the engine's whole life: group MEMBERSHIP
        #: never changes mid-anneal, only member poses do.
        _members: dict[int, list[int]] = {g: [] for g in range(ir.n_groups)}
        for i in range(n):
            gid = int(ir.inst_group[i])
            if gid >= 0:
                _members[gid].append(i)
        self._group_members: dict[int, tuple[int, ...]] = {
            g: tuple(sorted(m)) for g, m in _members.items()
        }

        def _movable(fixed: np.ndarray) -> list[int]:
            # An instance is only movable if NEITHER it nor any of its
            # own group-mates is fixed on this axis — a rigid body can't
            # honour "this one part stays put" while every move that
            # touches it drags the whole group, so the group's motion on
            # that axis must be refused wholesale rather than silently
            # dragging a locked member along.
            out = []
            for i in range(n):
                if bool(fixed[i]):
                    continue
                gid = int(ir.inst_group[i])
                if gid >= 0 and any(bool(fixed[m]) for m in self._group_members[gid]):
                    continue
                out.append(i)
            return out

        self._movable_xy = _movable(ir.inst_fixed_xy)
        self._movable_rot = _movable(ir.inst_fixed_rot)
        self.board_side = max(20.0, 6.0 * math.sqrt(max(n, 1)))
        #: TRANSLATE's clamp bounds (module docstring's board_edge_
        #: clearance section) — the real outline's bounding box when the
        #: design has authored one (:func:`~precis.pcb.cost.outline_bbox`,
        #: the SAME approximation the ``board_edge_clearance`` cost term
        #: uses), else the synthetic origin-anchored square this engine
        #: has always used, unchanged for a design with no outline.
        #:
        #: **The outline CAPS the domain; it never expands it** (see
        #: :meth:`_derive_placement_bounds`): the seed extent padded by
        #: ``board_side``, clipped to the outline bbox inset by
        #: :data:`_EDGE_MARGIN_MM`. Handing the anneal an oversized/
        #: placeholder outline's full bbox was tried twice and reverted
        #: twice — it once quadrupled DRC errors on the ESP32-C3
        #: reference fixture, and on 2026-08-31 it flipped two of that
        #: fixture's five seeds to ``no_path`` — because the cooling
        #: schedule (``t0``/step size, both derived from this SAME
        #: ``board_side``, an n-instance heuristic, not outline-aware)
        #: stays exactly as "hot" and fast-cooling as it always has, so a
        #: component that randomly drifts far during the still-permissive
        #: early schedule has no realistic way to walk back within the
        #: fixed iteration budget once the schedule hardens. The visible
        #: symptom the cap causes — a big authored board delivered with
        #: everything in one corner quadrant — is solved by
        #: :func:`recentre_in_outline` translating the FINISHED placement
        #: instead, which routing (translation-invariant) never notices.
        #: **The domain must CONTAIN the seed.** ``seed_placement`` drops
        #: clusters on a fixed-pitch grid that can span well past
        #: ``board_side`` for a dense design; without an outline the
        #: domain is derived from the SEED's own extent (adjacency-
        #: clustered, so it is a real answer, not a heuristic square),
        #: padded by ``board_side`` — unchanged, see
        #: :meth:`_derive_placement_bounds`'s no-outline fallback. A
        #: seeded part outside the domain gets clamped into the corner by
        #: its first TRANSLATE, and SWAP can never move it at all (a swap
        #: whose partner sits outside the bounds is rejected forever) — a
        #: pile-up that produces overlapping courtyards.
        #: Per-instance courtyard POLYGON in the part's own local frame —
        #: the hull of its pads offset by :data:`~precis.pcb.ir.
        #: COURTYARD_CLEARANCE_MM`, floored to a square at the nominal
        #: half-courtyard so a pinless part (mounting hole, fiducial) still
        #: occupies space. **The same shape** :mod:`precis.pcb.silk` draws
        #: and ``courtyard_overlap`` DRC checks: one definition, three
        #: consumers, so a part's placement legality and the boundary a
        #: DRC run enforces cannot drift into two different answers for
        #: the same part. A RADIUS stood here until 2026-08-30 and could
        #: not do that job — it over-reserves an edge connector eightfold
        #: while UNDER-reserving a SOIC-8, and no single radius fixes both
        #: (:func:`~precis.pcb.ir.instance_courtyard_polygon` carries the
        #: measured table). Computed once: only a part's POSE changes
        #: during the anneal, never its footprint.
        self._keepout_poly = instance_courtyard_polygons(
            ir,
            clearance_mm=COURTYARD_CLEARANCE_MM,
            fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
        )
        #: The broad phase for :meth:`_placement_is_legal`, derived FROM
        #: the polygon rather than independently: a rotation-invariant
        #: circumscribed radius (:func:`~precis.pcb.ir.
        #: courtyard_bound_radius_mm`). This is the same vectorized
        #: ``d2 < (r_i + r_j)^2`` sweep that used to BE the legality test;
        #: it is now the filter in front of the exact polygon test, so the
        #: common "nowhere near each other" answer still costs one numpy
        #: comparison over the whole board.
        self._keepout_r = courtyard_bound_radius_mm(self._keepout_poly)
        #: Every mounting hole as a STATIC obstacle (gr263082-adjacent
        #: user report: Q3/R1/C1 landing on/under an M4 solder-nut hole) —
        #: a fixed-position, fixed-radius circumscribing polygon
        #: (:func:`_hole_polygon`) plus its own keep-out radius
        #: (:func:`_hole_keepout_radius_mm`), computed ONCE (a hole's pose
        #: never moves, unlike an instance's). :meth:`_placement_is_legal`
        #: hard-rejects any instance courtyard that overlaps one;
        #: :meth:`_refresh_courtyard` folds a graded pressure into the
        #: SAME ``courtyard_overlap`` margin term instance-instance
        #: overlap already uses (see :data:`_HOLE_KEY_BASE`), so the
        #: anneal has a slope pushing a part off a hole even when the
        #: seed happened to park it there — see :func:`seed_placement`'s
        #: docstring for why the seed itself doesn't avoid holes.
        self._hole_polys: list[list[tuple[float, float]]] = [
            _hole_polygon(h.x, h.y, _hole_keepout_radius_mm(h))
            for h in ir.mounting_holes
        ]
        self._hole_radius: list[float] = [
            _hole_keepout_radius_mm(h) for h in ir.mounting_holes
        ]
        #: The courtyard grid's cell size — the maximum distance at which
        #: any pair can have a nonzero graded ``courtyard_overlap`` value.
        #: The graded term reads polygon separation against a routing-
        #: corridor budget (:func:`~precis.pcb.cost.courtyard_overlap_
        #: pair_term`), so two instances interact iff their centres are
        #: within ``r_i + r_j + corridor`` — bounded by ``2*max(r) +
        #: corridor``. Cell size >= that bound is what keeps the
        #: 3x3-neighbourhood query (:meth:`_courtyard_candidates_near`)
        #: exact; a flat ``COURTYARD_MIN_SEPARATION_MM`` cell was exact
        #: only while the term itself was a flat 2.0mm circle (floored to
        #: it still, so a pinless-only board keeps the old geometry).
        r_max = float(self._keepout_r.max()) if len(self._keepout_r) else 0.0
        self._courtyard_cell_mm = max(
            COURTYARD_MIN_SEPARATION_MM,
            2.0 * r_max + config.cost.default_pitch_mm,
        )
        #: ``inst -> ((x, y, rot), world-frame polygon)``. Keyed on the
        #: POSE it was computed at rather than invalidated on move: a
        #: stale entry is then impossible by construction, which matters
        #: because moves arrive from several paths (TRANSLATE, SWAP,
        #: ROTATE, and the seeder) and an invalidation any one of them
        #: forgot would silently reserve a part's old footprint.
        self._world_poly: dict[
            int, tuple[tuple[float, float, float], list[tuple[float, float]]]
        ] = {}
        self._placement_bounds = self._derive_placement_bounds()
        #: ``t0`` and TRANSLATE's step both scale off ``board_side``. It is
        #: deliberately NOT re-derived from the (larger) placement bounds:
        #: inflating it makes the anneal start hot enough to scatter a
        #: compact seed across the whole domain, and the same fixed
        #: iteration budget cannot walk that back. The domain is where
        #: parts MAY go; ``board_side`` stays the scale at which they
        #: actually move.

        # per-segment endpoint-instance arrays, for the vectorized
        # "newly closer than cached" gap-capacity check (module docstring).
        self._seg_inst_a = ir.pin_instance[ir.seg_pin_a].astype(np.int64)
        self._seg_inst_b = ir.pin_instance[ir.seg_pin_b].astype(np.int64)
        self._seg_gap_distance = np.full(ir.n_segments, math.inf)
        self._seg_nearest_instance = np.full(ir.n_segments, -1, dtype=np.int64)

        # crossings state: GEOMETRIC (module docstring's crossings-term
        # section) -- `_segments_by_layer[layer]` is the
        # segment-membership index a bounded recount needs to enumerate
        # "the rest of that layer"; `_seg_crossing_partners[seg_id]` is the
        # set of OTHER same-layer segments `seg_id` currently, geometrically
        # crosses (symmetric: `seg_id in partners[other]` too);
        # `_layer_crossing_count[layer]` is the running per-layer total,
        # never recomputed by summing the sets. Populated by
        # `_init_crossing_state`, maintained by `_recompute_seg_crossings`.
        self._segments_by_layer: dict[int, set[int]] = {}
        self._seg_crossing_partners: dict[int, set[int]] = {}
        self._layer_crossing_count = [0] * ir.n_layers

        # courtyard-overlap state: a uniform spatial grid, cell size ==
        # `COURTYARD_MIN_SEPARATION_MM` (module docstring's courtyard_
        # overlap section) -- `_courtyard_grid[(cx, cy)]` is which instance
        # ids currently occupy that cell; `_inst_cell[inst]` is that
        # instance's own current cell (so a move can find and vacate its
        # OLD cell before relocating); `_courtyard_partners[inst]` is which
        # OTHER instances `inst` currently, geometrically overlaps
        # (symmetric, same shape as `_seg_crossing_partners`). Populated by
        # `_init_courtyard_state`, maintained by `_refresh_courtyard`.
        self._courtyard_grid: dict[tuple[int, int], set[int]] = {}
        self._inst_cell: dict[int, tuple[int, int]] = {}
        self._courtyard_partners: dict[int, set[int]] = {}

        # alignment state: piggybacks the courtyard grid above (module
        # docstring's alignment section) -- no second spatial index.
        # `_alignment_partners[inst]` is which OTHER instances currently
        # contribute a NONZERO `alignment` dollar value against `inst`
        # (symmetric, same shape as `_courtyard_partners`);
        # `_alignment_pair_usd[pair]` is that pair's current dollar value,
        # kept ONLY so a later refresh can subtract the old value before
        # adding the new one -- a MONEY term has no per-pair TermValue
        # survivor to re-derive a penalty from the way `_margin` does, just
        # a running total (`_money_static_by_name["alignment"]`, set below,
        # the same running-sum shape `_refresh_via_count_for_segment`
        # already uses for `via_count`). Populated by
        # `_init_alignment_state`, maintained by `_refresh_alignment`.
        self._alignment_partners: dict[int, set[int]] = {}
        self._alignment_pair_usd: dict[tuple[int, int], float] = {}

        # measures state: author-supplied proximity/separation pair bounds
        # (`config.measures`, see :class:`MeasureSpec`), resolved to
        # instance ids ONCE here (never re-resolved mid-anneal — the
        # NAMED pair is fixed for the design's whole life, unlike
        # courtyard/alignment's dynamically-discovered spatial neighbours).
        # `_measures_by_inst[inst]` is the FIXED list of `_measure_pairs`
        # indices naming `inst` — no discard-then-rediscover dance is
        # needed the way courtyard/alignment's grid search requires, since
        # membership never changes, only the two named instances' own
        # positions do. `_measure_usd[idx]` mirrors `_alignment_pair_usd`'s
        # own "remember the OLD value so a later refresh can subtract it"
        # role for the SAME running-total shape, folded into
        # `_money_measures` (not `_money_static_by_name` — see
        # :attr:`OptimizeConfig.measures`'s and :meth:`money`'s own
        # docstrings for why this term stays outside the `_BY_NAME`-keyed
        # catalogue). An unresolvable measure (a refdes not on this design,
        # or naming an instance twice) is silently dropped here, mirroring
        # `precis.pcb.eyes._resolve`'s own leniency — a design changing
        # underneath a stale measure should not crash placement.
        refdes_to_inst = {str(ir.instance_refdes[i]): i for i in range(ir.n_instances)}
        self._measure_pairs: list[tuple[int, int, MeasureSpec]] = []
        for spec in config.measures:
            mia = refdes_to_inst.get(spec.refdes_a)
            mib = refdes_to_inst.get(spec.refdes_b)
            if mia is None or mib is None or mia == mib:
                continue
            self._measure_pairs.append((mia, mib, spec))
        self._measures_by_inst: dict[int, list[int]] = {}
        for idx, (mia, mib, _spec) in enumerate(self._measure_pairs):
            self._measures_by_inst.setdefault(mia, []).append(idx)
            self._measures_by_inst.setdefault(mib, []).append(idx)
        self._measure_usd: dict[int, float] = {}
        self._money_measures: float = 0.0

        # margin cache: (term_name, key) -> latest TermValue. `key` is a
        # segment id for gap_capacity/loop_inductance, a sorted segment-id
        # pair for coupling, a net name for thermal_rise, a layer id for
        # crossings, a sorted instance-id pair for courtyard_overlap, or a
        # single instance id for board_edge_clearance.
        self._margin: dict[tuple[str, MarginKey], TermValue] = {}
        self._loop_applicable: set[int] = set()
        self._coupling_candidates = coupling_candidates(ir, config.cost)
        self._coupling_candidate_set = set(self._coupling_candidates)
        self._coupling_pairs: set[tuple[int, int]] = set()

        self._money_static_by_name: dict[str, float] = {}
        self._money_board_area = 0.0
        #: seg_id -> that segment's OWN implied via count (:func:`precis.
        #: pcb.rules.implied_via_count`), the per-segment breakdown behind
        #: the ``via_count`` entry in ``_money_static_by_name`` — kept so
        #: :meth:`_refresh_via_count_for_segment` can diff exactly ONE
        #: segment's old vs. new count instead of resumming the board.
        self._seg_via_count: dict[int, int] = {}

        # Layer-move eligibility: LAYER_ASSIGN targets "signal" role
        # layers only (a routed trace belongs on a routing layer, not a
        # plane), PLANE_PROMOTE targets "plane" role layers only. This
        # reads the board's own `stackup` role field rather than
        # hardcoding SIG/GND/PWR/SIG indices — the backlog's "roles are
        # emergent, not hardcoded" applies to which nets occupy a plane
        # layer, not (in this slice) to reclassifying a layer's own role
        # mid-anneal, which stays a placement-time (stackup-authoring)
        # decision. Falls back to "every layer is eligible" for a
        # stackup that never declared roles, rather than making every
        # LAYER_ASSIGN/PLANE_PROMOTE move silently inert.
        # Both read :mod:`precis.pcb.ir`'s routable/pourable predicates. A
        # layer used to be either/or via ``role`` alone; a stackup can now
        # mark one BOTH (explicit ``routable``/``pourable`` keys), which is
        # what lets an inner layer carry traces while an outer one carries
        # traces AND a ground fill. ``_plane_layers`` still gates only the
        # AUTOMATIC move generator (``_gen_plane_promote``) — an authored
        # ``op='plane_net'`` reaches ``PcbIR.promote_plane`` directly
        # regardless, so this engine never starts auto-filling a layer the
        # user did not ask it to.
        self._signal_layers = routable_layers(ir) or list(range(ir.n_layers))
        self._plane_layers = pourable_layers(ir)

        self._init_caches()
        self._init_crossing_state()
        self._init_courtyard_state()
        self._init_alignment_state()
        self._init_measures_state()
        self._seed_layers()

    # -- one-time initialization (the only "full board" pass) ------------
    def _init_caches(self) -> None:
        ir, cfg = self.ir, self.config

        for s in range(ir.n_segments):
            self._refresh_gap(s)
            lt = loop_inductance_term(ir, s, self.level, cfg.cost)
            if lt is not None:
                self._loop_applicable.add(s)
                self._margin[("loop_inductance", s)] = lt

        cands = self._coupling_candidates
        for i in range(len(cands)):
            for j in range(i + 1, len(cands)):
                sa, sb = cands[i], cands[j]
                t = coupling_pair_term(ir, sa, sb, self.level, cfg.cost)
                if t is not None:
                    key = (sa, sb)
                    self._coupling_pairs.add(key)
                    self._margin[("coupling", key)] = t

        # static (placement-invariant) terms: one full pass, never again.
        # `via_count` is deliberately NOT read off this pass -- see below,
        # it needs its own per-segment seed so later moves get a real
        # per-segment diff to work from, not just a scalar to overwrite.
        full = evaluate_cost(ir, self.level, cfg.cost)
        for t in full.terms:
            if t.name in ("layer_count", "extended_part_fees"):
                self._money_static_by_name[t.name] = (
                    self._money_static_by_name.get(t.name, 0.0) + t.raw
                )
            elif t.name == "thermal_rise":
                self._margin[("thermal_rise", t.region)] = t

        via_usd_total = 0.0
        for s in range(ir.n_segments):
            n = implied_via_count(
                ir,
                s,
                fab_caps=cfg.cost.fab_caps,
                class_rules=cfg.cost.class_rules,
                temp_rise_c=cfg.cost.thermal_temp_rise_c,
                copper_oz=cfg.cost.thermal_copper_oz,
            )
            self._seg_via_count[s] = n
            via_usd_total += n * cfg.cost.via_usd
        self._money_static_by_name["via_count"] = via_usd_total

        for i in range(ir.n_instances):
            self._refresh_board_edge(i)

        self._refresh_board_area()

    def _init_crossing_state(self) -> None:
        """One-time full-board pass (mirrors :meth:`_init_caches`'s own
        "the only full board pass" contract): build ``_segments_by_layer``
        from whatever ``seg_layer`` the IR already carries at construction
        (typically all ``UNSET_LAYER`` for a fresh board, but never
        assumed to be), then a ONE-TIME O(sum of layer_size^2) pairwise
        pass populates ``_seg_crossing_partners``/``_layer_crossing_count``
        exactly (same one-time-full-pass budget ``_init_caches``'s own
        O(candidates^2) coupling double loop already spends) before
        :meth:`_refresh_crossings_term` seeds the margin cache for every
        layer. Runs BEFORE :meth:`_seed_layers` so that method's own
        default-layer assignment goes through
        :meth:`_recompute_seg_crossings` like any other ``seg_layer``
        change, rather than needing its own special-cased bulk seed."""
        ir = self.ir
        for s in range(ir.n_segments):
            layer = int(ir.seg_layer[s])
            if layer == UNSET_LAYER:
                continue
            self._segments_by_layer.setdefault(layer, set()).add(s)
            self._seg_crossing_partners.setdefault(s, set())
        for layer, seg_ids in self._segments_by_layer.items():
            ids = sorted(seg_ids)
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    sa, sb = ids[i], ids[j]
                    if self._segs_cross(sa, sb):
                        self._seg_crossing_partners[sa].add(sb)
                        self._seg_crossing_partners[sb].add(sa)
                        self._layer_crossing_count[layer] += 1
        for layer in range(ir.n_layers):
            self._refresh_crossings_term(layer)

    def _segs_cross(self, sa: int, sb: int) -> bool:
        """The exact geometric test one candidate pair needs — same-net
        (star spokes off one hub) never counts regardless of geometry, an
        unplaced (NaN) endpoint on EITHER segment excludes the pair rather
        than manufacturing a phantom crossing at the origin, and
        :func:`~precis.pcb.geom.segments_cross` supplies the rest
        (shared-endpoint / collinear / touch-only exclusion) — the same
        primitive :func:`precis.pcb.ir.same_layer_crossing_count` uses for
        the full-board sweep, so the incremental and from-scratch paths
        can never silently diverge on what counts as a crossing."""
        ir = self.ir
        if int(ir.seg_net[sa]) == int(ir.seg_net[sb]):
            return False
        pa, pb = segment_points(ir, sa), segment_points(ir, sb)
        if pa is None or pb is None:
            return False
        return segments_cross(pa[0], pa[1], pb[0], pb[1])

    def _refresh_crossings_term(self, layer: int) -> None:
        """Recompute the ``crossings`` :class:`~precis.pcb.cost.TermValue`
        for one layer from the cached running total — O(1), never a
        rescan. Mirrors :func:`precis.pcb.cost.crossings_term_for_layer`'s
        L3+ (geometric) branch exactly (same formula, same
        fraction/justification shape) so the two stay provably identical
        — see the delta-correctness tests."""
        fraction = (
            self._layer_crossing_count[layer] / self.config.cost.crossings_tolerance
        )
        self._margin[("crossings", layer)] = TermValue(
            "crossings",
            Family.MARGIN,
            f"layer{layer}",
            fraction,
            _BY_NAME["crossings"].justification,
            is_bound=True,
        )

    def _recompute_seg_crossings(
        self, seg_id: int, old_layer: int, new_layer: int
    ) -> None:
        """The bounded per-move crossing delta (module docstring's
        crossings-term section): discard ``seg_id``'s cached partnerships
        (O(old partner count)), then — if it still has a layer — retest it
        against every OTHER segment CURRENTLY on ``new_layer`` (O(layer
        size), via ``_segments_by_layer``, never O(board)). Correct
        regardless of call order or how many times one move touches the
        same pair (e.g. two segments of the SAME moved instance): each
        call fully resets its own segment's contribution before rebuilding
        it fresh from the CURRENT (already-updated) geometry of every
        counterpart, so whichever segment is processed second simply
        rediscovers the first's already-current state — see the
        delta-correctness tests, which pin this down against a full
        :func:`~precis.pcb.cost.evaluate_cost` re-run after every move,
        including moves that touch more than one segment at once."""
        old_partners = self._seg_crossing_partners.get(seg_id, set())
        touched_layers: set[int] = set()
        if old_partners:
            for p in old_partners:
                self._seg_crossing_partners[p].discard(seg_id)
            if old_layer != UNSET_LAYER:
                self._layer_crossing_count[old_layer] -= len(old_partners)
                touched_layers.add(old_layer)
        self._seg_crossing_partners[seg_id] = set()

        if old_layer != new_layer:
            if old_layer != UNSET_LAYER:
                self._segments_by_layer.get(old_layer, set()).discard(seg_id)
            if new_layer != UNSET_LAYER:
                self._segments_by_layer.setdefault(new_layer, set()).add(seg_id)

        if new_layer != UNSET_LAYER:
            candidates = self._segments_by_layer.get(new_layer, ())
            new_partners = {
                other
                for other in candidates
                if other != seg_id and self._segs_cross(seg_id, other)
            }
            self._seg_crossing_partners[seg_id] = new_partners
            for p in new_partners:
                self._seg_crossing_partners.setdefault(p, set()).add(seg_id)
            if new_partners:
                self._layer_crossing_count[new_layer] += len(new_partners)
                touched_layers.add(new_layer)

        for layer in touched_layers:
            self._refresh_crossings_term(layer)

    # -- courtyard-overlap delta (grid-bucketed — module docstring) ------
    def _courtyard_cell(self, inst: int) -> tuple[int, int]:
        x = float(self.ir.inst_x[inst])
        y = float(self.ir.inst_y[inst])
        cell = self._courtyard_cell_mm
        return (math.floor(x / cell), math.floor(y / cell))

    def _courtyard_candidates_near(self, inst: int) -> set[int]:
        """Every OTHER instance that could possibly carry a nonzero graded
        ``courtyard_overlap`` value against ``inst`` — the 3x3
        neighbourhood of grid cells around ``inst``'s own cell, which is
        EXACT (not an approximation) because the cell size
        (:attr:`_courtyard_cell_mm`) is at least the maximum pair
        interaction distance (see its construction in ``__init__``)."""
        cx, cy = self._courtyard_cell(inst)
        out: set[int] = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out |= self._courtyard_grid.get((cx + dx, cy + dy), set())
        out.discard(inst)
        return out

    def _init_courtyard_state(self) -> None:
        """One-time seed: refreshing every instance IN ORDER correctly
        populates the grid AND every pair's partnership with no separate
        O(n^2) pass needed — by the time instance ``k`` is refreshed,
        every earlier instance is already in the grid, so ``k``'s own
        neighbourhood query finds it (and, symmetrically, records the pair
        on both sides) exactly once. The SAME method
        :meth:`_refresh_courtyard` used for every later per-move delta."""
        for i in range(self.ir.n_instances):
            self._refresh_courtyard(i)

    def _refresh_courtyard(self, inst: int) -> None:
        """The bounded per-move courtyard-overlap delta: discard ``inst``'s
        cached partnerships (O(old partner count)), relocate it in the
        grid, then retest it against its (possibly new) 3x3-neighbourhood
        candidates only — O(local density), never O(board). Mirrors
        :meth:`_recompute_seg_crossings`'s discard-then-rebuild-fresh shape
        (see that method's and the module docstring's courtyard_overlap
        section) so it is correct regardless of call order for a multi-
        instance move too."""
        ir, cfg = self.ir, self.config
        old_partners = self._courtyard_partners.get(inst, set())
        for p in old_partners:
            self._courtyard_partners.get(p, set()).discard(inst)
            self._margin.pop(("courtyard_overlap", _pair_key(inst, p)), None)
        self._courtyard_partners[inst] = set()

        old_cell = self._inst_cell.get(inst)
        if old_cell is not None:
            self._courtyard_grid.get(old_cell, set()).discard(inst)
        new_cell = self._courtyard_cell(inst)
        self._courtyard_grid.setdefault(new_cell, set()).add(inst)
        self._inst_cell[inst] = new_cell

        for other in self._courtyard_candidates_near(inst):
            # The engine's pose-keyed world polygons ride along so the term
            # never recomputes a hull on the per-move path (same shape as
            # the term's own IR-derived fallback — see its docstring).
            t = courtyard_overlap_pair_term(
                ir,
                inst,
                other,
                self.level,
                cfg.cost,
                poly_a=self._world_courtyard(inst),
                poly_b=self._world_courtyard(other),
            )
            if t.raw <= 0.0:
                continue
            key = _pair_key(inst, other)
            self._margin[("courtyard_overlap", key)] = t
            self._courtyard_partners[inst].add(other)
            self._courtyard_partners.setdefault(other, set()).add(inst)

        # Mounting holes are STATIC (module docstring's mounting-hole
        # section) and few (a handful, never board-scale), so every
        # refresh just recomputes ``inst``'s own hole entries from scratch
        # rather than tracking a separate hole-partner set the way the
        # instance-instance grid above needs to — O(n_holes) per move,
        # not O(board). Folded into the SAME ``courtyard_overlap`` term
        # name (:data:`_HOLE_KEY_BASE`'s own docstring) so this is the
        # identical graded pressure an overlapping NEIGHBOUR already gets,
        # mirroring instance-instance overlap rather than inventing a
        # second cost shape for "too close to an obstacle".
        for hole_idx in range(len(self._hole_polys)):
            hole_key = ("courtyard_overlap", _pair_key(inst, _HOLE_KEY_BASE + hole_idx))
            corridor_mm = cfg.cost.default_pitch_mm
            separation_mm = convex_polygons_signed_separation(
                self._world_courtyard(inst), self._hole_polys[hole_idx]
            )
            fraction = max(0.0, (corridor_mm - separation_mm) / corridor_mm)
            if fraction <= 0.0:
                self._margin.pop(hole_key, None)
                continue
            self._margin[hole_key] = TermValue(
                "courtyard_overlap",
                Family.MARGIN,
                f"{ir.instance_refdes[inst]}~hole{hole_idx}",
                fraction,
                _BY_NAME["courtyard_overlap"].justification,
                is_bound=False,
            )

    # -- alignment delta (piggybacks the courtyard grid above) -----------
    def _init_alignment_state(self) -> None:
        """One-time seed, mirroring :meth:`_init_courtyard_state`'s own
        shape and correctness argument — run AFTER that method so every
        instance's own refresh already finds every OTHER instance in the
        (by then fully populated) courtyard grid, one forward pass, no
        separate O(n^2)."""
        for i in range(self.ir.n_instances):
            self._refresh_alignment(i)

    def _refresh_alignment(self, inst: int) -> None:
        """The bounded per-move ``alignment`` delta — reuses
        courtyard_overlap's OWN, already-relocated 3x3 grid neighbourhood
        (:meth:`_courtyard_candidates_near`; every call site below calls
        this immediately after :meth:`_refresh_courtyard` has moved
        ``inst`` to its current cell) rather than a second spatial index,
        and passes this engine's OWN ``_courtyard_cell_mm`` as
        :func:`~precis.pcb.cost.alignment_pair_term`'s ``neighbourhood_mm``
        — see that function's and :func:`~precis.pcb.cost.
        _alignment_neighbourhood_mm`'s docstrings for why the two are an
        EXACT match (same formula, same inputs), not merely a
        conveniently-overlapping bound: this candidate search can never
        miss a pair the term itself would score nonzero.

        A MONEY term sums rather than max-aggregates, so there is no
        per-pair TermValue survivor to keep the way ``_margin`` keeps one
        for ``courtyard_overlap`` — instead this maintains a RUNNING
        dollar total (``_money_static_by_name["alignment"]``, the same
        running-sum shape :meth:`_refresh_via_count_for_segment` already
        keeps for ``via_count``) plus a per-pair dollar cache
        (``_alignment_pair_usd``) whose only job is remembering the OLD
        value to subtract before the NEW one is added.
        ``_alignment_partners`` mirrors ``_courtyard_partners``'s
        discard-then-rebuild shape exactly — only nonzero pairs kept, the
        same convention :meth:`_refresh_courtyard` uses — so this stays
        correct regardless of call order for a multi-instance move too."""
        ir, cfg = self.ir, self.config
        old_partners = self._alignment_partners.get(inst, set())
        total = self._money_static_by_name.get("alignment", 0.0)
        for p in old_partners:
            self._alignment_partners.get(p, set()).discard(inst)
            total -= self._alignment_pair_usd.pop(_pair_key(inst, p), 0.0)
        self._alignment_partners[inst] = set()

        for other in self._courtyard_candidates_near(inst):
            t = alignment_pair_term(
                ir,
                inst,
                other,
                self.level,
                cfg.cost,
                neighbourhood_mm=self._courtyard_cell_mm,
            )
            if t.raw <= 0.0:
                continue
            key = _pair_key(inst, other)
            self._alignment_pair_usd[key] = t.raw
            total += t.raw
            self._alignment_partners[inst].add(other)
            self._alignment_partners.setdefault(other, set()).add(inst)

        self._money_static_by_name["alignment"] = total

    # -- measures delta (author-supplied proximity/separation pairs) -----
    def _init_measures_state(self) -> None:
        """One-time full pass over every resolved :attr:`_measure_pairs`
        entry — unlike :meth:`_init_courtyard_state`/:meth:`
        _init_alignment_state`, this needs no per-instance ordering
        argument: a measure's pair is FIXED (not spatial-neighbourhood
        discovered), so visiting each measure once, in any order, already
        computes the exact same total :meth:`_refresh_measures` maintains
        incrementally afterward."""
        total = 0.0
        for idx, (mia, mib, spec) in enumerate(self._measure_pairs):
            usd = _measure_pair_usd(self.ir, mia, mib, spec)
            self._measure_usd[idx] = usd
            total += usd
        self._money_measures = total

    def _refresh_measures(self, inst: int) -> None:
        """The bounded per-move measures delta: recompute exactly the
        measures NAMING ``inst`` (``_measures_by_inst[inst]``, a fixed
        list built once at construction — never a spatial search) from
        the instance's current (already-moved) position, subtracting each
        one's OLD cached dollar value before adding the new one — the same
        running-total shape :meth:`_refresh_alignment` uses for
        ``_money_static_by_name["alignment"]``, here folded into the
        SEPARATE ``_money_measures`` channel (see :attr:`OptimizeConfig.
        measures`'s docstring for why). Recomputing a measure spanning a
        SWAP's two instances twice (once per moved instance, the second
        time seeing both new positions) is correct regardless of call
        order — a fresh-from-current-position computation, not an
        incremental diff, so the LAST call touching any given measure is
        always the true answer, the same argument :meth:`_apply_placement`
        already relies on for courtyard/alignment."""
        indices = self._measures_by_inst.get(inst)
        if not indices:
            return
        total = self._money_measures
        for idx in indices:
            mia, mib, spec = self._measure_pairs[idx]
            total -= self._measure_usd.get(idx, 0.0)
            usd = _measure_pair_usd(self.ir, mia, mib, spec)
            self._measure_usd[idx] = usd
            total += usd
        self._money_measures = total

    # -- board-edge-clearance delta (O(1) per instance) -------------------
    def _refresh_board_edge(self, inst: int) -> None:
        t = board_edge_clearance_term(self.ir, inst, self.level, self.config.cost)
        key = ("board_edge_clearance", inst)
        if t is None:
            self._margin.pop(key, None)
        else:
            self._margin[key] = t

    def _seed_layers(self) -> None:
        """One-time seed (mirrors :func:`seed_placement`'s "seed then
        refine" shape, at L1 instead of L3): every segment still at
        ``UNSET_LAYER`` is assigned the first eligible signal layer.
        Needed so :meth:`precis.pcb.ir.PcbIR.set_layer` — which rejects a
        negative layer index — always has a valid value to restore on
        :meth:`undo_move`; a LAYER_ASSIGN move's ``old_int`` must never be
        ``UNSET_LAYER``. Cheap (``n_segments`` calls, once) and harmless
        to run even when every segment already has a layer (the loop is a
        no-op then)."""
        ir = self.ir
        if not self._signal_layers:
            return
        default_layer = self._signal_layers[0]
        seeded: list[int] = []
        for s in range(ir.n_segments):
            if int(ir.seg_layer[s]) == UNSET_LAYER:
                ir.set_layer(s, default_layer)
                self._recompute_seg_crossings(s, UNSET_LAYER, default_layer)
                seeded.append(s)
        self._refresh_layer_count()
        # This IS the engine consuming the dirty flags `set_layer` raised
        # (the layer_count + crossings refreshes above, plus the already-
        # current L4 margin caches from `_init_caches`, none of which read
        # `seg_side`/`inst_rot` — see the module-level ROTATE/SIDE_FLIP
        # note) — matches `PcbIR.clean`'s own contract ("acknowledge that
        # an engine consumed the dirty flags... and recomputed"). Leaving
        # these set would make every post-construction dirty-cascade
        # assertion (this slice's own delta-correctness tests included)
        # see stale "dirty since birth" flags that have nothing to do with
        # any move.
        ir.clean(Level.L1, seeded)
        ir.clean(Level.L4, seeded)
        ir.clean(Level.L5, seeded)

    def _refresh_layer_count(self) -> None:
        """Recompute the ``layer_count`` money term from scratch and
        overwrite its cached value — needed after any move that can
        change the *set* of layers in use (LAYER_ASSIGN, PLANE_PROMOTE,
        PLANE_DEMOTE). This is exactly the "slice 7's layer/topology moves
        will need to dirty them again" the module docstring flagged
        ahead of time; ``extended_part_fees`` stays static — no move class
        this slice registers touches extended-part membership. ``via_count``
        gets its OWN, more local refresh (:meth:`_refresh_via_count_for_
        segment`) rather than this whole-board recompute — see that
        method's docstring for why a per-segment SUM affords a real O(1)
        delta where a set-of-layers-in-use aggregate like this one does
        not."""
        self._money_static_by_name["layer_count"] = layer_count_term(
            self.ir, self.level, self.config.cost
        ).raw

    def _refresh_via_count_for_segment(self, seg_id: int) -> None:
        """Recompute segment ``seg_id``'s OWN implied via count
        (:func:`precis.pcb.rules.implied_via_count`) and fold the delta
        into the cached ``via_count`` money total — O(1), never a board
        rescan. Sound because ``implied_via_count`` reads only ``seg_id``'s
        own ``seg_net``/``seg_layer``/``net_plane_layers`` fields (see that
        function's docstring), so no OTHER segment's cached count can ever
        go stale as a side effect of this one segment's move.

        Called from every move kind that can change what
        ``implied_via_count`` reads for a segment: ``LAYER_ASSIGN``
        (``seg_layer``, one segment) and ``PLANE_PROMOTE``/``PLANE_DEMOTE``
        via :meth:`_rescan_net` (``net_plane_layers``, that net's own
        segments — never the whole board). Placement moves (TRANSLATE/
        ROTATE/SWAP/SIDE_FLIP/PIN_SWAP) never call this: none of them touch
        a field this function reads, matching the module docstring's
        placement-invariance claim exactly."""
        old = self._seg_via_count.get(seg_id, 0)
        new = implied_via_count(
            self.ir,
            seg_id,
            fab_caps=self.config.cost.fab_caps,
            class_rules=self.config.cost.class_rules,
            temp_rise_c=self.config.cost.thermal_temp_rise_c,
            copper_oz=self.config.cost.thermal_copper_oz,
        )
        if new == old:
            return
        self._seg_via_count[seg_id] = new
        delta_usd = (new - old) * self.config.cost.via_usd
        self._money_static_by_name["via_count"] = (
            self._money_static_by_name.get("via_count", 0.0) + delta_usd
        )

    # -- gap-capacity delta (the involved one — see module docstring) ----
    def _refresh_gap(self, seg_id: int) -> None:
        ir, cfg = self.ir, self.config
        found = nearest_other_instance(ir, seg_id)
        if found is None:
            self._seg_gap_distance[seg_id] = math.inf
            self._seg_nearest_instance[seg_id] = -1
            ir.seg_gap_capacity[seg_id] = math.nan
        else:
            gap, nearest_id = found
            self._seg_gap_distance[seg_id] = gap
            self._seg_nearest_instance[seg_id] = nearest_id
            ir.seg_gap_capacity[seg_id] = math.floor(gap / cfg.gap_pitch_mm)
        self._margin[("gap_capacity", seg_id)] = gap_capacity_term(
            ir, seg_id, self.level, cfg.cost
        )

    def _newly_closer_segments(
        self, moved_inst: int, already_handled: set[int]
    ) -> list[int]:
        """Vectorized O(n_segments) numpy scan — cheap (a subtraction + a
        compare over already-known arrays), never a re-derivation of the
        expensive nearest-instance search itself (module docstring's
        "what is NOT claimed local" carve-out applies to margin
        aggregation, not this: this check touches every segment but does
        none of the O(n_instances) work per segment)."""
        ir = self.ir
        mx = float(ir.inst_x[moved_inst])
        my = float(ir.inst_y[moved_inst])
        ax, ay = ir.inst_x[self._seg_inst_a], ir.inst_y[self._seg_inst_a]
        bx, by = ir.inst_x[self._seg_inst_b], ir.inst_y[self._seg_inst_b]
        with np.errstate(invalid="ignore"):
            da = np.hypot(ax - mx, ay - my)
            db = np.hypot(bx - mx, by - my)
            d = np.minimum(da, db)
            closer = d < self._seg_gap_distance
        incident = (self._seg_inst_a == moved_inst) | (self._seg_inst_b == moved_inst)
        closer = closer & ~incident
        out = [int(s) for s in np.flatnonzero(closer) if int(s) not in already_handled]
        for s in out:
            self._seg_gap_distance[s] = float(d[s])
            self._seg_nearest_instance[s] = moved_inst
            self.ir.seg_gap_capacity[s] = math.floor(
                float(d[s]) / self.config.gap_pitch_mm
            )
            self._margin[("gap_capacity", s)] = gap_capacity_term(
                self.ir, s, self.level, self.config.cost
            )
        return out

    def _refresh_board_area(self) -> None:
        self._money_board_area = board_area_term(
            self.ir, self.level, self.config.cost
        ).raw

    # -- hard placement constraints ------------------------------------
    def _derive_placement_bounds(self) -> tuple[float, float, float, float]:
        """The rectangle TRANSLATE/SWAP may put a component CENTRE in —
        the seeded parts' own bounding box padded by ``board_side``,
        clipped to the outline inset by :data:`_EDGE_MARGIN_MM`.

        Deliberately NOT the full outline bbox, even though the seed
        (:func:`seed_placement`) now centres inside the outline: measured
        2026-08-31 on the esp32c3 reference fixture (which authors a
        300x300 placeholder canvas), handing the anneal the whole outline
        let seeds 4 and 5 sprawl parts far enough apart that SCL came
        back ``no_path`` — the router's expansion budget is finite and a
        sparse board is a worse board anyway. The seed-extent-plus-pad
        cap is what keeps the working area compact; because the seed is
        centred, that capped domain is now centred in the authored board
        too, which is all the fill-the-outline complaint actually needed.
        A future spread-to-fill AESTHETIC pressure belongs in the cost
        function, not in this domain."""
        ir = self.ir
        ox0, oy0, ox1, oy1 = 0.0, 0.0, self.board_side, self.board_side
        if ir.outline and len(ir.outline) >= 3:
            ox0, oy0, ox1, oy1 = outline_bbox(ir.outline)
            ox0, oy0 = ox0 + _EDGE_MARGIN_MM, oy0 + _EDGE_MARGIN_MM
            ox1, oy1 = ox1 - _EDGE_MARGIN_MM, oy1 - _EDGE_MARGIN_MM
            if ox1 <= ox0 or oy1 <= oy0:  # outline smaller than its own margin
                return (ox0, oy0, ox0 + self.board_side, oy0 + self.board_side)
        placed = np.isfinite(ir.inst_x) & np.isfinite(ir.inst_y)
        if not placed.any():
            return (ox0, oy0, ox1, oy1)
        pad = self.board_side
        bx0 = max(ox0, float(ir.inst_x[placed].min()) - pad)
        by0 = max(oy0, float(ir.inst_y[placed].min()) - pad)
        bx1 = min(ox1, float(ir.inst_x[placed].max()) + pad)
        by1 = min(oy1, float(ir.inst_y[placed].max()) + pad)
        return (bx0, by0, max(bx1, bx0), max(by1, by0))

    def bounds_for(self, inst: int) -> tuple[float, float, float, float]:
        """Where instance ``inst``'s CENTRE may go: the board domain
        shrunk by that instance's own keep-out radius, so its outermost
        pad lands inside the domain rather than its centre. Degenerate
        (a part wider than the board) collapses to the domain centre
        rather than inverting."""
        x0, y0, x1, y1 = self._placement_bounds
        r = float(self._keepout_r[inst])
        if x1 - x0 < 2 * r:
            x0 = x1 = (x0 + x1) / 2.0
        else:
            x0, x1 = x0 + r, x1 - r
        if y1 - y0 < 2 * r:
            y0 = y1 = (y0 + y1) / 2.0
        else:
            y0, y1 = y0 + r, y1 - r
        return (x0, y0, x1, y1)

    def _rigid_members(self, inst: int) -> tuple[int, ...]:
        """``inst``'s whole rigid body — every OTHER instance that must
        move with it (a ``"group"``/``"pattern"`` declaration,
        :attr:`~precis.pcb.ir.PcbIR.inst_group`), or just ``(inst,)`` when
        it names no group — the one place TRANSLATE/ROTATE/SWAP's move
        generators resolve "which instance did I pick" into "which
        instances does this move actually touch"."""
        gid = int(self.ir.inst_group[inst])
        if gid < 0:
            return (inst,)
        return self._group_members[gid]

    def _placement_is_legal(
        self,
        proposals: Sequence[tuple[int, float, float]],
        *,
        rotations: dict[int, float] | None = None,
    ) -> bool:
        """True iff every proposed ``(instance, x, y)`` sits inside
        :attr:`_placement_bounds`, clears every other instance's keep-out
        (including the other proposals in the same move — SWAP moves two
        parts at once, a rigid-group TRANSLATE/ROTATE/SWAP more), AND
        clears every :attr:`_hole_polys` mounting hole — a categorical
        obstacle the same way another instance's courtyard is, not merely
        a cost to trade off.

        **The keep-out is per-instance, not a constant.** Two parts must
        be at least ``r_i + r_j`` apart where ``r`` is
        :attr:`_keepout_r` — the part's own land-pattern extent, floored
        at the nominal courtyard. A fixed
        :data:`~precis.pcb.cost.COURTYARD_MIN_SEPARATION_MM` (2.0mm) is
        not conservative-but-imprecise here, it is *wrong*: a 14-pin
        dual-row land pattern reaches 2.27mm from its own centre, so two
        of them at the nominal 2.0mm have interleaved pads — copper of
        different nets starting from the same coordinate. That was
        measured as 4 residual clearance errors that survived an
        occupancy-grid router which is otherwise incapable of producing
        one.

        Placement move generators call this and return ``None`` rather
        than offering the annealer an illegal state. The graded
        ``courtyard_overlap`` cost term stays: it steers the search away
        from *tight* packing, this only forbids the categorical violation
        it cannot price (a run with the term active still settled with 10
        overlapping pairs — a penalty is a price, and the search paid it).
        """
        ir = self.ir
        keepout = self._keepout_r
        moving = [inst for inst, _, _ in proposals]
        rotations = rotations or {}
        proposed_poly = {
            inst: self._world_courtyard(inst, x, y, rotations.get(inst))
            for inst, x, y in proposals
        }
        for i, (inst, x, y) in enumerate(proposals):
            x0, y0, x1, y1 = self.bounds_for(inst)
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                return False
            dx, dy = ir.inst_x - x, ir.inst_y - y
            d2 = dx * dx + dy * dy
            d2[moving] = math.inf  # a part never collides with its own old slot
            sep = keepout + keepout[inst]
            # Broad phase, then exact. NaN (unplaced) compares False —
            # correct, and it also keeps an unplaced part's NaN pose out of
            # `_world_courtyard` below, which would otherwise produce a
            # polygon of NaNs that no SAT axis can separate.
            near = np.nonzero(d2 < sep * sep)[0]
            for other in near:
                if convex_polygons_overlap(
                    proposed_poly[inst], self._world_courtyard(int(other))
                ):
                    return False
            for hole_idx, hole_poly in enumerate(self._hole_polys):
                hole = ir.mounting_holes[hole_idx]
                sep_h = keepout[inst] + self._hole_radius[hole_idx]
                if (x - hole.x) ** 2 + (y - hole.y) ** 2 >= sep_h * sep_h:
                    continue
                if convex_polygons_overlap(proposed_poly[inst], hole_poly):
                    return False
            for other, ox, oy in proposals[i + 1 :]:
                sep_ij = keepout[inst] + keepout[other]
                if (x - ox) ** 2 + (y - oy) ** 2 >= sep_ij * sep_ij:
                    continue
                if convex_polygons_overlap(proposed_poly[inst], proposed_poly[other]):
                    return False
        return True

    def _world_courtyard(
        self,
        inst: int,
        x: float | None = None,
        y: float | None = None,
        rot: float | None = None,
    ) -> list[tuple[float, float]]:
        """``inst``'s courtyard polygon in BOARD coordinates, at its
        current pose or at a proposed ``(x, y)`` / ``rot``.

        Through :func:`precis.pcb.landpattern.place_points`, the same
        affine path a PAD travels — a courtyard derived from pad geometry
        that rotated by a different convention would reserve space where
        the part's own copper is not, and look entirely plausible doing it.

        Mirroring is deliberately not modelled: ``PcbIR`` carries no
        per-instance board side, so this engine has always been
        side-agnostic (a circle was mirror-invariant, which is why the
        question never came up). For an asymmetric part on the bottom side
        the reserved area is its unmirrored twin — same extent, reflected.
        Fixing that needs a side on the IR, not a change here."""
        ir = self.ir
        px = float(ir.inst_x[inst]) if x is None else x
        py = float(ir.inst_y[inst]) if y is None else y
        prot = float(ir.inst_rot[inst]) if rot is None else rot
        prot = 0.0 if math.isnan(prot) else prot
        pose = (px, py, prot)
        cached = self._world_poly.get(inst)
        if cached is not None and cached[0] == pose:
            return cached[1]
        poly = place_points(self._keepout_poly[inst], cx=px, cy=py, rot_deg=prot)
        self._world_poly[inst] = (pose, poly)
        return poly

    def _rescan_after_move(self, moved_inst: int) -> None:
        """Refresh every cached term a single instance's move can affect.
        Called once per moved instance (translate/rotate: once; swap:
        twice, sequentially — see :meth:`apply_move`)."""
        ir = self.ir
        self._refresh_courtyard(moved_inst)
        self._refresh_alignment(moved_inst)
        self._refresh_measures(moved_inst)
        self._refresh_board_edge(moved_inst)

        direct = set(ir._segs_of_instance.get(moved_inst, ()))
        reassigned = {
            s
            for s in range(ir.n_segments)
            if s not in direct and int(self._seg_nearest_instance[s]) == moved_inst
        }
        to_search = direct | reassigned
        for s in to_search:
            self._refresh_gap(s)
        self._newly_closer_segments(moved_inst, to_search)

        for s in direct:
            if s in self._loop_applicable:
                lt = loop_inductance_term(ir, s, self.level, self.config.cost)
                # Applicability (does this connection carry a loop objective
                # at all) is position-independent — see `_loop_applicable`'s
                # construction in `_init_caches` — so a segment already in
                # that set can never newly evaluate to None here.
                assert lt is not None
                self._margin[("loop_inductance", s)] = lt
            # `crossings` depends only on `s`'s OWN two endpoints (component-
            # centroid granularity, same as loop_inductance above) — never
            # on another instance's proximity like gap_capacity's `reassigned`
            # set — so only `direct` segments ever need a recount, and their
            # layer never changes from a placement move (only LAYER_ASSIGN
            # touches `seg_layer`).
            layer = int(ir.seg_layer[s])
            self._recompute_seg_crossings(s, layer, layer)

        touched_candidates = direct & self._coupling_candidate_set
        for sa in touched_candidates:
            for sb in self._coupling_candidate_set:
                if sa == sb:
                    continue
                key = (sa, sb) if sa < sb else (sb, sa)
                if key not in self._coupling_pairs:
                    continue
                t = coupling_pair_term(ir, key[0], key[1], self.level, self.config.cost)
                if t is not None:
                    self._margin[("coupling", key)] = t

    def _rescan_segments(self, seg_ids: tuple[int, ...]) -> None:
        """Local refresh for LAYER_ASSIGN/SIDE_FLIP/PIN_SWAP: recompute
        the per-segment margin caches for exactly the touched segments —
        the segment-scoped analogue of :meth:`_rescan_after_move`'s
        per-instance version. No :meth:`_newly_closer_segments` scan
        here: none of these move kinds change any OTHER segment's
        nearest-obstacle distance (only a component translate does that —
        see that method's docstring), so only the touched segments
        themselves ever need a refresh."""
        for s in seg_ids:
            self._refresh_gap(s)
            if s in self._loop_applicable:
                lt = loop_inductance_term(self.ir, s, self.level, self.config.cost)
                assert lt is not None
                self._margin[("loop_inductance", s)] = lt
        touched = set(seg_ids) & self._coupling_candidate_set
        for sa in touched:
            for sb in self._coupling_candidate_set:
                if sa == sb:
                    continue
                key = (sa, sb) if sa < sb else (sb, sa)
                if key not in self._coupling_pairs:
                    continue
                t = coupling_pair_term(
                    self.ir, key[0], key[1], self.level, self.config.cost
                )
                if t is not None:
                    self._margin[("coupling", key)] = t

    def _rescan_net(self, net_id: int) -> None:
        """Local refresh for PLANE_PROMOTE/PLANE_DEMOTE: every segment of
        ``net_id`` (its ``gap_capacity`` value depends on
        ``net_plane_layers`` now — see :func:`precis.pcb.cost.
        gap_capacity_term`'s plane-exclusion branch, and its
        ``implied_via_count`` does too — a plane-promoted net's segments
        dog-bone instead of transitioning layers), scoped to that net's
        own segments only, never the whole board."""
        seg_ids = tuple(int(s) for s in np.flatnonzero(self.ir.seg_net == net_id))
        self._rescan_segments(seg_ids)
        for s in seg_ids:
            self._refresh_via_count_for_segment(s)

    # -- aggregate cost ----------------------------------------------------
    def money(self) -> float:
        return (
            sum(self._money_static_by_name.values())
            + self._money_board_area
            + self._money_measures
        )

    def risk(self) -> float:
        if not self._margin:
            return 0.0
        return max(
            _CRITICALITY_WEIGHT[_BY_NAME[name].criticality]
            * hardened_penalty(tv.raw, self.schedule)
            for (name, _key), tv in self._margin.items()
        )

    def total(self) -> float:
        return self.money() + self.config.cost.risk_to_money * self.risk()

    # -- move application ----------------------------------------------
    #: Kind -> does this move kind's payload live in the placement
    #: (instances/old/new) shape? Everything else is segment/net/pin
    #: shaped — see :class:`Move`'s docstring for the full breakdown.
    _PLACEMENT_KINDS = (MoveKind.TRANSLATE, MoveKind.ROTATE, MoveKind.SWAP)

    def apply_move(self, move: Move) -> None:
        self._dispatch(move, forward=True)

    def undo_move(self, move: Move) -> None:
        self._dispatch(move, forward=False)

    def _dispatch(self, move: Move, *, forward: bool) -> None:
        if move.kind in self._PLACEMENT_KINDS:
            self._apply_placement(move, move.new if forward else move.old)
        elif move.kind == MoveKind.LAYER_ASSIGN:
            self._apply_layer_assign(move, forward=forward)
        elif move.kind == MoveKind.SIDE_FLIP:
            self._apply_side_flip(move, forward=forward)
        elif move.kind == MoveKind.PLANE_PROMOTE:
            self._apply_plane_promote(move, forward=forward)
        elif move.kind == MoveKind.PLANE_DEMOTE:
            self._apply_plane_demote(move, forward=forward)
        elif move.kind == MoveKind.PIN_SWAP:
            self._apply_pin_swap(move, forward=forward)
        else:  # pragma: no cover — exhaustive over MoveKind by construction
            raise AssertionError(f"unhandled move kind {move.kind!r}")

    def _apply_placement(
        self, move: Move, coords: tuple[tuple[float, float, float], ...]
    ) -> None:
        for inst, (x, y, rot) in zip(move.instances, coords):
            self.ir.move_instance(inst, x=x, y=y, rot=rot)
            self._rescan_after_move(inst)
        self._refresh_board_area()

    def _apply_layer_assign(self, move: Move, *, forward: bool) -> None:
        seg = move.segments[0]
        old_layer = int(self.ir.seg_layer[seg])
        layer = move.new_int[0] if forward else move.old_int[0]
        self.ir.set_layer(seg, layer)
        self._recompute_seg_crossings(seg, old_layer, layer)
        self._rescan_segments((seg,))
        self._refresh_layer_count()
        self._refresh_via_count_for_segment(seg)

    def _apply_side_flip(self, move: Move, *, forward: bool) -> None:
        seg = move.segments[0]
        side = move.new_int[0] if forward else move.old_int[0]
        self.ir.set_side(seg, side)
        self._rescan_segments((seg,))

    def _apply_plane_promote(self, move: Move, *, forward: bool) -> None:
        # `_gen_plane_promote` only ever offers a bare (mask==0) net a
        # SINGLE new layer, so undo needs to remove only that one bit —
        # demoting the whole net (rather than just this move's layer)
        # would be wrong the day a locked/authored net could somehow
        # reach here with a second bit already set; this stays correct
        # either way since a bare net has nothing else to lose.
        assert move.net is not None
        if forward:
            self.ir.promote_plane(move.net, move.new_int[0])
        else:
            self.ir.demote_plane(move.net, move.new_int[0])
        self._rescan_net(move.net)
        self._refresh_layer_count()

    def _apply_plane_demote(self, move: Move, *, forward: bool) -> None:
        # Mirror of the above: `_gen_plane_demote` names the ONE layer bit
        # it is demoting (`move.old_int[0]`), so both directions touch only
        # that bit — any OTHER layer this net is poured on is untouched,
        # which is what makes a demote of one of several plane layers a
        # real, separately-reversible move rather than an all-or-nothing
        # one.
        assert move.net is not None
        if forward:
            self.ir.demote_plane(move.net, move.old_int[0])
        else:
            self.ir.promote_plane(move.net, move.old_int[0])
        self._rescan_net(move.net)
        self._refresh_layer_count()

    def _apply_pin_swap(self, move: Move, *, forward: bool) -> None:
        pairs = move.pin_pairs if forward else tuple(reversed(move.pin_pairs))
        touched: set[int] = set()
        for pin_a, pin_b in pairs:
            inst = int(self.ir.pin_instance[pin_a])
            for s in self.ir._segs_of_instance.get(inst, ()):
                a, b = int(self.ir.seg_pin_a[s]), int(self.ir.seg_pin_b[s])
                if pin_a in (a, b) or pin_b in (a, b):
                    touched.add(s)
            self.ir.swap_pins(pin_a, pin_b)
        self._rescan_segments(tuple(touched))

    # -- SA loop -----------------------------------------------------------
    def anneal(self, rng: random.Random) -> None:
        cfg = self.config
        t0 = cfg.t0 if cfg.t0 is not None else max(5.0, self.board_side / 2.0)
        temp = t0
        moves: list[MoveRecord] = []
        active_stage = _stage_index(cfg.schedule, 0.0)
        for it in range(cfg.iters):
            self.schedule = min(1.0, it / max(1, cfg.iters - 1))
            frac = it / max(1, cfg.iters)
            stage = _stage_index(cfg.schedule, frac)
            if stage != active_stage:
                # **Reheat at every schedule-stage boundary.** Without
                # this, a global exponential cool from `t0` over the WHOLE
                # `iters` budget is already near-zero (e.g. ~1e-22 x t0 by
                # 50% of a 20k-iteration run at the default
                # `cooling=0.995`) by the time a LATER stage's move kind
                # first becomes eligible. A newly-introduced kind whose
                # delta isn't exactly zero (LAYER_ASSIGN's `layer_count`
                # money step, PLANE_PROMOTE's) then can NEVER pay its
                # one-time entry cost and is silently frozen out for the
                # rest of the run. Reheating to `t0` at each boundary gives every stage's
                # newly-eligible move kinds the same fair, explorable
                # temperature budget the FIRST stage got, while cooling
                # still proceeds *within* a stage exactly as before — the
                # hardening (`self.schedule`, cost.py's convexity dial)
                # is untouched and keeps advancing monotonically across
                # the whole run regardless.
                temp = t0
                active_stage = stage
            # Re-baseline AFTER advancing the schedule, not before: the
            # hardening dial tightening on an existing (unresolved) margin
            # violation must never itself register as "this move made
            # things worse" — only the move's own marginal effect at the
            # *current* schedule should drive acceptance. `total()` is a
            # cheap scan over already-cached term values (module docstring
            # — aggregation, not physics), so recomputing it every
            # iteration doesn't touch the locality budget.
            cur_total = self.total()
            kind = _pick_move_kind(cfg.schedule, it, cfg.iters, rng)
            gen = MOVE_GENERATORS[kind]
            move = gen(self, rng, temp)
            if move is None:
                temp *= cfg.cooling
                continue
            self.apply_move(move)
            new_total = self.total()
            delta = new_total - cur_total
            accept = delta < 0 or rng.random() < math.exp(-delta / max(temp, 1e-6))
            refdes = tuple(str(self.ir.instance_refdes[i]) for i in move.instances)
            if accept:
                cur_total = new_total
                moves.append(MoveRecord(it, kind.value, refdes, True, delta))
            else:
                self.undo_move(move)
                moves.append(MoveRecord(it, kind.value, refdes, False, delta))
            temp *= cfg.cooling
        self.moves = moves

    # -- digest --------------------------------------------------------
    def _cell_label(self, x: float, y: float) -> str:
        cell = self.config.region_cell_mm
        return f"R{int(x // cell)}C{int(y // cell)}"

    def _midpoint(self, seg_id: int) -> tuple[float, float]:
        ir = self.ir
        ia, ib = int(self._seg_inst_a[seg_id]), int(self._seg_inst_b[seg_id])
        return (
            (float(ir.inst_x[ia]) + float(ir.inst_x[ib])) / 2.0,
            (float(ir.inst_y[ia]) + float(ir.inst_y[ib])) / 2.0,
        )

    def _region_for_key(self, term_name: str, key: MarginKey) -> str | None:
        if term_name in ("gap_capacity", "loop_inductance"):
            assert isinstance(key, int)
            x, y = self._midpoint(key)
            return self._cell_label(x, y)
        if term_name == "board_edge_clearance":
            assert isinstance(key, int)  # an instance id here, not a segment id
            return self._cell_label(
                float(self.ir.inst_x[key]), float(self.ir.inst_y[key])
            )
        if term_name == "coupling":
            assert isinstance(key, tuple)
            sa, sb = key
            xa, ya = self._midpoint(sa)
            xb, yb = self._midpoint(sb)
            return self._cell_label((xa + xb) / 2.0, (ya + yb) / 2.0)
        if term_name == "courtyard_overlap":
            assert isinstance(key, tuple)  # a sorted INSTANCE-id pair, not segments
            ia, ib = key
            xa, ya = float(self.ir.inst_x[ia]), float(self.ir.inst_y[ia])
            if ib >= _HOLE_KEY_BASE:
                # A mounting-hole entry (:data:`_HOLE_KEY_BASE`'s own
                # docstring) -- ``ib`` is not a real instance id, decode it
                # back into the hole it names instead of indexing `inst_x`
                # with it (which would silently read SOME real instance's
                # position, or raise, depending on board size).
                hole = self.ir.mounting_holes[ib - _HOLE_KEY_BASE]
                xb, yb = hole.x, hole.y
            else:
                xb, yb = float(self.ir.inst_x[ib]), float(self.ir.inst_y[ib])
            return self._cell_label((xa + xb) / 2.0, (ya + yb) / 2.0)
        if term_name == "crossings":
            # A layer, not a grid cell -- there is no single (x, y) home
            # for "all of layer k's crossings", so this uses the layer id
            # as its own region label rather than forcing it through
            # `_cell_label`. `_locked_refdes_in_region` simply finds no
            # matches for an "layerN" label, which is correct (a layer-
            # scoped row has no locked-INSTANCE story to tell).
            assert isinstance(key, int)
            return f"layer{key}"
        return None  # thermal_rise: no spatial home (position-independent)

    def _locked_refdes_in_region(self, region: str) -> list[str]:
        ir = self.ir
        out = []
        for i in range(ir.n_instances):
            x, y = float(ir.inst_x[i]), float(ir.inst_y[i])
            if math.isnan(x) or math.isnan(y):
                continue
            if self._cell_label(x, y) != region:
                continue
            if bool(ir.inst_fixed_xy[i]) or bool(ir.inst_fixed_rot[i]):
                out.append(str(ir.instance_refdes[i]))
        return out

    def digest(self) -> Digest:
        term_summaries: list[TermSummary] = []
        for name, val in self._money_static_by_name.items():
            term_summaries.append(
                TermSummary(name, "money", val, None, _BY_NAME[name].justification)
            )
        term_summaries.append(
            TermSummary(
                "board_area",
                "money",
                self._money_board_area,
                None,
                _BY_NAME["board_area"].justification,
            )
        )
        term_summaries.append(
            TermSummary(
                "measures", "money", self._money_measures, None, _MEASURES_JUSTIFICATION
            )
        )

        by_name: dict[str, list[tuple[str, float, str | None]]] = {}
        for (name, key), tv in self._margin.items():
            w = _CRITICALITY_WEIGHT[_BY_NAME[name].criticality]
            penalty = w * hardened_penalty(tv.raw, self.schedule)
            region = self._region_for_key(name, key)
            by_name.setdefault(name, []).append((tv.region, penalty, region))

        region_acc: dict[str, list[tuple[str, str, float]]] = {}
        for name, entries in by_name.items():
            peak_net, peak_penalty, peak_region = max(entries, key=lambda e: e[1])
            term_summaries.append(
                TermSummary(
                    name,
                    "margin",
                    peak_penalty,
                    peak_region,
                    _BY_NAME[name].justification,
                )
            )
            for net, penalty, region in entries:
                if region is None:
                    continue
                region_acc.setdefault(region, []).append((name, net, penalty))

        regions: list[RegionEntry] = []
        for region, region_entries in region_acc.items():
            term, _net, penalty = max(region_entries, key=lambda e: e[2])
            nets = tuple(sorted({e[1] for e in region_entries}))
            locked = tuple(sorted(self._locked_refdes_in_region(region)))
            regions.append(RegionEntry(region, term, penalty, nets, locked))
        regions.sort(key=lambda r: r.peak_penalty, reverse=True)

        move_counts: dict[str, int] = {}
        for m in self.moves:
            if m.accepted:
                move_counts[m.kind] = move_counts.get(m.kind, 0) + 1

        return Digest(
            total=self.total(),
            money=self.money(),
            risk=self.risk(),
            terms=tuple(term_summaries),
            regions=tuple(regions),
            move_counts=move_counts,
        )


# ── move generators (the registry slice 7 extends) ──────────────────────


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


#: How many draws a placement generator makes before conceding that this
#: iteration has no legal proposal (:meth:`OptimizeEngine._placement_is_
#: legal`). One draw would silently disable TRANSLATE for parts in a
#: crowded neighbourhood — the parts that most need to move.
_LEGALIZE_TRIES = 8


def _sample_delta_1d(rng: random.Random, step: float, lo: float, hi: float) -> float:
    """One axis of :func:`_gen_translate`'s proposed delta: a normal draw
    centred on 0 (today's fine, local-refinement behaviour) for the
    OVERWHELMING common case — an already-legal instance/group, where
    ``[lo, hi]`` straddles (or nearly straddles) 0 REGARDLESS of how wide
    the legal domain itself is (a part in the middle of a big board must
    still take small local steps, not uniform jumps across the whole
    domain on every draw — this is what a first version of this function
    got wrong: gating on raw domain WIDTH alone turned every ordinary
    TRANSLATE into a board-wide teleport and broke routing convergence on
    the esp32c3 reference board, which has no groups at all).

    Falls back to a UNIFORM draw across the WHOLE ``[lo, hi]`` corridor
    only when that corridor doesn't even reach the normal's typical
    span around 0 — i.e. the CURRENT position is already outside its own
    legal domain by more than the step can plausibly close in one draw.
    A badly-scattered seed (this module's own mounting-hole/pattern-tile
    sections) can put a legal corridor tens of millimetres away from 0
    entirely, and a normal that far out in its tail clamps to the SAME
    boundary value on nearly every draw, which turns ``_LEGALIZE_TRIES``
    retries into the identical proposal tried 8 times: if that one point
    happens to collide with anything, the move is dead until something
    ELSE moves out of its way first — measured on the nano fixture, a
    pattern tile whose only single-step-reachable corner sat on a
    neighbour never found a legal TRANSLATE in 2000 draws even after the
    corridor computation itself was fixed (:func:`_gen_translate`'s own
    docstring). Sampling UNIFORMLY across a corridor this far away gives
    every retry a genuinely different target instead of one degenerate,
    possibly-blocked point."""
    reach = 4.0 * step
    if lo > reach or hi < -reach:
        return rng.uniform(lo, hi)
    return _clamp(rng.gauss(0, step), lo, hi)


def _gen_translate(
    engine: OptimizeEngine, rng: random.Random, temp: float
) -> Move | None:
    movable = engine._movable_xy
    if not movable:
        return None
    ir = engine.ir
    inst = movable[rng.randrange(len(movable))]
    # A grouped/patterned instance drags its WHOLE rigid body along —
    # :meth:`OptimizeEngine._rigid_members` degrades to ``(inst,)`` for an
    # ungrouped instance, so this is exactly today's single-instance
    # TRANSLATE when there is no group at all.
    members = engine._rigid_members(inst)
    old = tuple(
        (float(ir.inst_x[m]), float(ir.inst_y[m]), float(ir.inst_rot[m]))
        for m in members
    )
    step = max(
        0.5, engine.config.translate_step_mm * (temp / max(engine.board_side, 1.0))
    )
    step = max(0.5, min(step, engine.config.translate_step_mm))
    # The delta range clamps to is the INTERSECTION of every member's OWN
    # `bounds_for`, not just the picked instance's -- a rigid body moves
    # every member by the SAME delta, so a delta that satisfies only the
    # picked instance's own (possibly wider) domain can still walk a
    # DIFFERENT, more tightly-bounded member (a bigger part, or one
    # nearer the outline) straight out of ITS legal range. Measured on
    # the nano fixture's own "channel" pattern tiles: clamping against
    # only the picked member's bounds left every proposal illegal
    # forever whenever that member's own domain was wider than a
    # group-mate's -- 0 legal TRANSLATE/ROTATE proposals found in 2000
    # draws for a tile whose four members' keep-out radii differ (a
    # screw-terminal vs. a diode), even though the tile itself is
    # perfectly capable of moving. Degrades to exactly the single-
    # instance clamp when ``members == (inst,)``.
    dx_lo = dx_hi = dy_lo = dy_hi = None
    for m, (mx, my, _mrot) in zip(members, old):
        mx0, my0, mx1, my1 = engine.bounds_for(m)
        lo_x, hi_x = mx0 - mx, mx1 - mx
        lo_y, hi_y = my0 - my, my1 - my
        dx_lo = lo_x if dx_lo is None else max(dx_lo, lo_x)
        dx_hi = hi_x if dx_hi is None else min(dx_hi, hi_x)
        dy_lo = lo_y if dy_lo is None else max(dy_lo, lo_y)
        dy_hi = hi_y if dy_hi is None else min(dy_hi, hi_y)
    assert dx_lo is not None and dx_hi is not None
    assert dy_lo is not None and dy_hi is not None
    if dx_lo > dx_hi or dy_lo > dy_hi:
        return None  # no shared delta can keep every member in its own domain
    # Retry a few draws before giving up: a single rejected sample would
    # make TRANSLATE effectively unavailable for a part in a crowded
    # neighbourhood, which is exactly where it is most needed.
    for _ in range(_LEGALIZE_TRIES):
        dx = _sample_delta_1d(rng, step, dx_lo, dx_hi)
        dy = _sample_delta_1d(rng, step, dy_lo, dy_hi)
        # The SAME (dx, dy) delta for every member -- a rigid body
        # translates as one, it doesn't re-derive each member's own step
        # independently (which could tear the group apart).
        new = tuple((mx + dx, my + dy, mrot) for mx, my, mrot in old)
        proposals = [(m, x, y) for m, (x, y, _r) in zip(members, new)]
        if engine._placement_is_legal(proposals):
            return Move(MoveKind.TRANSLATE, members, old, new)
    return None


def _gen_rotate(engine: OptimizeEngine, rng: random.Random, temp: float) -> Move | None:
    movable = engine._movable_rot
    if not movable:
        return None
    ir = engine.ir
    inst = movable[rng.randrange(len(movable))]
    members = engine._rigid_members(inst)
    old = tuple(
        (float(ir.inst_x[m]), float(ir.inst_y[m]), float(ir.inst_rot[m]))
        for m in members
    )
    delta = rng.choice((90.0, -90.0))
    # For a single (ungrouped) instance the centroid IS the instance's own
    # position, so rotating "the group" about it moves nothing — the
    # degenerate case that keeps this identical to the pre-group ROTATE.
    # For a real group, every member's CENTRE swings around the group's
    # own centroid (the same clockwise-from-north convention
    # `landpattern.rotate_offset` already uses for a footprint-local pad
    # offset, reused here for a board-space one) while its OWN rotation
    # also advances by `delta` -- a rigid body's orientation and its
    # members' positions turn together.
    cx = sum(x for x, _y, _r in old) / len(old)
    cy = sum(y for _x, y, _r in old) / len(old)
    new_list: list[tuple[float, float, float]] = []
    for x, y, rot in old:
        rdx, rdy = rotate_offset(x - cx, y - cy, delta)
        new_list.append((cx + rdx, cy + rdy, (rot + delta) % 360.0))
    new = tuple(new_list)
    # **A rotation can now make a placement illegal, and could not before.**
    # This move went unchecked while the keep-out was a CIRCLE, correctly:
    # a disc's reserved area is rotation-invariant, so spinning a part
    # could never bring it into a neighbour. A courtyard polygon is not —
    # swing an oblong part's long axis toward the part beside it and the
    # two overlap. Nothing downstream would have caught it either: no
    # cost term reads `inst_rot` (this module's "ROTATE is cost-neutral,
    # provably" note), so `delta` is exactly 0.0 and `anneal` accepts
    # every generated rotation unconditionally — the violation would
    # surface only in a later DRC run, as a `courtyard_overlap` the
    # annealer itself had no way to reject.
    proposals = [(m, x, y) for m, (x, y, _r) in zip(members, new)]
    rotations = {m: r for m, (_x, _y, r) in zip(members, new)}
    if not engine._placement_is_legal(proposals, rotations=rotations):
        return None
    return Move(MoveKind.ROTATE, members, old, new)


def _gen_swap(engine: OptimizeEngine, rng: random.Random, temp: float) -> Move | None:
    movable = engine._movable_xy
    if len(movable) < 2:
        return None
    ir = engine.ir
    ia, ib = rng.sample(movable, 2)
    ga, gb = int(ir.inst_group[ia]), int(ir.inst_group[ib])

    if ga == -1 and gb == -1:
        old_a = (float(ir.inst_x[ia]), float(ir.inst_y[ia]), float(ir.inst_rot[ia]))
        old_b = (float(ir.inst_x[ib]), float(ir.inst_y[ib]), float(ir.inst_rot[ib]))
        new_a = (old_b[0], old_b[1], old_a[2])
        new_b = (old_a[0], old_a[1], old_b[2])
        # Both destinations are existing, already-legal slots, so this can
        # only fail when one of them lies outside the placement bounds (a
        # seeded part the domain doesn't cover) — worth checking rather
        # than assuming, since accepting it would strand a part off-board.
        if not engine._placement_is_legal(
            ((ia, new_a[0], new_a[1]), (ib, new_b[0], new_b[1]))
        ):
            return None
        return Move(MoveKind.SWAP, (ia, ib), (old_a, old_b), (new_a, new_b))

    # A grouped instance may ONLY swap against a DIFFERENT group of the
    # SAME pattern (two congruent tiles trading anchors, task spec's
    # explicit carve-out) — never against an ungrouped instance, its own
    # group-mate, or a group with no pattern (or a different one). Every
    # other combination is refused outright rather than offered and
    # relied on `_placement_is_legal` to reject, since a mixed swap is
    # not merely unlikely to fit, it is not a move this design admits at
    # all (see MoveKind.SWAP's own docstring note).
    if ga == -1 or gb == -1 or ga == gb:
        return None
    pattern_a, pattern_b = ir.group_pattern[ga], ir.group_pattern[gb]
    if not pattern_a or pattern_a != pattern_b:
        return None
    members_a = engine._group_members[ga]
    members_b = engine._group_members[gb]
    if len(members_a) != len(members_b):
        return None  # an uneven/malformed pair of tiles -- never offered

    old = tuple(
        (float(ir.inst_x[m]), float(ir.inst_y[m]), float(ir.inst_rot[m]))
        for m in members_a + members_b
    )
    # Congruent tiles trade anchors: A's members take B's exact poses and
    # vice versa, index-aligned by the SAME sorted-instance-id
    # correspondence :func:`seed_placement`'s tiling stamp establishes —
    # two tiles built from that stamp always have matching internal
    # layout at matching index, so this is a straight positional
    # exchange, not a re-derivation of which member plays which role.
    new = tuple(
        (float(ir.inst_x[m]), float(ir.inst_y[m]), float(ir.inst_rot[m]))
        for m in members_b + members_a
    )
    instances = members_a + members_b
    proposals = [(m, x, y) for m, (x, y, _r) in zip(instances, new)]
    rotations = {m: r for m, (_x, _y, r) in zip(instances, new)}
    if not engine._placement_is_legal(proposals, rotations=rotations):
        return None
    return Move(MoveKind.SWAP, instances, old, new)


def _gen_layer_assign(
    engine: OptimizeEngine, rng: random.Random, temp: float
) -> Move | None:
    ir = engine.ir
    if ir.n_segments == 0 or len(engine._signal_layers) < 2:
        return None
    seg = rng.randrange(ir.n_segments)
    old_layer = int(ir.seg_layer[seg])
    choices = [layer for layer in engine._signal_layers if layer != old_layer]
    if not choices:
        return None
    new_layer = choices[rng.randrange(len(choices))]
    return Move(
        MoveKind.LAYER_ASSIGN,
        segments=(seg,),
        old_int=(old_layer,),
        new_int=(new_layer,),
    )


def _gen_side_flip(
    engine: OptimizeEngine, rng: random.Random, temp: float
) -> Move | None:
    """Toggles ``seg_side`` between the two-value placeholder vocabulary
    (0/1) — the "exact sketch anchor vocabulary" is an explicitly open
    backlog item ("settle in a short design note inside sketch.py's
    docstring... with the reference-board tests as the arbiter") that
    ``sketch.py`` (not this slice) owns; a binary toggle is the minimal,
    honest placeholder that exercises the move class without pretending
    to have resolved that open question."""
    ir = engine.ir
    if ir.n_segments == 0:
        return None
    seg = rng.randrange(ir.n_segments)
    old_side = int(ir.seg_side[seg])
    new_side = 0 if old_side else 1
    return Move(
        MoveKind.SIDE_FLIP, segments=(seg,), old_int=(old_side,), new_int=(new_side,)
    )


def _gen_plane_promote(
    engine: OptimizeEngine, rng: random.Random, temp: float
) -> Move | None:
    ir = engine.ir
    if not engine._plane_layers:
        return None
    # **A plane needs somewhere to be poured, and that is the OUTLINE.**
    # `realize.py` pours into the board profile, so on a design with no
    # outline feature every promoted net comes back
    # ``failed: unpourable_plane`` ("no outline to pour a plane into, so
    # this connection has nowhere to land") — the promotion cannot be
    # satisfied by any amount of routing effort. Offering it anyway lets
    # the anneal accept a move that silently converts a perfectly
    # routable 2-pin net into a permanently failed one, which is what it
    # did: found 2026-08-30 when a placement change shifted the cost
    # landscape enough to make this promotion attractive on a 3-part
    # fixture that had always been routable. Latent since PLANE_PROMOTE
    # existed, not caused by that change.
    if not ir.outline or len(ir.outline) < 3:
        return None
    # A locked net is never itself a promotion candidate (it already has
    # its human-fixed assignment) — see `OptimizeConfig.locked_plane_nets`'s
    # docstring for why this is a hard constraint, not a move the search
    # may merely disfavour. Its OWN plane layer(s) still show up in the
    # `taken` set below unchanged (its `net_plane_layers` bits are left
    # alone by every generator), so a locked net's plane layer(s) stay
    # correctly unavailable to any other net without this function needing
    # a second, separate exclusion for it.
    #
    # `net_plane_layers[n] == 0` (candidate = a net with NO plane layer
    # yet) rather than "any net not fully covered" is deliberate: this
    # generator only ever gives a still-unpromoted net its FIRST plane
    # layer. A net that already carries one (via a prior PLANE_PROMOTE
    # move here, or an authored `op='plane_net'` row) never gets a second
    # one offered automatically — multi-layer fill is reachable only
    # through the authored path (`handlers/pcb.py::_op_plane_net`, called
    # once per desired layer), never invented by the search on its own.
    locked = engine.config.locked_plane_nets
    candidates = [
        n
        for n in range(ir.n_nets)
        if int(ir.net_plane_layers[n]) == 0 and n not in locked
    ]
    if not candidates:
        return None
    # A plane layer carries ONE net. It is a sheet of copper, and two nets
    # cannot both be it — this is a hard physical constraint, not a
    # preference the annealer may pay for. Without the filter the search
    # cheerfully promoted nine nets onto two plane layers, which reads as a
    # legal state everywhere: `net_plane_layers` is per-net so it can
    # represent the contradiction, and every consumer that maps layer->net
    # silently keeps the last writer. Measured on seed 3 before this
    # filter: VBUS, VCC3V3, SDA, SCL, EN, GPIO2, GPIO9, TXD and J1_P7 all
    # promoted, two pours emitted, and every one of the other seven nets
    # left as pads and stubs connected to nothing.
    #
    # A layer is "taken" the moment ANY net has that bit set, not just
    # when a net's WHOLE mask equals it — a net may now legitimately have
    # several bits set (one net, several layers), and every one of those
    # bits still makes its layer unavailable to every OTHER net.
    taken: set[int] = set()
    for n in range(ir.n_nets):
        taken.update(plane_layers_of(int(ir.net_plane_layers[n])))
    free = [layer for layer in engine._plane_layers if layer not in taken]
    if not free:
        return None
    net = candidates[rng.randrange(len(candidates))]
    layer = free[rng.randrange(len(free))]
    return Move(MoveKind.PLANE_PROMOTE, net=net, new_int=(layer,))


def _gen_plane_demote(
    engine: OptimizeEngine, rng: random.Random, temp: float
) -> Move | None:
    ir = engine.ir
    # A locked net's assignment is a constraint, not a hint (see
    # `OptimizeConfig.locked_plane_nets`'s docstring for the measured
    # defect this closes: an authored plane demoted the instant this move
    # was offered it, since nothing here distinguished "the search's own
    # exploration" from "the one thing the caller said not to explore
    # away from").
    # One (net, layer) candidate per bit an unlocked net has set — today
    # an unlocked (search-derived) net never carries more than one bit
    # (`_gen_plane_promote` only ever gives a bare net its first layer),
    # so this degrades to the old one-candidate-per-net list exactly; the
    # per-bit shape is what stays correct the day a derived net legally
    # carries several (nothing here assumes "at most one").
    locked = engine.config.locked_plane_nets
    candidates = [
        (n, layer)
        for n in range(ir.n_nets)
        if n not in locked
        for layer in plane_layers_of(int(ir.net_plane_layers[n]))
    ]
    if not candidates:
        return None
    net, old_layer = candidates[rng.randrange(len(candidates))]
    return Move(MoveKind.PLANE_DEMOTE, net=net, old_int=(old_layer,))


def _gen_pin_swap(
    engine: OptimizeEngine, rng: random.Random, temp: float
) -> Move | None:
    """The "big one" (task instruction): rather than proposing a random
    pair and letting Metropolis sort it out (as TRANSLATE/ROTATE/SWAP
    do), this solves :func:`precis.pcb.pinswap.propose_reassignment`'s
    min-cost bipartite matching directly — a deterministic sub-solve, not
    a random walk, per the backlog's "polynomial and fast, likely cheaper
    than the annealing around it". Degrades to always-``None`` when
    ``config.pin_swap_groups`` is empty (the default) — see that field's
    docstring."""
    groups = engine.config.pin_swap_groups
    if not groups:
        return None
    group = groups[rng.randrange(len(groups))]
    pairs = pinswap.propose_reassignment(engine.ir, group)
    if not pairs:
        return None
    return Move(MoveKind.PIN_SWAP, pin_pairs=pairs)


MOVE_GENERATORS: dict[MoveKind, MoveGeneratorFn] = {
    MoveKind.TRANSLATE: _gen_translate,
    MoveKind.ROTATE: _gen_rotate,
    MoveKind.SWAP: _gen_swap,
    MoveKind.LAYER_ASSIGN: _gen_layer_assign,
    MoveKind.SIDE_FLIP: _gen_side_flip,
    MoveKind.PLANE_PROMOTE: _gen_plane_promote,
    MoveKind.PLANE_DEMOTE: _gen_plane_demote,
    MoveKind.PIN_SWAP: _gen_pin_swap,
}


def _active_weights(
    schedule: tuple[ScheduleStage, ...], frac: float
) -> dict[MoveKind, float]:
    for stage in schedule:
        if frac <= stage.through_fraction:
            return stage.weights
    return schedule[-1].weights


def _stage_index(schedule: tuple[ScheduleStage, ...], frac: float) -> int:
    """Which :class:`ScheduleStage` (by position) is active at ``frac`` —
    :meth:`OptimizeEngine.anneal`'s reheat trigger: a change in this
    value is a change in the *move-kind mix*, the moment a stage's newly-
    eligible kinds need a fair temperature budget (see that method for
    why)."""
    for i, stage in enumerate(schedule):
        if frac <= stage.through_fraction:
            return i
    return len(schedule) - 1


def _pick_move_kind(
    schedule: tuple[ScheduleStage, ...], it: int, iters: int, rng: random.Random
) -> MoveKind:
    frac = it / max(1, iters)
    weights = _active_weights(schedule, frac)
    kinds = list(weights.keys())
    w = list(weights.values())
    return rng.choices(kinds, weights=w, k=1)[0]


# ── the convenience entry point ──────────────────────────────────────────


def optimize(
    ir: PcbIR, config: OptimizeConfig | None = None, *, reseed: bool = False
) -> OptimizeResult:
    """Seed (if needed) + anneal. ``reseed=False`` (the default) only
    seeds instances with no position yet — a re-optimize call from the
    LLM's "re-anneal from current state with adjusted weights/locks" lever
    (backlog) keeps whatever placement already exists. ``reseed=True``
    discards positions and reseeds everyone (a fresh run).

    ``cost_before``/``cost_after`` are both measured at the **same**
    hardening-schedule value (the run's final one) — not, respectively, at
    schedule 0 and schedule 1. ``hardened_penalty`` is deliberately
    steeper at a later schedule for the *same* raw fraction (that is the
    whole point of driving the schedule during the anneal's own Metropolis
    acceptance), so comparing a schedule-0 "before" against a schedule-1
    "after" would conflate genuine placement improvement with the barrier
    simply having tightened — an easy way to make a correctly-improving
    anneal look like it made things worse. Holding the reporting schedule
    fixed is what makes ``cost_after < cost_before`` mean what it says.
    """
    config = config or OptimizeConfig()
    rng = random.Random(config.seed)
    seed_placement(
        ir,
        rng,
        max_cluster_size=config.max_cluster_size,
        pitch_mm=config.seed_pitch_mm,
        force=reseed,
    )
    engine = OptimizeEngine(ir, config)
    report_schedule = 1.0 if config.iters > 1 else 0.0
    engine.schedule = report_schedule
    cost_before = engine.total()
    engine.schedule = 0.0
    engine.anneal(rng)
    engine.schedule = report_schedule
    cost_after = engine.total()
    # Cosmetic, post-measurement: `cost_before`/`cost_after` grade the
    # anneal's own work; the rigid recentre below changes absolute
    # coordinates only (routing is translation-invariant, the engine is
    # not consulted again).
    recentre_in_outline(ir)
    positions = {
        str(ir.instance_refdes[i]): (
            float(ir.inst_x[i]),
            float(ir.inst_y[i]),
            float(ir.inst_rot[i]),
        )
        for i in range(ir.n_instances)
    }
    return OptimizeResult(
        seed=config.seed,
        iters=config.iters,
        cost_before=cost_before,
        cost_after=cost_after,
        digest=engine.digest(),
        moves=tuple(engine.moves),
        positions=positions,
    )


# ── TOON digest rendering ────────────────────────────────────────────────


def digest_toon(result: OptimizeResult) -> str:
    """The TOON-style summary the tool surface returns: a one-line
    scalar header, then a per-term table, then a per-region table
    (worst-first) — legibility as a requirement, not a hope (backlog)."""
    accepted = sum(result.digest.move_counts.values())
    header = (
        f"total={result.cost_after:.4f} money={result.digest.money:.4f} "
        f"risk={result.digest.risk:.4f} before={result.cost_before:.4f} "
        f"accepted={accepted}/{result.iters} "
        f"moves={','.join(f'{k}:{v}' for k, v in sorted(result.digest.move_counts.items()))}"
    )
    term_rows = [
        {
            "name": t.name,
            "family": t.family,
            "value": round(t.value, 6),
            "peak_region": t.peak_region or "",
            "why": t.justification,
        }
        for t in result.digest.terms
    ]
    terms_table = toon.dump(
        term_rows, schema=["name", "family", "value", "peak_region", "why"]
    )
    if result.digest.regions:
        region_rows = [
            {
                "region": r.region,
                "peak_term": r.peak_term,
                "peak_penalty": round(r.peak_penalty, 6),
                "nets": ",".join(r.nets),
                "locked": ",".join(r.locked_instances),
            }
            for r in result.digest.regions
        ]
        regions_table = toon.dump(
            region_rows,
            schema=["region", "peak_term", "peak_penalty", "nets", "locked"],
        )
    else:
        regions_table = "(no margin-bearing regions)"
    return "\n\n".join([header, terms_table, regions_table])


__all__ = [
    "DEFAULT_SCHEDULE",
    "MOVE_GENERATORS",
    "Digest",
    "Move",
    "MoveKind",
    "MoveRecord",
    "OptimizeConfig",
    "OptimizeEngine",
    "OptimizeResult",
    "PinSwapGroup",
    "RegionEntry",
    "ScheduleStage",
    "TermSummary",
    "digest_toon",
    "optimize",
    "recentre_in_outline",
    "seed_placement",
]

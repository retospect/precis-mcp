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
  ``seg_layer``/``net_plane_layer``/``inst_extended_part``/``net_class``,
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
  floor(y / cell)`` with ``cell == cost.COURTYARD_MIN_SEPARATION_MM`` —
  cell size equal to the interaction radius makes the 3x3-neighbourhood
  query (:meth:`OptimizeEngine._courtyard_candidates_near`) exact.
  :meth:`OptimizeEngine._refresh_courtyard` is the bounded delta (discard
  cached partnerships, relocate in grid, retest 3x3 neighbourhood) —
  call-order-independent for a multi-instance move (SWAP). Only
  overlapping pairs are cached.
- ``board_edge_clearance`` (gr267456): depends only on the moved instance
  + ``ir.outline`` — O(1) delta, no grid: :meth:`OptimizeEngine.
  _refresh_board_edge` re-evaluates
  :func:`~precis.pcb.cost.board_edge_clearance_term` for that instance.
  When ``ir.outline`` exists, ``_placement_bounds`` (via
  :func:`~precis.pcb.cost.outline_bbox`, the same approximation the cost
  term uses) replaces the synthetic ``(0, board_side)`` TRANSLATE clamp
  square, capped to ``board_side`` per axis (anchored at the outline's
  min corner) rather than uncapped: an uncapped clamp against an
  oversized/placeholder outline more than quadrupled DRC errors on the
  ESP32-C3 reference fixture, because the cooling schedule (still
  ``board_side``-derived, not outline-aware) lets a component drift too
  far to walk back once hardened.

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
from precis.pcb.geom import segments_cross
from precis.pcb.ir import (
    UNSET_LAYER,
    Level,
    PcbIR,
    instance_keepout_radius_mm,
    nearest_other_instance,
    segment_points,
)
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
      ``(x, y, rot)`` triples in ``old``/``new``.
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
#: ``net_plane_layer``) AND ``gap_capacity`` (the plane-exclusion branch)
#: respond to those moves — see :meth:`OptimizeEngine._refresh_layer_count`,
#: :meth:`OptimizeEngine._recompute_seg_crossings` and
#: :func:`precis.pcb.cost.gap_capacity_term`. PIN_SWAP's effect is real
#: but measured entirely by :mod:`precis.pcb.pinswap`'s own crossing
#: evaluator (over real per-pin geometry via
#: :func:`precis.pcb.pinswap.offsets_from_pads` when supplied), never by
#: ``total()`` — no ``cost.py`` term reads pin identity or sub-instance
#: pad position.


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

    ``force=False`` (the default, "re-anneal from current state" — the
    LLM's lever per the backlog) only seeds instances with no position yet
    (``nan``); already-placed instances keep their coordinates.
    ``force=True`` reseeds everyone. Either way, every mutation goes
    through :meth:`PcbIR.move_instance` — never a direct array write —
    honouring the "only sanctioned way to change state" contract
    ``ir.py`` documents.
    """
    clusters = _cluster_instances(ir, max_cluster_size=max_cluster_size)
    clusters.sort(key=len, reverse=True)
    radii = instance_keepout_radius_mm(
        ir, min_radius_mm=max(COURTYARD_MIN_SEPARATION_MM / 2.0, pitch_mm / 8.0)
    )
    order = [inst for members in clusters for inst in members]
    # A square-ish shelf region: total footprint area, with slack for the
    # wrap waste an unsorted (adjacency-ordered, not size-ordered) shelf
    # pack leaves behind.
    total_area = float(sum((2.0 * radii[i]) ** 2 for i in order))
    row_width = max(2.0 * float(radii.max()), math.sqrt(total_area) * 1.2)

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
    if ir.outline and len(ir.outline) >= 3:
        ox0, oy0, ox1, oy1 = outline_bbox(ir.outline)
        origin_x, origin_y = ox0 + _EDGE_MARGIN_MM, oy0 + _EDGE_MARGIN_MM
        usable = (ox1 - _EDGE_MARGIN_MM) - origin_x
        if usable > 2.0 * float(radii.max()):
            row_width = min(row_width, usable)

    shelf_x = origin_x
    shelf_y = origin_y
    shelf_h = 0.0
    positions: dict[int, tuple[float, float]] = {}
    for inst in order:
        diameter = 2.0 * float(radii[inst])
        if shelf_x > origin_x and shelf_x + diameter > origin_x + row_width:
            shelf_x, shelf_y, shelf_h = origin_x, shelf_y + shelf_h, 0.0
        positions[inst] = (
            shelf_x + radii[inst] + _SEED_EPSILON_MM,
            shelf_y + radii[inst] + _SEED_EPSILON_MM,
        )
        shelf_x += diameter + _SEED_EPSILON_MM
        shelf_h = max(shelf_h, diameter + _SEED_EPSILON_MM)

    for members in clusters:
        for inst in members:
            x, y = positions[inst]
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
            elif rot is not None:
                ir.move_instance(inst, rot=rot)


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
        self._movable_xy = [i for i in range(n) if not bool(ir.inst_fixed_xy[i])]
        self._movable_rot = [i for i in range(n) if not bool(ir.inst_fixed_rot[i])]
        self.board_side = max(20.0, 6.0 * math.sqrt(max(n, 1)))
        #: TRANSLATE's clamp bounds (module docstring's board_edge_
        #: clearance section) — the real outline's bounding box when the
        #: design has authored one (:func:`~precis.pcb.cost.outline_bbox`,
        #: the SAME approximation the ``board_edge_clearance`` cost term
        #: uses), else the synthetic origin-anchored square this engine
        #: has always used, unchanged for a design with no outline.
        #:
        #: **Capped to ``board_side`` per axis, anchored at the outline's
        #: own min corner (never at the synthetic square's origin).**
        #: Clamping TRANSLATE to an oversized/placeholder outline instead
        #: of capping to ``board_side`` inflates DRC errors: the anneal's
        #: cooling schedule (``t0``/step size, both derived from
        #: this SAME ``board_side``, module-docstring-documented as an
        #: n-instance heuristic, not an outline-aware one) stays exactly
        #: as "hot" and fast-cooling as it always has, so a component that
        #: randomly drifts far during the still-permissive early schedule
        #: has no realistic way to walk back within the fixed iteration
        #: budget once the schedule hardens -- classic SA
        #: schedule-vs-search-domain mismatch, not a defect in the clamp
        #: mechanism itself. Capping means a genuinely oversized/placeholder
        #: outline can never make the search domain LARGER than what this
        #: engine's own schedule was calibrated for, while a real,
        #: comparably- (or more tightly-) sized outline still governs and
        #: gets real prevention. Re-tuning ``t0``/step size to scale with
        #: the actual placement-bounds diagonal (rather than capping) is
        #: the more complete fix and is flagged, not silently guessed at,
        #: as its own follow-up.
        #: **The domain must CONTAIN the seed.** Capping at ``board_side``
        #: anchored on the outline's own corner is not sufficient by
        #: itself: ``seed_placement`` drops clusters on a fixed-pitch
        #: grid that can span well past ``board_side`` for a dense
        #: design. A seeded part outside the cap gets clamped into the
        #: corner by its first TRANSLATE, and SWAP can never move it at
        #: all (a swap whose partner sits outside the bounds is rejected
        #: forever) — a pile-up that produces overlapping courtyards.
        #: The domain is therefore derived from the SEED's own extent
        #: (adjacency-clustered, so it is a real answer, not a heuristic
        #: square), padded, then clipped to the outline inset by
        #: :data:`_EDGE_MARGIN_MM`.
        #: Per-instance placement keep-out RADIUS: the part's own outermost
        #: pad plus a pad-to-pad breathing gap, floored at the nominal
        #: half-courtyard so a pinless part (mounting hole, fiducial) still
        #: occupies space — :func:`~precis.pcb.ir.instance_keepout_radius_mm`,
        #: the ONE formula every keep-out consumer (this engine, its own
        #: seeder, and the DRC courtyard geometry
        #: :mod:`precis.handlers.pcb` builds) shares, so a part's placement
        #: legality and its DRC courtyard radius cannot silently drift
        #: apart into two different numbers for the same part. Two
        #: instances are legal when their centres are at least the SUM of
        #: their radii apart — see :meth:`_placement_is_legal` for why a
        #: single constant is not merely coarse but incorrect.
        self._keepout_r = instance_keepout_radius_mm(
            ir, min_radius_mm=COURTYARD_MIN_SEPARATION_MM / 2.0
        )
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
        self._signal_layers = [
            i for i, layer in enumerate(ir.stackup) if layer.get("role") == "signal"
        ] or list(range(ir.n_layers))
        self._plane_layers = [
            i for i, layer in enumerate(ir.stackup) if layer.get("role") == "plane"
        ]

        self._init_caches()
        self._init_crossing_state()
        self._init_courtyard_state()
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
        cell = COURTYARD_MIN_SEPARATION_MM
        return (math.floor(x / cell), math.floor(y / cell))

    def _courtyard_candidates_near(self, inst: int) -> set[int]:
        """Every OTHER instance that could possibly be within
        ``COURTYARD_MIN_SEPARATION_MM`` of ``inst`` — the 3x3 neighbourhood
        of grid cells around ``inst``'s own cell, which is EXACT (not an
        approximation) because the cell size equals the interaction radius
        (module docstring's courtyard_overlap section)."""
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
            t = courtyard_overlap_pair_term(ir, inst, other, self.level, cfg.cost)
            if t.raw <= 0.0:
                continue
            key = _pair_key(inst, other)
            self._margin[("courtyard_overlap", key)] = t
            self._courtyard_partners[inst].add(other)
            self._courtyard_partners.setdefault(other, set()).add(inst)

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
        own ``seg_net``/``seg_layer``/``net_plane_layer`` fields (see that
        function's docstring), so no OTHER segment's cached count can ever
        go stale as a side effect of this one segment's move.

        Called from every move kind that can change what
        ``implied_via_count`` reads for a segment: ``LAYER_ASSIGN``
        (``seg_layer``, one segment) and ``PLANE_PROMOTE``/``PLANE_DEMOTE``
        via :meth:`_rescan_net` (``net_plane_layer``, that net's own
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
        clipped to the outline inset by :data:`_EDGE_MARGIN_MM`. Falls
        back to the historical origin-anchored ``board_side`` square when
        nothing is placed yet (no seed to derive from)."""
        ir = self.ir
        if ir.outline and len(ir.outline) >= 3:
            ox0, oy0, ox1, oy1 = outline_bbox(ir.outline)
            ox0, oy0 = ox0 + _EDGE_MARGIN_MM, oy0 + _EDGE_MARGIN_MM
            ox1, oy1 = ox1 - _EDGE_MARGIN_MM, oy1 - _EDGE_MARGIN_MM
        else:
            ox0, oy0, ox1, oy1 = 0.0, 0.0, self.board_side, self.board_side
        if ox1 <= ox0 or oy1 <= oy0:  # an outline smaller than its own margin
            return (ox0, oy0, ox0 + self.board_side, oy0 + self.board_side)
        placed = np.isfinite(ir.inst_x) & np.isfinite(ir.inst_y)
        if not placed.any():
            return (
                ox0,
                oy0,
                min(ox1, ox0 + self.board_side),
                min(oy1, oy0 + self.board_side),
            )
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

    def _placement_is_legal(
        self, proposals: Sequence[tuple[int, float, float]]
    ) -> bool:
        """True iff every proposed ``(instance, x, y)`` sits inside
        :attr:`_placement_bounds` and clears every other instance's
        keep-out — including the other proposals in the same move (SWAP
        moves two parts at once).

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
        for i, (inst, x, y) in enumerate(proposals):
            x0, y0, x1, y1 = self.bounds_for(inst)
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                return False
            dx, dy = ir.inst_x - x, ir.inst_y - y
            d2 = dx * dx + dy * dy
            d2[moving] = math.inf  # a part never collides with its own old slot
            sep = keepout + keepout[inst]
            if np.any(d2 < sep * sep):  # NaN (unplaced) compares False — correct
                return False
            for other, ox, oy in proposals[i + 1 :]:
                sep_ij = keepout[inst] + keepout[other]
                if (x - ox) ** 2 + (y - oy) ** 2 < sep_ij * sep_ij:
                    return False
        return True

    def _rescan_after_move(self, moved_inst: int) -> None:
        """Refresh every cached term a single instance's move can affect.
        Called once per moved instance (translate/rotate: once; swap:
        twice, sequentially — see :meth:`apply_move`)."""
        ir = self.ir
        self._refresh_courtyard(moved_inst)
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
        ``net_plane_layer`` now — see :func:`precis.pcb.cost.
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
        return sum(self._money_static_by_name.values()) + self._money_board_area

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
        assert move.net is not None
        if forward:
            self.ir.promote_plane(move.net, move.new_int[0])
        else:
            self.ir.demote_plane(move.net)
        self._rescan_net(move.net)
        self._refresh_layer_count()

    def _apply_plane_demote(self, move: Move, *, forward: bool) -> None:
        assert move.net is not None
        if forward:
            self.ir.demote_plane(move.net)
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


def _gen_translate(
    engine: OptimizeEngine, rng: random.Random, temp: float
) -> Move | None:
    movable = engine._movable_xy
    if not movable:
        return None
    ir = engine.ir
    inst = movable[rng.randrange(len(movable))]
    old = (float(ir.inst_x[inst]), float(ir.inst_y[inst]), float(ir.inst_rot[inst]))
    step = max(
        0.5, engine.config.translate_step_mm * (temp / max(engine.board_side, 1.0))
    )
    step = max(0.5, min(step, engine.config.translate_step_mm))
    x0, y0, x1, y1 = engine.bounds_for(inst)
    # Retry a few draws before giving up: a single rejected sample would
    # make TRANSLATE effectively unavailable for a part in a crowded
    # neighbourhood, which is exactly where it is most needed.
    for _ in range(_LEGALIZE_TRIES):
        nx = _clamp(old[0] + rng.gauss(0, step), x0, x1)
        ny = _clamp(old[1] + rng.gauss(0, step), y0, y1)
        if engine._placement_is_legal(((inst, nx, ny),)):
            return Move(MoveKind.TRANSLATE, (inst,), (old,), ((nx, ny, old[2]),))
    return None


def _gen_rotate(engine: OptimizeEngine, rng: random.Random, temp: float) -> Move | None:
    movable = engine._movable_rot
    if not movable:
        return None
    ir = engine.ir
    inst = movable[rng.randrange(len(movable))]
    old = (float(ir.inst_x[inst]), float(ir.inst_y[inst]), float(ir.inst_rot[inst]))
    new_rot = (old[2] + rng.choice((90.0, -90.0))) % 360.0
    return Move(MoveKind.ROTATE, (inst,), (old,), ((old[0], old[1], new_rot),))


def _gen_swap(engine: OptimizeEngine, rng: random.Random, temp: float) -> Move | None:
    movable = engine._movable_xy
    if len(movable) < 2:
        return None
    ir = engine.ir
    ia, ib = rng.sample(movable, 2)
    old_a = (float(ir.inst_x[ia]), float(ir.inst_y[ia]), float(ir.inst_rot[ia]))
    old_b = (float(ir.inst_x[ib]), float(ir.inst_y[ib]), float(ir.inst_rot[ib]))
    new_a = (old_b[0], old_b[1], old_a[2])
    new_b = (old_a[0], old_a[1], old_b[2])
    # Both destinations are existing, already-legal slots, so this can
    # only fail when one of them lies outside the placement bounds (a
    # seeded part the domain doesn't cover) — worth checking rather than
    # assuming, since accepting it would strand a part off-board.
    if not engine._placement_is_legal(
        ((ia, new_a[0], new_a[1]), (ib, new_b[0], new_b[1]))
    ):
        return None
    return Move(MoveKind.SWAP, (ia, ib), (old_a, old_b), (new_a, new_b))


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
    # A locked net is never itself a promotion candidate (it already has
    # its human-fixed assignment) — see `OptimizeConfig.locked_plane_nets`'s
    # docstring for why this is a hard constraint, not a move the search
    # may merely disfavour. Its OWN plane layer still shows up in the
    # `taken` set below unchanged (its `net_plane_layer` is left alone by
    # every generator, so it stays != UNSET_LAYER throughout), so a locked
    # net's plane layer is correctly unavailable to any other net without
    # this function needing a second, separate exclusion for it.
    locked = engine.config.locked_plane_nets
    candidates = [
        n
        for n in range(ir.n_nets)
        if int(ir.net_plane_layer[n]) == UNSET_LAYER and n not in locked
    ]
    if not candidates:
        return None
    # A plane layer carries ONE net. It is a sheet of copper, and two nets
    # cannot both be it — this is a hard physical constraint, not a
    # preference the annealer may pay for. Without the filter the search
    # cheerfully promoted several nets onto the same handful of plane
    # layers, which reads as a legal state everywhere: `net_plane_layer`
    # is per-net so it can represent the contradiction, and every
    # consumer that maps layer->net silently keeps the last writer,
    # leaving every other promoted net's pads and stubs connected to
    # nothing.
    taken = {
        int(ir.net_plane_layer[n])
        for n in range(ir.n_nets)
        if int(ir.net_plane_layer[n]) != UNSET_LAYER
    }
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
    locked = engine.config.locked_plane_nets
    candidates = [
        n
        for n in range(ir.n_nets)
        if int(ir.net_plane_layer[n]) != UNSET_LAYER and n not in locked
    ]
    if not candidates:
        return None
    net = candidates[rng.randrange(len(candidates))]
    old_layer = int(ir.net_plane_layer[net])
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
    "seed_placement",
]

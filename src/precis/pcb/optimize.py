"""The joint place+route optimizer — ONE engine over a shared
(placement, sketch) state, per docs/backlog/pcb-guided-place-route.md
§"Joint place+route optimizer". **Slice 6 ships the walking skeleton: the
move set is restricted to placement (translate, rotate 90°, swap-pair).**
Slice 7 turns on topology/layer/pin-swap moves *in this same engine* — the
move-generator registry, the schedule-as-data shape, and the incremental
cost-delta plumbing below are all written so that slice only adds registry
entries, never restructures this module (backlog, verbatim: "not a second
engine: 6 and 7 are one optimize.py shipped in two steps").

**Why place and route are one engine, not two stages** (backlog): the
classic place-then-route split is a workaround for maze routing, where a
route *is* geometry and any component move invalidates it. Under
:mod:`precis.pcb.ir`'s rubber-band sketch, topology (L0-L2) is invariant to
a small placement (L3) perturbation — a component move dirties L3/L4/L5
and leaves L1/L2 untouched (see ``PcbIR.move_instance``). Slice 6 only
*exercises* placement moves, but the state, the cost plumbing and the SA
loop are the shared ones slice 7 extends, not a placement-only algorithm
that would need rewriting.

**The hard locality constraint** (backlog, verbatim): "every cost term
must decompose into local contributions with an efficient delta... a term
requiring board-wide re-evaluation per move is disqualified however cheap
it looks." This module never calls :func:`precis.pcb.cost.evaluate_cost`
per move — that would silently violate the constraint regardless of how
fast any one call happens to be at a 30-component test board. Instead:

- :mod:`precis.pcb.cost` was extended (behaviour-preserving refactor, same
  tests, same numbers) to expose single-item evaluators alongside its
  existing full-board loops: :func:`~precis.pcb.cost.gap_capacity_term`,
  :func:`~precis.pcb.cost.loop_inductance_term`,
  :func:`~precis.pcb.cost.coupling_pair_term` /
  :func:`~precis.pcb.cost.coupling_candidates`, and
  :func:`~precis.pcb.cost.board_area_term`. This engine calls those
  per-item functions for exactly the segments/pairs a move can affect,
  never re-deriving the whole board's terms.
- ``layer_count``, ``via_count``, ``extended_part_fees`` and
  ``thermal_rise`` are **provably invariant under a placement-only move**
  (they read ``seg_layer``/``net_plane_layer``/``inst_extended_part``/
  ``net_class`` — none of which a translate/rotate/swap touches), so they
  are evaluated ONCE at construction and never touched again for the rest
  of slice 6's restricted move set. Slice 7's layer/topology moves dirty
  ``layer_count`` (:meth:`OptimizeEngine._refresh_layer_count`, a full
  O(n_segments) rescan — the set of layers in use is a whole-board
  aggregate, same carve-out as ``board_area``) and, since 2026-08-28,
  ``via_count`` too (:meth:`OptimizeEngine._refresh_via_count_for_segment`
  — via count is a per-segment SUM, not a set aggregate, so unlike
  ``layer_count`` it gets a real O(1) bounded delta: see
  :func:`precis.pcb.rules.implied_via_count`'s docstring for why a via
  count only ever depends on its OWN segment's net/layer fields). Before
  that fix ``via_count`` read ``ir.n_vias``, a field nothing in production
  ever grew, so treating it as move-invariant was accidentally correct
  (0 always) rather than a real locality argument — this is exactly the
  kind of registry-entry addition the module docstring anticipated.
- ``board_area`` is the one registered term that genuinely cannot
  decompose locally — a bounding box is a whole-board aggregate by
  definition, not an oversight of this engine. It is recomputed via
  :func:`~precis.pcb.cost.board_area_term` (an O(n_instances) scan) after
  every move rather than pretended away; O(n_instances), not
  O(n_segments) or O(board geometry), is the honest cost this buys —
  cheap in practice (board scale here is bounded by the fab's own part
  count) but flagged here rather than silently declared compliant with
  the locality rule, per this task's "if the spec is wrong on contact,
  say so" instruction.
- ``gap_capacity``'s per-segment "nearest other instance" search is
  ``ir.py``'s own shipped algorithm (O(n_instances) per segment, no
  spatial index yet — a documented future accelerant, not this slice's
  job). What IS this engine's job is knowing *which* segments a move can
  invalidate without re-running that search for every segment on the
  board: :func:`precis.pcb.ir.nearest_other_instance` returns which
  instance realized the minimum, cached here as ``_seg_nearest_instance``.
  A moved instance ``M`` can only invalidate a segment ``s`` if (a) ``s``
  is incident to ``M`` (its own search origin moved), (b) ``s``'s cached
  nearest instance *was* ``M`` (the previous answer just moved and needs
  re-confirming), or (c) ``M``'s new position is now closer to ``s`` than
  ``s``'s cached distance (M "cuts in front" of whatever was nearest) —
  checked via one vectorized numpy comparison over all segments'
  endpoint-to-``M`` distance, not a re-derivation of anything expensive.
  Only segments flagged by (a)-(c) get a real (bounded) re-search; this is
  *exact*, not an approximation — see ``tests/test_pcb_optimize.py``'s
  delta-correctness property test.
- ``loop_inductance`` depends only on a segment's own two endpoints, never
  another instance's position, so its delta is trivially local: recompute
  only for segments incident to the moved instance(s).
- ``coupling``'s *candidate list* (which segments are aggressor/victim
  material at all) is position-independent — computed once
  (:func:`~precis.pcb.cost.coupling_candidates`) and never touched again.
  Only the *pairwise proximity* changes with placement, and only for pairs
  naming a moved segment, so a move re-scores at most
  ``|touched candidates| x |all candidates|`` pairs — bounded by the
  candidate list's size ("dozens, not thousands" per the backlog), never
  by segment count.
- ``crossings`` (:func:`precis.pcb.cost.crossings_term_for_layer`, backed
  since 2026-08-28 by :func:`precis.pcb.ir.same_layer_crossing_count`'s
  geometric sweep-line count — the Euler-bound backing this engine used
  to maintain via a per-layer running ``(V, E)`` pair was retired: it was
  provably always zero on a real board (a star-decomposed segment graph
  is a vertex-disjoint forest — see that function's own docstring for the
  proof), so an ``(V, E)`` cache tracking it, however O(1) per move,
  was maintaining an incremental delta for a quantity that could never
  move. The GEOMETRIC count cannot be maintained the same way — a
  vertex/edge tally has nothing to do with whether two segments' straight
  lines happen to cross — so the incremental structure changed shape, not
  just its backing formula:
  ``_segments_by_layer`` (``{layer: {seg_id, ...}}``, the segment
  membership index a geometric recount needs to enumerate "the rest of
  that layer") plus ``_seg_crossing_partners`` (``{seg_id: {other_seg_id,
  ...}}``, which OTHER same-layer segments ``seg_id`` currently crosses —
  a symmetric adjacency cache) plus ``_layer_crossing_count`` (the
  per-layer total, ``sum(len(partners)) / 2`` maintained as a running
  int, never recomputed from the sets). :meth:`OptimizeEngine.
  _recompute_seg_crossings` is the bounded delta: for ONE touched segment,
  discard its cached partnerships (O(old partner count)), then retest it
  against every OTHER segment CURRENTLY on its (possibly new) layer —
  O(layer size), never O(board) — exactly the "recount intersections
  involving the touched segments against the rest of that layer, not all
  pairs" shape the fix's design called for. Called once per segment
  incident to a moved instance (TRANSLATE/ROTATE/SWAP, inside
  :meth:`_rescan_after_move` — geometry changed) and once for a
  ``LAYER_ASSIGN`` move's single segment (layer membership changed).
  SIDE_FLIP and PIN_SWAP never touch a segment's INSTANCE-centroid
  endpoints (see those move kinds' own notes below), so neither ever
  calls this — provably cost-neutral for ``crossings`` specifically, not
  merely untested.

**What is NOT claimed local, on purpose.** Aggregating the margin family
(:func:`precis.pcb.cost.aggregate_margin`'s max) over the already-cached
per-item penalties is a linear scan over however many margin entries exist
— cheap (these are floats already sitting in a dict, not a geometry
re-derivation) but genuinely O(cached entries), not O(touched). At this
slice's board scale (a few hundred entries) that is microseconds; a
production-scale board would want an incrementally-maintained max
structure (a lazy-deletion max-heap) instead of this linear scan — noted
here as the natural next step rather than built now, since the *expensive*
work (the physics above) is what the hard locality rule is actually
protecting against.

**Constraint hardening IS the schedule** (backlog, verbatim — the same
mechanism as the cost function's convexity): this engine drives
:func:`precis.pcb.cost.hardened_penalty`'s ``schedule`` parameter from 0
(exploratory) to 1 (barrier) linearly over the anneal's iteration count.
It does not invent a second hardening mechanism.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from precis.format import toon
from precis.pcb import pinswap
from precis.pcb.cost import (
    _BY_NAME,
    _CRITICALITY_WEIGHT,
    CostConfig,
    Family,
    TermValue,
    board_area_term,
    coupling_candidates,
    coupling_pair_term,
    evaluate_cost,
    gap_capacity_term,
    hardened_penalty,
    layer_count_term,
    loop_inductance_term,
)
from precis.pcb.geom import segments_cross
from precis.pcb.ir import (
    UNSET_LAYER,
    Level,
    PcbIR,
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

#: **Known, expected characteristic, not a bug — and now narrower than it
#: used to be.** No term registered in ``cost.py`` reads ``inst_rot``
#: (component-centroid granularity — the same limitation ``place.py``
#: documented: "rotation has no effect on the crossing metric until real
#: pad offsets land"), so ROTATE stays cost-neutral under every
#: currently-registered term: its ``total()`` delta is a true, provable
#: zero, not an approximation rounding to zero. It is still exercised
#: here (dirty cascade honoured, `fixed='rot'` respected) so the
#: move-generator registry, the schedule, and the delta-correctness
#: plumbing are already exactly the shape a future cost.py term (real
#: per-pin footprint offsets for ROTATE, the same data
#: :func:`precis.pcb.pinswap.offsets_from_pads` now wires through for
#: PIN_SWAP) needs to land into.
#:
#: **SIDE_FLIP is cost-neutral too, and still genuinely CANNOT be
#: otherwise at this engine's fidelity — the reason changed on 2026-08-28
#: when `crossings` moved from the Euler bound to a GEOMETRIC sweep-line
#: count, but the conclusion didn't.** ``crossings`` is now backed by
#: :func:`precis.pcb.ir.same_layer_crossing_count`, which reads segment
#: endpoints at INSTANCE-centroid granularity (the same fidelity every
#: other L3 term here uses — no sub-instance pad geometry exists in the
#: IR yet). ``seg_side`` records WHICH SIDE of an obstacle a connection
#: routes, a property the realizer's arcs/tangents would need but that
#: never perturbs an instance's own (x, y) — so a straight-line count
#: between component centroids is structurally blind to it, same as it
#: is blind to `inst_rot`. A term that responded to a side flip would
#: need real sub-instance geometry (the same missing ingredient ROTATE's
#: note names) or realize.py's face tracing — both out of scope here.
#: SIDE_FLIP remains exercised here (dirty cascade honoured) so the
#: plumbing is ready the moment ``sketch.py``'s anchor vocabulary (still
#: an explicitly open backlog item) gives a side choice real geometric
#: meaning.
#:
#: LAYER_ASSIGN and PLANE_PROMOTE/DEMOTE are NOT cost-neutral: LAYER_
#: ASSIGN's ``layer_count`` money term AND ``crossings`` margin term
#: respond to it; PLANE_PROMOTE/DEMOTE's ``layer_count`` (via
#: ``net_plane_layer``) AND ``gap_capacity`` (the plane-exclusion branch)
#: respond to those moves — see :meth:`OptimizeEngine._refresh_layer_count`,
#: :meth:`OptimizeEngine._recompute_seg_crossings` and
#: :func:`precis.pcb.cost.gap_capacity_term`. PIN_SWAP is its own
#: separate story: see :mod:`precis.pcb.pinswap`'s module docstring — its
#: effect is real and measured by that module's own crossing evaluator
#: (now over REAL per-pin geometry when the caller supplies footprint
#: pads via :func:`precis.pcb.pinswap.offsets_from_pads`), but still
#: invisible to ``total()`` (no term registered in ``cost.py`` reads pin
#: identity or sub-instance pad position — pin swap's payoff lives
#: entirely in pinswap.py's own linearized matching, not the registry).


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
    """The constructive seed: connectivity clustering (above) then
    "cluster drop" — clusters land on a coarse grid (bigger clusters
    first), members scatter on a small local sub-grid with mild jitter
    inside their cluster's cell. Adjacency-aware, unlike a uniform random
    scatter: instances that share nets start out near each other, which
    is what makes the SA refiner's job tractable.

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
    n_clusters = max(1, len(clusters))
    cols = max(1, math.ceil(math.sqrt(n_clusters)))
    cluster_pitch = pitch_mm * max(1, math.ceil(math.sqrt(max_cluster_size))) * 1.5

    for ci, members in enumerate(clusters):
        row, col = divmod(ci, cols)
        cx, cy = col * cluster_pitch, row * cluster_pitch
        local_cols = max(1, math.ceil(math.sqrt(len(members))))
        for mi, inst in enumerate(members):
            lrow, lcol = divmod(mi, local_cols)
            x = cx + lcol * pitch_mm + rng.uniform(-0.1, 0.1) * pitch_mm
            y = cy + lrow * pitch_mm + rng.uniform(-0.1, 0.1) * pitch_mm
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
#: a sorted segment-id pair (coupling), or a net name (thermal_rise).
MarginKey = int | tuple[int, int] | str


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

        # per-segment endpoint-instance arrays, for the vectorized
        # "newly closer than cached" gap-capacity check (module docstring).
        self._seg_inst_a = ir.pin_instance[ir.seg_pin_a].astype(np.int64)
        self._seg_inst_b = ir.pin_instance[ir.seg_pin_b].astype(np.int64)
        self._seg_gap_distance = np.full(ir.n_segments, math.inf)
        self._seg_nearest_instance = np.full(ir.n_segments, -1, dtype=np.int64)

        # crossings state: GEOMETRIC (module docstring's crossings-term
        # section, revised 2026-08-28) -- `_segments_by_layer[layer]` is the
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

        # margin cache: (term_name, key) -> latest TermValue. `key` is a
        # segment id for gap_capacity/loop_inductance, a sorted segment-id
        # pair for coupling, a net name for thermal_rise, a layer id for
        # crossings.
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

    def _rescan_after_move(self, moved_inst: int) -> None:
        """Refresh every cached term a single instance's move can affect.
        Called once per moved instance (translate/rotate: once; swap:
        twice, sequentially — see :meth:`apply_move`)."""
        ir = self.ir
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
                # **Reheat at every schedule-stage boundary** — found on
                # contact while measuring slice-7 throughput (2026-08-28):
                # without this, a global exponential cool from `t0` over
                # the WHOLE `iters` budget is already near-zero (e.g.
                # ~1e-22 x t0 by 50% of a 20k-iteration run at the
                # default `cooling=0.995`) by the time a LATER stage's
                # move kind first becomes eligible. A newly-introduced
                # kind whose delta isn't exactly zero (LAYER_ASSIGN's
                # `layer_count` money step, PLANE_PROMOTE's) then can
                # NEVER pay its one-time entry cost and is silently
                # frozen out for the rest of the run — measured directly:
                # zero LAYER_ASSIGN/PLANE_PROMOTE acceptances over a full
                # anneal despite thousands of proposals, before this fix.
                # Reheating to `t0` at each boundary gives every stage's
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
        if term_name == "coupling":
            assert isinstance(key, tuple)
            sa, sb = key
            xa, ya = self._midpoint(sa)
            xb, yb = self._midpoint(sb)
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
    nx = _clamp(old[0] + rng.gauss(0, step), 0.0, engine.board_side)
    ny = _clamp(old[1] + rng.gauss(0, step), 0.0, engine.board_side)
    return Move(MoveKind.TRANSLATE, (inst,), (old,), ((nx, ny, old[2]),))


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
    candidates = [
        n for n in range(ir.n_nets) if int(ir.net_plane_layer[n]) == UNSET_LAYER
    ]
    if not candidates:
        return None
    net = candidates[rng.randrange(len(candidates))]
    layer = engine._plane_layers[rng.randrange(len(engine._plane_layers))]
    return Move(MoveKind.PLANE_PROMOTE, net=net, new_int=(layer,))


def _gen_plane_demote(
    engine: OptimizeEngine, rng: random.Random, temp: float
) -> Move | None:
    ir = engine.ir
    candidates = [
        n for n in range(ir.n_nets) if int(ir.net_plane_layer[n]) != UNSET_LAYER
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

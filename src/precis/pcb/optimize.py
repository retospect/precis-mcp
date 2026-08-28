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
  (they read ``seg_layer``/``n_vias``/``inst_extended_part``/``net_class``
  — none of which a translate/rotate/swap touches), so they are evaluated
  ONCE at construction and never touched again for the rest of slice 6's
  restricted move set. Slice 7's layer/topology moves will need to dirty
  them again; that is exactly the kind of registry-entry addition the
  move-mix schedule below is shaped to absorb.
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
from precis.pcb.cost import (
    _BY_NAME,
    _CRITICALITY_WEIGHT,
    CostConfig,
    TermValue,
    board_area_term,
    coupling_candidates,
    coupling_pair_term,
    evaluate_cost,
    gap_capacity_term,
    hardened_penalty,
    loop_inductance_term,
)
from precis.pcb.ir import Level, PcbIR, nearest_other_instance

# ── move set (slice 6: placement only) ──────────────────────────────────


class MoveKind(Enum):
    """The restricted slice-6 move set. Slice 7 adds
    ``TOPOLOGY_FLIP``/``LAYER_ASSIGN``/``PIN_SWAP``/... members here and
    registers their generators in :data:`MOVE_GENERATORS` — nothing else
    in this module changes shape to accommodate that."""

    TRANSLATE = "translate"
    ROTATE = "rotate"
    SWAP = "swap"


@dataclass(frozen=True, slots=True)
class Move:
    """One proposed move: which instance(s), their pre- and post-move
    ``(x, y, rot)``. A pure data record — :meth:`OptimizeEngine.apply_move`
    / :meth:`OptimizeEngine.undo_move` are what actually mutate state, so a
    rejected move costs exactly one more application of the same
    (exact, bounded) update pipeline, never a special-cased rollback path.
    """

    kind: MoveKind
    instances: tuple[int, ...]
    old: tuple[tuple[float, float, float], ...]
    new: tuple[tuple[float, float, float], ...]


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


#: Placement-dominant for the whole run — there is no topology to become
#: dominant *over* yet (slice 7). Translate carries most of the weight
#: (it is what actually moves the congestion picture); rotate is included
#: for architectural completeness even though it is currently cost-neutral
#: (see the module-level note below) so the move-mix machinery already
#: exercises a 3-kind registry, matching the slice-7 shape.
DEFAULT_SCHEDULE: tuple[ScheduleStage, ...] = (
    ScheduleStage(
        1.0,
        {MoveKind.TRANSLATE: 0.6, MoveKind.ROTATE: 0.15, MoveKind.SWAP: 0.25},
    ),
)

#: **Known, expected characteristic, not a bug**: no term registered in
#: ``cost.py`` reads ``inst_rot`` yet (component-centroid granularity —
#: the same limitation ``place.py`` documented: "rotation has no effect
#: on the crossing metric until real pad offsets land"). A ROTATE move is
#: therefore cost-neutral under every current term; it is still exercised
#: here (dirtying L3/L4/L5 like any move, honouring `fixed='rot'`) so the
#: move-generator registry and the locality plumbing are already exactly
#: the shape slice 7's per-pin footprint offsets need — nothing here
#: special-cases rotation as inert.


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

        # margin cache: (term_name, key) -> latest TermValue. `key` is a
        # segment id for gap_capacity/loop_inductance, a sorted segment-id
        # pair for coupling, a net name for thermal_rise.
        self._margin: dict[tuple[str, MarginKey], TermValue] = {}
        self._loop_applicable: set[int] = set()
        self._coupling_candidates = coupling_candidates(ir, config.cost)
        self._coupling_candidate_set = set(self._coupling_candidates)
        self._coupling_pairs: set[tuple[int, int]] = set()

        self._money_static_by_name: dict[str, float] = {}
        self._money_board_area = 0.0

        self._init_caches()

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
        full = evaluate_cost(ir, self.level, cfg.cost)
        for t in full.terms:
            if t.name in ("layer_count", "via_count", "extended_part_fees"):
                self._money_static_by_name[t.name] = (
                    self._money_static_by_name.get(t.name, 0.0) + t.raw
                )
            elif t.name == "thermal_rise":
                self._margin[("thermal_rise", t.region)] = t

        self._refresh_board_area()

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
    def apply_move(self, move: Move) -> None:
        self._apply(move, move.new)

    def undo_move(self, move: Move) -> None:
        self._apply(move, move.old)

    def _apply(
        self, move: Move, coords: tuple[tuple[float, float, float], ...]
    ) -> None:
        for inst, (x, y, rot) in zip(move.instances, coords):
            self.ir.move_instance(inst, x=x, y=y, rot=rot)
            self._rescan_after_move(inst)
        self._refresh_board_area()

    # -- SA loop -----------------------------------------------------------
    def anneal(self, rng: random.Random) -> None:
        cfg = self.config
        temp = cfg.t0 if cfg.t0 is not None else max(5.0, self.board_side / 2.0)
        moves: list[MoveRecord] = []
        for it in range(cfg.iters):
            self.schedule = min(1.0, it / max(1, cfg.iters - 1))
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


MOVE_GENERATORS: dict[MoveKind, MoveGeneratorFn] = {
    MoveKind.TRANSLATE: _gen_translate,
    MoveKind.ROTATE: _gen_rotate,
    MoveKind.SWAP: _gen_swap,
}


def _active_weights(
    schedule: tuple[ScheduleStage, ...], frac: float
) -> dict[MoveKind, float]:
    for stage in schedule:
        if frac <= stage.through_fraction:
            return stage.weights
    return schedule[-1].weights


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
    "RegionEntry",
    "ScheduleStage",
    "TermSummary",
    "digest_toon",
    "optimize",
    "seed_placement",
]

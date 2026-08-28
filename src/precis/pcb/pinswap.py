"""Pin/gate swap — the move class a conventional (place-then-maze-route)
flow structurally cannot express: reassigning which of an instance's
functionally-interchangeable physical pins carries which net. See
docs/backlog/pcb-guided-place-route.md's "Pin swap" paragraph in the
optimizer section and the IR's own "A win that only exists in graph
space" note.

**Admissible sets with side constraints, not free permutation** (task
instruction, verbatim intent): a real part has pins that LOOK
interchangeable but are not — ESP32 strapping pins that break boot if
loaded, GPIOs that vanish under JTAG, ADC2 unusable while WiFi is on.
This module never invents that domain knowledge. :class:`PinSwapGroup` is
the caller-supplied admissible set (which physical pins are candidates at
all) plus an explicit exclusion set (which of those must never move); the
matcher only ever operates inside what it's told is safe. **Where the
equivalence data doesn't exist yet, the degrade path is silence**:
:data:`precis.pcb.optimize.OptimizeConfig.pin_swap_groups` defaults to
``()``, so :func:`propose_reassignment` is simply never called and no
swap is ever proposed — nothing here guesses an equivalence class from a
part number or footprint shape.

**Why this needs geometry the base IR doesn't otherwise carry.** At pure
instance-centroid granularity (every pin of one instance sharing the same
``inst_x``/``inst_y``), swapping which net occupies which of an
instance's OWN pins cannot change any distance-based metric — there is
nothing to gain. The real effect described in the backlog ("collapse a
large fraction of crossings") only exists once individual pins have
distinguishable positions relative to their instance's origin (real
footprint pad offsets — the same data :mod:`precis.pcb.escape` derives
per-footprint). :class:`PinSwapGroup.offsets` carries exactly that,
**scoped to this module only**: it is a side-channel the caller supplies
(mirroring :data:`precis.pcb.cost.CostConfig.net_annotations` — optional
domain data, not an IR field), never wired into
:mod:`precis.pcb.cost`'s registered terms. A pin with no offset entry
defaults to ``(0.0, 0.0)`` (its instance's own centroid) — the same
"unmodeled = centroid" state every other pin in the IR is already in, so
a group with no offset data degrades to a genuinely no-op swap rather
than a crash.

**The cost matrix is a linearized approximation of a jointly-quadratic
problem, and that is stated here rather than glossed over.** The TRUE
objective — total crossings among every pair of this instance's airwires
under a full reassignment — is not decomposable into independent
``(net, candidate pin)`` cells (two group members' airwires can cross
each other, and that pairwise term depends on BOTH of their destinations
at once). :func:`build_cost_matrix` scores each cell against every OTHER
pin held at its CURRENT position (a standard "coordinate descent" style
linearization), which is exact for the background (non-participating)
segments and an approximation for participant-vs-participant crossings.
It is still useful and testable: :func:`total_group_crossings` is the
exact ground-truth count :func:`propose_reassignment` is trying to
reduce, and callers/tests can measure the real before/after effect
independently of the matrix's own approximation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from precis.pcb.geom import Point, segments_cross
from precis.pcb.ir import PcbIR

# ── the admissible set + exclusions ─────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PinSwapGroup:
    """One instance's admissible pin-swap set — caller-supplied, never
    inferred. ``pins`` are candidate physical pin ids (all must share
    ``instance``); ``excluded`` names pins that are group members for cost
    accounting but must never be reassigned (the strapping-pin case);
    ``offsets`` is the optional per-pin footprint position (mm, relative
    to the instance origin) the crossing evaluator needs — see the module
    docstring's geometry note. A pin absent from ``offsets`` is treated as
    sitting at the instance's own centroid (0.0, 0.0)."""

    instance: int
    pins: tuple[int, ...]
    offsets: dict[int, Point] = field(default_factory=dict)
    excluded: frozenset[int] = frozenset()


# ── local geometry: this instance's own airwires only ───────────────────


def _pin_pos(inst_x: float, inst_y: float, group: PinSwapGroup, pin: int) -> Point:
    dx, dy = group.offsets.get(pin, (0.0, 0.0))
    return (inst_x + dx, inst_y + dy)


def _segments_near_pin(ir: PcbIR, instance: int) -> dict[int, int]:
    """``{segment_id: near_pin_id}`` for every segment touching
    ``instance`` — the endpoint that sits on this instance (the other end
    is "far"). A segment with both endpoints on the same instance
    (degenerate, but not disallowed by the IR) picks its ``pin_a`` side
    arbitrarily; nothing downstream depends on which."""
    out: dict[int, int] = {}
    for seg_id in ir._segs_of_instance.get(instance, ()):
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        out[seg_id] = a if int(ir.pin_instance[a]) == instance else b
    return out


def _instance_edges(ir: PcbIR, instance: int) -> dict[int, list[Point]]:
    """``{near_pin: [far_point, ...]}`` for every segment touching
    ``instance`` — the far endpoint's OWN instance centroid (only the near
    side, on ``instance``, ever has sub-instance offset detail in this
    module)."""
    edges: dict[int, list[Point]] = {}
    for seg_id, near_pin in _segments_near_pin(ir, instance).items():
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        other = b if a == near_pin else a
        inst = int(ir.pin_instance[other])
        x, y = float(ir.inst_x[inst]), float(ir.inst_y[inst])
        if math.isnan(x) or math.isnan(y):
            continue
        edges.setdefault(near_pin, []).append((x, y))
    return edges


def total_group_crossings(ir: PcbIR, group: PinSwapGroup) -> int:
    """Ground truth: genuine crossings among every airwire touching
    ``group.instance`` at the CURRENT pin assignment. This is the exact
    quantity a proposed swap is trying to reduce — used by tests (and any
    caller wanting a real before/after measurement) independently of
    :func:`build_cost_matrix`'s linearized approximation."""
    inst_x, inst_y = float(ir.inst_x[group.instance]), float(ir.inst_y[group.instance])
    if math.isnan(inst_x) or math.isnan(inst_y):
        return 0
    edges = _instance_edges(ir, group.instance)
    airwires: list[tuple[Point, Point]] = []
    for pin, fars in edges.items():
        near = _pin_pos(inst_x, inst_y, group, pin)
        airwires.extend((near, far) for far in fars)
    count = 0
    for i in range(len(airwires)):
        for j in range(i + 1, len(airwires)):
            if segments_cross(*airwires[i], *airwires[j]):
                count += 1
    return count


# ── the cost matrix + min-cost bipartite matching ────────────────────────


def build_cost_matrix(
    ir: PcbIR, group: PinSwapGroup
) -> tuple[list[int], list[list[float]]] | None:
    """``(movable_pins, cost)`` where ``cost[i][j]`` is the crossing count
    if the net currently at ``movable_pins[i]`` moved to ``movable_pins[j]``
    while every OTHER pin (movable or background) stays at its CURRENT
    position (module docstring's linearization). ``None`` when there is
    nothing to evaluate: fewer than two non-excluded pins, or the instance
    has no L3 position yet (nothing to measure)."""
    movable = [p for p in group.pins if p not in group.excluded]
    if len(movable) < 2:
        return None
    inst_x, inst_y = float(ir.inst_x[group.instance]), float(ir.inst_y[group.instance])
    if math.isnan(inst_x) or math.isnan(inst_y):
        return None
    edges = _instance_edges(ir, group.instance)
    movable_set = set(movable)
    background = [
        (_pin_pos(inst_x, inst_y, group, pin), far)
        for pin, fars in edges.items()
        if pin not in movable_set
        for far in fars
    ]
    n = len(movable)
    cost = [[0.0] * n for _ in range(n)]
    for i, src_pin in enumerate(movable):
        fars = edges.get(src_pin, [])
        if not fars:
            continue  # this pin carries no segment -- moving it is free
        others_fixed = list(background)
        for other_pin in movable:
            if other_pin == src_pin:
                continue
            near = _pin_pos(inst_x, inst_y, group, other_pin)
            others_fixed.extend((near, far) for far in edges.get(other_pin, ()))
        for j, dst_pin in enumerate(movable):
            dst_pos = _pin_pos(inst_x, inst_y, group, dst_pin)
            total = 0
            for far in fars:
                for near, ofar in others_fixed:
                    if segments_cross(dst_pos, far, near, ofar):
                        total += 1
            cost[i][j] = float(total)
    return movable, cost


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Min-cost perfect bipartite matching on a square cost matrix —
    ``assign[i]`` is the column matched to row ``i``. The classical
    O(n^3) successive-shortest-augmenting-path formulation (Kuhn-Munkres
    with potentials); ``n`` here is one instance's pin-swap group size (a
    handful of pins), never board scale — this is the "polynomial and
    fast, likely cheaper than the annealing around it" primitive the
    backlog calls for, not a per-move board-wide search."""
    n = len(cost)
    inf = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)  # p[j] = 1-indexed row currently matched to column j
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = -1
            for j in range(1, n + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    assign = [0] * n
    for j in range(1, n + 1):
        if p[j] != 0:
            assign[p[j] - 1] = j - 1
    return assign


def _cycles(perm: list[int]) -> list[list[int]]:
    n = len(perm)
    seen = [False] * n
    cycles: list[list[int]] = []
    for i in range(n):
        if seen[i]:
            continue
        if perm[i] == i:
            seen[i] = True
            continue
        cyc = [i]
        seen[i] = True
        j = perm[i]
        while j != i:
            cyc.append(j)
            seen[j] = True
            j = perm[j]
        cycles.append(cyc)
    return cycles


def propose_reassignment(
    ir: PcbIR, group: PinSwapGroup
) -> tuple[tuple[int, int], ...] | None:
    """The pin-swap move generator's core: solve the min-cost bipartite
    matching, decompose the winning permutation into pairwise
    :meth:`precis.pcb.ir.PcbIR.swap_pins` calls (a fixed-pivot cycle
    decomposition — for cycle ``(i0, i1, ..., ik)`` the transpositions
    ``(i0,i1), (i0,i2), ..., (i0,ik)`` applied in order realize it), and
    return ``None`` when there is nothing to evaluate OR the optimal
    assignment already matches the current one (no beneficial swap
    found — degrade cleanly rather than propose a no-op)."""
    built = build_cost_matrix(ir, group)
    if built is None:
        return None
    movable, cost = built
    n = len(movable)
    identity_cost = sum(cost[i][i] for i in range(n))
    assign = _hungarian(cost)
    matched_cost = sum(cost[i][assign[i]] for i in range(n))
    if matched_cost >= identity_cost:
        return None
    pairs: list[tuple[int, int]] = []
    for cyc in _cycles(assign):
        if len(cyc) < 2:
            continue
        pivot = movable[cyc[0]]
        pairs.extend((pivot, movable[idx]) for idx in cyc[1:])
    if not pairs:
        return None
    return tuple(pairs)


__all__ = [
    "PinSwapGroup",
    "build_cost_matrix",
    "propose_reassignment",
    "total_group_crossings",
]

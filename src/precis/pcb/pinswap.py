"""Pin-swap warm-start helpers for the route job's legacy ``radial``
mode and the connected-via ``distance`` assignment.

The general assignment (supply/client roles, cross-instance, Euclidean
min-cost) lives in :mod:`precis.pcb.matching` and is applied by
``pcb_route._resolve_supply_matching`` before routing; this module keeps
only what ``pcb_route._apply_pin_swap_warm_start`` still calls:
:class:`PinSwapGroup` (a caller-supplied admissible set plus per-pin
offsets), :func:`offsets_from_ir`, :func:`propose_distance_assignment` and
:func:`propose_radial_assignment`. Nothing here invents an equivalence
class from a part number or footprint shape. The anneal no longer swaps
pins (``MoveKind.PIN_SWAP`` was removed with
``OptimizeConfig.pin_swap_groups``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from precis.pcb.geom import Point
from precis.pcb.ir import PcbIR, pin_point

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


# ── wiring real footprint geometry into a group's offsets ───────────────


def offsets_from_ir(
    ir: PcbIR, instance: int, pins: tuple[int, ...]
) -> dict[int, Point]:
    """Per-pin offsets read off the IR's OWN placed pin positions
    (:func:`precis.pcb.ir.pin_point` minus the instance origin) — the
    board-frame answer, for a group whose footprint has already been
    applied to the IR (``session.apply_real_pin_offsets``, wired into
    ``build_ir``).

    Preferred over :func:`offsets_from_pads` for anything that came
    through ``build_ir``, for three reasons that each made the pads path
    a silent no-op on a real part: (1) a cached EasyEDA/JLC footprint
    keys its pads by raw ``number`` (``"1"``..``"80"``) and names the pin
    via a separate ``pin_map`` — ``offsets_from_pads`` matches ``number``
    against :attr:`PcbIR.pin_label` directly, so every pin of an HV507
    (or any part with named pins) missed and collapsed to the centroid,
    where every swap scores zero; (2) a bottom-mounted instance is
    MIRRORED and (3) any instance may be rotated — both already folded
    into ``pin_point`` and neither into a raw pad list. Found wiring
    pin swap through ``op='route'`` (docs/backlog/pcb-ewod-multitile.md
    ruling 6, 2026-09-19): the dogfood settled 0 swaps until this. A pin
    still unplaced (``pin_point`` is ``None``) or with no real offset is
    left absent, which the group's own centroid default handles."""
    x0, y0 = float(ir.inst_x[instance]), float(ir.inst_y[instance])
    offsets: dict[int, Point] = {}
    for pin in pins:
        pt = pin_point(ir, pin)
        if pt is None:
            continue
        dx, dy = pt[0] - x0, pt[1] - y0
        if dx == 0.0 and dy == 0.0:
            continue
        offsets[pin] = (dx, dy)
    return offsets


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
    ``instance`` — the far endpoint at its own PIN position
    (:func:`precis.pcb.ir.pin_point`: the instance origin plus the real
    per-pin offset ``build_ir`` applied, or the bare centroid when none
    is known). Until 2026-09-19 this used the far instance's centroid
    outright, which made the whole evaluator blind whenever every far
    end sits on ONE instance: an ``ewod_pad_array`` is a single component
    carrying all 64 electrode pins, so every airwire from the HV507 sink
    converged on the same point and no swap could ever change a crossing
    count (the dogfood settled 0 swaps with a correctly resolved group —
    docs/backlog/pcb-ewod-multitile.md ruling 6)."""
    edges: dict[int, list[Point]] = {}
    for seg_id, near_pin in _segments_near_pin(ir, instance).items():
        a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
        other = b if a == near_pin else a
        far = pin_point(ir, other)
        if far is None:
            continue
        edges.setdefault(near_pin, []).append(far)
    return edges


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


def propose_distance_assignment(
    ir: PcbIR, group: PinSwapGroup, far_by_pin: dict[int, Point]
) -> tuple[tuple[tuple[int, int], ...], float] | None:
    """Match nets to pads by Manhattan distance to their fixed-island vias.

    The caller resolves actual connected terminals, never electrode centroids
    or a generator ledger. Incomplete groups fall back to radial assignment.
    An empty permutation is a valid optimum, distinct from unavailable geometry.
    """
    pins = [p for p in group.pins if p not in group.excluded]
    if len(pins) < 2 or any(
        p not in far_by_pin or p not in group.offsets for p in pins
    ):
        return None
    x, y = float(ir.inst_x[group.instance]), float(ir.inst_y[group.instance])
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    near = [_pin_pos(x, y, group, p) for p in pins]
    # Stable rows make tied optima independent of a stored prior swap.
    sources = sorted(pins, key=lambda p: str(ir.net_name[int(ir.pin_net[p])]))
    cost = [
        [abs(far_by_pin[p][0] - nx) + abs(far_by_pin[p][1] - ny) for nx, ny in near]
        for p in sources
    ]
    assign = _hungarian(cost)
    # Prefer shorter straight-line chords only among Manhattan optima.
    # An independent Euclidean solve is a tie heuristic, not a claim of
    # lexicographic optimality; retain the primary solve when it costs more.
    chords = [
        [math.hypot(far_by_pin[p][0] - nx, far_by_pin[p][1] - ny) for nx, ny in near]
        for p in sources
    ]
    alternate = _hungarian(chords)
    optimum = math.fsum(cost[i][j] for i, j in enumerate(assign))
    alternate_cost = math.fsum(cost[i][j] for i, j in enumerate(alternate))
    if abs(alternate_cost - optimum) <= 4 * math.ulp(optimum) and math.fsum(
        chords[i][j] for i, j in enumerate(alternate)
    ) < math.fsum(chords[i][j] for i, j in enumerate(assign)):
        assign = alternate
    targets = {p: pins[assign[i]] for i, p in enumerate(sources)}
    index = {p: i for i, p in enumerate(pins)}
    permutation = [index[targets[p]] for p in pins]
    pairs: list[tuple[int, int]] = []
    for cycle in _cycles(permutation):
        pivot = pins[cycle[0]]
        pairs.extend((pivot, pins[i]) for i in cycle[1:])
    return tuple(pairs), math.fsum(cost[i][j] for i, j in enumerate(assign))


def propose_radial_assignment(
    ir: PcbIR, group: PinSwapGroup
) -> tuple[tuple[int, int], ...] | None:
    """A planar warm start for a ring-shaped group: match pins to their
    airwires' far ends in CYCLIC ANGULAR ORDER about the instance origin.

    For the one shape that dominates here — an
    HV507-class sink under an electrode field, its pads on a ring, every
    far end a plaza via inside or around that ring (docs/backlog/
    pcb-ewod-multitile.md ruling 6, "pin swaps make routing trivial") —
    the non-crossing assignment IS known in closed form: sort the pins by
    angle, sort the far ends by angle, and match them in the same cyclic
    order; two chords of a ring cross exactly when their endpoint orders
    interleave, so an order-preserving matching has none. The one free
    choice is the cyclic rotation (which pin the first far end takes);
    every rotation is tried and the one with the smallest total airwire
    length wins, which also keeps each via near its own pad.

    Only pins with exactly one airwire take part (a pin with none has
    nothing to place; several would need a dart-order permutation the IR
    does not support — same limit as :meth:`PcbIR.swap_pins`). Returns
    the transpositions that realize the permutation (the same fixed-pivot
    cycle decomposition :func:`propose_distance_assignment` uses) or ``None``
    when fewer than two pins qualify or the order already holds."""
    movable = [p for p in group.pins if p not in group.excluded]
    if len(movable) < 2:
        return None
    inst_x, inst_y = float(ir.inst_x[group.instance]), float(ir.inst_y[group.instance])
    if math.isnan(inst_x) or math.isnan(inst_y):
        return None
    edges = _instance_edges(ir, group.instance)
    pins = [p for p in movable if len(edges.get(p, ())) == 1]
    if len(pins) < 2:
        return None
    near = {p: _pin_pos(inst_x, inst_y, group, p) for p in pins}
    far = {p: edges[p][0] for p in pins}

    def _ang(pt: Point) -> float:
        return math.atan2(pt[1] - inst_y, pt[0] - inst_x)

    pins_by_angle = sorted(pins, key=lambda p: _ang(near[p]))
    # The far ends travel with their CURRENT pin; sort the far ends
    # themselves, then decide which pin each one should land on.
    fars_by_angle = sorted(pins, key=lambda p: _ang(far[p]))
    n = len(pins)
    best: tuple[float, int] | None = None
    for shift in range(n):
        total = 0.0
        for k in range(n):
            dst = pins_by_angle[(k + shift) % n]
            fx, fy = far[fars_by_angle[k]]
            nx, ny = near[dst]
            total += math.hypot(fx - nx, fy - ny)
        if best is None or total < best[0]:
            best = (total, shift)
    assert best is not None
    shift = best[1]
    # target[src_pin] = the pin its net should move to.
    target = {fars_by_angle[k]: pins_by_angle[(k + shift) % n] for k in range(n)}
    index = {p: i for i, p in enumerate(pins)}
    assign = [index[target[p]] for p in pins]
    pairs: list[tuple[int, int]] = []
    for cyc in _cycles(assign):
        if len(cyc) < 2:
            continue
        pivot = pins[cyc[0]]
        pairs.extend((pivot, pins[idx]) for idx in cyc[1:])
    return tuple(pairs) or None


__all__ = [
    "PinSwapGroup",
    "offsets_from_ir",
    "propose_distance_assignment",
    "propose_radial_assignment",
]

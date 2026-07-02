"""Auto-kinda-place + route-feasibility estimate (ADR 0042 §9).

Continuous (no-grid) placement that **minimizes the crossing count** Slice 4
measures — plus ratsnest length and the `soft` measures — by force-directed
seeding then simulated annealing. `fixed` instances never move.

v1 works at component-centroid granularity (pins ≈ the instance centroid),
so it optimizes **translation**; rotation has no effect on the crossing metric
until real pad offsets land (Slice 2 footprints), at which point the same loop
gains a rotate move. Pure / deterministic given `seed` — no DB, no GL.

Each anneal move perturbs exactly one refdes, so the loop scores it with a
**delta objective**: only the moved part's nets are re-MSTed and re-checked
for crossings against the (unchanged) rest, and only the measures naming that
part are re-penalised — O(deg·W) per iteration instead of the O(W²) full
rebuild, which at route-round-trip iteration counts (1500→4500) is the
difference between sub-second and minutes inside one MCP call.

The actual place↔route round-trip against Freerouting (ADR 0042 §9, §13a) is
wired in Slice 6 (the export/router integration); this module is the placer +
the H/V route-*feasibility* estimate it hands the LLM in the meantime.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from precis.pcb import ratsnest
from precis.pcb.eyes import measure_bound
from precis.pcb.geom import Point, bbox, bboxes_disjoint, dist, segments_cross

# Objective weights: a crossing is expensive (it's the headline), length is the
# tie-breaker, a violated soft measure sits between.
W_CROSS = 100.0
W_LEN = 1.0
W_MEASURE = 10.0


@dataclass(frozen=True, slots=True)
class PlaceResult:
    positions: dict[str, Point]
    crossings_before: int
    crossings_after: int
    length_before: float
    length_after: float
    objective_before: float
    objective_after: float
    iters: int


@dataclass(frozen=True, slots=True)
class _MeasureSpec:
    """A placement-drivable measure, pre-resolved: operands and role
    membership are fixed for the whole anneal — only positions move."""

    refs: tuple[str, ...]
    bound: str  # lower | upper | target (eyes.measure_bound)
    goal: float
    weight: float


def _measure_specs(
    measures: list[dict],
    positions: dict[str, Point],
    roles: dict[str, set[str]],
) -> list[_MeasureSpec]:
    out: list[_MeasureSpec] = []
    for m in measures:
        if (m.get("strength") or "gauge") == "gauge":
            continue
        metric = (m.get("metric") or "").lower()
        if metric not in ("separation", "proximity"):
            continue
        goal = m.get("goal")
        if goal is None:
            continue
        refs = _resolve(m.get("operands") or [], positions, roles)
        if len(refs) < 2:
            continue
        # NB `is None`, not `or`: weight=0 is the author's way to record a
        # measure while taking it out of the objective.
        weight = 1.0 if m.get("weight") is None else float(m["weight"])
        out.append(
            _MeasureSpec(
                refs=tuple(refs),
                bound=measure_bound(m.get("direction"), metric),
                goal=float(goal),
                weight=weight,
            )
        )
    return out


def _spec_penalty(spec: _MeasureSpec, positions: dict[str, Point]) -> float:
    """Continuous penalty for one measure: 0 when satisfied, grows with the
    violation. The binding pair follows the bound (eyes._judge semantics)."""
    refs = spec.refs
    gaps = [
        dist(positions[refs[i]], positions[refs[j]])
        for i in range(len(refs))
        for j in range(i + 1, len(refs))
    ]
    if spec.bound == "lower":  # keep apart → penalise the closest pair
        return spec.weight * max(0.0, spec.goal - min(gaps))
    if spec.bound == "upper":  # keep close → penalise the furthest pair
        return spec.weight * max(0.0, max(gaps) - spec.goal)
    val = max(gaps, key=lambda g: abs(g - spec.goal))  # target → furthest off
    return spec.weight * abs(val - spec.goal)


def _measure_penalty(
    positions: dict[str, Point],
    measures: list[dict],
    roles: dict[str, set[str]],
) -> float:
    """Continuous penalty for violated `soft`/`hard` placement measures
    (separation / proximity). 0 when satisfied; grows with the violation."""
    specs = _measure_specs(measures, positions, roles)
    return sum(_spec_penalty(s, positions) for s in specs)


def _resolve(
    operands: list[dict], positions: dict[str, Point], roles: dict[str, set[str]]
) -> list[str]:
    out: list[str] = []
    for op in operands or []:
        if "instance" in op and op["instance"] in positions:
            out.append(op["instance"])
        elif "role" in op:
            out += [r for r, rs in roles.items() if op["role"] in rs and r in positions]
    return list(dict.fromkeys(out))


def _objective(
    positions: dict[str, Point],
    nets: list[dict],
    measures: list[dict],
    roles: dict[str, set[str]],
) -> tuple[float, int, float]:
    wires = ratsnest.build_airwires(positions, nets)
    n_cross = len(ratsnest.crossings(wires))
    length = ratsnest.total_length(wires)
    pen = _measure_penalty(positions, measures, roles)
    return W_CROSS * n_cross + W_LEN * length + W_MEASURE * pen, n_cross, length


def _count_cross(ws_a: list[ratsnest.Airwire], ws_b: list[ratsnest.Airwire]) -> int:
    """Crossings between two wire sets (different nets only) — the delta leg."""
    n = 0
    boxes_b = [bbox(w.p1, w.p2) for w in ws_b]
    for wa in ws_a:
        ba = bbox(wa.p1, wa.p2)
        for wb, bb in zip(ws_b, boxes_b):
            if wa.net == wb.net:
                continue
            if bboxes_disjoint(ba, bb):
                continue
            if segments_cross(wa.p1, wa.p2, wb.p1, wb.p2):
                n += 1
    return n


def autoplace(
    instances: list[dict],
    nets: list[dict],
    *,
    measures: list[dict] | None = None,
    iters: int = 1500,
    seed: int = 0,
) -> PlaceResult:
    """Place the non-`fixed` instances to minimize crossings + length + soft
    measures. ``instances`` rows carry ``refdes, x, y, fixed, roles``.

    Returns a :class:`PlaceResult` with the new positions (for *all* movable
    instances) + before/after metrics. Deterministic given ``seed``.
    """
    measures = measures or []
    rng = random.Random(seed)
    roles = {i["refdes"]: set(i.get("roles") or []) for i in instances}
    fixed = {i["refdes"] for i in instances if (i.get("fixed") or "") in ("xy", "both")}
    movable = [i["refdes"] for i in instances if i["refdes"] not in fixed]

    # board area: a square sized to the part count (centroid placement).
    n = len(instances)
    side = max(20.0, 5.0 * math.sqrt(max(n, 1)))

    # seed: keep placed coords; scatter the unplaced movable parts.
    pos: dict[str, Point] = {}
    for i in instances:
        if i.get("x") is not None and i.get("y") is not None:
            pos[i["refdes"]] = (float(i["x"]), float(i["y"]))
        elif i["refdes"] in fixed:
            pos[i["refdes"]] = (0.0, 0.0)  # a fixed part should carry coords
        else:
            pos[i["refdes"]] = (rng.uniform(0, side), rng.uniform(0, side))

    obj0, cross0, len0 = _objective(pos, nets, measures, roles)
    if not movable or len(pos) < 2:
        return PlaceResult(pos, cross0, cross0, len0, len0, obj0, obj0, 0)

    # ── incremental objective state ──────────────────────────────────
    # Per signal net: its current airwires. Plane nets never produce wires,
    # so they are dropped here rather than rebuilt to [] every move.
    relevant = [
        net
        for net in nets
        if (net.get("net_class") or "").strip().lower() not in ratsnest.PLANE_CLASSES
    ]
    net_wires: list[list[ratsnest.Airwire]] = [
        ratsnest.build_airwires(pos, [net]) for net in relevant
    ]
    nets_of: dict[str, list[int]] = {}
    for k, net in enumerate(relevant):
        for m in net.get("members") or []:
            rd = m["refdes"] if isinstance(m, dict) else m
            if rd in pos and k not in nets_of.setdefault(rd, []):
                nets_of[rd].append(k)

    specs = _measure_specs(measures, pos, roles)
    pen_cache = [_spec_penalty(s, pos) for s in specs]
    specs_of: dict[str, list[int]] = {}
    for si, s in enumerate(specs):
        for rd in s.refs:
            specs_of.setdefault(rd, []).append(si)

    best = dict(pos)
    best_obj = obj0
    cur = dict(pos)
    cur_obj = obj0
    temp = side / 2.0
    cooling = 0.997
    for _ in range(iters):
        rd = movable[rng.randrange(len(movable))]
        old = cur[rd]
        step = max(0.5, temp)
        cur[rd] = (
            min(side, max(0.0, old[0] + rng.gauss(0, step))),
            min(side, max(0.0, old[1] + rng.gauss(0, step))),
        )

        # delta: only rd's nets change wires; only rd's measures change penalty
        aff = nets_of.get(rd, [])
        aff_set = set(aff)
        old_flat = [w for k in aff for w in net_wires[k]]
        others = [w for k, ws in enumerate(net_wires) if k not in aff_set for w in ws]
        new_per_net = [ratsnest.build_airwires(cur, [relevant[k]]) for k in aff]
        new_flat = [w for ws in new_per_net for w in ws]
        d_cross = (
            _count_cross(new_flat, others)
            + len(ratsnest.crossings(new_flat))
            - _count_cross(old_flat, others)
            - len(ratsnest.crossings(old_flat))
        )
        d_len = sum(w.length for w in new_flat) - sum(w.length for w in old_flat)
        touched = specs_of.get(rd, [])
        new_pens = [(si, _spec_penalty(specs[si], cur)) for si in touched]
        d_pen = sum(p - pen_cache[si] for si, p in new_pens)

        new_obj = cur_obj + W_CROSS * d_cross + W_LEN * d_len + W_MEASURE * d_pen
        delta = new_obj - cur_obj
        if delta < 0 or rng.random() < math.exp(-delta / max(temp, 1e-6)):
            cur_obj = new_obj
            for k, ws in zip(aff, new_per_net):
                net_wires[k] = ws
            for si, p in new_pens:
                pen_cache[si] = p
            if new_obj < best_obj:
                best, best_obj = dict(cur), new_obj
        else:
            cur[rd] = old  # reject
        temp *= cooling

    obj1, cross1, len1 = _objective(best, nets, measures, roles)
    return PlaceResult(best, cross0, cross1, len0, len1, obj0, obj1, iters)


def route_feasibility(airwires: list[ratsnest.Airwire]) -> dict[str, float | int]:
    """A coarse H/V routability estimate (ADR 0042 §9) — NOT real routing.

    Assign each airwire to the horizontal or vertical signal layer by its
    dominant direction, then count the **residual same-layer crossings** — each
    needs a via to escape — as the via estimate. Labelled as an estimate.
    """
    h: list[ratsnest.Airwire] = []
    v: list[ratsnest.Airwire] = []
    for w in airwires:
        (h if abs(w.p1[0] - w.p2[0]) >= abs(w.p1[1] - w.p2[1]) else v).append(w)
    residual = len(ratsnest.crossings(h)) + len(ratsnest.crossings(v))
    return {
        "airwires": len(airwires),
        "h_layer": len(h),
        "v_layer": len(v),
        "residual_crossings": residual,
        "vias_estimate": residual,
    }

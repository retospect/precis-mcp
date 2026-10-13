"""Router evidence -> the anneal's outer-loop contract
(:mod:`precis.pcb.optimize`'s :class:`RouteOutcome`).

Sits between :mod:`precis.pcb.realize` (which produces the raw
:class:`~precis.pcb.realize.RealizeResult`) and
:func:`precis.pcb.optimize.anneal_route_loop` (which consumes a
``route(ir) -> RouteOutcome`` callable). One adapter,
:func:`outcome_from_realize`, keeps the mapping honest in exactly one
place: failure causes pass through verbatim (an unknown cause stays
unknown — the backlog's ban on re-labelling it as congestion), escapes
are classified by the router's own cause kind, and the routed/total
counts are per-NET — a net is routed only when every one of its
segments is — matching the ``pcb_routes.status`` honesty the route job
already enforces.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from precis.pcb.optimize import DemandCell, RouteFailure, RouteOutcome
from precis.pcb.realize import pin_point

if TYPE_CHECKING:
    from precis.pcb.ir import PcbIR
    from precis.pcb.realize import RealizeResult


def _pin_xy(ir: PcbIR, pid: int) -> tuple[float, float] | None:
    """The terminal's actual pad position (``pin_point``), or the owning
    instance centre when the pad's own offset is unresolvable — the pad
    neighbourhood an escape penalty belongs to, not a guessed region."""
    point = pin_point(ir, pid)
    if point is not None:
        return (float(point[0]), float(point[1]))
    return _inst_xy(ir, int(ir.pin_instance[pid]))


def _inst_xy(ir: PcbIR, inst: int) -> tuple[float, float] | None:
    x, y = float(ir.inst_x[inst]), float(ir.inst_y[inst])
    if math.isnan(x) or math.isnan(y):
        return None
    return (x, y)


def _seg_points(ir: PcbIR, seg_id: int) -> tuple[tuple[float, float], ...]:
    """The segment's implicated board positions — both endpoint instance
    centres plus the midpoint, the engine's own centroid granularity.
    Unplaced endpoints contribute nothing (evidence stays on the failure
    itself; a point that doesn't exist cannot implicate a cell)."""
    a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
    ia, ib = int(ir.pin_instance[a]), int(ir.pin_instance[b])
    out: list[tuple[float, float]] = []
    for inst in (ia, ib):
        xy = _inst_xy(ir, inst)
        if xy is not None:
            out.append(xy)
    if len(out) == 2:
        out.append(((out[0][0] + out[1][0]) / 2.0, (out[0][1] + out[1][1]) / 2.0))
    return tuple(out)


def outcome_from_realize(ir: PcbIR, rres: RealizeResult) -> RouteOutcome:
    """Map one :class:`RealizeResult` to the outer loop's
    :class:`RouteOutcome`.

    - ``unrouted`` segments -> :class:`RouteFailure`, cause from
      ``unrouted_reasons`` (``'unknown'`` when the router gave none —
      preserved, not guessed). Escape causes (:data:`_ESCAPE_CAUSES`)
      tie to the segment's actual terminal instances; every other cause
      implicates the segment's span cells.
    - ``congestion_cells`` (PathFinder's final present congestion,
      grid-to-mm already resolved by the realizer) -> ``demand``,
      provenance (layer, grid pitch) carried through.
    - ``warnings`` (the hard grid's own over-capacity gaps — the only
      congestion evidence a negotiate=0 run produces) join ``demand``
      too, at each warned instance's position.
    - ``routed``/``total`` are PER NET: a net with any segment left
      unrouted is not routed. Dangling nets (no segments) count neither
      — same honest denominator the baseline pin uses.
    """
    reason_by_seg = {r.seg_id: r for r in rres.unrouted_reasons}
    failures: list[RouteFailure] = []
    for seg_id in rres.unrouted:
        net_id = int(ir.seg_net[seg_id])
        net = str(ir.net_name[net_id])
        reason = reason_by_seg.get(seg_id)
        cause = reason.kind if reason else "unknown"
        # An escape is named by EVIDENCE, not cause vocabulary: the
        # terminal pins the router implicated (`UnroutedReason.pins`).
        # The penalise step gets exactly those terminals' pad
        # neighbourhoods — never both endpoints plus the midpoint, and
        # a span-level kind (``'no_path'`` included) stays a span
        # failure when the router named no end.
        pins = tuple(reason.pins) if reason else ()
        if pins:
            insts = tuple(dict.fromkeys(int(ir.pin_instance[p]) for p in pins))
            points = tuple(xy for p in pins if (xy := _pin_xy(ir, p)) is not None)
        else:
            a, b = int(ir.seg_pin_a[seg_id]), int(ir.seg_pin_b[seg_id])
            insts = (int(ir.pin_instance[a]), int(ir.pin_instance[b]))
            points = _seg_points(ir, seg_id)
        failures.append(
            RouteFailure(
                net=net,
                cause=cause,
                points=points,
                escape=bool(pins),
                instances=insts,
                seg_id=seg_id,
                detail=reason.message if reason else "",
            )
        )
    demand = [
        DemandCell(
            x_mm=c.x_mm,
            y_mm=c.y_mm,
            usage=c.usage,
            layer=c.layer,
            pitch_mm=c.pitch_mm,
        )
        for c in rres.congestion_cells
    ]
    # The hard grid's own over-capacity gaps are a DIFFERENT demand unit
    # than PathFinder's per-cell claim count — a gap's `usage` is nets
    # wanting through a gap of `capacity` strands, not contested cell
    # claims. They ride the same DemandCell channel but with their own
    # source/capacity, so `apply_route_feedback` normalises them
    # separately (`_GAP_WARNING_FULL_OVERAGE`) and the digest's units
    # line names the difference.
    for w in rres.warnings:
        for inst in w.participants_key:
            xy = _inst_xy(ir, inst)
            if xy is not None:
                demand.append(
                    DemandCell(
                        x_mm=xy[0],
                        y_mm=xy[1],
                        usage=w.usage,
                        capacity=w.capacity,
                        pitch_mm=w.pitch_mm,
                        source="gap_warning",
                    )
                )
    unrouted_segs = set(rres.unrouted)
    covered = {int(t.seg_id) for t in rres.tracks} | set(rres.fixed_realized)
    net_segs: dict[int, list[int]] = {}
    for s in range(ir.n_segments):
        net_segs.setdefault(int(ir.seg_net[s]), []).append(s)
    total = len(net_segs)
    routed = sum(1 for segs in net_segs.values() if all(s in covered for s in segs))
    return RouteOutcome(
        routed=routed,
        total=total,
        complete=not unrouted_segs,
        failures=tuple(failures),
        demand=tuple(demand),
    )


__all__ = ["outcome_from_realize"]

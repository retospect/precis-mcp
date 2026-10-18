"""Route clean-up post-pass — move another net's blocking via so a kinked
track runs straight (gr495397; Reto 2026-10-18 "gr495397 is good to go").

No slice of ``docs/backlog/pcb-vector-router.md`` covers this: its slice 7
SCP cleanup runs under ``router='topo'`` only, and ``realize._shove_vias``
moves a route's OWN via while straightening it, never another net's.

**Where it runs.** :func:`precis.pcb.realize._realize_maze`, after the
winning reorder/negotiation pass is chosen and before pours, stitching and
diagnosis, when :attr:`~precis.pcb.realize.RealizeConfig.route_cleanup` is
on and the caller supplied a DRC check (:data:`~precis.pcb.realize.
CleanupCheck`; the route job does).

**No incremental un-claim.** The occupancy grid cannot release one net's
cells (:func:`~precis.pcb.realize._realize_maze`'s docstring), so a trial
is a replay: a fresh grid, the static claims, every kept segment's
recorded path stamped verbatim in its original commit order
(:func:`~precis.pcb.realize._route_pass`'s ``kept``), then the two ripped
nets through the ordinary search, snap, straighten and commit. Kept copper
comes out identical, and the ripped nets' copper is as legal on the grid
as any first-pass route.

**A move** takes a victim segment (more avoidable bends than
:data:`MAX_AVOIDABLE_BENDS`, or longer than :data:`MAX_DETOUR_RATIO` times
its octilinear shortest distance — the 45-degree yardstick, since the
router cannot beat it) and up to :data:`VIAS_PER_VICTIM` vias of other
routed nets whose keep-out (via radius + clearance + victim half-width)
meets the victim's straight chord or either octilinear dogleg. Vias of
plane fan-outs, stitching and fixed copper never move. It rips both nets
whole (a net is the unit that moves, as in ``realize._negotiate``),
victim first. The move is kept only if both nets route everything they
routed before, every new via stays within :data:`MOVE_RADIUS_MM` of one
of its net's old vias, no net uses a new layer, length, bends and vias
(over the two nets, which equals the board total since nothing else
moved) all hold with one improving, and no ``(rule, severity)`` count of
the caller's DRC check rises — errors and warns alike. An accepted move
is the new baseline and the victim list is recomputed.

**Bounds**: ``RealizeConfig.cleanup_max_moves`` trials and
``cleanup_budget_s`` seconds per :func:`~precis.pcb.realize.realize` call,
checked before each trial. The route job prints
:meth:`~precis.pcb.realize.CleanupReport.line`.

A rejected move leaves the outcome untouched, so a board with no
improving move is byte-identical to ``route_cleanup=False``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from precis.pcb import maze, realize
from precis.pcb.geom import Point, dist_point_to_segment
from precis.pcb.ir import PcbIR

#: A victim has more AVOIDABLE bends than this: bends beyond the one its
#: shortest 45-degree path needs (:func:`octilinear_min_bends`; a layer
#: change is not a bend). The U1-C2 kink of gr495397 is two avoidable
#: bends on a track that would otherwise run straight.
MAX_AVOIDABLE_BENDS = 0
#: ... or a centreline longer than this multiple of its octilinear shortest
#: distance, by at least :data:`MIN_DETOUR_MM`.
MAX_DETOUR_RATIO = 1.15
MIN_DETOUR_MM = 0.1
#: Pins this close to a 0/45/90-degree line count as aligned.
_ALIGN_MM = 1e-3
#: Blocking vias tried per victim, nearest the chord first.
VIAS_PER_VICTIM = 3
#: A re-routed net's every new via lies within this of one of its old vias.
MOVE_RADIUS_MM = 1.0
#: The smallest length change counted as an improvement.
MIN_LENGTH_GAIN_MM = 0.01

_EPS = 1e-6

Outcome = tuple[
    list[realize.RealizedTrack],
    list[realize.RealizedVia],
    list[int],
    dict[int, str],
    list[int],
]


@dataclass(frozen=True, slots=True)
class _Score:
    length_mm: float
    bends: int
    vias: int


def path_length_and_bends(path: maze.RoutePath) -> tuple[float, int]:
    """Centreline length (mm, in-plane spans only) and bend count: a
    direction change between consecutive spans on one layer."""
    length = 0.0
    bends = 0
    prev: tuple[float, float] | None = None
    for a, b in zip(path.points, path.points[1:], strict=False):
        if a[2] != b[2]:
            prev = None
            continue
        dx, dy = b[0] - a[0], b[1] - a[1]
        span = math.hypot(dx, dy)
        if span < _EPS:
            continue
        length += span
        d = (dx / span, dy / span)
        if prev is not None:
            cross = prev[0] * d[1] - prev[1] * d[0]
            dot = prev[0] * d[0] + prev[1] * d[1]
            if abs(cross) > _EPS or dot < 0.0:
                bends += 1
        prev = d
    return length, bends


def octilinear_distance(a: Point, b: Point) -> float:
    """The shortest 45-degree path between ``a`` and ``b``."""
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    return max(dx, dy) + (math.sqrt(2.0) - 1.0) * min(dx, dy)


def _doglegs(a: Point, b: Point) -> list[tuple[Point, Point]]:
    """The straight chord plus both octilinear doglegs, as spans."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    m = min(abs(dx), abs(dy))
    sx, sy = math.copysign(m, dx), math.copysign(m, dy)
    diag_first = (a[0] + sx, a[1] + sy)
    diag_last = (b[0] - sx, b[1] - sy)
    return [(a, b), (a, diag_first), (diag_first, b), (a, diag_last), (diag_last, b)]


def octilinear_min_bends(a: Point, b: Point) -> int:
    """Bends the shortest 45-degree path needs: 0 when ``a`` and ``b`` line
    up on a multiple of 45 degrees, else 1."""
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    aligned = dx < _ALIGN_MM or dy < _ALIGN_MM or abs(dx - dy) < _ALIGN_MM
    return 0 if aligned else 1


def _is_victim(path: maze.RoutePath) -> tuple[float, int] | None:
    """``(detour excess mm, avoidable bends)`` ranking a victim, or
    ``None`` if it is not one."""
    a = (path.points[0][0], path.points[0][1])
    b = (path.points[-1][0], path.points[-1][1])
    shortest = octilinear_distance(a, b)
    length, bends = path_length_and_bends(path)
    excess = length - shortest
    avoidable = bends - octilinear_min_bends(a, b)
    detoured = excess >= MIN_DETOUR_MM and length > MAX_DETOUR_RATIO * shortest
    if avoidable > MAX_AVOIDABLE_BENDS or detoured:
        return max(excess, 0.0), avoidable
    return None


def _candidates(
    ir: PcbIR,
    ctx: realize._RouteContext,
    stamped: Mapping[int, maze.RoutePath],
    vias: list[realize.RealizedVia],
) -> Iterator[tuple[int, realize.RealizedVia]]:
    """``(victim seg, blocking via)`` pairs, worst victim first (most
    excess length, then most avoidable bends)."""
    victims = []
    for seg_id, path in stamped.items():
        rank = _is_victim(path)
        if rank is not None:
            victims.append((-rank[0], -rank[1], seg_id))
    victims.sort()
    movable = [v for v in vias if v.seg_id in stamped]
    for _excess, _bends, seg_id in victims:
        path = stamped[seg_id]
        net_id = int(ir.seg_net[seg_id])
        a = (path.points[0][0], path.points[0][1])
        b = (path.points[-1][0], path.points[-1][1])
        spans = _doglegs(a, b)
        half_w = ctx.rules_by_net[net_id].track_width_mm / 2.0
        gap = ctx.net_clearance.get(net_id, ctx.clearance)
        x0, x1 = min(a[0], b[0]), max(a[0], b[0])
        y0, y1 = min(a[1], b[1]), max(a[1], b[1])
        near = []
        for v in movable:
            if v.net_id == net_id:
                continue
            reach = v.dia_mm / 2.0 + gap + half_w
            if not (x0 - reach < v.x < x1 + reach and y0 - reach < v.y < y1 + reach):
                continue  # the chord and both doglegs lie inside the bbox
            d = min(dist_point_to_segment((v.x, v.y), p, q) for p, q in spans)
            if d < reach:
                near.append((d, v.x, v.y, v))
        near.sort(key=lambda t: (t[0], t[1], t[2]))
        for _d, _x, _y, v in near[:VIAS_PER_VICTIM]:
            yield seg_id, v


def _score(
    ir: PcbIR,
    nets: set[int],
    stamped: Mapping[int, maze.RoutePath],
    vias: list[realize.RealizedVia],
) -> _Score:
    length = 0.0
    bends = 0
    for seg_id, path in stamped.items():
        if int(ir.seg_net[seg_id]) in nets:
            seg_len, seg_bends = path_length_and_bends(path)
            length += seg_len
            bends += seg_bends
    return _Score(length, bends, sum(1 for v in vias if v.net_id in nets))


def _layers(path: maze.RoutePath) -> set[int]:
    return {p[2] for p in path.points}


def _move_is_legal(
    ir: PcbIR,
    nets: set[int],
    old: Mapping[int, maze.RoutePath],
    new: Mapping[int, maze.RoutePath],
) -> bool:
    """Every ripped segment routed again, on its net's old layers, with
    every new via near one of that net's old vias."""
    for net_id in nets:
        old_segs = [s for s in old if int(ir.seg_net[s]) == net_id]
        if any(s not in new for s in old_segs):
            return False
        old_layers = set().union(*(_layers(old[s]) for s in old_segs))
        new_layers = set().union(*(_layers(new[s]) for s in old_segs))
        if not new_layers <= old_layers:
            return False
        old_vias = [(x, y) for s in old_segs for x, y, _lo, _hi in old[s].vias]
        for s in old_segs:
            for x, y, _lo, _hi in new[s].vias:
                if not any(
                    math.hypot(x - ox, y - oy) <= MOVE_RADIUS_MM for ox, oy in old_vias
                ):
                    return False
    return True


def _trial(
    ir: PcbIR,
    ctx: realize._RouteContext,
    config: realize.RealizeConfig,
    stamped: Mapping[int, maze.RoutePath],
    victim_net: int,
    via_net: int,
) -> tuple[Outcome, dict[int, maze.RoutePath]] | None:
    """Replay the board with ``victim_net`` and ``via_net`` ripped and
    re-routed in that order; ``None`` when a ripped segment fails."""
    nets = (victim_net, via_net)
    kept = {s: p for s, p in stamped.items() if int(ir.seg_net[s]) not in nets}
    ripped = [s for net in nets for s in stamped if int(ir.seg_net[s]) == net]
    grid = maze.OccupancyGrid(
        ctx.spec, clearance_mm=ctx.clearance, net_clearance_mm=ctx.net_clearance
    )
    grid.set_body_mask(ctx.body_mask)
    new_stamped: dict[int, maze.RoutePath] = {}
    out = realize._route_pass(
        ir,
        list(kept) + ripped,
        ctx.plane_ids,
        grid,
        config,
        ctx.rules_by_net,
        ctx.pads,
        ctx.clearance,
        ctx.signal_layers,
        ctx.spec,
        ctx.pad_geoms,
        ink_field=ctx.ink_field,
        fixed_copper=ctx.fixed_copper,
        island_terminals=ctx.island_terminals,
        net_layers=ctx.net_layers,
        plane_served_pins=ctx.plane_served_pins,
        kept=kept,
        stamped=new_stamped,
    )
    if any(s not in new_stamped for s in ripped):
        return None
    return out, new_stamped


def cleanup(
    ir: PcbIR,
    ctx: realize._RouteContext,
    config: realize.RealizeConfig,
    outcome: Outcome,
    stamped: dict[int, maze.RoutePath],
    check: realize.CleanupCheck,
) -> tuple[Outcome, realize.CleanupReport]:
    """Run the pass over the winning ``outcome`` and its ``stamped`` paths
    (module docstring). Returns the outcome to keep — ``outcome`` itself
    when no move was kept — and the report."""
    started = time.monotonic()
    deadline = started + config.cleanup_budget_s
    tried = kept = 0
    length_saved = 0.0
    bends_saved = vias_saved = 0
    out_of_time = False
    # A trial depends only on the (victim net, via net) pair: the same two
    # nets ripped over the same kept copper re-route the same way. That
    # stays true after an accepted move (re-ripping the pair just accepted
    # reproduces it), so a pair is tried once per pass.
    seen: set[tuple[int, int]] = set()
    baseline_drc: Mapping[tuple[str, str], int] | None = None
    queue = list(_candidates(ir, ctx, stamped, outcome[1]))
    while tried < config.cleanup_max_moves:
        pair: tuple[int, int] | None = None
        while queue:
            seg_id, via = queue.pop(0)
            candidate = (int(ir.seg_net[seg_id]), via.net_id)
            if candidate not in seen:
                pair = candidate
                break
        if pair is None:
            break
        if time.monotonic() >= deadline:
            out_of_time = True
            break
        seen.add(pair)
        tried += 1
        victim_net, via_net = pair
        nets = {victim_net, via_net}
        trial = _trial(ir, ctx, config, stamped, victim_net, via_net)
        if trial is None:
            continue
        (t_tracks, t_vias, _unrouted, t_notes, _failed), t_stamped = trial
        if not _move_is_legal(ir, nets, stamped, t_stamped):
            continue
        before = _score(ir, nets, stamped, outcome[1])
        after = _score(ir, nets, t_stamped, t_vias)
        holds = (
            after.length_mm <= before.length_mm + _EPS
            and after.bends <= before.bends
            and after.vias <= before.vias
        )
        gains = (
            after.length_mm < before.length_mm - MIN_LENGTH_GAIN_MM
            or after.bends < before.bends
            or after.vias < before.vias
        )
        if not (holds and gains):
            continue
        if baseline_drc is None:
            baseline_drc = check(ir, outcome[0], outcome[1])
        drc_after = check(ir, t_tracks, t_vias)
        if any(n > baseline_drc.get(key, 0) for key, n in drc_after.items()):
            continue
        ripped = {s for s in stamped if int(ir.seg_net[s]) in nets}
        notes = {s: n for s, n in outcome[3].items() if s not in ripped}
        notes.update(t_notes)
        outcome = (t_tracks, t_vias, outcome[2], notes, outcome[4])
        stamped = t_stamped
        baseline_drc = drc_after
        kept += 1
        length_saved += before.length_mm - after.length_mm
        bends_saved += before.bends - after.bends
        vias_saved += before.vias - after.vias
        queue = list(_candidates(ir, ctx, stamped, outcome[1]))
    report = realize.CleanupReport(
        kept=kept,
        tried=tried,
        length_saved_mm=length_saved,
        bends_saved=bends_saved,
        vias_saved=vias_saved,
        out_of_time=out_of_time,
        elapsed_s=time.monotonic() - started,
    )
    return outcome, report

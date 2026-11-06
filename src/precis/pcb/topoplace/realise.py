"""Phase 4 — the topo realiser (spec §5).

Turns the 2b wires (edge sequences on the mesh, taut ``points`` per
branch) into copper: a per-branch layer assignment, via sites as
``(edge_id, t)`` positions, and ``RealizedTrack``/``RealizedVia`` rows
emitted through the normal :mod:`precis.pcb.realize` store path —
never a second copper shape.

The layer assignment is the minimal slice-5 form of phase-1's
colouring, not a separate engine: pad sides fix wire ends (Decision
6 — a surface pad's mount side stays the terminal layer, TH pads are
free), the class ``layers`` rule narrows candidates
(:attr:`TopoDesign.net_layer_force`), and same-layer crossings are
found off the 2b ``order`` lists exactly as Decision 3 defines them —
two wires on one layer cross iff their relative order differs between
two edges both cross. The yielder at a crossing follows the Decision-6
table (TH net, then an F↔B pair, then an SMD-only net): the piece of
its wire spanning the shared edges flips to a free candidate layer —
an inner or the other outer (gr520902's runs/cuts model: a cut mints
a via on that edge, a run takes a layer) — and two via sites bracket
it. A crossing that survives the flip bound is reported on
:attr:`RoutePlan.crossings` for the caller to count as DRC, not hidden.

Via SITING is the planter's (G2g, topo S3/gr520901): the route search
sites each layer hop where the barrel clears every body a via must
clear, and :func:`assign_layers`' moves are legal only where a cut
holds such a point — the via the search admits IS the via committed,
nothing re-sites it. A branch whose join was never minted lands on
:attr:`RoutePlan.no_via` so the caller reports the net
``failed: no via site`` instead of committing keepout-breaking copper.

Wires are any-angle with few segments (Reto 2026-11-01, superseding the
2026-08-31 90/45 rule): :func:`_emit_shapes` pulls each piece's
spoke-offset polyline taut against the copper it must clear, and the
anti-short guard and :func:`realize_result` both read that one shape.

``net_rules`` keys net NAME → resolved :class:`~precis.pcb.rules.NetRules`
(the caller resolves per-net width/clearance the same way
``build_region_graph``'s consumers do); ``None`` takes the 2b default
pair, matching ``build_wires``' own fallback.

:func:`widen_power` is the ruling's post-route stage: GND/power tracks
widen where clearance allows, never below the net's rule width. The
clearance question is answered by an injected oracle — the caller's own
DRC over the emitted copper — so "allows" means exactly what the gate
accepts, not a second geometric estimate that could drift. The oracle
returns FINDINGS, not a count: a step that erases one error and mints
another (widening a rail can bridge its own broken pieces while it
eats a foreign pad's clearance) keeps an error count level, and was
accepted — gr524392. Only a step whose keyed findings are a
sub-multiset of the baseline's is kept.
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import itertools
import logging
import math
from collections import Counter
from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon

from precis.pcb.clearance import FAB_QUANTUM_MM
from precis.pcb.ratsnest import PLANE_CLASSES
from precis.pcb.rules import (
    JLC_VIA_HOLE_TO_HOLE_MM,
    NetRules,
    net_current_a_or_none,
    route_via,
    via_count_for_current,
)
from precis.pcb.topoplace import proxy as topo_proxy
from precis.pcb.topoplace import wires as _wires
from precis.pcb.topoplace.copper_index import (
    ANY_NET,
    Body,
    CopperIndex,
)
from precis.pcb.topoplace.crossings import (
    CROSS_EPS,
    Cross,
    Pt,
    branch_geom,
    crossing_events,
    edge_xy,
    resolve_crossings,
    seg_dist,
)
from precis.pcb.topoplace.design import _COPPER_ZONE_SAG_MM, TopoDesign, TopoZone
from precis.pcb.topoplace.masks import BoardMasks, corridor_score
from precis.pcb.topoplace.mesh import TopoMesh
from precis.pcb.topoplace.wires import (
    CorridorCut,
    CorridorOverflow,
    Unreachable,
    WirePath,
    Wires,
    obstacle_vertices,
)

log = logging.getLogger(__name__)

_DEFAULT = NetRules(track_width_mm=0.15, clearance_mm=0.15)

#: Fallback via geometry when no fab/net rule resolves one — the same
#: order of magnitude the fab floor uses; the store path always passes
#: resolved rules, so this only ever serves synthetic designs.
_VIA_DIA_MM = 0.6
_VIA_DRILL_MM = 0.3

#: Flip bound — a crossing the yielder can't resolve inside this many
#: passes is left on :attr:`RoutePlan.crossings` (bounded like shove:
#: a cap, not the mechanism).
_MAX_FLIPS = 200

#: Corridor mask cost: one width-aware spine crossing counts like this
#: fraction of the corridor blocked (pcb-layer-argmin).  Calibrated from
#: taz-leaf-local-56's wires.json corridor block; tune from data, not
#: vibes.
_MASK_CROSS_K = 0.25

#: The widen ladder, descending — the first step the DRC oracle accepts
#: wins. Uniform per net (a rail's tracks share one width, what the fab
#: and the reader both expect); 1.0 mm caps it — a rail wider than that
#: is pour territory, and the ruling is no pours.
_WIDEN_LADDER: tuple[float, ...] = (1.0, 0.8, 0.6, 0.5, 0.4, 0.35, 0.3, 0.25, 0.2)


@dataclass(frozen=True, slots=True)
class RoutePiece:
    """One branch's contiguous run on one layer — a track's polyline."""

    net: int  # net_pins index
    branch: int  # index into Wires.branches
    layer: int
    points: tuple[tuple[float, float], ...]
    #: The branch's endpoint pins (IR pin ids) — a ``pcb_routes`` sketch
    #  row keys a connection by its two pins, never by branch index.
    pin_a: int = -1
    pin_b: int = -1


@dataclass(frozen=True, slots=True)
class ViaSite:
    """A via at ``(edge, t)`` on the mesh — world ``(x, y)`` the point
    the planter qualified (G2g: the site the search or the resolver
    admitted IS the site kept — nothing re-sites it). ``dia_mm``/
    ``drill_mm`` are the pair it was qualified with; ``0.0`` means the
    net rule's own via_dia/via_drill (the comfortable house default)."""

    net: int
    branch: int
    edge: int
    t: float
    x: float
    y: float
    dia_mm: float = 0.0
    drill_mm: float = 0.0


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """Copper intent for one routed pose: layer-assigned pieces, via
    sites, and the diagnostics the summary/DRC read."""

    pieces: tuple[RoutePiece, ...]
    vias: tuple[ViaSite, ...]
    layers: tuple[int, ...]  # primary (first-piece) layer per branch
    crossings: tuple[tuple[int, int], ...] = ()  # unresolved same-layer pairs
    unreachable: tuple[int, ...] = ()  # net_pins indices with stranded comps
    #: The stranded components verbatim (:class:`Unreachable` records —
    #: stranded pins + walling owners), so a caller's failure note can
    #: name WHAT is cut off, not just the net. ``unreachable`` stays the
    #: net-level digest of this same list.
    stranded: tuple[Unreachable, ...] = ()
    #: Branches whose layer join found no keepout-clean via site —
    #: minted no via, emitted their pieces anyway (the failed net's
    #: honest geometry), and land on the caller's ``pcb_routes`` row as
    #: ``failed: no via site`` (gr520901).
    no_via: tuple[int, ...] = ()
    #: Why each ``no_via`` branch has none: the cut and why it minted
    #: none — a dropped planted hop, or the branch's own barrel spacing.
    no_via_reason: dict[int, str] = dataclasses.field(default_factory=dict)
    #: Nets (net_pins indices) whose emitted copper failed the geometric
    #: anti-short guard — withdrawn whole (all pieces and vias), never
    #: committed as shorting copper (gr524239). ``sketch_rows`` reports
    #: them ``failed: short-risk``. The plan's own ``pieces`` keep the
    #: withdrawn geometry for the record, exactly like ``crossings``.
    guarded: tuple[int, ...] = ()
    #: Nets whose emitted copper is not one connected piece joining all
    #: their pads — the emit-connectivity check (gr537831) withdraws
    #: them whole like ``guarded``; ``sketch_rows`` reports them
    #: ``failed: unconnected``. A falsely realized broken net must never
    #: reach DRC.
    severed: tuple[int, ...] = ()
    #: Nets withdrawn after the FIRST route pass (Reto 2026-10-31: SDA
    #: carried 16 segments around GND's taut polyline — and GND was
    #: never committed). A withdrawn net must not shape its neighbours:
    #: ``route_design`` re-routes the survivors with these nets' pins
    #: removed and keeps the first pass's geometry and problems for the
    #: withdrawn set only. ``sketch_rows`` attributes first-pass
    #: crossings to withdrawn nets alone.
    withdrawn: tuple[int, ...] = ()
    #: Plane nets (gr524239): ``(net, layer)`` per net_pins index that
    #: reaches a reserved plane layer by fanout vias instead of wires.
    planes: tuple[tuple[int, int], ...] = ()
    #: ``(net, pin)`` per plane-net SMD pad no legal fanout via site was
    #: found for — the net reports ``failed: no fanout via``.
    fanout_missing: tuple[tuple[int, int], ...] = ()
    #: Design-net index → barrels the net's current needs PER LAYER HOP,
    #: rated at the SITED diameter (:func:`_via_need`), kept only where
    #: it is > 1 and the net hopped at all: the router sites one barrel
    #: and this field names the shortfall — a stitched group is not
    #: planted yet, and no hop reports a silent single via.
    via_need: dict[int, int] = dataclasses.field(default_factory=dict)
    #: ``(net, pin)`` per plane-net SMD pad whose fanout via only the
    #: ring-agnostic fallback could site (gr-fanout-ring, Reto
    #: 2026-11-02): the preferred pass keeps the barrel outside every
    #: part's silk ring band so ``silk.part_outlines`` draws straight;
    #: where no such site exists the nearest legal site is taken — a
    #: ring with a via gap beats a pad with no via — and counted here so
    #: the run can still report how many rings it opens. Includes the
    #: pad-scaled rung sites ``_fanout_vias`` plants for a pad wider
    #: than the fixed ladder (G2d — the thermal slug's straight stub
    #: off its own copper, no escape cap; pre-route since round 5).
    fanout_fallback: tuple[tuple[int, int], ...] = ()
    #: ``(net, pin)`` per plane-net SMD pad that reached its via by a
    #: routed escape branch (G2d): no ring site existed — a via-barred
    #: zone, or a pad wider than the fanout reach — so the pad wires to
    #: the nearest plantable point on its own layer instead. Counted so
    #: the run can report how many pads routed out rather than dropping
    #: straight down.
    fanout_escape: tuple[tuple[int, int], ...] = ()
    #: ``(net, pin) -> mm`` for a ``fanout_missing`` pad whose only
    #: blocker was ``_ESCAPE_LEN_CAP_MM``: the shortest legal escape it
    #: would have needed (G2d round 3's recorded finding), measured from
    #: where the stub leaves the pad's own copper (round 4).
    fanout_missing_len: dict[tuple[int, int], float] = dataclasses.field(
        default_factory=dict
    )
    #: ``(net, pin) -> why`` for a ``fanout_missing`` pad (G2d round 6):
    #: ``no_corridor`` when its own layer is walled, ``no_site`` when no
    #: reachable point plants a via, ``over_cap`` when only the length
    #: cap blocks it (``fanout_missing_len`` has the mm), or
    #: ``isolated_pour`` when every reachable site lands on pour copper
    #: the pad would only anchor, never join.
    fanout_missing_reason: dict[tuple[int, int], str] = dataclasses.field(
        default_factory=dict
    )
    #: The wires' phase-3 corridor cuts, routed and refused-negotiation
    #: demand alike (``CorridorCut.refused`` tags the latter):
    #: ``_route_pass`` raises on routed demand alone — a refused cut is
    #: a capacity fact for the report, not a stop (G2h).
    overflow: tuple[CorridorCut, ...] = ()
    #: Final branch index -> original input wires, including branches dropped
    #: before emission. See Wires.branch_lineage for the whole-tree rethread
    #: rule; shaping and withdrawals retain this map unchanged.
    branch_lineage: tuple[frozenset[WirePath], ...] = ()
    #: Concrete descendant identities in the same full index space as layers.
    #: ``None`` only for unknown rows from hand-built multipass plans.
    #: Unlike ancestry unions, these distinguish siblings and retained copper.
    branch_ids: tuple[WirePath | None, ...] = ()

    @property
    def n_routed_nets(self) -> int:
        return len({p.net for p in self.pieces})


def _plan_failed_nets(plan: RoutePlan) -> set[int]:
    """The net indices ``sketch_rows`` will report ``failed`` for this
    plan — every problem family it reads, resolved to nets by the
    plan's own branch→net map."""
    net_by_branch = {p.branch: p.net for p in plan.pieces}
    out: set[int] = set(plan.unreachable) | set(plan.guarded) | set(plan.severed)
    out |= {u.net for u in plan.stranded}
    out |= {k for k, _pin in getattr(plan, "fanout_missing", ())}
    for a, b in plan.crossings:
        for br in (a, b):
            if br in net_by_branch:
                out.add(net_by_branch[br])
    for br in plan.no_via:
        if br in net_by_branch:
            out.add(net_by_branch[br])
    return out


def _pin_forced(d: TopoDesign, pin: int, n_layers: int) -> int | None:
    """The outer layer a pad's mount side fixes (SMD), or ``None`` for a
    through-hole pad — present on every layer, it forces nothing."""
    side = int(d.pin_side[pin])
    if side == 0:
        return 0
    if side == 1:
        return n_layers - 1
    return None


def _run_bounds(cuts: list[int], n_edges: int) -> list[tuple[int, int]]:
    """Inclusive edge-index bounds per run — run p covers
    ``(cuts[p-1] .. cuts[p]]`` with sentinels −1 / E−1, so a cut at
    ``E−1`` mints an EMPTY tail run (the pad-side stub past the end
    via) and a cut at 0 a leading stub covering just ``{0}``."""
    bounds = []
    prev = -1
    for c in cuts:
        bounds.append((prev + 1, c))
        prev = c
    bounds.append((prev + 1, n_edges - 1))
    return bounds


def _span_layers(
    cuts: list[int], runs: list[int], n_edges: int, lo: int, hi: int
) -> set[int]:
    """The layers covering edge indices ``(lo .. hi]`` — the region a
    flipped shared-edge pair brackets on this branch's own index
    space."""
    return {
        runs[p]
        for p, (rlo, rhi) in enumerate(_run_bounds(cuts, n_edges))
        if rlo <= hi and rhi >= lo + 1
    }


def _insort_cut(cuts: list[int], runs: list[int], c: int) -> None:
    """Insert a new cut (a via on ``edges[c]``): the run it splits
    keeps its layer on BOTH sides."""
    i = bisect.bisect_left(cuts, c)
    if i < len(cuts) and cuts[i] == c:
        return
    cuts.insert(i, c)
    runs.insert(i, runs[i])


def _merge_same(cuts: list[int], runs: list[int]) -> None:
    """Drop cuts between equal-layer runs — a via joining a layer to
    itself is dead."""
    p = 0
    while p < len(cuts):
        if runs[p] == runs[p + 1]:
            cuts.pop(p)
            runs.pop(p + 1)
        else:
            p += 1


def _fix_terminals(
    d: TopoDesign, b: WirePath, n_layers: int, cuts: list[int], runs: list[int]
) -> None:
    """Re-bind the pad-forced sides after a span move (Decision 6): a
    terminal run whose layer disagrees with its pad's mount side gets
    a stub run — a cut at 0 for pin_a's end, at E−1 for pin_b's — and
    where the boundary already exists the stub just relabels. Accepted
    limitation (gr520902 review): a span move that touches edge 0 or
    E−1 is undone by this re-bind — the stub cannot tell the move meant
    to own the terminal edge."""
    n_edges = len(b.edges)
    if n_edges < 1:
        return
    la = _pin_forced(d, b.pin_a, n_layers)
    if la is not None and runs[0] != la:
        if cuts and cuts[0] == 0:
            runs[0] = la
        else:
            cuts.insert(0, 0)
            runs.insert(0, la)
    lb = _pin_forced(d, b.pin_b, n_layers)
    if lb is not None and runs[-1] != lb:
        if cuts and cuts[-1] == n_edges - 1:
            runs[-1] = lb
        else:
            cuts.append(n_edges - 1)
            runs.append(lb)


def _set_span(
    d: TopoDesign,
    b: WirePath,
    n_layers: int,
    cuts: list[int],
    runs: list[int],
    lo: int,
    hi: int,
    layer: int,
) -> None:
    """Move edge indices ``lo..hi`` (inclusive) onto ``layer``: bracket
    the span with cuts at ``lo-1`` and ``hi`` (each a via on that
    edge), relabel every run covering the span, then re-bind the
    terminals and drop dead via cuts."""
    n_edges = len(b.edges)
    if lo > 0:
        _insort_cut(cuts, runs, lo - 1)
    if hi < n_edges - 1:
        _insort_cut(cuts, runs, hi)
    for p, (rlo, rhi) in enumerate(_run_bounds(cuts, n_edges)):
        if rlo <= hi and rhi >= lo:
            runs[p] = layer
    _fix_terminals(d, b, n_layers, cuts, runs)
    _merge_same(cuts, runs)


def _run_open(b: WirePath, open_e: np.ndarray | None, lo: int, hi: int) -> int:
    """Bitmask of layers open on EVERY edge in ``b.edges[lo..hi]`` —
    the corridor's per-edge open set (:func:`wires._open_layers`)
    intersected over the run. ``-1`` (all layers) when the mesh carries
    no corridor data — synthetic fixtures, where the constraint is
    vacuous."""
    if open_e is None:
        return -1
    m = -1
    for i in range(lo, hi + 1):
        m &= int(open_e[int(b.edges[i])])
    return m


def _runs_open(
    b: WirePath, open_e: np.ndarray | None, cuts: list[int], runs: list[int]
) -> bool:
    """The hard layer rule (gr520902 review): every run sits on a layer
    open on EVERY edge it covers — a run on a closed layer is stacked
    foreign copper, never a cost to price."""
    if open_e is None:
        return True
    return all(
        _run_open(b, open_e, lo, hi) >> runs[p] & 1
        for p, (lo, hi) in enumerate(_run_bounds(cuts, len(b.edges)))
    )


def _layers_segment(
    d: TopoDesign, wires: Wires, b: WirePath, n_layers: int
) -> tuple[list[int], list[int]]:
    """One branch's ``(runs, cuts)`` — the segment model's form of its
    committed layering. A search-layered branch (:attr:`WirePath.layers`,
    G2g) reports verbatim, legal by construction: a cut is the hop edge —
    run ``p`` covers ``(cuts[p-1] .. cuts[p]]``, so a layer change
    between edges ``i`` and ``i+1`` is a via on edge ``i``. A branch
    with no assigned layering (synthetic fixtures) takes the nominal:
    both ends forcing opposite outers (F↔B) splits mid-branch, else one
    run on the forced or lowest outer candidate."""
    n_edges = len(b.edges)
    if len(b.layers) == n_edges + 1:
        runs = [int(b.layers[0])]
        cuts: list[int] = []
        for i in range(n_edges):
            if int(b.layers[i]) != int(b.layers[i + 1]):
                cuts.append(i)
                runs.append(int(b.layers[i + 1]))
        return runs, cuts
    la = _pin_forced(d, b.pin_a, n_layers)
    lb = _pin_forced(d, b.pin_b, n_layers)
    if la is not None and lb is not None and la != lb and n_edges >= 2:
        return [la, lb], [n_edges // 2]
    if la is not None:
        layer = la
    elif lb is not None:
        layer = lb
    else:
        cand = set(wires.net_layers[b.net]) if b.net < len(wires.net_layers) else set()
        layer = min(cand & {0, n_layers - 1} or cand or {0})
    return [layer], []


def _retained_branch_key(
    d: TopoDesign,
    wires: Wires,
    b: WirePath,
    n_layers: int,
    *,
    resolved: tuple[Sequence[int], Sequence[int]] | None = None,
) -> WirePath:
    """Compare retained copper at the same resolved planning stage.

    Rethreading supplies the actual runs/cuts for both sides: recomputing an
    old branch's nominal layers loses corridor legalization and earlier moves.
    Without resolved state this only normalizes the nominal representation.
    Ordinary edge-crossing parameters are bookkeeping for the
    exact ``points``; rebuilding can normalize them without moving copper.
    An unplanted layer join still uses its crossing as a physical site, so
    retain that parameter. Keep corridor, endpoints, points and planted vias
    exact: a different descendant must not inherit an existing withdrawal.
    This is only a matching key; the retained identity itself stays intact,
    including an identity carried through an earlier flip.
    """
    runs, cuts = (
        _layers_segment(d, wires, b, n_layers) if resolved is None else resolved
    )
    unsited = set(cuts) - {v.i for v in b.vias}
    return dataclasses.replace(
        b,
        layers=_wires._edge_layers(len(b.edges), runs, cuts),
        t=tuple(t if i in unsited else 0.0 for i, t in enumerate(b.t)),
    )


def _geometric_regions(
    mesh: TopoMesh, wires: Wires, obstacles: np.ndarray
) -> dict[tuple[int, int], list[tuple[int, int, int, int]]]:
    """Crossing pairs and their bracketed regions
    (:func:`crossing_events`), less the empty bigons: two crossings of a
    pair that are consecutive along BOTH wires bound a region, and when
    no obstacle point (``obstacles``, ``(n, 2)`` world xy — see
    :func:`_obstacle_xy`) lies strictly inside it the wires can slide
    apart through it, so both crossings cancel. Repeats until stable
    (a removal can make others consecutive); an odd count keeps its
    last crossing. A homotopy test on the polylines, not an order
    comparison. ``{(a, b): [(ia1, ia2, ib1, ib2), ...]}`` with
    ``a < b``."""
    return resolve_crossings(mesh, wires.branches, obstacles)[0]


def _obstacle_xy(mesh: TopoMesh, d: TopoDesign, n_layers: int) -> np.ndarray:
    """World xy of the mesh's obstacle vertices
    (:func:`~precis.pcb.topoplace.wires.obstacle_vertices`) — what
    a bigon between two wires must hold to stay a crossing."""
    vids = sorted(obstacle_vertices(mesh, d, n_layers))
    return np.asarray(mesh.verts, dtype=float)[np.asarray(vids, dtype=int)].reshape(
        -1, 2
    )


def _pair_live(
    branches: Sequence[WirePath],
    runs: list[list[int]],
    cuts: list[list[int]],
    regions: list[tuple[int, int, int, int]],
    a: int,
    b: int,
) -> bool:
    """Whether crossing pair ``(a, b)`` is still live — any flipped
    shared-edge pair whose bracketed regions still share a layer.
    Conservative: over-reports partial splits, never misses a real
    crossing."""
    ea = len(branches[a].edges)
    eb = len(branches[b].edges)
    return any(
        _span_layers(cuts[a], runs[a], ea, ia1, ia2)
        & _span_layers(cuts[b], runs[b], eb, ib1, ib2)
        for ia1, ia2, ib1, ib2 in regions
    )


def _live_crossings(
    d: TopoDesign,
    mesh: TopoMesh,
    wires: Wires,
    n_layers: int,
    runs: list[list[int]],
    cuts: list[list[int]],
) -> tuple[tuple[int, int], ...]:
    """Crossing pairs still live under the layering the plan emits —
    the ``b.layers`` the search committed where they exist (G2g), the
    ``runs``/``cuts`` span elsewhere: a region is live when the two
    wires share a layer at its end arc (``_pair_live``'s rule)."""
    regs = _geometric_regions(mesh, wires, _obstacle_xy(mesh, d, n_layers))
    out: list[tuple[int, int]] = []
    for (a, b), rg in sorted(regs.items()):
        ba, bb = wires.branches[a], wires.branches[b]
        if ba.net == bb.net:
            continue
        ea, eb = len(ba.edges), len(bb.edges)
        for ia1, ia2, ib1, ib2 in rg:
            la = (
                {int(ba.layers[min(ia2, len(ba.layers) - 1)])}
                if len(ba.layers) == ea + 1
                else _span_layers(cuts[a], runs[a], ea, ia1, ia2)
            )
            lb = (
                {int(bb.layers[min(ib2, len(bb.layers) - 1)])}
                if len(bb.layers) == eb + 1
                else _span_layers(cuts[b], runs[b], eb, ib1, ib2)
            )
            if la & lb:
                out.append((a, b))
                break
    return tuple(out)


def _yield_rank(d: TopoDesign, b: Any, n_layers: int) -> tuple[int, int]:
    """Decision 6's yielder order: a branch with a TH end (free — 0
    vias), then an F↔B SMD pair (moves its via — 1), then SMD-only
    (2 vias). Lower rank yields."""
    sides = (int(d.pin_side[b.pin_a]), int(d.pin_side[b.pin_b]))
    if 2 in sides:
        return (0, b.pin_a + b.pin_b)
    if sides[0] != sides[1]:
        return (1, b.pin_a + b.pin_b)
    return (2, b.pin_a + b.pin_b)


#: Live pairs one search round offers moves for — a synthetic crossing
#: storm (the n40 fixture) carries thousands of pair regions, and a
#: strict-improvement sweep over all of them costs more than the route.
#: The residue still resolves: an accepted move re-offers next round.
_LIVE_PAIR_CAP = 64


def _accepted_overflow(
    cut2: tuple[CorridorCut, ...], cur: tuple[CorridorCut, ...]
) -> tuple[CorridorCut, ...] | None:
    """The overflow an accepted rebuild/re-thread carries — ``None``
    when the move must be refused because ``cut2`` grows the ROUTED
    cut record. ``corridor.cut`` on candidate branches is routed-only
    while ``cur`` may also hold refused records (an unrouted net's
    recorded demand — report data, not committed demand): comparing
    lengths whole would let a move add that many routed over-capacity
    cuts and pass, and assigning ``cut2`` verbatim would drop the
    refused record outright (G2h review-3 B1)."""
    if len(cut2) > sum(1 for c in cur if not c.refused):
        return None
    return tuple(cut2) + tuple(c for c in cur if c.refused)


def _hv_pen_matrix(
    mesh: TopoMesh, br: WirePath, n_layers: int, weight: float
) -> list[list[float]]:
    """Per (edge, layer) H/V direction cost of ``br``'s own crossings
    (pcb-topo-hv-preference) — the ``pen`` ``layer_dp`` folds the
    preference through. Edge ``i``'s cell prices the travel from its
    OWN crossing point back to the previous one (the pin start for
    ``i == 0``), the same "run arriving at edge i" the step-cost search
    prices (:func:`~precis.pcb.topoplace.wires._direction_pen`)."""
    pts = [br.points[0]] + [
        edge_xy(mesh, int(br.edges[i]), float(br.t[i])) for i in range(len(br.edges))
    ]
    return [
        [
            _wires._direction_pen(pts[i], pts[i + 1], ly, n_layers, weight)
            for ly in range(n_layers)
        ]
        for i in range(len(br.edges))
    ]


def assign_layers(
    d: TopoDesign,
    mesh: TopoMesh,
    wires: Wires,
    n_layers: int,
    *,
    net_rules: Mapping[str, NetRules] | None = None,
    pose: Any = None,
    event: Callable[[str], None] | None = None,
    masks: BoardMasks | None = None,
    hv_weight: float = 0.0,
) -> tuple[Wires, list[list[int]], list[list[int]], tuple[tuple[int, int], ...]]:
    """Pair-level crossing resolution on planted vias (G2g call site 2).

    The start is the layering the search committed (``b.layers`` — legal
    by construction): per branch a runs/cuts model where a cut at edge
    index ``c`` mints a via on ``edges[c]``. Each round every live pair
    offers its moves: a whole-branch flip of EITHER partner to the other
    outer (a through-hole branch moves free — no cut, no via), each
    partner's bracketed span to each of its net's candidate layers, and
    — only once every flip stalls, and only on a real mesh — a re-thread
    of either side's net with the pair's shared corridor masked, so the
    wire regrows around the wrapped pad's other side.

    A move is legal when every run sits on a layer its edges leave open
    (:func:`_runs_open`) and every cut holds a concrete via point — the
    branch's own planted via where one stands on that edge, else the
    :class:`~precis.pcb.topoplace.wires._ViaPlanter`'s point judged
    against a :class:`CopperIndex` of the other branches' bodies and
    vias (rebuilt per accepted move). An accepted move's payload is the
    branch REBUILT through its hop points
    (:func:`~precis.pcb.topoplace.wires._build_branch` — the via is the
    run funnel's endpoint, so the piece machinery never re-sites it),
    the pair regions re-evaluated on the new taut wire. The cheapest
    move by ``(live pairs, overflow, vias, length, mask)`` wins; a round
    with no strictly improving move ends the search. Returns ``(wires,
    runs, cuts, remaining pairs)`` — the wires carry rebuilt branches
    and a rebuilt ``order``.

    ``event`` receives one progress line per search round — ``None``
    (the default) keeps the search silent. ``masks`` (pcb-layer-argmin)
    is the corridor mask set at the routed pose — move keys' fifth
    element, 0.0 everywhere without it. ``hv_weight``
    (pcb-topo-hv-preference, default 0.0/off) folds each branch's own
    In1-horizontal/In2-vertical direction cost into the ``pen`` matrix
    ``layer_dp`` reads, at both the unlayered-branch legalisation and
    the pair-move DP."""
    branches = list(wires.branches)
    cur_wires = dataclasses.replace(
        wires,
        branch_lineage=wires.branch_lineage
        or tuple(frozenset((b,)) for b in wires.branches),
        branch_ids=wires.branch_ids or wires.branches,
    )
    # Crossings are the taut polylines' own intersections (G1) — the
    # order lists name neither a crossing nor its absence.
    obstacles = _obstacle_xy(mesh, d, n_layers)
    pair_regs = _geometric_regions(mesh, cur_wires, obstacles)
    rules = net_rules or {}
    outers = frozenset((0, n_layers - 1))
    n_e = len(mesh.edges)
    tried_thread: set[tuple[int, frozenset[int]]] = set()
    # On a real mesh a run may only sit on a layer every one of its edges
    # leaves open, and a cut needs a point the via planter qualifies
    # against the other branches' copper. Synthetic fixtures carry no
    # corridor data: both rules are vacuous there (``open_e=None``, the
    # site is the wire's own edge crossing).
    ctx: Any = None
    corridor: Any = None
    planter: Any = None
    open_layers: dict[int, np.ndarray] = {}
    pin_vids: frozenset[int] = frozenset()
    pose_a = np.asarray(d.pose0 if pose is None else pose)
    if len(mesh.tris) and len(mesh.pin_vertex) == len(d.pin_xy):
        ctx = _wires._make_ctx(mesh, d, n_layers)
        # The corridor ledger the wires were priced and cut under —
        # rebuilt here so a re-thread regrowth pays the SAME occupancy
        # and faces the SAME demand-vs-capacity cut (gr520902 review).
        rules_used = [
            (
                float(rules[d.net_name[k]].track_width_mm),
                float(rules[d.net_name[k]].clearance_mm),
            )
            if k < len(d.net_name) and d.net_name[k] in rules
            else (_DEFAULT.track_width_mm, _DEFAULT.clearance_mm)
            for k in range(len(d.net_pins))
        ]
        corridor = _wires._corridor(ctx, mesh, d, n_layers, rules_used, pose_a)
        open_layers = _corridor_masks(d, mesh, wires, n_layers, rules, ctx)
        planter = _wires._ViaPlanter(
            ctx,
            d,
            pose_a,
            net_clearance=tuple(c for _w, c in rules_used),
            net_clearance_by_name={
                name: float(nr.clearance_mm) for name, nr in rules.items()
            },
        )
        pin_vids = frozenset(int(v) for v in mesh.pin_vertex)
    runs: list[list[int]] = []
    cuts: list[list[int]] = []

    # ── Corridor mask cost (pcb-layer-argmin), the fifth lexicographic
    # key element: a branch's runs scored against the routed pose's mask
    # set.  0.0 everywhere when the caller passes no masks — the search
    # then behaves exactly as the four-element key always has.
    rb_unions: list[Any] = []
    if masks is not None:
        from shapely.geometry import Polygon as _RbPoly
        from shapely.ops import unary_union as _rb_union

        rb_unions = [
            _rb_union([_RbPoly(r) for r in lm.route_block]) if lm.route_block else None
            for lm in masks.layers
        ]

    def _branch_mask_cost(br: WirePath, r_b: list[int], c_b: list[int]) -> float:
        if masks is None:
            return 0.0
        nb = len(br.edges)
        if nb == 0 or not r_b:
            return 0.0
        k = int(br.net)
        name = str(d.net_name[k]) if k < len(d.net_name) else str(k)
        width = float(rules.get(name, _DEFAULT).track_width_mm)
        bpts = [(float(x), float(y)) for x, y in br.points]
        marks = sorted(
            (
                *_polyline_arc(
                    bpts,
                    edge_xy(
                        mesh,
                        int(br.edges[min(int(ei), nb - 1)]),
                        float(br.t[int(ei)]) if int(ei) < len(br.t) else 0.5,
                    ),
                ),
                None,
            )
            for ei in c_b
        )
        attach: list[tuple[float, tuple[float, float], Any]] = [(0.0, bpts[0], None)]
        attach += [(s, p, None) for s, p, _via in marks]
        attach.append((float(len(bpts) - 1), bpts[-1], None))
        acc = 0.0
        tot = 0.0
        cross = 0
        for pi, (a_, b_) in enumerate(itertools.pairwise(attach)):
            if pi >= len(r_b):
                break
            run = _piece_run(bpts, a_, b_)
            if len(run) < 2:
                continue
            ly = int(r_b[pi])
            if ly < 0 or ly >= len(masks.layers):
                continue
            ln = sum(math.dist(p, q) for p, q in itertools.pairwise(run))
            if ln <= 0:
                continue
            blocked, c_ = corridor_score(
                masks.layers[ly], run, width, rb_union=rb_unions[ly]
            )
            acc += ln * blocked
            tot += ln
            cross += c_
        return (acc / tot if tot else 0.0) + _MASK_CROSS_K * cross

    def _bb_mask_cost(bb: int, r_b: list[int], c_b: list[int]) -> float:
        return _branch_mask_cost(branches[bb], r_b, c_b)

    def _state_mask_cost(
        brs: Sequence[WirePath], rs: Sequence[list[int]], cs: Sequence[list[int]]
    ) -> float:
        return sum(_branch_mask_cost(b_, rs[i], cs[i]) for i, b_ in enumerate(brs))

    def live_pairs(
        brs: Sequence[WirePath],
        rs: list[list[int]],
        cs: list[list[int]],
        regs: dict[tuple[int, int], list[tuple[int, int, int, int]]],
    ) -> set[tuple[int, int]]:
        return {
            (a, b)
            for (a, b), rg in regs.items()
            if brs[a].net != brs[b].net and _pair_live(brs, rs, cs, rg, a, b)
        }

    def n_vias(cs: list[list[int]]) -> int:
        return sum(len(c) for c in cs)

    def _routed(ovf: Iterable[CorridorCut]) -> tuple[CorridorCut, ...]:
        """The ROUTED records of an overflow tuple — a refused cut is
        an unrouted net's recorded demand, not committed demand, so the
        state key counts routed records alone."""
        return tuple(c for c in ovf if not c.refused)

    def cand_layers(k: int) -> Iterable[int]:
        c = cur_wires.net_layers[k] if k < len(cur_wires.net_layers) else frozenset()
        return sorted(c) if c else sorted(outers)

    def _rule_of(k: int) -> NetRules:
        name = str(d.net_name[k]) if k < len(d.net_name) else str(k)
        return rules.get(name, _DEFAULT)

    geom_of: dict[int, Any] = {}
    dyn_of: dict[tuple[int, bool], CopperIndex] = {}

    def _geom(bi: int) -> Any:
        g = geom_of.get(bi)
        if g is None:
            g = geom_of[bi] = branch_geom(mesh, branches[bi])
        return g

    def _dyn(bb: int, *, whole_net: bool = False) -> CopperIndex:
        """The bodies a move on branch ``bb``'s cuts must clear: every
        OTHER branch's wire (net-tagged — the pair's own net exempts
        itself, the same way the search's ``dyn`` did) plus every other
        branch's planted vias. ``bb``'s own copper is being replaced, so
        it is not in the index at all. Rebuilt per accepted move — the
        cache dies with the branch list it was read from. A rethread
        replaces the whole net, so it excludes every sibling too."""
        key = (bb, whole_net)
        ix = dyn_of.get(key)
        if ix is None:
            others = [
                ob
                for j, ob in enumerate(branches)
                if j != bb and (not whole_net or ob.net != branches[bb].net)
            ]
            bodies: list[Body] = []
            for ob in others:
                if len(ob.points) < 2:
                    continue
                nr = _rule_of(int(ob.net))
                bodies.append(
                    Body(
                        LineString(list(dict.fromkeys(ob.points))),
                        r=float(nr.track_width_mm) / 2.0,
                        net=int(ob.net),
                        clearance=float(nr.clearance_mm),
                    )
                )
            ix = CopperIndex(bodies)
            for ob in others:
                for v in ob.vias:
                    ix.add_disc(
                        v.x,
                        v.y,
                        v.r_site_mm,
                        net=ANY_NET,
                        clearance=float(_rule_of(int(ob.net)).clearance_mm),
                    )
            dyn_of[key] = ix
        return ix

    def _cut_points(bb: int, c2: list[int]) -> list[tuple[float, float, float]] | None:
        """A via point ``(x, y, t)`` per proposed cut of branch ``bb`` —
        the branch's own planted via where one already stands on that
        edge, else the planter's point against the other branches'
        copper; ``None`` when one cut has no plantable site, which makes
        the move illegal. On a synthetic fixture the site is the wire's
        own crossing of the cut edge."""
        b = branches[bb]
        k = int(b.net)
        nr = _rule_of(k)
        r_site = float(nr.via_dia_mm or _VIA_DIA_MM) / 2.0
        cl = float(nr.clearance_mm)
        by_i = {int(v.i): v for v in b.vias}
        dyn: CopperIndex | None = None
        out: list[tuple[float, float, float]] = []
        for c in c2:
            v = by_i.get(c)
            if v is not None:
                t = (
                    _wires._edge_param_at(ctx, int(b.edges[c]), (v.x, v.y))
                    if ctx is not None
                    else float(b.t[c])
                )
                out.append((v.x, v.y, t))
                continue
            if ctx is None:
                e = int(b.edges[c])
                t = float(b.t[c])
                x, y = edge_xy(mesh, e, t)
                out.append((x, y, t))
                continue
            if dyn is None:
                dyn = _dyn(bb)
            pt = planter.point(
                int(b.edges[c]),
                r_site,
                cl,
                k,
                dyn,
                drill_mm=float(nr.via_drill_mm or _VIA_DRILL_MM),
            )
            if pt is None:
                return None
            out.append(pt)
        return out

    def _fit_mask(bb: int) -> list[bool]:
        """Per-edge cut legality for the DP — the branch's own planted
        via already standing on the edge counts, else the planter's
        point against the other branches' copper (same judge as
        :func:`_cut_points`, one boolean per edge)."""
        b = branches[bb]
        k = int(b.net)
        nr = _rule_of(k)
        r_site = float(nr.via_dia_mm or _VIA_DIA_MM) / 2.0
        cl = float(nr.clearance_mm)
        have = {int(v.i) for v in b.vias}
        dyn: CopperIndex | None = None
        out: list[bool] = []
        for i, e in enumerate(b.edges):
            if i in have:
                out.append(True)
                continue
            if dyn is None:
                dyn = _dyn(bb)
            out.append(
                planter.point(
                    int(e),
                    r_site,
                    cl,
                    k,
                    dyn,
                    drill_mm=float(nr.via_drill_mm or _VIA_DRILL_MM),
                )
                is not None
            )
        return out

    def _bb_regs(
        nb: WirePath, bb: int
    ) -> dict[tuple[int, int], list[tuple[int, int, int, int]]]:
        """The pair regions of a rebuilt branch against every other —
        the moving wire's taut points change, so its crossings
        re-derive; pairs not touching ``bb`` keep ``pair_regs``."""
        others = [j for j in range(len(branches)) if j != bb]
        bl = [nb] + [branches[j] for j in others]
        geoms = [branch_geom(mesh, nb)] + [_geom(j) for j in others]
        ev = crossing_events(mesh, bl, geoms, first_only=True)
        regs, _gone = resolve_crossings(mesh, bl, obstacles, ev)
        out: dict[tuple[int, int], list[tuple[int, int, int, int]]] = {}
        for (_a, m), rg in regs.items():
            j = others[m - 1]
            if bb < j:
                out[(bb, j)] = rg
            else:
                # the key is (j, bb) but ``rg`` brackets nb first — swap
                # each region's halves so _pair_live reads a's span on a
                out[(j, bb)] = [(ib1, ib2, ia1, ia2) for ia1, ia2, ib1, ib2 in rg]
        return out

    def eval_move(
        bb: int, r2: list[int], c2: list[int]
    ) -> tuple[tuple[int, int, int, float, float], tuple[Any, ...]] | None:
        """One flip/span move's ``(live pairs, overflow, vias, length,
        mask)`` key and splice payload — ``None`` when illegal: a run on
        a layer an edge closes (:func:`_runs_open`), a cut with no
        plantable via point, or two proposed hops landing drill-close.
        The payload is the REBUILT branch: a relabel that touches no
        hop site only swaps ``layers``; a move whose cut set or points
        change is re-funnelled through its via points by
        :func:`wires._build_branch`, its crossings re-derived and its
        corridor re-cut — a rebuild that grows the ROUTED
        demand-vs-capacity cut is refused outright."""
        b = branches[bb]
        k = int(b.net)
        if not _runs_open(b, open_layers.get(k), c2, r2):
            return None
        pts = _cut_points(bb, c2)
        if pts is None:
            return None
        nr = _rule_of(k)
        vd = float(nr.via_dia_mm or _VIA_DIA_MM)
        cl = float(nr.clearance_mm)
        for i, (x, y, _t) in enumerate(pts):
            for x0, y0, _t0 in pts[:i]:
                if math.dist((x, y), (x0, y0)) < vd + cl + FAB_QUANTUM_MM:
                    return None
        new_lays = tuple(_wires._edge_layers(len(b.edges), r2, c2))
        kept = {int(v.i): (v.x, v.y) for v in b.vias}
        same_sites = len(kept) == len(c2) and all(
            kept.get(c) == (x, y) for c, (x, y, _t) in zip(c2, pts)
        )
        regs2 = pair_regs
        ovf = len(_routed(cur_wires.overflow))
        if same_sites or ctx is None:
            nb = dataclasses.replace(
                b,
                layers=new_lays,
                vias=b.vias
                if same_sites
                else tuple(v for v in b.vias if int(v.i) in set(c2)),
            )
            cw = cur_wires
        else:
            hops = [(c, x, y, t) for c, (x, y, t) in zip(c2, pts)]
            nb, _re = _wires._build_branch(
                ctx,
                mesh,
                k,
                b.pin_a,
                b.pin_b,
                list(b.tris),
                list(b.edges),
                open_layers[k],
                pin_vids,
                new_lays,
                hops,
                vd,
                float(getattr(nr, "via_drill_mm", 0.0) or 0.0),
            )
            cbr2 = list(branches)
            cbr2[bb] = nb
            regs2 = {p: rg for p, rg in pair_regs.items() if bb not in p}
            regs2.update(_bb_regs(nb, bb))
            order2, _pos2, res2 = _wires._order(ctx, mesh, tuple(cbr2))
            cut2 = corridor.cut(cbr2, order2)
            merged = _accepted_overflow(cut2, cur_wires.overflow)
            if merged is None:
                return None
            ovf = len(cut2)
            cw = dataclasses.replace(
                cur_wires,
                branches=tuple(cbr2),
                order=order2,
                order_residual=res2,
                overflow=merged,
            )
        cbr = list(branches)
        cbr[bb] = nb
        r2n, c2n = _layers_segment(d, cur_wires, nb, n_layers)
        rs2, cs2 = list(runs), list(cuts)
        rs2[bb], cs2[bb] = r2n, c2n
        key = (
            len(live_pairs(cbr, rs2, cs2, regs2)),
            ovf,
            n_vias(cs2),
            sum(x.length_mm for x in cbr),
            mask_tot
            - _bb_mask_cost(bb, runs[bb], cuts[bb])
            + _branch_mask_cost(nb, r2n, c2n),
        )
        return key, (bb, nb, r2n, c2n, regs2, cw)

    def rethread(
        bb: int, shared: frozenset[int]
    ) -> tuple[tuple[int, int, int, float, float], tuple[Any, ...]] | None:
        """Net ``bb``'s tree regrown with the pair's shared corridor
        masked — ``None`` when the regrow strands a component, yields
        no branch, or its demand-vs-capacity cut grows the board's
        ROUTED overflow record (gr520902 review: the regrowth pays
        build_wires' own occupancy and faces the same cut)."""
        k = int(branches[bb].net)
        name = str(d.net_name[k]) if k < len(d.net_name) else str(k)
        nr = rules.get(name, _DEFAULT)
        mask = np.zeros(n_e, dtype=bool)
        mask[sorted(shared)] = True
        occ = corridor.occ(corridor.used_of(x for x in branches if x.net != k))
        found, comps, masks_k = _wires._net_tree(
            ctx,
            mesh,
            d,
            k,
            n_layers,
            float(nr.track_width_mm),
            float(nr.clearance_mm),
            occ,
            extra_blocked=mask,
            via_dia_mm=float(nr.via_dia_mm or _VIA_DIA_MM),
            via_drill_mm=float(nr.via_drill_mm or _VIA_DRILL_MM),
            planter=planter,
            occ_view=_wires._OccView(
                closed=np.zeros(n_e, dtype=np.int64),
                occupied=np.zeros((n_e, n_layers), dtype=np.int32),
                dyn=_dyn(bb, whole_net=True),
            ),
        )
        if not found or len(comps) > 1:
            return None
        pin_vids = frozenset(int(v) for v in mesh.pin_vertex)
        newb = [
            _wires._build_branch(
                ctx,
                mesh,
                k,
                pa,
                pb,
                t2,
                e2,
                masks_k.open,
                pin_vids,
                lays,
                hops,
                float(nr.via_dia_mm or _VIA_DIA_MM),
                float(getattr(nr, "via_drill_mm", 0.0) or 0.0),
                bounds=bounds,
            )[0]
            for pa, pb, t2, e2, lays, hops, bounds in found
        ]
        idx = [i for i, x in enumerate(branches) if x.net == k]
        lo_i, hi_i = idx[0], idx[-1] + 1
        # the splice below assumes a net's branches sit contiguously —
        # a rethread interleaves them only if a caller broke that
        assert idx == list(range(lo_i, hi_i))
        cbr = branches[:lo_i] + newb + branches[hi_i:]
        order2, _pos2, residual2 = _wires._order(ctx, mesh, tuple(cbr))
        cut2 = corridor.cut(cbr, order2)
        merged = _accepted_overflow(cut2, cur_wires.overflow)
        if merged is None:
            return None
        rs2 = list(runs[:lo_i])
        cs2 = list(cuts[:lo_i])
        for nb_ in newb:
            r0, c0 = _layers_segment(d, cur_wires, nb_, n_layers)
            rs2.append(r0)
            cs2.append(c0)
        rs2 += runs[hi_i:]
        cs2 += cuts[hi_i:]
        # Compare the states this candidate actually evaluates/emits. The old
        # runs can differ from its input annotation after corridor legalization.
        retained = {
            _retained_branch_key(d, cur_wires, b, n_layers, resolved=(r, c)): identity
            for b, identity, r, c in zip(
                branches, cur_wires.branch_ids, runs, cuts, strict=True
            )
        }
        cw = dataclasses.replace(
            cur_wires,
            branches=tuple(cbr),
            branch_lineage=(
                cur_wires.branch_lineage[:lo_i]
                + (frozenset().union(*cur_wires.branch_lineage[lo_i:hi_i]),) * len(newb)
                + cur_wires.branch_lineage[hi_i:]
            ),
            branch_ids=(
                cur_wires.branch_ids[:lo_i]
                + tuple(
                    retained.get(
                        _retained_branch_key(
                            d, cur_wires, b, n_layers, resolved=(r, c)
                        ),
                        b,
                    )
                    for b, r, c in zip(
                        newb,
                        rs2[lo_i : lo_i + len(newb)],
                        cs2[lo_i : lo_i + len(newb)],
                        strict=True,
                    )
                )
                + cur_wires.branch_ids[hi_i:]
            ),
            order=order2,
            order_residual=residual2,
            overflow=merged,
        )
        regs2 = _geometric_regions(mesh, cw, obstacles)
        key = (
            len(live_pairs(cbr, rs2, cs2, regs2)),
            len(cut2),
            n_vias(cs2),
            sum(x.length_mm for x in cbr),
            _state_mask_cost(cbr, rs2, cs2),
        )
        return key, (cbr, rs2, cs2, cw, regs2)

    def state_key() -> tuple[int, int, int, float, float]:
        return (
            len(cur_live),
            len(_routed(cur_wires.overflow)),
            n_vias(cuts),
            sum(x.length_mm for x in branches),
            mask_tot,
        )

    for bi, br0 in enumerate(wires.branches):
        r0, c0 = _layers_segment(d, wires, br0, n_layers)
        k = int(br0.net)
        open_e = open_layers.get(k)
        if (
            ctx is not None
            and open_e is not None
            and len(br0.layers) != len(br0.edges) + 1
            and br0.edges
            and not _runs_open(br0, open_e, c0, r0)
        ):
            # An unlayered branch on a real mesh (test-built or legacy
            # callers) has no committed layering to read back — the
            # nominal can sit a run on a layer an edge closes. Keep one
            # run on a candidate layer every edge leaves open when that
            # exists; else legalise through the same DP the resolver
            # uses, its cut legality judged by the planter against the
            # other branches' copper.
            cand = set(cand_layers(k))
            one = _run_open(br0, open_e, 0, len(br0.edges) - 1) & sum(
                1 << ly for ly in cand
            )
            if one:
                r0, c0 = [int((one & -one).bit_length() - 1)], []
            else:
                nr0 = _rule_of(k)
                fit0 = [
                    planter.point(
                        int(e),
                        float(nr0.via_dia_mm or _VIA_DIA_MM) / 2.0,
                        float(nr0.clearance_mm),
                        k,
                        _dyn(bi),
                        drill_mm=float(nr0.via_drill_mm or _VIA_DRILL_MM),
                    )
                    is not None
                    for e in br0.edges
                ]
                cmask = sum(1 << ly for ly in cand)
                r1, c1, dead = _wires.layer_dp(
                    [int(open_e[int(e)]) for e in br0.edges],
                    fit0,
                    cmask,
                    cmask,
                    n_layers,
                    _hv_pen_matrix(mesh, br0, n_layers, hv_weight)
                    if hv_weight
                    else None,
                )
                if dead is None:
                    r0, c0 = r1, c1
        runs.append(r0)
        cuts.append(c0)
    mask_tot = _state_mask_cost(branches, runs, cuts)
    cur_live = live_pairs(branches, runs, cuts, pair_regs)
    best_key = state_key()
    best = (
        cur_wires,
        [list(r) for r in runs],
        [list(c) for c in cuts],
        pair_regs,
    )
    pair_off = 0  # rotating window into the sorted live pairs
    stalled_windows = 0  # consecutive windows served without a move
    for _round in range(_MAX_FLIPS):
        if not cur_live:
            break
        if event is not None:
            event(
                f"route: assign round {_round + 1} — "
                f"{len(cur_live)} live pair(s), {n_vias(cuts)} via site(s)"
            )
        cur_key = state_key()
        chosen_key: tuple[int, int, int, float, float] | None = None
        chosen: tuple[str, Any] | None = None
        # a cap keeps a crossing storm's round bounded, but the same
        # first-64 pairs must not own every round: the window rotates
        # on, so a pair past the cap is offered its flips on a later
        # round; a full cycle with no move is the stall that breaks.
        pairs_sorted = sorted(cur_live)
        if pair_off >= len(pairs_sorted):
            pair_off = 0
        window = pairs_sorted[pair_off : pair_off + _LIVE_PAIR_CAP]
        for a, b in window:
            regs = pair_regs[(a, b)]
            # whole-branch flips to the other outer — the move that
            # frees a TH branch's crossing for 0 vias
            for bb in (a, b):
                nb = len(branches[bb].edges)
                if not nb:
                    continue
                on = _span_layers(cuts[bb], runs[bb], nb, -1, nb - 1)
                target = n_layers - 1 if n_layers - 1 not in on else 0
                c2, r2 = list(cuts[bb]), list(runs[bb])
                _set_span(d, branches[bb], n_layers, c2, r2, 0, nb - 1, target)
                got = eval_move(bb, r2, c2)
                if (
                    got is not None
                    and got[0] < cur_key
                    and (chosen_key is None or got[0] < chosen_key)
                ):
                    chosen_key, chosen = got[0], ("flip", got[1])
            # EITHER partner's bracketed span to each candidate layer —
            # the union of the regions this pair marks on its own
            # edge-index space. The yielder goes first (Decision 6): the
            # cheapest key wins anyway, so the non-yielder moves exactly
            # when the yielder has no plantable cut.
            for bb in sorted(
                (a, b), key=lambda x: _yield_rank(d, branches[x], n_layers)
            ):
                k = int(branches[bb].net)
                j = 0 if bb == a else 2
                lo = min(r_[j] for r_ in regs) + 1
                hi = max(r_[j + 1] for r_ in regs)
                for ly in cand_layers(k):
                    c2, r2 = list(cuts[bb]), list(runs[bb])
                    _set_span(d, branches[bb], n_layers, c2, r2, lo, hi, ly)
                    got = eval_move(bb, r2, c2)
                    if (
                        got is not None
                        and got[0] < cur_key
                        and (chosen_key is None or got[0] < chosen_key)
                    ):
                        chosen_key, chosen = got[0], ("flip", got[1])
            # EITHER partner's whole-corridor excursion: the bracketed
            # span's cuts must land on the region's own bracket edges —
            # the DP's land wherever a via plants. Each partner's
            # corridor is re-layered by layer_dp with the other's
            # bracket layers priced over its own, so a resolving
            # excursion (leave + return) is what the DP returns first.
            # The rebuilt wire's taut detours around its new vias, so a
            # move can stay live on a shifted bracket — the DP then
            # re-pens the still-live regions and widens the excursion,
            # a bounded fixpoint (_relayer_branch's loop, pair level).
            if ctx is not None:
                for bb in sorted(
                    (a, b), key=lambda x: _yield_rank(d, branches[x], n_layers)
                ):
                    bp = branches[bb]
                    other = b if bb == a else a
                    bo = branches[other]
                    ne_b = len(bp.edges)
                    k = int(bp.net)
                    open_k = open_layers.get(k)
                    if not ne_b or open_k is None:
                        continue
                    j, jo = (0, 2) if bb == a else (2, 0)
                    pen = [[0.0] * n_layers for _ in bp.edges]
                    if hv_weight:
                        hv_pen = _hv_pen_matrix(mesh, bp, n_layers, hv_weight)
                        for i_hv in range(ne_b):
                            for ly_hv in range(n_layers):
                                pen[i_hv][ly_hv] += hv_pen[i_hv][ly_hv]

                    def _add_pen(
                        rg_: tuple[int, int, int, int],
                        margin: int = 0,
                        *,
                        pen_: list[list[float]] = pen,
                        cuts_: list[list[int]] = cuts,
                        runs_: list[list[int]] = runs,
                        other_: int = other,
                        bo_: WirePath = bo,
                        j_: int = j,
                        jo_: int = jo,
                        ne_: int = ne_b,
                    ) -> None:
                        o_lays = _span_layers(
                            cuts_[other_],
                            runs_[other_],
                            len(bo_.edges),
                            rg_[jo_],
                            rg_[jo_ + 1],
                        )
                        lo_ = max(0, rg_[j_] + 1 - margin)
                        hi_ = min(rg_[j_ + 1] + 1 + margin, ne_)
                        for i_ in range(lo_, hi_):
                            for ly_ in o_lays:
                                pen_[i_][ly_] += 1.0

                    for r_ in regs:
                        _add_pen(r_)
                    cmask_d = _wires._cand_mask(
                        _wires._net_layers(d, k, n_layers), n_layers
                    )
                    fit_p: list[bool] | None = None
                    for _try in range(10):
                        if fit_p is None:
                            fit_p = _fit_mask(bb)
                        r2, c2, dead = _wires.layer_dp(
                            [int(open_k[int(e)]) for e in bp.edges],
                            fit_p,
                            _wires._pin_layers(d, bp.pin_a, n_layers, cmask_d),
                            _wires._pin_layers(d, bp.pin_b, n_layers, cmask_d),
                            n_layers,
                            pen,
                        )
                        if dead is not None or (r2, c2) == (runs[bb], cuts[bb]):
                            break
                        got = eval_move(bb, r2, c2)
                        if got is None:
                            break
                        _nb2, _r2n, _c2n, _regs2 = (
                            got[1][1],
                            got[1][2],
                            got[1][3],
                            got[1][4],
                        )
                        _cbr = list(branches)
                        _cbr[bb] = _nb2
                        _rs2, _cs2 = list(runs), list(cuts)
                        _rs2[bb], _cs2[bb] = _r2n, _c2n
                        rg2 = _regs2.get((a, b), [])
                        still = [
                            rg_
                            for rg_ in rg2
                            if _span_layers(
                                _cs2[bb], _rs2[bb], ne_b, rg_[j], rg_[j + 1]
                            )
                            & _span_layers(
                                _cs2[other],
                                _rs2[other],
                                len(bo.edges),
                                rg_[jo],
                                rg_[jo + 1],
                            )
                        ]
                        if (
                            not still
                            and got[0] < cur_key
                            and (chosen_key is None or got[0] < chosen_key)
                        ):
                            chosen_key, chosen = got[0], ("flip", got[1])
                        if not still:
                            break
                        for rg_ in still:
                            _add_pen(rg_, margin=2 * _try + 1)
        if chosen is None and ctx is not None:
            # flips stalled — regrow either side's tree around the
            # masked shared corridor (ctx is None only on synthetic
            # fixtures, which carry no tris/pin_vertex and never reach
            # here)
            for a, b in sorted(cur_live):
                shared: set[int] = set()
                # a crossing needs no shared edge, so the masked corridor
                # is the edges both wires use around it
                for ia1, ia2, ib1, ib2 in pair_regs[(a, b)]:
                    shared |= {int(e) for e in branches[a].edges[max(ia1, 0) : ia2 + 1]}
                    shared |= {int(e) for e in branches[b].edges[max(ib1, 0) : ib2 + 1]}
                if not shared:
                    continue
                for bb in (a, b):
                    mark = (int(branches[bb].net), frozenset(shared))
                    if mark in tried_thread:
                        continue
                    tried_thread.add(mark)
                    got = rethread(bb, frozenset(shared))
                    if (
                        got is not None
                        and got[0] < cur_key
                        and (chosen_key is None or got[0] < chosen_key)
                    ):
                        chosen_key, chosen = got[0], ("thread", got[1])
        if chosen is None:
            stalled_windows += 1
            if stalled_windows < -(-len(pairs_sorted) // _LIVE_PAIR_CAP):
                pair_off = (pair_off + _LIVE_PAIR_CAP) % len(pairs_sorted)
                continue
            break
        pair_off = 0
        stalled_windows = 0
        kind, payload = chosen
        if kind == "flip":
            bb, nb, r2n, c2n, regs2, cw = payload
            mask_tot += _branch_mask_cost(nb, r2n, c2n) - _bb_mask_cost(
                bb, runs[bb], cuts[bb]
            )
            branches[bb] = nb
            runs[bb], cuts[bb] = r2n, c2n
            pair_regs = regs2
            cur_wires = dataclasses.replace(cw, branches=tuple(branches))
            geom_of.clear()
            dyn_of.clear()
        else:
            branches, runs, cuts, cur_wires, pair_regs = payload
            mask_tot = _state_mask_cost(branches, runs, cuts)
            geom_of.clear()
            dyn_of.clear()
        cur_live = live_pairs(branches, runs, cuts, pair_regs)
        key = state_key()
        if key < best_key:
            best_key = key
            best = (
                cur_wires,
                [list(r) for r in runs],
                [list(c) for c in cuts],
                pair_regs,
            )
    w_fin, r_fin, c_fin, regs_fin = best
    return (
        w_fin,
        r_fin,
        c_fin,
        tuple(sorted(live_pairs(w_fin.branches, r_fin, c_fin, regs_fin))),
    )


def _polyline_arc(
    pts: Sequence[tuple[float, float]], xy: tuple[float, float]
) -> tuple[float, tuple[float, float]]:
    """``xy``'s closest point on the polyline, as ``(s, point)`` — ``s``
    is the arc coordinate ``i + f`` for fraction ``f`` of segment ``i``
    (vertex ``j`` sits at ``s = j``). A tied nearest distance takes the
    earliest arc position."""
    best = (math.inf, 0.0, (float(pts[0][0]), float(pts[0][1])))
    for i, (p, q) in enumerate(itertools.pairwise(pts)):
        dx, dy = q[0] - p[0], q[1] - p[1]
        d2 = dx * dx + dy * dy
        f = (
            0.0
            if d2 == 0
            else min(1.0, max(0.0, ((xy[0] - p[0]) * dx + (xy[1] - p[1]) * dy) / d2))
        )
        cand = (p[0] + f * dx, p[1] + f * dy)
        dist = math.dist(xy, cand)
        if dist < best[0]:
            best = (dist, i + f, cand)
    return best[1], best[2]


def _piece_run(
    pts: Sequence[tuple[float, float]],
    a: tuple[float, tuple[float, float], tuple[float, float] | None],
    b: tuple[float, tuple[float, float], tuple[float, float] | None],
) -> list[tuple[float, float]]:
    """The polyline run between two attachments ``(s, point, via|None)``
    — the piece's copper: bracketing via first/last (an off-path via
    joins by the short stub to its own projection), the path vertices
    strictly inside the interval between them. Near-coincident
    neighbours collapse (an on-path via IS its projection)."""
    sa, pa, va = a
    sb, pb, vb = b
    run: list[tuple[float, float]] = []
    if va is not None:
        run.append(va)
    run.append(pa)
    for j in range(1, len(pts) - 1):
        if sa < j < sb:
            run.append(pts[j])
    run.append(pb)
    if vb is not None:
        run.append(vb)
    out = [run[0]]
    for p in run[1:]:
        if math.dist(p, out[-1]) > 1e-9:
            out.append(p)
    return out


#: The shared-gap unwind's displacement cap, as a multiple of the
#: pair's gap — a vertex whose slot-side target sits further than this
#: is a weave, not a fan the unwind can lift; the move is skipped and
#: the emit pass owns the vertex instead (gr537831 review: an
#: unbounded push could drag a leg across the board).
_UNWIND_MOVE_CAP = 4.0
#: Sample spacing (mm) for the free stretch of a shared edge, below the
#: narrowest pad feature that can wall it.
_SPREAD_SAMPLE_MM = 0.02
#: A wrap's outward offset divides the slot sum by the sine of half the
#: bend's interior angle (the legs shift that much per unit of travel
#: along the bisector), floored here so a hairpin does not fling the
#: vertex across the board.
_FAN_MITRE_FLOOR = 0.25


#: A turn past this many degrees is a sharp one — a hairpin no wire
#: should be drawn with (Reto 2026-11-01: any angle, few segments).
_SHARP_TURN_DEG = 120.0


def _turn_deg(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> float:
    """The change of heading at ``b`` along ``a → b → c``, degrees (0 =
    straight on, 180 = doubling back); 0 for a degenerate leg."""
    ux, uy, vx, vy = b[0] - a[0], b[1] - a[1], c[0] - b[0], c[1] - b[1]
    lu, lv = math.hypot(ux, uy), math.hypot(vx, vy)
    if lu < 1e-9 or lv < 1e-9:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, (ux * vx + uy * vy) / (lu * lv)))))


def _sharp_turns(pts: Sequence[tuple[float, float]]) -> int:
    """How many vertices of ``pts`` turn the wire past
    :data:`_SHARP_TURN_DEG`."""
    return sum(
        _turn_deg(a, b, c) > _SHARP_TURN_DEG
        for a, b, c in zip(pts, pts[1:], pts[2:], strict=False)
    )


def _swap_bigon(
    pts_a: Sequence[Pt],
    pts_b: Sequence[Pt],
    x: Cross,
    y: Cross,
    gap: float,
) -> tuple[list[Pt], list[Pt]] | None:
    """Two wires that cross twice around an empty region, uncrossed: each
    takes the other's arc between the crossing points. They then touch at
    the two tips and no longer cross, and since the same points are still
    traversed nothing else can newly cross. Each tip bend then retracts
    into its own angle by half the pair's slot ``gap``, which opens the
    touch to the slot without moving the wire out of its corner's sector.
    ``x`` and ``y`` are the crossings' arc positions (``a``, ``b``). None
    when a result would touch itself or an arc is degenerate."""
    from shapely.ops import substring

    def line(pts: Sequence[Pt]) -> Any:
        return LineString([p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]])

    la, lb = line(pts_a), line(pts_b)
    (sa1, sb1), (sa2, sb2) = ((x[0], x[1]), (y[0], y[1]))
    if sa1 > sa2:
        (sa1, sb1), (sa2, sb2) = (sa2, sb2), (sa1, sb1)
    t1, t2 = min(sb1, sb2), max(sb1, sb2)
    parts = []
    for ln, s1, s2 in ((la, sa1, sa2), (lb, t1, t2)):
        head, mid, tail = (
            substring(ln, 0.0, s1),
            substring(ln, s1, s2),
            substring(ln, s2, ln.length),
        )
        if any(g.geom_type != "LineString" for g in (head, mid, tail)):
            return None
        parts.append((list(head.coords), list(mid.coords), list(tail.coords)))
    (ha, ma, ta), (hb, mb, tb) = parts
    # a's arc runs crossing sa1 -> sa2; b's counterpart runs t1 -> t2, the
    # same way when sb1 < sb2 and the other way when not
    same_way = sb1 < sb2
    b_arc = mb if same_way else mb[::-1]
    a_arc = ma if same_way else ma[::-1]
    new_a = ha + b_arc[1:] + ta[1:]
    new_b = hb + a_arc[1:] + tb[1:]
    for pts, tips in (
        (new_a, (len(ha) - 1, len(ha) + len(b_arc) - 2)),
        (new_b, (len(hb) - 1, len(hb) + len(a_arc) - 2)),
    ):
        for k in tips:
            if 0 < k < len(pts) - 1:
                out = _outward(pts[k - 1], pts[k], pts[k + 1])
                if out is not None:
                    h = (gap / 2.0 + 0.02) / max(out[2], _FAN_MITRE_FLOOR)
                    pts[k] = (pts[k][0] - h * out[0], pts[k][1] - h * out[1])
    if not (LineString(new_a).is_simple and LineString(new_b).is_simple):
        return None
    return (
        [(float(px), float(py)) for px, py in new_a],
        [(float(px), float(py)) for px, py in new_b],
    )


def _repair_bigon(
    a: WirePath,
    b: WirePath,
    x: Cross,
    y: Cross,
    gap: float,
    on_foreign_pad: Callable[[Any, int], float],
    *,
    via_xy: tuple[Iterable[Pt], Iterable[Pt]] | None = None,
) -> tuple[list[Pt], list[Pt]] | None:
    """Return an admissible arc swap, or refuse it. The occupancy witness
    and realiser share every refusal here so a 2D cancellation cannot
    promise a repair the emitter forbids. Each caller supplies its current
    points and pad oracle; the realiser retains its cached rounded via
    coordinates while the witness uses the original planted coordinates.
    """

    def arc_layers(br: WirePath, lo: int, hi: int) -> set[int] | None:
        if len(br.layers) != len(br.edges) + 1:
            return None
        return {
            int(br.layers[i])
            for i in range(max(lo + 1, 0), min(hi, len(br.edges) - 1) + 1)
        }

    def arc_has_via(br: WirePath, points: Iterable[Pt], s1: float, s2: float) -> bool:
        ln = LineString(br.points)
        lo, hi = min(s1, s2), max(s1, s2)
        return any(lo - 1e-9 <= ln.project(Point(p)) <= hi + 1e-9 for p in points)

    if via_xy is None:
        via_xy = ([(v.x, v.y) for v in a.vias], [(v.x, v.y) for v in b.vias])
    if arc_has_via(a, via_xy[0], x[0], y[0]) or arc_has_via(b, via_xy[1], x[1], y[1]):
        return None
    # Both exchanged arcs must occupy the same single layer when known.
    la = arc_layers(a, min(x[2][0], y[2][0]), max(x[2][1], y[2][1]))
    lb = arc_layers(b, min(x[2][2], y[2][2]), max(x[2][3], y[2][3]))
    if la is not None and lb is not None and (len(la) != 1 or la != lb):
        return None
    swapped = _swap_bigon(a.points, b.points, x, y, gap)
    if swapped is None:
        return None
    # Never exchange an arc that increases foreign-pad overlap.
    was = on_foreign_pad(LineString(a.points), a.net) + on_foreign_pad(
        LineString(b.points), b.net
    )
    now = on_foreign_pad(LineString(swapped[0]), a.net) + on_foreign_pad(
        LineString(swapped[1]), b.net
    )
    if now > was + 1e-6:
        return None
    return swapped


def _outside_zone(d: TopoDesign, s_min: float) -> Any:
    """The board's OUTSIDE as an obstacle polygon: the clearance-grown
    edge band when the design carries an edge clearance, else the plain
    exterior of the outline. ``None`` for a design without an outline."""
    from shapely.geometry import box

    edge = _edge_zone(d, s_min)
    if edge is not None:
        return edge[0]
    if len(d.outline) < 3:
        return None
    board = Polygon(np.asarray(d.outline, dtype=float))
    x0, y0, x1, y1 = board.bounds
    return box(x0 - 5.0, y0 - 5.0, x1 + 5.0, y1 + 5.0).difference(board)


def _zone_bodies(
    d: TopoDesign,
    pose: np.ndarray,
    net_id: Mapping[str, int],
    net_rules: Mapping[str, NetRules] | None = None,
) -> list[tuple[Body, Sequence[int]]]:
    """The authored keepout/mounting/fixed-copper zones as index bodies,
    each with the layers it blocks (empty = every layer). Named fixed
    copper/via zones let their net through (:attr:`TopoZone.net`) and
    carry its clearance, so a foreign pair owes the larger rule; a
    ``label_keepout`` is VIA-only (silk over traces) and not a track
    obstacle at all."""
    rules = net_rules or {}
    out: list[tuple[Body, Sequence[int]]] = []
    for z in d.zones:
        if z.kind == "label_keepout":
            continue
        ring = topo_proxy.zone_world_ring(d, z, pose)
        if len(ring) < 3:
            continue
        zp = Polygon(np.asarray(ring))
        if zp.is_empty or zp.area <= 0.0:
            continue
        name = str(z.net or "") if z.kind in ("fixed_copper", "fixed_via") else ""
        clearance = float(rules.get(name, _DEFAULT).clearance_mm) if name else 0.0
        out.append((Body(zp, 0.0, net_id.get(name, ANY_NET), clearance), z.layers))
    return out


def _outward(
    prev: tuple[float, float], p: tuple[float, float], nxt: tuple[float, float]
) -> tuple[float, float, float] | None:
    """The unit vector out of the bend at ``p`` — away from the obstacle
    the bend wraps, which sits inside the angle, so minus the legs'
    bisector — and the sine of half the interior angle (a bend's legs
    shift that fraction of the travel along the bisector). None for a
    (near-)straight pass, which has no side."""
    ax, ay = prev[0] - p[0], prev[1] - p[1]
    bx, by = nxt[0] - p[0], nxt[1] - p[1]
    la, lb = math.hypot(ax, ay), math.hypot(bx, by)
    if la <= 1e-9 or lb <= 1e-9:
        return None
    sx, sy = ax / la + bx / lb, ay / la + by / lb
    ns = math.hypot(sx, sy)
    if ns < 1e-3:
        return None
    return (-sx / ns, -sy / ns, math.sqrt(max(0.0, 1.0 - ns * ns / 4.0)))


def _lift_t(
    free: Callable[[float], bool], *, step: float = 0.02, limit: float = 1.0
) -> float | None:
    """The smallest travel ``t`` (a multiple of ``step``, up to
    ``limit``) at which ``free(t)`` holds, 0.0 when it already does,
    None when it never does."""
    return next(
        (k * step for k in range(int(limit / step) + 1) if free(k * step)), None
    )


@dataclasses.dataclass(frozen=True)
class SpreadOverflow:
    """A run of crossings the free stretch cannot hold: ``need`` mm of
    slots against ``room`` mm between ``lo`` and ``hi``. The spread
    samples the stretch at :data:`_SPREAD_SAMPLE_MM`, so ``room`` can read
    narrower than the corridor cut's ``free``; either way it is named, not
    clamped into a neighbour."""

    need: float
    room: float


def _spread_places(
    nat: Sequence[float], slot: Sequence[float], lo: float, hi: float
) -> list[float] | SpreadOverflow:
    """Where sorted crossings of one edge sit once neighbours are at
    least ``slot`` apart: each as near its natural place ``nat`` as the
    others let it, within ``[lo, hi]`` (mm along the edge). Wires already
    apart stay put; a tight run moves as one block about its mean — a
    single squeezed pair never packs the whole edge.

    In start space (``y_j = place_j - offs_j``, ``offs`` the cumulative
    slots) the slots hold exactly when the starts are non-decreasing, and
    the run lies in ``[lo, hi]`` exactly when every start does:
    ``lo <= y_j <= hi - offs[-1]``. Clipping each block's start to that one
    interval keeps the order, so one pass is exact. A run that does not
    fit (``lo > hi - offs[-1]``) is a :class:`SpreadOverflow`."""
    offs = [0.0]
    for sl in slot:
        offs.append(offs[-1] + sl)
    y_lo, y_hi = lo, hi - offs[-1]
    if y_lo > y_hi + 1e-9:
        return SpreadOverflow(need=offs[-1], room=hi - lo)
    # blocks of consecutive hits: (first, last, start)
    blocks: list[tuple[int, int, float]] = []
    for k, x in enumerate(nat):
        blocks.append((k, k, x - offs[k]))
        while len(blocks) > 1:
            a, b = blocks[-2], blocks[-1]
            if a[2] <= b[2] + 1e-12:  # starts non-decreasing: no overlap
                break
            first, last = a[0], b[1]
            start = sum(nat[j] - offs[j] for j in range(first, last + 1)) / (
                last - first + 1
            )
            blocks[-2:] = [(first, last, start)]
    places = [0.0] * len(nat)
    for first, last, start in blocks:
        y = min(max(start, y_lo), max(y_lo, y_hi))
        for j in range(first, last + 1):
            places[j] = y + offs[j]
    return places


def _spoke_offsets(
    d: TopoDesign,
    mesh: TopoMesh,
    wires: Wires,
    n_layers: int,
    *,
    net_rules: Mapping[str, NetRules] | None = None,
    pose: Any = None,
) -> list[tuple[tuple[float, float], ...]]:
    """The SURF spoke model (topo S2, gr520900): per-branch polylines
    with the wires that share an obstacle vertex fanned out to legal
    spacing.

    The taut funnel bends every wire at the SAME mesh vertex (pad
    centres and pad-ring corners are the obstacles the corridor wraps),
    so two wires round one obstacle leave the realiser coincident copper
    (0.000 mm clearance). For each vertex more than one wire wraps, the
    wraps are ranked by ``wires.order`` on a shared spoke edge —
    nearest-to-vertex first, the tighter wrap — and each bend is moved
    out along the bend's bisector, away from the obstacle the bend
    wraps, by the sum of the inner wires' ``(width + clearance)`` slots.
    A vertex one wire wraps keeps its taut bend: grazes of pads, zones
    and peers are :func:`_emit_shapes`' kink, judged against the
    :class:`CopperIndex` at the point they happen. (``wires.order`` ranks
    only these fan wraps now; the shared-edge spread below sorts by the
    crossings' ``t``.) Two wires that cross twice around an empty bigon
    swap the arcs between the crossings (:func:`_swap_bigon`) before
    the fan, so they no longer cross. A fan move may not increase its
    proper crossings with any foreign peer: a bend otherwise creates
    a new bigon after that repair pass, including on a pair whose
    pre-existing crossing lives on different layers.

    The spread can still interleave a pair that is NOT a real crossing
    (:func:`_geometric_regions` cancels an empty bigon): the taut funnel
    wraps FREE mesh vertices, so one wire's interior vertices land on
    the far side of a peer's lane and the polylines cross. The
    shared-gap unwind (gr537726) lifts such a vertex to the slot gap on
    its own side of the peer, when its two new legs sit no deeper in
    foreign copper than the old ones and cross no peer they did not
    already cross. A pair that IS a real crossing (``braided``) gets no
    spacing from this stage outside the bracketed span: layer assignment
    splits it, :func:`_emit_shapes` attempts the spacing per piece on the
    layers they share, and the anti-short guard verifies it."""
    from shapely import STRtree

    rules = net_rules or {}
    pose_a = np.asarray(d.pose0 if pose is None else pose)

    def nrules(net: int) -> NetRules:
        name = str(d.net_name[net]) if net < len(d.net_name) else str(net)
        return rules.get(name, _DEFAULT)

    def ws(bi: int) -> tuple[float, float]:
        nr = nrules(wires.branches[bi].net)
        return float(nr.track_width_mm), float(nr.clearance_mm)

    vid_of = {
        (round(float(x), 6), round(float(y), 6)): i
        for i, (x, y) in enumerate(mesh.verts)
    }
    spokes: dict[int, list[int]] = {}
    for e, (u, v) in enumerate(mesh.edges):
        spokes.setdefault(int(u), []).append(e)
        spokes.setdefault(int(v), []).append(e)
    pts_out: list[list[tuple[float, float]]] = [
        [(float(x), float(y)) for x, y in b.points] for b in wires.branches
    ]

    # the copper a bend must clear, layer-blind: pads (their net, their
    # clearance), authored zones and the board edge
    net_id = {str(n): k for k, n in enumerate(d.net_name)}
    bodies = [b for b, _lay in _zone_bodies(d, pose_a, net_id, net_rules)]
    outside = _outside_zone(
        d,
        min(
            (float(nrules(b.net).clearance_mm) for b in wires.branches),
            default=float(_DEFAULT.clearance_mm),
        ),
    )
    if outside is not None and not outside.is_empty:
        bodies.append(Body(outside))
    # a built mesh's wires ctx already holds the same pad set
    # (build_wires ran it on this mesh/design) — reuse rather than
    # re-measure every pad; a hand-built mesh has no ctx fields, so it
    # keeps the direct measure (_mesh_pads' own discriminator)
    if mesh.pads is not None:
        _ctx = _wires._make_ctx(mesh, d, n_layers, pose_a)
        pad_polys, pad_pins = _ctx.pad_poly, _ctx.pad_pin
    else:
        _gp = _wires.guard_pad_rects(mesh, d, n_layers, pose_a)
        pad_polys, pad_pins = _gp.polys, _gp.pin
    pad_bodies: list[Body] = []
    pin_nets = d.pin_nets()
    for pi in range(len(pad_polys)):
        pin = int(pad_pins[pi])
        pname, pnet = pin_nets[pin] if 0 <= pin < len(pin_nets) else ("", -1)
        pad_bodies.append(
            Body(
                # the real copper: a comb's gaps are free space, not its box
                pad_polys[pi],
                0.0,
                pnet if pnet >= 0 else ANY_NET,
                float(rules.get(pname, _DEFAULT).clearance_mm) if pname else 0.0,
            )
        )
    bodies += pad_bodies
    index = CopperIndex(bodies)
    # a shared edge is spread over what the PADS leave of it: the mesh
    # already walls zones and the board edge out of the corridors
    pad_index = CopperIndex(pad_bodies)

    pad_geoms = list(pad_polys)
    pad_net = [
        pin_nets[int(p)][1] if 0 <= int(p) < len(pin_nets) else -1 for p in pad_pins
    ]
    pad_tree = STRtree(pad_geoms) if pad_geoms else None

    def on_foreign_pad(geom: Any, net: int) -> float:
        if pad_tree is None:
            return 0.0
        return sum(
            float(geom.intersection(pad_geoms[int(k)]).length)
            for k in pad_tree.query(geom, predicate="intersects")
            if pad_net[int(k)] != net
        )

    # ---- shared-edge crossings: wires threading one gap are spread over
    # the edge's free stretch (what the index leaves of it) at pairwise
    # (w_i+w_j)/2 + clearance spacing, in order of their crossing
    # parameter ``t``, when their natural parameters violate it. A moved crossing becomes a
    # polyline vertex on its edge, which keeps the wire's homotopy — each
    # new leg stays inside the corridor tri it was threaded through. A
    # planted via IS its vertex (G2g): it never moves — the other wires'
    # window clips to the side of it they sit on, its barrel plus their
    # track stand-off wide.
    via_of = {bi: {int(v.i): v for v in b.vias} for bi, b in enumerate(wires.branches)}
    via_xy = {
        bi: frozenset((round(v.x, 6), round(v.y, 6)) for v in b.vias)
        for bi, b in enumerate(wires.branches)
        if b.vias
    }
    eu = mesh.verts[mesh.edges[:, 0]]
    ev = mesh.verts[mesh.edges[:, 1]]
    edge_pos = [{int(e): i for i, e in enumerate(b.edges)} for b in wires.branches]
    moved: dict[int, dict[int, tuple[float, float]]] = {}
    for e in range(len(mesh.edges)):
        hits_all = sorted(
            (
                (bi, edge_pos[bi][e], float(wires.branches[bi].t[edge_pos[bi][e]]))
                for bi in wires.order[e]
                if e in edge_pos[bi]
                and 1e-9 < wires.branches[bi].t[edge_pos[bi][e]] < 1.0 - 1e-9
            ),
            key=lambda h: h[2],
        )
        fixed = [h for h in hits_all if h[1] in via_of.get(h[0], ())]
        hits = [h for h in hits_all if h[1] not in via_of.get(h[0], ())]
        seglen = float(np.hypot(*(ev[e] - eu[e])))
        if len(hits) < 2 or seglen <= 1e-9:
            continue
        n_s = int(seglen / _SPREAD_SAMPLE_MM) + 2
        ts = np.linspace(0.0, 1.0, n_s)
        # a pad of any of the crossing nets walls none of them
        free_s = np.zeros(n_s, dtype=bool)
        for bi in {h[0] for h in hits}:
            w_k, s_k = ws(bi)
            free_s |= ~pad_index.blocked_points(
                eu[e] + ts[:, None] * (ev[e] - eu[e]),
                w_k / 2.0,
                s_k,
                net=wires.branches[bi].net,
            )
        # the longest free run, as parameters (empty when walled)
        lo_t = hi_t = 0.0
        run = 0
        for k in range(n_s + 1):
            if k < n_s and free_s[k]:
                run += 1
                continue
            if run and ts[k - 1] - ts[k - run] > hi_t - lo_t:
                lo_t, hi_t = float(ts[k - run]), float(ts[k - 1])
            run = 0
        if hi_t <= lo_t:
            continue  # walled edge: nothing to spread over
        # group the crossings by which side of each planted via their t
        # sits on: a group spreads inside [lo_t, hi_t] clipped by the via
        # margins on either side (r_site + own half-width + the two
        # clearances' max)
        vrows = sorted((h[2], via_of[h[0]][h[1]], ws(h[0])[1]) for h in fixed)
        bounds_t = [0.0] + [r[0] for r in vrows] + [1.0]
        groups: dict[int, list[tuple[int, int, float]]] = {}
        for h in hits:
            gi = max(g for g, p in enumerate(bounds_t) if p <= h[2] + 1e-12)
            groups.setdefault(min(gi, len(bounds_t) - 2), []).append(h)
        for gi, grp in sorted(groups.items()):
            lo_g, hi_g = lo_t, hi_t
            if gi > 0:
                v = vrows[gi - 1]
                lo_g = max(
                    lo_g,
                    v[0]
                    + max(
                        v[1].r_site_mm
                        + ws(h[0])[0] / 2.0
                        + max(v[2], ws(h[0])[1])
                        + FAB_QUANTUM_MM
                        for h in grp
                    )
                    / seglen,
                )
            if gi < len(vrows):
                v = vrows[gi]
                hi_g = min(
                    hi_g,
                    v[0]
                    - max(
                        v[1].r_site_mm
                        + ws(h[0])[0] / 2.0
                        + max(v[2], ws(h[0])[1])
                        + FAB_QUANTUM_MM
                        for h in grp
                    )
                    / seglen,
                )
            slot = [
                (ws(bi)[0] + ws(bj)[0]) / 2.0
                + max(ws(bi)[1], ws(bj)[1])
                + FAB_QUANTUM_MM
                for (bi, _i, _t), (bj, _j, _u) in itertools.pairwise(grp)
            ]
            nat = [h[2] for h in grp]
            if all(
                (nat[k + 1] - nat[k]) * seglen >= slot[k] - 1e-9
                for k in range(len(slot))
            ) and all(lo_g - 1e-9 <= t <= hi_g + 1e-9 for t in nat):
                continue
            places = _spread_places(
                [t * seglen for t in nat], slot, lo_g * seglen, hi_g * seglen
            )
            if isinstance(places, SpreadOverflow):
                # the slots do not fit the free stretch: the crossings stay
                # where they are and the anti-short guard owns the pair
                log.info(
                    "topo S2: edge %d spread overflow — %d crossings need %.3f mm "
                    "of slots, %.3f mm free (sampled at %.2f mm)",
                    e,
                    len(grp),
                    places.need,
                    places.room,
                    _SPREAD_SAMPLE_MM,
                )
                continue
            for (bi, i, _t), p in zip(grp, places, strict=True):
                t_new = min(max(p / seglen, 0.0), 1.0)
                moved.setdefault(bi, {})[i] = (
                    float(eu[e][0] + t_new * (ev[e][0] - eu[e][0])),
                    float(eu[e][1] + t_new * (ev[e][1] - eu[e][1])),
                )
    for bi, mv in moved.items():
        br = wires.branches[bi]
        by_leg: dict[int, list[tuple[float, tuple[float, float]]]] = {}
        for i, pt in mv.items():
            q = edge_xy(mesh, int(br.edges[i]), float(br.t[i]))
            j, _d = min(
                (
                    (j, seg_dist(q, br.points[j], br.points[j + 1]))
                    for j in range(len(br.points) - 1)
                ),
                key=lambda js: js[1],
            )
            dx, dy = (
                br.points[j + 1][0] - br.points[j][0],
                br.points[j + 1][1] - br.points[j][1],
            )
            frac = (
                (q[0] - br.points[j][0]) * dx + (q[1] - br.points[j][1]) * dy
            ) / max(dx * dx + dy * dy, 1e-12)
            by_leg.setdefault(j, []).append((frac, pt))
        newp = [pts_out[bi][0]]
        for j in range(len(br.points) - 1):
            newp += [pt for _f, pt in sorted(by_leg.get(j, []))]
            newp.append(pts_out[bi][j + 1])
        # a crossing moved far along a wire that meets its edge at a
        # shallow angle doubles the wire back: such a move is dropped
        added = set(mv.values())
        k = 1
        while k < len(newp) - 1:
            if newp[k] in added and (
                _turn_deg(newp[k - 1], newp[k], newp[k + 1]) > _SHARP_TURN_DEG
            ):
                del newp[k]
                k = max(1, k - 1)
            else:
                k += 1
        pts_out[bi] = newp

    # ---- cancelled bigons: two wires crossing twice around an empty
    # region can slide apart through it (:func:`_geometric_regions`
    # cancels the pair), but nothing in the copper does it — they swap
    # the arcs between the crossings, so they touch at the tips and cross
    # nowhere. Events are re-derived after each swap.
    obstacles = _obstacle_xy(mesh, d, n_layers)

    skipped: set[tuple[int, int, float, float]] = set()
    for _swap in range(64):
        cur = dataclasses.replace(
            wires,
            branches=tuple(
                dataclasses.replace(b, points=tuple(p))
                for b, p in zip(wires.branches, pts_out, strict=True)
            ),
        )
        todo = [
            (a, b, x, y, (a, b, round(x[0], 3), round(y[0], 3)))
            for a, b, x, y in resolve_crossings(mesh, cur.branches, obstacles)[1]
            if wires.branches[a].net != wires.branches[b].net
        ]
        todo = [c for c in todo if c[4] not in skipped]
        if not todo:
            break
        a, b, x, y, key = todo[0]

        swapped = _repair_bigon(
            cur.branches[a],
            cur.branches[b],
            x,
            y,
            (ws(a)[0] + ws(b)[0]) / 2.0 + max(ws(a)[1], ws(b)[1]) + FAB_QUANTUM_MM,
            on_foreign_pad,
            via_xy=(via_xy.get(a, ()), via_xy.get(b, ())),
        )
        if swapped is None:
            skipped.add(key)
            log.info("topo S2: cancelled bigon of branches %d, %d not swapped", a, b)
            continue
        pts_out[a], pts_out[b] = swapped

    # ---- bend offsets: wires wrapping one obstacle vertex fan by rank.
    bend_branches = [
        dataclasses.replace(b, points=tuple(p))
        for b, p in zip(wires.branches, pts_out, strict=True)
    ]
    bend_geoms = [branch_geom(mesh, b) for b in bend_branches]
    bend_bounds = np.asarray([LineString(b.points).bounds for b in bend_branches])

    def admit_fan(moves: list[tuple[int, int, Pt]]) -> None:
        """Judge a shared-vertex fan after all its branches have moved.

        A legal collective fan can cross a peer's old position while
        moving to its own slot. Only the complete fan's crossings matter.
        """
        if not moves:
            return
        candidates: dict[int, list[Pt]] = {}
        for bi, j, point in moves:
            candidates.setdefault(bi, list(pts_out[bi]))[j] = point
        envelope = np.asarray(
            [p for bi, pts in candidates.items() for p in (*pts_out[bi], *pts)]
        )
        lo, hi = envelope.min(axis=0) - CROSS_EPS, envelope.max(axis=0) + CROSS_EPS
        near = np.flatnonzero(
            (bend_bounds[:, 0] <= hi[0])
            & (bend_bounds[:, 2] >= lo[0])
            & (bend_bounds[:, 1] <= hi[1])
            & (bend_bounds[:, 3] >= lo[1])
        )
        ids = sorted(set(map(int, near)) | candidates.keys())
        branches = [bend_branches[k] for k in ids]
        geoms = [bend_geoms[k] for k in ids]
        before = crossing_events(mesh, branches, geoms)
        for i, bi in enumerate(ids):
            if bi in candidates:
                branches[i] = dataclasses.replace(
                    branches[i], points=tuple(candidates[bi])
                )
                geoms[i] = branch_geom(mesh, branches[i])
        after = crossing_events(mesh, branches, geoms)
        if any(
            branches[a].net != branches[b].net
            and (ids[a] in candidates or ids[b] in candidates)
            and len(events) > len(before.get((a, b), ()))
            for (a, b), events in after.items()
        ):
            return
        for i, bi in enumerate(ids):
            if bi in candidates:
                pts_out[bi] = candidates[bi]
                bend_branches[bi], bend_geoms[bi] = branches[i], geoms[i]
                bend_bounds[bi] = LineString(branches[i].points).bounds

    wraps: dict[int, list[tuple[int, int]]] = {}
    for bi, pts in enumerate(pts_out):
        for j in range(1, len(pts) - 1):
            v = vid_of.get((round(pts[j][0], 6), round(pts[j][1], 6)))
            if v is not None:
                wraps.setdefault(v, []).append((bi, j))

    def tightness(bi: int, j: int) -> float:
        pts = pts_out[bi]
        a = np.asarray(pts[j - 1]) - np.asarray(pts[j])
        bb = np.asarray(pts[j + 1]) - np.asarray(pts[j])
        la, lb = float(np.hypot(*a)), float(np.hypot(*bb))
        if la <= 1e-9 or lb <= 1e-9:
            return 0.0
        return float(np.hypot(*(a / la + bb / lb)))

    for vid, lst in wraps.items():
        bis = {bi for bi, _ in lst}
        if len(bis) < 2:
            continue
        seq: list[int] = []
        for e in spokes.get(vid, ()):
            shared = [bi for bi in wires.order[e] if bi in bis]
            if len(shared) >= 2:
                seq = shared if int(mesh.edges[e, 0]) == vid else shared[::-1]
                break
        if not seq:
            seq = [
                bi
                for bi, _ in sorted(
                    lst, key=lambda k: (tightness(k[0], k[1]), k[0], k[1])
                )
            ]
        rank = {
            bi: r for r, bi in enumerate(dict.fromkeys(seq + [bi for bi, _ in lst]))
        }
        w = np.asarray(mesh.verts[vid], dtype=float)
        moves: list[tuple[int, int, Pt]] = []
        for bi, j in lst:
            w_k, s_k = ws(bi)
            inner = sum(
                float(nrules(wires.branches[ob].net).track_width_mm)
                + max(s_k, float(nrules(wires.branches[ob].net).clearance_mm))
                + FAB_QUANTUM_MM
                for ob in seq[: rank.get(bi, 0)]
                if ob != bi
            )
            pts = pts_out[bi]
            out = _outward(pts[j - 1], (float(w[0]), float(w[1])), pts[j + 1])
            if out is None:
                continue
            net = wires.branches[bi].net

            def clear(
                g: Any, w_k: float = w_k, s_k: float = s_k, net: int = net
            ) -> bool:
                return not index.blocked(g, w_k / 2.0, s_k, net=net)

            # off the obstacle the bend wraps by its margin, then the
            # inner wires' slots on top
            candidate = _lift_bend(
                pts[j - 1],
                (float(w[0]), float(w[1])),
                pts[j + 1],
                clear,
                extra=inner / max(out[2], _FAN_MITRE_FLOOR),
                legs_first=True,
                limit=max(1.0, 2.0 * (w_k / 2.0 + s_k)),
            )
            if candidate != pts[j]:
                moves.append((bi, j, candidate))
        admit_fan(moves)

    # ── shared-gap unwind (gr537726) ─────────────────────────────────
    braided = set(_geometric_regions(mesh, wires, _obstacle_xy(mesh, d, n_layers)))
    lines = [LineString(p) for p in pts_out]
    pair_gap: dict[tuple[int, int], float] = {}
    hull = [lines[i].bounds for i in range(len(lines))]
    for i in range(len(pts_out)):
        w_i, s_i = ws(i)
        for j in range(i + 1, len(pts_out)):
            if wires.branches[i].net == wires.branches[j].net or (i, j) in braided:
                continue
            w_j, s_j = ws(j)
            gap = (w_i + w_j) / 2.0 + max(s_i, s_j) + FAB_QUANTUM_MM
            infl = gap * (1.0 + _UNWIND_MOVE_CAP)
            if not (
                hull[i][2] < hull[j][0] - infl
                or hull[j][2] < hull[i][0] - infl
                or hull[i][3] < hull[j][1] - infl
                or hull[j][3] < hull[i][1] - infl
            ):
                pair_gap[(i, j)] = gap

    geoms = [b.geom for b in bodies]
    tree = STRtree(geoms) if geoms else None
    reach = max((b.r + b.clearance for b in bodies), default=0.0)

    def depth(geom: Any, bi: int) -> float:
        """How far ``geom`` sits inside the clearance ring of the index's
        foreign copper for branch ``bi`` (mm; 0 = clear) — the figure the
        unwind compares old legs against new ones with."""
        if tree is None:
            return 0.0
        w_k, s_k = ws(bi)
        net = wires.branches[bi].net
        worst = 0.0
        for k in tree.query(
            geom, predicate="dwithin", distance=w_k / 2.0 + s_k + reach + FAB_QUANTUM_MM
        ):
            b = bodies[int(k)]
            if b.net == net:
                continue
            need = w_k / 2.0 + b.r + max(s_k, b.clearance) + FAB_QUANTUM_MM
            gap = geoms[int(k)].distance(geom)
            # a leg through the body is deeper than one grazing it:
            # count the length it spends inside
            inside = geoms[int(k)].intersection(geom).length if gap <= 0.0 else 0.0
            worst = max(worst, need - gap + inside)
        return worst

    def _nearest(
        p: tuple[float, float], pts: list[tuple[float, float]]
    ) -> tuple[tuple[float, float], tuple[float, float], float]:
        """The polyline point nearest ``p`` — (point, host leg
        direction, distance). The leg direction is what the slot
        side is measured against."""
        q_out, d_out, dir_out = pts[0], math.inf, (1.0, 0.0)
        for k in range(len(pts) - 1):
            a_, b_ = pts[k], pts[k + 1]
            dx, dy = b_[0] - a_[0], b_[1] - a_[1]
            l2 = dx * dx + dy * dy
            u = (
                0.0
                if l2 <= 1e-12
                else min(
                    max(((p[0] - a_[0]) * dx + (p[1] - a_[1]) * dy) / l2, 0.0),
                    1.0,
                )
            )
            dd = math.dist(p, (a_[0] + u * dx, a_[1] + u * dy))
            if dd < d_out:
                d_out = dd
                q_out = (a_[0] + u * dx, a_[1] + u * dy)
                dir_out = (dx, dy)
        return q_out, dir_out, d_out

    def _end_side(m: int, peer: int) -> int:
        """+1/-1 — the side of the peer's polyline the mover's two
        endpoints agree on (left of each nearest leg's travel
        direction), or 0 when they straddle it."""
        side = 0
        for p_end in (pts_out[m][0], pts_out[m][-1]):
            q, ld, _d = _nearest(p_end, pts_out[peer])
            cr = ld[0] * (p_end[1] - q[1]) - ld[1] * (p_end[0] - q[0])
            if abs(cr) <= 1e-9:
                continue  # endpoint sits on the line — no vote
            s = 1 if cr > 0.0 else -1
            if side != 0 and s != side:
                return 0
            side = s
        return side

    def _wrong_depth(m: int, peer: int) -> float:
        """How far the mover's interior vertices sit on the wrong
        side of the peer's lane — summed distance past the line,
        0 when its endpoints straddle the peer (a weave). The wire
        deeper in is the one the unwind pushes."""
        side = _end_side(m, peer)
        total = 0.0
        for pv in pts_out[m][1:-1] if side else ():
            q, ld, _dd = _nearest(pv, pts_out[peer])
            ln = math.hypot(*ld)
            if ln > 1e-9:
                total += max(
                    0.0,
                    -((ld[0] * (pv[1] - q[1]) - ld[1] * (pv[0] - q[0])) / ln) * side,
                )
        return total

    def _unwind_ok(bi: int, jj: int, cand: tuple[float, float]) -> bool:
        """The check a displaced vertex's two NEW legs must pass before
        the unwind may commit them. Both compare against the OLD legs: a
        move may keep a violation it already carried (a leg lifting OFF a
        pad corner is exactly the move the unwind exists for) but never
        mints a fresh one — the new legs sit no deeper in the index's
        foreign copper (pads, zones, the board edge) than the old ones,
        and cross no other non-braided polyline the old legs did not
        cross. A move that fails keeps its old vertex."""
        mpts = pts_out[bi]
        legs = (
            LineString([mpts[jj - 1], cand]),
            LineString([cand, mpts[jj + 1]]),
        )
        old_legs = (
            LineString([mpts[jj - 1], mpts[jj]]),
            LineString([mpts[jj], mpts[jj + 1]]),
        )
        if (
            max(depth(leg, bi) for leg in legs)
            > max(depth(o, bi) for o in old_legs) + 1e-9
        ):
            return False
        for j, other in enumerate(lines):
            if j == bi or (min(bi, j), max(bi, j)) in braided:
                continue
            ob = other.bounds
            for leg in legs:
                lb = leg.bounds
                if lb[2] < ob[0] or ob[2] < lb[0] or lb[3] < ob[1] or ob[3] < lb[1]:
                    continue
                if leg.crosses(other) and not any(o.crosses(other) for o in old_legs):
                    return False
        return True

    def _unwind_pair(mover: int, peer: int, gap: float) -> None:
        """Lift the mover's interior vertices that sit on the wrong
        side of the peer's lane out to the slot gap."""
        m_pts = pts_out[mover]
        side = _end_side(mover, peer)
        if side == 0:
            return  # straddling ends: a weave, not a fan
        for jj in range(1, len(m_pts) - 1):
            pv = m_pts[jj]
            if (round(pv[0], 6), round(pv[1], 6)) in via_xy.get(mover, ()):
                continue  # a planted via never moves
            q, ld, _dd = _nearest(pv, pts_out[peer])
            ln = math.hypot(*ld)
            if ln <= 1e-9:
                continue
            s = ((ld[0] * (pv[1] - q[1]) - ld[1] * (pv[0] - q[0])) / ln) * side
            if s >= 0.0:
                continue  # slot side held — crowding is the emit pass's
            cand = (
                q[0] + (gap + 0.05) * (-ld[1] / ln) * side,
                q[1] + (gap + 0.05) * (ld[0] / ln) * side,
            )
            # the move is bounded — a far-side vertex is a weave, not a
            # fan the unwind can lift
            if math.dist(pv, cand) > _UNWIND_MOVE_CAP * gap:
                continue
            if _unwind_ok(mover, jj, cand):
                m_pts[jj] = cand
                lines[mover] = LineString(m_pts)

    for _sweep in range(2):
        for (i, j), gap in sorted(pair_gap.items()):
            # the wire sitting deeper on the wrong side moves first; the
            # other then lifts whatever of its own is still wrong (a
            # vertex on its slot side stays), ties start with the
            # higher-index wire
            first, second = (
                (i, j) if _wrong_depth(i, j) > _wrong_depth(j, i) else (j, i)
            )
            _unwind_pair(first, second, gap)
            _unwind_pair(second, first, gap)

    # ── no offset may put a leg on a foreign pad: a vertex the stages
    # above moved (the fan jog, the crossing spread, the unwind) that now
    # drags a leg through another net's copper goes back to the nearest
    # taut vertex, where the corridor walls kept the wire off every pad
    for bi, pts in enumerate(pts_out):
        net = wires.branches[bi].net
        taut = [(float(x), float(y)) for x, y in wires.branches[bi].points]
        for j in range(1, len(pts) - 1):
            now = on_foreign_pad(LineString(pts[j - 1 : j + 2]), net)
            if now <= 1e-6:
                continue
            back = min(taut, key=functools.partial(math.dist, pts[j]))
            if on_foreign_pad(LineString([pts[j - 1], back, pts[j + 1]]), net) < now:
                pts[j] = back

    return [tuple(p) for p in pts_out]


#: The last :func:`_corridor_masks` result with the objects it was keyed on
#: (held, so their ids stay theirs): ``assign_layers`` asks for the same
#: masks of the same pose back to back.
_OPEN_MASKS_MEMO: tuple[tuple[Any, ...], dict[int, np.ndarray]] | None = None


def _corridor_masks(
    d: TopoDesign,
    mesh: TopoMesh,
    wires: Wires,
    n_layers: int,
    rules: Mapping[str, NetRules],
    ctx: Any = None,
) -> dict[int, np.ndarray]:
    """Per net, the corridor's per-edge open-layer bitmask
    (:func:`wires._open_layers`) — what :func:`assign_layers` holds every
    run to. Empty on a mesh with no corridor data (synthetic fixtures)."""
    global _OPEN_MASKS_MEMO
    if not (len(mesh.tris) and len(mesh.pin_vertex) == len(d.pin_xy)):
        return {}
    key = (d, mesh, wires.net_layers, rules, n_layers)
    memo = _OPEN_MASKS_MEMO
    if memo is not None and all(a is b for a, b in zip(memo[0][:4], key[:4])):
        if memo[0][4] == n_layers and all(
            int(b.net) in memo[1] for b in wires.branches
        ):
            return memo[1]
    if ctx is None:
        ctx = _wires._make_ctx(mesh, d, n_layers)
    out: dict[int, np.ndarray] = {}
    for k in {int(b.net) for b in wires.branches}:
        name = str(d.net_name[k]) if k < len(d.net_name) else str(k)
        nr = rules.get(name, _DEFAULT)
        m = _wires._net_masks(
            ctx,
            d,
            k,
            n_layers,
            float(nr.track_width_mm),
            float(nr.clearance_mm),
            float(nr.via_dia_mm or _VIA_DIA_MM),
        )
        out[k] = m.open
    _OPEN_MASKS_MEMO = (key, out)
    return out


def plan_route(
    d: TopoDesign,
    mesh: TopoMesh,
    wires: Wires,
    n_layers: int,
    *,
    net_rules: Mapping[str, NetRules] | None = None,
    pose: Any = None,
    min_via_dia: float | None = None,
    min_via_drill: float | None = None,
    paths: Sequence[Sequence[tuple[float, float]]] | None = None,
    event: Callable[[str], None] | None = None,
    masks: BoardMasks | None = None,
    hv_weight: float = 0.0,
) -> RoutePlan:
    """Layer assignment → pieces + via sites for one routed pose.

    Per branch the runs/cuts model drives the joins directly: every
    cut mints a via site (an F↔B mid-branch join, a crossing's
    bracketed piece, an end via where a forced pad side must be the
    terminal layer), every run emits one :class:`RoutePiece` on its
    layer. Each join's site is the point the search or the resolver
    planted (G2g) — kept verbatim, never re-sited; a join whose hop
    the same-net collision pass dropped mints none and lands on
    :attr:`RoutePlan.no_via`. ``min_via_dia``/``min_via_drill`` are
    accepted for the call chain's signature but inert: via sizing is
    the planter's business, there is no smaller-diameter fallback
    site anymore (a corridor with no rule-dia site strands the layer
    change at search time, not at emit)."""
    wires2, runs, cuts, _crossings_dp = assign_layers(
        d,
        mesh,
        wires,
        n_layers,
        net_rules=net_rules,
        pose=pose,
        event=event,
        masks=masks,
        hv_weight=hv_weight,
    )
    if wires2.branches is not wires.branches and paths is not None:
        # the spoke fan was computed on the branches the caller handed
        # in — a resolver-rebuilt branch carries different taut points,
        # so its row is stale; refit the whole fan (the wraps rank
        # against each other)
        paths = _spoke_offsets(
            d, mesh, wires2, n_layers, net_rules=net_rules, pose=pose
        )
    wires = wires2
    # crossings are judged on the layering the plan emits: the branch's
    # b.layers where present (G2g), the runs/cuts elsewhere
    crossings = _live_crossings(d, mesh, wires, n_layers, runs, cuts)
    rules = net_rules or {}
    # A mesh with a corridor had a planter run in it (search or
    # resolver): its join sites are the planted points. A corridor-less
    # synthetic mesh never did — its joins site on the wire's own
    # crossing of the cut edge, the naive mint, since there is nothing
    # else to judge a point against.
    real_mesh = len(mesh.tris) > 0 and len(mesh.pin_vertex) == len(d.pin_xy)
    pieces: list[RoutePiece] = []
    vias: list[ViaSite] = []
    no_via: list[int] = []
    no_via_reason: dict[int, str] = {}
    for bi, b in enumerate(wires.branches):
        # the committed layering is authoritative (G2g): a layer change
        # is a planted via — kept, or dropped by the same-net collision
        # pass (a join that was never minted, named below). runs/cuts
        # carry it verbatim for every branch (:func:`_layers_segment`).
        planted = {v.i: v for v in b.vias}
        joins = cuts[bi]
        run_layers = runs[bi]
        # ``paths`` carries the spoke-offset polyline (gr520900) for the
        # emitted geometry; the edge/t bookkeeping stays on ``b``'s
        # corridor-true fields.
        bpts = paths[bi] if paths is not None else b.points
        if not joins:
            pieces.append(
                RoutePiece(b.net, bi, run_layers[0], tuple(bpts), b.pin_a, b.pin_b)
            )
            lb = _pin_forced(d, b.pin_b, n_layers)
            if lb is not None and lb != run_layers[0]:
                # An edgeless hop (``n_e == 0`` — the whole wire sits
                # inside pad territory) carries no mesh edge a join
                # could site on, so no cut was planned and the piece
                # lands on its run's layer while the far pad's mount
                # side needs ``lb`` — an island (gr537831's s5 BOOT).
                # Report the join as unsited rather than emitting copper
                # that cannot connect.
                no_via.append(bi)
                no_via_reason[bi] = "edgeless branch ends on the wrong pad side"
            continue
        name = str(d.net_name[b.net]) if b.net < len(d.net_name) else str(b.net)
        nr = rules.get(name, _DEFAULT)
        via_dia = float(nr.via_dia_mm or _VIA_DIA_MM)
        clearance = float(nr.clearance_mm)
        pts = list(bpts)
        cut_xy: list[tuple[float, float]] = []
        sited = True
        reasons: list[str] = []
        own: list[tuple[float, float, float]] = []  # this branch's vias
        for p, ei in enumerate(joins):
            v = planted.get(ei)
            if v is not None:
                # the point the search admitted is the point kept —
                # nothing re-sites it (G2g)
                e, t = int(b.edges[ei]), float(b.t[ei])
                x, y, via_d, drill = v.x, v.y, v.dia_mm, v.drill_mm
            elif not real_mesh:
                # the naive mint: no planter ran on a corridor-less
                # mesh, so the join sites on the wire's own crossing of
                # the cut edge — still spaced off the branch's own
                # barrels. Pads are unchecked: the path exists for
                # corridor-less fixture meshes only, where there is no
                # corridor (and so no planter index) to judge a site
                # against.
                e, t = int(b.edges[ei]), float(b.t[ei])
                x, y = edge_xy(mesh, e, t)
                via_d, drill = via_dia, float(getattr(nr, "via_drill_mm", 0.0) or 0.0)
                if any(
                    math.dist((x, y), o[:2])
                    < (via_d + o[2]) / 2.0 + clearance + FAB_QUANTUM_MM
                    for o in own
                ):
                    sited = False
                    reasons.append(f"cut@{ei}: collides with the branch's earlier via")
                    cut_xy.append((x, y))
                    continue
            elif len(b.layers) == len(b.edges) + 1:
                # a layer change the search made but never minted — the
                # same-net collision pass dropped it
                sited = False
                reasons.append(f"cut@{ei}: planted vias collide (same net)")
                cut_xy.append(edge_xy(mesh, int(b.edges[ei]), float(b.t[ei])))
                continue
            else:
                # an unlayered branch on a real mesh: nothing planted a
                # point and no other qualification exists — refuse
                # rather than emit unjudged copper
                sited = False
                reasons.append(f"cut@{ei}: no planted via site")
                cut_xy.append(edge_xy(mesh, int(b.edges[ei]), float(b.t[ei])))
                continue
            own.append((x, y, via_d))
            vias.append(ViaSite(b.net, bi, e, t, x, y, via_d, drill))
            cut_xy.append((x, y))
        if not sited:
            no_via.append(bi)
            no_via_reason[bi] = "; ".join(reasons)
        # split the polyline at each via's OWN point on the path — the
        # wire's crossing of the cut edge when the site stayed put, the
        # walked site's projection otherwise (a short stub joins an
        # off-path via). Never the nearest vertex: a taut branch keeps
        # vertices only at bends, so a vertex split re-issued the
        # neighbouring runs' geometry on the piece's own layer — the
        # coincident-copper DRC (gr520902 s4d). Sort the (arc, site)
        # pairs TOGETHER (a.2): a site walked off its intended edge can
        # invert the join order, and only a pair sorted as one keeps
        # each run joined to its via.
        marks = sorted((*_polyline_arc(pts, xy), xy) for xy in cut_xy)
        attach: list[tuple[float, tuple[float, float], tuple[float, float] | None]] = [
            (0.0, pts[0], None)
        ]
        attach += [(s, p, xy) for s, p, xy in marks]
        attach.append((float(len(pts) - 1), pts[-1], None))
        for pi, (a_, b_) in enumerate(itertools.pairwise(attach)):
            run = _piece_run(pts, a_, b_)
            if len(run) >= 2:
                pieces.append(
                    RoutePiece(b.net, bi, run_layers[pi], tuple(run), b.pin_a, b.pin_b)
                )
    unrouted = tuple(sorted({u.net for u in wires.unreachable}))
    return RoutePlan(
        pieces=tuple(pieces),
        vias=tuple(vias),
        layers=tuple(runs[bi][0] for bi in range(len(wires.branches))),
        crossings=crossings,
        unreachable=unrouted,
        stranded=wires.unreachable,
        no_via=tuple(sorted(no_via)),
        no_via_reason=no_via_reason,
        branch_lineage=wires.branch_lineage,
        branch_ids=wires.branch_ids,
    )


#: Emit-side vertex dedupe reach (gr543719): the spoke offsets and the
#: piece-split projections can mint near-zero-length stubs (stored
#: copper carried 0.01 mm segments on taz-leaf-i). Vertices closer than
#: this to their predecessor coalesce before segments are built.
_COALESCE_EPS_MM = 1e-3


def _coalesce_micro(
    pts: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    """Drop micro segments and redundant collinear interior vertices.

    Collinear mesh crossings must not anchor the kink search inside a
    peer's clearance band: they change no geometry but can leave every
    short leg with a blocked endpoint. The endpoints stay verbatim —
    they are pad and via joins of this already split piece.
    """
    if len(pts) < 3:
        return tuple((float(x), float(y)) for x, y in pts)
    out: list[tuple[float, float]] = [(float(pts[0][0]), float(pts[0][1]))]
    for x, y in pts[1:-1]:
        v = (float(x), float(y))
        if math.dist(out[-1], v) >= _COALESCE_EPS_MM:
            out.append(v)
    end = (float(pts[-1][0]), float(pts[-1][1]))
    if len(out) > 1 and math.dist(out[-1], end) < _COALESCE_EPS_MM:
        out[-1] = end
    else:
        out.append(end)
    straight: list[tuple[float, float]] = []
    for p in out:
        while len(straight) >= 2 and seg_dist(straight[-1], straight[-2], p) <= 1e-9:
            straight.pop()
        straight.append(p)
    return tuple(straight)


def _pull_taut(
    pts: tuple[tuple[float, float], ...],
    clear: Callable[[LineString], bool],
) -> tuple[tuple[float, float], ...]:
    """The fewest-segment form of a piece's polyline (Reto 2026-11-01:
    any angle, few segments): from each kept vertex, jump to the
    FARTHEST later vertex whose straight segment ``clear`` accepts. A
    vertex no shortcut can skip stays, and the original legs are kept
    as they are. Endpoints never move."""
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    i, n = 0, len(pts)
    while i < n - 1:
        j = n - 1
        while j > i + 1 and not clear(LineString([pts[i], pts[j]])):
            j -= 1
        out.append(pts[j])
        i = j
    return tuple(out)


#: Kink offsets tried at the offending point, perpendicular to the leg,
#: each side, nearest first (mm). Small on purpose: a kink repairs a
#: graze, it does not re-route — the axis-corner and 0.8/1.2 mm
#: candidates found "clear" board-wide detours whose copper, frozen for
#: the re-route rounds, walled the remaining nets off (sanity 7/20
#: routed with them, 18/20 without; taz-leaf-local-66 11/31 either way).
#: Kink-at-the-offence retested (G3) without 0.8/1.2: same numbers on
#: sanity and 2×, so they stay out.
_KINK_OFFSETS_MM = (0.05, 0.1, 0.2, 0.3, 0.5)
#: A kink's two new legs may themselves graze elsewhere: each is kinked
#: in turn, this many levels deep.
_KINK_DEPTH = 2
#: Spacing of the sample points that locate where a blocked leg presses
#: on copper (mm) — below the narrowest pad feature a graze can hide in.
_KINK_SAMPLE_MM = 0.05


def _kink(
    p: tuple[float, float],
    q: tuple[float, float],
    clear: Callable[[LineString], bool],
    offence: Callable[[tuple[float, float], tuple[float, float]], tuple[float, float]],
    depth: int = _KINK_DEPTH,
    offsets: Sequence[float] = _KINK_OFFSETS_MM,
) -> list[tuple[float, float]] | None:
    """The points after ``p`` that carry the leg ``p → q`` clear of
    copper, ending at ``q``, or None — the repair for a leg that grazes
    (Reto 2026-11-01: "kink the wires … after topo router is done").
    ``offence`` names the point of the leg that presses on copper; the
    kink goes there, perpendicular to the leg, nearest offset first and
    each side, so a wire grazing one pad bends once, at the pad. A new
    leg that still grazes is kinked in turn, ``depth`` levels deep."""
    if clear(LineString([p, q])):
        return [q]
    length = math.dist(p, q)
    if depth <= 0 or length < _COALESCE_EPS_MM:
        return None
    x = offence(p, q)
    nx, ny = -(q[1] - p[1]) / length, (q[0] - p[0]) / length
    for k in offsets:
        for s in (1.0, -1.0):
            c = (x[0] + s * nx * k, x[1] + s * ny * k)
            # a repair of a repair stays small
            head = _kink(p, c, clear, offence, depth - 1, _KINK_OFFSETS_MM[:4])
            tail = (
                _kink(c, q, clear, offence, depth - 1, _KINK_OFFSETS_MM[:4])
                if head
                else None
            )
            if head and tail:
                path = [p, *head, *tail]
                if all(
                    _turn_deg(a, b, c) <= _SHARP_TURN_DEG
                    for a, b, c in zip(path, path[1:], path[2:], strict=False)
                ):
                    return head + tail
    return None


def _lift_bend(
    prev: tuple[float, float],
    p: tuple[float, float],
    nxt: tuple[float, float],
    clear: Callable[[Any], bool],
    *,
    extra: float = 0.0,
    legs_first: bool = False,
    legs_too: bool = True,
    limit: float = 1.0,
) -> tuple[float, float]:
    """A bend that presses on copper, moved off it — outward, away from
    the obstacle it wraps, the other way when that side is walled — by
    the least travel that clears the bend and both legs (failing that,
    the bend alone: :func:`_kink` then repairs the legs), plus ``extra``
    further along the same way. With ``legs_first`` false a clear bend
    stays put whatever its legs do; the legs are the kink's, and
    ``legs_too`` false drops the legs from the search altogether. A bend
    with no side to move to stays."""
    out = _outward(prev, p, nxt)
    if out is None:
        return p
    sign, t0 = 1.0, None
    if not legs_first and clear(Point(p)):
        t0 = 0.0
    for legs in (True, False) if legs_too else (False,):
        for sgn in (1.0, -1.0):
            if t0 is not None:
                break

            def free(t: float, sgn: float = sgn, legs: bool = legs) -> bool:
                q = (p[0] + sgn * t * out[0], p[1] + sgn * t * out[1])
                return clear(Point(q)) and (
                    not legs
                    or (clear(LineString([prev, q])) and clear(LineString([q, nxt])))
                )

            t0 = _lift_t(free, limit=limit)
            if t0 is not None:
                sign = sgn
    if t0 is None:
        return p  # no travel clears it: an offset would be unchecked
    t = sign * (t0 + extra)
    q = (p[0] + t * out[0], p[1] + t * out[1])
    if not clear(Point(q)):
        return p
    if _turn_deg(prev, q, nxt) > max(_SHARP_TURN_DEG, _turn_deg(prev, p, nxt)):
        return p
    return q


def _emit_shapes(
    plan: RoutePlan,
    d: TopoDesign,
    ir: Any,
    n_layers: int,
    pose: np.ndarray,
    net_rules: Mapping[str, NetRules] | None,
    *,
    skip: Container[int] = (),
) -> RoutePlan:
    """The plan with every piece drawn as the copper it will commit
    (Reto 2026-11-01): any angle, few segments. Each piece's spoke-offset
    polyline is coalesced, a leg that grazes copper gets one kink at the
    graze (:func:`_kink`), and the result is pulled taut
    (:func:`_pull_taut`) against what it must clear — one
    :class:`CopperIndex` per layer holding the foreign pads (the DRC's
    pad set, :func:`_pad_polys`), keepout zones, the board-edge band and
    other nets' frozen vias, plus the CURRENT shape of every other piece
    on its layer. Pieces are drawn in order, each against its
    neighbours' latest shapes, so every accepted shortcut was checked
    against the final shape of every piece drawn before it and constrains
    every piece drawn after it. When the repairs add a hairpin the
    offsets' polyline did not have, the piece falls back to that polyline
    pulled taut — which can restore a grazing leg the kink had mended;
    the anti-short guard then catches it, except at the board edge. The
    guard and :func:`realize_result` both read these points, so the
    copper judged is the copper written. Pieces of a branch in ``skip`` (an unresolved
    crossing, never emitted) keep their shape and constrain nothing."""
    from shapely.ops import nearest_points

    rules = net_rules or {}
    net_id = {str(n): k for k, n in enumerate(d.net_name)}

    def nm(k: int) -> str:
        return str(d.net_name[k]) if 0 <= k < len(d.net_name) else str(k)

    def nrules(name: str) -> NetRules:
        return rules.get(name, _DEFAULT)

    # static obstacles per layer, as index bodies: a pad carries its net
    # and that net's clearance, a zone its net (none = foreign to all)
    bodies: list[list[Body]] = [[] for _ in range(n_layers)]
    for lay, pads in _pad_polys(d, ir, n_layers, pose).items():
        for pname, poly, _pin in pads:
            bodies[lay].append(
                Body(
                    poly,
                    0.0,
                    net_id.get(pname, ANY_NET) if pname else ANY_NET,
                    float(nrules(pname).clearance_mm) if pname else 0.0,
                )
            )
    for body, layers in _zone_bodies(d, pose, net_id, net_rules):
        for lay in layers or range(n_layers):
            if 0 <= int(lay) < n_layers:
                bodies[int(lay)].append(body)
    s_min = min(
        (float(r.clearance_mm) for r in rules.values()),
        default=float(_DEFAULT.clearance_mm),
    )
    outside = _outside_zone(d, s_min)
    if outside is not None and not outside.is_empty:
        for lay in range(n_layers):
            bodies[lay].append(Body(outside))
    index = [CopperIndex(b) for b in bodies]
    for v in plan.vias:
        if v.branch in skip:
            continue
        nrv = nrules(nm(v.net))
        rad = float(v.dia_mm or nrv.via_dia_mm or _VIA_DIA_MM) / 2.0
        for ix in index:
            ix.add_disc(v.x, v.y, rad, net=v.net, clearance=float(nrv.clearance_mm))
    reach = (
        max(
            (float(r.track_width_mm) for r in rules.values()),
            default=float(_DEFAULT.track_width_mm),
        )
        + max(s_min, max((float(r.clearance_mm) for r in rules.values()), default=0.0))
        + max(
            (float(r.via_dia_mm or _VIA_DIA_MM) for r in rules.values()),
            default=_VIA_DIA_MM,
        )
        + FAB_QUANTUM_MM
    )

    pieces = list(plan.pieces)
    live = [i for i, pc in enumerate(pieces) if pc.branch not in skip]
    lines = {
        i: LineString([(float(x), float(y)) for x, y in pieces[i].points]) for i in live
    }
    by_layer: dict[int, list[int]] = {}
    for i in live:
        by_layer.setdefault(pieces[i].layer, []).append(i)

    for i in live:
        pc = pieces[i]
        nri = nrules(nm(pc.net))
        wi, ci = float(nri.track_width_mm), float(nri.clearance_mm)
        lay = int(pc.layer)
        ix = index[lay] if 0 <= lay < len(index) else CopperIndex([])
        peers = [j for j in by_layer.get(lay, []) if pieces[j].net != pc.net]
        # (peer index, bounds, clearance need) snapshot — the same rows
        # every ``clear``/``offence`` call of this piece re-derived per
        # leg; peers' lines do not move while piece i is being drawn
        peer_rows: list[tuple[int, tuple[float, float, float, float], float]] = []
        for j in peers:
            nrj = nrules(nm(pieces[j].net))
            need = (
                (wi + float(nrj.track_width_mm)) / 2.0
                + max(ci, float(nrj.clearance_mm))
                + FAB_QUANTUM_MM
            )
            peer_rows.append((j, lines[j].bounds, need))

        def clear(
            seg: LineString,
            *,
            ix: CopperIndex = ix,
            wi: float = wi,
            ci: float = ci,
            peer_rows: list[
                tuple[int, tuple[float, float, float, float], float]
            ] = peer_rows,
            net: int = pc.net,
        ) -> bool:
            if ix.blocked(seg, wi / 2.0, ci, net=net):
                return False
            x0, y0, x1, y1 = seg.bounds
            for j, jb, need in peer_rows:
                if (
                    jb[0] > x1 + reach
                    or jb[2] < x0 - reach
                    or jb[1] > y1 + reach
                    or jb[3] < y0 - reach
                ):
                    continue
                if lines[j].distance(seg) < need - 1e-9:
                    return False
            return True

        def offence(
            a: tuple[float, float],
            b: tuple[float, float],
            *,
            exact: bool = True,
            ix: CopperIndex = ix,
            wi: float = wi,
            ci: float = ci,
            peer_rows: list[
                tuple[int, tuple[float, float, float, float], float]
            ] = peer_rows,
            net: int = pc.net,
        ) -> tuple[float, float]:
            """Where leg ``a → b`` presses on copper: the middle of the
            sample points the index (or a peer piece) blocks. When
            samples miss a narrow peer graze, use its exact nearest
            point. ``exact=False`` retains the sampled midpoint as an
            alternative when the exact point has no legal detour."""
            n = max(2, min(4000, int(math.dist(a, b) / _KINK_SAMPLE_MM) + 1))
            t = np.linspace(0.0, 1.0, n)[:, None]
            xy = np.asarray(a) + t * (np.asarray(b) - np.asarray(a))
            hit = ix.blocked_points(xy, wi / 2.0, ci, net=net)
            spts = shapely.points(xy)
            for j, _jb, need in peer_rows:
                hit |= shapely.distance(spts, lines[j]) < need
            where = np.flatnonzero(hit)
            if exact and not len(where):
                leg = LineString([a, b])
                for j, _jb, need in peer_rows:
                    if lines[j].distance(leg) < need:
                        point = nearest_points(leg, lines[j])[0]
                        return float(point.x), float(point.y)
            m = int(where[len(where) // 2]) if len(where) else n // 2
            return (float(xy[m, 0]), float(xy[m, 1]))

        base = _coalesce_micro(pc.points)
        pts = list(base)
        for j in range(1, len(pts) - 1):
            q = _lift_bend(
                pts[j - 1],
                pts[j],
                pts[j + 1],
                clear,
                legs_too=False,
                limit=max(1.0, 2.0 * (wi / 2.0 + ci)),
            )
            # a lift never carries the wire through a peer
            if q != pts[j] and not any(
                LineString([pts[j - 1], q]).crosses(lines[o])
                or LineString([q, pts[j + 1]]).crosses(lines[o])
                for o in peers
            ):
                pts[j] = q
        kinked = [pts[0]]
        for a, b in itertools.pairwise(pts):
            repair = _kink(a, b, clear, offence)
            if repair is None:
                repair = _kink(a, b, clear, functools.partial(offence, exact=False))
            kinked += repair or [b]
        shaped = _pull_taut(tuple(kinked), clear)
        if _sharp_turns(shaped) > _sharp_turns(base):
            # the repairs added a hairpin the offsets did not have: leave
            # the offsets' shape and pull that taut
            shaped = _pull_taut(base, clear)
        pieces[i] = dataclasses.replace(pc, points=shaped)
        lines[i] = LineString(shaped)
    return dataclasses.replace(plan, pieces=tuple(pieces))


def realize_result(
    plan: RoutePlan,
    d: TopoDesign,
    ir: Any,
    n_layers: int,
    *,
    net_rules: Mapping[str, NetRules] | None = None,
) -> Any:
    """The plan as a :class:`precis.pcb.realize.RealizeResult` — the one
    shape the store path, ``result_copper_rows`` and the DRC gate all
    read. ``ir`` maps the design's ``net_name`` back to ir net ids. Each
    piece's points are written verbatim, at any angle (Reto 2026-11-01).
    The route stage coalesces and shapes the pieces before validation
    (:func:`_emit_shapes`); repeating simplification here could erase a
    bend the anti-short guard validated and change its clearance."""
    from precis.pcb.realize import (
        RealizedTrack,
        RealizedVia,
        RealizeResult,
        through_span,
    )

    ir_net = {str(n): i for i, n in enumerate(ir.net_name)}
    tracks: list[RealizedTrack] = []
    vias: list[RealizedVia] = []
    rules = net_rules or {}

    def nm(k: int) -> str:
        return str(d.net_name[k]) if 0 <= k < len(d.net_name) else str(k)

    for p in plan.pieces:
        name = nm(p.net)
        nr = rules.get(name, _DEFAULT)
        pts = p.points
        segs = tuple(
            {
                "shape": "line",
                "start": [float(x0), float(y0)],
                "end": [float(x1), float(y1)],
            }
            for (x0, y0), (x1, y1) in itertools.pairwise(pts)
        )
        tracks.append(
            RealizedTrack(
                seg_id=p.branch,
                net_id=ir_net[name],
                layer=p.layer,
                segments=segs,
                length_mm=sum(
                    math.hypot(x1 - x0, y1 - y0)
                    for (x0, y0), (x1, y1) in itertools.pairwise(pts)
                ),
                blocked_by=None,
                width_mm=float(nr.track_width_mm),
            )
        )
    lo, hi = through_span(n_layers)
    for v in plan.vias:
        name = str(d.net_name[v.net]) if v.net < len(d.net_name) else str(v.net)
        nr = rules.get(name, _DEFAULT)
        vias.append(
            RealizedVia(
                seg_id=v.branch,
                net_id=ir_net.get(name, -1),
                x=v.x,
                y=v.y,
                # a fallback-sited barrel keeps its qualified
                # dia/drill pair (gr524334) — 0.0 means the net rule's own
                dia_mm=float(v.dia_mm or nr.via_dia_mm or _VIA_DIA_MM),
                drill_mm=float(v.drill_mm or nr.via_drill_mm or _VIA_DRILL_MM),
                layer_lo=lo,
                layer_hi=hi,
                endpoint="a",
            )
        )
    return RealizeResult(tuple(tracks), tuple(vias), ())


@dataclass(frozen=True, slots=True)
class _PadBody(Body):
    """An exact pad envelope plus its optional circular or slotted void.

    Route obstacles use the filled envelope; a pour must touch copper
    outside the hole to prove electrical contact.
    """

    hole: Body | None = None


def _pad_hole(raw: dict[str, Any], pose_row: np.ndarray) -> Body | None:
    """A router hole uses the same placed centre and slot ends as export."""
    from precis.pcb.padplace import place_pad_drill, place_pad_point

    inst = {
        "x": float(pose_row[0]),
        "y": float(pose_row[1]),
        "rot": 90.0 * int(pose_row[2]),
        "layer": "bottom" if pose_row[3] else "top",
    }
    bx, by = place_pad_point(raw, inst)
    hole = place_pad_drill(raw, inst, bx, by)
    if hole is None:
        return None
    core = LineString(hole["slot"]) if hole.get("slot") else Point(hole["x"], hole["y"])
    return Body(core, r=float(hole["dia_mm"]) / 2.0)


def _pad_hole_bodies(d: TopoDesign, ir: Any, pose: np.ndarray) -> list[Body]:
    """Physical pad drills bar new vias even beyond a land's open slot ends.

    Copper envelopes still serve routing clearance and mask web. Drilling
    additionally needs the actual holes, including non-primary pad holes;
    these have no own-net exemption and retain the fabrication wall.
    """
    out: list[Body] = []
    for pad in d.footprint_pads or getattr(ir, "footprint_pads", ()):
        part = int(pad.instance)
        if part >= len(pose) or not np.isfinite(pose[part, :2]).all():
            continue
        hole = _pad_hole(pad.raw, pose[part])
        if hole is not None:
            out.append(dataclasses.replace(hole, clearance=JLC_VIA_HOLE_TO_HOLE_MM))
    return out


def _pad_bodies(
    d: TopoDesign, ir: Any, n_layers: int, pose: np.ndarray, *, exact: bool = True
) -> dict[int, list[tuple[str, Body, int]]]:
    """World-frame pad geometry per layer, tagged ``(net name, geom,
    pin id)`` — the SAME pad set the DRC flashes: each pin's pad plus
    every non-primary footprint land (``pad_extra_*``: a pin's second
    same-named pad, a split slug half, an unpinned land — gr524239; the
    pin-only roster passed a track through copper the checker then
    errored on). ``NO_NET``/unpinned lands carry the empty name (foreign
    to every net) and pin ``-1``. SMD pads live on their mount side, TH
    on all layers.

    A ``'circle'`` pad is its smaller-dimension disk, an ``'obround'``
    is a segment swept by its smaller half-size, and an authored polygon
    keeps its ring. Raw footprint dimensions and pad rotation supersede
    the extent arrays, which over-cover oblique pads. A rounded or custom
    outline must carry an authored polygon ring; the native shape format
    has no rounded-rectangle radius. Unresolved polygons and unknown
    shapes supply no connectivity proof. ``exact=False`` preserves their
    conservative obstacle envelope from the extent arrays (disk or
    rectangle), including the established obround bounding boxes, so
    exact contact checks do not change the pre-route obstacle roster.
    The same resolved set serves both the
    foreign-copper guard and the emit-join check (a corner touch on a
    round land is not a join — gr537831 review). A disk stays an exact
    point swept by its radius: connectivity must not mistake an outward
    polygon's excess for a copper join."""
    from precis.pcb.ir import NO_NET

    # The IR extent arrays intentionally over-cover oblique pads. Read
    # their original dimensions and rotation when the raw pad set exists;
    # a rotated bounding box is an obstacle, never proof of copper contact.
    raw_primary: dict[int, Any] = {}
    raw_extra: dict[tuple[int, int, float, float], Any] = {}
    for pad in (d.footprint_pads or getattr(ir, "footprint_pads", ())) if exact else ():
        if pad.primary:
            raw_primary[int(pad.pin)] = pad.raw
        else:
            raw_extra[
                (
                    int(pad.instance),
                    int(pad.pin),
                    float(pad.raw["x"]),
                    float(pad.raw["y"]),
                )
            ] = pad.raw

    pad_bodies: dict[int, list[tuple[str, Body, int]]] = {
        i: [] for i in range(n_layers)
    }

    def add_pad(
        part: int,
        px: float,
        py: float,
        hw: float,
        hh: float,
        pname: str,
        side: int,
        pin: int,
        shape: str,
        ring: Any,
        raw: Any = None,
    ) -> None:
        if part >= len(pose) or not np.isfinite(pose[part, :2]).all():
            return
        angle = 0.0
        if not exact:
            raw = None
        if raw is not None:
            px, py = float(raw["x"]), float(raw["y"])
            shape = str(raw.get("shape") or "rect")
            angle = math.radians(float(raw.get("rot") or 0.0))
            ring = (
                np.asarray(raw["poly"], dtype=float)
                - np.asarray([float(raw["x"]), float(raw["y"])])
                if raw.get("poly")
                else None
            )
            if ring is not None and len(ring) >= 3:
                hw, hh = (float(v) / 2.0 for v in np.ptp(ring, axis=0))
            else:
                hw = float(raw.get("w", 2.0 * hw)) / 2.0
                hh = float(raw.get("h", raw.get("w", 2.0 * hh))) / 2.0
        if exact:
            shape = {"ellipse": "circle", "oval": "obround"}.get(
                shape.lower(), shape.lower()
            )
        if hw <= 0.0 or hh <= 0.0:
            return

        def world(pts: np.ndarray) -> np.ndarray:
            if angle:
                c, s = math.cos(angle), math.sin(angle)
                center = np.asarray([px, py])
                pts = (pts - center) @ np.asarray([[c, -s], [s, c]]) + center
            return topo_proxy.local_to_world(pts, pose[part])

        if ring is not None and len(ring) >= 3:
            pts = np.asarray(ring, dtype=np.float64) + np.asarray([px, py])
            # Authored polygon coordinates already include the pad's own
            # rotation, matching padplace.place_footprint_pads.
            body = Body(
                Polygon(np.asarray(topo_proxy.local_to_world(pts, pose[part]), float))
            )
        elif shape == "circle" or (exact and shape == "obround" and hw == hh):
            (cx, cy) = topo_proxy.local_to_world(
                np.asarray([[px, py]], dtype=float), pose[part]
            )[0]
            body = Body(Point(float(cx), float(cy)), r=min(hw, hh))
        elif exact and shape == "obround":
            half = abs(hw - hh)
            pts = np.asarray(
                [[px - half, py], [px + half, py]]
                if hw > hh
                else [[px, py - half], [px, py + half]]
            )
            body = Body(LineString(world(pts)), r=min(hw, hh))
        elif shape == "polygon":
            if exact:
                return
            # Missing geometry may still block a route, but it cannot
            # prove a copper join.
            (cx, cy) = topo_proxy.local_to_world(
                np.asarray([[px, py]], dtype=float), pose[part]
            )[0]
            body = Body(Point(float(cx), float(cy)), r=math.hypot(hw, hh))
        else:
            if exact and shape not in ("", "rect"):
                return
            corners = np.asarray(
                [
                    [px - hw, py - hh],
                    [px + hw, py - hh],
                    [px + hw, py + hh],
                    [px - hw, py + hh],
                ]
            )
            body = Body(Polygon(np.asarray(world(corners), float)))
        if body.geom.is_empty or (body.r <= 0.0 and body.geom.area <= 0.0):
            return
        if raw is not None and raw.get("drill"):
            hole = _pad_hole(raw, pose[part])
            body = _PadBody(body.geom, body.r, body.net, body.clearance, hole)
        layers: Iterable[int] = (
            range(n_layers) if side == 2 else (0,) if side == 0 else (n_layers - 1,)
        )
        for lay in layers:
            pad_bodies[lay].append((pname, body, pin))

    for p in range(len(d.pin_xy)):
        pnet = int(d.pin_net[p])
        pname = "" if pnet == int(NO_NET) else str(ir.net_name[pnet])
        add_pad(
            int(d.pin_part[p]),
            float(d.pin_xy[p][0]),
            float(d.pin_xy[p][1]),
            float(d.pin_wh[p, 0]) / 2.0 if len(d.pin_wh) > p else 0.0,
            float(d.pin_wh[p, 1]) / 2.0 if len(d.pin_wh) > p else 0.0,
            pname,
            int(d.pin_side[p]),
            p,
            str(d.pin_shape[p]) if p < len(d.pin_shape) else "",
            d.pin_poly[p] if p < len(d.pin_poly) else None,
            raw_primary.get(p),
        )
    for k in range(len(d.pad_extra_xy)):
        xpin = int(d.pad_extra_pin[k])
        pnet = int(d.pin_net[xpin]) if 0 <= xpin < len(d.pin_net) else int(NO_NET)
        pname = "" if pnet == int(NO_NET) else str(ir.net_name[pnet])
        add_pad(
            int(d.pad_extra_part[k]),
            float(d.pad_extra_xy[k, 0]),
            float(d.pad_extra_xy[k, 1]),
            float(d.pad_extra_wh[k, 0]) / 2.0,
            float(d.pad_extra_wh[k, 1]) / 2.0,
            pname,
            int(d.pad_extra_side[k]),
            xpin,
            str(d.pad_extra_shape[k]) if k < len(d.pad_extra_shape) else "",
            d.pad_extra_poly[k] if k < len(d.pad_extra_poly) else None,
            raw_extra.get(
                (
                    int(d.pad_extra_part[k]),
                    xpin,
                    float(d.pad_extra_xy[k, 0]),
                    float(d.pad_extra_xy[k, 1]),
                )
            ),
        )
    return pad_bodies


def _pad_polys(
    d: TopoDesign, ir: Any, n_layers: int, pose: np.ndarray
) -> dict[int, list[tuple[str, Any, int]]]:
    """The pad bodies as clearance obstacles, enclosing their true copper.

    Circle facets are circumscribed within the kernel's fine sag bound.
    Connectivity reads :func:`_pad_bodies` instead: excess outside a pad
    cannot certify a copper join. Shared through-pad polygons stay shared
    across layers, so the via planter can deduplicate them."""
    from precis.pcb.clearance import outward_circle

    polygons: dict[int, Any] = {}
    out: dict[int, list[tuple[str, Any, int]]] = {}
    for layer, bodies in _pad_bodies(d, ir, n_layers, pose, exact=False).items():
        out[layer] = []
        for name, body, pin in bodies:
            key = id(body)
            if key not in polygons:
                if isinstance(body.geom, Point):
                    polygons[key] = outward_circle(body.geom.x, body.geom.y, body.r)
                else:
                    polygons[key] = body.geom
            out[layer].append((name, polygons[key], pin))
    return out


def _plane_copper_indices(
    d: TopoDesign,
    ir: Any,
    pose: np.ndarray,
    rules: Mapping[str, NetRules],
    tracks: Sequence[Any],
    vias: Sequence[Any],
) -> list[CopperIndex]:
    """Whole-copper oracle for plane stubs and barrels, one index per layer.

    Every foreign body retains its clearance. The outward geometry is
    shared with signal routing, including authored arcs; exact pad bodies
    remain reserved for connectivity. Same-net copper may join here;
    drill spacing and the own-pad mask web belong to the via planter.
    """
    from precis.pcb import clearance as pcb_clearance
    from precis.pcb import realize as pcb_realize

    net_ids = {str(n): k for k, n in enumerate(d.net_name)}
    n_layers = int(ir.n_layers)
    bodies: list[list[Body]] = [[] for _ in range(n_layers)]
    for body, layers in _zone_bodies(d, pose, net_ids, rules):
        for lay in layers or range(n_layers):
            bodies[int(lay)].append(body)
    layer_names = [str(layer.get("name") or "") for layer in ir.stackup]
    copper = pcb_realize.RealizeResult(tuple(tracks), (), ())
    for row in pcb_realize.result_copper_rows(copper, ir, layer_names, derived=True):
        raw = pcb_clearance.body_of(dict(row))
        if raw is None:
            continue
        name = str(row.get("net") or "")
        if raw.arcs or raw.core is None:
            geom = pcb_clearance.outward_polygon(raw)
            radius = 0.0
        else:
            geom, radius = raw.core, raw.r
        bodies[layer_names.index(str(row["layer"]))].append(
            Body(
                geom,
                radius,
                net_ids.get(name, ANY_NET),
                float(rules.get(name, _DEFAULT).clearance_mm) if name else 0.0,
            )
        )
    for via in vias:
        name = str(ir.net_name[via.net_id])
        body = Body(
            Point(via.x, via.y),
            via.dia_mm / 2.0,
            net_ids.get(name, ANY_NET),
            float(rules.get(name, _DEFAULT).clearance_mm),
        )
        for lay in range(via.layer_lo, via.layer_hi + 1):
            bodies[lay].append(body)
    for lay, pads in _pad_polys(d, ir, n_layers, pose).items():
        for name, geom, _pin in pads:
            bodies[lay].append(
                Body(
                    geom,
                    net=net_ids.get(name, ANY_NET),
                    clearance=float(rules.get(name, _DEFAULT).clearance_mm)
                    if name
                    else 0.0,
                )
            )
    return [CopperIndex(rows) for rows in bodies]


def _guard_fanouts(
    d: TopoDesign,
    ir: Any,
    pose: np.ndarray,
    rules: Mapping[str, NetRules],
    routed: Any,
    tracks: Sequence[Any],
    vias: Sequence[Any],
) -> set[int]:
    """Fanout pairs that cannot emit against the complete signal/fanout set.

    Signal guards run before fanout copper is appended. Recheck every
    plane net here so no ordering or signal-guard omission can emit a
    short. The caller defers each rejected pair's pad to the escape pass.
    """
    indices = _plane_copper_indices(
        d,
        ir,
        pose,
        rules,
        tuple(routed.tracks) + tuple(tracks),
        tuple(routed.vias) + tuple(vias),
    )
    net_ids = {str(n): k for k, n in enumerate(d.net_name)}
    bad: set[int] = set()
    board = Polygon(np.asarray(d.outline, dtype=float)) if len(d.outline) >= 3 else None
    boundary = board.boundary if board is not None else None
    for i, (track, via) in enumerate(zip(tracks, vias, strict=True)):
        name = str(ir.net_name[track.net_id])
        k = net_ids[name]
        c = float(rules.get(name, _DEFAULT).clearance_mm)
        line = LineString(
            [tuple(s["start"]) for s in track.segments]
            + [tuple(track.segments[-1]["end"])]
        )
        edge = max(float(d.edge_clearance_mm or 0.0), c) + FAB_QUANTUM_MM
        if board is not None and boundary is not None:
            barrel = Point(via.x, via.y)
            if (
                not board.covers(line)
                or line.distance(boundary) < track.width_mm / 2.0 + edge - 1e-9
                or not board.covers(barrel)
                or barrel.distance(boundary) < via.dia_mm / 2.0 + edge - 1e-9
            ):
                bad.add(i)
                continue
        if indices[track.layer].blocked(line, track.width_mm / 2.0, c, net=k) or any(
            indices[lay].blocked(Point(via.x, via.y), via.dia_mm / 2.0, c, net=k)
            for lay in range(via.layer_lo, via.layer_hi + 1)
        ):
            bad.add(i)
    return bad


def _guarded_nets(
    emit: RoutePlan,
    d: TopoDesign,
    ir: Any,
    n_layers: int,
    pose: np.ndarray,
    net_rules: Mapping[str, NetRules] | None,
) -> set[int]:
    """Net-level view of :func:`_guarded_branches` — the nets with at
    least one short-risk branch."""
    return set(_guarded_branches(emit, d, ir, n_layers, pose, net_rules))


def _guarded_branches(
    emit: RoutePlan,
    d: TopoDesign,
    ir: Any,
    n_layers: int,
    pose: np.ndarray,
    net_rules: Mapping[str, NetRules] | None,
) -> dict[int, set[int]]:
    """The anti-short guard (gr524239): net indices of the emit set whose
    copper would violate foreign clearance — a track over a foreign pad,
    two foreign tracks closer than their widths plus clearance, a via
    barrel crowding foreign copper. The order-based crossing detector
    misses these (a taut polyline may cross a foreign pad without an
    order inversion on shared edges), so the emit set is checked
    geometrically before it may be realized. ``NO_NET`` pads are foreign
    to every net — the baseline run's shorts were tracks over unwired
    pads. A violating net is withdrawn WHOLE (no orphan fragments) and
    recorded on :attr:`RoutePlan.guarded`. Centerline-vs-centerline
    distances against both nets' resolved widths and clearances — the
    same figures the DRC gate measures, so a pass here is a pass there.

    Per BRANCH since 2026-10-31 (Reto: "C14, C4 GND segment could be
    routed" — GND's 35-branch tree was withdrawn whole for one shorting
    branch): returns ``{net: {branch, ...}}``; the caller drops the
    offending branches' pieces and vias, the emit-connectivity check
    then trims what dangles, and the copper that joins pads stays. The
    net still reports ``failed: short-risk``."""
    from shapely.geometry import LineString

    rules = net_rules or {}
    names = [str(n) for n in d.net_name]

    def nm(k: int) -> str:
        return names[k] if 0 <= k < len(names) else str(k)

    def nrules(name: str) -> NetRules:
        return rules.get(name, _DEFAULT)

    pad_polys = _pad_polys(d, ir, n_layers, pose)
    # the emitted copper itself: the route stage drew the pieces
    # (_emit_shapes) before this guard runs, so judged == written
    lines = {
        i: LineString([(float(x), float(y)) for x, y in pc.points])
        for i, pc in enumerate(emit.pieces)
    }
    by_layer: dict[int, list[int]] = {}
    for i, pc in enumerate(emit.pieces):
        by_layer.setdefault(pc.layer, []).append(i)

    guarded: dict[int, set[int]] = {}

    def mark(net: int, branch: int) -> None:
        guarded.setdefault(int(net), set()).add(int(branch))

    def marked(net: int, branch: int) -> bool:
        return int(branch) in guarded.get(int(net), ())

    # frozen copper (route_design's re-route rounds: the survivors'
    # tracks and vias as net-tagged fixed_copper zones) — foreign to
    # every other net on its layers; a via zone spans all of them
    from precis.pcb.topoplace.proxy import zone_world_ring

    frozen: list[tuple[Polygon, tuple[int, ...], str]] = []
    keepouts: list[tuple[Polygon, tuple[int, ...], str]] = []
    for z in d.zones:
        if z.kind == "label_keepout":
            continue  # silk over traces: via-only
        ring = zone_world_ring(d, z, pose)
        if len(ring) >= 3:
            (frozen if z.kind in ("fixed_copper", "fixed_via") else keepouts).append(
                (Polygon(np.asarray(ring)), tuple(z.layers), str(z.net or ""))
            )
    # copper inside a keepout is a DRC error (keepout_copper) — the
    # taut legs of a part placed off the outline crossed one
    for i, pc in enumerate(emit.pieces):
        half = float(nrules(nm(pc.net)).track_width_mm) / 2.0
        for zp, zl, _zn in keepouts:
            if zl and pc.layer not in zl:
                continue
            if lines[i].distance(zp) < half + FAB_QUANTUM_MM:
                mark(pc.net, pc.branch)
                break
    if frozen:
        for i, pc in enumerate(emit.pieces):
            nri = nrules(nm(pc.net))
            wi, ci = float(nri.track_width_mm), float(nri.clearance_mm)
            for zp, zl, zn in frozen:
                if zn == nm(pc.net) or (zl and pc.layer not in zl):
                    continue
                cz = float(nrules(zn).clearance_mm) if zn else 0.0
                if lines[i].distance(zp) < wi / 2.0 + max(ci, cz) + FAB_QUANTUM_MM:
                    mark(pc.net, pc.branch)
                    break
        for v in emit.vias:
            nrv = nrules(nm(v.net))
            dia = float(v.dia_mm or nrv.via_dia_mm or _VIA_DIA_MM)
            pt = Point(v.x, v.y)
            for zp, _zl, zn in frozen:
                cz = float(nrules(zn).clearance_mm) if zn else 0.0
                if zn != nm(v.net) and zp.distance(pt) < (
                    dia / 2.0 + max(float(nrv.clearance_mm), cz) + FAB_QUANTUM_MM
                ):
                    mark(v.net, v.branch)
                    break

    for lay, idxs in by_layer.items():
        for i in idxs:
            pi = emit.pieces[i]
            if marked(pi.net, pi.branch):
                continue
            nri = nrules(nm(pi.net))
            wi, ci = float(nri.track_width_mm), float(nri.clearance_mm)
            for pname, poly, _pin in pad_polys.get(lay, []):
                if pname and pname == nm(pi.net):
                    continue  # own-net pads are where terminals land
                cp = float(nrules(pname).clearance_mm) if pname else ci
                if lines[i].distance(poly) < wi / 2.0 + max(ci, cp) + FAB_QUANTUM_MM:
                    mark(pi.net, pi.branch)
                    break
            if marked(pi.net, pi.branch):
                continue
            for j in idxs:
                if j <= i:
                    continue
                pj = emit.pieces[j]
                if pj.net == pi.net or marked(pj.net, pj.branch):
                    continue
                nrj = nrules(nm(pj.net))
                gap = (
                    (wi + float(nrj.track_width_mm)) / 2.0
                    + max(ci, float(nrj.clearance_mm))
                    + FAB_QUANTUM_MM
                )
                if lines[i].distance(lines[j]) < gap:
                    mark(pi.net, pi.branch)
                    mark(pj.net, pj.branch)
    # Two emitted barrels also owe the larger net rule. Connecting tracks
    # can point away from the pair and clear both barrels while the
    # wider barrels themselves violate clearance.
    for i, v in enumerate(emit.vias):
        nrv = nrules(nm(v.net))
        rv = float(v.dia_mm or nrv.via_dia_mm or _VIA_DIA_MM) / 2.0
        for other in emit.vias[i + 1 :]:
            nro = nrules(nm(other.net))
            distance = math.hypot(v.x - other.x, v.y - other.y)
            drill = float(v.drill_mm or nrv.via_drill_mm or _VIA_DRILL_MM)
            other_drill = float(other.drill_mm or nro.via_drill_mm or _VIA_DRILL_MM)
            wall = JLC_VIA_HOLE_TO_HOLE_MM if other.net == v.net else 0.0
            if distance < (drill + other_drill) / 2.0 + wall + FAB_QUANTUM_MM:
                mark(v.net, v.branch)
                mark(other.net, other.branch)
            if other.net == v.net:
                continue
            ro = float(other.dia_mm or nro.via_dia_mm or _VIA_DIA_MM) / 2.0
            clearance = max(float(nrv.clearance_mm), float(nro.clearance_mm))
            if distance < rv + ro + clearance + FAB_QUANTUM_MM:
                mark(v.net, v.branch)
                mark(other.net, other.branch)

    # A via barrel spans every layer: vs pads and foreign pieces. Every
    # pad holds the soldermask web (:attr:`TopoDesign.via_mask_web_mm` —
    # the via's own pads included, Reto 2026-11-03); a foreign pad's
    # copper clearance still applies where it is larger.
    web = float(d.via_mask_web_mm)
    holes = _pad_hole_bodies(d, ir, pose)
    for v in emit.vias:
        if marked(v.net, v.branch):
            continue
        nrv = nrules(nm(v.net))
        dia, cv = (
            float(v.dia_mm or nrv.via_dia_mm or _VIA_DIA_MM),
            float(nrv.clearance_mm),
        )
        pt = Point(v.x, v.y)
        drill = float(v.drill_mm or nrv.via_drill_mm or _VIA_DRILL_MM)
        if any(
            pt.distance(hole.geom)
            < drill / 2.0 + hole.r + hole.clearance + FAB_QUANTUM_MM
            for hole in holes
        ):
            mark(v.net, v.branch)
            continue
        hit = False
        for lay in range(n_layers):
            for pname, poly, _pin in pad_polys[lay]:
                if pname and pname == nm(v.net):
                    if poly.distance(pt) < dia / 2.0 + web + FAB_QUANTUM_MM:
                        hit = True
                        break
                    continue
                cp = float(nrules(pname).clearance_mm) if pname else cv
                if poly.distance(pt) < dia / 2.0 + max(cv, cp, web) + FAB_QUANTUM_MM:
                    hit = True
                    break
            if hit:
                break
            for j in by_layer.get(lay, []):
                pj = emit.pieces[j]
                if pj.net == v.net:
                    continue
                nrj = nrules(nm(pj.net))
                cj = float(nrj.clearance_mm)
                # the piece is a centerline — its own half-width is part
                # of the gap: barrel edge to wire edge keeps the
                # clearance, like the CopperIndex's r + r_b + max(c,c_b)
                if lines[j].distance(pt) < (
                    dia / 2.0
                    + float(nrj.track_width_mm) / 2.0
                    + max(cv, cj)
                    + FAB_QUANTUM_MM
                ):
                    hit = True
                    break
            if hit:
                break
        if hit:
            mark(v.net, v.branch)
    return guarded


#: Same-layer touch slack for the emit-connectivity check — the pieces'
#: join points are constructed coincident (a cut point is appended to both
#: runs, a branch end sits inside its pad), so a nanometre of fp slack is
#: all this needs; a real gap must never be rescued by it.
_SEVER_EPS_MM = 1e-6


def _severed_nets(
    emit: RoutePlan,
    d: TopoDesign,
    ir: Any,
    n_layers: int,
    pose: np.ndarray,
    net_rules: Mapping[str, NetRules] | None,
) -> dict[int, tuple[frozenset[int], frozenset[int]]]:
    """The emit-connectivity check (gr537831): net indices whose committed
    copper — pieces, vias and the pads they must reach — is not ONE
    connected piece. The crossing detector and the anti-short guard ask
    foreign-copper questions; nothing asked whether a net's OWN copper is
    whole, so an edgeless F↔B hop could end at the far pad's coordinates
    on the wrong layer and still read ``realized`` (s5's BOOT). A severed
    net is withdrawn whole like ``guarded``: committed copper that cannot
    connect is worse than an honest failure. Joins are geometric, not
    by-construction: a piece touches a same-layer piece, a pad on its
    layer or a via barrel (spans every layer) within its swept radius —
    the same gap arithmetic :mod:`precis.pcb.connectivity` judges the
    committed board by, over the exact pad shapes :func:`_pad_bodies`
    flashes (a corner touch on a round land is not a join).

    Review fix: withdrawal is per-component — a component joining at
    least two of the net's pads is real copper and STAYS (a stranded net
    keeps the branches it did route); only components joining fewer than
    two pads — floating or dangling copper — are dropped. A net whose
    every component is broken loses its copper whole, as before. Returns
    ``{net: (drop piece indices, drop via indices)}`` covering every
    severed net so ``plan.severed`` still reports them all —
    ``sketch_rows`` writes ``unrouted`` and ``unconnected`` as
    appropriate."""
    rules = net_rules or {}
    names = [str(n) for n in d.net_name]

    def nm(k: int) -> str:
        return names[k] if 0 <= k < len(names) else str(k)

    def nrules(name: str) -> NetRules:
        return rules.get(name, _DEFAULT)

    pad_bodies = _pad_bodies(d, ir, n_layers, pose)
    lines = {
        i: LineString([(float(x), float(y)) for x, y in pc.points])
        for i, pc in enumerate(emit.pieces)
    }
    severed: dict[int, tuple[frozenset[int], frozenset[int]]] = {}
    for net in {pc.net for pc in emit.pieces}:
        nrm = nrules(nm(net))
        wi = float(nrm.track_width_mm)
        # members: (layer set, geometry, reach, pin key, kind, emit index)
        # — pads of one pin union internally (the part joins its own
        # lands), which the pin key asserts like connectivity's
        # _union_same_pin_pads; the kind/index pair is the drop record
        # ("piece"/"via" is committed copper, "pad" a terminal).
        members: list[tuple[frozenset[int], Any, float, int, str, int]] = []
        for i, pc in enumerate(emit.pieces):
            if pc.net == net:
                members.append(
                    (frozenset({pc.layer}), lines[i], wi / 2.0, -1, "piece", i)
                )
        for vi, v in enumerate(emit.vias):
            if v.net == net:
                dia = float(v.dia_mm or nrm.via_dia_mm or _VIA_DIA_MM)
                members.append(
                    (
                        frozenset(range(n_layers)),
                        Point(v.x, v.y),
                        dia / 2.0,
                        -1,
                        "via",
                        vi,
                    )
                )
        own = nm(net)
        for lay, entries in pad_bodies.items():
            for pname, body, pin in entries:
                if pname == own:
                    members.append(
                        (frozenset({lay}), body.geom, body.r, pin, "pad", -1)
                    )
        parent = list(range(len(members)))

        def find(a: int, parent: list[int] = parent) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for a in range(len(members)):
            la, ga, ra, ka_, _knd_a, _i_a = members[a]
            for b in range(a + 1, len(members)):
                lb, gb, rb, kb_, _knd_b, _i_b = members[b]
                joined = (ka_ >= 0 and ka_ == kb_) or (
                    bool(la & lb) and ga.distance(gb) <= ra + rb + _SEVER_EPS_MM
                )
                if joined:
                    ra_, rb_ = find(a), find(b)
                    if ra_ != rb_:
                        parent[rb_] = ra_
        comps: dict[int, tuple[set[int], set[int], set[int]]] = {}
        for i, (_l, _g, _r, k, kind, idx) in enumerate(members):
            pins_, pieces_, vias_ = comps.setdefault(find(i), (set(), set(), set()))
            if kind == "pad":
                if k >= 0:
                    pins_.add(k)
            elif kind == "piece":
                pieces_.add(idx)
            else:
                vias_.add(idx)
        if len(comps) <= 1:
            continue
        drop_p: set[int] = set()
        drop_v: set[int] = set()
        for pins_, pieces_, vias_ in comps.values():
            if len(pins_) < 2:
                drop_p |= pieces_
                drop_v |= vias_
        severed[net] = (frozenset(drop_p), frozenset(drop_v))
    return severed


#: Fanout via search around a plane net's SMD pad (gr524239): ring radii
#: from the pad centre (mm), nearest first, and directions per ring. The
#: stub stays short so it is a pad escape, never a wire to dodge.
_FANOUT_RADII_MM = (0.45, 0.6, 0.75, 0.9, 1.1, 1.3, 1.6, 2.0)
_FANOUT_DIRECTIONS = 16
#: How far PAST the ring clearance the preferred ladder reaches (mm):
#: every sited stub is frozen copper the corridor LP must fit, and the
#: full 2.0 mm budget spent past the band overflowed a corridor on x2
#: (review round 2). Measured on the taz gauges at the current-rated
#: barrel: 2.0 drops local-61's corridor-bound orders ~4 nets, 1.1 and
#: 1.6 land even overall — 1.1 keeps the shortest frozen stubs where
#: corridors bind. Past the cap the pad takes the fallback rather than
#: a longer frozen wire.
_FANOUT_LADDER_CAP_MM = 1.1

#: The longest wire a pad may take to its fanout via (coordinator
#: ruling, G2d round 5 — 5.0 in round 3): a stub needing more is no
#: fanout — the pad is the named finding, with the length it would
#: have needed on ``RoutePlan.fanout_missing_len``.
_ESCAPE_LEN_CAP_MM = 10.0


def _plane_nets(d: TopoDesign, ir: Any) -> dict[int, int]:
    """``{net_pins index: plane layer}`` for every routed net the board
    pours (``ir.net_plane_layers``, the authored ``plane_net``), lowest
    poured layer when a net pours several."""
    masks = getattr(ir, "net_plane_layers", None)
    if masks is None:
        return {}
    ir_of = {str(n): i for i, n in enumerate(ir.net_name)}
    out: dict[int, int] = {}
    for k, name in enumerate(d.net_name):
        i = ir_of.get(str(name))
        if i is None or k >= len(d.net_pins) or not len(d.net_pins[k]):
            continue
        mask = int(masks[i])
        layers = [lay for lay in range(int(ir.n_layers)) if mask >> lay & 1]
        if layers:
            out[k] = layers[0]
    return out


def _fanout_vias(
    d: TopoDesign,
    ir: Any,
    n_layers: int,
    pose: np.ndarray,
    net_rules: Mapping[str, NetRules] | None,
    plane_nets: Mapping[int, int],
    *,
    min_via_dia: float | None = None,
    min_via_drill: float | None = None,
) -> tuple[
    list[Any],
    list[Any],
    list[tuple[int, int]],
    list[tuple[int, int]],
]:
    """One via per SMD pad of every plane net, joined by a stub on the
    pad's layer (gr524239: "plane nets drop to the plane by via, not
    routed on F.Cu"). A drilled pad already reaches the plane and needs
    none. Returns ``(tracks, vias, fell_back, deferred)``: the stubs,
    the vias, ``(net, pin)`` for each pad only the ring-agnostic
    fallback (or the pad-scaled rung) could site, and ``(net, pin)``
    for each pad no site serves — those wire out on their own layer
    after the signal route (:func:`_emit_escapes`, G2d).

    A site is legal when the barrel — it spans every layer — keeps its
    keep-out from every pad (the fanned pad included: no via-in-pad),
    from every zone a via may not sit in (keepouts, label boxes, mounting
    holes, other nets' frozen copper), from every via already placed, and
    stays edge clearance plus its radius inside the outline; the stub
    keeps half its width plus clearance from every pad of another net on
    its layer. ``net_rules`` is the ROUTE-CHOICE map
    (:func:`_via_choice_rules` — the current-rated barrel the signal
    route sites: a net with no current figure gets the fab floor, which
    also shrinks the frozen stub's footprint); the fab-minimum barrel
    (gr524334) is tried only where the choice finds no site.

    Two passes per pad, silk-aware (Reto 2026-11-02, taz-leaf 2x: fanout
    vias sat 0-0.2 mm off their part's outline ring, and the silk pass
    bent every ring around them — "end these weird shared outlines";
    the ring now opens a gap at each in-band via instead):

    - **preferred**: sites at least ``outline_reach_mm`` (the furthest
      :func:`precis.pcb.silk.part_outlines`' ring can sit off the pad
      hull — the ``outline_target_mm`` offset plus the shared-wall
      extension) + half a stroke + ``via_to_silk_mm`` + the barrel's
      radius off EVERY part's pad hull — the same hulls the silk pass
      offsets (``d.part_body``, ``instance_courtyard_polygons`` at zero
      clearance, convex-hulled at this pose). A site that far out can
      never reach a drawn ring's legal clearance, own part or
      neighbour's, so the outline stays straight. The radii run from
      that distance out to it plus ``_FANOUT_LADDER_CAP_MM``: a stub
      sited past the band is still frozen copper the corridor LP must
      fit, so the preferred search keeps a 1.1 mm escape budget where
      the fallback allows 2.0 mm from the pad centre.
    - **fallback**: the original search — ``_FANOUT_RADII_MM`` from the
      pad centre, nearest first, no ring condition — run only when no
      preferred site exists. A ring opened at the via beats a plane pad
      with no via (measured 2026-11-02: every fallback pad is a GND pin
      with no other route to the plane); the pads it sites land on
      ``fell_back``.

    G2d changes two ends of that contract. The fallback radii scale
    with the pad: after the fixed ladder the search goes on at
    ``own_min + r`` — the ring just past the pad's own keep-out (its
    circumradius plus the barrel radius plus the larger of the
    clearance and the soldermask web) — so a pad wider than the fanout
    reach (a thermal slug) still finds a site off its own copper
    WITHOUT via-in-pad (Reto ruling, the G2h follow-on). And a pad no
    ring site serves is not yet ``missing``: it is deferred to a routed
    two-pin branch on its own layer to the nearest point the search's
    via planter (:class:`~precis.pcb.topoplace.wires._ViaPlanter` —
    pads, the soldermask web, the outline margin and every via-barred
    zone, the soldermask-web own-pad bar included) admits, sited by
    :func:`~precis.pcb.topoplace.wires.fanout_escape`. Only a pad the
    escape cannot reach a plantable point from lands on ``missing`` —
    :func:`_emit_escapes` names that finding, not this pass.
    """
    from shapely import STRtree
    from shapely.geometry import MultiPoint, box
    from shapely.ops import unary_union

    from precis.pcb.gerber import DEFAULT_SILK_WIDTH_MM
    from precis.pcb.ir import NO_NET
    from precis.pcb.realize import RealizedTrack, RealizedVia, through_span
    from precis.pcb.topoplace.proxy import local_to_world, zone_world_ring

    rules = net_rules or {}
    ir_net = {str(n): i for i, n in enumerate(ir.net_name)}
    # The via keeps the soldermask web off EVERY pad — the fanned pad
    # included (Reto 2026-11-03, :attr:`TopoDesign.via_mask_web_mm`); the
    # copper clearance still applies to foreign pads where it is larger.
    web = float(d.via_mask_web_mm)
    pad_polys = _pad_polys(d, ir, n_layers, pose)
    # A broad phase must enclose the exact max(c, cp, web) test, even
    # for a coarse foreign-net rule or a large fab mask-web requirement.
    pad_clearance = max(
        [
            float(_DEFAULT.clearance_mm),
            *(float(nr.clearance_mm) for nr in rules.values()),
        ]
    )
    # every pad on every layer, once per (pin, polygon) — a TH pad is
    # listed per layer by _pad_polys; the barrel only needs it once
    seen: set[int] = set()
    pads: list[tuple[str, Any, int, int]] = []
    for lay in range(n_layers):
        for pname, poly, pin in pad_polys.get(lay, []):
            if id(poly) in seen:
                continue
            seen.add(id(poly))
            pads.append((pname, poly, pin, lay))
    pad_tree = STRtree([pp[1] for pp in pads]) if pads else None
    layer_pads = [pad_polys.get(lay, []) for lay in range(n_layers)]
    layer_trees = [STRtree([pp[1] for pp in lp]) if lp else None for lp in layer_pads]
    net_ids = {str(n): k for k, n in enumerate(d.net_name)}
    zone_rows = _zone_bodies(d, pose, net_ids, rules)
    via_zones = [body for body, _layers in zone_rows]
    hole_index = CopperIndex(_pad_hole_bodies(d, ir, pose))
    for z in d.zones:
        if z.kind not in ("label_keepout", "fixed_via"):
            continue
        ring = zone_world_ring(d, z, pose)
        if len(ring) >= 3:
            # An authored barrel bars NEW drills even on its own net.
            # Keep its rule while removing the same-net exemption.
            name = str(z.net or "")
            via_zones.append(
                Body(
                    Polygon(np.asarray(ring)),
                    clearance=float(rules.get(name, _DEFAULT).clearance_mm)
                    if name and z.kind == "fixed_via"
                    else 0.0,
                )
            )
    zone_index = CopperIndex(via_zones)
    # Keep the established conservative pre-route site roster: even a
    # zone on another layer bars a fanout stub here. Admitting additional
    # frozen stubs changes the signal corridors (local-61's GND[47]
    # cost four clean nets). Deferred escapes use the layer-aware oracle
    # after signals, where their copper cannot change those corridors.
    stub_zone_index = CopperIndex([b for b, _layers in zone_rows])
    board = Polygon(np.asarray(d.outline, dtype=float)) if len(d.outline) >= 3 else None
    boundary = board.boundary if board is not None else None
    # The hulls silk's rings grow from, at this pose: each part's
    # zero-clearance courtyard ring (``d.part_body`` — the same
    # ``instance_courtyard_polygons`` output ``silk.part_outlines`` hulls)
    # convex-hulled. A part with no ring draws no outline, so it claims
    # no band; the courtyard ring stands in where a synthetic design
    # carries no body.
    hulls: list[Any] = []
    for part in range(len(d.parts)):
        if part >= len(pose) or not np.isfinite(pose[part, :2]).all():
            continue
        cring = d.part_body[part] if part < len(d.part_body) else None
        if (cring is None or not len(cring)) and part < len(d.part_outline):
            cring = d.part_outline[part]
        if cring is None:
            continue
        pts = np.asarray(cring, dtype=np.float64).reshape(-1, 2)
        if len(pts) < 3:
            continue
        hull = MultiPoint(local_to_world(pts, pose[part])).convex_hull
        if not hull.is_empty:
            hulls.append(hull)
    # How far off a hull a via centre must sit to be outside the ring's
    # reach: the furthest a drawn line can sit off it
    # (:class:`SilkRoom.outline_reach_mm` — the ring offset plus the
    # shared-wall extension) plus the legal clearance
    # ``silk._via_gap_cut`` prices — half a stroke plus
    # ``via_to_silk_mm`` — plus the barrel's own radius. The extra slack
    # covers the drawn ring's sampled boundary settling past the analytic
    # offset (decimate growth, GEOS's chorded buffer arcs) — measured
    # ~0.03 mm on taz-leaf's crowded rings.
    ring_clear = (
        float(d.silk.outline_reach_mm)
        + DEFAULT_SILK_WIDTH_MM / 2.0
        + float(d.silk.via_to_silk_mm)
        + 0.05
    )
    band_of: dict[float, Any] = {}

    def ring_band(rad: float) -> Any:
        """Every part hull grown to this barrel's ring reach, unioned —
        a point inside it is closer than the legal clearance to some
        outline's centreline."""
        if rad not in band_of:
            band_of[rad] = (
                unary_union([h.buffer(ring_clear + rad) for h in hulls])
                if hulls
                else None
            )
            if band_of[rad] is not None:
                shapely.prepare(band_of[rad])
        return band_of[rad]

    lo, hi = through_span(n_layers)
    # Drill spacing is net-blind. Copper spacing exempts the same net,
    # but includes BOTH the stub and barrel of every earlier fanout.
    placed_vias = CopperIndex([])
    placed: list[list[Body]] = [[] for _ in range(n_layers)]
    placed_index = [CopperIndex([]) for _ in range(n_layers)]
    placed_nets: set[int] = set()

    def _site(
        radii: Iterable[float],
        angs: list[float],
        rad: float,
        drill: float,
        inner: Any,
        band: Any,
        *,
        cx: float,
        cy: float,
        pin: int,
        lay: int,
        w: float,
        c: float,
        name: str,
    ) -> tuple[float, float] | None:
        """First legal site on ``radii`` × ``angs`` for one pad — the
        whole legality contract above; ``band`` (the ring-band union) is
        the preferred pass's extra veto, ``None`` in the fallback."""
        k = net_ids[name]
        foreign_placed = bool(placed_nets - {k})
        for r in radii:
            for a in angs:
                x, y = cx + r * math.cos(a), cy + r * math.sin(a)
                pt = Point(x, y)
                if inner is not None and not inner.covers(pt):
                    continue
                if band is not None and band.contains(pt):
                    continue
                if placed_vias.blocked(pt, rad, c):
                    continue
                stub = LineString([(cx, cy), (x, y)])
                if board is not None and boundary is not None:
                    edge = max(float(d.edge_clearance_mm or 0.0), c) + FAB_QUANTUM_MM
                    if (
                        not board.covers(stub)
                        or stub.distance(boundary) < w / 2.0 + edge - 1e-9
                        or pt.distance(boundary) < rad + edge - 1e-9
                    ):
                        continue
                if foreign_placed and placed_index[lay].blocked(
                    stub, w / 2.0, c, net=k
                ):
                    continue
                if foreign_placed and any(
                    ix.blocked(pt, rad, c, net=k) for ix in placed_index
                ):
                    continue
                reach = rad + max(c, web, pad_clearance) + FAB_QUANTUM_MM
                ok = True
                hits = (
                    pad_tree.query(box(x - reach, y - reach, x + reach, y + reach))
                    if pad_tree is not None
                    else []
                )
                for j in hits:
                    pname, poly, _ppin, _play = pads[int(j)]
                    cp = float(rules.get(pname, _DEFAULT).clearance_mm) if pname else c
                    if (
                        poly.distance(pt)
                        < rad + max(c, cp, web) + FAB_QUANTUM_MM - 1e-9
                    ):
                        ok = False
                        break
                if not ok:
                    continue
                ltree = layer_trees[lay]
                stub_reach = w / 2.0 + max(c, pad_clearance) + FAB_QUANTUM_MM
                for j in (
                    ltree.query(
                        box(
                            min(cx, x) - stub_reach,
                            min(cy, y) - stub_reach,
                            max(cx, x) + stub_reach,
                            max(cy, y) + stub_reach,
                        )
                    )
                    if ltree is not None
                    else []
                ):
                    pname, poly, ppin = layer_pads[lay][int(j)]
                    if ppin == pin or (pname and pname == name):
                        continue
                    cp = float(rules.get(pname, _DEFAULT).clearance_mm) if pname else c
                    if (
                        poly.distance(stub)
                        < w / 2.0 + max(c, cp) + FAB_QUANTUM_MM - 1e-9
                    ):
                        ok = False
                        break
                if not ok:
                    continue
                if zone_index.blocked(pt, rad, c, net=k):
                    continue
                if hole_index.blocked(pt, drill / 2.0, 0.0, net=ANY_NET):
                    continue
                if stub_zone_index.blocked(stub, w / 2.0, c, net=k):
                    continue
                if ok:
                    return x, y
        return None

    tracks: list[Any] = []
    vias: list[Any] = []
    fell_back: list[tuple[int, int]] = []
    # G2d: pads no preferred or fixed-radius site serves wire out on
    # their own layer AFTER the signal route (:func:`_emit_escapes`),
    # so the signal mesh sees exactly the copper the pre-G2d pass saw.
    deferred: list[tuple[int, int]] = []
    for k in sorted(plane_nets):
        name = str(d.net_name[k])
        nr = rules.get(name, _DEFAULT)
        w, c = float(nr.track_width_mm), float(nr.clearance_mm)
        edge = max(float(d.edge_clearance_mm or 0.0), c) + FAB_QUANTUM_MM
        barrel_sizes = [
            (
                float(nr.via_dia_mm or _VIA_DIA_MM),
                float(nr.via_drill_mm or _VIA_DRILL_MM),
                0.0,
                0.0,
            )
        ]
        if min_via_dia and float(min_via_dia) < barrel_sizes[0][0]:
            mdia = float(min_via_dia)
            mdrill = float(min_via_drill or min(barrel_sizes[0][1], mdia / 2.0))
            barrel_sizes.append((mdia, mdrill, mdia, mdrill))
        # ``inner`` rides the barrel: board and ``edge`` are fixed for the
        # whole search, so the buffered interior is computed once per
        # barrel for all the net's pins and both passes.
        barrels = [
            (
                dia,
                drill,
                q_dia,
                q_drill,
                board.buffer(-(dia / 2.0 + edge)) if board is not None else None,
            )
            for dia, drill, q_dia, q_drill in barrel_sizes
        ]
        for pin in (int(p) for p in d.net_pins[k]):
            side = int(d.pin_side[pin])
            if side == 2:
                continue  # drilled: the pad already reaches the plane
            lay = 0 if side == 0 else n_layers - 1
            part = int(d.pin_part[pin])
            cx, cy = (
                float(v) for v in local_to_world(d.pin_xy[pin][None, :], pose[part])[0]
            )
            # outward first: away from the part's centre, so the escape
            # does not wall the part's other pads in
            ox, oy = cx - float(pose[part, 0]), cy - float(pose[part, 1])
            out_ang = math.atan2(oy, ox) if math.hypot(ox, oy) > 1e-9 else 0.0
            angs = sorted(
                (
                    2.0 * math.pi * i / _FANOUT_DIRECTIONS
                    for i in range(_FANOUT_DIRECTIONS)
                ),
                key=lambda a: abs(math.remainder(a - out_ang, 2.0 * math.pi)),
            )
            site: tuple[Any, ...] | None = None
            for dia, drill, q_dia, q_drill, inner in barrels:
                rad = dia / 2.0
                clear = ring_clear + rad
                # no site nearer than ``clear`` can clear the ring: the
                # pad centre sits inside its own hull, so a candidate
                # r < clear off it is within ``clear`` of that hull too
                hit = _site(
                    (
                        clear,
                        *(
                            clear + r
                            for r in _FANOUT_RADII_MM
                            if r <= _FANOUT_LADDER_CAP_MM
                        ),
                    ),
                    angs,
                    rad,
                    drill,
                    inner,
                    ring_band(rad),
                    cx=cx,
                    cy=cy,
                    pin=pin,
                    lay=lay,
                    w=w,
                    c=c,
                    name=name,
                )
                if hit is not None:
                    site = (*hit, dia, drill, q_dia, q_drill)
                    break
            if site is None:
                for dia, drill, q_dia, q_drill, inner in barrels:
                    rad = dia / 2.0
                    hit = _site(
                        _FANOUT_RADII_MM,
                        angs,
                        rad,
                        drill,
                        inner,
                        None,
                        cx=cx,
                        cy=cy,
                        pin=pin,
                        lay=lay,
                        w=w,
                        c=c,
                        name=name,
                    )
                    if hit is not None:
                        site = (*hit, dia, drill, q_dia, q_drill)
                        break
                if site is not None:
                    fell_back.append((k, pin))
            if site is None:
                # G2d round 5: a pad WIDER than the whole fixed ladder
                # (``own_min > _FANOUT_RADII_MM[-1]`` — the U1/U2 thermal
                # slug) plants its pad-scaled rung BEFORE the route,
                # frozen like any fanout via: post-route the signals
                # already hold the ring (round 4: every candidate
                # vetoed). Ring-agnostic like the fallback, so a sited
                # pad reports on ``fell_back``/``fanout_fallback``.
                own_pols = [g for _pn, g, ppin in pad_polys.get(lay, []) if ppin == pin]
                own = unary_union(own_pols) if own_pols else None
                if own is not None and own.is_empty:
                    own = None
                rho = 0.0
                if own is not None:
                    for g_ in getattr(own, "geoms", (own,)):
                        for px_, py_ in g_.exterior.coords:
                            rho = max(rho, math.hypot(float(px_) - cx, float(py_) - cy))
                for dia, drill, q_dia, q_drill, inner in barrels:
                    rad = dia / 2.0
                    own_min = rho + rad + max(c, web)
                    if own_min <= _FANOUT_RADII_MM[-1]:
                        break
                    hit = _site(
                        (own_min + r for r in _FANOUT_RADII_MM),
                        angs,
                        rad,
                        drill,
                        inner,
                        None,
                        cx=cx,
                        cy=cy,
                        pin=pin,
                        lay=lay,
                        w=w,
                        c=c,
                        name=name,
                    )
                    if hit is not None:
                        site = (*hit, dia, drill, q_dia, q_drill)
                        fell_back.append((k, pin))
                        break
            if site is None:
                # G2d: no preferred, fixed-radius or pad-scaled site —
                # the pad wires out on its own layer AFTER the signal
                # route (:func:`_emit_escapes`), so its copper never
                # enters the signal mesh.
                deferred.append((k, pin))
                continue
            x, y, dia, drill, q_dia, q_drill = site[:6]
            stub_pts = [(cx, cy), (x, y)]
            placed_vias.add_disc(x, y, dia / 2.0, clearance=c)
            placed_nets.add(k)
            for layer in range(n_layers):
                placed[layer].append(Body(Point(x, y), dia / 2.0, k, c))
            placed[lay].append(Body(LineString(stub_pts), w / 2.0, k, c))
            placed_index = [CopperIndex(rows) for rows in placed]
            net_id = ir_net.get(name, int(NO_NET))
            segs = tuple(
                {
                    "shape": "line",
                    "start": [float(px), float(py)],
                    "end": [float(qx), float(qy)],
                }
                for (px, py), (qx, qy) in itertools.pairwise(stub_pts)
            )
            tracks.append(
                RealizedTrack(
                    seg_id=-1,
                    net_id=net_id,
                    layer=lay,
                    segments=segs,
                    length_mm=sum(
                        math.hypot(qx - px, qy - py)
                        for (px, py), (qx, qy) in itertools.pairwise(stub_pts)
                    ),
                    blocked_by=None,
                    width_mm=w,
                )
            )
            vias.append(
                RealizedVia(
                    seg_id=-1,
                    net_id=net_id,
                    x=x,
                    y=y,
                    dia_mm=dia,
                    drill_mm=drill,
                    layer_lo=lo,
                    layer_hi=hi,
                    endpoint="a",
                )
            )
    return tracks, vias, fell_back, deferred


def _emit_escapes(
    d_sig: TopoDesign,
    ir: Any,
    deferred: Sequence[tuple[int, int]],
    pose: np.ndarray,
    rules: Mapping[str, NetRules],
    rres: Any,
    n_layers: int,
    *,
    min_via_dia: float | None = None,
    min_via_drill: float | None = None,
    mesh: Any | None = None,
    joined_pours: Mapping[int, Sequence[tuple[int, Any]]] | None = None,
    fixed_copper: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[
    list[Any],
    list[Any],
    list[tuple[int, int]],
    list[tuple[int, int]],
    dict[tuple[int, int], float],
    dict[tuple[int, int], str],
    dict[tuple[int, int], tuple[int, int]],
]:
    """Emit the deferred escapes AFTER the signal route (G2d round 2):
    pads the preferred and fixed-radius fanout could not site wire out
    on their own layer — signals never see their copper as frozen
    zones, so the signal mesh is exactly the pre-G2d one.

    The branch runs on the mesh the signals used (``d_sig`` carries the
    fanout zones; ``mesh`` reuses the route pass's build when given), to
    the nearest point the same ``_ViaPlanter`` admits. Routed copper is
    real geometry the corridor must keep off: track legs cover their
    edge stretches and veto a leg of the escape's triangles
    (``step_ok``), via barrels and the board outline's margin join that
    check and the per-edge cover, and the taut polyline is verified
    against every foreign leg, barrel and the outline before it emits
    (G2d round 3 — the edge-interval model alone let stubs cross routed
    copper). Both routed-copper checks price only legs on the escape's
    own layer (round 6 — unfiltered they vetoed inner/bottom signal
    legs of F.Cu escapes). A stub longer than ``_ESCAPE_LEN_CAP_MM`` is
    no fanout: the pad is the named finding, its needed length on
    ``need``. The stub's length is measured from where it LEAVES the
    pad's own copper — a wide pad's own half-width is not escape length
    (G2d round 4).

    The normal and minimum barrel sizes finish their established
    single-site searches before extra candidate sites on any size are
    tried. A larger barrel's new alternative must not displace an
    already successful smaller-barrel fallback.

    Only after all those searches fail, bounded outward zone-boundary
    proposals can supply a direct fresh site inside a triangle. They pass
    the planter's shared arbitrary-point predicates and the same full
    verifier, including the unchanged cap; they never reuse a barrel.

    ``joined_pours`` is the round-6 join check: per net, the pre-escape
    pour pieces already wired to it (:func:`_joined_plane_pieces`), as
    ``(poured layer, polygon)`` pairs. An escape counts only if its via
    lands inside one — a site on any other copper anchors a floating
    sliver without joining the plane (SNS2's 28/51 mm stubs onto an
    isolated In1 island: GND still severed, ``pour_fragmented`` new),
    so it is refused and the search tries the next site. ``None`` —
    no footprints, no pour pass — skips the check.

    Returns ``(tracks, vias, missing, emitted, need, reasons, spans)``;
    ``reasons`` names why each ``missing`` pad failed: ``no_corridor``
    (its own layer walled), ``no_site`` (nothing plantable reachable),
    ``over_cap`` (only blocker the length cap — the needed mm is on
    ``need``) or ``isolated_pour`` (every reachable site lands on pour
    copper the pad would only anchor, never join). ``spans`` is the
    ``(layer_lo, layer_hi)`` of the barrel each ``emitted`` pad's stub
    ends on — through for a fresh via, the authored span for a reused
    one (G2d round 9: an escape may end only on a same-net barrel
    reaching BOTH the stub's layer and the joined piece's layer).
    """
    tracks_out: list[Any] = []
    vias_out: list[Any] = []
    still: list[tuple[int, int]] = []
    done: list[tuple[int, int]] = []
    need: dict[tuple[int, int], float] = {}
    reasons: dict[tuple[int, int], str] = {}
    spans: dict[tuple[int, int], tuple[int, int]] = {}
    if not deferred:
        return tracks_out, vias_out, still, done, need, reasons, spans
    from shapely.ops import unary_union

    from precis.pcb.ir import NO_NET
    from precis.pcb.realize import RealizedTrack, RealizedVia, through_span
    from precis.pcb.topoplace.mesh import MeshConflictError, build_mesh

    rules_t = tuple(
        (
            float(rules[d_sig.net_name[k2]].track_width_mm),
            float(rules[d_sig.net_name[k2]].clearance_mm),
            float(rules[d_sig.net_name[k2]].via_dia_mm or _VIA_DIA_MM),
        )
        if k2 < len(d_sig.net_name) and d_sig.net_name[k2] in rules
        else (_DEFAULT.track_width_mm, _DEFAULT.clearance_mm, _VIA_DIA_MM)
        for k2 in range(len(d_sig.net_pins))
    )
    tr, cl = ring_rule(rules_t)
    if mesh is None:
        try:
            mesh = build_mesh(d_sig, pose, trace_mm=tr, clearance_mm=cl)
        except MeshConflictError:
            return (
                tracks_out,
                vias_out,
                list(deferred),
                done,
                need,
                {(k, p): "no_corridor" for k, p in deferred},
                spans,
            )
    ctx = _wires._make_ctx(mesh, d_sig, n_layers, pose)
    planter = _wires._ViaPlanter(
        ctx, d_sig, pose, net_clearance=tuple(rr[1] for rr in rules_t)
    )
    names = [str(n) for n in ir.net_name]
    ir_net = {names[i]: i for i in range(len(names))}
    k_of_name = {str(n): i for i, n in enumerate(d_sig.net_name)}
    lo, hi = through_span(n_layers)
    layer_names = [str(e.get("name")) for e in ir.stackup]
    # an authored via's REAL barrel span (``fixed_copper`` ``span``
    # pair): ``ir.fixed_vias`` is through-treated for placement, but an
    # escape may end on a barrel only when it actually reaches the
    # stub's layer and the joined piece's (G2d round 9). Rows with no
    # span pair are through.
    authored_span: dict[tuple[float, float], tuple[int, int]] = {}
    for row in fixed_copper or ():
        if str(row.get("ctype") or "") != "via":
            continue
        sp = [str(s) for s in (row.get("span") or ())]
        if len(sp) != 2 or sp[0] not in layer_names or sp[1] not in layer_names:
            continue
        authored_span[(float(row["x"]), float(row["y"]))] = (
            min(layer_names.index(sp[0]), layer_names.index(sp[1])),
            max(layer_names.index(sp[0]), layer_names.index(sp[1])),
        )
    edge_cl = float(d_sig.edge_clearance_mm or 0.0)
    outline_poly = (
        Polygon(np.asarray(d_sig.outline, dtype=float))
        if len(d_sig.outline) >= 3
        else None
    )
    # the DRC's own pad set (``_site``'s geometry): the escape length's
    # own-copper union reads it
    pad_rows = _pad_polys(d_sig, ir, n_layers, pose)
    # the routed copper, four ways: every barrel an ANY_NET disc for the
    # planter's dynamic index and an obstacle point; every leg a capsule
    # source for the wire cover, an obstacle segment inside each mesh
    # triangle it crosses, and a verify target on the emitted line
    # (own-net copper exempt — a same-net branch may touch it).
    bodies: list[Any] = []
    legs_flat: list[tuple[Any, int, int, float, float]] = []  # line, lay, k, w, c
    vias_flat: list[
        tuple[float, float, float, int, float, int, int]
    ] = []  # x, y, r, k, c, lo, hi
    legs: dict[int, list[tuple[Any, float, int, float]]] = {}
    for t in rres.tracks:
        nm = names[t.net_id] if 0 <= t.net_id < len(names) else ""
        tk = k_of_name.get(nm)
        tc = float(rules.get(nm, _DEFAULT).clearance_mm) if nm else 0.0
        pts = [tuple(seg["start"]) for seg in t.segments]
        pts.append(tuple(t.segments[-1]["end"]))
        bodies.append(
            Body(
                LineString(pts),
                r=t.width_mm / 2.0,
                net=tk if tk is not None else ANY_NET,
                clearance=tc,
            )
        )
        if tk is not None:
            legs.setdefault(t.layer, []).append((pts, t.width_mm, tk, tc))
            legs_flat.append((LineString(pts), t.layer, tk, t.width_mm, tc))
    for v in rres.vias:
        nm = names[v.net_id] if 0 <= v.net_id < len(names) else ""
        vk = k_of_name.get(nm, -1)
        vc = float(rules.get(nm, _DEFAULT).clearance_mm) if nm else 0.0
        bodies.append(
            Body(Point(v.x, v.y), r=v.dia_mm / 2.0, net=ANY_NET, clearance=vc)
        )
        vias_flat.append(
            (v.x, v.y, v.dia_mm / 2.0, vk, vc, int(v.layer_lo), int(v.layer_hi))
        )
    # authored vias too: they ride ``d_sig`` only as same-net-EXEMPT
    # ``fixed_via`` zones, so without a net-blind body an escape barrel
    # could plant on top of one — the same drill-wall defect the
    # round-7 fanout-via merge closed (G2d round 8). They are also the
    # reuse sites a same-net escape may end on — carrying their REAL
    # authored span, not the through treatment the placer gives them
    # (round 9).
    for v in getattr(ir, "fixed_vias", ()) or ():
        nm = str(v.net or "")
        vk = k_of_name.get(nm, -1)
        vc = float(rules.get(nm, _DEFAULT).clearance_mm) if nm else 0.0
        bodies.append(
            Body(
                Point(float(v.x), float(v.y)),
                r=float(v.dia_mm) / 2.0,
                net=ANY_NET,
                clearance=vc,
            )
        )
        aspan = authored_span.get((float(v.x), float(v.y)), (lo, hi))
        vias_flat.append(
            (float(v.x), float(v.y), float(v.dia_mm) / 2.0, vk, vc, *aspan)
        )
    # one STRtree over every edge's AABB, for the wire-cover candidate
    # scan, and one over the triangles for the step check's obstacle
    # index (both built once — every escape queries them).
    edge_tree = shapely.STRtree(
        shapely.box(
            ctx.edge_lo[:, 0],
            ctx.edge_lo[:, 1],
            ctx.edge_hi[:, 0],
            ctx.edge_hi[:, 1],
        )
    )
    tri_polys = [Polygon(np.asarray(txy, dtype=float)) for txy in ctx.tri_xy]
    tri_tree = shapely.STRtree(tri_polys)
    # generous obstacle reach: the largest exclusion a leg or barrel
    # could ask of any rule — indexes every tri that could matter, the
    # step/verify checks then price the net's own (w, c) exactly.
    reach = (
        max(
            float(r.track_width_mm) / 2.0 + float(r.clearance_mm)
            for r in (*rules.values(), _DEFAULT)
        )
        + FAB_QUANTUM_MM
    )
    tri_legs: dict[int, list[int]] = {}
    tri_vias: dict[int, list[int]] = {}
    tri_band = np.zeros(len(tri_polys), dtype=bool)
    dirty = True

    def rebuild_index() -> None:
        nonlocal dirty
        tri_legs.clear()
        tri_vias.clear()
        for li, (ln, _lay, _tk, tw, _tc) in enumerate(legs_flat):
            rad = tw / 2.0 + reach
            for ti in tri_tree.query(ln.buffer(rad)):
                tri_legs.setdefault(int(ti), []).append(li)
        for vi, (vx, vy, vr, _tk, _tc, _vl, _vh) in enumerate(vias_flat):
            for ti in tri_tree.query(Point(vx, vy).buffer(vr + reach)):
                tri_vias.setdefault(int(ti), []).append(vi)
        if outline_poly is not None:
            margin = reach + edge_cl
            for ti in tri_tree.query(outline_poly.exterior.buffer(margin)):
                tri_band[int(ti)] = True
        dirty = False

    def wire_cover(k: int, lay: int, w: float, c: float) -> Any:
        """Per-edge stretches of ``lay`` priced off by foreign routed
        copper — each leg a capsule of ``their_w/2 + w/2 + max(c,
        their_c)``, each barrel the same around its point — clipped to
        the edge parameter by ``_capsule_cover``."""
        out: dict[int, list[tuple[float, float]]] = {}
        for pts, tw, tk, tc in legs.get(lay, ()):
            if tk == k:
                continue
            rad = tw / 2.0 + w / 2.0 + max(c, tc) + FAB_QUANTUM_MM
            for a_, b_ in itertools.pairwise(pts):
                a = np.asarray(a_, dtype=float)
                b = np.asarray(b_, dtype=float)
                blo = np.minimum(a, b) - rad
                bhi = np.maximum(a, b) + rad
                cand = np.asarray(
                    edge_tree.query(shapely.box(blo[0], blo[1], bhi[0], bhi[1])),
                    dtype=int,
                )
                if not len(cand):
                    continue
                u = ctx.seg_u[cand]
                dv = ctx.seg_v[cand] - u
                aa = np.broadcast_to(a, (len(cand), 2))
                bb = np.broadcast_to(b, (len(cand), 2))
                clo, chi = _wires._capsule_cover(u, dv, aa, bb, rad)
                for j in np.flatnonzero(chi - clo > 1e-12):
                    out.setdefault(int(cand[j]), []).append(
                        (float(clo[j]), float(chi[j]))
                    )
        for vx, vy, vr, tk, tc, _vl, _vh in vias_flat:
            if tk == k:
                continue
            rad = vr + w / 2.0 + max(c, tc) + FAB_QUANTUM_MM
            cand = np.asarray(
                edge_tree.query(shapely.box(vx - rad, vy - rad, vx + rad, vy + rad)),
                dtype=int,
            )
            if not len(cand):
                continue
            u = ctx.seg_u[cand]
            dv = ctx.seg_v[cand] - u
            aa = np.full((len(cand), 2), vx, dtype=float)
            bb = np.full((len(cand), 2), vy, dtype=float)
            clo, chi = _wires._capsule_cover(u, dv, aa, bb, rad)
            for j in np.flatnonzero(chi - clo > 1e-12):
                out.setdefault(int(cand[j]), []).append((float(clo[j]), float(chi[j])))
        return out

    def make_checks(
        lay: int, k: int, w: float, c: float, pin: int, name: str
    ) -> tuple[Any, Any, list[CopperIndex], Any]:
        """The pad's ``step_ok``/``verify``: a leg p→q inside
        a tri is legal only off every foreign leg, barrel and the
        outline's margin; the emitted polyline is checked whole before
        emit — against every foreign PAD too (round 8: the corridor's
        edge-interval cover only sees pads the crossed edges meet, so a
        stub could graze a foreign land inside a tri at less than the
        pair's clearance)."""
        whole = _plane_copper_indices(
            d_sig,
            ir,
            pose,
            rules,
            tuple(rres.tracks) + tuple(tracks_out),
            tuple(rres.vias) + tuple(vias_out),
        )
        edge_margin = w / 2.0 + max(edge_cl, c) + FAB_QUANTUM_MM
        boundary = outline_poly.boundary if outline_poly is not None else None
        active_tris = {
            tri
            for tri, indices in tri_legs.items()
            if any(legs_flat[i][1] == lay and legs_flat[i][2] != k for i in indices)
        } | {
            tri
            for tri, indices in tri_vias.items()
            if any(vias_flat[i][3] != k for i in indices)
        }

        @functools.cache
        def check_step(tri: int, p: Any, q: Any) -> bool:
            seg = LineString([p, q])
            for li in tri_legs.get(tri, ()):
                ln, llay, tk, tw, tc = legs_flat[li]
                if (
                    tk != k
                    and llay == lay
                    and seg.distance(ln)
                    < tw / 2 + w / 2 + max(c, tc) + FAB_QUANTUM_MM - 1e-9
                ):
                    return False
            for vi in tri_vias.get(tri, ()):
                vx, vy, vr, tk, tc, _vl, _vh = vias_flat[vi]
                if tk != k and seg.distance(Point(vx, vy)) < (
                    vr + w / 2 + max(c, tc) + FAB_QUANTUM_MM - 1e-9
                ):
                    return False
            if (
                tri_band[tri]
                and outline_poly is not None
                and (
                    not outline_poly.covers(seg)
                    or (
                        boundary is not None
                        and seg.distance(boundary) < edge_margin - 1e-9
                    )
                )
            ):
                return False
            return True

        interval_obstacles: dict[int, list[Any]] = {}

        @functools.cache
        def iv_ok(tri: int, p_seg: Any, q_seg: Any) -> bool:
            # Build no obstacle footprints on successful witness paths.
            # The failed interval alone pays for analytic shadow roots.
            got = interval_obstacles.get(tri)
            if got is None:
                got = []
                for li in tri_legs.get(tri, ()):
                    ln, llay, tk, tw, tc = legs_flat[li]
                    if tk != k and llay == lay:
                        rad = tw / 2.0 + w / 2.0 + max(c, tc) + FAB_QUANTUM_MM
                        got.extend(
                            (a, b, rad) for a, b in itertools.pairwise(ln.coords)
                        )
                for vi in tri_vias.get(tri, ()):
                    vx, vy, vr, tk, tc, _vl, _vh = vias_flat[vi]
                    if tk != k:
                        got.append(
                            (
                                (vx, vy),
                                (vx, vy),
                                vr + w / 2.0 + max(c, tc) + FAB_QUANTUM_MM,
                            )
                        )
                if tri_band[tri] and boundary is not None:
                    got.extend(
                        (a, b, edge_margin)
                        for a, b in itertools.pairwise(boundary.coords)
                    )
                interval_obstacles[tri] = got
            return _wires._escape_interval_open(got, p_seg, q_seg)

        def step_ok(tri: int, p: Any, q: Any) -> bool:
            # A reversed segment has the same clearance. Both search
            # passes share the immutable obstacle snapshot for this pad.
            if tri not in active_tris and not tri_band[tri]:
                return True
            if q < p:
                p, q = q, p
            return check_step(tri, p, q)

        def verify(pts: Any, _via: Any) -> bool:
            ln = LineString(pts)
            if whole[lay].blocked(ln, w / 2.0, c, net=k):
                return False
            if outline_poly is not None:
                if not outline_poly.covers(ln):
                    return False
                if boundary is not None and ln.distance(boundary) < edge_margin - 1e-9:
                    return False
            return True

        return step_ok, verify, whole, iv_ok

    for k, pin in deferred:
        name = str(d_sig.net_name[k])
        nr = rules.get(name, _DEFAULT)
        w, c = float(nr.track_width_mm), float(nr.clearance_mm)
        side = int(d_sig.pin_side[pin])
        lay = 0 if side == 0 else n_layers - 1
        cand_mask = _wires._cand_mask(_wires._net_layers(d_sig, k, n_layers), n_layers)
        if not cand_mask >> lay & 1:
            still.append((k, pin))
            reasons[(k, pin)] = "no_corridor"
            continue
        open_ = _wires._open_layers(ctx, d_sig, cand_mask, name, frozenset({pin}), w, c)
        if not np.any((open_ >> lay) & 1):
            still.append((k, pin))
            reasons[(k, pin)] = "no_corridor"
            continue
        barrel_sizes = [
            (
                float(nr.via_dia_mm or _VIA_DIA_MM),
                float(nr.via_drill_mm or _VIA_DRILL_MM),
            )
        ]
        if min_via_dia and float(min_via_dia) < barrel_sizes[0][0]:
            barrel_sizes.append(
                (
                    float(min_via_dia),
                    float(min_via_drill or min(barrel_sizes[0][1], min_via_dia / 2.0)),
                )
            )
        # the pad's own copper on ``lay`` (G2d round 4): the stub's
        # escape length is measured from where it LEAVES that copper —
        # a wide pad's own half-width is not escape length
        own_pols = [g for _pn, g, ppin in pad_rows.get(lay, []) if ppin == pin]
        own = unary_union(own_pols) if own_pols else None
        if own is not None and own.is_empty:
            own = None

        sited: tuple[Any, ...] | None = None
        if dirty:
            rebuild_index()
        wc = wire_cover(k, lay, w, c)
        step_ok, verify, whole, iv_ok = make_checks(lay, k, w, c, pin, name)
        # the barrels of this net already on the board — routed,
        # fanout, authored — an escape may END on one rather than drill
        # a second barrel beside it (G2d round 8 ruling), and only when
        # its span reaches both the stub's layer and the joined piece's
        # (round 9 — a blind barrel beside the plane is no join).
        reuse = [
            (vx, vy, vlo, vhi)
            for vx, vy, _vr, tk, _tc, vlo, vhi in vias_flat
            if tk == k
        ]
        span_of = {(vx, vy): (vlo, vhi) for vx, vy, vlo, vhi in reuse}
        # round 6: the via must land on pour copper already joined to the
        # net — inside a piece that holds one of the net's primitives on
        # the poured layer — else it only anchors a floating sliver and
        # the pad is still missing (``isolated_pour``).
        isolated = [False]
        if joined_pours is not None:
            geo_verify = verify
            pieces = tuple(joined_pours.get(k, ()))

            def verify(
                pts: Any,
                via: Any,
                _gv: Any = geo_verify,
                _ps: Any = pieces,
                _flag: Any = isolated,
                _spans: Any = span_of,
            ) -> bool:
                if not _gv(pts, via):
                    return False
                vlo, vhi = _spans.get((float(via[0]), float(via[1])), (lo, hi))
                for lay2, p_ in _ps:
                    if vlo <= lay2 <= vhi and p_.intersects(
                        Point(float(via[0]), float(via[1]))
                    ):
                        return True
                _flag[0] = True
                return False

        # the length cap inside ``verify`` (G2d round 9): an over-cap
        # site — fresh or reuse — is refused like any other, so the
        # search keeps going and a shorter legal site still wins. The
        # shortest refused length is the ``need`` figure.
        cap_min = [math.inf]
        geo_join = verify
        start = mesh.verts[int(mesh.pin_vertex[pin])]
        pad_radius = 0.0
        if own is not None:
            x0, y0, x1, y1 = own.bounds
            pad_radius = max(
                math.dist(start, corner)
                for corner in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
            )

        def radius_mm(_cap: Any = cap_min, _r: float = pad_radius) -> float:
            return math.inf if math.isinf(_cap[0]) else _ESCAPE_LEN_CAP_MM + _r

        def verify(
            pts: Any,
            via: Any,
            _gv: Any = geo_join,
            _own: Any = own,
            _cap: Any = cap_min,
        ) -> bool:
            ln = LineString(pts)
            over = float(
                ln.length - (ln.intersection(_own).length if _own is not None else 0.0)
            )
            # Once a legal over-cap witness exists, a still-longer
            # candidate can neither emit nor improve the diagnostic.
            if over > _ESCAPE_LEN_CAP_MM and over >= _cap[0]:
                return False
            if not _gv(pts, via):
                return False
            if over > _ESCAPE_LEN_CAP_MM:
                _cap[0] = min(_cap[0], over)
                return False
            return True

        # Outward frozen rings can put an existing barrel behind an
        # earlier fresh-site portal. A straight reuse is independent of
        # that triangulation: offer it first, with the same whole-copper,
        # layer-span, pour-join and length checks as every other stub.
        for vx, vy, vlo, vhi in sorted(
            reuse, key=lambda site: math.dist(start, site[:2])
        ):
            if not vlo <= lay <= vhi:
                continue
            site = (vx, vy, 0.0)
            direct = [tuple(start), (vx, vy)]
            if verify(direct, site):
                dia, drill = barrel_sizes[0]
                sited = (direct, site, dia, drill, True)
                break
        # Finish the established search for EVERY size before extra
        # sites of a larger barrel can displace a successful smaller
        # fallback. With one size, fanout_escape already preserves that
        # order within its own search. Reuse each size's unchanged
        # dynamic snapshot between phases so planter verdicts stay cached.
        site_phases = (False, True) if len(barrel_sizes) > 1 else (True,)
        dyn_by_size: dict[tuple[float, float], CopperIndex] = {}
        verify_by_size: dict[tuple[float, float], Callable[[Any, Any], bool]] = {}
        for continue_sites, (dia, drill) in itertools.product(
            site_phases, barrel_sizes
        ):
            if sited is not None:
                break
            dyn = dyn_by_size.get((dia, drill))
            if dyn is None:
                dyn = CopperIndex(list(bodies))
                dyn_by_size[(dia, drill)] = dyn
            # The planter certifies the barrel, while this independent
            # whole-copper oracle also checks it and every stub segment
            # against named zones and all earlier emitted copper.
            geo_verify = verify

            def verify_site(
                pts: Any,
                via: Any,
                _dia: float = dia,
                _c: float = c,
                _k: int = k,
                _whole: Any = whole,
                _gv: Any = geo_verify,
                _spans: Any = span_of,
            ) -> bool:
                pt = Point(float(via[0]), float(via[1]))
                # Reuse emits only the stub. An authored barrel can sit
                # in a region barred to NEW drills; its existing size
                # and geometry must not be replaced by this candidate's.
                if (float(via[0]), float(via[1])) in _spans:
                    return _gv(pts, via)
                if outline_poly is not None:
                    if not outline_poly.covers(pt) or pt.distance(
                        outline_poly.boundary
                    ) < (_dia / 2.0 + max(edge_cl, _c) - 1e-9):
                        return False
                return not any(
                    _whole[lay2].blocked(pt, _dia / 2.0, _c, net=_k)
                    for lay2 in range(lo, hi + 1)
                ) and _gv(pts, via)

            verify_by_size[(dia, drill)] = verify_site
            out = _wires.fanout_escape(
                ctx,
                mesh,
                pin,
                lay,
                k,
                open_,
                w,
                c,
                dia,
                planter,
                dyn,
                wire_cover=wc,
                step_ok=step_ok,
                verify=verify_site,
                reuse_pts=reuse,
                radius_mm=radius_mm,
                iv_ok=iv_ok,
                via_drill_mm=drill,
                continue_sites=continue_sites,
            )
            if out is None:
                continue
            sited = (out[0], out[1], dia, drill, out[2])
            break
        # A via-barred region may contain every mesh portal even when
        # a short straight stub reaches a clear interior site. Preserve
        # ALL established searches first, then try bounded zone-boundary
        # proposals with the planter's full rules and this same verifier.
        if sited is None:
            for dia, drill in barrel_sizes:
                proposals = planter.boundary_points(
                    (float(start[0]), float(start[1])), dia / 2.0, c, k
                )
                xy = np.asarray(proposals, dtype=float).reshape(-1, 2)
                free = planter.plantable_points(
                    xy, dia / 2.0, c, k, dyn_by_size[(dia, drill)], drill_mm=drill
                )
                check_site = verify_by_size[(dia, drill)]
                for px, py in xy[free]:
                    site_xy = (float(px), float(py))
                    # A fresh point must never take verify_site's
                    # authored-barrel reuse exemption, even at an exact
                    # coordinate match. It always emits a new drill.
                    if site_xy in span_of:
                        continue
                    site = (*site_xy, 0.0)
                    direct = [tuple(start), site_xy]
                    if check_site(direct, site):
                        sited = (direct, site, dia, drill, False)
                        break
                if sited is not None:
                    break
        if sited is None:
            still.append((k, pin))
            if cap_min[0] < math.inf:
                need[(k, pin)] = cap_min[0]
                reasons[(k, pin)] = "over_cap"
            elif isolated[0]:
                reasons[(k, pin)] = "isolated_pour"
            else:
                reasons[(k, pin)] = "no_site"
            continue
        pts, via, dia, drill, reused = sited
        x, y = float(via[0]), float(via[1])
        net_id = ir_net.get(name, int(NO_NET))
        segs = tuple(
            {
                "shape": "line",
                "start": [float(px), float(py)],
                "end": [float(qx), float(qy)],
            }
            for (px, py), (qx, qy) in itertools.pairwise(pts)
        )
        tracks_out.append(
            RealizedTrack(
                seg_id=-1,
                net_id=net_id,
                layer=lay,
                segments=segs,
                length_mm=sum(
                    math.hypot(qx - px, qy - py)
                    for (px, py), (qx, qy) in itertools.pairwise(pts)
                ),
                blocked_by=None,
                width_mm=w,
            )
        )
        if not reused:
            vias_out.append(
                RealizedVia(
                    seg_id=-1,
                    net_id=net_id,
                    x=x,
                    y=y,
                    dia_mm=dia,
                    drill_mm=drill,
                    layer_lo=lo,
                    layer_hi=hi,
                    endpoint="a",
                )
            )
            bodies.append(Body(Point(x, y), r=dia / 2.0, net=ANY_NET, clearance=c))
            vias_flat.append((x, y, dia / 2.0, k, c, lo, hi))
        bodies.append(Body(LineString(pts), r=w / 2.0, net=k, clearance=c))
        legs.setdefault(lay, []).append((list(pts), w, k, c))
        legs_flat.append((LineString(pts), lay, k, w, c))
        dirty = True
        done.append((k, pin))
        spans[(k, pin)] = span_of.get((x, y), (lo, hi))
    return tracks_out, vias_out, still, done, need, reasons, spans


def _plane_pours(
    ir: Any,
    tracks: Sequence[Any],
    vias: Sequence[Any],
    d: TopoDesign,
    pose: np.ndarray,
    net_rules: Mapping[str, NetRules] | None,
    footprints: Mapping[str, Any] | None,
    fixed_copper: Sequence[Mapping[str, Any]] | None,
    hole_clearance_mm: float,
) -> list[dict[str, Any]]:
    """The plane layers poured over the finished copper — the legacy
    realizer's own pour pass (:func:`precis.pcb.realize._pour_planes`,
    antipads around every foreign barrel) plus its bare-island drop, so
    no fill fragment floats without a via of its net (gr524239; an
    interim, Reto 2026-11-01: "if we really need to make a pour on
    in1"). Keepout zones are cut out of the fill by clearance: the
    legacy pass never saw them, and taz-leaf's no-copper hanger ring and
    zip-tie discs came back as five keepout_copper errors."""
    from shapely.ops import unary_union

    from precis.pcb import planes as pcb_planes
    from precis.pcb import realize as pcb_realize
    from precis.pcb.topoplace.proxy import zone_world_ring

    rules = net_rules or {}
    clearance = max(
        (float(r.clearance_mm) for r in rules.values()),
        default=float(_DEFAULT.clearance_mm),
    )
    edge = max(clearance, float(d.edge_clearance_mm or 0.0)) + 0.01
    pours, _extra = pcb_realize._pour_planes(
        ir,
        list(tracks),
        list(vias),
        [],
        [],
        clearance,
        edge,
        dict(footprints or {}),
        [dict(r) for r in fixed_copper or ()],
        npth_clearance_mm=float(hole_clearance_mm),
    )
    if not pours:
        return []
    layer_names = [str(e.get("name")) for e in ir.stackup]
    cuts: dict[str, list[Any]] = {}
    for z in d.zones:
        if z.kind not in ("keepout", "mounting_hole"):
            continue
        ring = zone_world_ring(d, z, pose)
        if len(ring) < 3:
            continue
        g = Polygon(np.asarray(ring)).buffer(clearance)
        for lay in z.layers or range(len(layer_names)):
            if 0 <= int(lay) < len(layer_names):
                cuts.setdefault(layer_names[int(lay)], []).append(g)
    if cuts:
        cut_pours: list[dict[str, Any]] = []
        for pour in pours:
            lay_cuts = cuts.get(str(pour.get("layer")))
            if not lay_cuts:
                cut_pours.append(pour)
                continue
            poly = Polygon(pour["polygon"], pour.get("holes") or []).buffer(0)
            rest = poly.difference(unary_union(lay_cuts))
            for frag in getattr(rest, "geoms", [rest]):
                if frag.is_empty or frag.area < pcb_planes.MIN_FRAGMENT_MM2:
                    continue
                cut_pours.append(
                    pcb_planes._pour_item(frag, str(pour["layer"]), str(pour["net"]))
                )
        pours = cut_pours
    pours, _dropped = pcb_realize._drop_bare_islands(
        ir,
        list(tracks),
        list(vias),
        pours,
        pcb_realize.pads_for_ir(ir, layer_names, dict(footprints or {})),
        [dict(r) for r in fixed_copper or ()],
    )
    return pours


def _joined_plane_pieces(
    d: TopoDesign,
    ir: Any,
    planes: Mapping[int, int],
    tracks: Sequence[Any],
    vias: Sequence[Any],
    pose: np.ndarray,
    net_rules: Mapping[str, NetRules] | None,
    footprints: Mapping[str, Any] | None,
    fixed_copper: Sequence[Mapping[str, Any]] | None,
    hole_clearance_mm: float,
    *,
    pours: Sequence[dict[str, Any]] | None = None,
) -> dict[int, list[tuple[int, Any]]]:
    """Per plane net, the pour pieces an escape via would actually JOIN
    (G2d round 6): the pre-escape pour — the deferred pads' copper does
    not exist yet — restricted to the pieces already wired to their
    net, i.e. holding one of its primitives on the poured layer: a via
    barrel spanning it, a same-layer track body, a pad land that reaches
    it, or the net's authored copper there. A piece holding none is a
    floating sliver an escape via only ANCHORS (the bare-island drop
    keeps it once the via sits on it): ``pour_fragmented`` is new and
    the pad is still unconnected — SNS2's In1 island. Each entry is
    ``(poured layer, polygon)`` — round 9: a reused barrel joins only
    on layers inside its span.
    ``_emit_escapes`` reads the result as its site gate, so ``{}``
    pieces for a net means every site is refused. ``pours`` skips the
    pour pass when the caller already ran one over the same copper
    (route_design's pre-escape pour and — for the final-pour re-verify
    of round 8 — the post-escape one)."""
    from precis.pcb import realize as pcb_realize
    from precis.pcb.ir import plane_layers_of

    out: dict[int, list[tuple[int, Any]]] = {k: [] for k in planes}
    if not planes or footprints is None:
        return out
    if pours is None:
        pours = _plane_pours(
            ir,
            tracks,
            vias,
            d,
            pose,
            net_rules,
            footprints,
            fixed_copper,
            hole_clearance_mm,
        )
    if not pours:
        return out
    layer_names = [str(e.get("name")) for e in ir.stackup]
    net_ids = {str(ir.net_name[n]): n for n in range(ir.n_nets)}
    pad_rows = _pad_bodies(d, ir, int(ir.n_layers), pose)
    pieces: dict[tuple[str, str], list[Any]] = {}
    for p in pours:
        poly = pcb_realize._pour_polygon(p)
        if not poly.is_empty:
            pieces.setdefault(
                (str(p.get("net", "")), str(p.get("layer", ""))), []
            ).append(poly)
    for k in planes:
        name = str(d.net_name[k])
        net_id = net_ids.get(name)
        if net_id is None:
            continue
        for lay in plane_layers_of(int(ir.net_plane_layers[net_id])):
            if not 0 <= lay < len(layer_names):
                continue
            lname = layer_names[lay]
            for poly in pieces.get((name, lname), ()):
                if _pour_piece_joined(
                    poly,
                    lay,
                    lname,
                    name,
                    net_id,
                    tracks,
                    vias,
                    pad_rows,
                    fixed_copper,
                    layer_names,
                ):
                    out[k].append((lay, poly))
    return out


def _pour_piece_joined(
    poly: Any,
    layer: int,
    layer_name: str,
    net: str,
    net_id: int,
    tracks: Sequence[Any],
    vias: Sequence[Any],
    pad_rows: Mapping[int, Sequence[tuple[str, Any, int]]],
    fixed_copper: Sequence[Mapping[str, Any]] | None,
    layer_names: Sequence[str],
) -> bool:
    """Does ``poly`` — one pour piece of ``net`` on ``layer`` — already
    hold a primitive that wires it to the net on that layer? A through
    via or drilled pad's barrel spans every layer; an SMD pad's land
    reaches only its own (the bare-island drop counts pads generously
    on every layer precisely so a piece kept that way is still reported
    — kept is not joined). ``pad_rows`` is :func:`_pad_bodies`' per-layer
    world geometry — the pad's REAL copper shape, not its circumradius:
    a fragment near a 4x1 pad's centre but off the rect does not join
    (round 8 — the half-diagonal reach admitted it)."""
    from precis.pcb.clearance import Body as ExactBody
    from precis.pcb.clearance import body_of, region_touches_land, signed_gap

    pour_body = ExactBody(poly, (), 0.0)

    def via_joined(x: float, y: float, dia: float, drill: float) -> bool:
        # A fragment wholly inside the drill void has no copper contact.
        # The farthest point of a polygon from the centre is a vertex.
        for piece in shapely.get_parts(poly):
            if piece.is_empty or piece.distance(Point(x, y)) > dia / 2.0:
                continue
            if drill <= 0.0:
                return True
            coords = shapely.get_coordinates(piece)
            if np.any(np.hypot(coords[:, 0] - x, coords[:, 1] - y) >= drill / 2.0):
                return True
        return False

    def track_joined(segments: Any, width: float) -> bool:
        body = body_of({"ctype": "track", "segments": segments, "width_mm": width})
        return body is not None and signed_gap(pour_body, body) <= 0.0

    for v in vias:
        if (
            v.net_id == net_id
            and v.layer_lo <= layer <= v.layer_hi
            and via_joined(v.x, v.y, float(v.dia_mm), float(v.drill_mm))
        ):
            return True
    for t in tracks:
        if t.net_id != net_id or t.layer != layer:
            continue
        if track_joined(t.segments, float(t.width_mm)):
            return True
    hole_cuts: dict[int, Any] = {}
    for pname, g, _pin in pad_rows.get(layer, ()):
        body = g if isinstance(g, Body) else Body(g)
        if pname != net:
            continue
        hole = body.hole if isinstance(body, _PadBody) else None
        if region_touches_land(
            poly,
            ExactBody(body.geom, (), body.r),
            ExactBody(hole.geom, (), hole.r) if hole is not None else None,
            hole_cuts=hole_cuts,
        ):
            return True
    for item in fixed_copper or []:
        if str(item.get("net") or "") != net:
            continue
        if item.get("x") is not None and item.get("y") is not None:
            span = [str(s) for s in (item.get("span") or ())]
            if len(span) == 2 and span[0] in layer_names and span[1] in layer_names:
                lo_i, hi_i = layer_names.index(span[0]), layer_names.index(span[1])
            else:
                lo_i, hi_i = 0, len(layer_names) - 1
            if lo_i <= layer <= hi_i and via_joined(
                float(item["x"]),
                float(item["y"]),
                float(item.get("dia_mm", 0.0)),
                float(item.get("drill_mm", 0.0)),
            ):
                return True
            continue
        if item.get("polygon"):
            if str(item.get("layer")) == layer_name and poly.intersects(
                Polygon(
                    [(float(q[0]), float(q[1])) for q in item["polygon"]],
                    [
                        [(float(q[0]), float(q[1])) for q in h]
                        for h in (item.get("holes") or [])
                        if len(h) >= 3
                    ],
                )
            ):
                return True
            continue
        if str(item.get("layer")) == layer_name and track_joined(
            item.get("segments") or [], float(item.get("width_mm", 0.0))
        ):
            return True
    return False


def _world_to_part_local(
    xy: tuple[float, float], pose_row: np.ndarray
) -> tuple[float, float]:
    """Inverse of :func:`topo_proxy.local_to_world` for one ``(4,)``
    pose row — mirror and quarter-turn are each their own inverse under
    negation, so this is exact, not an approximation. td569572's
    synthetic via-landing pins need the reverse direction: a board-space
    point back into a part's footprint frame."""
    x, y, rot_q, side = (float(v) for v in pose_row[:4])
    rx, ry = xy[0] - x, xy[1] - y
    r = round(rot_q) % 4
    if r == 0:
        mx, ly = rx, ry
    elif r == 1:
        mx, ly = -ry, rx
    elif r == 2:
        mx, ly = -rx, -ry
    else:
        mx, ly = ry, -rx
    lx = -mx if side >= 0.5 else mx
    return lx, ly


def _plane_trees(
    d: TopoDesign,
    ir: Any,
    planes: Mapping[int, int],
    pose_a: np.ndarray,
    tracks: Sequence[Any],
    vias: Sequence[Any],
    f_vias: Sequence[Any],
    e_tracks: Sequence[Any],
    e_done: Sequence[tuple[int, int]],
    e_spans: Mapping[tuple[int, int], tuple[int, int]],
    net_rules: Mapping[str, NetRules] | None,
    min_via_dia: float | None,
    min_via_drill: float | None,
    min_via_ring: float | None,
    event: Callable[[str], None] | None,
) -> tuple[list[Any], list[Any], set[int]]:
    """Grow each plane net's copper tree on its own reserved layer
    (td569572, ruling me569463: "every net including GND is a tree";
    me569570 stage 4) over the signal+fanout+escape copper already
    frozen in ``tracks``/``vias`` — :func:`route_design`'s pour
    (:func:`_plane_pours`) stays only as the fallback for a net this
    pass cannot connect whole.

    A fanout via's barrel spans every layer (:func:`_fanout_vias`) and
    an escape's lands on a barrel reaching the plane layer
    (``e_spans``) — both are real copper already, just not a MESH PIN
    the search can grow a branch to. This mints one synthetic
    through-hole pin per landing (plus every drilled pin already on
    ``d.net_pins[k]``, which reaches any layer with no via at all) at
    the landing's board-space point, anchored to an arbitrary existing
    part (part 0 — a via landing belongs to no part; the anchor only
    supplies a pose to invert) via :func:`_world_to_part_local`. It
    also carries a ``pin_wh`` the size of a via barrel: with none,
    :func:`_pad_bodies` mints no pad for it at all (``hw <= 0.0``), and
    :func:`_severed_nets`'s emit-connectivity check — which counts a
    component "joining the net's pads" only by a ``pad_bodies`` hit —
    then sees a perfectly connected tree with ZERO pads and withdraws
    it whole.

    The grown design forces each plane net onto exactly its layer
    (``net_layer_force``) and bars no layer (``plane_layers=()`` — the
    bar exists only to keep OTHER nets off the plane during the shared
    signal pass; nothing else routes here). Returns ``(tracks, vias,
    ok)`` — the tree's extra copper and the subset of ``planes`` keys
    it connected as ONE component spanning every landing; the caller
    drops those nets' pour fragments and leaves the rest to the pour."""
    from precis.pcb.realize import RealizeResult
    from precis.pcb.topoplace.mesh import MeshConflictError

    ir_of = {str(n): i for i, n in enumerate(ir.net_name)}
    n_layers = int(ir.n_layers)
    anchor_part = 0
    pose_row = pose_a[anchor_part]

    real_pins: dict[int, list[int]] = {}
    new_xy: list[tuple[float, float]] = []
    new_net: list[int] = []
    owner_k: list[int] = []
    for k in planes:
        ir_id = ir_of.get(str(d.net_name[k]))
        if ir_id is None:
            real_pins[k] = []
            continue
        real_pins[k] = [int(p) for p in d.net_pins[k] if int(d.pin_side[p]) == 2]
        for v in f_vias:
            if int(v.net_id) == ir_id:
                new_xy.append((float(v.x), float(v.y)))
                new_net.append(ir_id)
                owner_k.append(k)
        lay = planes[k]
        for t, (kk, pin) in zip(e_tracks, e_done):
            if kk != k:
                continue
            slo, shi = e_spans.get((kk, pin), (0, n_layers - 1))
            if slo <= lay <= shi:
                end = t.segments[-1]["end"]
                new_xy.append((float(end[0]), float(end[1])))
                new_net.append(ir_id)
                owner_k.append(k)

    counts: dict[int, int] = dict.fromkeys(planes, 0)
    for k in owner_k:
        counts[k] += 1
    for k, pins in real_pins.items():
        counts[k] += len(pins)
    attemptable = {k for k, n in counts.items() if n >= 2}
    if not attemptable:
        return [], [], set()

    base = len(d.pin_xy)
    n_new = len(new_xy)
    if n_new:
        local = [_world_to_part_local(xy, pose_row) for xy in new_xy]
        pin_xy = np.vstack([d.pin_xy, np.asarray(local, dtype=np.float64)])
        pin_part = np.concatenate(
            [d.pin_part, np.full(n_new, anchor_part, dtype=d.pin_part.dtype)]
        )
        pin_side = np.concatenate(
            [d.pin_side, np.full(n_new, 2, dtype=d.pin_side.dtype)]
        )
        pin_net = np.concatenate(
            [d.pin_net, np.asarray(new_net, dtype=d.pin_net.dtype)]
        )
        pin_label = (
            np.concatenate([d.pin_label, np.full(n_new, "gnd-tree-via", dtype=object)])
            if len(d.pin_label) >= base
            else d.pin_label
        )
        via_dia = np.asarray(
            [
                (net_rules or {}).get(str(d.net_name[k]), _DEFAULT).via_dia_mm
                or _VIA_DIA_MM
                for k in owner_k
            ],
            dtype=np.float64,
        )
        pin_wh = (
            np.vstack([d.pin_wh, np.column_stack([via_dia, via_dia])])
            if len(d.pin_wh) >= base
            else d.pin_wh
        )
    else:
        pin_xy, pin_part, pin_side, pin_net, pin_label, pin_wh = (
            d.pin_xy,
            d.pin_part,
            d.pin_side,
            d.pin_net,
            d.pin_label,
            d.pin_wh,
        )

    net_pins_new: list[np.ndarray] = []
    for k in range(len(d.net_pins)):
        if k not in attemptable:
            net_pins_new.append(np.zeros(0, dtype=np.int64))
            continue
        extra = [base + i for i, kk in enumerate(owner_k) if kk == k]
        net_pins_new.append(np.asarray(real_pins[k] + extra, dtype=np.int64))

    # the pads _pad_bodies/_severed_nets gather are read off pin_net,
    # not net_pins — a plane net's SMD pad (fanned out before this pass,
    # excluded above) still carries d.pin_net == this net, so the
    # connectivity check sees it as a dangling GND pad with no copper
    # and calls the whole tree severed. Disown it here: its real join
    # is the fanout via, already frozen outside d_gnd.
    from precis.pcb.ir import NO_NET

    pin_net = pin_net.copy()
    for k in attemptable:
        kept = set(real_pins[k])
        for p in d.net_pins[k]:
            if int(p) not in kept:
                pin_net[int(p)] = NO_NET

    force = np.array(d.net_layer_force, dtype=np.int8, copy=True)
    if len(force) < len(d.net_pins):
        force = np.concatenate(
            [force, np.zeros(len(d.net_pins) - len(force), dtype=np.int8)]
        )
    for k in attemptable:
        force[k] = planes[k]

    copper = RealizeResult(tuple(tracks), tuple(vias), ())
    d_gnd = dataclasses.replace(
        d,
        pin_xy=pin_xy,
        pin_part=pin_part,
        pin_side=pin_side,
        pin_net=pin_net,
        pin_label=pin_label,
        pin_wh=pin_wh,
        net_pins=tuple(net_pins_new),
        net_layer_force=force,
        plane_layers=(),
        zones=tuple(d.zones) + tuple(_copper_zones(copper, d, ir, pose_a)),
    )
    try:
        plan_gnd, rres_gnd = _route_signals(
            d_gnd,
            ir,
            pose=pose_a,
            net_rules=net_rules,
            min_via_dia=min_via_dia,
            min_via_drill=min_via_drill,
            min_via_ring=min_via_ring,
            event=event,
        )
    except (CorridorOverflow, MeshConflictError):
        return [], [], set()

    failed = _plan_failed_nets(plan_gnd)
    ok = attemptable - failed
    if not ok:
        return [], [], set()
    ok_ir_ids = {ir_of[str(d.net_name[k])] for k in ok}
    tree_tracks = [t for t in rres_gnd.tracks if int(t.net_id) in ok_ir_ids]
    tree_vias = [v for v in rres_gnd.vias if int(v.net_id) in ok_ir_ids]
    return tree_tracks, tree_vias, ok


def route_design(
    d: TopoDesign,
    ir: Any,
    *,
    pose: Any = None,
    net_rules: Mapping[str, NetRules] | None = None,
    min_via_dia: float | None = None,
    min_via_drill: float | None = None,
    min_via_ring: float | None = None,
    event: Callable[[str], None] | None = None,
    masks: BoardMasks | None = None,
    footprints: Mapping[str, Any] | None = None,
    fixed_copper: Sequence[Mapping[str, Any]] | None = None,
    hole_clearance_mm: float = 0.0,
    hv_weight: float = 0.0,
) -> tuple[RoutePlan, Any]:
    """Mesh → wires → plan → copper for one pose — the route half every
    caller (the ``placer='topo'`` job, ``pcb.local``) shares.

    A net the board pours (``plane_net``, ``ir.net_plane_layers``) is not
    wired (gr524239: "plane nets drop to the plane by via, not routed on
    F.Cu"): each of its SMD pads gets a fanout via first
    (:func:`_fanout_vias`), frozen as copper the signal route keeps off,
    the plane layer is barred to every routed net
    (:attr:`TopoDesign.plane_layers`), the signals route
    (:func:`_route_signals`), and — when the caller passes
    ``footprints`` — the plane is poured over the finished copper
    (:func:`_plane_pours`). Without plane nets this is
    :func:`_route_signals` verbatim; see it for ``pose``, ``net_rules``,
    ``event``, ``masks`` and ``hv_weight``."""
    kw: dict[str, Any] = dict(
        pose=pose,
        net_rules=net_rules,
        min_via_dia=min_via_dia,
        min_via_drill=min_via_drill,
        min_via_ring=min_via_ring,
        event=event,
        masks=masks,
        hv_weight=hv_weight,
    )
    planes = _plane_nets(d, ir)
    if not planes:
        return _route_signals(d, ir, **kw)
    pose_a = np.asarray(d.pose0 if pose is None else pose, dtype=float)
    # the fanout sits the same current-rated barrel the signal route
    # sites (:func:`_via_choice_rules`): a net with no current figure
    # gets the fab floor, shrinking the frozen stub's footprint too
    frules, _fcur = _via_choice_rules(
        ir, net_rules or {}, min_via_dia, min_via_drill, min_via_ring
    )
    f_tracks, f_vias, fell_back, deferred = _fanout_vias(
        d,
        ir,
        int(ir.n_layers),
        pose_a,
        frules,
        planes,
        min_via_dia=min_via_dia,
        min_via_drill=min_via_drill,
    )
    if event is not None:
        event(
            f"route: planes — {len(planes)} net(s) fan out by {len(f_vias)} via(s), "
            f"{len(fell_back)} inside a ring, {len(deferred)} deferred (G2d)"
        )
    from precis.pcb.realize import RealizeResult

    fan = RealizeResult(tuple(f_tracks), tuple(f_vias), ())
    d_sig = dataclasses.replace(
        d,
        net_pins=tuple(
            np.zeros(0, dtype=np.int64) if k in planes else pins
            for k, pins in enumerate(d.net_pins)
        ),
        zones=tuple(d.zones) + tuple(_copper_zones(fan, d, ir, pose_a)),
        plane_layers=tuple(sorted(set(planes.values()))),
    )
    mesh_sink: list[Any] = []
    kw["mesh_sink"] = mesh_sink
    plan, rres = _route_signals(d_sig, ir, **kw)
    bad_fanouts = _guard_fanouts(d, ir, pose_a, frules, rres, f_tracks, f_vias)
    if bad_fanouts:
        rejected: set[tuple[int, int]] = set()
        for i in bad_fanouts:
            track = f_tracks[i]
            name = str(ir.net_name[track.net_id])
            k = list(d.net_name).index(name)
            start = np.asarray(track.segments[0]["start"], dtype=float)
            for pin in d.net_pins[k]:
                part = int(d.pin_part[pin])
                xy = topo_proxy.local_to_world(d.pin_xy[pin][None, :], pose_a[part])[0]
                if np.linalg.norm(start - xy) < 1e-9:
                    rejected.add((k, int(pin)))
        deferred.extend(sorted(rejected))
        fell_back = [kp for kp in fell_back if kp not in rejected]
        f_tracks = [t for i, t in enumerate(f_tracks) if i not in bad_fanouts]
        f_vias = [v for i, v in enumerate(f_vias) if i not in bad_fanouts]
    # G2d: the deferred pads wire out on the mesh the signals used —
    # their stubs were never frozen copper, so the signal route is the
    # pre-G2d one bit for bit. An escape counts only when its via lands
    # on pour copper already joined to the net (round 6): the pre-
    # escape pour's wired pieces gate the site.
    joined: dict[int, list[tuple[int, Any]]] | None = None
    pours_pre: list[dict[str, Any]] | None = None
    if deferred and footprints is not None:
        pours_pre = _plane_pours(
            ir,
            list(rres.tracks) + f_tracks,
            list(rres.vias) + f_vias,
            d,
            pose_a,
            net_rules,
            footprints,
            fixed_copper,
            hole_clearance_mm,
        )
        joined = _joined_plane_pieces(
            d,
            ir,
            planes,
            list(rres.tracks) + f_tracks,
            list(rres.vias) + f_vias,
            pose_a,
            net_rules,
            footprints,
            fixed_copper,
            hole_clearance_mm,
            pours=pours_pre,
        )
    # the corridor must keep off the fanout stubs too: the stub zones
    # exempt same-net copper, but via_via_keepout is net-blind — an
    # escape barrel planted on a same-net fanout via is a real drill
    # collision, so index the stubs as copper for the emit.
    esc_copper = dataclasses.replace(
        rres,
        tracks=tuple(rres.tracks) + tuple(f_tracks),
        vias=tuple(rres.vias) + tuple(f_vias),
    )
    (
        e_tracks,
        e_vias,
        e_missing,
        e_done,
        e_need,
        e_reasons,
        e_spans,
    ) = _emit_escapes(
        d_sig,
        ir,
        deferred,
        pose_a,
        frules,
        esc_copper,
        int(ir.n_layers),
        min_via_dia=min_via_dia,
        min_via_drill=min_via_drill,
        mesh=mesh_sink[0] if mesh_sink else None,
        joined_pours=joined,
        fixed_copper=fixed_copper,
    )
    missing = e_missing
    tracks = tuple(rres.tracks) + tuple(f_tracks) + tuple(e_tracks)
    vias = tuple(rres.vias) + tuple(f_vias) + tuple(e_vias)
    pours: list[dict[str, Any]] = []
    if footprints is not None:
        # the pre-escape pour pass is the final one when no escape
        # copper exists — otherwise the pour runs again over all of it
        pours = (
            pours_pre
            if pours_pre is not None and not e_tracks and not e_vias
            else _plane_pours(
                ir,
                tracks,
                vias,
                d,
                pose_a,
                net_rules,
                footprints,
                fixed_copper,
                hole_clearance_mm,
            )
        )
        # round 8: ``joined`` gated each site on the PRE-escape pour —
        # a later escape's antipad can cut the neck an earlier escape
        # joined through. Re-verify every emitted escape against the
        # final pour (the same primitive set — pre-escape copper — so
        # two escapes on one otherwise-isolated piece cannot prop each
        # other up); a pad whose piece severed is dropped back to the
        # named finding and the pour re-run without its copper. Round 9:
        # an EMPTY final pour re-verifies too — every ``joined_f`` piece
        # is gone, so every escape drops (the old ``pours and`` kept
        # them); and the piece's layer must lie inside the barrel span
        # the stub ended on (``e_spans``).
        if e_done:
            joined_f = _joined_plane_pieces(
                d,
                ir,
                planes,
                list(rres.tracks) + f_tracks,
                list(rres.vias) + f_vias,
                pose_a,
                net_rules,
                footprints,
                fixed_copper,
                hole_clearance_mm,
                pours=pours,
            )
            sites = [tuple(t.segments[-1]["end"]) for t in e_tracks]
            keep: list[int] = []
            dropped: list[tuple[int, int]] = []
            for i, (k, pin) in enumerate(e_done):
                pt = Point(float(sites[i][0]), float(sites[i][1]))
                slo, shi = e_spans.get((k, pin), (0, int(ir.n_layers) - 1))
                if any(
                    slo <= lay2 <= shi and pc.intersects(pt)
                    for lay2, pc in joined_f.get(k, ())
                ):
                    keep.append(i)
                else:
                    dropped.append((k, pin))
            if dropped:
                drop_at = {
                    sites[i] for i, kp in enumerate(e_done) if kp in set(dropped)
                }
                e_tracks = [e_tracks[i] for i in keep]
                e_vias = [v for v in e_vias if (float(v.x), float(v.y)) not in drop_at]
                e_done = [e_done[i] for i in keep]
                for k, pin in dropped:
                    e_missing.append((k, pin))
                    e_reasons[(k, pin)] = "isolated_pour"
                missing = e_missing
                tracks = tuple(rres.tracks) + tuple(f_tracks) + tuple(e_tracks)
                vias = tuple(rres.vias) + tuple(f_vias) + tuple(e_vias)
                pours = _plane_pours(
                    ir,
                    tracks,
                    vias,
                    d,
                    pose_a,
                    net_rules,
                    footprints,
                    fixed_copper,
                    hole_clearance_mm,
                )
    tree_tracks, tree_vias, tree_ok = _plane_trees(
        d,
        ir,
        planes,
        pose_a,
        tracks,
        vias,
        f_vias,
        e_tracks,
        e_done,
        e_spans,
        net_rules,
        min_via_dia,
        min_via_drill,
        min_via_ring,
        event,
    )
    if tree_ok:
        tracks = tracks + tuple(tree_tracks)
        vias = vias + tuple(tree_vias)
        if pours:
            treed_names = {str(d.net_name[k]) for k in tree_ok}
            pours = [p for p in pours if str(p.get("net")) not in treed_names]
        if event is not None:
            event(
                f"route: plane-tree — {len(tree_ok)}/{len(planes)} net(s) treed, "
                f"{len(pours)} pour fragment(s) remain"
            )
    rres = dataclasses.replace(
        rres, tracks=tracks, vias=vias, pours=tuple(rres.pours) + tuple(pours)
    )
    plan = dataclasses.replace(
        plan,
        planes=tuple(sorted(planes.items())),
        fanout_missing=tuple(missing),
        fanout_fallback=tuple(fell_back),
        fanout_escape=tuple(e_done),
        fanout_missing_len=e_need,
        fanout_missing_reason=e_reasons,
    )
    return plan, rres


def _route_signals(
    d: TopoDesign,
    ir: Any,
    *,
    pose: Any = None,
    net_rules: Mapping[str, NetRules] | None = None,
    min_via_dia: float | None = None,
    min_via_drill: float | None = None,
    min_via_ring: float | None = None,
    event: Callable[[str], None] | None = None,
    masks: BoardMasks | None = None,
    mesh_sink: list[Any] | None = None,
    hv_weight: float = 0.0,
) -> tuple[RoutePlan, Any]:
    """Mesh → wires → plan → copper for one pose — the route half every
    caller (the ``placer='topo'`` job, ``pcb.local.topo_place``) shares.

    ``pose`` defaults to ``d.pose0``: a routing design built with
    ``design_from_ir(route_planes=True)`` over an IR whose poses were
    already settled reads it straight. A caller holding the anneal's
    ``best_pose`` (``pcb.local``) passes it — the mesh is rebuilt at the
    routed pose, not the authored one. ``net_rules`` feeds BOTH the wire
    corridor widths and the emitted track widths, per net name — pass
    the ROUTE-stage resolution
    (:func:`precis.pcb.session.route_net_rules`), whose current-derived
    IPC-2221 widths are dropped: those are ``widen_power`` targets, not
    routing demands.

    The emitted :class:`RealizeResult` DROPS every branch an unresolved
    same-layer crossing still names (:attr:`RoutePlan.crossings`): that
    copper is a real short, never committable — the plan keeps reporting
    it so the caller can fail the net, but no caller may persist or gate
    on it.

    ``event`` (gr513870's per-round progress) receives one line per
    stage plus each of ``assign_layers``' search rounds — ``None``
    keeps the route silent; the job wires it to ``job_event`` chunks,
    the laptop path exposes it as ``progress=``.

    ``masks`` (pcb-layer-argmin) — the two-kernel mask set at the
    ROUTED pose; forwarded to ``assign_layers`` as the corridor-cost
    input.  The caller builds it per route attempt (pose-fixed inside
    one call); ``None`` keeps the legacy search order.

    ``hv_weight`` (pcb-topo-hv-preference, default 0.0/off) is forwarded
    to both ``build_wires`` and ``plan_route`` unchanged.

    Two passes (:attr:`RoutePlan.withdrawn`): the first pass's withdrawn
    nets (crossings, stranded, no via site, short-risk, severed,
    off-angle) are removed — their ``net_pins`` emptied — and the
    survivors are routed again without those nets' taut polylines in
    the corridor, spoke and chord passes. Measured on taz-leaf-local-63:
    SDA 16 → 2 segments, SCL 12 → 3, LBST 10 → 4, VREF_SAMP 4 → 2, with
    GND (a 35-branch failed tree) no longer shaping them. The second
    pass is kept only when it withdraws no survivor — when it strands
    one, pass 1 stands for the survivors and the failed nets still get
    their re-route rounds against the survivors' pass-1 copper
    (:func:`_strip_failed`); the withdrawn nets' final pieces and
    problems come from their last attempt, re-indexed behind the kept
    pass's branches."""
    from precis.pcb.currents import require_net_currents
    from precis.pcb.topoplace.mesh import MeshConflictError

    require_net_currents(zip(ir.net_name, ir.net_current_a))
    kw: dict[str, Any] = dict(
        pose=pose,
        net_rules=net_rules,
        min_via_dia=min_via_dia,
        min_via_drill=min_via_drill,
        min_via_ring=min_via_ring,
        event=event,
        masks=masks,
        mesh_sink=mesh_sink,
        hv_weight=hv_weight,
    )
    plan, rres = _route_pass(d, ir, **kw)
    failed = _plan_failed_nets(plan)
    survivors = {p.net for p in plan.pieces} - failed
    if not failed or not survivors:
        return plan, rres
    if event is not None:
        event(
            f"route: pass 2 — {len(failed)} net(s) withdrawn, "
            f"re-routing {len(survivors)} survivor(s) without them"
        )

    def _only(nets: set[int]) -> tuple[np.ndarray, ...]:
        return tuple(
            pins if k in nets else np.zeros(0, dtype=np.int64)
            for k, pins in enumerate(d.net_pins)
        )

    try:
        plan2, rres2 = _route_pass(
            dataclasses.replace(d, net_pins=_only(survivors)), ir, **kw
        )
    except CorridorOverflow:
        return plan, rres
    if _plan_failed_nets(plan2):
        # a survivor fails without its neighbours — pass 1 stands for
        # the survivors, but the failed set still gets its re-route
        # rounds below, against the survivors' pass-1 copper (its own
        # speculative pieces stripped so they neither freeze it for the
        # co-batch nets nor double it when a round lands)
        kept_plan, kept_rres = _strip_failed(plan, rres, failed, d, ir)
    else:
        kept_plan, kept_rres = plan2, rres2
    # passes 3..: the withdrawn nets again, in rounds, each against ALL
    # copper kept so far frozen as same-net-passable zones — a rip-up
    # and re-route of the failed set. A round routes the remaining nets
    # together; the ones that come out whole are then routed ONCE MORE
    # alone (the pass-2 rule inside the round: a net that fails in the
    # round must not shape the ones that succeed — DEC5 kept a 1.5 mm
    # B.Cu hop under C10 for a crossing with a net that then failed),
    # frozen, and the rest go to the next round until a round lands
    # nothing. The per-branch guard keeps what joins pads, so a tree
    # still lands its easy branches when it fails.
    remaining = set(failed)
    last_plan: RoutePlan | None = None
    last_rres: Any = None
    pass_no = 3
    # the 2-3-pin nets get their rounds before the trees join: a tree's
    # taut polyline across the board fails the trivial nets around it
    small_phase = True
    for _round in range(2 * _REROUTE_ROUNDS):
        batch = (
            {k for k in remaining if len(d.net_pins[k]) <= 3}
            if small_phase
            else set(remaining)
        )
        if not batch:
            if small_phase:
                small_phase = False
                continue
            break
        try:
            frozen = _copper_zones(kept_rres, d, ir, pose)
            d3 = dataclasses.replace(
                d, net_pins=_only(batch), zones=tuple(d.zones) + tuple(frozen)
            )
            if event is not None:
                event(
                    f"route: pass {pass_no} — {len(batch)} withdrawn net(s) "
                    f"re-routed against {len(frozen)} frozen copper piece(s)"
                )
            pass_no += 1
            plan3, rres3 = _route_pass(d3, ir, **kw)
        except (CorridorOverflow, MeshConflictError):
            # the frozen copper may defeat the CDT's edge recovery — the
            # remaining nets stay withdrawn rather than routed without it
            break
        last_plan, last_rres = plan3, rres3
        landed = batch - _plan_failed_nets(plan3)
        if not landed:
            if small_phase:
                small_phase = False
                continue
            break
        if landed != batch:
            try:
                d4 = dataclasses.replace(d3, net_pins=_only(landed))
                plan4, rres4 = _route_pass(d4, ir, **kw)
                pass_no += 1
                if not (_plan_failed_nets(plan4) & landed):
                    plan3, rres3 = plan4, rres4
            except (CorridorOverflow, MeshConflictError):
                pass
        kept_plan, kept_rres = _merge_pass(
            kept_plan, kept_rres, plan3, rres3, landed, d, ir
        )
        remaining -= landed
    still = set(remaining)
    if still:
        # the still-failed nets' last attempt rides along for the record
        # (plan pieces/problems) and its simple partial copper (below);
        # when every round raised there IS no last attempt — pass 1's
        # own records stand in, the pieces and stranded reports the old
        # bail kept whole (G2h review-3 M3)
        kept_plan, kept_rres = _merge_pass(
            kept_plan,
            kept_rres,
            last_plan if last_plan is not None else plan,
            last_rres if last_rres is not None else rres,
            still,
            d,
            ir,
        )
    # A net that still fails keeps only its SIMPLE partial copper — the
    # single-layer branches that join two of its pads in a few legs (the
    # C4–C14 GND class). Its via hops and long detours are speculative
    # copper toward pads it never reached (RF_FEED: a 40-leg B.Cu snake
    # along the leaf edge) and go with the net.
    from precis.pcb.ir import pin_point

    still_names = {str(d.net_name[k]) for k in still if k < len(d.net_name)}
    # a refused cut is a fact about the FAILED nets' demand — it
    # survives onto the report even when the kept pass (re-routed
    # without those nets) cut clean, but only while a net it names is
    # still failed: a net a later round landed owes no report (M2)
    extra_cuts = tuple(
        c
        for c in plan.overflow
        if c.edge not in {c2.edge for c2 in kept_plan.overflow}
        and not still_names.isdisjoint(c.nets)
    )
    if extra_cuts:
        kept_plan = dataclasses.replace(
            kept_plan, overflow=tuple(kept_plan.overflow) + extra_cuts
        )
    pad_xy: dict[int, list[tuple[float, float]]] = {}
    for pid in range(ir.n_pins):
        pt = pin_point(ir, pid)
        if pt is not None and int(ir.pin_net[pid]) >= 0:
            pad_xy.setdefault(int(ir.pin_net[pid]), []).append(
                (float(pt[0]), float(pt[1]))
            )

    def _pad_to_pad(t: Any) -> bool:
        a = tuple(float(v) for v in t.segments[0]["start"])
        b = tuple(float(v) for v in t.segments[-1]["end"])
        pads_ = pad_xy.get(int(t.net_id), [])
        return (
            len(t.segments) <= _PARTIAL_MAX_SEGMENTS
            and any(math.dist(a, p) < 1e-3 for p in pads_)
            and any(math.dist(b, p) < 1e-3 for p in pads_)
        )

    kept_rres = dataclasses.replace(
        kept_rres,
        tracks=tuple(
            t
            for t in kept_rres.tracks
            if str(ir.net_name[t.net_id]) not in still_names or _pad_to_pad(t)
        ),
        vias=tuple(
            v for v in kept_rres.vias if str(ir.net_name[v.net_id]) not in still_names
        ),
    )
    return dataclasses.replace(kept_plan, withdrawn=tuple(sorted(still))), kept_rres


#: The most legs a still-failed net's partial track may keep (route_design).
_PARTIAL_MAX_SEGMENTS = 6
#: Rip-up rounds over the withdrawn set (route_design passes 3..).
_REROUTE_ROUNDS = 4


def _merge_pass(
    base: RoutePlan,
    base_r: Any,
    extra: RoutePlan,
    extra_r: Any,
    nets: set[int],
    d: TopoDesign,
    ir: Any,
) -> tuple[RoutePlan, Any]:
    """``base`` plus ``extra``'s records for ``nets`` — branch indices of
    ``extra`` shifted behind ``base``'s so the two index spaces never
    collide; copper joined by net name (``RealizeResult`` nets are IR
    indices, plan nets design indices)."""
    n0 = len(base.layers)
    names = {str(d.net_name[k]) for k in nets if k < len(d.net_name)}
    plan = dataclasses.replace(
        base,
        pieces=tuple(base.pieces)
        + tuple(
            dataclasses.replace(p, branch=p.branch + n0)
            for p in extra.pieces
            if p.net in nets
        ),
        vias=tuple(base.vias)
        + tuple(
            dataclasses.replace(v, branch=v.branch + n0)
            for v in extra.vias
            if v.net in nets
        ),
        layers=tuple(base.layers) + tuple(extra.layers),
        # Keep the same concatenated index space even for discarded branches.
        # Hand-built plans without provenance contribute unknown (empty) rows.
        branch_lineage=(base.branch_lineage or (frozenset(),) * len(base.layers))
        + (extra.branch_lineage or (frozenset(),) * len(extra.layers)),
        branch_ids=(base.branch_ids or (None,) * len(base.layers))
        + (extra.branch_ids or (None,) * len(extra.layers)),
        crossings=tuple(base.crossings)
        + tuple((a + n0, b + n0) for a, b in extra.crossings),
        unreachable=tuple(
            sorted(set(base.unreachable) | (set(extra.unreachable) & nets))
        ),
        stranded=tuple(base.stranded)
        + tuple(u for u in extra.stranded if u.net in nets),
        no_via=tuple(base.no_via) + tuple(b + n0 for b in extra.no_via),
        # ``extra``'s siting evidence shifts with its branch indices —
        # without it a rerouted board's plan names no_via branches whose
        # ``why`` never survived the merge.
        no_via_reason={
            **base.no_via_reason,
            **{b + n0: r for b, r in extra.no_via_reason.items()},
        },
        guarded=tuple(sorted(set(base.guarded) | (set(extra.guarded) & nets))),
        severed=tuple(sorted(set(base.severed) | (set(extra.severed) & nets))),
        via_need={**base.via_need, **extra.via_need},
        # one record per edge — rounds re-cut the same edge with
        # different demand; the LATER pass's measurement stands (M2)
        overflow=tuple(
            c for c in base.overflow if c.edge not in {c2.edge for c2 in extra.overflow}
        )
        + tuple(extra.overflow),
    )
    rres = dataclasses.replace(
        base_r,
        tracks=tuple(base_r.tracks)
        + tuple(t for t in extra_r.tracks if str(ir.net_name[t.net_id]) in names),
        vias=tuple(base_r.vias)
        + tuple(v for v in extra_r.vias if str(ir.net_name[v.net_id]) in names),
        pours=tuple(base_r.pours) + tuple(extra_r.pours),
        warnings=tuple(base_r.warnings) + tuple(extra_r.warnings),
    )
    return plan, rres


def _strip_failed(
    plan: RoutePlan, rres: Any, failed: set[int], d: TopoDesign, ir: Any
) -> tuple[RoutePlan, Any]:
    """``plan``/``rres`` minus the ``failed`` nets' records and copper —
    the survivors' share of a pass the withdrawn nets then get
    re-routed against (:func:`_route_signals`' pass-2 bail). Their own
    pieces go too: the rounds re-emit whatever lands, and what never
    lands falls back on its last attempt's record — keeping the first
    pass's would double the copper. ``layers`` stays whole: branch
    indices must keep naming it for :func:`_merge_pass`."""
    names = {str(d.net_name[k]) for k in failed if k < len(d.net_name)}
    net_by_branch = {p.branch: p.net for p in plan.pieces}
    plan = dataclasses.replace(
        plan,
        pieces=tuple(p for p in plan.pieces if p.net not in failed),
        vias=tuple(v for v in plan.vias if v.net not in failed),
        crossings=tuple(
            pair
            for pair in plan.crossings
            if net_by_branch.get(pair[0]) not in failed
            and net_by_branch.get(pair[1]) not in failed
        ),
        unreachable=tuple(k for k in plan.unreachable if k not in failed),
        stranded=tuple(u for u in plan.stranded if u.net not in failed),
        no_via=tuple(b for b in plan.no_via if net_by_branch.get(b) not in failed),
        no_via_reason={
            b: r
            for b, r in plan.no_via_reason.items()
            if net_by_branch.get(b) not in failed
        },
        guarded=tuple(k for k in plan.guarded if k not in failed),
        severed=tuple(k for k in plan.severed if k not in failed),
        via_need={k: v for k, v in plan.via_need.items() if k not in failed},
    )
    rres = dataclasses.replace(
        rres,
        tracks=tuple(t for t in rres.tracks if str(ir.net_name[t.net_id]) not in names),
        vias=tuple(v for v in rres.vias if str(ir.net_name[v.net_id]) not in names),
    )
    return plan, rres


def _edge_zone(d: TopoDesign, s_min: float) -> tuple[Polygon, TopoZone] | None:
    """The board's OUTSIDE as a zone obstacle for the spoke/chord passes:
    the outline's exterior grown by ``edge_clearance − min clearance``,
    so a branch holding its usual ``s + w/2`` off it holds
    ``edge_clearance + w/2`` off the real edge — the DRC's bar. Never
    added to ``d.zones`` (the mesh would read the ring as a region)."""
    edge = float(getattr(d, "edge_clearance_mm", 0.0) or 0.0)
    if edge <= 0.0 or len(d.outline) < 3:
        return None
    from shapely.geometry import box

    outline = Polygon(np.asarray(d.outline, dtype=float))
    if outline.is_empty:
        return None
    x0, y0, x1, y1 = outline.bounds
    ext = box(x0 - 5.0, y0 - 5.0, x1 + 5.0, y1 + 5.0).difference(outline)
    grow = edge - s_min
    if grow > 0.0:
        ext = ext.buffer(grow, join_style=2)
    if ext.is_empty:
        return None
    return ext, TopoZone(
        kind="board_edge", ring=np.asarray(d.outline), name="board edge"
    )


def _copper_zones(
    rres: Any, d: TopoDesign, ir: Any, pose: Any = None
) -> list[TopoZone]:
    """Realized tracks and vias as ``fixed_copper`` :class:`TopoZone`
    rows (net-tagged, so the same net may still touch them) — the
    survivors' copper frozen for :func:`route_design`'s pass 3.

    Each piece is clipped to outside every pad of its layer (the copper
    the mesh will constrain, at ``pose``, default ``d.pose0``): a
    fanout stub starts at its pad's centre, and a zone vertex inside a
    pad ring would collapse that pad's wall in the mesh. The clipped
    stub still touches the pad, so the pad and its stub stay one
    obstacle."""
    from shapely.ops import unary_union

    from precis.pcb import realize as pcb_realize
    from precis.pcb.topoplace.mesh import world_pads

    layers = [str(layer.get("name") or "") for layer in ir.stackup]
    name_to_idx = {n: i for i, n in enumerate(layers)}
    n_layers = int(ir.n_layers)
    pads = world_pads(d, np.asarray(d.pose0 if pose is None else pose, dtype=float))
    lands: dict[int | None, Any] = {}

    def lands_on(layer: int | None) -> Any:
        """The copper union of the pads with copper on ``layer``. A through
        barrel (``None``) is cut only by the drilled pads, which are
        copper on every layer it spans — under an SMD pad its barrel is
        still copper on the layers that pad does not occupy."""
        if layer not in lands:
            lands[layer] = unary_union(
                [
                    p.poly
                    for p in pads
                    if p.side == 2
                    or (
                        layer is not None
                        and layer == (n_layers - 1 if p.side == 1 else 0)
                    )
                ]
            )
        return lands[layer]

    from precis.pcb import clearance as pcb_clearance

    out: list[TopoZone] = []
    for row in pcb_realize.result_copper_rows(rres, ir, layers, derived=True):
        body = pcb_clearance.body_of(dict(row))
        if body is None:
            continue
        # a buffered track is a rounded sliver with dozens of ring
        # vertices; the CDT's edge recovery wants a coarse ring — but
        # never an inscribed one: every route-path oracle reads the
        # ring (the mesh grows it, the emit indices and the anti-short
        # guard measure it), and a polygon whose chords dip inside the
        # copper lets a wire pulled taut to its edge land inside its
        # own clearance — the track↔fanout-via drc_rule warns. The
        # kernel's outward polygon holds the copper within
        # ``_COPPER_ZONE_SAG_MM`` of it.
        geom = pcb_clearance.outward_polygon(body, 0.0, _COPPER_ZONE_SAG_MM)
        if geom.is_empty:
            continue
        layer = str(row.get("layer") or "")
        # a via barrel spans every layer — its zone must too
        is_via = str(row.get("ctype") or "") == "via"
        idx = None if is_via or layer not in name_to_idx else name_to_idx[layer]
        geom = geom.difference(lands_on(idx))
        for part in getattr(geom, "geoms", (geom,)):
            if part.geom_type != "Polygon" or part.area <= 1e-6:
                continue
            out.append(
                TopoZone(
                    kind="fixed_copper",
                    ring=np.asarray(part.exterior.coords, dtype=np.float64),
                    layers=() if idx is None else (idx,),
                    net=str(row.get("net") or ""),
                    name=f"pass-2 copper {row.get('ctype') or '?'}",
                )
            )
    return out


def ring_rule(rules_t: Sequence[tuple[float, ...]]) -> tuple[float, float]:
    """The ``(trace, clearance)`` the mesh stands its rings off the copper
    for: the finest net rule's. A net with a coarser rule keeps
    :func:`wires._pad_eval`'s edge closure at its own, larger radius for
    PADS only; zone and board rings stay at the finest rule's stand-off."""
    if not rules_t:
        return 0.0, 0.0
    return _wires.finest_rule(rules_t)


def _via_choice_rules(
    ir: Any,
    rules: Mapping[str, NetRules],
    min_via_dia: float | None,
    min_via_drill: float | None,
    min_via_ring: float | None = None,
) -> tuple[dict[str, NetRules], dict[str, float | None]]:
    """``rules`` with each net's ``via_dia_mm``/``via_drill_mm`` replaced
    by the route-stage choice — :func:`precis.pcb.rules.route_via` on the
    net's IR current (:attr:`precis.pcb.ir.PcbIR.net_current_a`) against
    the fab-floor pair ``(min_via_dia, min_via_drill)`` — and the
    per-net current map the choice read (``via_need`` re-rates a
    fallback-sited barrel at its own diameter after the plan exists).
    Every downstream reader of the map — ``rules_t``'s via entry (the
    search's fit), ``plan_route``'s first ``_site_via`` diameter, the
    emitted dia/drill pair — then sees the chosen barrel with no second
    code path. ``min_via_ring`` is the fab's annular-ring minimum: the
    emitted drill never leaves the chosen diameter less ring than
    ``dia - 2 x ring_min`` (a choice drill of ``None`` falls back to the
    net rule's, clamped the same way)."""
    cur = {
        str(n): net_current_a_or_none(float(c))
        for n, c in zip(getattr(ir, "net_name", ()), getattr(ir, "net_current_a", ()))
    }
    out: dict[str, NetRules] = {}
    for name, nr in rules.items():
        choice = route_via(
            cur.get(name),
            (min_via_dia, min_via_drill),
            nr,
            ring_min_mm=min_via_ring,
        )
        if choice is None:
            out[name] = nr
            continue
        drill = choice.drill_mm if choice.drill_mm is not None else nr.via_drill_mm
        if drill is not None and min_via_ring is not None:
            cap = float(choice.dia_mm) - 2.0 * float(min_via_ring)
            drill = None if cap <= 0.0 else min(float(drill), cap)
        out[name] = dataclasses.replace(
            nr,
            via_dia_mm=choice.dia_mm,
            via_drill_mm=drill,
        )
    return out, cur


def _via_need(
    plan: RoutePlan,
    d: TopoDesign,
    rules: Mapping[str, NetRules],
    cur: Mapping[str, float | None],
) -> dict[int, int]:
    """Design-net index → barrels the net's current needs PER LAYER HOP,
    kept only where it is > 1 — and only for nets that actually hop
    (a ``ViaSite`` in the plan): a stitched group is not planted yet, so
    the router sites one barrel and this field names the shortfall rather
    than committing a silent single via. The count is rated at the
    SITED diameter: a hop ``_site_via`` dropped to the fab-minimum
    fallback (``via_dia_mm`` under the net rule's) carries a smaller
    barrel than the choice asked, so its group is larger — an
    under-rated hop is named, never hidden. A site with ``dia_mm`` 0 is
    the net rule's own barrel (:class:`ViaSite`)."""
    need: dict[int, int] = {}
    for v in plan.vias:
        name = str(d.net_name[v.net]) if v.net < len(d.net_name) else str(v.net)
        dia = float(v.dia_mm)
        if dia <= 0.0:
            nr = rules.get(name)
            dia = float(nr.via_dia_mm) if nr is not None and nr.via_dia_mm else 0.0
        n = via_count_for_current(cur.get(name), dia)
        if n > need.get(int(v.net), 0):
            need[int(v.net)] = n
    return {k: n for k, n in need.items() if n > 1}


def _route_pass(
    d: TopoDesign,
    ir: Any,
    *,
    pose: Any = None,
    net_rules: Mapping[str, NetRules] | None = None,
    min_via_dia: float | None = None,
    min_via_drill: float | None = None,
    min_via_ring: float | None = None,
    event: Callable[[str], None] | None = None,
    masks: BoardMasks | None = None,
    mesh_sink: list[Any] | None = None,
    hv_weight: float = 0.0,
) -> tuple[RoutePlan, Any]:
    """One mesh → wires → plan → copper pass; :func:`route_design`'s body."""
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import build_wires

    mesh_pose = d.pose0 if pose is None else pose
    rules, via_cur = _via_choice_rules(
        ir, net_rules or {}, min_via_dia, min_via_drill, min_via_ring
    )
    rules_t = tuple(
        (
            float(rules[d.net_name[k]].track_width_mm),
            float(rules[d.net_name[k]].clearance_mm),
            float(rules[d.net_name[k]].via_dia_mm or _VIA_DIA_MM),
        )
        if k < len(d.net_name) and d.net_name[k] in rules
        else (_DEFAULT.track_width_mm, _DEFAULT.clearance_mm, _VIA_DIA_MM)
        for k in range(len(d.net_pins))
    )
    trace_mm, clearance_mm = ring_rule(rules_t)
    mesh = build_mesh(d, mesh_pose, trace_mm=trace_mm, clearance_mm=clearance_mm)
    if mesh_sink is not None:
        mesh_sink.append(mesh)
    if event is not None:
        event(f"route: mesh — {len(mesh.tris)} tris, {len(mesh.edges)} edges")
    wires = build_wires(
        mesh,
        d,
        ir.n_layers,
        rules_t,
        pose=mesh_pose,
        net_clearance_by_name={
            name: float(nr.clearance_mm) for name, nr in rules.items()
        },
        masks=masks,
        hv_weight=hv_weight,
    )
    routed_over = tuple(c for c in wires.overflow if not c.refused)
    if routed_over:
        # honest refusal (gr520904): a channel whose routed demand
        # exceeds capacity is named with its owners — never silently
        # crammed. A cut that is only refused-negotiation demand stays
        # on the plan's record; the board still routes (G2h ruling).
        raise CorridorOverflow(routed_over)
    if event is not None:
        event(
            f"route: wires — {len(wires.branches)} branch(es) "
            f"over {len(d.net_pins)} net(s)"
        )
    # gr520900: the taut paths share obstacle vertices and edge
    # crossings — the spoke offsets fan coincident copper to legal
    # spacing before the plan freezes it.
    paths = _spoke_offsets(d, mesh, wires, ir.n_layers, net_rules=rules, pose=mesh_pose)
    plan = plan_route(
        d,
        mesh,
        wires,
        ir.n_layers,
        net_rules=rules,
        pose=mesh_pose,
        min_via_dia=min_via_dia,
        min_via_drill=min_via_drill,
        paths=paths,
        event=event,
        masks=masks,
        hv_weight=hv_weight,
    )
    if wires.overflow:
        plan = dataclasses.replace(plan, overflow=wires.overflow)
    via_need = _via_need(plan, d, rules, via_cur)
    if via_need:
        plan = dataclasses.replace(plan, via_need=via_need)
    if event is not None:
        event(
            f"route: plan — {len(plan.pieces)} piece(s), "
            f"{len(plan.vias)} via(s), {len(plan.crossings)} unresolved "
            f"crossing(s), {len(plan.unreachable)} unreachable"
        )
    # Any angle, few segments (Reto 2026-11-01): every emitted piece is
    # drawn once, here, as the copper it will commit — the guard below
    # and realize_result read the same points.
    bad = {b for pair in plan.crossings for b in pair}
    plan = _emit_shapes(
        plan, d, ir, ir.n_layers, np.asarray(mesh_pose), rules, skip=bad
    )
    emit = plan
    if bad:
        emit = dataclasses.replace(
            plan,
            pieces=tuple(p for p in plan.pieces if p.branch not in bad),
            vias=tuple(v for v in plan.vias if v.branch not in bad),
        )
    # gr524239: the order-based crossing detector misses geometric
    # shorts (a taut polyline crossing a foreign pad, an overlap without
    # order inversion), so the emit set is checked against foreign
    # copper before it may be realized. A flagged net is withdrawn
    # whole — never commit shorting copper; the plan records it so
    # sketch_rows reports the net failed.
    gbr = _guarded_branches(emit, d, ir, ir.n_layers, np.asarray(mesh_pose), rules)
    guarded = set(gbr)
    if guarded:
        bad_br = {(n, b) for n, bs in gbr.items() for b in bs}
        emit = dataclasses.replace(
            emit,
            pieces=tuple(p for p in emit.pieces if (p.net, p.branch) not in bad_br),
            vias=tuple(v for v in emit.vias if (v.net, v.branch) not in bad_br),
        )
        plan = dataclasses.replace(plan, guarded=tuple(sorted(guarded)))
        if event is not None:
            event(
                f"route: guard — {len(bad_br)} branch(es) of {len(guarded)} "
                "net(s) withdrawn (short risk)"
            )
    # gr537831: a net is "realized" only when its committed copper —
    # pieces, vias and the pads they must reach — is ONE connected
    # piece. A severed emit (the edgeless F↔B hop ending on the wrong
    # layer) is withdrawn whole like the anti-short guard, never
    # committed as a falsely routed net.
    severed = _severed_nets(emit, d, ir, ir.n_layers, np.asarray(mesh_pose), rules)
    if severed:
        # per-component withdrawal: the copper that joins >=2 of the
        # net's pads is real and stays — a stranded net keeps its routed
        # branches; only floating/dangling components are dropped. A net
        # whose every component is broken loses its copper whole.
        drop_p = {i for dp, _dv in severed.values() for i in dp}
        drop_v = {i for _dp, dv in severed.values() for i in dv}
        emit = dataclasses.replace(
            emit,
            pieces=tuple(p for i, p in enumerate(emit.pieces) if i not in drop_p),
            vias=tuple(v for i, v in enumerate(emit.vias) if i not in drop_v),
        )
        plan = dataclasses.replace(plan, severed=tuple(sorted(severed)))
        if event is not None:
            event(f"route: severed — {len(severed)} net(s) trimmed (unconnected emit)")
    rres = realize_result(emit, d, ir, ir.n_layers, net_rules=rules)
    if event is not None:
        event(f"route: emit — {len(rres.tracks)} track(s), {len(rres.vias)} via(s)")
    return plan, rres


def sketch_rows(
    plan: RoutePlan,
    d: TopoDesign,
    ir: Any,
    layer_names: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """The ``Store.pcb_routes_write`` rows for one routed plan — one row
    per IR net, keyed by net name, in the ``{tree, topology,
    layer_assign, status, fail, note}`` shape the deleted job wrote.
    ``tree``/``layer_assign`` entries key a connection by its two pins
    (``refdes.pin``, :func:`precis.pcb.session._pin_key`'s convention),
    never by the ephemeral branch index; ``topology`` stays empty — the
    topo placer's layer choice is a piece geometry fact, not a pinned
    side override for a later run to re-apply.

    The status ladder: a <2-member net is ``realized`` with the dangling
    note (legal, nothing to route — the wedge fix the deleted job
    documented); a net carrying a stranded component
    (:attr:`RoutePlan.stranded`) or a branch in an unresolved
    ``crossings`` pair is ``failed`` with ``unrouted`` /
    ``same-layer-crossing`` ``fail.problems`` — the same reason strings
    that job emitted, so ``note``-reading consumers (the check view's
    ``unrouted`` findings, ``view='route-status'``) parse it unchanged;
    a net the anti-short guard withdrew joins them as ``failed:
    short-risk`` (gr524239); a net with emitted pieces is ``realized``;
    anything left is ``sketched``. A net absent from the route
    design's ``net_pins`` gets no row — under
    ``design_from_ir(route_planes=True)`` only <2-member nets drop, but
    this never guesses. A plane assignment realizes nothing (the
    no-pours ruling) — it is board-script metadata, not copper."""
    from precis.pcb import DANGLING_NET_NOTE
    from precis.pcb.ir import net_member_counts
    from precis.pcb.session import _pin_key

    def pin_key(pin_id: int) -> str:
        return _pin_key(
            str(ir.instance_refdes[int(ir.pin_instance[pin_id])]),
            str(ir.pin_label[pin_id]),
        )

    net_by_branch = {p.branch: p.net for p in plan.pieces}
    branch_pair: dict[int, tuple[int, int]] = {}
    for p in plan.pieces:
        branch_pair.setdefault(p.branch, (p.pin_a, p.pin_b))
    stranded_by_net = {u.net: u for u in plan.stranded}
    planes_of = dict(getattr(plan, "planes", ()))
    fan_missing = tuple(getattr(plan, "fanout_missing", ()))
    name_to_k = {str(n): k for k, n in enumerate(d.net_name)}
    member_counts = net_member_counts(ir)
    rows: dict[str, dict[str, Any]] = {}
    for net_id in range(ir.n_nets):
        name = str(ir.net_name[net_id])
        if member_counts.get(net_id, 0) < 2:
            rows[name] = {
                "tree": [],
                "topology": [],
                "layer_assign": [],
                "status": "realized",
                "fail": None,
                "note": DANGLING_NET_NOTE,
            }
            continue
        k = name_to_k.get(name)
        if k is None:
            continue
        if k in planes_of:
            # gr524239: a plane net is not wired — every SMD pad drops to
            # its plane by a fanout via; a pad without a site fails it
            lay = planes_of[k]
            lname = str(layer_names[lay]) if 0 <= lay < len(layer_names) else "?"
            miss = [pin for kk, pin in fan_missing if kk == k]
            fail = (
                {
                    "problems": [
                        {
                            "reason": "no fanout via",
                            "message": "no via site next to "
                            + ", ".join(pin_key(p) for p in miss[:6])
                            + (f" (+{len(miss) - 6} more)" if len(miss) > 6 else ""),
                        }
                    ]
                }
                if miss
                else None
            )
            rows[name] = {
                "tree": [],
                "topology": [],
                "layer_assign": [],
                "status": "failed" if miss else "realized",
                "fail": fail,
                "note": (
                    f"failed: no fanout via — {fail['problems'][0]['message']}"
                    if fail
                    else f"plane {lname}: every pad drops to it by via"
                ),
            }
            continue
        branches_k = [b for b, n in net_by_branch.items() if n == k]
        problems: list[dict[str, Any]] = []
        u = stranded_by_net.get(k)
        if u is not None:
            pins = u.pins or (u.pin,)
            problems.append(
                {
                    "reason": "unrouted",
                    "message": "pins walled off: "
                    + ", ".join(pin_key(p) for p in pins),
                }
            )
        withdrawn = set(getattr(plan, "withdrawn", ()))
        for a, b in plan.crossings:
            if net_by_branch.get(a) != k and net_by_branch.get(b) != k:
                continue
            if withdrawn and k not in withdrawn:
                # a first-pass crossing with a since-withdrawn net — the
                # survivor was re-routed without it (route_design pass 2)
                continue
            other_branch = b if net_by_branch.get(a) == k else a
            other = str(d.net_name[net_by_branch[other_branch]])
            layer = int(plan.layers[a])
            lname = str(layer_names[layer]) if 0 <= layer < len(layer_names) else "?"
            problems.append(
                {
                    "reason": "same-layer-crossing",
                    "with": other,
                    "layer": layer,
                    "message": f"crosses {other} on {lname}",
                }
            )
        for b in plan.no_via:
            if net_by_branch.get(b) != k or b not in branch_pair:
                continue
            pa, pb = branch_pair[b]
            problems.append(
                {
                    "reason": "no via site",
                    "message": "no keepout-clean edge for a via between "
                    f"{pin_key(pa)} and {pin_key(pb)}",
                }
            )
        # duck-typed plans (the bench tests' SimpleNamespace) predate the
        # guarded field — absent means nothing was withdrawn.
        if k in getattr(plan, "guarded", ()):
            problems.append(
                {
                    "reason": "short-risk",
                    "message": "copper withdrawn: would short another net (emit guard)",
                }
            )
        if k in getattr(plan, "severed", ()):
            problems.append(
                {
                    "reason": "unconnected",
                    "message": "copper withdrawn: emitted copper is not one "
                    "connected piece (emit check)",
                }
            )
        if problems:
            status = "failed"
            kinds = sorted({str(p["reason"]) for p in problems})
            details = list(dict.fromkeys(str(p["message"]) for p in problems))
            note = f"failed: {', '.join(kinds)} — " + "; ".join(details[:3])
            if len(details) > 3:
                note += f" (+{len(details) - 3} more)"
        elif branches_k:
            status, note = "realized", None
        else:
            status, note = "sketched", None
        tree: list[dict[str, str]] = []
        layer_assign: list[dict[str, Any]] = []
        seen: set[str] = set()
        for b in branches_k:
            pa, pb = branch_pair[b]
            ka, kb = pin_key(pa), pin_key(pb)
            key = "|".join(sorted((ka, kb)))
            if key in seen:
                continue
            seen.add(key)
            tree.append({"a": ka, "b": kb})
            layer_assign.append({"a": ka, "b": kb, "layer": int(plan.layers[b])})
        rows[name] = {
            "tree": tree,
            "topology": [],
            "layer_assign": layer_assign,
            "status": status,
            "fail": {"problems": problems} if problems else None,
            "note": note,
        }
        need = int(getattr(plan, "via_need", {}).get(k, 1) or 1)
        if need > 1:
            # the current wants a stitched group per hop; the router
            # planted one barrel — the report names the shortfall, never
            # a silent single via.
            rows[name]["via_need"] = need
            rows[name]["note"] = (f"{note}; " if note else "") + (
                f"needs {need} vias per layer hop (planted 1)"
            )
    return rows


def _error_keys(findings: Iterable[Any]) -> Counter:
    """A finding multiset keyed ``(rule, sorted participant identities,
    layer)`` — :func:`precis.pcb.drc.finding_identity`, with the
    finding's ``where`` when it names no objects. A multiset, not a
    count: a widening step that erases one error and mints another must
    not hide behind an unchanged total (gr524392, reopened — widening a
    rail can bridge its own broken pieces, removing a connectivity
    error, while it eats the clearance to a foreign pad; the count
    stayed equal and the step was accepted — on prod a VIN_DC track
    widened to 0.5 mm came out touching pad U2/VSS at 0.000 mm).
    Several findings may legitimately share a key (two clearance hits
    between the same pair on one layer), so keys count with
    multiplicity: a second hit on an already-faulted pair is a NEW
    finding, not the old one seen twice."""
    from precis.pcb import drc as pcb_drc

    keys: Counter = Counter()
    for f in findings:
        rule, ids, layer = pcb_drc.finding_identity(f)
        keys[(rule, ids or (f.where,), layer)] += 1
    return keys


def widen_targets(
    ir: Any,
    route_rules: Mapping[str, NetRules],
    clearance_rules: Mapping[str, NetRules],
) -> tuple[tuple[int, ...], dict[int, float]]:
    """The widen stage's net set and per-net width targets (gr524239) —
    the ONE selection both ``widen_power`` callers (the ``placer='topo'``
    job and ``pcb.local.topo_place``) read, so job and kernel cannot
    drift.

    ``targets`` maps IR net id → the net's current-derived IPC-2221
    width — the ``clearance_rules`` resolution — for nets carrying a
    real ``est_current_a`` annotation (the net's own, or the member
    pin currents rolled up — :attr:`precis.pcb.ir.PcbIR.net_current_a`
    already carries either). The widen set is the plane/power-class
    rails (``ratsnest.PLANE_CLASSES`` or ``net_domain == "power"`` —
    GND and power route as normal nets and widen in the final stage)
    plus every net whose target beats the width it was BORN at (the
    ``route_rules`` resolution): a 1 mA signal net asks ~0.09 mm and
    is never in it, and a plane net WITH an estimate still gets only
    its current-driven target — the annotation outranks the class.
    A plane net WITHOUT an estimate enters the set with no target;
    :func:`widen_power` applies the legacy ruling to it — the widest
    step the oracle accepts — while a signal net with no estimate
    keeps its rule width (gr524239: widening follows estimated
    current, and only a rail class may serve free space). Ids come
    back sorted so the first rail widened is copper the next must
    clear, deterministically."""
    targets: dict[int, float] = {}
    for net_id in range(ir.n_nets):
        name = str(ir.net_name[net_id])
        cur = float(ir.net_current_a[net_id])
        if name in clearance_rules and math.isfinite(cur) and cur > 0.0:
            targets[net_id] = float(clearance_rules[name].track_width_mm)

    def born(net_id: int) -> float:
        name = str(ir.net_name[net_id])
        return (
            float(route_rules[name].track_width_mm) if name in route_rules else math.inf
        )

    def plane_or_power(net_id: int) -> bool:
        classes = getattr(ir, "net_class", ())
        domains = getattr(ir, "net_domain", ())
        return (
            net_id < len(classes)
            and str(classes[net_id] or "").strip().lower() in PLANE_CLASSES
        ) or (net_id < len(domains) and str(domains[net_id] or "") == "power")

    power_ids = tuple(
        net_id
        for net_id in range(ir.n_nets)
        if plane_or_power(net_id) or targets.get(net_id, 0.0) > born(net_id)
    )
    return power_ids, targets


def widen_power(
    result: Any,
    power_net_ids: Iterable[int],
    *,
    drc_findings: Callable[[Any], Iterable[Any]],
    targets: Mapping[int, float] | None = None,
    ladder: Sequence[float] = _WIDEN_LADDER,
) -> tuple[Any, dict[int, float]]:
    """The post-route power stage (the ruling): a rail claims its
    current-derived width where the DRC oracle allows, never below the
    net's rule width.

    ``power_net_ids`` are IR net ids (``RealizedTrack.net_id``);
    ``result``/the oracle traffic in :class:`precis.pcb.realize.
    RealizeResult`. ``drc_findings`` returns the ERROR findings of the
    caller's own DRC over the emitted copper, so "clearance allows" is
    exactly what the gate accepts. Baseline is the result AS GIVEN: a
    widened step is kept only when its findings are a sub-multiset of
    the baseline's — it adds NO new error finding (removing errors is
    fine; a board already carrying errors is not asked to heal them to
    widen a rail, but it is not allowed to trade one for another
    either — gr524392).

    Each trial's DRC copy adds ``2 * FAB_QUANTUM_MM`` to only that
    net's track widths: its swept radius then reserves the export
    margin once against every foreign object and edge. The accepted
    tracks retain the requested width, and the baseline and DRC rules
    remain exact. Otherwise widening could consume the routing reserve
    just before a Gerber aperture rounds upward.

    ``targets`` is the per-net width the net's estimated current asks
    for — the IPC-2221 width :func:`precis.pcb.session.
    clearance_net_rules` resolves, keyed by IR net id, carrying ONLY
    nets with a real current annotation (the net's own ``est_current_a``
    or the member pin currents rolled up). A rail tries the ladder
    steps between its rule width and its target (plus the target
    itself), widest first, and claims the first the oracle accepts; a
    target at or below the rule width widens nothing — a 1 mA rail
    keeps its rule width even when its plane class put it in the set
    (gr524239: the annotation outranks the class). A net in the set
    WITHOUT a target — an unannotated plane/power rail, the only kind
    :func:`widen_targets` admits that way — keeps the legacy ruling:
    the widest ladder step the oracle accepts (gr520904); a signal net
    with no estimate is never in the set at all. ``targets=None``
    legacy-gates the whole stage the same way for synthetic boards
    that carry no current annotations at all.

    Each net's tracks move together — one rail, one width. Accepted
    widths carry into later nets' trials (the first rail widened is
    copper the next must clear). Returns ``(result, {net_id: width_mm})``
    covering every power net that had tracks — widened or not — so the
    caller can report the delta; a net that never beat the oracle keeps
    its rule width verbatim."""
    base = _error_keys(drc_findings(result))
    tracks = list(result.tracks)
    widths: dict[int, float] = {}
    for net_id in sorted({int(n) for n in power_net_ids}):
        idx = {i for i, t in enumerate(tracks) if int(t.net_id) == net_id}
        if not idx:
            continue
        rule_w = float(tracks[next(iter(idx))].width_mm)
        widths[net_id] = rule_w
        tgt = None if targets is None else targets.get(net_id)
        if tgt is None:
            # an unannotated plane/power rail — the only set member
            # without a target — keeps the legacy ruling: the widest
            # ladder step the oracle accepts (gr520904's contract,
            # restored after gr524239's review).
            steps = [w for w in ladder if w > rule_w]
        elif float(tgt) <= rule_w:
            continue  # the est current needs no more than the rule width
        else:
            steps = sorted(
                {w for w in ladder if rule_w < w <= float(tgt)} | {float(tgt)},
                reverse=True,
            )
        for w in steps:
            trial = tuple(
                dataclasses.replace(t, width_mm=float(w)) if i in idx else t
                for i, t in enumerate(tracks)
            )
            diagnostic = tuple(
                dataclasses.replace(t, width_mm=float(w) + 2 * FAB_QUANTUM_MM)
                if i in idx
                else t
                for i, t in enumerate(trial)
            )
            trial_errors = _error_keys(
                drc_findings(dataclasses.replace(result, tracks=diagnostic))
            )
            if not (trial_errors - base):
                tracks, widths[net_id] = list(trial), float(w)
                break
    return dataclasses.replace(result, tracks=tuple(tracks)), widths


__all__ = [
    "RoutePiece",
    "RoutePlan",
    "ViaSite",
    "assign_layers",
    "plan_route",
    "realize_result",
    "route_design",
    "sketch_rows",
    "widen_power",
    "widen_targets",
]

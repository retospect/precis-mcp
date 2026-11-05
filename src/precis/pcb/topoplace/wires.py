"""Slice 2b — wires as edge sequences on the 2a CDT mesh (spec §2).

A wire carries NO coordinates of its own (Decision 3): it is a sequence
of crossed mesh-edge ids — its homotopy class. What this module adds on
top is the taut representative (the funnel over the triangle corridor),
kept so each crossing sits at a measured parameter ``t`` along its edge.
The recorded corridor is re-derived by walking the taut polyline through
the mesh (:func:`_retrack`), so ``tris``/``edges``/``t`` describe the
geometry, not the search corridor that found it — courtyard and outline
corners are not homotopy obstacles (seam ruling R-a).

Routing passability is 2b's own per-layer rule, NOT the placement
``edge_side`` field (R-a — courtyards never block routing). Exemption
keys on the wire's NET, not the endpoint part (round-2 ruling): the
walls of a pin on the wire's own net are open, a pad of another net is
always a wall — even on the wire's own part:

- an SMD pad blocks its own face: every edge on or inside its polygon
  (the mesh constrains the ring, so none crosses it) is closed on that
  outer layer to every wire of another net;
- a drilled pad (TH pin) blocks every layer the same way;
- a keepout/fixed-copper zone blocks the zone's ``layers`` on every
  edge on or inside its polygon — except fixed copper of the SAME net,
  which lets the wire through;
- the board edge never passes.

Per net the pins' tree is Prim over PATH distances (R-b): one persistent
multi-source search grows from the tree; the nearest unreached pin by
geodesic distance attaches next (a fresh attach re-seeds at cost 0, so
the heap key means distance-to-tree, not distance-to-root). A pin boxed
in on every candidate layer is left unplaced; the search then roots a
fresh component at the lowest unplaced pin — a caged root does not drop
the net — and every separated component is reported once on
``Wires.unreachable`` — the rest of the net still connects in the order
loop (the old Euclidean-MST hub failure reported every downstream branch).
The shove may later withdraw that refused partial tree to free its channel;
then one failure row names all pins, including those formerly connected.

``order`` is Decision 3's per-edge ordered wire list. Crossing
parameters tie wherever taut paths wrap a shared vertex, so tied wires
are ordered by one scalar per (branch, vertex) — the wrap tightness —
so a shared run keeps a single order on every edge; ``t`` is geometry
only.
``capacity_mm`` is the slice-4 corridor input — the edge's free crossing
length after the pad extents at its endpoints are priced off.

After the order loop, the shove phase (topo-route-shove S2) re-grows
each still-failing net pinned with its blocking partners rip-and-retryed
in route order — bounded rounds, a grow-count budget, and the same
accept rule the loop's one-shot negotiation uses: the failing set over
the swapped nets may only shrink, never relocate (Reto 2026-11-03).
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import heapq
import itertools
import math
import random
import types
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points

from precis.errors import BadInput
from precis.pcb.clearance import FAB_QUANTUM_MM
from precis.pcb.geom import segments_cross
from precis.pcb.rules import JLC_VIA_HOLE_TO_HOLE_MM
from precis.pcb.topoplace import proxy as topo_proxy
from precis.pcb.topoplace.copper_index import (
    ANY_NET,
    NO_OWN_NET,
    Body,
    CopperIndex,
)
from precis.pcb.topoplace.crossings import (
    Geom,
    branch_geom,
    crossing_events,
    edge_xy,
    resolve_crossings,
)
from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.mesh import (
    _GROW_MARGIN,
    OWNER_BOARD,
    OWNER_NONE,
    MeshPad,
    TopoMesh,
    world_pads,
)

if TYPE_CHECKING:
    from precis.pcb.topoplace.masks import BoardMasks
    from precis.pcb.topoplace.realise import RoutePlan

_EPS = 1e-9

#: Default design rules for the pad-gap passability check when the
#: caller resolves no per-net ``NetRules`` (synth fixtures, boards
#: without class rows): a 0.15 mm trace and 0.15 mm clearance.
_DEFAULT_TRACE_MM = 0.15
_DEFAULT_CLEAR_MM = 0.15

#: Occupancy pricing strength (gr520904): a channel edge at full
#: corridor capacity costs ``1 + _CONGESTION_ALPHA`` times its step
#: length, so late nets detour instead of piling into the same
#: corridor.
_CONGESTION_ALPHA = 4.0

#: Through-via diameter when the caller resolves no per-net rule (synth
#: fixtures) — the same fallback ``realise`` sites a barrel with.
_DEFAULT_VIA_MM = 0.6

#: What a layer change costs the route search, in mm of wire (G2c's
#: first experiment — the plan's number, measured before any tuning).
VIA_COST_MM = 3.0

#: What an edge already carrying an earlier net's wire on the same layer
#: adds to a step across it, as a multiple of the step (G2e): a wire
#: prefers a free layer or corridor, but two wires side by side in a
#: channel with room still cost almost the same as one.
OCC_LAYER_COST = 0.25

#: What a triangle already holding an earlier net's corridor on the same
#: layer adds to a step into it, in mm (G2g): a taut crossing lives
#: INSIDE a triangle — perpendicular wires share no edge, so the edge
#: occupancy term never sees it. Pricing the triangle the wire would
#: cross in steers the layered search off the crossed layer before the
#: crossing happens — what ``layer_dp``'s ``pen`` did after the fact.
#: Parallel wires in one channel share the triangle too and pay it as
#: ordinary congestion, the same reading ``occ`` gives edges.
TRI_OCC_MM = 1.0

#: Negotiated congestion with history (td569540, the PathFinder stage of
#: the route-then-shove model): ``build_wires`` runs ``_NEGOTIATE_ITERS``
#: passes over the net set instead of one. Every pass re-grows the nets
#: that failed or sit on an oversubscribed resource on ANY axis the
#: search prices — an (edge, layer) corridor past its cut, a (tri,
#: layer) past ``_NEG_TRI_CAP``, a same-layer chord crossing — against
#: an occupancy that holds the other nets' CURRENT wires (each re-grown
#: net's previous wires are ripped out first — ``_Occupancy.rebuild``);
#: a net whose wires satisfy every resource keeps them (selective
#: re-route). Two conditions keep the variant honest (spec-owner ruling
#: 2026-11-05): a pass entered while the overuse total has not fallen
#: for two consecutive passes re-grows EVERY net in order once — the
#: stall escape, so a clean net parked on a contested resource cannot
#: hold it for ever — and a final pass entered with conflicts standing
#: does the same.
#: Passes 1..K-1 soften today's legality into priced resources at
#: ``base × (1 + p_k × present_overuse) + h``; pass K restores hard
#: legality and ``_Shove`` repairs as before. ``p_k`` ramps from
#: ``_NEG_P0`` by ``_NEG_PRAMP`` per pass; ``h`` accumulates
#: ``_NEG_H_* × overuse`` on every resource still oversubscribed at a
#: pass's end. The pass with the best ``(failed nets, crossings, vias,
#: length)`` ledger that stays corridor-legal is emitted.
_NEGOTIATE_ITERS = 10
#: ``negotiate`` beyond this refuses: ``_NEG_P0 × _NEG_PRAMP**99`` is
#: already ~1e14, so every soft price is indistinguishable from inf
#: long before float64 overflows (~2,000 passes).
_NEG_ITERS_MAX = 100
_NEG_P0 = 0.5
_NEG_PRAMP = 1.4
_NEG_H_EDGE = 0.4  # mm per slot over capacity per pass
_NEG_H_TRI = 0.15  # mm per corridor over _NEG_TRI_CAP per pass
_NEG_H_CROSS = 0.8  # mm per residual same-layer crossing per pass
_NEG_CROSS_MM = 2.0  # base mm price of one chord crossing at p = 0
_NEG_TRI_CAP = 2.0  # corridors a (tri, layer) holds before overuse

#: Step-cost gate: a step through a triangle touching an edge this long
#: is priced by the portal (point-to-segment) metric, not the
#: midpoint-to-midpoint one — an open-area CDT edge is tens of trace
#: widths (the boards' open fields run 16-34 mm edges) while every
#: priced corridor is an order of magnitude finer, so the line
#: separates "the wire cuts a corner of a fat tri" (midpoint overprices
#: ~3x) from "the wire travels along a thin tri" (portal underprices).
_PORTAL_EDGE_MM = 15.0

#: The shortest free stretch of an edge a via is taken to fit in: the
#: siting walk steps 0.005-0.05 mm, so a sliver below this is
#: unreachable however open the mask calls it.
_VIA_FIT_MIN_MM = 0.01

#: Slack on the pad-AABB candidate test of :func:`_pad_eval` (mm). A grown
#: mesh puts ring vertices exactly ``r + _GROW_MARGIN`` from the copper, so
#: the edge between two of them reaches the pad's AABB grown by ``r`` only
#: to within that margin; the slack must exceed it, hence derived from it.
_AABB_SLACK_MM = 10.0 * _GROW_MARGIN


def finest_rule(rules: Sequence[Sequence[float]]) -> tuple[float, float]:
    """The ``(trace, clearance)`` of the finest rule in ``rules`` (rows
    starting ``trace, clearance``): the least ``trace/2 + clearance``, the
    centreline's stand-off from copper. The mesh grows its rings by it
    (:func:`~precis.pcb.topoplace.realise.ring_rule`) and the corridor
    prices capacity against it (:func:`_corridor`), so both pick alike."""
    if not rules:
        return _DEFAULT_TRACE_MM, _DEFAULT_CLEAR_MM
    t, c = min(rules, key=lambda r: r[0] / 2.0 + r[1])[:2]
    return float(t), float(c)


@dataclass(frozen=True, slots=True)
class PlantedVia:
    """A layer hop the search itself sited (G2g): the via IS a vertex of
    the wire, chosen where its barrel clears every body a via must
    clear — nothing slides it afterwards. ``i`` is the hop edge's index
    into :attr:`WirePath.edges`, ``k`` its vertex's index into
    :attr:`WirePath.points`. ``drill_mm`` 0.0 carries the net rule's own
    drill (the :class:`realise.ViaSite` convention); ``r_site_mm`` is the
    footprint radius the point was qualified at (``dia/2`` for a
    ``count``-1 via — a later slice plants wider arrays)."""

    i: int  # index into WirePath.edges — the hop edge
    k: int  # index into WirePath.points — the via IS this vertex
    x: float
    y: float
    dia_mm: float
    drill_mm: float
    r_site_mm: float
    count: int = 1


@dataclass(frozen=True, slots=True)
class WirePath:
    """One pin→pin branch of a net's wire tree (a geodesic-Prim edge
    pulled taut). ``edges`` IS the wire (homotopy class); ``tris`` the
    corridor re-walked under the taut polyline; ``t``/``points`` the
    taut representative used for ordering and geometry."""

    net: int  # net_pins index
    pin_a: int
    pin_b: int
    tris: tuple[int, ...]
    edges: tuple[int, ...]
    t: tuple[float, ...]  # crossing parameter per edge, on edges[e]'s u→v
    points: tuple[tuple[float, float], ...]  # taut polyline, pads at ends
    #: The layer in each corridor triangle (``len(edges) + 1``; edge ``i``
    #: belongs to ``layers[i]``, the run layer it is crossed on) as the
    #: layered search chose it (G2c/G2g) — what the later nets' search
    #: sees. ``()`` where none was assigned.
    layers: tuple[int, ...] = ()
    #: The via points the search planted on its hop edges (G2g), in edge
    #: order — each a vertex of ``points``.
    vias: tuple[PlantedVia, ...] = ()

    @property
    def length_mm(self) -> float:
        return sum(math.dist(p, q) for p, q in zip(self.points, self.points[1:]))


@dataclass(frozen=True, slots=True)
class Unreachable:
    """A separated component of a net — no dual path links it to the
    net's main component on any candidate layer. ``pin`` names the
    component's root and ``pins`` every member, so a certificate can
    list a multi-pin cut-off group without re-flooding (round-3
    finding R3-4); ``owners`` names what walls it off, in the
    ``edge_owner`` id space (part id, OWNER_BOARD, or n_parts + zone):
    the colour pass reports it as a certificate input. When the shove
    withdraws a refused net's partial copper, one row names ALL its pins:
    a net with no copper must still be reported failed, never sketched."""

    net: int
    pin: int
    owners: tuple[int, ...]
    pins: tuple[int, ...] = ()
    #: The component's any-layer region meets the main component's —
    #: some path exists, but none the layered search could take (no
    #: plantable hop). G2g names it instead of routing it any-layer.
    layered: bool = False
    #: The earlier net the negotiation tried before giving up (G2h);
    #: for a chord-blocked failure (S1) every crossing partner,
    #: most-crossed first.
    blocked_by: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Wires:
    """Every net's wire tree plus the shared-edge order structure the
    crossing graph (``layers.py``) reads. ``order[e]`` is the branch ids
    crossing edge ``e`` in edge-parameter order — ties at a wrapped
    vertex resolved by wrap tightness (:func:`_order`), not by id."""

    branches: tuple[WirePath, ...]
    order: tuple[tuple[int, ...], ...]
    capacity_mm: np.ndarray  # (E,) free crossing length per edge
    net_layers: tuple[frozenset[int], ...]  # candidate layers per net (full stackup)
    unreachable: tuple[Unreachable, ...]
    # slice-4 channel record (round-4): per edge and layer, the stretch
    # left uncovered by ringed pads buffered at the finest resolved rule
    # (−1 where no pad reaches the edge), and the TOTAL traces that
    # stretch takes — floor(free / (trace + clearance)) + 1 where a
    # centreline survives, 0 where the channel is closed (round-5:
    # the count is measured in the free width's own frame — a just-
    # passable gap records 1, not 0).
    gap_free_mm: np.ndarray  # (E, L) mm, -1 = not a pad-channel edge
    gap_cap: np.ndarray  # (E, L) int, -1 = not a pad-channel edge
    order_residual: int = 0  # tied-pair order flips _repair_order left over
    #: Parallel to ``order``: each entry's index into the branch's
    #: ``edges`` — a lollipop corridor lists the branch twice on its
    #: stem edges and the positions tell the two crossings apart.
    order_pos: tuple[tuple[int, ...], ...] = ()
    #: The demand-vs-capacity cut (spec phase 3, gr520904): edges whose
    #: routed demand exceeds what their corridor takes — the caller
    #: refuses on a non-empty ROUTED record (``route_design`` raises
    #: :class:`CorridorOverflow`), never silently crams copper through.
    #: Cuts tagged ``refused`` hold an unrouted net's refused-
    #: negotiation demand instead (G2h): recorded and reported on the
    #: plan, never raised on.
    overflow: tuple[CorridorCut, ...] = ()
    #: ``(net, pin_a, pin_b)`` of every branch whose planted vias stand
    #: closer than drill-to-drill clearance (G2g): the later one is
    #: dropped, the hop vertex stays, ``plan_route`` names it ``no_via``.
    via_collisions: tuple[tuple[int, int, int], ...] = ()
    #: ``(later, earlier, kept)`` per negotiation (G2h): a later net
    #: with no way got ONE earlier net re-grown once with its need
    #: walled, kept only if that net's key did not worsen.
    negotiated: tuple[tuple[int, int, bool], ...] = ()
    #: S2: ``_grow_net`` calls the shove phase spent — the budget is a
    #: count, never wall time. Accepted swaps are grouped by start, then round.
    shove_grows: int = 0
    shove_rounds: tuple[tuple[int, ...], ...] = ()
    #: Input-wire identities per branch after layer planning. A whole-tree
    #: rethread gives every replacement the union of the replaced identities;
    #: flips retain them. Empty before planning; never use final plan indices
    #: to address the original branch list.
    branch_lineage: tuple[frozenset[WirePath], ...] = ()
    #: Concrete planned-descendant identities, separate from input ancestry.
    #: Seeded with the input wires; flips retain them. A rethread seeds each
    #: new branch with its own wire, reusing a retained branch's identity.
    #: Shaping and withdrawals preserve this full map, including dropped rows.
    branch_ids: tuple[WirePath, ...] = ()


@dataclass(frozen=True, slots=True)
class CorridorCut:
    """One mesh edge whose routed demand exceeds its corridor capacity —
    the phase-3 cut condition (R11). ``owners`` names what bounds the
    corridor: the refdes of every part whose pads ring or endpoint the
    edge, the name of every contributing zone, ``"outline"`` for the
    board edge. ``nets`` names the nets whose wires make the demand —
    the ones a caller would reroute or relax."""

    edge: int
    demand: float  # finest-rule trace slots
    cap: float  # total traces the corridor takes (all layers)
    owners: tuple[str, ...]
    nets: tuple[str, ...]
    x: float  # edge midpoint, world mm
    y: float
    #: ``True`` when the over-capacity demand includes an UNROUTED
    #: net's refused-negotiation share (``_Corridor.cut``'s ``extra``):
    #: a capacity fact for the plan's report, not a stop — the caller
    #: raises only on routed demand (G2h).
    refused: bool = False


class CorridorOverflow(BadInput):
    """``failed: corridor overflow`` — a mesh edge's routed demand
    exceeds its capacity, so the route refuses rather than emitting
    copper that cannot fit (gr520904). Carries the full cut record;
    ``str()`` names the worst edges — bounding owners and demanding net
    names, so the reader sees WHAT to move and WHICH wires to reroute —
    capped at three edges, six nets each, the rest counted."""

    _MSG_EDGES = 3
    _MSG_NETS = 6

    def __init__(self, cuts: tuple[CorridorCut, ...]) -> None:
        self.cuts = cuts
        self.count = len(cuts)
        worst = sorted(cuts, key=lambda c: c.demand - c.cap, reverse=True)[
            : self._MSG_EDGES
        ]
        shown = []
        for c in worst:
            nets = " ".join(c.nets[: self._MSG_NETS])
            if len(c.nets) > self._MSG_NETS:
                nets += f" +{len(c.nets) - self._MSG_NETS} more"
            shown.append(
                f"edge at ({c.x:.2f}, {c.y:.2f}) between "
                f"{', '.join(c.owners)}, demand {c.demand:g} > cap {c.cap:g}, "
                f"nets {nets}"
            )
        super().__init__(
            "corridor overflow: "
            + "; ".join(shown)
            + (
                f" (+{len(cuts) - self._MSG_EDGES} more edge(s))"
                if len(cuts) > self._MSG_EDGES
                else ""
            ),
            next=(
                "spread the bounding parts to widen the corridor, reroute "
                "a net around it, or relax the net's design rule — no "
                "copper was written"
            ),
        )


def _net_layers(d: TopoDesign, k: int, n_layers: int) -> frozenset[int]:
    """The net's candidate layers — the class ``layers`` rule
    (``net_layer_force``) over the WHOLE stackup: a forced index, the
    inners {1..n−2} for an inner-only lock (a piece bracketed by two
    vias can live there, gr520902), else every layer. Pad sides
    (``net_pin_force``) are NOT in the and — Decision 6 binds a pad's
    mount side to the terminal piece only, which ``_pin_forced``
    enforces at plan time. An empty set stays the inner-only clash the
    colour pass reports as a certificate; the path builder treats it
    as free so the wire exists to be diagnosed."""
    force = int(d.net_layer_force[k]) if len(d.net_layer_force) > k else -1
    if force >= 0:
        cand = frozenset({force})
    elif force == -2:
        cand = frozenset(range(1, n_layers - 1))
    else:
        cand = frozenset(range(n_layers))
    # a plane-reserved layer carries its pour, never a wire (gr524239)
    return cand - frozenset(d.plane_layers)


@dataclass(slots=True)
class _Ctx:
    """Per-:func:`build_wires` mesh caches — everything the search and
    the re-walk read more than once."""

    n_layers: int
    grow_mm: float  # how far the mesh's pad rings stand off their copper
    all_mask: int  # bitmask of every layer
    tri_verts: list[tuple[int, int, int]]
    tri_set: list[frozenset[int]]
    v2t: list[list[int]]  # vertex -> incident triangle ids (ascending)
    pin_wall: dict[int, frozenset[int]]  # pin -> edges on or in any of its pads
    pin_core: dict[int, frozenset[int]]  # pin -> edges strictly inside its pads
    pad_contrib: dict[int, list[tuple[int, int]]]  # edge -> [(pin, mask)]
    zone_contrib: dict[int, list[tuple[int, int]]]  # ring edge -> [(zone idx, mask)]
    zone_closed: dict[int, list[tuple[int, int]]]  # + interior edges, same shape
    # Every pad the mesh holds (:func:`world_pads`) for the gap rule and
    # the closure: the real copper, its world AABB, and whether the AABB
    # IS the copper (an axis-aligned rectangle — the slab clip is exact
    # there; a disk, an outlined land or a comb is clipped as a polygon).
    pad_pin: np.ndarray  # (Rp,) global pin id per pad, -1 = unpinned extra
    pad_part: np.ndarray  # (Rp,) owning part id per pad (extras included)
    pad_lo: np.ndarray  # (Rp, 2) world AABB lo
    pad_hi: np.ndarray  # (Rp, 2) world AABB hi
    pad_copper: np.ndarray  # (Rp,) int — layers the pad has copper on
    pad_poly: tuple[Polygon, ...]  # (Rp,) the copper
    pad_rect: np.ndarray  # (Rp,) bool — AABB == copper
    pad_inside: tuple[np.ndarray, ...]  # (Rp,) edges on or inside the pad's ring
    # cover_cache[r][e] = [(a, b, pad)] — the stretches of edge ``e`` (as
    # parameters) the pads grown by ``r`` cover; ``_pad_eval`` fills it
    # and the own-net re-merge (``_edge_free``) reads it back.
    cover_cache: dict[float, dict[int, list[tuple[float, float, int]]]]
    edge_lo: np.ndarray  # (E, 2) edge AABB lo
    edge_hi: np.ndarray  # (E, 2) edge AABB hi
    seg_u: np.ndarray  # (E, 2) edge u endpoint
    seg_v: np.ndarray  # (E, 2) edge v endpoint
    seg_len: np.ndarray  # (E,) edge length
    pin_seg: np.ndarray  # (E, n_pins) pin vertex -> segment distance
    pad_block_all: np.ndarray  # (E,) int — all pads' blocked-layer OR
    zone_block: dict[str, np.ndarray]  # net name -> (E,) zone-blocked OR
    gap_eval_cache: dict[
        float,
        tuple[np.ndarray, np.ndarray, dict[int, np.ndarray], dict[int, list[int]]],
    ]
    # gap_eval_cache[r] = (blocked (E,) int, free (E, L) mm, edge_pads,
    # pad_edges) — the all-pads coverage eval; _net_masks re-opens
    # a net's own pads on top of it.
    # via_fit_cache[(r, margin)] = (E,) bool — edges where a through via
    # of keep-out radius ``r`` fits, before the per-net zone bar.
    via_fit_cache: dict[tuple[float, float], np.ndarray]
    board: np.ndarray  # (E,) bool — board-outline edges
    # inset-ring edges a pad's ring reaches: edge -> [(pin, layer mask)],
    # board for everyone but that pin's net, and only on its layers
    board_reach: dict[int, list[tuple[int, int]]]
    vid_of: dict[tuple[float, float], int]  # rounded xy -> vertex id
    eu: list[tuple[float, float]]  # edge u endpoint xy (python floats)
    ev: list[tuple[float, float]]  # edge v endpoint xy
    edge_ends: list[tuple[int, int]]  # edge -> (u vid, v vid)
    edge_tris: list[tuple[int, int]]  # edge -> (tri, tri) ids, -1 off-mesh
    tri_xy: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]
    tri_edge_list: list[tuple[int, int, int]]  # tri -> its 3 edge ids
    tri_next: list[dict[int, tuple[tuple[int, int, float], ...]]]
    # tri_next[t][e] = ((e2, t2, step cost) for the tri's OTHER edges —
    # mid→mid distance, or the portal mid→segment metric on pad-ring
    # slivers and on steps touching a >= _PORTAL_EDGE_MM edge
    fan_ring_cache: dict[int, list[tuple[int, int]]] = dataclasses.field(
        default_factory=dict
    )
    # fan_ring_cache[w] = _fan_ring(ctx, w) — the cyclic fan is a pure
    # function of the mesh; every vertex passage asked for it anew


def _pin_mask(pin_side: int, n_layers: int) -> int:
    """Blocked-layer bitmask for one pad: SMD blocks its own face, a
    drilled (TH) pad blocks every layer."""
    if pin_side == 2:
        return (1 << n_layers) - 1
    return 1 << (n_layers - 1 if pin_side == 1 else 0)


@dataclass(frozen=True, slots=True)
class GuardPads:
    """The pad set the wire guard's gap rule measures — the same pads
    :func:`precis.pcb.realize.pads_for_ir` hands the DRC (gr524392):
    each pin's pad plus every non-primary footprint pad
    (:attr:`TopoDesign.pad_extra_*` — a pin's second same-named land, a
    split slug half, an unpinned land), in their real shapes
    (:func:`~precis.pcb.topoplace.mesh.world_pads`). ``pin`` is the
    owning pin id, ``-1`` for an unpinned land — foreign copper to every
    net, the same ``net=""`` the DRC pad set gives it. ``part`` the
    owning instance for attribution; ``copper`` the ``_pin_mask`` layer
    bitmask; ``lo``/``hi`` the copper's bounding box. ``pad_contrib``
    maps each mesh edge on or inside a pad's constrained ring to
    ``(pin, mask)`` — the pad walls — and ``pin_wall`` each pin to the
    edges of its own pads, for the own-net exemption."""

    pin: np.ndarray  # (Rp,) global pin id per pad, -1 = unpinned extra
    part: np.ndarray  # (Rp,) owning part id per pad
    lo: np.ndarray  # (Rp, 2) world AABB lo
    hi: np.ndarray  # (Rp, 2) world AABB hi
    copper: np.ndarray  # (Rp,) blocked-layer bitmask
    polys: tuple[Polygon, ...]  # (Rp,) the copper
    rect: np.ndarray  # (Rp,) bool — the AABB is the copper
    inside: tuple[np.ndarray, ...]  # (Rp,) edges on or inside each pad's ring
    pin_wall: dict[int, frozenset[int]]  # pin -> edges on or in its pads
    pin_core: dict[int, frozenset[int]]  # pin -> edges strictly inside its pads
    pad_contrib: dict[int, list[tuple[int, int]]]  # edge -> [(pin, mask)]


def _mesh_pads(
    mesh: TopoMesh, d: TopoDesign, pose: np.ndarray | None
) -> tuple[MeshPad, ...]:
    """The pads the mesh constrained — or, for a hand-built mesh that
    never recorded any, the design's at ``pose`` (default ``d.pose0``)."""
    if mesh.pads is not None:
        return mesh.pads
    return world_pads(d, np.asarray(d.pose0 if pose is None else pose))


def _edges_in(
    mesh: TopoMesh, polys: Sequence[Polygon], *, strict: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    """``(edge, polygon)`` index pairs for every mesh edge lying on or
    inside a polygon. A constrained ring is never crossed, so an edge is
    wholly on one side of it and its midpoint decides; the 1 µm slack
    takes the edges ON the ring (vertices carry 1e-9 rounding).
    ``strict`` keeps only the edges inside it, not on its ring."""
    if not len(polys) or not len(mesh.edges):
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    mids = shapely.points(
        0.5 * (mesh.verts[mesh.edges[:, 0]] + mesh.verts[mesh.edges[:, 1]])
    )
    if strict:
        core = [p.buffer(-1e-6) for p in polys]
        hit = shapely.STRtree(core).query(mids, predicate="within")
    else:
        hit = shapely.STRtree(list(polys)).query(
            mids, predicate="dwithin", distance=1e-6
        )
    return hit[0], hit[1]


def guard_pad_rects(
    mesh: TopoMesh,
    d: TopoDesign,
    n_layers: int,
    pose: np.ndarray | None = None,
) -> GuardPads:
    """The guard's pads for a mesh built at ``pose`` (defaults
    ``d.pose0``, the same default :func:`build_wires` applies)."""
    pads = _mesh_pads(mesh, d, pose)
    pin = np.asarray([p.pin for p in pads], dtype=np.int64)
    copper = np.asarray([_pin_mask(p.side, n_layers) for p in pads], dtype=np.int64)
    bounds = np.asarray([p.poly.bounds for p in pads], dtype=np.float64).reshape(-1, 4)
    # An edge on or inside a pad's ring is wall on the pad's layers. The
    # ring (the polygon the CDT holds), not the copper: a round land's
    # octagon circumscribes the disk.
    rings = [Polygon(p.ring) for p in pads]
    ei, pi = _edges_in(mesh, rings)
    # ... and the edges strictly inside it: the pad's copper plus, on a
    # grown mesh, the stand-off band round it (the ring is the grown
    # polygon). A wire of the pad's net travels it without owing
    # clearance to what is beside the pad
    ci, cpi = _edges_in(mesh, rings, strict=True)
    pin_core: dict[int, set[int]] = {}
    for e, k in zip(ci.tolist(), cpi.tolist(), strict=True):
        if pin[k] >= 0:
            pin_core.setdefault(int(pin[k]), set()).add(e)
    pad_contrib: dict[int, list[tuple[int, int]]] = {}
    pin_wall: dict[int, set[int]] = {}
    inside: list[list[int]] = [[] for _ in pads]
    for e, k in zip(ei.tolist(), pi.tolist(), strict=True):
        pad_contrib.setdefault(e, []).append((int(pin[k]), int(copper[k])))
        inside[k].append(e)
        if pin[k] >= 0:
            pin_wall.setdefault(int(pin[k]), set()).add(e)
    # A pin with no pad size has no polygon to constrain: its wall stays
    # the boundary of its vertex's fan (the edges opposite the vertex) — the
    # synth fixtures and any footprint that authors no pad extent.
    with_pad = set(pin.tolist())
    padless = [p for p in range(len(mesh.pin_vertex)) if p not in with_pad]
    if padless:
        pose_a = np.asarray(d.pose0 if pose is None else pose)
        verts_of = {int(mesh.pin_vertex[p]): p for p in padless}
        for t in np.flatnonzero(np.isin(mesh.tris, list(verts_of)).any(axis=1)):
            for va in (int(v) for v in mesh.tris[t] if int(v) in verts_of):
                p = verts_of[va]
                side = (
                    2
                    if int(d.pin_side[p]) == 2
                    else int(pose_a[int(d.pin_part[p]), 3] >= 0.5)
                )
                for e in (int(x) for x in mesh.tri_edges[t]):
                    if va not in mesh.edges[e]:
                        pad_contrib.setdefault(e, []).append(
                            (p, _pin_mask(side, n_layers))
                        )
                        pin_wall.setdefault(p, set()).add(e)
    return GuardPads(
        pin=pin,
        part=np.asarray([p.part for p in pads], dtype=np.int64),
        lo=bounds[:, :2],
        hi=bounds[:, 2:],
        copper=copper,
        polys=tuple(p.poly for p in pads),
        rect=np.asarray([p.rect for p in pads], dtype=bool),
        inside=tuple(np.asarray(v, dtype=np.int64) for v in inside),
        pin_wall={k: frozenset(v) for k, v in pin_wall.items()},
        pin_core={k: frozenset(v) for k, v in pin_core.items()},
        pad_contrib=pad_contrib,
    )


#: The last contexts built, newest first: ``(key, mesh, design, ctx)`` —
#: the mesh and design are held so their ids stay valid. A route pass asks
#: for the context of one mesh a dozen times (layer assignment, the
#: corridor, obstacle vertices) and each build is a pad/zone containment
#: query over every mesh edge. Two entries: a context is large.
_CTX_CACHE: list[tuple[tuple, TopoMesh, TopoDesign, _Ctx]] = []


def _make_ctx(
    mesh: TopoMesh, d: TopoDesign, n_layers: int, pose: np.ndarray | None = None
) -> _Ctx:
    key = (
        id(mesh),
        id(d),
        n_layers,
        # (a built mesh carries its own pads; only a hand-built one reads the pose)
        None
        if pose is None or mesh.pads is not None
        else np.asarray(pose, dtype=np.float64).tobytes(),
    )
    for k, m, dd, got in _CTX_CACHE:
        if k == key and m is mesh and dd is d:
            return got
    ctx = _build_ctx(mesh, d, n_layers, pose)
    _CTX_CACHE.insert(0, (key, mesh, d, ctx))
    del _CTX_CACHE[2:]
    return ctx


def _build_ctx(
    mesh: TopoMesh, d: TopoDesign, n_layers: int, pose: np.ndarray | None = None
) -> _Ctx:
    pads = guard_pad_rects(mesh, d, n_layers, pose)
    pad_contrib = pads.pad_contrib
    tri_verts = [(int(a), int(b), int(c)) for a, b, c in mesh.tris]
    v2t: list[list[int]] = [[] for _ in range(len(mesh.verts))]
    for t, tri in enumerate(tri_verts):
        for v in tri:
            v2t[v].append(t)
    n_parts = len(d.parts)
    all_mask = (1 << n_layers) - 1
    zone_contrib: dict[int, list[tuple[int, int]]] = {}
    board = np.zeros(len(mesh.edges), dtype=bool)
    for e in range(len(mesh.edges)):
        for slot in range(2):
            owner = int(mesh.edge_owner[e, slot])
            if owner == OWNER_NONE:
                break
            if owner == OWNER_BOARD:
                board[e] = True
            elif owner >= n_parts:
                z = owner - n_parts
                layers = d.zones[z].layers
                zm = all_mask if not layers else _layer_mask(layers)
                zone_contrib.setdefault(e, []).append((z, zm))
    # The mesh's second board ring (G2f) stands the centreline off the
    # edge by the edge clearance. A pad inside that band is reached across
    # the ring, so the stretch of the ring within a pad's ring opens: for
    # that pad's net only, on the pad's layers only (``board_reach``,
    # read by :func:`_open_layers`). It stays board for everyone else and
    # on every other layer — a wire on the opposite layer must not enter
    # the band and run inside the edge clearance.
    board_reach: dict[int, list[tuple[int, int]]] = {}
    if board.any() and len(d.outline) >= 3 and pad_contrib:
        outline_line = Polygon(np.asarray(d.outline, dtype=float)).exterior
        for be in np.flatnonzero(board):
            if int(be) in pad_contrib:
                mid = shapely.points(
                    0.5
                    * (mesh.verts[mesh.edges[be, 0]] + mesh.verts[mesh.edges[be, 1]])
                )
                if shapely.distance(mid, outline_line) > 1e-6:
                    board_reach[int(be)] = [
                        (pin, pm) for pin, pm in pad_contrib[int(be)] if pin >= 0
                    ]
    # A zone is closed through its interior too: its ring is constrained,
    # so the edges inside it are the zone's copper-bar on its layers.
    zone_closed = {e: list(lst) for e, lst in zone_contrib.items()}
    # (a hand-built mesh records no polygons and keeps its ring edges only)
    live = [
        (z, g)
        for z, g in enumerate(mesh.zone_polys or ())
        if g is not None and not g.is_empty
    ]
    zei, zpi = _edges_in(mesh, [g for _z, g in live])
    for e, k in zip(zei.tolist(), zpi.tolist(), strict=True):
        z = live[k][0]
        layers = d.zones[z].layers
        zm = all_mask if not layers else _layer_mask(layers)
        if all(z2 != z for z2, _m in zone_closed.get(e, ())):
            zone_closed.setdefault(e, []).append((z, zm))
    vid_of = {
        (round(float(x), 6), round(float(y), 6)): i
        for i, (x, y) in enumerate(mesh.verts)
    }
    eu = [(float(xy[0]), float(xy[1])) for xy in mesh.verts[mesh.edges[:, 0]]]
    ev = [(float(xy[0]), float(xy[1])) for xy in mesh.verts[mesh.edges[:, 1]]]
    eu_arr = mesh.verts[mesh.edges[:, 0]]
    ev_arr = mesh.verts[mesh.edges[:, 1]]
    edge_lo = np.minimum(eu_arr, ev_arr)
    edge_hi = np.maximum(eu_arr, ev_arr)
    edge_ends = [(int(a), int(b)) for a, b in mesh.edges]
    edge_tris = [(int(a), int(b)) for a, b in mesh.edge_tris]
    tri_xy = [
        (
            (float(a[0]), float(a[1])),
            (float(b[0]), float(b[1])),
            (float(c[0]), float(c[1])),
        )
        for a, b, c in mesh.verts[mesh.tris]
    ]
    tri_edge_list = [(int(a), int(b), int(c)) for a, b, c in mesh.tri_edges]
    mid_arr = 0.5 * (eu_arr + ev_arr)  # edge midpoints
    # Step cost through a tri: the portal metric — the symmetric
    # mid→segment projection — wherever the midpoint metric misprices:
    # on the pad-ring sliver tris (a legal pad gap must not cost the
    # wrap-around detour) and on any step touching an open-area edge of
    # ``_PORTAL_EDGE_MM`` up. A fat-tri crossing near a vertex travels
    # far less than |mid→mid| — on taz-leaf the A* read ~32 mm over a
    # real ~10 and bought a 51 mm two-via loop instead of a legal 33 mm
    # F.Cu line (2026-11-02 detour finding). Portal is NOT applied
    # everywhere: in a thin channel tri it charges ~the wedge altitude
    # while the wire advances along the channel, underpricing the long
    # homotopy — that mis-order is what the old hybrid comment blamed
    # for the n=100 ringless order residual 6 → 42, and the G6 table
    # still drops strict rows on x2/local-61 under all-portal. The A*
    # heuristic stays valid either way: pin→segment distance is a lower
    # bound on the wire's own length under both metrics.
    # Vectorized: pt→seg distance over the (E, 3) in/out pairs of the
    # per-tri edge lists.
    ecol = np.asarray(tri_edge_list, dtype=np.int64)  # (T, 3)
    mid_e = mid_arr[ecol]  # (T, 3, 2)

    def ptseg_mat(
        pts: np.ndarray, ei: np.ndarray
    ) -> np.ndarray:  # pts (...,2), ei (...,) -> (...,)
        su, sv = eu_arr[ei], ev_arr[ei]
        dd = sv - su
        ddd = np.maximum((dd * dd).sum(axis=-1), _EPS)
        tp = np.clip(((pts - su) * dd).sum(axis=-1) / ddd, 0.0, 1.0)
        return np.linalg.norm(pts - (su + tp[..., None] * dd), axis=-1)

    # wstep[t, i, j] = mid(e_i) -> seg(e_j) distance for the tri's edges
    T = len(ecol)
    pts_grid = np.broadcast_to(mid_e[:, :, None, :], (T, 3, 3, 2)).reshape(T, 9, 2)
    ei_grid = np.broadcast_to(ecol[:, None, :], (T, 3, 3)).reshape(T, 9)
    w_in = ptseg_mat(pts_grid, ei_grid).reshape(T, 3, 3)
    w_out = w_in.transpose(0, 2, 1)
    wstep_portal = 0.5 * (w_in + w_out)
    wstep_mid = np.linalg.norm(mid_e[:, :, None, :] - mid_e[:, None, :, :], axis=-1)
    # ring-neighbourhood tris of pads — tris sharing a vertex with a
    # pad-wall edge: the sliver channel around a pad ring, where the
    # portal metric is needed; plus every step touching a long
    # (open-area) edge, where midpoint pricing mis-reads a corner-cut
    fan_tris = np.zeros(T, dtype=bool)
    for e in pad_contrib:
        for v in edge_ends[e]:
            for t2 in v2t[v]:
                fan_tris[t2] = True
    e_len_t = np.linalg.norm(ev_arr - eu_arr, axis=1)[ecol]  # (T, 3)
    long_step = (e_len_t[:, :, None] >= _PORTAL_EDGE_MM) | (
        e_len_t[:, None, :] >= _PORTAL_EDGE_MM
    )
    wstep = np.where(fan_tris[:, None, None] | long_step, wstep_portal, wstep_mid)
    tris_of = np.asarray(edge_tris, dtype=np.int64)  # (E, 2)
    tri_next: list[dict[int, tuple[tuple[int, int, float], ...]]] = []
    for t, es in enumerate(tri_edge_list):
        row: dict[int, tuple[tuple[int, int, float], ...]] = {}
        for i, e_in in enumerate(es):
            nxt = []
            for j, e2 in enumerate(es):
                if e2 == e_in:
                    continue
                t2a, t2b = tris_of[e2]
                t2 = t2a if t2a != t else t2b
                nxt.append((e2, int(t2), float(wstep[t, i, j])))
            row[e_in] = tuple(nxt)
        tri_next.append(row)
    # (E, n_pins) pin-vertex -> segment distance — every net's A*
    # heuristic and seed cost are column slices of this matrix (round-4:
    # rebuilding the rel/proj stack per net dominated _net_tree).
    n_pins_all = len(mesh.pin_vertex)
    if n_pins_all:
        pvv = mesh.verts[np.asarray(mesh.pin_vertex, dtype=np.int64)]
        dd_all = ev_arr - eu_arr
        ddd = np.maximum((dd_all * dd_all).sum(axis=1), _EPS)
        relv = pvv[None, :, :] - eu_arr[:, None, :]
        tpv = np.clip((relv * dd_all[:, None, :]).sum(axis=2) / ddd[:, None], 0.0, 1.0)
        projv = eu_arr[:, None, :] + tpv[:, :, None] * dd_all[:, None, :]
        pin_seg = np.linalg.norm(pvv[None, :, :] - projv, axis=2)
    else:
        pin_seg = np.zeros((len(mesh.edges), 0))
    pad_block_all = np.zeros(len(mesh.edges), dtype=np.int64)
    for e, lst in pad_contrib.items():
        for _pin, pm in lst:
            pad_block_all[e] |= pm
    zone_block: dict[str, np.ndarray] = {}
    return _Ctx(
        n_layers=n_layers,
        grow_mm=mesh.pad_grow_mm,
        all_mask=all_mask,
        tri_verts=tri_verts,
        tri_set=[frozenset(t) for t in tri_verts],
        v2t=v2t,
        pin_wall=pads.pin_wall,
        pin_core=pads.pin_core,
        pad_contrib=pad_contrib,
        zone_contrib=zone_contrib,
        zone_closed=zone_closed,
        pad_pin=pads.pin,
        pad_part=pads.part,
        pad_lo=pads.lo,
        pad_hi=pads.hi,
        pad_copper=pads.copper,
        pad_poly=pads.polys,
        pad_rect=pads.rect,
        pad_inside=pads.inside,
        cover_cache={},
        edge_lo=edge_lo,
        edge_hi=edge_hi,
        seg_u=eu_arr,
        seg_v=ev_arr,
        seg_len=np.hypot(*(ev_arr - eu_arr).T),
        pin_seg=pin_seg,
        pad_block_all=pad_block_all,
        zone_block=zone_block,
        gap_eval_cache={},
        via_fit_cache={},
        board=board,
        board_reach=board_reach,
        vid_of=vid_of,
        eu=eu,
        ev=ev,
        edge_ends=edge_ends,
        edge_tris=edge_tris,
        tri_xy=tri_xy,
        tri_edge_list=tri_edge_list,
        tri_next=tri_next,
    )


def _layer_mask(layers) -> int:
    m = 0
    for ly in layers:
        m |= 1 << int(ly)
    return m


def _cand_mask(cand: frozenset[int], n_layers: int) -> int:
    """Candidate-layers bitmask; an empty candidate set is the inner-only
    clash the colour pass reports — the path builder treats it as free."""
    return _layer_mask(cand) if cand else (1 | (1 << (n_layers - 1)))


# A 2-vector: python-float pair (the hot paths) or an ndarray row.
_V2 = tuple[float, float] | np.ndarray


def _pt_seg(p: _V2, a: _V2, b: _V2) -> float:
    """Distance from ``p`` to segment ``a→b``."""
    ex, ey = b[0] - a[0], b[1] - a[1]
    dd = ex * ex + ey * ey
    if dd <= _EPS:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = min(1.0, max(0.0, ((p[0] - a[0]) * ex + (p[1] - a[1]) * ey) / dd))
    return math.hypot(p[0] - (a[0] + t * ex), p[1] - (a[1] + t * ey))


def _cross(u: _V2, v: _V2) -> float:
    return float(u[0] * v[1] - u[1] * v[0])


def _area2(a: _V2, b: _V2, c: _V2) -> float:
    """Mononen's triarea2 — positive when ``c`` is right of ``a→b``."""
    return (c[0] - a[0]) * (b[1] - a[1]) - (b[0] - a[0]) * (c[1] - a[1])


def _same(a: _V2, b: _V2) -> bool:
    return math.dist(a, b) <= 1e-9


def _funnel(
    portals: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[tuple[float, float]]:
    """The simple-stupid funnel over oriented ``(left, right)`` portal
    endpoints."""
    apex = portals[0][0]
    goal = portals[-1][0]
    left_v = right_v = apex
    apex_i = left_i = right_i = 0
    path = [apex]
    i = 1
    n = len(portals)
    while i < n:
        lp, rp = portals[i]
        if _area2(apex, right_v, rp) <= 0.0:
            if _same(apex, right_v) or _area2(apex, left_v, rp) > 0.0:
                right_v, right_i = rp, i
            else:
                path.append(left_v)
                apex, apex_i = left_v, left_i
                left_v = right_v = apex
                left_i = right_i = apex_i
                i = apex_i + 1
                continue
        if _area2(apex, left_v, lp) >= 0.0:
            if _same(apex, left_v) or _area2(apex, right_v, lp) < 0.0:
                left_v, left_i = lp, i
            else:
                path.append(right_v)
                apex, apex_i = right_v, right_i
                left_v = right_v = apex
                left_i = right_i = apex_i
                i = apex_i + 1
                continue
        i += 1
    if not _same(path[-1], goal):
        path.append(goal)
    return path


def _chord_crossed(chord: tuple[float, float, float, float], p: _V2, q: _V2) -> bool:
    """One stored chord ``(px, py, qx, qy)`` vs the step ``p→q`` — the
    same side/touch tests :func:`_chord_ok` applies, per chord, for the
    negotiated passes that PRICE crossings instead of forbidding them."""
    px, py, qx, qy = chord
    dx, dy = qx - px, qy - py
    dd = dx * dx + dy * dy
    if dd <= 1e-18:
        return False
    dead = 1e-9 * math.sqrt(dd)
    sa = _area2((px, py), (qx, qy), p)
    sb = _area2((px, py), (qx, qy), q)
    if (sa > dead and sb < -dead) or (sa < -dead and sb > dead):
        return True
    for s, (x, y) in ((sa, p), (sb, q)):
        if abs(s) <= dead:
            u = ((x - px) * dx + (y - py) * dy) / dd
            if 1e-6 < u < 1.0 - 1e-6:
                return True
    return False


def _chord_ok(cs: Iterable[tuple[float, float, float, float]], p: _V2, q: _V2) -> bool:
    """Both ends of the step ``p→q`` on the same side of every chord in
    ``cs`` — a point on the chord's line is a touch, never a crossing
    (``|area2| ≤ 1e-9·|PQ|`` is 0); the triangle's convexity makes the
    line test exact for its interior (G2h). The touch exception ends at
    the chord's ENDS: a step endpoint on the chord's segment interior
    sits on the earlier wire itself — a crossing, not a touch (a chord
    lying along a mesh edge is recorded under the triangle across the
    edge too, so it blocks a crossing of that edge)."""
    return not any(_chord_crossed(c, p, q) for c in cs)


def _chords_crossed_n(
    cs: Iterable[tuple[float, float, float, float]], p: _V2, q: _V2
) -> int:
    """How many chords the step ``p→q`` crosses — the negotiated
    passes' present-overuse count for the chord resource."""
    return sum(1 for c in cs if _chord_crossed(c, p, q))


def _taut_pts(
    ctx: _Ctx,
    mesh: TopoMesh,
    tris: list[int],
    edges: list[int],
    a: tuple[float, float],
    b: tuple[float, float],
    bounds: Sequence[tuple[float, float]] | None = None,
    ins_mm: float = 0.0,
) -> list[tuple[float, float]]:
    """The funnel taut polyline over the corridor, endpoint to endpoint
    — an endpoint may be any point (a planted via on the far edge of the
    run's last triangle), not only a pin vertex. ``bounds`` (G2h) clips
    each portal to the sub-interval the search committed to — shrunk a
    further ``ins_mm`` from an earlier wire's crossing point (the
    taut path keeps the chosen side of the chord); a portal the inset
    inverts collapses to the interval midpoint."""
    if not edges:
        return [a, b]
    portals: list[tuple[tuple[float, float], tuple[float, float]]] = [(a, a)]
    for i, e in enumerate(edges):
        u, v = ctx.eu[e], ctx.ev[e]
        ends = ctx.edge_ends[e]
        apex = next(
            ctx.tri_xy[tris[i + 1]][j]
            for j, w in enumerate(ctx.tri_verts[tris[i + 1]])
            if w not in ends
        )
        fwd = _area2(u, v, apex) < 0.0
        if bounds is not None:
            lo, hi = bounds[i]
            sl = float(ctx.seg_len[e])
            if sl > _EPS:
                lo = lo + ins_mm / sl if lo > 0.0 else lo
                hi = hi - ins_mm / sl if hi < 1.0 else hi
            if lo >= hi:
                mid_ = 0.5 * (bounds[i][0] + bounds[i][1])
                pa = pb = (
                    u[0] + mid_ * (v[0] - u[0]),
                    u[1] + mid_ * (v[1] - u[1]),
                )
            else:
                pa = (u[0] + lo * (v[0] - u[0]), u[1] + lo * (v[1] - u[1]))
                pb = (u[0] + hi * (v[0] - u[0]), u[1] + hi * (v[1] - u[1]))
            portals.append((pa, pb) if fwd else (pb, pa))
            continue
        portals.append((u, v) if fwd else (v, u))
    portals.append((b, b))
    pts = _funnel(portals)
    return [p for i, p in enumerate(pts) if i == 0 or not _same(p, pts[i - 1])]


def _wedge_tri(ctx: _Ctx, tris, w: int, dxy: tuple[float, float]) -> int | None:
    """The fan tri of ``w`` whose wedge contains direction ``dxy`` — the
    tri a ray from ``w`` along ``dxy`` enters first."""
    for t in sorted(int(x) for x in tris):
        tri = ctx.tri_verts[t]
        if w not in tri:
            continue
        i = tri.index(w)
        xy = ctx.tri_xy[t]
        nxt = (
            xy[(i + 1) % 3][0] - xy[i][0],
            xy[(i + 1) % 3][1] - xy[i][1],
        )
        prv = (
            xy[(i - 1) % 3][0] - xy[i][0],
            xy[(i - 1) % 3][1] - xy[i][1],
        )
        if _cross(nxt, dxy) >= -1e-12 and _cross(dxy, prv) >= -1e-12:
            return t
    return None


def _in_tri(ctx: _Ctx, t: int, p: tuple[float, float]) -> bool:
    a, b, c = ctx.tri_xy[t]
    return (
        _area2(a, b, p) >= -1e-9
        and _area2(b, c, p) >= -1e-9
        and _area2(c, a, p) >= -1e-9
    )


def _tri_exit(
    ctx: _Ctx,
    t: int,
    skip: int,
    p: tuple[float, float],
    q: tuple[float, float],
) -> tuple[str, int, float, float] | None:
    """Where the ray ``p→q`` leaves tri ``t``: ``('edge', e, s, r)`` for
    an interior crossing (s on the stored u→v, r along p→q), or
    ``('vertex', w, 0, r)`` when it exits exactly through vertex ``w``.
    The earliest exit wins; the just-crossed ``skip`` edge is ignored."""
    best: tuple[str, int, float, float] | None = None
    px, py = p
    d_sx, d_sy = q[0] - px, q[1] - py
    for e in ctx.tri_edge_list[t]:
        if e == skip:
            continue
        ux, uy = ctx.eu[e]
        vx, vy = ctx.ev[e]
        ex, ey = vx - ux, vy - uy
        denom = d_sx * ey - d_sy * ex
        if abs(denom) <= _EPS:
            continue
        wx, wy = ux - px, uy - py
        s = (wx * d_sy - wy * d_sx) / denom
        r = (wx * ey - wy * ex) / denom
        if r <= 1e-9 or s < -1e-9 or s > 1.0 + 1e-9:
            continue
        if best is not None and r >= best[3] - 1e-9:
            continue
        if s <= 1e-9:
            best = ("vertex", ctx.edge_ends[e][0], 0.0, r)
        elif s >= 1.0 - 1e-9:
            best = ("vertex", ctx.edge_ends[e][1], 0.0, r)
        else:
            best = ("edge", e, s, r)
    return best


def _fan_ring(ctx: _Ctx, w: int) -> list[tuple[int, int]]:
    """The cyclic fan around interior vertex ``w``: ``(tri, spoke_edge)``
    pairs where ``spoke_edge`` leads to the next fan tri. Deterministic —
    starts at the lowest tri id and takes the lowest valid spoke. Cached
    on the context: one build per vertex, every vertex passage reads it."""
    ring = ctx.fan_ring_cache.get(w)
    if ring is not None:
        return ring
    fan = ctx.v2t[w]
    if not fan:
        ctx.fan_ring_cache[w] = []
        return []
    first = fan[0]
    ring = []
    cur, came = first, -1
    for _ in range(len(fan) + 2):
        nxt = None
        for e in sorted(ctx.tri_edge_list[cur]):
            if e == came or w not in ctx.edge_ends[e]:
                continue
            t2a, t2b = ctx.edge_tris[e]
            t2 = t2a if t2a != cur else t2b
            if t2 >= 0 and w in ctx.tri_set[t2]:
                nxt = (e, t2)
                break
        if nxt is None:
            break
        ring.append((cur, nxt[0]))
        if nxt[1] == first:
            break
        cur, came = nxt[1], nxt[0]
    ctx.fan_ring_cache[w] = ring
    return ring


def _fan_arc(
    ctx: _Ctx,
    w: int,
    t_in: int,
    d_in: tuple[float, float],
    d_out: tuple[float, float],
    passable=None,
) -> tuple[int, list[tuple[int, int]]]:
    """The fan-arc walk for a vertex-exact passage at ``w``: from entry
    tri ``t_in`` to the wedge holding ``d_out``, sweeping the arc on the
    OUTER side of the bend — the side the search corridor took. A taut
    bend wraps ``w`` away from the chord side; sweeping the turn
    (inner) side would route the wire through walls the corridor
    avoided. When a leg runs ALONG a spoke the wedge picks a side by
    triangle id — wrong half the time (round-2 finding 1): the picks
    are nudged to the outer side so the arc can never sweep back
    across the collinear spoke. A straight pass-through has no outer
    side — keep the corridor on the incoming tri's side and take the
    shorter arc. Returns ``(exit_tri, [(spoke_edge, entered_tri), ...])``."""
    ring = _fan_ring(ctx, w)
    pos_of = {tri: i for i, (tri, _e) in enumerate(ring)}
    if t_in not in pos_of:
        return t_in, []
    ring_tris = [tri for tri, _e in ring]

    def biased(
        d: tuple[float, float], toward: tuple[float, float]
    ) -> tuple[float, float]:
        """``d`` nudged toward ``toward``'s side — breaks a
        spoke-collinear wedge tie to the wanted side, leaves an
        interior direction on its own wedge."""
        l = math.hypot(d[0], d[1])
        if l <= _EPS:
            return d
        px, py = -d[1] / l, d[0] / l
        s = px * toward[0] + py * toward[1]
        if abs(s) <= 1e-12:
            return d
        if s < 0.0:
            px, py = -px, -py
        eps = 1e-9 * l
        return (d[0] + eps * px, d[1] + eps * py)

    # outward bisector: unit(d_in) - unit(d_out) points away from the
    # bend's interior; degenerate (~0) on a straight pass-through
    li = math.hypot(d_in[0], d_in[1])
    lo = math.hypot(d_out[0], d_out[1])
    away: tuple[float, float] | None = None
    if li > _EPS and lo > _EPS:
        ax, ay = d_in[0] / li - d_out[0] / lo, d_in[1] / li - d_out[1] / lo
        if ax * ax + ay * ay > 1e-12:
            away = (ax, ay)
    n = len(ring)

    def arc_to(i_c: int, fwd_dir: bool) -> list[tuple[int, int]]:
        """The swept ``(spoke, entered_tri)`` list from ``t_in`` to the
        wedge at ring index ``i_c``, walking in ``fwd_dir``."""
        i_in = pos_of[t_in]
        out: list[tuple[int, int]] = []
        i = i_in
        while i != i_c:
            if fwd_dir:
                e = ring[i][1]
                i = (i + 1) % n
            else:
                i = (i - 1) % n
                e = ring[i][1]
            out.append((e, ring[i][0]))
        return out

    if away is not None:
        # the entry wedge stays the walker's actual tri: rewriting it
        # to the away side would lie about the recorded corridor — a
        # corridor that flips sides of a collinear incoming spoke emits
        # that spoke once as a real vertex crossing via the arc below
        t_out = _wedge_tri(ctx, ring_tris, w, biased(d_out, away))
        if t_out is None or t_out == t_in:
            return t_in, []
        i_in, i_out = pos_of[t_in], pos_of[t_out]
        fwd = (i_out - i_in) % n
        go_fwd = fwd <= n - fwd
        # the outer arc sweeps directions from -d_in to d_out through
        # the away bisector — measured in angle space, because 'away'
        # can land inside an end wedge on a shallow bend and a wedge
        # probe then picks the inner side (round-2 finding 1)
        a_in = math.atan2(-d_in[1], -d_in[0])
        a_out = math.atan2(d_out[1], d_out[0])
        a_aw = math.atan2(away[1], away[0])
        tau = 2.0 * math.pi
        outer_ccw = (a_aw - a_in) % tau <= (a_out - a_in) % tau
        # ring orientation: the entry wedge's interior spans previous
        # spoke -> forward spoke; a <180 CCW span means forward is CCW
        i_w = ctx.tri_verts[t_in].index(w)
        wxy = ctx.tri_xy[t_in][i_w]

        def sdir(e: int) -> float:
            u2, _v2 = ctx.edge_ends[e]
            fx, fy = ctx.ev[e] if u2 == w else ctx.eu[e]
            return math.atan2(fy - wxy[1], fx - wxy[0])

        fwd_ccw = (sdir(ring[i_in][1]) - sdir(ring[(i_in - 1) % n][1])) % tau < math.pi
        go_fwd = outer_ccw == fwd_ccw
        swept = arc_to(i_out, go_fwd)
    else:
        # straight pass-through: no outer side. When the exit direction
        # runs ALONG a spoke both adjacent wedges hold the path — take
        # the one whose corridor (approached without crossing the
        # spoke) stays passable; otherwise hold the incoming tri's side
        i_w = ctx.tri_verts[t_in].index(w)
        wxy = ctx.tri_xy[t_in][i_w]
        others = [ctx.tri_xy[t_in][j] for j in range(3) if j != i_w]
        rel = (
            others[0][0] + others[1][0] - 2.0 * wxy[0],
            others[0][1] + others[1][1] - 2.0 * wxy[1],
        )
        i_spoke = None
        for i_r, (_t, e) in enumerate(ring):
            u2, _v2 = ctx.edge_ends[e]
            fx, fy = ctx.ev[e] if u2 == w else ctx.eu[e]
            sx, sy = fx - wxy[0], fy - wxy[1]
            if (
                lo > _EPS
                and abs(d_out[0] * sy - d_out[1] * sx) <= 1e-9 * lo * math.hypot(sx, sy)
                and d_out[0] * sx + d_out[1] * sy > 0.0
            ):
                i_spoke = i_r
                break
        if i_spoke is not None and passable is not None:
            scored = []
            # ring[i_spoke].tri is entered forward without crossing the
            # spoke; ring[i_spoke+1].tri backward — both sides offered
            for i_c, gd in ((i_spoke, True), ((i_spoke + 1) % n, False)):
                sw = arc_to(i_c, gd)
                bad = sum(1 for e, _t in sw if not passable(e))
                scored.append((bad, len(sw), i_c, sw))
            scored.sort(key=lambda o: (o[0], o[1], o[2]))
            bad, _ln, i_c, sw = scored[0]
            if bad == 0:
                return ring[i_c][0], sw
        t_out = _wedge_tri(ctx, ring_tris, w, biased(d_out, rel))
        if t_out is None or t_out == t_in:
            return t_in, []
        i_in, i_out = pos_of[t_in], pos_of[t_out]
        fwd = (i_out - i_in) % n
        go_fwd = fwd <= n - fwd
        swept = arc_to(i_out, go_fwd)
    # a side-ambiguous pick can sweep the arc through a wall; the other
    # side holds the same polyline, so take it when it is clean —
    # otherwise keep the primary arc and let the caller's validation
    # fall back to the search corridor
    if passable is not None and any(not passable(e) for e, _t in swept):
        alt = arc_to(i_out, not go_fwd)
        if all(passable(e) for e, _t in alt):
            swept = alt
    return ring[i_out][0], swept


def _retrack(
    ctx: _Ctx,
    mesh: TopoMesh,
    va: int,
    vb: int,
    pts: list[tuple[float, float]],
    passable=None,
    start: tuple[int, int] | None = None,
    end_skip: int = -1,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[float, ...]]:
    """Re-derive the corridor by walking the taut polyline through the
    mesh: ``(tris, edges, t)`` where tris has len(edges)+1, edge i
    separates tris i and i+1, and ``t`` is the crossing parameter on the
    stored u→v of each edge. Vertex-exact passages emit the fan spokes
    swept on the turn side — the ties the order pass resolves by
    divergence. ``start = (tri, skip_edge)`` begins the walk in ``tri``
    at ``pts[0]`` — a point on ``skip_edge``'s interior (a planted via
    ends the previous run there) — instead of the wedge lookup, with
    ``skip_edge`` never recorded first; ``end_skip`` drops a trailing
    recording of the end hop's edge (the endpoint sits on it)."""
    if start is None:
        start_t = _wedge_tri(
            ctx, ctx.v2t[va], va, (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
        )
        if start_t is None:
            raise ValueError("retrack: no start wedge")
        tri = start_t
        skip = -1
    else:
        tri, skip = start
    tris: list[int] = [tri]
    edges: list[int] = []
    t_out: list[float] = []
    pos = pts[0]
    guard = 0
    seen_v: set[tuple[int, int]] = set()
    for i in range(1, len(pts)):
        q = pts[i]
        while True:
            guard += 1
            if guard > 8 * len(mesh.edges):
                raise ValueError("retrack: runaway walk")
            if _in_tri(ctx, tri, q):
                pos = q
                break
            hit = _tri_exit(ctx, tri, skip, pos, q)
            if hit is None:
                raise ValueError("retrack: stuck")
            kind, a_, _b_, r = hit
            if kind == "edge":
                e = a_
                # both callers reject a corridor touching a blocked
                # edge — stop here rather than walking the rest
                if passable is not None and not passable(e):
                    raise ValueError("retrack: blocked edge")
                edges.append(e)
                t_out.append(_b_)
                t2a, t2b = ctx.edge_tris[e]
                t2 = t2a if t2a != tri else t2b
                if t2 < 0:
                    raise ValueError("retrack: left the mesh")
                tri = t2
                tris.append(tri)
                skip = e
                pos = (pos[0] + r * (q[0] - pos[0]), pos[1] + r * (q[1] - pos[1]))
                continue
            w = a_
            wpos = (float(mesh.verts[w, 0]), float(mesh.verts[w, 1]))
            if _same(q, wpos) and i == len(pts) - 1:
                pos = q
                break  # the path ends at vertex vb — not a crossing
            # find where the path leaves w: the first later point off w
            d_out = None
            for j in range(i + 1, len(pts)):
                if not _same(pts[j], wpos):
                    d_out = (pts[j][0] - wpos[0], pts[j][1] - wpos[1])
                    break
            if d_out is None:
                d_out = (q[0] - wpos[0], q[1] - wpos[1])
            if (w, tri) in seen_v:
                # the sweep returned to a vertex from the same tri —
                # a two-vertex ping-pong the corridor has to break
                raise ValueError("retrack: vertex loop")
            seen_v.add((w, tri))
            tri, swept = _fan_arc(
                ctx, w, tri, (q[0] - pos[0], q[1] - pos[1]), d_out, passable
            )
            for e, t2 in swept:
                if passable is not None and not passable(e):
                    raise ValueError("retrack: blocked edge")
                edges.append(e)
                t_out.append(_edge_param_at(ctx, e, wpos))
                tris.append(t2)
            skip = -1
            pos = wpos
            if _same(q, wpos):
                break  # bend exactly at w; next segment takes over
    # cancel back-to-back e,e pairs — a spoke swept then re-crossed is a
    # corridor spike, not a real crossing (round-2 finding 1): dropping
    # the pair and the middle tri keeps the word reduced
    i = 0
    while i < len(edges) - 1:
        if edges[i] == edges[i + 1]:
            if tris[i] != tris[i + 2]:
                raise ValueError("retrack: uncancellable edge repeat")
            del edges[i : i + 2]
            del t_out[i : i + 2]
            del tris[i + 1 : i + 3]
            i = max(0, i - 1)
            continue
        i += 1
    if edges and edges[-1] == end_skip:
        # the endpoint sits ON the end hop's edge — the walk recorded
        # crossing it at r≈1 into a triangle the run never enters
        edges.pop()
        t_out.pop()
        tris.pop()
    return tuple(tris), tuple(edges), tuple(t_out)


def _edge_param_at(ctx: _Ctx, e: int, p: tuple[float, float]) -> float:
    """The parameter on edge ``e``'s stored u→v of point ``p``."""
    ux, uy = ctx.eu[e]
    ex, ey = ctx.ev[e][0] - ux, ctx.ev[e][1] - uy
    dd = ex * ex + ey * ey
    if dd <= _EPS:
        return 0.0
    return min(1.0, max(0.0, ((p[0] - ux) * ex + (p[1] - uy) * ey) / dd))


def obstacle_vertices(mesh: TopoMesh, d: TopoDesign, n_layers: int) -> frozenset[int]:
    """The mesh vertices around which a wire side choice can LOCK —
    gr536410's braid-obstacle definition, the same predicate
    :func:`_shortcut` uses to keep wraps: pin vertices plus endpoints of
    board (outline) edges and zone (keepout) edges. Every other ring
    vertex is FREE — two wires may pass on opposite sides of it without
    a topological crossing, so an order flip localised to free vertices
    is a fan, not a braid."""
    obs = {int(v) for v in mesh.pin_vertex}
    edge_owner = getattr(mesh, "edge_owner", None)
    if edge_owner is None or len(edge_owner) != len(mesh.edges):
        # hand-built mesh (synth fixtures): no board/zone edge ownership
        # was ever recorded — pin vertices are the only obstacles.
        return frozenset(obs)
    ctx = _make_ctx(mesh, d, n_layers)
    for e in range(len(mesh.edges)):
        if ctx.board[e] or e in ctx.zone_contrib:
            obs.update(ctx.edge_ends[e])
    return frozenset(obs)


def _shortcut(
    ctx: _Ctx,
    mesh: TopoMesh,
    va: int,
    vb: int,
    pts: list[tuple[float, float]],
    passable,
    pin_vids: frozenset[int],
    corridor_ok=None,
    start: tuple[int, int] | None = None,
    end_skip: int = -1,
) -> list[tuple[float, float]]:
    """Release wraps the search corridor forced around NON-obstacle
    vertices (courtyard corners, ring-crossing vertices): if removing a
    bend keeps the re-walked path on passable edges, the shorter class
    wins (seam finding 7). Pads, zone corners and outline corners are
    obstacles and keep their wraps. ``corridor_ok`` (G2c) is a further
    test on the re-walked edges — the layered search chose a corridor
    where the layers and vias work out, and a shorter class that leaves
    no legal layering is not a release."""
    incident_blocked: dict[int, bool] = {}

    def obstacle(w: int) -> bool:
        if w in pin_vids:
            return True
        cached = incident_blocked.get(w)
        if cached is not None:
            return cached
        obs = False
        for t in ctx.v2t[w]:
            for e in ctx.tri_edge_list[t]:
                if w in ctx.edge_ends[e] and (ctx.board[e] or e in ctx.zone_contrib):
                    obs = True
                    break
            if obs:
                break
        incident_blocked[w] = obs
        return obs

    out = list(pts)
    # a failed removal only becomes worth retrying when a neighbour
    # moved — key on the candidate triple so the restart scan skips
    # re-walks it already paid for (round-4 perf: the re-tries were
    # most of _shortcut's _retrack calls)
    failed: set[
        tuple[tuple[float, float], tuple[float, float], tuple[float, float]]
    ] = set()
    for _ in range(3):
        changed = False
        for i in range(1, len(out) - 1):
            w = _vertex_at(ctx, mesh, out[i])
            if w is None or obstacle(w):
                continue
            key = (out[i - 1], out[i], out[i + 1])
            if key in failed:
                continue
            cand = out[:i] + out[i + 1 :]
            try:
                _t2, edges2, _tt = _retrack(
                    ctx, mesh, va, vb, cand, passable, start, end_skip
                )
            except ValueError:
                failed.add(key)
                continue
            if all(passable(int(e)) for e in edges2) and (
                corridor_ok is None or corridor_ok(edges2)
            ):
                out = cand
                changed = True
                break
            failed.add(key)
        if not changed:
            return out
    return out


def _vertex_at(ctx: _Ctx, mesh: TopoMesh, p: _V2) -> int | None:
    v = ctx.vid_of.get((round(float(p[0]), 6), round(float(p[1]), 6)))
    if v is not None and float(((mesh.verts[v] - p) ** 2).sum()) <= 1e-12:
        return v
    d2 = (mesh.verts - np.asarray(p)) ** 2
    i = int(np.argmin(d2[:, 0] + d2[:, 1]))
    return i if float(d2[i, 0] + d2[i, 1]) <= 1e-12 else None


def _order(
    ctx: _Ctx, mesh: TopoMesh, branches: tuple[WirePath, ...]
) -> tuple[tuple[tuple[int, ...], ...], tuple[tuple[int, ...], ...], int]:
    """Decision 3's per-edge ordered wire lists: branches sorted by their
    taut crossing parameter. Ties sit at a wrapped vertex ``w`` (t is 0
    or 1); they sort by wrap TIGHTNESS — ``|u_in + u_out|`` over the
    unit leg directions at the bend. A sharper bend sweeps the wider
    arc, so its crossing sits nearer ``w``: first at the ``u`` end, last
    at the ``v`` end — the sign flip keeps one transverse order on every
    edge of a shared run (seam finding 1); ``t`` stays geometry only.
    Interior-t ties happen on a shared bend-to-bend run, where both
    wires left the same wrap vertex through the same last spoke: the
    key then propagates that wrap's signed tightness, so the order
    fixed at the run's first vertex holds across the whole run (seam
    round-2 finding 6). Where the heuristic still leaves a shared run
    with an inconsistent normalized order, a repair pass carries the
    sign the pair shows where it separates across the tied edges —
    the divergence order the crossing graph needs, made exact.

    One entry per CROSSING: a lollipop corridor (a pocket via the wire
    spikes to and returns from on the other layer, G2g) crosses the
    stem edges twice, so the branch is listed twice — ``order_pos``
    carries each entry's index into the branch's ``edges`` so the
    occurrences stay distinguishable."""
    tri_cent = [
        (
            (xy[0][0] + xy[1][0] + xy[2][0]) / 3.0,
            (xy[0][1] + xy[1][1] + xy[2][1]) / 3.0,
        )
        for xy in ctx.tri_xy
    ]
    wrap: list[dict[int, float]] = []
    for br in branches:
        pts = br.points
        w_at: dict[int, float] = {}
        for j in range(1, len(pts) - 1):
            v = _vertex_at(ctx, mesh, np.asarray(pts[j]))
            if v is None:
                continue
            a = np.asarray(pts[j - 1]) - np.asarray(pts[j])
            b = np.asarray(pts[j + 1]) - np.asarray(pts[j])
            la = math.hypot(float(a[0]), float(a[1]))
            lb = math.hypot(float(b[0]), float(b[1]))
            if la <= _EPS or lb <= _EPS:
                continue
            w_at[v] = math.hypot(
                float(a[0]) / la + float(b[0]) / lb,
                float(a[1]) / la + float(b[1]) / lb,
            )
        wrap.append(w_at)
    # corridor-vs-edge direction sign per crossing position — the same
    # normalization the seam test applies (travel direction against the
    # stored u→v); a tied order that flips with it keeps the normalized
    # transverse side constant along a bending corridor
    cdir: list[list[float]] = []
    for br in branches:
        row = []
        for i, e in enumerate(br.edges):
            ca, cb = tri_cent[br.tris[i]], tri_cent[br.tris[i + 1]]
            ux, uy = ctx.eu[e]
            cr = (cb[0] - ca[0]) * (ctx.ev[e][1] - uy) - (cb[1] - ca[1]) * (
                ctx.ev[e][0] - ux
            )
            row.append(-1.0 if cr < -1e-12 else 1.0)
        cdir.append(row)
    order: list[list[tuple[float, float, int, int, int]]] = [
        [] for _ in range(len(mesh.edges))
    ]
    for bi, br in enumerate(branches):
        w_at = wrap[bi]
        # backward pass: the NEXT endpoint-tied (vertex, sign) after each
        # position — a run that starts before the wire's first wrap
        # (e.g. at a shared pad) still has an end vertex to inherit from
        nxt: list[tuple[int, float] | None] = [None] * len(br.edges)
        n: tuple[int, float] | None = None
        for i in range(len(br.edges) - 1, -1, -1):
            nxt[i] = n
            te_i = br.t[i]
            if te_i <= 1e-9:
                n = (ctx.edge_ends[br.edges[i]][0], -1.0)
            elif te_i >= 1.0 - 1e-9:
                n = (ctx.edge_ends[br.edges[i]][1], 1.0)
        last: tuple[int, float] | None = None  # last wrapped (vertex, sign)
        for i, (e, te) in enumerate(zip(br.edges, br.t)):
            if te <= 1e-9:
                last = (ctx.edge_ends[e][0], -1.0)
            elif te >= 1.0 - 1e-9:
                last = (ctx.edge_ends[e][1], 1.0)
            ref = last if last is not None else nxt[i]
            key = ref[1] * w_at.get(ref[0], 0.0) if ref is not None else 0.0
            order[e].append(
                (float(te), cdir[bi][i] * key, int(cdir[bi][i]) * bi, bi, i)
            )
    ents: list[list[tuple[int, int]]] = [
        [(bi, i) for _t, _k, _c, bi, i in sorted(lst)] for lst in order
    ]
    residual = _repair_order(ctx, branches, cdir, ents)
    ids = tuple(tuple(bi for bi, _i in lst) for lst in ents)
    poss = tuple(tuple(i for _bi, i in lst) for lst in ents)
    return ids, poss, residual


def _repair_order(
    ctx: _Ctx,
    branches: tuple[WirePath, ...],
    cdir: list[list[float]],
    ents: list[list[tuple[int, int]]],
) -> int:
    """Align tied pairs on a shared run to one normalized order.

    The normalized side of two wires on edge ``e`` is their position in
    ``ents[e]`` signed by the trailing wire's travel direction against
    the stored u→v. A real separation fixes that sign on untied edges;
    the same sign must hold on edges where the crossings tie exactly —
    otherwise the crossing graph reads a phantom order flip. For each
    run of shared edges we take the pair's sign at the first edge where
    they separate (the first edge, when the whole run is tied) and move
    the violating partner inside the equal-``t`` group to match. Every
    co-tied pair of a group contributes its constraint — a pair split
    by a third wire in the group is still reordered by the fix, so
    collecting only adjacent pairs would leave its want unspoken and
    let the reorder create the flip it was meant to remove. A want
    anchored on an untied edge outranks an all-tied run's keep-as-is
    want when the two cycle inside one group. Ties are exact
    (``te`` bitwise equal): a near-tie order is geometry, not ours to
    flip.

    ``ents[e]`` entries are ``(branch, position)`` — position the
    crossing's index into the branch's ``edges``. A lollipop corridor
    lists one branch twice on the stem edges; the runs, norms and fixes
    all carry positions so the two traversals never alias.
    """
    lpos: list[dict[int, list[int]]] = []
    for br in branches:
        m: dict[int, list[int]] = {}
        for i, e in enumerate(br.edges):
            m.setdefault(int(e), []).append(i)
        lpos.append(m)

    def norm(e: int, a: int, b: int, ia: int, ib: int) -> int:
        lst = ents[e]
        s = 1 if lst.index((a, ia)) < lst.index((b, ib)) else -1
        return s * int(cdir[b][ib])

    def runs_of(a: int, b: int) -> list[list[tuple[int, int, int]]]:
        """The pair's shared corridor stretches as ``(edge, pos_a,
        pos_b)`` triples — occurrence-aligned: a wire crossing an edge
        twice (lollipop stem) matches the continuation next to the
        previous match, forward or reversed."""
        ea, lb = branches[a].edges, lpos[b]
        used: set[int] = set()
        out: list[list[tuple[int, int, int]]] = []
        i = 0
        while i < len(ea):
            start = [p for p in lb.get(int(ea[i]), ()) if p not in used]
            if not start:
                i += 1
                continue
            run = None
            for p0 in start:
                cand = [(int(ea[i]), i, p0)]
                last, s = p0, 0
                j = i + 1
                while j < len(ea):
                    nx = [p for p in lb.get(int(ea[j]), ()) if p not in used]
                    if s:
                        nx = [p for p in nx if p - last == s]
                    else:
                        nx = [p for p in nx if abs(p - last) == 1]
                    if not nx:
                        break
                    p2 = min(nx, key=lambda p: (abs(p - last), p))
                    if not s:
                        s = p2 - last
                    cand.append((int(ea[j]), j, p2))
                    last = p2
                    j += 1
                if len(cand) >= 2:
                    run = cand
                    break
            if run is None:
                i += 1
                continue
            for _e, _ia, _ib in run:
                used.add(_ib)
            out.append(run)
            i = run[-1][1] + 1
        return out

    def tied(a: int, b: int, ia: int, ib: int) -> bool:
        return abs(branches[a].t[ia] - branches[b].t[ib]) <= 1e-9

    def groups(e: int) -> list[tuple[int, int]]:
        """Contiguous equal-``t`` spans of the sorted list — the units a
        fix may reorder without touching real geometry."""
        lst = ents[e]
        out = []
        i = 0
        while i < len(lst):
            lo = hi = branches[lst[i][0]].t[lst[i][1]]
            j = i + 1
            while j < len(lst):
                te_j = branches[lst[j][0]].t[lst[j][1]]
                if max(hi, te_j) - min(lo, te_j) > 1e-9:
                    break
                lo, hi = min(lo, te_j), max(hi, te_j)
                j += 1
            out.append((i, j))
            i = j
        return out

    pair_runs: dict[tuple[int, int], list[list[tuple[int, int, int]]]] = {}

    def shared_runs(a: int, b: int) -> list[list[tuple[int, int, int]]]:
        key = (a, b) if a < b else (b, a)
        got = pair_runs.get(key)
        if got is None:
            got = runs_of(*key)
            pair_runs[key] = got
        return got

    def slot(e: int) -> dict[tuple[int, int], int]:
        return {en: r for r, en in enumerate(ents[e])}

    for _round in range(4):
        # (lead, trail, anchored) per edge — anchored wants come from a
        # run with an untied edge; all-tied runs only ask to keep the
        # order they show at run[0] and yield on conflict
        fixes: dict[int, list[tuple[tuple[int, int], tuple[int, int], bool]]] = {}
        seen: set[tuple[int, int]] = set()
        for e, lst in enumerate(ents):
            for i, j in groups(e):
                for ea_, eb_ in itertools.combinations(lst[i:j], 2):
                    # one orientation per pair: a reversed run's norm is
                    # the same consistency condition (negated
                    # same-direction, identical opposite-direction)
                    key = (ea_[0], eb_[0]) if ea_[0] < eb_[0] else (eb_[0], ea_[0])
                    if key in seen:
                        continue
                    seen.add(key)
                    if branches[key[0]].net == branches[key[1]].net:
                        continue
                    a, b = key
                    for run in shared_runs(a, b):
                        want = next(
                            (
                                norm(e2, a, b, ia, ib)
                                for e2, ia, ib in run
                                if not tied(a, b, ia, ib)
                            ),
                            None,
                        )
                        anchored = want is not None
                        if want is None:
                            e0, ia0, ib0 = run[0]
                            want = norm(e0, a, b, ia0, ib0)
                        for e2, ia, ib in run:
                            if tied(a, b, ia, ib) and norm(e2, a, b, ia, ib) != want:
                                lead, trail = (
                                    ((a, ia), (b, ib))
                                    if want * cdir[b][ib] > 0
                                    else ((b, ib), (a, ia))
                                )
                                fixes.setdefault(e2, []).append((lead, trail, anchored))
        if not fixes:
            break
        for e2 in sorted(fixes):
            rel = fixes[e2]
            sl = slot(e2)
            for i, j in groups(e2):
                rel_g = sorted(
                    {
                        (sl[c[0]] - i, sl[c[1]] - i)
                        for c in rel
                        if c[2]
                        and i <= sl.get(c[0], -1) < j
                        and i <= sl.get(c[1], -1) < j
                    }
                ) + sorted(
                    {
                        (sl[c[0]] - i, sl[c[1]] - i)
                        for c in rel
                        if not c[2]
                        and i <= sl.get(c[0], -1) < j
                        and i <= sl.get(c[1], -1) < j
                    }
                )
                if rel_g:
                    ents[e2][i:j] = _topo_group(ents[e2][i:j], rel_g)
                    sl = slot(e2)
    # residual: tied-pair violations the rounds could not settle
    # (cycle-dropped constraints or the round cap) — 2c's parity check
    # wants this surfaced, not silent (round-3 finding R3-3)
    leftover = 0
    counted: set[tuple[int, int]] = set()
    for e, lst in enumerate(ents):
        for i, j in groups(e):
            for ea_, eb_ in itertools.combinations(lst[i:j], 2):
                key = (ea_[0], eb_[0]) if ea_[0] < eb_[0] else (eb_[0], ea_[0])
                if key in counted:
                    continue
                counted.add(key)
                if branches[key[0]].net == branches[key[1]].net:
                    continue
                a, b = key
                for run in shared_runs(a, b):
                    want = next(
                        (
                            norm(e2, a, b, ia, ib)
                            for e2, ia, ib in run
                            if not tied(a, b, ia, ib)
                        ),
                        None,
                    )
                    if want is None:
                        e0, ia0, ib0 = run[0]
                        want = norm(e0, a, b, ia0, ib0)
                    leftover += sum(
                        1
                        for e2, ia, ib in run
                        if tied(a, b, ia, ib) and norm(e2, a, b, ia, ib) != want
                    )
    return leftover


def _topo_group(
    grp: list[tuple[int, int]], rel: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """The equal-``t`` group reordered to satisfy ``(lead, trail)``
    constraints — deterministic (current position breaks ties).
    Constraints apply in the given order; one that would close a cycle
    is dropped, so a conflicting round still yields a consistent order.
    ``rel`` indexes positions inside ``grp`` — entries, not values, so
    a branch listed twice stays unambiguous."""
    n = len(grp)
    after: list[set[int]] = [set() for _ in range(n)]

    def reaches(src: int, dst: int) -> bool:
        stack, seen = [src], {src}
        while stack:
            for y in after[stack.pop()]:
                if y == dst:
                    return True
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return False

    indeg: list[int] = [0] * n
    for lead, trail in rel:
        if trail in after[lead] or reaches(trail, lead):
            continue
        after[lead].add(trail)
        indeg[trail] += 1
    out: list[tuple[int, int]] = []
    done = [False] * n
    avail = sorted(x for x in range(n) if indeg[x] == 0)
    while avail:
        x = avail.pop(0)
        done[x] = True
        out.append(grp[x])
        for y in sorted(after[x]):
            indeg[y] -= 1
            if indeg[y] == 0:
                bisect.insort(avail, y)
    return out + [grp[x] for x in range(n) if not done[x]]


def _edge_capacity_mm(
    ctx: _Ctx, mesh: TopoMesh, d: TopoDesign, pose: np.ndarray
) -> np.ndarray:
    """Per-edge free crossing length — the corridor-cut input slice 4
    prices demand against: edge length minus each endpoint pad's extent
    projected onto the edge direction, read through the part pose so a
    quarter-turn or a bottom-side mirror prices the pad's real world
    shadow (the per-layer split lands with slice 4's corridor model)."""
    vid_pin: dict[int, int] = {}
    for p in range(len(mesh.pin_vertex)):
        vid_pin[int(mesh.pin_vertex[p])] = p
    cap = np.zeros(len(mesh.edges), dtype=np.float64)
    for e in range(len(mesh.edges)):
        a, b = mesh.verts[mesh.edges[e, 0]], mesh.verts[mesh.edges[e, 1]]
        dvec = b - a
        length = float(np.hypot(dvec[0], dvec[1]))
        cap[e] = length
        if length <= _EPS:
            continue
        ux, uy = float(dvec[0]) / length, float(dvec[1]) / length
        for vid in (int(mesh.edges[e, 0]), int(mesh.edges[e, 1])):
            pin = vid_pin.get(vid)
            if pin is None or len(d.pin_wh) <= pin:
                continue
            part = int(d.pin_part[pin])
            rot_q = round(float(pose[part, 2])) % 4 if pose.shape[1] > 2 else 0
            side = float(pose[part, 3]) if pose.shape[1] > 3 else 0.0
            # world edge dir -> footprint-local frame: un-rotate, un-mirror
            if rot_q == 0:
                lx, ly = ux, uy
            elif rot_q == 1:
                lx, ly = -uy, ux
            elif rot_q == 2:
                lx, ly = -ux, -uy
            else:
                lx, ly = uy, -ux
            if side >= 0.5:
                lx = -lx
            w, h = float(d.pin_wh[pin, 0]), float(d.pin_wh[pin, 1])
            cap[e] -= (w * abs(lx) + h * abs(ly)) / 2.0
        if cap[e] < 0.0:
            cap[e] = 0.0
    return cap


def _flood_region(
    ctx: _Ctx,
    mesh: TopoMesh,
    pins: list[int],
    base_pass: np.ndarray,
) -> set[int]:
    """The triangles a search could expand through from these pins —
    plain reachability over ``base_pass``: under net-level exemption
    the mask already opens every own-net pin's walls, so what it
    cannot reach is genuinely walled off."""
    seen: set[int] = set()
    stack: list[int] = []
    for pin in pins:
        for t in ctx.v2t[int(mesh.pin_vertex[pin])]:
            if t not in seen:
                seen.add(t)
                stack.append(t)
    while stack:
        t = stack.pop()
        for e in (int(x) for x in mesh.tri_edges[t]):
            if not base_pass[e]:
                continue
            t2 = int(mesh.edge_tris[e, 0])
            t2 = int(mesh.edge_tris[e, 1]) if t2 == t else t2
            if t2 >= 0 and t2 not in seen:
                seen.add(t2)
                stack.append(t2)
    return seen


def _flood_states(
    ctx: _Ctx,
    mesh: TopoMesh,
    pins: list[int],
    pin_lay: list[int],
    open_v: np.ndarray,
    fit: np.ndarray,
    cand_list: list[int],
) -> set[tuple[int, int]]:
    """The ``(tri, layer)`` states a comp's pins reach under the view's
    effective passability — ``_flood_region`` made layered: a step keeps
    its layer on an edge open there, a layer change travels an edge a
    via plants on (``fit``), chords aside. What separates two comps
    under this is a real closure — a consumed channel — not a chord."""
    seen: set[tuple[int, int]] = set()
    stack: list[tuple[int, int]] = []
    for pin, pl in zip(pins, pin_lay, strict=True):
        for t in ctx.v2t[int(mesh.pin_vertex[pin])]:
            for ly in cand_list:
                if pl >> ly & 1 and (t, ly) not in seen:
                    seen.add((t, ly))
                    stack.append((t, ly))
    while stack:
        t, ly = stack.pop()
        for e in (int(x) for x in mesh.tri_edges[t]):
            if open_v[e] >> ly & 1:
                t2 = int(mesh.edge_tris[e, 0])
                t2 = int(mesh.edge_tris[e, 1]) if t2 == t else t2
                if t2 >= 0 and (t2, ly) not in seen:
                    seen.add((t2, ly))
                    stack.append((t2, ly))
            if fit[e]:
                om = int(open_v[e])
                for m in cand_list:
                    if m != ly and om >> m & 1 and (t, m) not in seen:
                        seen.add((t, m))
                        stack.append((t, m))
    return seen


def _wall_owners(
    ctx: _Ctx,
    mesh: TopoMesh,
    d: TopoDesign,
    own: frozenset[int],
    region: set[int],
    tree_region: set[int],
    cand_mask: int,
    net_name: str,
    r: float,
) -> tuple[int, ...]:
    """What separates an unreachable component's region from the main
    tree's: owners of the blocked edges on the cut between the two
    flooded regions — part ids, ``n_parts + zone`` or OWNER_BOARD (the
    ``edge_owner`` id space). Falls back to the region's whole blocked
    boundary when the regions meet only at vertices."""
    n_parts = len(d.parts)
    _b, _f, gap_pads, _p = _pad_eval(ctx, r)
    cut: set[int] = set()
    boundary: set[int] = set()
    for t in region:
        for e in (int(x) for x in mesh.tri_edges[t]):
            t2 = int(mesh.edge_tris[e, 0])
            t2 = int(mesh.edge_tris[e, 1]) if t2 == t else t2
            if t2 in region:
                continue
            boundary.add(e)
            if t2 >= 0 and t2 in tree_region:
                cut.add(e)

    def blame(e: int, acc: set[int]) -> None:
        if ctx.board[e]:
            acc.add(OWNER_BOARD)
        for pin, pm in ctx.pad_contrib.get(e, ()):
            if pin not in own and pm & cand_mask:
                acc.add(int(d.pin_part[pin]))
        for z, zm in ctx.zone_closed.get(e, ()):
            zone = d.zones[z]
            if zm & cand_mask and not (
                zone.kind in ("fixed_copper", "fixed_via")
                and zone.net
                and zone.net == net_name
            ):
                acc.add(n_parts + z)
        for pi_ in gap_pads.get(e, ()):
            pin = int(ctx.pad_pin[pi_])
            if pin in own:
                continue
            if any(q == pi_ for _a, _b, q in ctx.cover_cache[r].get(e, ())):
                acc.add(int(ctx.pad_part[pi_]))

    owners: set[int] = set()
    for e in cut or boundary:
        blame(e, owners)
    return tuple(sorted(owners))


def _capsule_cover(
    p: np.ndarray, dv: np.ndarray, a: np.ndarray, b: np.ndarray, r: float
) -> tuple[np.ndarray, np.ndarray]:
    """Per row, the parameters ``[lo, hi]`` (clipped to ``[0, 1]``) where
    the segment ``p + t·dv`` lies within ``r`` of the segment ``a→b``;
    ``hi - lo`` is ≤ 1e-12 where they never meet. The ``r``-capsule is
    convex, so its cut with a line is one interval — the hull of the
    cuts with the two end disks and the body slab."""
    n = len(p)
    lo = np.full(n, np.inf)
    hi = np.full(n, -np.inf)
    dd = (dv * dv).sum(axis=1)
    live = dd > _EPS
    with np.errstate(divide="ignore", invalid="ignore"):
        for c in (a, b):
            pc = p - c
            qb = 2.0 * (dv * pc).sum(axis=1)
            disc = qb * qb - 4.0 * dd * ((pc * pc).sum(axis=1) - r * r)
            ok = live & (disc >= 0.0)
            sq = np.sqrt(np.where(ok, disc, 0.0))
            lo = np.where(ok, np.minimum(lo, (-qb - sq) / (2.0 * dd)), lo)
            hi = np.where(ok, np.maximum(hi, (-qb + sq) / (2.0 * dd)), hi)
        ab = b - a
        length = np.hypot(ab[:, 0], ab[:, 1])
        body = live & (length > _EPS)
        u = ab / np.where(length > _EPS, length, 1.0)[:, None]
        nrm = np.stack([-u[:, 1], u[:, 0]], axis=1)
        t_lo = np.full(n, -np.inf)
        t_hi = np.full(n, np.inf)
        for axis, lo_b, hi_b in (
            (u, np.zeros(n), length),
            (nrm, np.full(n, -r), np.full(n, r)),
        ):
            x0 = ((p - a) * axis).sum(axis=1)
            xd = (dv * axis).sum(axis=1)
            flat = np.abs(xd) <= 1e-12
            ta, tb = (lo_b - x0) / xd, (hi_b - x0) / xd
            t_lo = np.maximum(t_lo, np.where(flat, -np.inf, np.minimum(ta, tb)))
            t_hi = np.minimum(t_hi, np.where(flat, np.inf, np.maximum(ta, tb)))
            body &= ~flat | ((x0 >= lo_b - 1e-12) & (x0 <= hi_b + 1e-12))
        body &= t_lo <= t_hi
        lo = np.where(body, np.minimum(lo, t_lo), lo)
        hi = np.where(body, np.maximum(hi, t_hi), hi)
    return np.maximum(lo, 0.0), np.minimum(hi, 1.0)


def _poly_cover(
    ctx: _Ctx, pi: int, edges: np.ndarray, r: float
) -> list[tuple[int, float, float]]:
    """``(edge, a, b)`` stretches of ``edges`` within ``r`` of the
    non-rectangular pad ``pi`` (a comb, a round land). Edges on or inside
    its ring are covered whole; the rest never cross the constrained
    ring, so their cover is the union of the capsules of its boundary
    segments — read off a segment-box index, never a polygon overlay
    (a 900-vertex comb made every overlay a millisecond)."""
    out: list[tuple[int, float, float]] = [
        (int(e), 0.0, 1.0) for e in ctx.pad_inside[pi]
    ]
    rest = np.setdiff1d(edges, ctx.pad_inside[pi], assume_unique=True)
    pts = np.asarray(ctx.pad_poly[pi].exterior.coords)
    s0, s1 = pts[:-1], pts[1:]
    if not len(rest) or not len(s0):
        return out
    seg_boxes = shapely.box(
        np.minimum(s0, s1)[:, 0] - r,
        np.minimum(s0, s1)[:, 1] - r,
        np.maximum(s0, s1)[:, 0] + r,
        np.maximum(s0, s1)[:, 1] + r,
    )
    edge_boxes = shapely.box(
        ctx.edge_lo[rest, 0],
        ctx.edge_lo[rest, 1],
        ctx.edge_hi[rest, 0],
        ctx.edge_hi[rest, 1],
    )
    qi, si = shapely.STRtree(seg_boxes).query(edge_boxes, predicate="intersects")
    if not len(qi):
        return out
    e = rest[qi]
    p = ctx.seg_u[e]
    lo, hi = _capsule_cover(p, ctx.seg_v[e] - p, s0[si], s1[si], r)
    out.extend(
        (int(k), float(a), float(b))
        for k, a, b in zip(
            e[hi - lo > 1e-12], lo[hi - lo > 1e-12], hi[hi - lo > 1e-12], strict=True
        )
    )
    return out


def _merge_len(ints: list[tuple[float, float]]) -> float:
    """Covered measure of ``[0,1]`` under sorted-merge of intervals."""
    ints.sort()
    cov = 0.0
    ca = cb = -math.inf
    for a, b in ints:
        if a <= cb:
            cb = max(cb, b)
        else:
            cov += cb - ca if ca > -math.inf else 0.0
            ca, cb = a, b
    if ca > -math.inf:
        cov += cb - ca
    return min(cov, 1.0)


def _pad_eval(
    ctx: _Ctx, r: float
) -> tuple[np.ndarray, np.ndarray, dict[int, np.ndarray], dict[int, list[int]]]:
    """Coverage of every mesh edge by every pad grown by ``r``
    (``trace/2 + clearance`` — the exclusion a centreline must keep).
    Returns ``(blocked, free, edge_pads, pad_edges)``: ``blocked[e]`` is
    the layer mask where no free stretch of ``e`` survives, ``free[e,
    l]`` the uncovered length in mm (−1 where no pad reaches ``e``), and
    the two maps index candidate pads per edge / candidate edges per
    pad for the per-net own-pad re-eval (the covered stretches behind
    them stay in ``ctx.cover_cache[r]``). Cached per ``r`` — nets
    sharing a rule share the eval (round-4 rework: the old
    endpoint-coincidence test missed every channel split by a non-pad
    vertex, and ran the O(ring²) pair scan per call)."""
    got = ctx.gap_eval_cache.get(r)
    if got is not None:
        return got
    n_e = len(ctx.board)
    n_l = ctx.n_layers
    free = np.full((n_e, n_l), -1.0)
    blocked = np.zeros(n_e, dtype=np.int64)
    edge_pads: dict[int, np.ndarray] = {}
    pad_edges: dict[int, list[int]] = {}
    out = (blocked, free, edge_pads, pad_edges)
    ctx.gap_eval_cache[r] = out
    cover: dict[int, list[tuple[float, float, int]]] = {}
    ctx.cover_cache[r] = cover
    if not len(ctx.pad_pin):
        return out
    # candidate (edge, pad) pairs: the grown pad AABB meets the edge's. A
    # grown mesh puts ring vertices exactly ``r`` + :data:`_GROW_MARGIN` from
    # the copper, so the edge between two of them sits at (or a rounding
    # off) the AABB bound — :data:`_AABB_SLACK_MM`, larger than the margin
    # by construction, keeps it a candidate (its free stretch is the
    # channel's capacity).
    hit = np.logical_and(
        ctx.pad_lo[None] <= ctx.edge_hi[:, None] + r + _AABB_SLACK_MM,
        ctx.pad_hi[None] >= ctx.edge_lo[:, None] - r - _AABB_SLACK_MM,
    ).all(axis=2)
    ei, pi = np.nonzero(hit)
    if not len(ei):
        return out
    # slab clip of every rectangle pair's segment against its grown box
    pu = ctx.seg_u[ei]
    dd = ctx.seg_v[ei] - pu
    plo = ctx.pad_lo[pi] - r
    phi = ctx.pad_hi[pi] + r
    ent = np.zeros(len(ei))
    ext = np.ones(len(ei))
    feas = np.ones(len(ei), dtype=bool)
    for ax in range(2):
        dax = dd[:, ax]
        pax = pu[:, ax]
        big = np.abs(dax) > _EPS
        t0 = np.empty(len(ei))
        t1 = np.empty(len(ei))
        t0[big] = (plo[big, ax] - pax[big]) / dax[big]
        t1[big] = (phi[big, ax] - pax[big]) / dax[big]
        ent[big] = np.maximum(ent[big], np.minimum(t0[big], t1[big]))
        ext[big] = np.minimum(ext[big], np.maximum(t0[big], t1[big]))
        small = ~big
        feas[small] &= (pax[small] >= plo[small, ax] - 1e-12) & (
            pax[small] <= phi[small, ax] + 1e-12
        )
    valid = feas & (ext - ent > 1e-12) & ctx.pad_rect[pi]
    for k in np.flatnonzero(valid):
        cover.setdefault(int(ei[k]), []).append(
            (float(ent[k]), float(ext[k]), int(pi[k]))
        )
    # every other land is covered where it is near the edge as a polygon
    for p_ in np.unique(pi[~ctx.pad_rect[pi]]):
        for e, a, b in _poly_cover(ctx, int(p_), ei[pi == p_], r):
            cover.setdefault(e, []).append((a, b, int(p_)))
    cand: dict[int, set[int]] = {}
    for e, p_ in zip(ei.tolist(), pi.tolist(), strict=True):
        cand.setdefault(e, set()).add(p_)
    # per edge, per layer: merge the covered intervals
    for e, plist in cand.items():
        edge_pads[e] = np.asarray(sorted(plist))
        for p_ in edge_pads[e]:
            pad_edges.setdefault(int(p_), []).append(e)
        ints = cover.get(e, [])
        for ly in range(n_l):
            bit = 1 << ly
            sel = [(a, b) for a, b, q in ints if int(ctx.pad_copper[q]) & bit]
            cov = _merge_len(sel) if sel else 0.0
            free[e, ly] = float(ctx.seg_len[e]) * (1.0 - cov)
            if free[e, ly] <= 1e-9:
                blocked[e] |= bit
    return out


def _edge_free(
    ctx: _Ctx, e: int, r: float, drop: frozenset[int], edge_pads
) -> np.ndarray:
    """``free`` row for edge ``e`` re-merged without the net's own pads
    (``drop`` = pad indices) — the own-net exemption applied to the gap
    rule: a channel off the wire's own pad is measured against foreign
    copper only."""
    free = np.empty(ctx.n_layers)
    ints = [
        (a, b, int(ctx.pad_copper[q]))
        for a, b, q in ctx.cover_cache[r].get(e, ())
        if q not in drop
    ]
    for ly in range(ctx.n_layers):
        bit = 1 << ly
        cov = _merge_len([(a, b) for a, b, m in ints if m & bit]) if ints else 0.0
        free[ly] = float(ctx.seg_len[e]) * (1.0 - cov)
    return free


def _open_layers(
    ctx: _Ctx,
    d: TopoDesign,
    cand_mask: int,
    net_name: str,
    own: frozenset[int],
    trace_mm: float,
    clearance_mm: float,
) -> np.ndarray:
    """The net's per-edge OPEN-layer bitmask — what the any-layer
    passability collapsed (``!= 0``) before G2c: bit ``l`` set where the edge is passable on
    layer ``l`` for this net (own-net pads exempt, board edges always
    0). The layer assignment reads this as the corridor's per-edge open
    set (gr520902 FIX): a run may only sit on a layer every one of its
    edges leaves open."""
    # base blocked layers: every pad wall, then the own-net exemption
    # re-merges only edges on or inside an own pin's pads (round-4
    # perf — the per-net full contrib scan dominated the net masks);
    # zones are cached per net name.
    full_blocked = ctx.pad_block_all.copy()
    for pin in own:
        for e in ctx.pin_wall.get(pin, ()):
            m = 0
            for p2, pm in ctx.pad_contrib.get(e, ()):
                if p2 not in own:
                    m |= pm
            full_blocked[e] = m
    zb = ctx.zone_block.get(net_name)
    if zb is None:
        zb = np.zeros(len(ctx.board), dtype=np.int64)
        for e, lst in ctx.zone_closed.items():
            m = 0
            for z, zm in lst:
                zone = d.zones[z]
                if not (
                    zone.kind in ("fixed_copper", "fixed_via")
                    and zone.net
                    and zone.net == net_name
                ):
                    m |= zm
            if m:
                zb[e] = m
        ctx.zone_block[net_name] = zb
    full_blocked |= zb
    r = trace_mm / 2.0 + clearance_mm + FAB_QUANTUM_MM
    # The mesh's pad rings already stand ``ctx.grow_mm`` off the copper, so
    # an edge within that of a pad lies inside its ring and the ring walls
    # (above) closed it: the edge-level gap rule would repeat them. A net
    # with a COARSER rule than the rings were grown for (a fat power
    # track, ``r`` > ``grow_mm``) still needs it — its centreline must
    # stand off further than any ring does — so it keeps the closure at
    # its own radius, measured on the edge where no ring reaches.
    if r > ctx.grow_mm + 1e-9:
        blocked_gap, _free, edge_pads, pad_edges = _pad_eval(ctx, r)
    else:
        blocked_gap = np.zeros(len(ctx.board), dtype=np.int64)
        edge_pads, pad_edges = {}, {}
    if edge_pads:
        gap_mask = blocked_gap.copy()
        # own-net pads stay out of the coverage — the pin's ringed pad
        # AND every extra pad pinned to one of the net's pins (a second
        # same-named land is the same net's copper). An unpinned extra
        # (pad_pin -1) is in no net's own set — foreign to all.
        own_pads = frozenset(i for i, pn in enumerate(ctx.pad_pin) if int(pn) in own)
        if own_pads:
            seen_e: set[int] = set()
            for pi_ in own_pads:
                for e in pad_edges.get(pi_, ()):
                    if e in seen_e:
                        continue
                    seen_e.add(e)
                    free = _edge_free(ctx, e, r, own_pads, edge_pads)
                    m = 0
                    for ly in range(ctx.n_layers):
                        if free[ly] <= 1e-9:
                            m |= 1 << ly
                    gap_mask[e] = m
        # inside the net's own pad the wire is on its own copper: no gap
        # to measure (a comb finger beside the other comb's is walled on
        # its flanks, but travels its own length)
        for pin in own:
            for e in ctx.pin_core.get(pin, ()):
                gap_mask[e] = 0
        full_blocked |= gap_mask
    open_ = np.int64(cand_mask) & ~full_blocked
    open_[ctx.board] = 0
    for e, reach in ctx.board_reach.items():
        m = 0
        for pin, pm in reach:
            if pin in own:
                m |= pm
        if m:
            open_[e] = np.int64(cand_mask) & m & ~full_blocked[e]
    return open_


def _longest_free(ints: list[tuple[float, float]]) -> float:
    """The longest uncovered stretch of ``[0, 1]`` under ``ints`` — one
    via needs one stretch, not the sum of slivers."""
    ints = sorted(ints)
    best = 0.0
    cur = 0.0
    for a, b in ints:
        best = max(best, a - cur)
        cur = max(cur, b)
    return max(best, 1.0 - cur)


def _via_fit_base(ctx: _Ctx, d: TopoDesign, via_dia: float, clearance: float):
    """Edges where a through via of ``via_dia`` fits, before any zone
    bar: the pads grown by ``via_dia/2 + max(clearance, d.via_mask_web_mm)``
    (the cover :func:`_pad_eval` already holds for that radius) leave a
    free stretch of the edge on ALL layers at once — the barrel is plated
    through, so a pad on any layer takes the point — and the board
    outline's margin (``via_dia/2`` plus the larger of the clearance and
    the fab's edge bar) leaves the stretch inside the board. No pad is
    exempt, the net's own included: the via must keep the soldermask web
    (:attr:`TopoDesign.via_mask_web_mm`, Reto 2026-11-03) from every pad's
    copper, and the planter's index judges every pad, so a via on or too
    near its own pad's copper finds no site. Cached per radius pair."""
    r = via_dia / 2.0 + max(clearance, float(d.via_mask_web_mm)) + FAB_QUANTUM_MM
    margin = (
        via_dia / 2.0
        + max(clearance, float(d.edge_clearance_mm or 0.0))
        + FAB_QUANTUM_MM
    )
    got = ctx.via_fit_cache.get((r, margin))
    if got is not None:
        return got
    _pad_eval(ctx, r)
    fit = ~ctx.board
    for e, ints in ctx.cover_cache[r].items():
        if not fit[e]:
            continue
        if float(ctx.seg_len[e]) * _longest_free([(a, b) for a, b, _q in ints]) < (
            _VIA_FIT_MIN_MM
        ):
            fit[e] = False
    if len(d.outline) >= 3:
        inner = Polygon(np.asarray(d.outline, dtype=float)).buffer(-margin)
        if inner.is_empty:
            fit[:] = False
        else:
            segs = shapely.linestrings(np.stack([ctx.seg_u, ctx.seg_v], axis=1))
            fit &= shapely.intersects(inner, segs)
    ctx.via_fit_cache[(r, margin)] = fit
    return fit


class _ViaPlanter:
    """One build's via-point oracle (G2g): a layer change on edge ``e``
    is legal only where a concrete point of ``e`` clears every body a
    via barrel must clear. The candidates come from the edge's own free
    stretch — the pad-cover complement at ``r_site + pad_gap``,
    sampled midpoint-out per sub-interval (stage A) — judged against two
    static indexes (stage B): the pads, net-tagged so a FOREIGN pad
    keeps the full ``r_site + pad_gap`` ring while the net's OWN pad
    bars ``max(the soldermask web, the net's clearance)`` —
    ``d.via_mask_web_mm``, Reto 2026-11-03: a via keeps
    ``expansion + dam + expansion`` of mask web off EVERY
    pad, its own net's included — the own-pad copper-overlap exemption
    is gone, vias are not tented; ``pad_gap`` is the larger of the
    clearance and the web, so stage A has already ended the free
    stretch at that ring of every pad, own ones too — and the via-bar
    zones, net-tagged fixed
    copper and the outline's fab margin; then the dynamic index of
    earlier nets' wires and planted vias (stage C). Stage A+B are cached
    per ``(r_site, clearance)`` over the whole build; stage C runs per
    (net, edge)."""

    def __init__(
        self,
        ctx: _Ctx,
        d: TopoDesign,
        pose: np.ndarray | None = None,
        *,
        net_clearance: Sequence[float] | None = None,
        net_clearance_by_name: Mapping[str, float] | None = None,
    ) -> None:
        self.ctx = ctx
        #: The via-to-pad soldermask web every pad holds
        #: (:attr:`TopoDesign.via_mask_web_mm`).
        self.mask_web = float(d.via_mask_web_mm)
        pose_a = np.asarray(d.pose0 if pose is None else pose, dtype=float)
        net_ix = {str(n): i for i, n in enumerate(d.net_name)}
        nc = tuple(float(c) for c in net_clearance) if net_clearance is not None else ()
        named_clearance = {
            str(name): nc[k] for name, k in net_ix.items() if k < len(nc)
        }
        named_clearance.update(net_clearance_by_name or {})
        # The pad index IS the emit guard's pad set — same roster and the
        # same world geometry (:func:`realise._pad_polys`, the shapes the
        # DRC flashes: real rings, circumscribed discs, rotated rects — not
        # the mesh's faceted copper). Each body carries its OWN net's
        # clearance, so a foreign pad bars ``max(caller, cp, web)`` —
        # ``_guarded_branches``' predicate, never a smaller one. The shim
        # feeds ``_pad_polys``' IR name lookup; ``pin_nets`` maps those
        # IDs to the filtered routed-net indices. Lazy import: realise
        # imports wires at module level.
        from precis.pcb.topoplace import realise as _realise

        names = d.ir_net_name or tuple(d.net_name)
        need = int(d.pin_net.max()) + 1 if len(d.pin_net) else 0
        shim = types.SimpleNamespace(
            net_name=names + tuple("" for _ in range(max(0, need - len(names))))
        )
        pad_b: list[Body] = []
        pin_nets = d.pin_nets()
        seen: set[int] = set()
        for rows in _realise._pad_polys(d, shim, ctx.n_layers, pose_a).values():
            for pname, g, pin in rows:
                if id(g) in seen:
                    continue
                seen.add(id(g))
                net = pin_nets[pin][1] if 0 <= pin < len(pin_nets) else ANY_NET
                pad_b.append(
                    Body(
                        g,
                        net=net if net >= 0 else ANY_NET,
                        clearance=float(
                            named_clearance.get(pname, _realise._DEFAULT.clearance_mm)
                        )
                        if pname
                        else 0.0,
                    )
                )
        self.pad_ix = CopperIndex(pad_b)
        # Physical drilling has its own radius and fabrication wall. Net
        # copper clearance and a via's larger copper land do not enlarge it.
        self.hole_ix = CopperIndex(_realise._pad_hole_bodies(d, shim, pose_a))
        self.default_drill_mm = float(_realise._VIA_DRILL_MM)
        env_b: list[Body] = []
        for z in d.zones:
            if z.kind in ("keepout", "mounting_hole", "label_keepout"):
                net = ANY_NET
            elif z.kind in ("fixed_copper", "fixed_via"):
                net = net_ix.get(str(z.net or ""), ANY_NET)
            else:
                continue
            g = Polygon(
                np.asarray(topo_proxy.zone_world_ring(d, z, pose_a), dtype=float)
            )
            if not g.is_empty and g.area > 0.0:
                env_b.append(
                    Body(
                        g,
                        net=net,
                        clearance=(
                            float(
                                named_clearance.get(
                                    str(z.net), _realise._DEFAULT.clearance_mm
                                )
                            )
                            if z.net and z.kind in ("fixed_copper", "fixed_via")
                            else 0.0
                        ),
                    )
                )
        # Keep the authoritative world-space zones for bounded direct
        # site proposals. The outline exterior below is a clearance
        # obstacle, not a via-barred region containing the source pad.
        self._env_zones = tuple(env_b)
        if len(d.outline) >= 3:
            env_b.append(
                Body(
                    Polygon(np.asarray(d.outline, dtype=float)).exterior,
                    clearance=float(d.edge_clearance_mm or 0.0),
                )
            )
        self.env_ix = CopperIndex(env_b)
        self.cache: dict[
            tuple[float, float], dict[int, tuple[np.ndarray, np.ndarray]]
        ] = {}
        # (net, edge, r_site, clearance) -> statically-surviving points
        # for that net — the re-judge runs once per pair, attempts share it
        self._net_free: dict[tuple[int, int, float, float], np.ndarray] = {}
        # (id(dyn), dyn._version, net, edge, r_site, clearance, lo, hi, drill)
        # -> plantable points — the dyn index is per view, shared by that
        # net's retry attempts; the version key is the index's mutation
        # count, so a grow loop adding its own vias mid-attempt re-judges
        self._pts: dict[
            tuple[int, int, int, int, float, float, float, float, float],
            np.ndarray,
        ] = {}
        self._dyn_refs: dict[int, CopperIndex] = {}  # pins ids against reuse
        # (r_site, clearance) -> (edges a pad could block, edges an env
        # body could block): the tree's reach bound, so an edge outside
        # both skips the per-point index queries entirely
        self._near: dict[tuple[float, float], tuple[set[int], set[int]]] = {}

    def _static_blocked(
        self,
        xy: np.ndarray,
        r_site: float,
        clearance: float,
        net: int,
        *,
        check_pads: bool = True,
        check_env: bool = True,
    ) -> np.ndarray:
        """Shared static predicates; portal callers retain near-edge skips."""
        blocked = np.zeros(len(xy), dtype=bool)
        if check_pads and len(xy):
            blocked |= self.pad_ix.blocked_points(
                xy,
                r_site,
                max(clearance, self.mask_web),
                net=net,
                own_clearance=self.mask_web if net != NO_OWN_NET else None,
            )
        if check_env and len(xy):
            blocked |= self.env_ix.blocked_points(xy, r_site, clearance, net=net)
        return blocked

    def _drill_blocked(self, xy: np.ndarray, drill_mm: float) -> np.ndarray:
        if self.hole_ix._tree is None or not len(xy):
            return np.zeros(len(xy), dtype=bool)
        drill_mm = float(drill_mm or self.default_drill_mm)
        return self.hole_ix.blocked_points(xy, drill_mm / 2.0, 0.0, net=NO_OWN_NET)

    @staticmethod
    def _dynamic_blocked(
        xy: np.ndarray,
        r_site: float,
        clearance: float,
        net: int,
        dyn: CopperIndex | None,
    ) -> np.ndarray:
        if dyn is None or not len(xy):
            return np.zeros(len(xy), dtype=bool)
        return dyn.blocked_points(xy, r_site, clearance, net=net)

    def plantable_points(
        self,
        xy: np.ndarray,
        r_site: float,
        clearance: float,
        net: int,
        dyn: CopperIndex | None = None,
        *,
        drill_mm: float = 0.0,
    ) -> np.ndarray:
        """Certify arbitrary centers with the portal planter's exact rules.

        These uncached verdicts share own-pad web, named static copper,
        physical drilling and dynamic-copper predicates with edge sites.
        Whole-stub, pour-join and outline containment remain the emitter's.
        """
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        blocked = self._static_blocked(xy, r_site, clearance, net)
        blocked |= self._drill_blocked(xy, drill_mm)
        blocked |= self._dynamic_blocked(xy, r_site, clearance, net, dyn)
        return ~blocked

    def boundary_points(
        self, start: tuple[float, float], r_site: float, clearance: float, net: int
    ) -> list[tuple[float, float]]:
        """At most 64 nearest outward zone-boundary proposals, not verdicts.

        Only a net-blocking zone covering the start center proposes a
        point. Circumscribe the buffer's chordal rounding and add a second
        fabrication quantum to avoid tangent-roundoff rejection. Exact
        shared predicates still check every overlapping body afterwards.
        """
        source = Point(start)
        proposals: set[tuple[float, float]] = set()
        for body in self._env_zones:
            if body.net == net or not body.geom.covers(source):
                continue
            reach = (
                r_site + body.r + max(clearance, body.clearance) + 2.0 * FAB_QUANTUM_MM
            )
            boundary = body.geom.buffer(
                reach / math.cos(math.pi / 64.0), quad_segs=16
            ).boundary
            if boundary.is_empty:
                continue
            point = nearest_points(source, boundary)[1]
            proposals.add((float(point.x), float(point.y)))
        return sorted(
            proposals,
            key=lambda p: ((p[0] - start[0]) ** 2 + (p[1] - start[1]) ** 2, p),
        )[:64]

    def _near_edges(self, r_site: float, clearance: float) -> tuple[set[int], set[int]]:
        key = (r_site, clearance)
        got = self._near.get(key)
        if got is not None:
            return got
        ctx = self.ctx
        pad_reach = r_site + max(clearance, self.mask_web) + FAB_QUANTUM_MM
        env_reach = r_site + clearance + FAB_QUANTUM_MM
        segs = shapely.linestrings(np.stack([ctx.seg_u, ctx.seg_v], axis=1))
        pad_near: set[int] = set()
        if self.pad_ix._tree is not None:
            pad_near = {
                int(i)
                for i in self.pad_ix._tree.query(
                    segs,
                    predicate="dwithin",
                    distance=pad_reach + self.pad_ix._reach,
                )[0]
            }
        env_near: set[int] = set()
        if self.env_ix._tree is not None:
            env_near = {
                int(i)
                for i in self.env_ix._tree.query(
                    segs,
                    predicate="dwithin",
                    distance=env_reach + self.env_ix._reach,
                )[0]
            }
        got = (pad_near, env_near)
        self._near[key] = got
        return got

    def candidates(
        self, e: int, r_site: float, clearance: float
    ) -> tuple[np.ndarray, np.ndarray]:
        """Stage A+B, cached per (edge, via rule): the sampled candidate
        points and their static verdict with every pad foreign (the
        design's stage B; per-net own-pad re-judging happens in
        :meth:`point`)."""
        by_edge = self.cache.setdefault((r_site, clearance), {})
        got = by_edge.get(e)
        if got is not None:
            return got
        ctx = self.ctx
        pad_gap = max(clearance, self.mask_web)
        r = r_site + pad_gap + FAB_QUANTUM_MM
        _pad_eval(ctx, r)
        seglen = float(ctx.seg_len[e])
        ints = sorted((a, b) for a, b, _q in ctx.cover_cache[r].get(e, ()))
        free: list[tuple[float, float]] = []
        cur = 0.0
        for a, b in ints:
            if a > cur:
                free.append((cur, a))
            cur = max(cur, b)
        if cur < 1.0:
            free.append((cur, 1.0))
        # the disc may not contain a triangulation vertex: a via at an
        # edge endpoint has no well-defined corridor sides, and a centre
        # within r_site of a vertex collides with vid_of keying
        inset = r_site / seglen if seglen > _EPS else 1.0
        free = [
            (max(a, inset), min(b, 1.0 - inset))
            for a, b in free
            if max(a, inset) < min(b, 1.0 - inset)
        ]
        keep = sorted(
            ((a, b) for a, b in free if (b - a) * seglen >= _VIA_FIT_MIN_MM),
            key=lambda ab: ab[0] - ab[1],
        )
        s = max(r_site, 0.05) / seglen if seglen > _EPS else 1.0
        ts: list[float] = []
        for a, b in keep:
            mid = 0.5 * (a + b)
            pts = {mid}
            n = int(max(b - mid, mid - a) / s) + 1
            for i in range(1, n + 1):
                for t in (mid - i * s, mid + i * s):
                    if a - 1e-12 <= t <= b + 1e-12:
                        pts.add(min(1.0, max(0.0, t)))
            ts.extend(sorted(pts, key=lambda t: (abs(t - mid), t)))
        xy = ctx.seg_u[e] + np.asarray(ts[:64])[:, None] * (ctx.seg_v[e] - ctx.seg_u[e])
        if not len(xy):
            xy = np.zeros((0, 2), dtype=np.float64)
        pad_near, env_near = self._near_edges(r_site, clearance)
        blocked = self._static_blocked(
            xy,
            r_site,
            clearance,
            NO_OWN_NET,
            check_pads=e in pad_near,
            check_env=e in env_near,
        )
        out = (xy, blocked)
        by_edge[e] = out
        return out

    def static_survivors(
        self, e: int, net: int, r_site: float, clearance: float
    ) -> np.ndarray:
        """The edge's via candidates that clear every static body FOR
        THIS NET — foreign pads at ``max(clearance, the pad's own net
        clearance, mask_web)`` (the emit guard's bar exactly), own pads
        at ``max(mask_web, the net's own clearance)`` — one corner
        stricter than the guard's ``web``-only own bar when a net's
        clearance exceeds the web — same-net fixed copper exempt.
        Cached per (net, edge): the build's attempts share it."""
        key = (net, int(e), r_site, clearance)
        free = self._net_free.get(key)
        if free is None:
            xy, blocked = self.candidates(e, r_site, clearance)
            if not len(xy):
                free = xy
            else:
                free_mask = ~blocked
                rej = np.flatnonzero(blocked)
                if len(rej):
                    # the cached verdict priced every pad foreign —
                    # re-judge the rejects at this net: foreign pads at
                    # the full ring, its own pads at ``max(the mask web,
                    # the net's own clearance)`` — the same ``pad_gap``
                    # ring stage A priced them at — same-net fixed
                    # copper exempt
                    pad_near, env_near = self._near_edges(r_site, clearance)
                    sub = xy[rej]
                    revive = ~self._static_blocked(
                        sub,
                        r_site,
                        clearance,
                        net,
                        check_pads=e in pad_near,
                        check_env=e in env_near,
                    )
                    free_mask[rej[revive]] = True
                free = xy[free_mask]
            self._net_free[key] = free
        return free

    def point(
        self,
        e: int,
        r_site: float,
        clearance: float,
        net: int,
        dyn: CopperIndex | None = None,
        lo: float = 0.0,
        hi: float = 1.0,
        *,
        drill_mm: float = 0.0,
    ) -> tuple[float, float, float] | None:
        """The first plantable via point on edge ``e`` for net ``net``,
        ``(x, y, t)`` — or ``None``. Own pads bar ``max(the soldermask
        web, the net's clearance)``: stage A already ended the free
        stretch ``max(clearance, mask_web)`` off every pad, and the
        re-judge's ``own_clearance=self.mask_web`` meets an own pad
        tagged with the net's own clearance — both reduce to
        ``max(cv, web)`` (the emit guard's own bar is the web alone;
        the ``cv > web`` corner is stricter here, never a
        disagreement). Same-net fixed copper never blocks; the
        dynamic index holds earlier wires and planted vias. ``lo``/``hi``
        (G2h) bound the edge parameter the point may take — the sub-
        interval the arriving run ends in."""
        return next(
            self.points(e, r_site, clearance, net, dyn, lo, hi, drill_mm=drill_mm),
            None,
        )

    def points(
        self,
        e: int,
        r_site: float,
        clearance: float,
        net: int,
        dyn: CopperIndex | None = None,
        lo: float = 0.0,
        hi: float = 1.0,
        *,
        drill_mm: float = 0.0,
    ) -> Iterator[tuple[float, float, float]]:
        """Plantable points in :meth:`point`'s existing candidate order.

        Cache the same static, drill, interval and dynamic verdict for
        this index state. Coordinate tuples are produced only as consumed,
        so a caller accepting the first point never walks the remainder.
        """
        ctx = self.ctx
        if ctx.board[e]:
            return
        # Zero is the stored via convention for the emitted default drill.
        drill_mm = float(drill_mm or self.default_drill_mm)
        # one verdict per (dynamic index STATE, net, edge): a net's
        # attempts all read the same dyn object, so retries are dict
        # hits — but _grow_net adds this net's own vias to its index
        # between branches, so the key must carry the mutation version,
        # not just the object
        if dyn is not None:
            self._dyn_refs[id(dyn)] = dyn
        key = (
            id(dyn),
            dyn._version if dyn is not None else 0,
            net,
            int(e),
            r_site,
            clearance,
            lo,
            hi,
            drill_mm,
        )
        if key in self._pts:
            free = self._pts[key]
        else:
            free = self.static_survivors(e, net, r_site, clearance)
            if len(free) and self.hole_ix._tree is not None:
                free = free[~self._drill_blocked(free, drill_mm)]
            if (lo > 0.0 or hi < 1.0) and len(free):
                u = ctx.seg_u[e]
                dv = ctx.seg_v[e] - u
                dd = float((dv * dv).sum())
                tp = (
                    ((free - u) * dv).sum(axis=1) / dd
                    if dd > _EPS
                    else np.zeros(len(free))
                )
                free = free[(tp >= lo - 1e-9) & (tp <= hi + 1e-9)]
            if dyn is not None and len(free):
                free = free[~self._dynamic_blocked(free, r_site, clearance, net, dyn)]
            self._pts[key] = free
        for xy in free:
            x, y = float(xy[0]), float(xy[1])
            yield x, y, _edge_param_at(ctx, e, (x, y))


def _capsule_shadow(
    p: _V2, qa: _V2, qb: _V2, a: _V2, b: _V2, radius: float
) -> list[tuple[float, float]]:
    """Target parameters hidden from ``p`` by an exact swept segment.

    Visibility changes only at a tangent from ``p`` to either end disk,
    or where the target crosses an end disk or offset side. Partition
    at those analytic roots and classify each open piece by its exact
    segment distance; no polygonal approximation can close a legal gap.
    """
    radius = max(0.0, radius - 1e-9)
    if radius <= 0.0:
        return []
    core = LineString([a, b])
    if Point(p).distance(core) < radius:
        return [(0.0, 1.0)]
    vx, vy = qa[0] - p[0], qa[1] - p[1]
    dx, dy = qb[0] - qa[0], qb[1] - qa[1]
    cuts = [0.0, 1.0]

    def roots(aa: float, bb: float, cc: float) -> None:
        if abs(aa) < 1e-14:
            if abs(bb) > 1e-14:
                cuts.append(-cc / bb)
            return
        disc = bb * bb - 4.0 * aa * cc
        if disc >= 0.0:
            rr = math.sqrt(disc)
            cuts.extend(((-bb - rr) / (2.0 * aa), (-bb + rr) / (2.0 * aa)))

    for cx, cy in (a, b):
        sx, sy = cx - p[0], cy - p[1]
        cross0, cross1 = vx * sy - vy * sx, dx * sy - dy * sx
        roots(
            cross1 * cross1 - radius * radius * (dx * dx + dy * dy),
            2.0 * (cross0 * cross1 - radius * radius * (vx * dx + vy * dy)),
            cross0 * cross0 - radius * radius * (vx * vx + vy * vy),
        )
        ex, ey = qa[0] - cx, qa[1] - cy
        roots(
            dx * dx + dy * dy,
            2.0 * (ex * dx + ey * dy),
            ex * ex + ey * ey - radius * radius,
        )
    ax, ay = b[0] - a[0], b[1] - a[1]
    length = math.hypot(ax, ay)
    side0 = ax * (qa[1] - a[1]) - ay * (qa[0] - a[0])
    side1 = ax * dy - ay * dx
    if abs(side1) > 1e-14:
        cuts.extend(
            ((radius * length - side0) / side1, (-radius * length - side0) / side1)
        )
    cuts = sorted({max(0.0, min(1.0, t)) for t in cuts if -1e-12 <= t <= 1.0 + 1e-12})
    blocked: list[tuple[float, float]] = []
    for lo, hi in itertools.pairwise(cuts):
        mid = (lo + hi) / 2.0
        q = (qa[0] + mid * dx, qa[1] + mid * dy)
        if LineString([p, q]).distance(core) < radius:
            if blocked and lo <= blocked[-1][1] + 1e-12:
                blocked[-1] = (blocked[-1][0], hi)
            else:
                blocked.append((lo, hi))
    return blocked


def _escape_interval_open(
    obstacles: Sequence[tuple[_V2, _V2, float]],
    p_seg: tuple[_V2, _V2],
    q_seg: tuple[_V2, _V2],
) -> bool:
    """Subtract capsule umbrae only for a failed portal pair.

    A fixed source gives exact shadow subtraction. For two intervals,
    each convex obstacle's endpoint-shadow intersection is hidden from
    the entire source interval. Empty remaining intervals prove refusal;
    surviving intervals may over-admit a multi-obstacle split, so the
    reconstructed polyline still needs the emitter's complete guard.
    """
    p_seg = (
        (float(p_seg[0][0]), float(p_seg[0][1])),
        (float(p_seg[1][0]), float(p_seg[1][1])),
    )
    q_seg = (
        (float(q_seg[0][0]), float(q_seg[0][1])),
        (float(q_seg[1][0]), float(q_seg[1][1])),
    )

    def remaining(source: tuple[_V2, _V2], target: tuple[_V2, _V2]) -> bool:
        free = [(0.0, 1.0)]
        for a, b, rad in obstacles:
            sa = _capsule_shadow(source[0], *target, a, b, rad)
            sb = (
                sa
                if source[0] == source[1]
                else _capsule_shadow(source[1], *target, a, b, rad)
            )
            for aa, ab in sa:
                for ba, bb in sb:
                    lo, hi = max(aa, ba), min(ab, bb)
                    if hi <= lo:
                        continue
                    free = [
                        piece
                        for fa, fb in free
                        for piece in ((fa, min(fb, lo)), (max(fa, hi), fb))
                        if piece[1] - piece[0] > 1e-12
                    ]
                    if not free:
                        return False
        return True

    return remaining(p_seg, q_seg) and (p_seg[0] == p_seg[1] or remaining(q_seg, p_seg))


def fanout_escape(
    ctx: _Ctx,
    mesh: TopoMesh,
    pin: int,
    lay: int,
    k: int,
    open_: np.ndarray,
    trace_mm: float,
    clearance_mm: float,
    via_dia_mm: float,
    planter: _ViaPlanter,
    dyn: CopperIndex | None = None,
    wire_cover: Mapping[int, Sequence[tuple[float, float]]] | None = None,
    step_ok: Callable[[int, _V2, _V2], bool] | None = None,
    verify: (
        Callable[
            [list[tuple[float, float]], tuple[float, float, float]],
            bool,
        ]
        | None
    ) = None,
    reuse_pts: Sequence[tuple[float, float, int, int]] | None = None,
    radius_mm: Callable[[], float] | None = None,
    iv_ok: Callable[[int, tuple[_V2, _V2], tuple[_V2, _V2]], bool] | None = None,
    via_drill_mm: float = 0.0,
    *,
    continue_sites: bool = True,
) -> tuple[list[tuple[float, float]], tuple[float, float, float], bool] | None:
    """A plane-net pad no ring site serves escapes by wire (G2d): a
    two-pin routed branch on the pad's own layer from the pad to the
    nearest point the planter admits OUTSIDE the via-barred zones —
    the via then drops to the plane like any fanout's.

    Dijkstra over ``(edge, free interval, triangle)`` — the same
    passability and interval model the layered search runs, one layer
    and one goal: the first edge whose interval holds a via point
    :meth:`_ViaPlanter.point` accepts (the planter's env index is what
    keeps the barrel off every via-barred zone). The intervals are the
    pad-cover complement at the wire's centreline radius
    ``trace_mm/2 + clearance_mm`` — the ``_pad_eval`` cover the corridor
    reads — with the pad's own lands dropped; ``open_`` is the net's
    open-layer bitmask under that same ``own={pin}`` exemption. ``dyn``
    carries the barrels and escapes already sited, so two escapes never
    claim the same room; ``wire_cover`` adds per-edge covered stretches
    (routed wires read as capsules) so a deferred escape keeps off the
    signal route. ``step_ok(tri, p, q)`` vetoes a leg ``p→q`` inside
    ``tri`` — the deferred caller's routed-copper check — and every
    popped goal is reconstructed and offered to ``verify(pts, via)``;
    a rejected site queues the same interval's remaining
    :meth:`_ViaPlanter.points`. They are tried only after the established
    search and its endpoint fallback exhaust, preserving every already
    successful escape's site and path. ``continue_sites=False`` disables
    extra sites so the caller can preserve established successes across
    multiple barrel sizes first. ``reuse_pts`` (G2d round 8) are
    ``(x, y, layer_lo, layer_hi)`` of barrels already on the net — an
    escape may END on one instead of drilling a second barrel on top of
    it, but only when the barrel actually reaches the stub's layer (a
    buried In1–In2 barrel has no F.Cu land, so round 9 filters the
    offer on ``lo <= lay <= hi``; the caller's verify owns the pour
    half of the span rule). A point inside a goal state's far triangle
    is offered as the site after the planter's own answer, and the
    caller emits the stub alone. Returns
    ``(pts, (x, y, t), reused)`` — the taut polyline, the site, whether
    it is an existing barrel — or ``None`` when no reachable interval
    plants or reuses a via — the pad's finding.

    ``radius_mm`` is a conservative start-to-portal distance bound. The
    emitter tightens it only after recording a legal over-cap candidate,
    so the search still reports the needed length without exhausting
    corridors that cannot lead to an under-cap stub.

    ``step_ok`` tests midpoints and quarters first. If that search fails
    with a closed interval, a fallback phase adds inset endpoint witnesses.
    Keeping the established successful corridors first avoids moving a
    legal escape into space a later pad needs. Endpoint witnesses keep
    a disc blocking the middle samples from closing a passable interval.
    Only if all five witnesses fail does ``iv_ok`` subtract the interval's
    obstacle shadows. A surviving transition is a reachability possibility,
    not a certified taut corridor; ``verify`` checks the complete emitted
    polyline. The expensive fallback is local and lazy so successful
    witness paths keep their order and cost.
    """
    r = trace_mm / 2.0 + clearance_mm
    _pad_eval(ctx, r)
    r_site = via_dia_mm / 2.0
    bit = 1 << lay
    own_pads = frozenset(i for i, p in enumerate(ctx.pad_pin) if int(p) == pin)
    iv_memo: dict[int, list[tuple[float, float]]] = {}
    samples: tuple[float, ...] = (0.5, 0.25, 0.75)
    reach_cache: dict[tuple[float, int, float, float], bool] = {}

    def within_radius(e: int, a: float, b: float) -> bool:
        limit = math.inf if radius_mm is None else radius_mm()
        if not math.isfinite(limit):
            return True
        key = (limit, e, a, b)
        if key not in reach_cache:
            u, dv = ctx.seg_u[e], ctx.seg_v[e] - ctx.seg_u[e]
            reach_cache[key] = _pt_seg(vpos, u + a * dv, u + b * dv) <= limit + 1e-9
        return reach_cache[key]

    def free_ivs(e: int) -> list[tuple[float, float]]:
        """``e``'s uncovered stretches on ``lay`` — the portal the wire
        may take — at the centreline radius, own lands excluded, plus
        any ``wire_cover`` stretches (routed copper)."""
        got = iv_memo.get(e)
        if got is None:
            ints = [
                (a, b)
                for a, b, q in ctx.cover_cache[r].get(e, ())
                if int(ctx.pad_copper[q]) >> lay & 1 and q not in own_pads
            ]
            if wire_cover:
                ints += list(wire_cover.get(e, ()))
            ints.sort()
            got = []
            cur = 0.0
            for a, b in ints:
                if a > cur:
                    got.append((cur, a))
                cur = max(cur, b)
            if cur < 1.0:
                got.append((cur, 1.0))
            sl = float(ctx.seg_len[e])
            got = [(a, b) for a, b in got if (b - a) * sl > 1e-6]
            iv_memo[e] = got
        return got

    def branch(
        goal: tuple[int, int, int],
    ) -> tuple[list[int], list[int], list[tuple[float, float]]]:
        """The goal's corridor back to the pad's tri — the points chain
        so ``_taut_pts`` can pull the wire."""
        chain: list[tuple[int, int, int]] = []
        s: tuple[int, int, int] | None = goal
        while s is not None:
            chain.append(s)
            s = prev[s]
        chain.reverse()
        e0 = chain[0][0]
        t1 = chain[0][2]
        ta, tb = ctx.edge_tris[e0]
        tris = [ta if tb == t1 else tb] + [st2[2] for st2 in chain]
        edges = [st2[0] for st2 in chain]
        bounds = [free_ivs(st2[0])[st2[1]] for st2 in chain]
        return tris, edges, bounds

    def leg_ok(t: int, e1: int, iv1: int, e2: int, iv2: int) -> bool:
        """Try interior and endpoint witnesses before closing an interval."""
        if step_ok is None:
            return True
        a1, b1 = free_ivs(e1)[iv1]
        a2, b2 = free_ivs(e2)[iv2]
        for f1 in samples:
            p = edge_xy(mesh, e1, a1 + (b1 - a1) * f1)
            for f2 in samples:
                if step_ok(t, p, edge_xy(mesh, e2, a2 + (b2 - a2) * f2)):
                    return True
        return (
            endpoint_pass
            and iv_ok is not None
            and iv_ok(
                t,
                (edge_xy(mesh, e1, a1), edge_xy(mesh, e1, b1)),
                (edge_xy(mesh, e2, a2), edge_xy(mesh, e2, b2)),
            )
        )

    def seed_ok(t0: int, e0: int, iv: int) -> bool:
        """``leg_ok`` for the pad-to-portal first leg — the pad vertex
        is fixed, only the portal point varies."""
        if step_ok is None:
            return True
        a, b = free_ivs(e0)[iv]
        for f in samples:
            if step_ok(t0, start, edge_xy(mesh, e0, a + (b - a) * f)):
                return True
        return (
            endpoint_pass
            and iv_ok is not None
            and iv_ok(t0, (start, start), (edge_xy(mesh, e0, a), edge_xy(mesh, e0, b)))
        )

    # ``reuse_pts`` indexed by the triangle that holds each — an escape
    # may end ON a barrel of its own net (an authored fixed via, an
    # earlier fanout via) rather than drill a second barrel beside it.
    # The span travels with the point: only a barrel reaching ``lay``
    # can take the stub (G2d round 9).
    reuse_in_tri: dict[int, list[tuple[float, float]]] = {}
    if reuse_pts:
        tri_tree = shapely.STRtree(
            [Polygon(np.asarray(txy, dtype=float)) for txy in ctx.tri_xy]
        )
        for pt in reuse_pts:
            if not (int(pt[2]) <= lay <= int(pt[3])):
                continue
            for ti in tri_tree.query(
                Point(float(pt[0]), float(pt[1])), predicate="intersects"
            ):
                reuse_in_tri.setdefault(int(ti), []).append(
                    (float(pt[0]), float(pt[1]))
                )

    v = int(mesh.pin_vertex[pin])
    vpos = mesh.verts[v]
    start = (float(vpos[0]), float(vpos[1]))
    INF = math.inf
    dist: dict[tuple[int, int, int], float] = {}
    prev: dict[tuple[int, int, int], tuple[int, int, int] | None] = {}
    heap: list[tuple[float, int, int, int]] = []
    # Failed interior-witness transitions are tried only after the
    # established search exhausts. Keep its visited states and planter
    # cache rather than rebuilding and walking the same corridors twice.
    deferred: list[
        tuple[int, int, int, int, int, int, float, tuple[int, int, int] | None]
    ] = []  # tri, incoming edge/interval, outgoing edge/interval/tri, cost, parent
    continuations: dict[
        tuple[int, int, int],
        tuple[
            tuple[float, float, float],
            list[int],
            list[int],
            list[tuple[float, float]],
        ],
    ] = {}
    endpoint_pass = False
    for t0 in ctx.v2t[v]:
        # a reuse barrel inside the pad's own triangle is the shortest
        # possible escape — a straight stub onto it, no new drill
        for px, py in reuse_in_tri.get(int(t0), ()):
            if step_ok is not None and not step_ok(int(t0), start, (px, py)):
                continue
            site = (px, py, 0.0)
            if verify is None or verify([start, (px, py)], site):
                return [start, (px, py)], site, True
        for e0 in ctx.tri_edge_list[t0]:
            if not open_[e0] & bit:
                continue
            t2a, t2b = ctx.edge_tris[e0]
            t2 = t2a if t2a != t0 else t2b
            if t2 < 0:
                continue
            u, w_ = ctx.seg_u[e0], ctx.seg_v[e0]
            for iv, (a, b) in enumerate(free_ivs(e0)):
                if not within_radius(e0, a, b):
                    continue
                cost = _pt_seg(vpos, u + a * (w_ - u), u + b * (w_ - u))
                if not seed_ok(int(t0), e0, iv):
                    deferred.append((int(t0), -1, -1, e0, iv, t2, cost, None))
                    continue
                st = (e0, iv, t2)
                if cost < dist.get(st, INF) - 1e-12:
                    dist[st] = cost
                    prev[st] = None
                    heapq.heappush(heap, (cost, e0, iv, t2))
    while heap or deferred:
        if not heap:
            endpoint_pass = True
            samples = (0.5, 0.25, 0.75, 1e-6, 1.0 - 1e-6)
            pending, deferred = deferred, []
            for t0, e1, iv1, e2, iv2, t2, cost, parent in pending:
                st2 = (e2, iv2, t2)
                if cost >= dist.get(st2, INF) - 1e-12:
                    continue
                a2, b2 = free_ivs(e2)[iv2]
                if not within_radius(e2, a2, b2):
                    continue
                allowed = (
                    seed_ok(t0, e2, iv2) if e1 < 0 else leg_ok(t0, e1, iv1, e2, iv2)
                )
                if allowed:
                    dist[st2] = cost
                    prev[st2] = parent
                    heapq.heappush(heap, (cost, e2, iv2, t2))
            if not heap:
                break
        g, e, iv, t = heapq.heappop(heap)
        st = (e, iv, t)
        if g > dist.get(st, INF) + 1e-9:
            continue
        a, b = free_ivs(e)[iv]
        if not within_radius(e, a, b):
            continue
        via = planter.point(
            e, r_site, clearance_mm, k, dyn, a, b, drill_mm=via_drill_mm
        )
        if via is not None:
            tris, edges, bounds = branch(st)
            pts = _taut_pts(
                ctx,
                mesh,
                tris,
                edges,
                start,
                (float(via[0]), float(via[1])),
                bounds,
            )
            if verify is None or verify(pts, via):
                return pts, via, False
            # Keep the actual corridor: endpoint fallback can later
            # improve prev, so reconstructing st at the end could change
            # the branch whose first candidate was just checked.
            if continue_sites:
                continuations[st] = (via, tris, edges, bounds)
        # a barrel of the pad's own net already inside the goal's far
        # triangle serves as the site — the stub ends on it, no new
        # barrel is drilled (G2d round 8 ruling)
        for px, py in reuse_in_tri.get(t, ()):
            tp = min(1.0, max(0.0, _edge_param_at(ctx, e, (px, py))))
            tp = min(b, max(a, tp))
            portal = edge_xy(mesh, e, tp)
            if step_ok is not None and not step_ok(t, portal, (px, py)):
                continue
            site = (px, py, _edge_param_at(ctx, e, (px, py)))
            tris, edges, bounds = branch(st)
            pts = _taut_pts(ctx, mesh, tris, edges, start, (px, py), bounds)
            if verify is None or verify(pts, site):
                return pts, site, True
        for e2, t2, wstep in ctx.tri_next[t].get(e, ()):
            if t2 < 0 or not open_[e2] & bit:
                continue
            nd = g + wstep
            for iv2, (a2, b2) in enumerate(free_ivs(e2)):
                st2 = (e2, iv2, t2)
                # A transition that cannot improve this state cannot
                # alter its corridor; skip geometry and endpoint work.
                if nd >= dist.get(st2, INF) - 1e-12:
                    continue
                if not within_radius(e2, a2, b2):
                    continue
                if not leg_ok(t, e, iv, e2, iv2):
                    if not endpoint_pass:
                        deferred.append((t, e, iv, e2, iv2, t2, nd, st))
                    continue
                dist[st2] = nd
                prev[st2] = st
                heapq.heappush(heap, (nd, e2, iv2, t2))
    # Preserve every established successful escape before allocating
    # different room to this pad. Only an exhausted single-site search
    # reaches the other certified barrel sites on its visited portals.
    for (e, iv, _t), (via, tris, edges, bounds) in continuations.items():
        a, b = free_ivs(e)[iv]
        if not within_radius(e, a, b):
            continue
        for alternative in planter.points(
            e, r_site, clearance_mm, k, dyn, a, b, drill_mm=via_drill_mm
        ):
            if alternative == via:
                continue
            pts = _taut_pts(ctx, mesh, tris, edges, start, alternative[:2], bounds)
            if verify is None or verify(pts, alternative):
                return pts, alternative, False
    return None


@dataclass(frozen=True, slots=True)
class _NetMasks:
    """One net's per-edge routing masks: ``open[e]`` the layer bitmask
    the wire may take across ``e`` (:func:`_open_layers`), ``fit[e]``
    whether a through via of the net's rule fits on ``e`` (a layer
    change there is legal), ``base_pass[e]`` any layer open."""

    open: np.ndarray
    fit: np.ndarray
    base_pass: np.ndarray


def _net_masks(
    ctx: _Ctx,
    d: TopoDesign,
    k: int,
    n_layers: int,
    trace_mm: float,
    clearance_mm: float,
    via_dia_mm: float,
    extra_blocked: np.ndarray | None = None,
) -> _NetMasks:
    """The net's open-layer masks and its via-fit mask. An edge is open
    on a candidate layer unless a pin of ANOTHER net, a foreign-net
    zone, a too-narrow foreign pad-to-pad gap or the board edge blocks
    it there (R-a + round-2 ruling: exemption keys on the wire's net; a
    foreign-net pad on an endpoint part stays a wall. Reto 2026-10-25:
    each pad is buffered by ``trace/2 + clearance`` and an edge
    survives a layer only where a free stretch remains, so a channel
    closes exactly when ``gap < trace + 2·clearance``, per LAYER, the
    net's own pads dropped from the coverage). A via also needs the
    edge clear of every zone that bars a layer for this net — the
    barrel spans the stack. ``extra_blocked`` closes edges on top (the
    crossing-resolution re-thread)."""
    net_name = d.net_name[k] if k < len(d.net_name) else str(k)
    pins = frozenset(int(p) for p in d.net_pins[k])
    cand_mask = _cand_mask(_net_layers(d, k, n_layers), n_layers)
    open_ = _open_layers(ctx, d, cand_mask, net_name, pins, trace_mm, clearance_mm)
    fit = _via_fit_base(ctx, d, via_dia_mm, clearance_mm) & (
        ctx.zone_block[net_name] == 0
    )
    if extra_blocked is not None:
        gone = np.asarray(extra_blocked, dtype=bool)
        open_ = np.where(gone, 0, open_)
        fit = fit & ~gone
    return _NetMasks(open=open_, fit=fit, base_pass=open_ != 0)


def _pin_layers(d: TopoDesign, pin: int, n_layers: int, cand_mask: int) -> int:
    """The candidate layers a wire may leave or reach ``pin`` on without
    a via: an SMD pad's own face, every layer for a drilled pad. A pad
    face outside the net's candidates binds nothing here (the terminal
    stub the plan mints is :func:`realise._fix_terminals`' business)."""
    return _pin_mask(int(d.pin_side[pin]), n_layers) & cand_mask or cand_mask


def layer_dp(
    open_e: Sequence[int],
    fit_e: Sequence[bool],
    start: int,
    end: int,
    n_layers: int,
    pen: Sequence[Sequence[float]] | None = None,
    prefer: Sequence[int] | None = None,
) -> tuple[list[int], list[int], tuple[int, int] | None]:
    """Fewest-via layer assignment of a fixed corridor. Per edge the
    state is a layer open on it; a change on edge ``i`` is legal iff
    both layers are open there AND a concrete via point stands on it
    (G2g — ``fit_e[i]`` is the plantable mask; there is no sliding:
    the via the move mints IS the via the emit keeps). The first edge
    is entered on a layer of ``start`` (a bitmask
    — the near pad's face), the last left on one of ``end``. Cost is
    ``(pen, deviation from prefer, vias, edges on inner layers)``, so
    ties prefer outers, and a
    tie between staying and changing stays. ``pen[i][ly]`` (G2e) is what
    running edge ``i`` on layer ``ly`` costs ahead of any via — an earlier
    net's occupancy there, or a preference being kept; ``None`` is all
    zero. ``prefer[i]`` (G2g) is the layer the search gave edge ``i``:
    after ``pen``, a layering is charged per edge it leaves it — the
    planted vias stay unless a crossing pays for the move. Returns
    ``(runs, cuts, None)`` —
    cut ``c`` is a via on edge ``c``; run ``p`` covers
    ``(cuts[p-1] .. cuts[p]]``, so a cut at the last edge mints the empty
    tail run on the end layer — or ``([], [], (x, y))`` naming the
    edges: ``y`` the first no layer reaches (or the last edge when the
    end pad's face is unreachable), ``x`` the last edge before it where
    a change was still possible."""
    n = len(open_e)
    layers = range(n_layers)
    outer = (0, n_layers - 1)
    inf = (math.inf, math.inf, math.inf, math.inf)

    def inner(ly: int) -> int:
        return 0 if ly in outer else 1

    def at(i: int, ly: int) -> tuple[float, float]:
        p = pen[i][ly] if pen is not None else 0.0
        m = 0.0 if prefer is None or int(prefer[i]) == ly else 1.0
        return p, m

    arrive = [
        (*at(0, ly), 0, inner(ly)) if (open_e[0] & start) >> ly & 1 else inf
        for ly in layers
    ]
    src: list[list[int]] = []  # src[i][m] = arrival layer behind departing m
    last_change = 0
    for i in range(n):
        if all(c == inf for c in arrive):
            return [], [], (last_change, i)
        om = int(open_e[i])
        dep = list(inf for _ in layers)
        how = [-1] * n_layers
        for m in layers:
            if arrive[m] != inf:
                dep[m], how[m] = arrive[m], m
        for m in layers:
            if not om >> m & 1:
                continue
            for ly in layers:
                if ly == m or arrive[ly] == inf:
                    continue
                both = (1 << ly) | (1 << m)
                if not (om & both == both and fit_e[i]):
                    continue
                last_change = i
                cost = (
                    arrive[ly][0],
                    arrive[ly][1],
                    arrive[ly][2] + 1,
                    arrive[ly][3],
                )
                if cost < dep[m]:
                    dep[m], how[m] = cost, ly
        src.append(how)
        if i == n - 1:
            ends = [m for m in layers if end >> m & 1 and dep[m] != inf]
            if not ends:
                return [], [], (last_change, i)
            m_end = min(ends, key=lambda m: (dep[m], m))
            break
        arrive = [
            (
                dep[m][0] + at(i + 1, m)[0],
                dep[m][1] + at(i + 1, m)[1],
                dep[m][2],
                dep[m][3] + inner(m),
            )
            if dep[m] != inf and open_e[i + 1] >> m & 1
            else inf
            for m in layers
        ]
    # walk back: departure layer d_i, arrival layer a_i = src[i][d_i]
    cuts: list[int] = []
    cut_layer: list[int] = []
    d_i = m_end
    for i in range(n - 1, -1, -1):
        a_i = src[i][d_i]
        if a_i != d_i:
            cuts.append(i)
            cut_layer.append(a_i)
        d_i = a_i
    cuts.reverse()
    cut_layer.reverse()
    return cut_layer + [m_end], cuts, None


@dataclass(slots=True)
class _NegCost:
    """One negotiated pass's pricing state (td569540): ``soft`` makes
    capacity closures and chord crossings priced resources instead of
    walls (the final pass is hard); ``p`` the ramped present factor;
    ``edge_hist``/``tri_hist``/``chord_hist`` the accumulated
    ``(E, L)``/``(T, L)`` history tables every net's view reads."""

    soft: bool
    p: float
    edge_hist: np.ndarray
    tri_hist: np.ndarray
    chord_hist: np.ndarray


@dataclass(slots=True)
class _OccView:
    """What the earlier nets left, as one net's search sees it (G2e):
    ``closed[e]`` the layers closed on ``e`` for this net (slots used up,
    a wall round's wires, or a planted via's barrel reach), ``occupied[e,
    l]`` an earlier wire on ``e`` on layer ``l`` (priced, not closed),
    ``tri_used[t, l]`` how many earlier corridors hold tri ``t`` on layer
    ``l`` (the crossing signal — priced, not closed), ``dyn`` the
    dynamic index of those wires plus every planted via — stage C of
    the plant test (G2g). The ``neg_*`` fields carry the negotiated
    pass's tables (:class:`_NegCost`); all ``None``/``False`` is the
    single-pass behaviour."""

    closed: np.ndarray
    occupied: np.ndarray
    dyn: CopperIndex | None = None
    tri_used: np.ndarray | None = None
    #: (tri, layer) -> earlier wires' chord segments (G2h); ``None`` is
    #: the unsplit legacy view (search built by hand in tests).
    chords: dict[tuple[int, int], list[tuple[float, float, float, float]]] | None = None
    #: (edge, layer) -> earlier wires' crossing parameters, sorted.
    splits: dict[tuple[int, int], list[float]] | None = None
    #: negotiated pass: crossings/capacity priced, not forbidden.
    neg_soft: bool = False
    neg_p: float = 0.0
    #: (E, L) demand beyond capacity this net would add, per (edge,
    #: layer) — filled by ``view`` only on soft passes.
    neg_edge_over: np.ndarray | None = None
    neg_edge_hist: np.ndarray | None = None
    neg_tri_hist: np.ndarray | None = None
    neg_chord_hist: np.ndarray | None = None
    #: (T, L) same-layer crossings the committed wires still carry.
    neg_tri_cross: np.ndarray | None = None


def _net_tree(
    ctx: _Ctx,
    mesh: TopoMesh,
    d: TopoDesign,
    k: int,
    n_layers: int,
    trace_mm: float,
    clearance_mm: float,
    occ: np.ndarray,
    extra_blocked: np.ndarray | None = None,
    via_dia_mm: float = _DEFAULT_VIA_MM,
    occ_view: _OccView | None = None,
    planter: _ViaPlanter | None = None,
    via_drill_mm: float = 0.0,
) -> tuple[
    list[
        tuple[
            int,
            int,
            list[int],
            list[int],
            tuple[int, ...],
            list[tuple[int, float, float, float]],
            list[tuple[float, float]],
        ]
    ],
    list[list[int]],
    _NetMasks,
]:
    """Geodesic Prim over one net's pins (R-b): per COMPONENT a
    persistent multi-source A* search — the heap carries relax states
    keyed on ``g + h`` (h = the midpoint distance to the nearest pin;
    consistent, so the first goal popped is optimal) plus lazy goal
    entries carrying exact totals, and a fresh attach re-seeds at cost
    0 so the heap key means distance-to-tree (Prim), not
    distance-to-root. A pin no path reaches under the net's passability
    stays unplaced; the search then roots a fresh component at the
    lowest unplaced pin — a caged root does not drop the net
    (round-2 finding 4). Under net-level exemption passability is a
    uniform per-net mask, so there is no goal-dependent rescue: a
    lost pin's flood region is exactly what it can reach.

    The state is (edge, triangle, layer) (G2c). Crossing an edge keeps
    the layer and needs it open there; a layer change at the current
    edge costs :data:`VIA_COST_MM` and is legal only where the planter
    sites a concrete via point on it (G2g — ``_NetMasks.fit`` stays the
    cheap prefilter): both layers open plus ``plant(e)``. A pin starts
    on the layers it can leave on (an SMD pad's face, any layer for a
    drilled pad) and is reached on one of them — or by a planted hop on
    the last edge. ``h`` ignores vias, so it stays consistent. A pin
    the layered search cannot reach stays unplaced and roots its own
    component — there is no any-layer fallback (G2g): the named
    ``Unreachable`` carries ``layered`` when an any-layer path does
    exist.

    ``occ_view`` (G2e) is what the earlier nets left on each layer: a
    layer is closed on an edge whose slots are used up there, priced
    where an earlier wire runs; its ``dyn`` index — those wires plus
    every planted via — is stage C of the plant test.

    ``extra_blocked`` is an (E,) bool mask of edges closed on top of
    the net's own passability — the crossing-resolution re-thread masks
    a wrapped pad's shared corridor so the tree re-grows around its
    other side (gr520902). Returns ``(branches, components, masks)`` —
    a branch is ``(pin_a, pin_b, tris, edges, layers, hops)`` with
    ``layers`` the per-triangle layer (``len(edges) + 1``) and ``hops``
    ``(edge index, x, y, t)`` per planted via; a component is the
    member pin indices of one separated region (``build_wires`` reports
    every non-main component once on ``Wires.unreachable``)."""
    pins = [int(p) for p in d.net_pins[k]]
    if len(pins) < 2:
        none = np.zeros(len(ctx.board), bool)
        masks = _NetMasks(open=none.astype(np.int64), fit=none, base_pass=none)
        return [], [[j] for j in range(len(pins))], masks
    masks = _net_masks(
        ctx, d, k, n_layers, trace_mm, clearance_mm, via_dia_mm, extra_blocked
    )
    verts = [int(mesh.pin_vertex[p]) for p in pins]
    vpos = [mesh.verts[v] for v in verts]
    cand_mask = _cand_mask(_net_layers(d, k, n_layers), n_layers)
    pin_lay = [_pin_layers(d, p, n_layers, cand_mask) for p in pins]
    nl = n_layers
    base_pass = masks.base_pass
    open_ = masks.open
    fit = masks.fit
    ov = occ_view
    if ov is not None:
        open_ = np.where(ov.closed != 0, open_ & ~ov.closed, open_)
    if planter is None:
        planter = _ViaPlanter(ctx, d)
    dyn = ov.dyn if ov is not None else None
    r_site = via_dia_mm / 2.0
    # G2h: each edge is split per layer at the earlier wires' crossing
    # points and the state is (edge, interval, layer) — a step across a
    # triangle is legal only when both interval midpoints lie on the
    # same side of every same-layer chord there, so a crossing cannot
    # be made. ``None`` splits/chords = the unsplit legacy view.
    splits_v = ov.splits if ov is not None and ov.splits is not None else {}
    chords_v = ov.chords if ov is not None else None
    # negotiated pass (td569540): ``neg_soft`` prices capacity closures
    # and chord crossings instead of forbidding them; the history
    # tables price persisted oversubscription on every pass.
    neg_soft = bool(ov is not None and ov.neg_soft)
    neg_p = float(ov.neg_p) if ov is not None else 0.0
    neg_any = ov is not None and (
        neg_soft
        or ov.neg_edge_over is not None
        or ov.neg_edge_hist is not None
        or ov.neg_tri_hist is not None
        or ov.neg_chord_hist is not None
        or ov.neg_tri_cross is not None
    )
    ivmax = (
        1 + max((len(v) for v in splits_v.values()), default=0)
        if chords_v is not None
        else 1
    )
    _ivmemo: dict[tuple[int, int], list[tuple[float, float]]] = {}

    def ivs(e: int, ly: int) -> list[tuple[float, float]]:
        got = _ivmemo.get((e, ly))
        if got is None:
            spl = splits_v.get((e, ly))
            got = (
                [(0.0, 1.0)]
                if not spl
                else [
                    iv
                    for iv in itertools.pairwise([0.0, *spl, 1.0])
                    if iv[1] - iv[0] > 1e-9
                ]
            )
            _ivmemo[(e, ly)] = got
        return got

    def interval_at(e: int, ly: int, t: float) -> int:
        spl = splits_v.get((e, ly))
        return bisect.bisect_right(spl, t - 1e-9) if spl else 0

    # The occupancy intervals are immutable for this tree search (ivs
    # already memoizes them). Drain revisits their midpoints millions
    # of times; keep the coordinates only for this search's lifetime.
    @functools.cache
    def mid(e: int, ly: int, iv: int) -> tuple[float, float]:
        lo, hi = ivs(e, ly)[iv]
        return edge_xy(mesh, e, (lo + hi) * 0.5)

    def step_ok(tri: int, ly: int, p: _V2, q: _V2) -> bool:
        if neg_soft:
            return True
        return chords_v is None or _chord_ok(chords_v.get((tri, ly), ()), p, q)

    # one plant test per (edge, interval) per attempt: the point the
    # gate admits is the point the branch keeps — nothing re-sites it
    # later (G2g); the via must lie in the interval the arriving run
    # ends in (G2h)
    planted: dict[tuple[int, int, int], tuple[float, float, float] | None] = {}

    def plant(e: int, iv: int, ly: int) -> tuple[float, float, float] | None:
        key = (e, iv, ly)
        got = planted.get(key)
        if got is None and key not in planted:
            lo, hi = ivs(e, ly)[iv]
            got = planter.point(
                e, r_site, clearance_mm, k, dyn, lo, hi, drill_mm=via_drill_mm
            )
            planted[key] = got
        return got

    # layers the net sees alike — open on the same edges, usable by the
    # same pins (the inners of a board with no drilled pad in the way) —
    # are one state: the search keeps the first of each kind
    kinds: set[tuple] = set()
    cand_list: list[int] = []
    for ly in range(n_layers):
        if not cand_mask >> ly & 1:
            continue
        kind: tuple = (
            ((open_ >> ly) & 1).astype(np.uint8).tobytes(),
            ov.occupied[:, ly].tobytes() if ov is not None else b"",
            ov.tri_used[:, ly].tobytes()
            if ov is not None and ov.tri_used is not None
            else b"",
            tuple(pl >> ly & 1 for pl in pin_lay),
        )
        if neg_any and ov is not None:
            # layers alike on the negotiated tables too — a cheaper
            # history layer must not hide behind a dearer twin (td569540)
            kind += (
                ov.neg_edge_over[:, ly].tobytes()
                if ov.neg_edge_over is not None
                else b"",
                ov.neg_edge_hist[:, ly].tobytes()
                if ov.neg_edge_hist is not None
                else b"",
                ov.neg_tri_hist[:, ly].tobytes()
                if ov.neg_tri_hist is not None
                else b"",
                ov.neg_chord_hist[:, ly].tobytes()
                if ov.neg_chord_hist is not None
                else b"",
                ov.neg_tri_cross[:, ly].tobytes()
                if ov.neg_tri_cross is not None
                else b"",
            )
        if kind not in kinds:
            kinds.add(kind)
            cand_list.append(ly)
    # the step multiplier per layer: the congestion term, plus the price
    # of an earlier wire on that layer (a memoryview — plain-float reads)
    mul: dict[int, memoryview] = {
        ly: memoryview(
            np.ascontiguousarray(
                occ * (1.0 + OCC_LAYER_COST * ov.occupied[:, ly])
                if ov is not None
                else occ,
                dtype=float,
            )
        )
        for ly in cand_list
    }
    # the per-layer earlier-corridor count per triangle — the crossing
    # price a step INTO the tri pays (TRI_OCC_MM × count)
    tovm: dict[int, memoryview] = (
        {
            ly: memoryview(np.ascontiguousarray(ov.tri_used[:, ly], dtype=float))
            for ly in cand_list
        }
        if ov is not None and ov.tri_used is not None
        else {}
    )
    # negotiated tables (td569540), memoryviews over EVERY layer — the
    # goal gate can reach a layer the kind dedup folded away:
    #   muln  = mul × (1 + p × edge overuse)
    #   eadd  = edge history, mm per crossing
    #   tmul  = tri_used × (1 + p × tri overuse past _NEG_TRI_CAP)
    #   tadd  = tri history, mm per entry
    #   chadd = chord history, mm per crossing
    #   tcr   = committed crossings per (tri, layer) — chord overuse base
    muln: dict[int, memoryview] = {}
    eadd: dict[int, memoryview] = {}
    tmul: dict[int, memoryview] = {}
    tadd: dict[int, memoryview] = {}
    chadd: dict[int, memoryview] = {}
    tcr: dict[int, memoryview] = {}
    if neg_any and ov is not None:
        z_e = np.zeros(len(ctx.board))
        z_t = np.zeros(len(mesh.tris))
        for ly in range(n_layers):
            base = (
                occ * (1.0 + OCC_LAYER_COST * ov.occupied[:, ly])
                if ov is not None
                else occ
            )
            if ov.neg_edge_over is not None:
                base = base * (1.0 + neg_p * ov.neg_edge_over[:, ly])
            muln[ly] = memoryview(np.ascontiguousarray(base, dtype=float))
            eadd[ly] = memoryview(
                np.ascontiguousarray(
                    ov.neg_edge_hist[:, ly] if ov.neg_edge_hist is not None else z_e,
                    dtype=float,
                )
            )
            tu = ov.tri_used[:, ly] if ov.tri_used is not None else z_t
            tmul[ly] = memoryview(
                np.ascontiguousarray(
                    tu * (1.0 + neg_p * np.maximum(0.0, tu + 1.0 - _NEG_TRI_CAP)),
                    dtype=float,
                )
            )
            tadd[ly] = memoryview(
                np.ascontiguousarray(
                    ov.neg_tri_hist[:, ly] if ov.neg_tri_hist is not None else z_t,
                    dtype=float,
                )
            )
            chadd[ly] = memoryview(
                np.ascontiguousarray(
                    ov.neg_chord_hist[:, ly] if ov.neg_chord_hist is not None else z_t,
                    dtype=float,
                )
            )
            tcr[ly] = memoryview(
                np.ascontiguousarray(
                    ov.neg_tri_cross[:, ly] if ov.neg_tri_cross is not None else z_t,
                    dtype=float,
                )
            )

    def chord_cost(tri: int, ly: int, p: _V2, q: _V2) -> float:
        """The priced chord crossings of step ``p→q`` inside ``tri`` on
        layer ``ly`` — soft passes only: each crossed chord costs the
        base price scaled by the present factor over this tri-layer's
        committed crossings, plus its accumulated history (td569540)."""
        if not neg_soft or chords_v is None:
            return 0.0
        cs = chords_v.get((tri, ly))
        if not cs:
            return 0.0
        nc = _chords_crossed_n(cs, p, q)
        if not nc:
            return 0.0
        return nc * (
            _NEG_CROSS_MM * (1.0 + neg_p * (tcr[ly][tri] + nc)) + chadd[ly][tri]
        )

    # A* heuristic: distance from each pin to the NEAREST POINT of each
    # crossed edge (segment distance, not midpoint — round-4 finding 4:
    # sliver-fan midpoints sit millimetres off the channel axis, which
    # inflated h and g until a legal pad gap cost more than wrapping
    # the row). h(e) is the min over UNATTACHED pins, raised on each
    # attach (round-3 finding R3-2): an attached pin left in the min
    # keeps h ~0 around the growing tree and A* degenerates to Dijkstra
    # from the whole tree. Raising h leaves already-queued keys as
    # underestimates, so lazy entries stay correct; only edges whose
    # min was the attached pin are rescanned. ``pin_dist`` is a slice of
    # the ctx-wide pin→segment matrix (mutated by retire_pin, so a copy).
    pin_dist = ctx.pin_seg[:, pins].copy()
    hvec = pin_dist.min(axis=1)
    # the relax loop reads h per push — a memoryview gives plain-float
    # reads while retire_pin's numpy writes stay visible (round-4 perf)
    hview = memoryview(hvec)

    def retire_pin(j: int) -> None:
        affected = hvec == pin_dist[:, j]
        pin_dist[:, j] = math.inf
        if affected.any():
            hvec[affected] = pin_dist[affected].min(axis=1)

    def h(e: int) -> float:
        return hview[e]

    # which net pins sit in each triangle (goal check without scanning)
    tri_pins: dict[int, list[int]] = {}
    for j in range(len(pins)):
        for t in ctx.v2t[verts[j]]:
            tri_pins.setdefault(t, []).append(j)

    INF = math.inf
    span = len(mesh.tris) + 1
    # state key (G2h): ((e * nl + layer) * ivmax + iv) * span + t —
    # (edge, interval, layer) entering triangle t; the heap's b field is
    # (t * nl + layer) * ivmax + iv
    unplaced = set(range(len(pins)))
    branches: list[
        tuple[
            int,
            int,
            list[int],
            list[int],
            tuple[int, ...],
            list[tuple[int, float, float, float]],
            list[tuple[float, float]],
        ]
    ] = []
    components: list[list[int]] = []

    def grow(root: int) -> None:
        """One component's tree from ``root`` (already retired)."""
        dist: dict[int, float] = {}
        prev: dict[int, int] = {}
        src: dict[int, int] = {}
        # (key, tag, a, b, g): relax entries key on g + h and carry g so
        # a stale pop is detected against dist; goal entries key on the
        # exact total and carry it in b (encoded state) and g (unused).
        heap: list[tuple[float, int, int, int, float]] = []
        in_tree = {root}
        # attach[j] = (branch length, (src pin, chain) | None, src pin | None)
        attach: dict[
            int, tuple[float, tuple[int, tuple[int, ...]] | None, int | None]
        ] = {root: (0.0, None, None)}

        def push_state(
            e0: int,
            iv: int,
            t2: int,
            cost: float,
            j: int,
            ly: int,
            *,
            prev: dict[int, int] = prev,
            dist: dict[int, float] = dist,
            src: dict[int, int] = src,
            heap: list[tuple[float, int, int, int, float]] = heap,
        ) -> None:
            st = ((e0 * nl + ly) * ivmax + iv) * span + t2
            if cost >= dist.get(st, INF) - 1e-12:
                return
            if st in dist:
                # a cheaper re-seed roots the chain here: dropping the
                # old parent keeps prev a forest of edges new→old —
                # without it a descendant could re-parent a seeded
                # state and close a cycle in the walk (R4)
                prev.pop(st, None)
            dist[st] = cost
            src[st] = j
            heapq.heappush(
                heap, (cost + h(e0), 0, e0, (t2 * nl + ly) * ivmax + iv, cost)
            )

        def seed(j: int) -> None:
            # Prim: a fresh attach re-seeds at cost 0 — the heap key is
            # distance-to-tree, not distance-to-root (round-2 finding 3).
            # pin_dist[:, j] is already retired to inf (R3-2), so the
            # seed cost is read straight off the geometry. The pin leaves
            # on the layers it carries copper on.
            vj = vpos[j]
            lays = [ly for ly in cand_list if pin_lay[j] >> ly & 1]
            for t0 in ctx.v2t[verts[j]]:
                for e0 in ctx.tri_edge_list[t0]:
                    if not base_pass[e0]:
                        continue
                    t2a, t2b = ctx.edge_tris[e0]
                    t2 = t2a if t2a != t0 else t2b
                    if t2 < 0:
                        continue
                    seg = _pt_seg(vj, ctx.seg_u[e0], ctx.seg_v[e0])
                    for ly in lays:
                        if not open_[e0] >> ly & 1:
                            continue
                        for iv in range(len(ivs(e0, ly))):
                            q_ = mid(e0, ly, iv)
                            if not step_ok(t0, ly, vj, q_):
                                continue
                            if neg_any:
                                cst = (
                                    seg * muln[ly][e0]
                                    + eadd[ly][e0]
                                    + TRI_OCC_MM * tmul[ly][t2]
                                    + tadd[ly][t2]
                                    + chord_cost(t0, ly, vj, q_)
                                )
                            else:
                                cst = seg * mul[ly][e0] + (
                                    TRI_OCC_MM * tovm[ly][t2] if tovm else 0.0
                                )
                            push_state(e0, iv, t2, cst, j, ly)

        def push_goals(
            j: int,
            *,
            heap: list[tuple[float, int, int, int, float]] = heap,
        ) -> None:
            """Direct same-tri goals from a freshly attached pin."""
            vj = vpos[j]
            for t0 in ctx.v2t[verts[j]]:
                for j2 in tri_pins.get(t0, ()):
                    # a pad-to-pad wire inside one triangle hops nothing:
                    # it only exists on a face layer both pins share
                    if j2 in unplaced and pin_lay[j] & pin_lay[j2]:
                        total = math.dist(vj, vpos[j2])
                        heapq.heappush(heap, (total, 1, j2, -j - 1, total))

        def drain() -> None:
            while heap and unplaced:
                key, tag, a_, b_, g = heapq.heappop(heap)
                if tag == 1:
                    attach_goal(key, a_, b_)
                    continue
                e = a_
                xl, iv = divmod(b_, ivmax)
                t, ly = divmod(xl, nl)
                st = ((e * nl + ly) * ivmax + iv) * span + t
                if g > dist.get(st, INF) + 1e-9:
                    continue
                for j2 in tri_pins.get(t, ()):
                    if j2 not in unplaced:
                        continue
                    extra = 0.0
                    if not pin_lay[j2] >> ly & 1:
                        # reach the pad's face by a planted via on this
                        # last edge — the gate is the same plant test,
                        # bound to the arriving run's interval (G2h)
                        if not (fit[e] and open_[e] & pin_lay[j2]):
                            continue
                        via = plant(e, iv, ly)
                        if via is None:
                            continue
                        m0 = pin_lay[j2] & -pin_lay[j2]
                        ly2 = m0.bit_length() - 1
                        iv2 = interval_at(e, ly2, via[2])
                        qp = mid(e, ly2, iv2)
                        if not step_ok(t, ly2, qp, vpos[j2]):
                            continue
                        extra = VIA_COST_MM + chord_cost(t, ly2, qp, vpos[j2])
                    else:
                        qp = mid(e, ly, iv)
                        if not step_ok(t, ly, qp, vpos[j2]):
                            continue
                        extra = chord_cost(t, ly, qp, vpos[j2])
                    total = g + extra + float(pin_dist[e, j2])
                    heapq.heappush(heap, (total, 1, j2, st, total))
                if fit[e]:
                    om = int(open_[e])
                    if om & cand_mask & ~(1 << ly):
                        via = plant(e, iv, ly)
                        if via is not None:
                            for m in cand_list:
                                if m == ly or not om >> m & 1:
                                    continue
                                iv2 = interval_at(e, m, via[2])
                                nxt = ((e * nl + m) * ivmax + iv2) * span + t
                                nd = g + VIA_COST_MM
                                if nd < dist.get(nxt, INF) - 1e-12:
                                    prev[nxt] = st
                                    src[nxt] = src[st]
                                    dist[nxt] = nd
                                    heapq.heappush(
                                        heap,
                                        (
                                            nd + h(e),
                                            0,
                                            e,
                                            (t * nl + m) * ivmax + iv2,
                                            nd,
                                        ),
                                    )
                mid_e = mid(e, ly, iv)
                for e2, t2, wstep in ctx.tri_next[t].get(e, ()):
                    if t2 < 0 or not open_[e2] >> ly & 1:
                        continue
                    for iv2 in range(len(ivs(e2, ly))):
                        q_ = mid(e2, ly, iv2)
                        if not step_ok(t, ly, mid_e, q_):
                            continue
                        nxt = ((e2 * nl + ly) * ivmax + iv2) * span + t2
                        if neg_any:
                            nd = (
                                g
                                + wstep * muln[ly][e2]
                                + eadd[ly][e2]
                                + TRI_OCC_MM * tmul[ly][t2]
                                + tadd[ly][t2]
                                + chord_cost(t, ly, mid_e, q_)
                            )
                        else:
                            nd = (
                                g
                                + wstep * mul[ly][e2]
                                + (TRI_OCC_MM * tovm[ly][t2] if tovm else 0.0)
                            )
                        if nd < dist.get(nxt, INF) - 1e-12:
                            prev[nxt] = st
                            src[nxt] = src[st]
                            dist[nxt] = nd
                            heapq.heappush(
                                heap,
                                (nd + h(e2), 0, e2, (t2 * nl + ly) * ivmax + iv2, nd),
                            )

        def attach_goal(key: float, j2: int, b_: int) -> None:
            if j2 not in unplaced:
                return
            unplaced.discard(j2)
            in_tree.add(j2)
            retire_pin(j2)
            if b_ >= 0:
                # snapshot the winning chain NOW: later attaches
                # re-seed at 0 and can relabel prev/src under a
                # recorded goal state (a chain rerouted through the
                # attached pin's own seed rebuilt a self-branch)
                chain = []
                seen: set[int] = set()
                s = int(b_)
                while s in prev and s not in seen:
                    seen.add(s)
                    chain.append(s)
                    s = prev[s]
                chain.append(s)
                chain.reverse()
                attach[j2] = (key, (src[s], tuple(chain)), None)
            else:
                attach[j2] = (key, None, -b_ - 1)
            seed(j2)
            push_goals(j2)

        seed(root)
        push_goals(root)
        drain()

        members = sorted(in_tree)
        components.append(members)
        for j in members[1:]:
            _total, enc, src_j = attach[j]
            if enc is None:
                # direct same-tri goal: src_j is the tree pin index; the
                # branch lives on the lowest face layer the pins share
                i = src_j if src_j is not None else root
                shared = next(
                    t for t in ctx.v2t[verts[i]] if verts[j] in ctx.tri_set[t]
                )
                m0 = int(pin_lay[i] & pin_lay[j])
                branches.append(
                    (
                        pins[i],
                        pins[j],
                        [shared],
                        [],
                        ((m0 & -m0).bit_length() - 1,),
                        [],
                        [],
                    )
                )
                continue
            i, chain_t = enc
            # decode the state chain: a run of states is one corridor
            # step; two states on the same (edge, tri) are an in-place
            # hop — layers[i] is edge i's crossing layer, layers[i+1]
            # the layer it departs into the next tri on; bounds[i] the
            # interval the crossing was chosen in (G2h)
            et: list[int] = []
            layers: list[int] = []
            bounds: list[tuple[float, float]] = []
            hops: list[tuple[int, float, float, float]] = []
            prev_x = -1
            prev_eiv = (-1, -1, -1)  # (e, iv, ly) of the arriving state
            for s_ in chain_t:
                slot_, tt = divmod(int(s_), span)
                v_, iv = divmod(slot_, ivmax)
                e_, ly = divmod(v_, nl)
                x = e_ * span + tt
                if x == prev_x:
                    # in-place layer change on the current edge: the via
                    # is the point planted for the arriving run's
                    # interval, the post-hop layer is the next edge's
                    # crossing layer (appended by the next state)
                    hop = planted[prev_eiv]
                    assert hop is not None
                    hops.append((len(layers) - 1, hop[0], hop[1], hop[2]))
                    prev_eiv = (e_, iv, ly)
                    continue
                et.append(x)
                layers.append(ly)
                bounds.append(ivs(e_, ly)[iv])
                prev_x = x
                prev_eiv = (e_, iv, ly)
            edges = [x // span for x in et]
            first_ta, first_tb = ctx.edge_tris[edges[0]]
            first_t = first_tb if first_ta == et[0] % span else first_ta
            tris = [first_t] + [x % span for x in et]
            # the last triangle's layer is the layer the wire departs
            # the last edge on — the last state's, or the pin's face
            # after the "hop on the last edge" the goal gate admitted
            if not pin_lay[j] >> layers[-1] & 1:
                hop = planted[prev_eiv]
                assert hop is not None
                hops.append((len(edges) - 1, hop[0], hop[1], hop[2]))
                m0 = pin_lay[j] & -pin_lay[j]
                layers.append(m0.bit_length() - 1)
            else:
                layers.append(layers[-1])
            branches.append(
                (pins[i], pins[j], tris, edges, tuple(layers), hops, bounds)
            )

    while unplaced:
        root0 = min(unplaced)
        unplaced.discard(root0)
        retire_pin(root0)
        grow(root0)
    return branches, components, masks


def _make_passable(base_pass: np.ndarray):
    """The branch passable check for the shortcut pass and the re-walk
    validation — under net-level exemption it is simply the net's
    passable mask."""

    def passable(e: int) -> bool:
        return bool(base_pass[e])

    return passable


def _chords_crossed(
    ctx: _Ctx,
    mesh: TopoMesh,
    chords: Mapping[tuple[int, int], list[tuple[float, float, float, float]]],
    ly: int,
    tris: Sequence[int],
    edges: Sequence[int],
    t: Sequence[float],
    s_xy: tuple[float, float],
    e_xy: tuple[float, float],
) -> None:
    """Raise ``ValueError`` when the re-walked run crosses an earlier
    same-layer chord inside any of its triangles (G2h) — a shortcut
    release or the re-walk itself can straighten across a wire the
    interval state could not."""
    for i, tr in enumerate(tris):
        cs = chords.get((int(tr), int(ly)))
        if not cs:
            continue
        p = s_xy if i == 0 else edge_xy(mesh, int(edges[i - 1]), float(t[i - 1]))
        q = e_xy if i == len(edges) else edge_xy(mesh, int(edges[i]), float(t[i]))
        if not _chord_ok(cs, p, q):
            raise ValueError("retrack: crossed chord")


def _build_branch(
    ctx: _Ctx,
    mesh: TopoMesh,
    k: int,
    pa: int,
    pb: int,
    tris: list[int],
    edges: list[int],
    open_v: np.ndarray,
    pin_vids: frozenset[int],
    layers: tuple[int, ...],
    hops: list[tuple[int, float, float, float]],
    via_dia_mm: float,
    via_drill_mm: float = 0.0,
    bounds: Sequence[tuple[float, float]] | None = None,
    chords: Mapping[tuple[int, int], list[tuple[float, float, float, float]]]
    | None = None,
    standoff_mm: float = 0.0,
) -> tuple[WirePath, tuple[int, ...]]:
    """One found tree edge → a taut :class:`WirePath` (G2g): the hop
    edges split the search corridor into single-layer runs; each run is
    funnelled from its start point (the pin vertex, or the previous
    hop's planted via) to its end point (the next planted via, or the
    far pin), shortcut-released and re-walked under the run's own layer
    — a run is on ONE layer, so the per-edge open bit on it is the
    whole rule (``open_v`` is the net's mask under the view's closed
    layers). The two-pointer on the SEARCH corridor is the per-run
    fallback as before. Returns the path plus the RETRACKED crossed-edge
    ids — what corridor accounting must see, not the search edges."""
    va, vb = int(mesh.pin_vertex[pa]), int(mesh.pin_vertex[pb])
    if not edges:
        a, b = mesh.verts[va], mesh.verts[vb]
        points = (
            (float(a[0]), float(a[1])),
            (float(b[0]), float(b[1])),
        )
        return (
            WirePath(k, pa, pb, (tris[0],), (), (), points, layers=layers),
            (),
        )
    hop_at = {int(i): (float(x), float(y), float(t)) for i, x, y, t in hops}
    cuts = sorted(hop_at)
    # run q covers corridor edge indices a..b-1 (tris a..b); the hop
    # edge at b belongs to no run
    run_bounds: list[tuple[int, int]] = []
    prev = 0
    for c in cuts:
        run_bounds.append((prev, c))
        prev = c + 1
    run_bounds.append((prev, len(edges)))
    axy = (float(mesh.verts[va, 0]), float(mesh.verts[va, 1]))
    bxy = (float(mesh.verts[vb, 0]), float(mesh.verts[vb, 1]))
    r_site = via_dia_mm / 2.0
    edges_o: list[int] = []
    t_o: list[float] = []
    tris_o: list[int] = []
    pts_o: list[tuple[float, float]] = []
    lays_o: list[int] = []
    vias_o: list[PlantedVia] = []
    for q, (a_q, b_q) in enumerate(run_bounds):
        ly_q = layers[a_q]
        s_xy = axy if q == 0 else (hop_at[a_q - 1][0], hop_at[a_q - 1][1])
        e_xy = bxy if q == len(run_bounds) - 1 else (hop_at[b_q][0], hop_at[b_q][1])
        tris_q = tris[a_q : b_q + 1]
        edges_q = edges[a_q:b_q]

        def passable_q(e: int, ly_q: int = ly_q) -> bool:
            return bool(open_v[int(e)] >> ly_q & 1)

        def corridor_ok_q(es, ly_q: int = ly_q) -> bool:
            return all(open_v[int(e)] >> ly_q & 1 for e in es)

        bounds_q = bounds[a_q:b_q] if bounds is not None else None
        pts_fun = _taut_pts(
            ctx, mesh, tris_q, edges_q, s_xy, e_xy, bounds_q, standoff_mm
        )
        start_q = None if q == 0 else (tris[a_q], int(edges[a_q - 1]))
        end_skip = int(edges[b_q]) if b_q < len(edges) else -1
        pts_q = _shortcut(
            ctx,
            mesh,
            va,
            vb,
            pts_fun,
            passable_q,
            pin_vids,
            corridor_ok_q,
            start_q,
            end_skip,
        )
        emit = None
        for cand in (pts_q, pts_fun):
            try:
                tris2, edges2, t2 = _retrack(
                    ctx, mesh, va, vb, cand, passable_q, start_q, end_skip
                )
                # the re-walked corridor must not cross a wall either —
                # a taut segment can run along a blocked mesh edge and
                # pick the wrong side (round-2 finding 1)
                if not all(passable_q(int(e)) for e in edges2):
                    raise ValueError("retrack: blocked edge")
                if not corridor_ok_q(edges2):
                    raise ValueError("retrack: closed layer")
                if chords is not None:
                    _chords_crossed(
                        ctx, mesh, chords, ly_q, tris2, edges2, t2, s_xy, e_xy
                    )
            except ValueError:
                continue
            emit = (cand, tris2, edges2, t2)
            break
        if emit is None:
            cand = pts_fun
            tris2, edges2, t2 = (
                tuple(tris_q),
                tuple(edges_q),
                _two_pointer_t(ctx, tris_q, edges_q, pts_fun),
            )
        else:
            cand, tris2, edges2, t2 = emit
        edges_o.extend(int(e) for e in edges2)
        t_o.extend(float(t) for t in t2)
        tris_o.extend(int(t) for t in tris2)
        pts_o.extend((float(p[0]), float(p[1])) for p in (cand if q == 0 else cand[1:]))
        lays_o.extend([int(ly_q)] * len(tris2))
        if q < len(run_bounds) - 1:
            x, y, tv = hop_at[b_q]
            vias_o.append(
                PlantedVia(
                    i=len(edges_o),
                    k=len(pts_o) - 1,
                    x=x,
                    y=y,
                    dia_mm=via_dia_mm,
                    drill_mm=via_drill_mm,
                    r_site_mm=r_site,
                )
            )
            edges_o.append(int(edges[b_q]))
            t_o.append(float(tv))
    return (
        WirePath(
            net=k,
            pin_a=pa,
            pin_b=pb,
            tris=tuple(tris_o),
            edges=tuple(edges_o),
            t=tuple(t_o),
            points=tuple(pts_o),
            layers=tuple(lays_o),
            vias=tuple(vias_o),
        ),
        tuple(int(e) for e in edges_o),
    )


def _two_pointer_t(
    ctx: _Ctx,
    tris: list[int],
    edges: list[int],
    pts: list[tuple[float, float]],
) -> tuple[float, ...]:
    """Fallback crossing parameters on the SEARCH corridor when the
    re-walk fails — the two-pointer, restricted to its current segment
    (seam finding 8)."""
    t: list[float] = []
    seg = 0
    for e in edges:
        hit = None
        while seg < len(pts) - 1:
            hit = _portal_t(ctx, e, pts[seg], pts[seg + 1])
            if hit is not None:
                break
            seg += 1
        if hit is None:
            ux, uy = ctx.eu[e]
            ex, ey = ctx.ev[e][0] - ux, ctx.ev[e][1] - uy
            near = min(seg, len(pts) - 1)
            hit = min(
                1.0,
                max(
                    0.0,
                    ((pts[near][0] - ux) * ex + (pts[near][1] - uy) * ey)
                    / (ex * ex + ey * ey),
                ),
            )
        t.append(hit)
    return tuple(t)


def _portal_t(
    ctx: _Ctx, e: int, p: tuple[float, float], q: tuple[float, float]
) -> float | None:
    """The parameter along edge ``e``'s stored (u→v) where segment
    ``p→q`` meets it — a proper straddle's intersection, or an
    endpoint-on-segment's projection. ``None`` when they don't touch."""
    ux, uy = ctx.eu[e]
    vx, vy = ctx.ev[e]
    ex, ey = vx - ux, vy - uy
    dd = ex * ex + ey * ey
    sx, sy = q[0] - p[0], q[1] - p[1]
    denom = sx * ey - sy * ex
    if abs(denom) > _EPS:
        s = ((ux - p[0]) * sy - (uy - p[1]) * sx) / denom
        r = ((ux - p[0]) * ey - (uy - p[1]) * ex) / denom
        if -1e-9 <= r <= 1.0 + 1e-9 and -1e-9 <= s <= 1.0 + 1e-9:
            return min(1.0, max(0.0, s))
        return None
    for pt in (p, q):
        if abs(_area2(ctx.eu[e], ctx.ev[e], pt)) <= _EPS * max(1.0, dd):
            dot = (pt[0] - ux) * ex + (pt[1] - uy) * ey
            if -1e-9 <= dot <= dd + 1e-9:
                return min(1.0, max(0.0, dot / dd))
    if segments_cross(p, q, ctx.eu[e], ctx.ev[e]):
        dot = ((p[0] + q[0]) / 2.0 - ux) * ex + ((p[1] + q[1]) / 2.0 - uy) * ey
        return min(1.0, max(0.0, dot / dd))
    return None


@dataclass(slots=True)
class _Corridor:
    """The slice-4 channel record as a reusable ledger (gr520904,
    extracted gr520902): per-edge free stretch and trace capacity under
    the finest resolved rule, the pins bounding each edge, and the
    per-net slot weights — everything the routing A*'s occupancy term
    and the demand-vs-capacity cut price. ``build_wires`` pays it once;
    the crossing-resolution re-thread reuses it so a regrown tree is
    priced and cut by the SAME bookkeeping, not a copy."""

    ctx: _Ctx
    d: TopoDesign
    rules_used: Sequence[tuple[float, float]]
    t_ref: float
    c_ref: float
    gap_free: np.ndarray
    gap_cap: np.ndarray
    capacity_mm: np.ndarray
    is_channel: np.ndarray
    cap_total: np.ndarray
    inv_cap: np.ndarray
    edge_pins: list[frozenset[int]]
    edge_pads: dict[int, np.ndarray]  # edge -> pad roster indices (extras incl.)
    own_pins: list[frozenset[int]]
    weight: list[float]

    def occ(self, used: np.ndarray) -> np.ndarray:
        """The A* occupancy term (gr520904): a channel edge at full
        corridor capacity costs ``1 + _CONGESTION_ALPHA`` times its
        step length."""
        return 1.0 + _CONGESTION_ALPHA * used * self.inv_cap

    def add(self, used: np.ndarray, k: int, edges: Iterable[int]) -> None:
        """Fold one routed branch's retracked edges into ``used`` — a
        wire squeezing between pads of its OWN net owes no foreign-pad
        clearance, so it contributes neither occupancy nor demand."""
        for e_ in edges:
            if not self.edge_pins[int(e_)] <= self.own_pins[k]:
                used[int(e_)] += self.weight[k]

    def used_of(self, branches: Iterable[WirePath]) -> np.ndarray:
        """The ``used`` ledger a set of routed branches reads — the
        re-thread's occupancy base (the caller passes the KEPT
        branches: the re-routed net's own wires are being replaced)."""
        used = np.zeros(len(self.inv_cap))
        for b in branches:
            self.add(used, int(b.net), b.edges)
        return used

    def cut(
        self,
        branches: Sequence[WirePath],
        order: tuple[tuple[int, ...], ...],
        extra: Mapping[int, Sequence[tuple[int, Sequence[int]]]] | None = None,
    ) -> tuple[CorridorCut, ...]:
        """The phase-3 cut on the ROUTED wires (gr520904): per edge
        the slot-weighted count of crossing branches against the
        corridor capacity — demand over capacity is recorded for the
        caller to refuse on, never silently priced through. ``extra``
        adds an UNROUTED net's demand on its cut edges as ``(net, needed
        layers)``: a net the search could not fit through its cut still
        demands the slot, but only against the capacity on the layers it
        could be on there — the all-layers ``cap_total`` would hide a
        saturated single-layer channel under inner slots a top-only,
        via-less net can never reach (G2h — a refused negotiation over a
        closure is a capacity fact the refusal must name, not an
        unrouted silence)."""
        ctx, d = self.ctx, self.d

        def _owners(e: int) -> tuple[str, ...]:
            names = {
                str(d.parts[int(d.pin_part[p])]) for p in self.edge_pins[e] if p >= 0
            }
            # unpinned extra pads (edge_pins -1) still name their part
            names.update(
                str(d.parts[int(ctx.pad_part[pi_])])
                for pi_ in self.edge_pads.get(e, ())
            )
            names.update(
                str(d.zones[z].name or f"zone{z}")
                for z, _zm in ctx.zone_closed.get(e, ())
            )
            if ctx.board[e]:
                names.add("outline")
            return tuple(sorted(names)) or ("free space",)

        demand = np.zeros(len(self.inv_cap))
        demanders: dict[int, list[int]] = {}
        for e, seq in enumerate(order):
            for bi in seq:
                k = int(branches[bi].net)
                if not self.edge_pins[e] <= self.own_pins[k]:
                    demand[e] += self.weight[k]
                    demanders.setdefault(e, []).append(k)

        def _lone_fits(e: int, k: int) -> bool:
            """A LONE wire needs a point in the free stretch, not a
            full slot — the slot ledger reads a single coarser-rule
            wire as demand > 1 on a one-slot corridor even when its
            centreline fits, which refused a routed esp32c3 net
            pre-hardening (gr520904 follow-up). It cuts only when its
            own rule leaves it no room on any layer (a fat rail
            through a thin gap: genuine congestion, and exactly the
            cram the cut exists to name)."""
            t_k, c_k = self.rules_used[k]
            if self.is_channel[e]:
                # gap_free is measured under the ref rule's growth —
                # the wire fits where a point survives its own growth
                need = (t_k + 2 * c_k) - (self.t_ref + 2 * self.c_ref)
                return bool((self.gap_free[e] > need + 1e-9).any())
            return bool(self.capacity_mm[e] > t_k + 2 * c_k + 2 * FAB_QUANTUM_MM + 1e-9)

        def _names(ks: Iterable[int]) -> tuple[str, ...]:
            return tuple(
                sorted(
                    {str(d.net_name[k]) if k < len(d.net_name) else str(k) for k in ks}
                )
            )

        def _record(
            e: int, dem: float, cap: float, ks: Iterable[int], refused: bool
        ) -> None:
            cur = cuts.get(e)
            if cur is None or dem - cap > cur.demand - cur.cap:
                cuts[e] = CorridorCut(
                    edge=e,
                    demand=float(dem),
                    cap=float(cap),
                    owners=_owners(e),
                    nets=_names(ks),
                    x=float((ctx.eu[e][0] + ctx.ev[e][0]) / 2.0),
                    y=float((ctx.eu[e][1] + ctx.ev[e][1]) / 2.0),
                    # a routed-demand record keeps its refusal weight:
                    # the extra path may still out-surplus it, but the
                    # edge's over-capacity is then real, not refused
                    refused=refused and (cur is None or cur.refused),
                )

        cuts: dict[int, CorridorCut] = {}
        for ei in np.flatnonzero(demand > self.cap_total + 1e-9):
            e = int(ei)
            dm = demanders[e]
            if len(dm) > 1 or not _lone_fits(e, dm[0]):
                _record(e, float(demand[e]), float(self.cap_total[e]), dm, False)
        if extra:
            # per-layer capacity an unrouted net's needed layers see —
            # cap_total's inner slots are not usable by a net that
            # cannot get off its pads' face
            n_ly = self.gap_cap.shape[1]
            free_cap = (
                np.floor(self.capacity_mm / (self.t_ref + self.c_ref + FAB_QUANTUM_MM))
                + 1.0
            )
            lay_cap = np.where(
                self.is_channel[:, None],
                np.clip(self.gap_cap, 0, None),
                np.broadcast_to(free_cap[:, None], self.gap_cap.shape),
            )
            for e, ks_lys in extra.items():
                e = int(e)
                use = np.zeros(n_ly)
                use_nets: list[set[int]] = [set() for _ in range(n_ly)]
                for bi in order[e]:
                    b = branches[bi]
                    kb = int(b.net)
                    if self.edge_pins[e] <= self.own_pins[kb]:
                        continue
                    for i, e_ in enumerate(b.edges):
                        if int(e_) != e:
                            continue
                        if len(b.layers) == len(b.edges) + 1:
                            use[int(b.layers[i])] += self.weight[kb]
                            use_nets[int(b.layers[i])].add(kb)
                        else:
                            use += self.weight[kb]
                            for s_ in use_nets:
                                s_.add(kb)
                firing = [
                    (k, tuple(int(ly) for ly in lys))
                    for k, lys in ks_lys
                    if not self.edge_pins[e] <= self.own_pins[k]
                    and float(sum(use[ly] for ly in lys)) + self.weight[k]
                    > float(sum(lay_cap[e, ly] for ly in lys)) + 1e-9
                ]
                if not firing:
                    continue
                lys_u = sorted({ly for _k, lys in firing for ly in lys})
                dem = float(sum(use[ly] for ly in lys_u)) + sum(
                    self.weight[k] for k, _l in firing
                )
                cap = float(sum(lay_cap[e, ly] for ly in lys_u))
                ks = {n for ly in lys_u for n in use_nets[ly]} | {k for k, _l in firing}
                if len(ks) == 1 and _lone_fits(e, next(iter(ks))):
                    continue
                _record(e, dem, cap, ks, True)
        return tuple(cuts[e] for e in sorted(cuts))


def _corridor(
    ctx: _Ctx,
    mesh: TopoMesh,
    d: TopoDesign,
    n_layers: int,
    rules_used: Sequence[tuple[float, float]],
    pose: np.ndarray,
) -> _Corridor:
    """Price the corridor ledger once (gr520904): the channel record
    at the finest resolved rule — the most slack a channel ever shows;
    coarser nets just see less free. Priced BEFORE routing: it feeds
    both the A* occupancy term and the demand-vs-capacity cut on the
    routed wires. ``pose`` is the pose the mesh was built at: the
    capacity reads each endpoint pad's extent through it."""
    t_ref, c_ref = finest_rule(rules_used)
    _b, free_ref, edge_pads_ref, _p = _pad_eval(
        ctx, t_ref / 2.0 + c_ref + FAB_QUANTUM_MM
    )
    n_e = len(mesh.edges)
    gap_free = np.full((n_e, n_layers), -1.0)
    gap_cap = np.full((n_e, n_layers), -1, dtype=np.int64)
    for e in edge_pads_ref:
        f = free_ref[e]
        gap_free[e] = f
        # total traces the free stretch takes, in the free width's own
        # frame: floor(f / (t + c)) + 1 — a just-passable channel
        # records 1, a closed one 0 (round-5: the earlier
        # (f + c) / (t + c) form undercounted by up to one trace and
        # recorded cap 0 on passable gaps)
        gap_cap[e] = np.where(
            f > 1e-9, np.floor(f / (t_ref + c_ref + FAB_QUANTUM_MM)) + 1.0, 0.0
        ).astype(np.int64)
    capacity_mm = _edge_capacity_mm(ctx, mesh, d, np.asarray(pose))
    # The corridor capacity the occupancy term and the cut share: a pad
    # channel takes gap_cap traces summed across EVERY layer a wire can
    # use (an inner signal layer is as good a crossing as an outer);
    # an edge no pad rings takes its free crossing length's worth on
    # each of the n_layers. Outer-layers-only pricing undercounted a
    # 4-layer channel by the inner share and refused boards that fit
    # (gr520904 follow-up, esp32c3 + nano probes).
    is_channel = (gap_cap >= 0).any(axis=1)
    cap_total = np.where(
        is_channel,
        np.clip(gap_cap, 0, None).sum(axis=1),
        (np.floor(capacity_mm / (t_ref + c_ref + FAB_QUANTUM_MM)) + 1.0) * n_layers,
    )
    inv_cap = np.zeros(n_e)
    np.divide(1.0, cap_total, out=inv_cap, where=cap_total > 0)
    # The pins bounding each edge — pads ringing it in the gap record
    # plus pins at its endpoints. A wire squeezing between pads of its
    # OWN net owes no foreign-pad clearance, so it contributes neither
    # occupancy nor demand (an own-net pin pair's edge is a legal
    # crossing even when its capacity reads 0).
    vid_pin = {int(mesh.pin_vertex[p]): p for p in range(len(mesh.pin_vertex))}
    edge_pins: list[frozenset[int]] = []
    for e in range(n_e):
        pins_e = {int(ctx.pad_pin[pi_]) for pi_ in edge_pads_ref.get(e, ())}
        pins_e.update(vid_pin[int(v)] for v in mesh.edges[e] if int(v) in vid_pin)
        edge_pins.append(frozenset(pins_e))
    own_pins = [
        frozenset(int(p) for p in d.net_pins[k]) for k in range(len(d.net_pins))
    ]
    weight = [
        (t + c + FAB_QUANTUM_MM) / (t_ref + c_ref + FAB_QUANTUM_MM)
        for t, c in rules_used
    ]
    return _Corridor(
        ctx=ctx,
        d=d,
        rules_used=rules_used,
        t_ref=t_ref,
        c_ref=c_ref,
        gap_free=gap_free,
        gap_cap=gap_cap,
        capacity_mm=capacity_mm,
        is_channel=is_channel,
        cap_total=cap_total,
        inv_cap=inv_cap,
        edge_pins=edge_pins,
        edge_pads=edge_pads_ref,
        own_pins=own_pins,
        weight=weight,
    )


@dataclass(slots=True)
class _Built:
    """One branch of a net's attempt: the taut wire and its RETRACKED
    corridor edges."""

    branch: WirePath
    edges: tuple[int, ...]


@dataclass(slots=True)
class _NetAttempt:
    """One growth of a net under an occupancy view: the tree, its built
    branches, the taut crossings left on their own layer (``cross``,
    :meth:`_Occupancy.crossings`' tuples), and the same-net via
    collisions the build dropped (G2g — ``(net, pin_a, pin_b)`` per
    branch)."""

    found: list[
        tuple[
            int,
            int,
            list[int],
            list[int],
            tuple[int, ...],
            list[tuple[int, float, float, float]],
            list[tuple[float, float]],
        ]
    ]
    comps: list[list[int]]
    masks: _NetMasks
    built: list[_Built]
    cross: list[tuple[int, int, int, int, int]]
    collisions: list[tuple[int, int, int]]

    def key(self) -> tuple[int, int]:
        """Worse sorts higher: unreached components, same-layer taut
        crossings."""
        return (len(self.comps), len(self.cross))


class _Occupancy:
    """What the nets built so far leave behind (G2e), as the next net's
    search and layer pass read it: per (edge, layer) the slot-weighted
    count of earlier wires against the layer's own capacity, and every
    finished branch's taut wire with the layers it runs on. ``add`` takes
    a finished branch; ``view`` is what the next net's search reads;
    ``crossings`` tests a new branch's taut wire against the earlier
    ones."""

    def __init__(
        self,
        corridor: _Corridor,
        mesh: TopoMesh,
        n_layers: int,
        pose: np.ndarray | None = None,
        *,
        planter: _ViaPlanter | None = None,
        net_clearance: Sequence[float] | None = None,
        net_clearance_by_name: Mapping[str, float] | None = None,
        obstacles: np.ndarray | None = None,
    ) -> None:
        self.corridor = corridor
        self.mesh = mesh
        self.n_layers = n_layers
        self.pose = pose
        n_e = len(corridor.inv_cap)
        self.used = np.zeros((n_e, n_layers))
        slot = corridor.t_ref + corridor.c_ref + FAB_QUANTUM_MM
        free_cap = np.floor(corridor.capacity_mm / slot) + 1.0
        self.cap = np.where(
            corridor.is_channel[:, None],
            np.clip(corridor.gap_cap, 0, None),
            free_cap[:, None],
        )
        # every finished branch: its taut line, crossing geometry and the
        # layer per corridor triangle (``None``: unknown)
        self.lines: list[tuple[WirePath, LineString, Geom, tuple[int, ...] | None]] = []
        self._line_tree: shapely.STRtree | None = None
        self.obstacles = (
            obstacles
            if obstacles is not None
            else np.asarray(mesh.verts, dtype=float)[
                np.asarray(
                    sorted(obstacle_vertices(mesh, corridor.d, n_layers)), dtype=int
                )
            ].reshape(-1, 2)
        )
        # G2g: the via-point oracle, the earlier wires' bodies for stage
        # C, and each planted via's barrel reach per edge — the least
        # ``segdist(edge, via) - r_site`` over vias planted so far
        self.planter = (
            planter
            if planter is not None
            else _ViaPlanter(
                corridor.ctx,
                corridor.d,
                pose,
                net_clearance=net_clearance,
                net_clearance_by_name=net_clearance_by_name,
            )
        )
        self.wire_bodies: list[Body] = []
        self.vias: list[
            tuple[float, float, float, float]
        ] = []  # x, y, r_site, clearance
        self.via_close = np.full(n_e, math.inf)
        # Both minima are needed for max(caller, obstacle): the closest
        # barrel need not be the one with the largest clearance reach.
        self.via_clear = np.full(n_e, math.inf)
        # per (tri, layer) how many earlier corridors hold the tri there —
        # the crossing signal the layered search prices (G2g): crossing
        # a wire on its layer inside a triangle it routes through
        self.tri_used = np.zeros((len(mesh.tris), n_layers))
        # negotiated passes: same-layer chord crossings the committed
        # wires still carry per (tri, layer) — the chord resource's
        # present overuse (td569540)
        self.cross_used = np.zeros((len(mesh.tris), n_layers))
        # G2h: every earlier wire's chord inside each corridor triangle
        # per layer, and the crossing parameters splitting each edge —
        # the search's legality, not a price
        self.chords: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
        self.splits: dict[tuple[int, int], list[float]] = {}

    def intervals(self, e: int, ly: int) -> list[tuple[float, float]]:
        """The sub-intervals ``(e, ly)``'s split points leave —
        consecutive bounds of ``[0, *splits, 1]`` (one interval when
        unsplit)."""
        spl = self.splits.get((int(e), int(ly)))
        if not spl:
            return [(0.0, 1.0)]
        bd = [0.0, *spl, 1.0]
        return [iv for iv in itertools.pairwise(bd) if iv[1] - iv[0] > 1e-9]

    def interval_at(self, e: int, ly: int, t: float) -> int:
        """The index into :meth:`intervals` the parameter ``t`` lands
        in."""
        spl = self.splits.get((int(e), int(ly)))
        return bisect.bisect_right(spl, t - 1e-9) if spl else 0

    def step_ok(self, tri: int, ly: int, p: _V2, q: _V2) -> bool:
        """Whether the step ``p→q`` through ``tri`` on layer ``ly``
        keeps one side of every earlier chord there."""
        return _chord_ok(self.chords.get((int(tri), int(ly)), ()), p, q)

    def nets_on(self, edges: Iterable[int], mask: int) -> Counter:
        """Nets whose wires cross any of ``edges`` on a layer in
        ``mask`` — most hits first (:class:`Counter`)."""
        es = frozenset(int(e) for e in edges)
        out: Counter = Counter()
        for b, _ln, _g, lays in self.lines:
            if not lays:
                continue
            for i, e in enumerate(b.edges):
                if int(e) in es and mask >> int(lays[i]) & 1:
                    out[int(b.net)] += 1
                    break
        return out

    def rebuild(self, kept: Iterable[tuple[int, _Built]]) -> _Occupancy:
        """A fresh ledger replayed over ``kept`` in order — the
        negotiation swaps whole instances rather than removing wires in
        place. The planter is shared: its caches are static."""
        new = _Occupancy(
            self.corridor,
            self.mesh,
            self.n_layers,
            self.pose,
            planter=self.planter,
            obstacles=self.obstacles,
        )
        for k, built in kept:
            new.add(k, built)
        return new

    def view(self, k: int, neg: _NegCost | None = None) -> _OccView:
        """Net ``k``'s view: a layer closes on an edge it has no slot
        left on, unless the edge only bounds the net's own pads. A
        planted via closes EVERY layer of every edge its barrel plus the
        net's track stand-off covers (G2g), the hop edge included.
        ``neg`` (td569540): on a soft pass the slot closures become a
        priced ``neg_edge_over`` table instead — only the via reach
        stays a wall — and the history tables ride the view."""
        c = self.corridor
        over = (self.used > 1e-9) & (self.used + c.weight[k] > self.cap + 1e-9)
        bits = (over * (1 << np.arange(self.n_layers))).sum(axis=1).astype(np.int64)
        exempt: list[int] = []
        for e in np.flatnonzero(bits):
            # only a true channel closes: an edge two pads bound. An edge
            # along one pad's side is a sliver of its own free stretch,
            # and wires wrapping the same corner share it
            if (
                c.edge_pins[int(e)] <= c.own_pins[k]
                or len(c.edge_pads.get(int(e), ())) < 2
            ):
                bits[e] = 0
                exempt.append(int(e))
        t_k, c_k = c.rules_used[k]
        via_bits = np.zeros(len(bits), dtype=np.int64)
        via_bits[self.via_close < t_k / 2.0 + c_k + FAB_QUANTUM_MM] |= (
            1 << self.n_layers
        ) - 1
        via_bits[self.via_clear < t_k / 2.0 + FAB_QUANTUM_MM] |= (
            1 << self.n_layers
        ) - 1
        edge_over = None
        if neg is not None and neg.soft:
            # demand beyond capacity this net's wire would add — the
            # same (used, weight, cap) reading the hard closure uses,
            # kept as a magnitude for the present factor (td569540)
            edge_over = np.where(
                self.used > 1e-9,
                np.maximum(0.0, self.used + c.weight[k] - self.cap),
                0.0,
            )
            if exempt:
                edge_over[exempt] = 0.0
        return _OccView(
            closed=via_bits if neg is not None and neg.soft else bits | via_bits,
            occupied=self.used > 1e-9,
            dyn=self.dyn_index(),
            tri_used=self.tri_used,
            chords=self.chords,
            splits=self.splits,
            neg_soft=neg is not None and neg.soft,
            neg_p=neg.p if neg is not None else 0.0,
            neg_edge_over=edge_over,
            neg_edge_hist=neg.edge_hist if neg is not None else None,
            neg_tri_hist=neg.tri_hist if neg is not None else None,
            neg_chord_hist=neg.chord_hist if neg is not None else None,
            neg_tri_cross=self.cross_used if neg is not None else None,
        )

    def dyn_index(
        self, extra: Iterable[tuple[float, float, float]] = ()
    ) -> CopperIndex:
        """The plant test's dynamic index as ``view`` builds it — the
        earlier wires' bodies plus every planted via's barrel — with
        ``extra`` ``(x, y, r_site)`` discs added ``ANY_NET``-tagged: a
        hop of the same net still bars the next one at drill clearance,
        so the net's own earlier vias must block like foreign ones."""
        dyn = CopperIndex(self.wire_bodies)
        for x, y, r, clearance in self.vias:
            dyn.add_disc(x, y, r, net=ANY_NET, clearance=clearance)
        for x, y, r in extra:
            dyn.add_disc(x, y, r, net=ANY_NET)
        return dyn

    def nearby(self, line: LineString) -> list[int]:
        """Intersecting branch indices, in ledger order; rebuilt after add.

        Crossing witnesses only depend on intersecting centrelines. Share this
        index with the shove's affected-net checks instead of scanning the cell.
        """
        if self._line_tree is None:
            self._line_tree = shapely.STRtree([row[1] for row in self.lines])
        return sorted(
            int(j) for j in self._line_tree.query(line, predicate="intersects")
        )

    def crossings(
        self, b: WirePath, *, witness: bool = False
    ) -> list[tuple[int, int, int, int, int]]:
        """The taut wire's own live crossings with earlier nets' wires,
        as ``(edge index on b, layer the other wire is on there, index of
        the other wire, the crossing's ``(lo, hi]`` edge-index span on
        that wire)``; the layer is ``-1`` where the other wire has none
        assigned (it crosses on every layer). The crossing rule is
        ``assign_layers``' (:mod:`.crossings`): taut polylines that swap
        sides, less the empty bigons the wires can slide apart through.
        One batched event search for all earlier wires b's line meets.

        A ``witness`` must describe copper the realiser can emit: restore
        cancelled bigons whose arc swap would move a planted via, change
        layers, hit foreign pads, or fail the realiser's own geometric
        swap test. A 2D cancellation alone cannot certify a chord-free
        probe."""
        pts = list(dict.fromkeys(b.points))
        if (not b.edges and not witness) or len(pts) < 2 or not self.lines:
            return []
        line = LineString(pts)
        near = self.nearby(line)
        if not near:
            return []
        branches = [b, *(self.lines[j][0] for j in near)]
        geom = [branch_geom(self.mesh, b), *(self.lines[j][2] for j in near)]
        events = crossing_events(self.mesh, branches, geom, first_only=True)
        regs, gone = resolve_crossings(self.mesh, branches, self.obstacles, events)
        if witness and gone:
            # Lazy: realise imports wires. Reuse the actual repair rather
            # than treating every same-layer empty bigon as repairable.
            from precis.pcb.topoplace.realise import _repair_bigon

            # _spoke_offsets also refuses swaps that increase foreign-pad
            # overlap. Use its same cached pad geometry and pin ownership.
            ctx, d = self.corridor.ctx, self.corridor.d
            pad_tree = shapely.STRtree(ctx.pad_poly)
            pin_nets = d.pin_nets()

            def on_foreign_pad(ln: LineString, net: int) -> float:
                return sum(
                    float(ln.intersection(ctx.pad_poly[int(pi)]).length)
                    for pi in pad_tree.query(ln, predicate="intersects")
                    if (
                        pin_nets[int(ctx.pad_pin[int(pi)])][1]
                        if 0 <= int(ctx.pad_pin[int(pi)]) < len(pin_nets)
                        else -1
                    )
                    != net
                )

            for a, m, x, y in gone:
                ba, bb = branches[a], branches[m]
                ta, ca = self.corridor.rules_used[ba.net]
                tb, cb = self.corridor.rules_used[bb.net]
                swapped = _repair_bigon(
                    ba,
                    bb,
                    x,
                    y,
                    (ta + tb) / 2 + max(ca, cb) + FAB_QUANTUM_MM,
                    on_foreign_pad,
                )
                if swapped is not None:
                    continue
                regs.setdefault((a, m), []).extend((x[2], y[2]))
        out: list[tuple[int, int, int, int, int]] = []
        for (_a, m), rs in sorted(regs.items()):
            j = near[m - 1]
            lays = self.lines[j][3]
            for _ia1, ia2, ib1, ib2 in rs:
                lo = lays[min(ib2, len(lays) - 1)] if lays else -1
                out.append((ia2, lo, j, ib1, ib2))
        return out

    def pen(
        self, b: WirePath, cross: Sequence[tuple[int, int, int, int, int]]
    ) -> list[list[float]]:
        """:func:`layer_dp`'s per-edge, per-layer price of a branch: one
        for each taut crossing (``cross``, :meth:`crossings`) on that
        layer. A crossing with a wire of no known layer is on every layer
        and tells the layers apart not at all."""
        rows = [[0.0] * self.n_layers for _ in b.edges]
        for i, lo, _j, _s0, _s1 in cross:
            if lo >= 0 and i < len(rows):
                rows[i][lo] += 1.0
        return rows

    def add(self, k: int, built: _Built) -> None:
        """Fold a finished branch into the ledger: its taut wire always
        (whatever layers it has), the slots it uses where the layers are
        known, its wire body and planted vias for the next nets' plant
        test (G2g)."""
        b, lays = built.branch, built.branch.layers
        t_k, c_k = self.corridor.rules_used[k]
        pts = list(dict.fromkeys(b.points))
        if len(pts) >= 2:
            self.lines.append((b, LineString(pts), branch_geom(self.mesh, b), lays))
            self._line_tree = None
            self.wire_bodies.append(
                Body(LineString(pts), r=t_k / 2.0, net=k, clearance=c_k)
            )
        for v in b.vias:
            self.vias.append((v.x, v.y, v.r_site_mm, c_k))
            # per edge, the least segdist - r_site over planted vias —
            # the barrel's reach the next view's closure prices
            u = self.corridor.ctx.seg_u
            dvec = self.corridor.ctx.seg_v - u
            dd = np.maximum((dvec * dvec).sum(axis=1), _EPS)
            tp = np.clip(
                ((v.x - u[:, 0]) * dvec[:, 0] + (v.y - u[:, 1]) * dvec[:, 1]) / dd,
                0.0,
                1.0,
            )
            near = u + tp[:, None] * dvec
            dist = np.hypot(v.x - near[:, 0], v.y - near[:, 1])
            self.via_close = np.minimum(self.via_close, dist - v.r_site_mm)
            self.via_clear = np.minimum(self.via_clear, dist - v.r_site_mm - c_k)
        if lays is None or len(lays) != len(b.edges) + 1:
            for t_ in b.tris:
                self.tri_used[int(t_), :] += 1
            return
        c = self.corridor
        for i, e in enumerate(b.edges):
            if not c.edge_pins[int(e)] <= c.own_pins[k]:
                self.used[int(e), lays[i]] += c.weight[k]
        for i, t_ in enumerate(b.tris):
            self.tri_used[int(t_), lays[i]] += 1
        # G2h: the wire's chord inside each corridor triangle (per layer)
        # and the parameters it splits every crossed edge at
        va = self.mesh.verts[int(self.mesh.pin_vertex[b.pin_a])]
        vb = self.mesh.verts[int(self.mesh.pin_vertex[b.pin_b])]
        pa_ = (float(va[0]), float(va[1]))
        pb_ = (float(vb[0]), float(vb[1]))
        for i, tr in enumerate(b.tris):
            p_ = (
                pa_
                if i == 0
                else edge_xy(self.mesh, int(b.edges[i - 1]), float(b.t[i - 1]))
            )
            q_ = (
                pb_
                if i == len(b.edges)
                else edge_xy(self.mesh, int(b.edges[i]), float(b.t[i]))
            )
            if math.hypot(q_[0] - p_[0], q_[1] - p_[1]) > 1e-9:
                cs_key = (int(tr), int(lays[i]))
                cs = self.chords.get(cs_key)
                if cs:
                    # negotiated passes price this (tri, layer)'s chord
                    # crossings by how many the committed wires carry —
                    # count the new chord's crossings of the earlier ones
                    # (the mirrored record below is the same physical
                    # segment and is not counted twice)
                    self.cross_used[int(tr), int(lays[i])] += sum(
                        1 for c in cs if _chord_crossed(c, p_, q_)
                    )
                self.chords.setdefault(cs_key, []).append((*p_, *q_))
                # a chord lying along one of the tri's own edges (every
                # fan spoke through a wrapped vertex leaves along the
                # spoke) is invisible to a step ending on that edge in
                # the triangle ACROSS it — record it there too so the
                # edge is blocked on this layer (G2h review-2 P1)
                for e_ in (int(x) for x in self.mesh.tri_edges[int(tr)]):
                    a_, b_ = c.ctx.eu[e_], c.ctx.ev[e_]
                    if not (
                        abs(_area2(a_, b_, p_)) <= 1e-7
                        and abs(_area2(a_, b_, q_)) <= 1e-7
                    ):
                        continue
                    t2a, t2b = c.ctx.edge_tris[e_]
                    t2 = t2a if t2a != int(tr) else t2b
                    if t2 >= 0:
                        self.chords.setdefault((t2, int(lays[i])), []).append(
                            (*p_, *q_)
                        )
                    break
        for i, e in enumerate(b.edges):
            ti = float(b.t[i])
            if ti <= 1e-9 or ti >= 1.0 - 1e-9:
                continue
            for ly in (int(lays[i]), int(lays[i + 1])):
                lst = self.splits.setdefault((int(e), ly), [])
                if all(abs(ti - x) > 1e-9 for x in lst):
                    bisect.insort(lst, ti)


def _edge_layers(n: int, runs: Sequence[int], cuts: Sequence[int]) -> tuple[int, ...]:
    """A branch's layer per corridor triangle from :func:`layer_dp`'s
    ``(runs, cuts)`` (:attr:`WirePath.layers`): edge ``i`` is on the run
    whose cut range ``(cuts[p-1] .. cuts[p]]`` holds it, the last
    triangle on the final run."""
    out: list[int] = []
    p = 0
    for i in range(n):
        while p < len(cuts) and cuts[p] < i:
            p += 1
        out.append(int(runs[p]))
    return (*out, int(runs[-1]))


def _bounds_of(
    occupancy: _Occupancy,
    edges2: Sequence[int],
    t: Sequence[float],
    lays: Sequence[int],
) -> list[tuple[float, float]]:
    """The (edge, layer) interval each rebuilt crossing lands in (G2h)
    — the relayer's rebuild funnels its portals to the same side of the
    earlier chords."""
    return [
        occupancy.intervals(int(e), int(lays[i]))[
            occupancy.interval_at(int(e), int(lays[i]), float(t[i]))
        ]
        for i, e in enumerate(edges2)
    ]


def _runs_view_closed(b: WirePath, open_v: np.ndarray) -> bool:
    """A rebuilt branch whose retracked run edges sit on a layer the
    occupancy view closed on them. The relayer DP's cut legality is the
    STATIC open mask — priced view closures enter only through
    ``pen`` — and ``_build_branch``'s retrack fallback never re-judges
    its corridor either, so the accept test re-checks every emitted
    edge against the view the search ran under."""
    rlays = b.layers
    return any(
        not (int(open_v[int(e)]) >> int(rlays[i])) & 1 for i, e in enumerate(b.edges)
    )


def _relayer_branch(
    ctx: _Ctx,
    mesh: TopoMesh,
    d: TopoDesign,
    n_layers: int,
    k: int,
    rule: tuple[float, ...],
    occupancy: _Occupancy,
    dyn: CopperIndex | None,
    masks: _NetMasks,
    open_v: np.ndarray,
    pin_vids: frozenset[int],
    cand_mask: int,
    branch: WirePath,
    edges2: tuple[int, ...],
    hits: list[tuple[int, int, int, int, int]],
) -> tuple[WirePath, tuple[int, ...], list[tuple[int, int, int, int, int]]]:
    """The planted resolver's in-build move (G2g): a same-layer crossing
    of the just-built wire can still flip LAYERS — what the wall loop
    otherwise re-grows the whole tree for — but the flip costs concrete
    vias now, so a :func:`layer_dp` over the
    corridor, ``pen`` on every crossed layer, a cut legal only on an
    edge ``occupancy.planter`` has a point for (against the same ``dyn``
    the search saw), and ``prefer`` keeping the search's vias unless a
    crossing pays. Legality is the STATIC open mask
    (``masks.open``): the occupancy view's closures are priced by
    ``pen`` and re-judged by the accept test, not bound — a foreign
    via's edge-granular barrel closure would otherwise kill the whole
    corridor. The rebuild plants the DP's cut points and is kept only
    where it strictly lowers the taut crossing count — a rebuilt run
    can walk onto a NEW crossing, and that trade never happens. A kept
    rebuild that still crosses loops again (strict decrease, so it
    terminates): the residue a rebuild picks up is usually another
    plantable hop away, and even a partial chain pulls crossing pairs
    off the emit (reverting a stalled chain measured worse)."""
    r_site = rule[2] / 2.0
    clearance = rule[1]
    drill_k = float(rule[3] if len(rule) > 3 and rule[3] else rule[2] * 0.5)
    hh_r = max(0.0, drill_k + JLC_VIA_HOLE_TO_HOLE_MM - r_site - clearance)
    while True:
        lays = branch.layers
        on_layer = [
            h
            for h in hits
            if lays and (h[1] < 0 or lays[min(h[0], len(lays) - 1)] == h[1])
        ]
        if not on_layer:
            return branch, edges2, hits
        bnd_old = _bounds_of(occupancy, edges2, branch.t, lays)
        plantable = [
            occupancy.planter.point(
                int(e),
                r_site,
                clearance,
                k,
                dyn,
                lo=bnd_old[i][0],
                hi=bnd_old[i][1],
                drill_mm=rule[3] if len(rule) > 3 else 0.0,
            )
            is not None
            for i, e in enumerate(edges2)
        ]
        runs, cuts, err = layer_dp(
            [int(masks.open[int(e)]) for e in edges2],
            plantable,
            _pin_layers(d, branch.pin_a, n_layers, cand_mask),
            _pin_layers(d, branch.pin_b, n_layers, cand_mask),
            n_layers,
            pen=occupancy.pen(branch, hits),
            prefer=lays,
        )
        if err is not None:
            return branch, edges2, hits
        new_lays = _edge_layers(len(edges2), runs, cuts)
        still = sum(
            1
            for h in hits
            if h[1] < 0 or new_lays[min(h[0], len(new_lays) - 1)] == h[1]
        )
        if still >= len(on_layer):
            return branch, edges2, hits
        bnd_new = _bounds_of(occupancy, edges2, branch.t, new_lays)
        hops: list[tuple[int, float, float, float]] = []
        for c in cuts:
            pt = occupancy.planter.point(
                int(edges2[c]),
                r_site,
                clearance,
                k,
                dyn,
                lo=bnd_new[c][0],
                hi=bnd_new[c][1],
                drill_mm=rule[3] if len(rule) > 3 else 0.0,
            )
            if pt is None:
                return branch, edges2, hits
            hops.append((c, pt[0], pt[1], pt[2]))
            if dyn is not None:
                dyn.add_disc(pt[0], pt[1], hh_r, net=ANY_NET)
        rebuilt, re2 = _build_branch(
            ctx,
            mesh,
            k,
            branch.pin_a,
            branch.pin_b,
            list(branch.tris),
            list(edges2),
            open_v,
            pin_vids,
            tuple(int(x) for x in new_lays),
            hops,
            rule[2],
            rule[3] if len(rule) > 3 else 0.0,
            bounds=bnd_new,
            chords=occupancy.chords,
            standoff_mm=rule[0] / 2.0 + rule[1] + FAB_QUANTUM_MM,
        )
        if not re2 or _runs_view_closed(rebuilt, open_v):
            return branch, edges2, hits
        hits2 = occupancy.crossings(rebuilt)
        rlays = rebuilt.layers
        on2 = [
            h
            for h in hits2
            if rlays and (h[1] < 0 or rlays[min(h[0], len(rlays) - 1)] == h[1])
        ]
        if len(on2) >= len(on_layer):
            return branch, edges2, hits
        branch, edges2, hits = rebuilt, re2, hits2


def _replant_branch(
    ctx: _Ctx,
    mesh: TopoMesh,
    d: TopoDesign,
    n_layers: int,
    k: int,
    rule: tuple[float, ...],
    occupancy: _Occupancy,
    masks: _NetMasks,
    open_v: np.ndarray,
    pin_vids: frozenset[int],
    cand_mask: int,
    branch: WirePath,
    edges2: tuple[int, ...],
    hits: list[tuple[int, int, int, int, int]],
    own_vias: Sequence[tuple[float, float, float]],
) -> tuple[WirePath, tuple[int, ...]] | None:
    """Re-site a branch's layer changes after one of its planted vias
    landed inside a sibling hop's drill reach (G2g): the same
    ``layer_dp`` ``_relayer_branch`` runs — ``prefer`` keeps the
    layering and the clean cuts — but the plant test reads a dynamic
    index extended with ``own_vias``, the net's committed hops (this
    branch's own vias are being re-sited, so they must NOT block — a
    kept hop's only candidate is often its own site), so a colliding
    hop re-sites a step ALONG its own edge: the collision poisons the
    collider's disc, not the edge — barring the whole edge stranded a
    mandatory hop whose only plantable edge held exactly one colliding
    point (x2 zero VBAT_UV). A cut's fresh site is folded back in
    before the next cut's ``point`` call, so two hops minted in one
    replant cannot coincide either. Returns the rebuilt
    ``(branch, edges)`` or ``None`` when no legal re-siting exists."""
    r_site = rule[2] / 2.0
    clearance = rule[1]
    dyn = occupancy.dyn_index(extra=own_vias)
    # the committed sibling hops must also hold the fab's same-net
    # hole-to-hole wall — ``dyn_index``'s discs carry barrel reach
    # only, which collapses under near-zero rules (see ``_grow_net``)
    drill_k = float(rule[3] if len(rule) > 3 and rule[3] else rule[2] * 0.5)
    hh_r = max(0.0, drill_k + JLC_VIA_HOLE_TO_HOLE_MM - r_site - clearance)
    for x, y, _r in own_vias:
        dyn.add_disc(x, y, hh_r, net=ANY_NET)
    bnd_old = _bounds_of(occupancy, edges2, branch.t, branch.layers)
    plantable = [
        occupancy.planter.point(
            int(e),
            r_site,
            clearance,
            k,
            dyn,
            lo=bnd_old[i][0],
            hi=bnd_old[i][1],
            drill_mm=rule[3] if len(rule) > 3 else 0.0,
        )
        is not None
        for i, e in enumerate(edges2)
    ]
    runs, cuts, err = layer_dp(
        [int(masks.open[int(e)]) for e in edges2],
        plantable,
        _pin_layers(d, branch.pin_a, n_layers, cand_mask),
        _pin_layers(d, branch.pin_b, n_layers, cand_mask),
        n_layers,
        pen=occupancy.pen(branch, hits),
        prefer=branch.layers,
    )
    if err is not None:
        return None
    new_lays = tuple(int(x) for x in _edge_layers(len(edges2), runs, cuts))
    bnd_new = _bounds_of(occupancy, edges2, branch.t, new_lays)
    hops: list[tuple[int, float, float, float]] = []
    for c in cuts:
        pt = occupancy.planter.point(
            int(edges2[c]),
            r_site,
            clearance,
            k,
            dyn,
            lo=bnd_new[c][0],
            hi=bnd_new[c][1],
            drill_mm=rule[3] if len(rule) > 3 else 0.0,
        )
        if pt is None:
            return None
        hops.append((c, pt[0], pt[1], pt[2]))
        dyn.add_disc(pt[0], pt[1], r_site, net=ANY_NET)
        dyn.add_disc(pt[0], pt[1], hh_r, net=ANY_NET)
    rebuilt, re2 = _build_branch(
        ctx,
        mesh,
        k,
        branch.pin_a,
        branch.pin_b,
        list(branch.tris),
        list(edges2),
        open_v,
        pin_vids,
        new_lays,
        hops,
        rule[2],
        rule[3] if len(rule) > 3 else 0.0,
        bounds=_bounds_of(occupancy, edges2, branch.t, new_lays),
        chords=occupancy.chords,
        standoff_mm=rule[0] / 2.0 + rule[1] + FAB_QUANTUM_MM,
    )
    if not re2 or _runs_view_closed(rebuilt, open_v):
        return None
    return rebuilt, re2


def _grow_net(
    ctx: _Ctx,
    mesh: TopoMesh,
    d: TopoDesign,
    n_layers: int,
    k: int,
    rule: tuple[float, ...],
    occ: np.ndarray,
    pin_vids: frozenset[int],
    occupancy: _Occupancy,
    view: _OccView,
) -> _NetAttempt:
    """One growth of net ``k`` under the view ``view``: its tree, every
    branch built per run around the via points the search planted
    (G2g). ``rule`` is the net's ``(trace_mm, clearance_mm, via_dia_mm
    [, via_drill_mm])``. The view's closed layers bind everything after
    the search too: the per-run shortcut and retrack read the reduced
    open mask on their own layer only, so a wall the search went round
    is not straightened back across. Two planted vias of one net closer
    than drill-to-drill clearance first try the replant DP — the
    committed sibling hops block their discs in the dynamic index,
    leaving the colliding hop free to re-site along its own edge —
    and only when no legal re-siting
    exists is the later via dropped (its vertex stays) and the branch
    lands on ``collisions`` for :attr:`Wires.via_collisions` to
    name."""
    found, comps, masks = _net_tree(
        ctx,
        mesh,
        d,
        k,
        n_layers,
        rule[0],
        rule[1],
        occ,
        via_dia_mm=rule[2],
        occ_view=view,
        planter=occupancy.planter,
        via_drill_mm=rule[3] if len(rule) > 3 else 0.0,
    )
    open_v = masks.open & ~view.closed
    clearance_k = rule[1]
    # Same-net via pairs owe the fab's drill-wall spacing — the emit
    # guard's JLC hole-to-hole rule — which the barrel+clearance term
    # in ``_colliding`` only covers while vias are full-sized: under
    # near-zero rules both shrink to ~0 and coincident hops slipped
    # through to be withdrawn downstream (x2 rev VBAT_UV/DEC3). The
    # rule tuple carries no drill, so estimate it at the house
    # proportion (drill ≈ dia/2 — JLC 0.6/0.3); where vias are
    # real-sized the barrel term still dominates and nothing changes.
    drill_k = float(rule[3] if len(rule) > 3 and rule[3] else rule[2] * 0.5)
    hh_need = drill_k + JLC_VIA_HOLE_TO_HOLE_MM
    hh_disc_r = max(
        0.0, drill_k + JLC_VIA_HOLE_TO_HOLE_MM - rule[2] / 2.0 - clearance_k
    )
    cand_mask = _cand_mask(_net_layers(d, k, n_layers), n_layers)
    built: list[_Built] = []
    cross: list[tuple[int, int, int, int, int]] = []
    collisions: list[tuple[int, int, int]] = []
    placed: list[tuple[float, float, float, int]] = []  # (x, y, r_site, branch)
    # the plant test's dynamic index extended with this net's own hops:
    # the search's ``view.dyn`` holds earlier NETS' vias only, so a
    # later branch could plant inside an earlier sibling hop's drill
    # reach — and the collision pass's drop left the layer change with
    # no via. The resolver and the replant DP read the extended index.
    dyn_own = occupancy.dyn_index()

    def _colliding(b: WirePath) -> set[int]:
        """``b``'s via indices closer than drill-to-drill clearance to
        an earlier via — the net's own ``placed`` plus ``b``'s own
        earlier ones, in via order. The reach is the larger of the
        barrel+clearance term and the fab's hole-to-hole wall: at
        near-zero rules the barrel term collapses and only the wall
        still separates two drills (``hh_need``)."""
        seen = [(x, y, r) for x, y, r, _bi in placed]
        out: set[int] = set()
        for vi, v in enumerate(b.vias):
            if any(
                math.dist((v.x, v.y), (px, py))
                < max(pr + v.r_site_mm + clearance_k, hh_need) + FAB_QUANTUM_MM
                for px, py, pr in seen
            ):
                out.add(vi)
            else:
                seen.append((v.x, v.y, v.r_site_mm))
        return out

    for pa, pb, tris, edges, lays_e, hops, bounds in found:
        branch, edges2 = _build_branch(
            ctx,
            mesh,
            k,
            pa,
            pb,
            tris,
            edges,
            open_v,
            pin_vids,
            lays_e,
            hops,
            rule[2],
            rule[3] if len(rule) > 3 else 0.0,
            bounds=bounds,
            # soft negotiated passes priced the crossings the search
            # took — the retrack must not reject them back to the
            # two-pointer corridor (td569540); hard passes unchanged
            chords=None if view.neg_soft else view.chords,
            standoff_mm=rule[0] / 2.0 + rule[1] + FAB_QUANTUM_MM,
        )
        if edges2:
            hits = occupancy.crossings(branch)
            # a same-layer taut crossing the search could not dodge:
            # re-layer the corridor around planted via points first —
            # cheaper than the wall loop's re-grow (G2g resolver)
            branch, edges2, hits = _relayer_branch(
                ctx,
                mesh,
                d,
                n_layers,
                k,
                rule,
                occupancy,
                dyn_own,
                masks,
                open_v,
                pin_vids,
                cand_mask,
                branch,
                edges2,
                hits,
            )
            # a hop the search minted before this net's own hops were
            # known can sit inside a sibling's drill reach: re-site the
            # layering through the DP with the net's committed hops
            # folded into the plant test — the colliding hop moves a
            # step along its own edge, the edge dies only when no
            # candidate clears the standing barrels. Dropping the via
            # leaves the layer change via-less.
            for _re in range(4):
                drop = _colliding(branch)
                if not drop:
                    break
                own_vias = [(x, y, r) for x, y, r, _bi in placed]
                got = _replant_branch(
                    ctx,
                    mesh,
                    d,
                    n_layers,
                    k,
                    rule,
                    occupancy,
                    masks,
                    open_v,
                    pin_vids,
                    cand_mask,
                    branch,
                    edges2,
                    hits,
                    own_vias,
                )
                if got is None:
                    break
                branch, edges2 = got
                hits = occupancy.crossings(branch)
            drop = _colliding(branch)
            if drop:
                collisions.append((k, branch.pin_a, branch.pin_b))
                branch = dataclasses.replace(
                    branch,
                    vias=tuple(v for vi, v in enumerate(branch.vias) if vi not in drop),
                )
            lays = branch.layers
            cross += [
                h for h in hits if h[1] < 0 or lays[min(h[0], len(lays) - 1)] == h[1]
            ]
        for v in branch.vias:
            placed.append((v.x, v.y, v.r_site_mm, len(built)))
            dyn_own.add_disc(v.x, v.y, v.r_site_mm, net=ANY_NET)
            # the same-net hole-to-hole wall as a second disc: the
            # barrel disc above collapses under near-zero rules, and
            # without this reach a sibling hop's replant re-sites onto
            # the standing drill (``hh_disc_r`` solves the index's
            # ``r_q + r_b + max(c_q, c_b)`` form for the pair rule)
            dyn_own.add_disc(v.x, v.y, hh_disc_r, net=ANY_NET)
        built.append(_Built(branch, edges2))
    return _NetAttempt(found, comps, masks, built, cross, collisions)


def _net_order(mesh: TopoMesh, d: TopoDesign, n_layers: int) -> list[int]:
    """The order the nets are routed in: short and forced nets first
    (gr520904) — a single-candidate-layer net and a short-spread net have
    the fewest alternative corridors, so they claim capacity before the
    free nets' occupancy term steers them elsewhere."""

    def key(k: int) -> tuple[bool, float, int]:
        cand = _net_layers(d, k, n_layers)
        pvs = [int(mesh.pin_vertex[p]) for p in d.net_pins[k]]
        span = (
            float(
                np.hypot(*(mesh.verts[pvs].max(axis=0) - mesh.verts[pvs].min(axis=0)))
            )
            if pvs
            else 0.0
        )
        return (len(cand) > 1, span, k)

    return sorted(range(len(d.net_pins)), key=key)


def _neg_touch(
    occupancy: _Occupancy,
    builts: Mapping[int, list[_Built]],
    keys: Mapping[int, tuple[int, int]],
    collisions: Sequence[tuple[int, int, int]],
) -> set[int]:
    """Last pass's nets that failed or still sit on an oversubscribed
    resource — the grow order's priority set (td569540): stranded,
    crossing or colliding nets first, then every net occupying a
    resource over capacity on ANY axis the search prices: an
    over-capacity ``(edge, layer)`` slot, a ``(tri, layer)`` past
    ``_NEG_TRI_CAP`` corridors, a crossed ``(tri, layer)`` chord
    resource."""
    coll = {c[0] for c in collisions}
    out: set[int] = set()
    for k, bl in builts.items():
        comps_n, cross_n = keys.get(k, (1, 0))
        if comps_n > 1 or cross_n or k in coll:
            out.add(k)
            continue
        hit = False
        for b in bl:
            br = b.branch
            lays = br.layers
            if lays is None or len(lays) != len(br.edges) + 1:
                # no per-edge layers: the corridor counts on every
                # layer, so an over-cap tri it crowds anywhere touches
                for t_ in br.tris:
                    if float(occupancy.tri_used[int(t_), :].max()) > _NEG_TRI_CAP:
                        hit = True
                        break
                continue
            for i, e in enumerate(br.edges):
                if (
                    occupancy.used[int(e), int(lays[i])]
                    > occupancy.cap[int(e), int(lays[i])] + 1e-9
                ):
                    hit = True
                    break
            if hit:
                break
            for i, t_ in enumerate(br.tris):
                if (
                    occupancy.cross_used[int(t_), int(lays[i])] > 0
                    or occupancy.tri_used[int(t_), int(lays[i])] > _NEG_TRI_CAP
                ):
                    hit = True
                    break
            if hit:
                break
        if hit:
            out.add(k)
    return out


def _neg_grow_set(
    touch: set[int],
    partners: Mapping[int, list[int]],
    *,
    final: bool,
    stall: bool,
) -> set[int] | None:
    """The next pass's grow set (td569540): the nets last pass left on
    a conflict — failed or holding an oversubscribed resource — plus
    the partners recorded against them; ``None`` re-grows EVERY net in
    order. The stall escape returns ``None`` (overuse flat two passes
    running: a clean net parked on a resource the conflicted ones need
    never yields to price alone) and so does a final pass entered with
    conflicts standing. An empty touch set returns ``None`` too — the
    detection missed something, so a full pass runs rather than stale
    prices."""
    if stall or (final and touch):
        return None
    grow = set(touch)
    for kk in touch:
        grow.update(partners.get(kk, ()))
    return grow or None


#: S2 (topo-route-shove): after the order loop, a shove phase —
#: rip-up with the failed net pinned, in bounded rounds over the
#: failed set. The budget is a COUNT of ``_grow_net`` calls, never
#: wall time, so the run stays deterministic (Reto 2026-11-03: ~20
#: per net; ``route_design`` ≤ 5 min at every order on the 2x board).
_SHOVE_ROUNDS = 2
_SHOVE_PARTNERS = 4
_SHOVE_GROWS = 20
#: The variant the phase runs — ``g`` the doc's greedy mechanism,
#: ``b`` a beam keeping the best ``_SHOVE_BEAM`` cell states across
#: rounds, ``m`` seeded multi-start keeping the best cell. Measured on
#: the g6 table at equal grow counts (Reto 2026-11-03: ship whichever
#: routes the most): m 400 > g 397 > b 378 — the seeded starts take it.
_SHOVE_VARIANT = "m"
_SHOVE_BEAM = 4
_SHOVE_SEEDS = 3
#: What a refused net's partial copper does during the phase (s1fix
#: review F1): ``True`` strips it from the cell when the net's tries
#: exhaust, so later grows price the freed channel instead of the
#: dead tree's ledger — permanently: the freed channel can be
#: re-occupied by a partner's re-grow, so the stale tree is never
#: emitted either. ``False`` keeps it. Measured both on the m table —
#: round 4 drop 403 vs keep 398 real strict-clean summed over the 5 orders,
#: never behind on any board (Reto 2026-11-03: ship the higher real
#: sum, tie → drop).
_SHOVE_DROP_PARTIAL = True


def _wire_stats(
    fail: int, cross: int, built: Iterable[_Built]
) -> tuple[int, int, int, float]:
    """One net's line in the swap accept rule (S2): failing flag,
    same-layer taut crossings, planted vias, taut length. Summed over
    a try's {k} ∪ P the tuples compare lexicographically — fewer
    failures wins, a tie needs the rest strictly lower (Reto's
    not-worse rule with tie-breaks, 2026-11-03)."""
    return (
        fail,
        cross,
        sum(len(b.branch.vias) for b in built),
        sum(b.branch.length_mm for b in built),
    )


def _witness_crossings(
    occupancy: _Occupancy, built: Iterable[_Built]
) -> list[tuple[int, int, int, int, int]]:
    """Same-layer crossings whose physical repair the realiser refuses.

    Use the same result for acceptance, committed keys and partner discovery;
    the growth oracle's 2D bigon cancellation is only a search heuristic.
    """
    return [
        h
        for b in built
        for h in occupancy.crossings(b.branch, witness=True)
        if occupancy.lines[h[2]][0].net != b.branch.net
        and (
            h[1] < 0
            or not b.branch.layers
            or b.branch.layers[min(h[0], len(b.branch.layers) - 1)] == h[1]
        )
    ]


def _wit_stats(occupancy: _Occupancy, att: _NetAttempt) -> tuple[int, int, int, float]:
    """Attempt status under the physical witness oracle (s1fix review F4):
    the plain 2D oracle cancels empty bigons whose arc swap a planted via,
    a layer change or a foreign pad makes
    unemittable — an attempt carrying one counts as failing, not clean,
    in the accept rule. Dropped same-net via joins also strand the plan,
    even when the geometric tree still has one component."""
    cross = len(_witness_crossings(occupancy, att.built))
    return _wire_stats(
        1 if len(att.comps) > 1 or cross or att.collisions else 0, cross, att.built
    )


def _affected_nets(
    before: _Occupancy, after: _Occupancy, changed: set[int]
) -> set[int]:
    """Nets touched by the old or new geometry of the changed nets.

    Untouched copper and its connectivity stay fixed. Only a branch meeting
    changed copper can gain or lose a crossing, including a cancelled bigon.
    """
    touched = set(changed)
    for occ in (before, after):
        for b, line, _geom, _layers in occ.lines:
            if b.net in changed:
                touched.update(occ.lines[j][0].net for j in occ.nearby(line))
    return touched


def _foreign_crossing_set(
    occupancy: _Occupancy, built: Iterable[_Built]
) -> set[tuple[WirePath, int, int, WirePath, int, int]]:
    """Physical foreign crossings, independent of ledger insertion indices.

    Include both branches and event spans: another crossing with an existing
    partner is still new harm, even if the total crossing count stays equal.
    """
    return {
        (b.branch, i, ly, occupancy.lines[j][0], lo, hi)
        for b in built
        for i, ly, j, lo, hi in _witness_crossings(occupancy, [b])
    }


def _preserves_other_nets(
    before: _Occupancy,
    after: _Occupancy,
    builts: Mapping[int, list[_Built]],
    changed: set[int],
) -> bool:
    """A failing partner cannot acquire a new short with untouched copper.

    Existing crossings belong to the later net's failure record (S1). Compare
    both physical states so an untouched net cannot gain ANY foreign crossing,
    even when it already fails. Its branches and connectivity stay unchanged.
    """
    return all(
        _foreign_crossing_set(after, builts[n])
        <= _foreign_crossing_set(before, builts[n])
        for n in sorted(_affected_nets(before, after, changed) - changed)
        if n in builts
    )


def _key_stats(
    keys: Mapping[int, tuple[int, int]], builts: Mapping[int, list[_Built]], k: int
) -> tuple[int, int, int, float]:
    """The committed cell's line for net ``k`` — the before side of
    the accept rule, off the recorded ``(comps, cross)`` key."""
    comps_n, cross_n = keys.get(k, (1, 0))
    return _wire_stats(1 if comps_n > 1 or cross_n else 0, cross_n, builts.get(k, ()))


def _sum_stats(
    rows: Iterable[tuple[int, int, int, float]],
) -> tuple[int, int, int, float]:
    f = c = v = 0
    length = 0.0
    for fi, ci, vi, li in rows:
        f += fi
        c += ci
        v += vi
        length += li
    return (f, c, v, length)


def _accept_stats(
    before: tuple[int, int, int, float],
    after: tuple[int, int, int, float],
    before_fail: frozenset[int] | set[int],
    after_fail: frozenset[int] | set[int],
) -> bool:
    """The unified accept rule — G2h's one-shot negotiation and S2's
    pinned swap alike: the failing set over the swapped nets may only
    SHRINK, so a swap that just moves the failure to another net is
    refused; on an equal set the rest — crossings, vias, length —
    must be strictly lower (Reto's not-worse rule, 2026-11-03)."""
    return after_fail <= before_fail and (after_fail != before_fail or after < before)


def _unreachable_rows(
    ctx: _Ctx,
    mesh: TopoMesh,
    d: TopoDesign,
    k: int,
    n_layers: int,
    comps: Sequence[Sequence[int]],
    masks: _NetMasks,
    standoff_mm: float,
    blocked_by: tuple[int, ...] = (),
) -> list[Unreachable]:
    """One :class:`Unreachable` per stranded component of a failed
    attempt — the naming the order loop and the shove phase share
    (S2): regions flooded on the attempt's own passability, the
    owners of the cut between a comp and the main component, the
    layered flag where any-layer reach exists but no plantable hop."""
    pins_l = [int(p) for p in d.net_pins[k]]
    regions = [
        _flood_region(ctx, mesh, [pins_l[j] for j in comp], masks.base_pass)
        for comp in comps
    ]
    main_i = max(range(len(comps)), key=lambda ci: (len(regions[ci]), len(comps[ci])))
    main_region = regions[main_i]
    own = frozenset(pins_l)
    net_name = str(d.net_name[k]) if k < len(d.net_name) else str(k)
    cand_mask = _cand_mask(_net_layers(d, k, n_layers), n_layers)
    out: list[Unreachable] = []
    for ci, comp in enumerate(comps):
        if ci == main_i:
            continue
        owners = _wall_owners(
            ctx,
            mesh,
            d,
            own,
            regions[ci],
            main_region,
            cand_mask,
            net_name,
            standoff_mm,
        )
        out.append(
            Unreachable(
                net=k,
                pin=pins_l[comp[0]],
                owners=owners,
                pins=tuple(pins_l[j] for j in comp),
                # the any-layer flood meets the main component: a path
                # exists, only the layered search — no plantable hop —
                # could not take it (G2g)
                layered=bool(regions[ci] & main_region),
                blocked_by=blocked_by,
            )
        )
    return out


@dataclass(slots=True)
class _ShoveState:
    """``build_wires``' per-net bookkeeping as one swappable cell
    (S2): an accepted try replaces it wholesale; the beam and
    multi-start variants hold several side by side. All mutation is
    copy-and-replace — a snapshot stays valid for a later start."""

    builts: dict[int, list[_Built]]
    keys: dict[int, tuple[int, int]]
    used: np.ndarray
    occupancy: _Occupancy
    collisions: list[tuple[int, int, int]]
    unreachable: list[Unreachable]
    extra: dict[int, list[tuple[int, list[int]]]]
    negotiated: list[tuple[int, int, bool]]
    #: The failed net's recorded trigger partners — the chord
    #: trigger's crossing nets or the closure trigger's cut owners —
    #: for the shove's tries when the probe names none fresh.
    partners: dict[int, list[int]]

    def fails(self, k: int) -> bool:
        comps_n, cross_n = self.keys.get(k, (1, 0))
        return comps_n > 1 or cross_n > 0 or any(c[0] == k for c in self.collisions)

    def stats(self, k: int) -> tuple[int, int, int, float]:
        row = _key_stats(self.keys, self.builts, k)
        return (int(self.fails(k)), *row[1:])

    def metric(self) -> tuple[int, int, int, float]:
        """The cell's standing under the accept rule — the beam's
        keep and the multi-start's pick."""
        return _sum_stats(self.stats(k) for k in self.keys)


class _Shove:
    """S2's shove phase: ``{k} ∪ P`` re-grown with the failed net
    pinned (the doc's mechanism), every ``_grow_net`` call counted
    against ``_SHOVE_GROWS × n_nets``. One code path serves the greedy
    rounds, the beam and the seeded starts."""

    def __init__(
        self,
        ctx: _Ctx,
        mesh: TopoMesh,
        d: TopoDesign,
        n_layers: int,
        rules: Sequence[tuple[float, ...]],
        pin_vids: frozenset[int],
        corridor: _Corridor,
        order: Sequence[int],
        *,
        net_clearance_by_name: Mapping[str, float] | None = None,
        masks: BoardMasks | None = None,
    ) -> None:
        from precis.pcb.rules import NetRules

        self.ctx = ctx
        self.mesh = mesh
        self.d = d
        self.n_layers = n_layers
        self.rules = rules
        self.masks = masks
        self.net_rules = {
            (str(d.net_name[k]) if k < len(d.net_name) else str(k)): NetRules(
                track_width_mm=r[0],
                clearance_mm=r[1],
                via_dia_mm=r[2] if len(r) > 2 else None,
                via_drill_mm=r[3] if len(r) > 3 else None,
            )
            for k, r in enumerate(rules)
        }
        for name, clearance in (net_clearance_by_name or {}).items():
            if name not in self.net_rules:
                self.net_rules[name] = NetRules(
                    track_width_mm=_DEFAULT_TRACE_MM, clearance_mm=clearance
                )
        # The guard reads the full IR table for pad identities, while branches
        # use the routed subset. TopoDesign retains both tables.
        # Hand-built snapshots can omit names. Use the same index fallback
        # as the branch rules so their pad ownership still reaches the guard.
        names = d.ir_net_name or tuple(
            str(d.net_name[k]) if k < len(d.net_name) else str(k)
            for k in range(len(d.net_pins))
        )
        self.ir = types.SimpleNamespace(net_name=names)
        self.pin_vids = pin_vids
        self.corridor = corridor
        self.order = list(order)
        self.order_pos = {k: i for i, k in enumerate(order)}
        self.grows = 0
        self.budget = _SHOVE_GROWS * len(d.net_pins)
        self.round_yield: list[tuple[int, ...]] = []
        self.exhaustive_noop = False

    def _grow(
        self,
        k: int,
        occupancy: _Occupancy,
        branches: list[WirePath],
        budget: int,
        *,
        probe: bool = False,
    ) -> _NetAttempt | None:
        """One counted grow of ``k`` against ``occupancy`` — ``None``
        when the budget is spent; the count, never the clock, is the
        bound, so a spent try just reads as refused. ``probe`` drops
        the chord/split legality exactly like the order loop's."""
        if self.grows >= budget:
            return None
        used = self.corridor.used_of(branches)
        view = occupancy.view(k)
        if probe:
            view = dataclasses.replace(view, chords=None, splits=None)
        att = _grow_net(
            self.ctx,
            self.mesh,
            self.d,
            self.n_layers,
            k,
            self.rules[k],
            self.corridor.occ(used),
            self.pin_vids,
            occupancy,
            view,
        )
        self.grows += 1
        return att

    def _ranked(
        self, occupancy: _Occupancy, cross: Sequence[tuple[int, int, int, int, int]]
    ) -> list[int]:
        """The crossing partners, most-crossed first, ties by net
        index (the doc's ``most_common`` tie-break)."""
        cnt = Counter(int(occupancy.lines[j][0].net) for _i, _lo, j, _s0, _s1 in cross)
        return [n for n, _c in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))]

    def _apply(
        self,
        st: _ShoveState,
        k: int,
        P: tuple[int, ...],
        atts: dict[int, _NetAttempt],
        occ2: _Occupancy,
        branches: list[WirePath],
    ) -> _ShoveState:
        """The accepted cell: {k} ∪ P's builts, keys, occupancy,
        collisions, unreachable rows and cut demand replaced
        wholesale; ``negotiated`` gains one kept row per partner."""
        S = {k, *P}
        builts = dict(st.builts)
        keys = dict(st.keys)
        partners = dict(st.partners)
        for n, att in atts.items():
            builts[n] = att.built
        affected = _affected_nets(st.occupancy, occ2, S)
        for n in sorted(affected):
            cross = _witness_crossings(occ2, builts.get(n, ()))
            comps = len(atts[n].comps) if n in atts else keys.get(n, (1, 0))[0]
            keys[n] = (comps, len(cross))
            if comps == 1 and not cross:
                partners.pop(n, None)
            elif cross:
                partners[n] = self._ranked(occ2, cross)
        collisions = [c for c in st.collisions if c[0] not in S]
        for att in atts.values():
            collisions += att.collisions
        unreachable = [u for u in st.unreachable if u.net not in S]
        for n in sorted(S, key=self.order_pos.__getitem__):
            att = atts[n]
            if len(att.comps) > 1:
                blocked_by = tuple(partners.get(n) or st.partners.get(n, []))
                unreachable += _unreachable_rows(
                    self.ctx,
                    self.mesh,
                    self.d,
                    n,
                    self.n_layers,
                    att.comps,
                    att.masks,
                    self.rules[n][0] / 2.0 + self.rules[n][1] + FAB_QUANTUM_MM,
                    blocked_by,
                )
                partners.setdefault(n, list(blocked_by))
        extra = {
            e: [(kk, lys) for kk, lys in rows if kk not in S]
            for e, rows in st.extra.items()
        }
        extra = {e: rows for e, rows in extra.items() if rows}
        return _ShoveState(
            builts=builts,
            keys=keys,
            used=self.corridor.used_of(branches),
            occupancy=occ2,
            collisions=collisions,
            unreachable=unreachable,
            extra=extra,
            negotiated=[*st.negotiated, *((k, p, True) for p in P)],
            partners=partners,
        )

    def _swap(
        self,
        st: _ShoveState,
        k: int,
        P: tuple[int, ...],
        kept_k: list[tuple[int, _Built]],
        budget: int,
    ) -> _ShoveState | None:
        """One pinned swap: ``k`` grown first into the cell minus
        {k} ∪ P — the pinned net never carries a crossing — then each
        partner in its original order, ``None`` when the unified
        accept rule refuses it."""
        S = {k, *P}
        kept = [(kk, b) for kk, b in kept_k if kk not in S]
        occ2 = st.occupancy.rebuild(kept)
        branches = [b.branch for _kk, b in kept]
        att_k = self._grow(k, occ2, branches, budget)
        if att_k is None or _wit_stats(occ2, att_k)[0]:
            return None
        for b in att_k.built:
            occ2.add(k, b)
        branches += [b.branch for b in att_k.built]
        atts = {k: att_k}
        for p in sorted(P, key=self.order_pos.__getitem__):
            att_p = self._grow(p, occ2, branches, budget)
            if att_p is None:
                return None
            atts[p] = att_p
            for b in att_p.built:
                occ2.add(p, b)
            branches += [b.branch for b in att_p.built]
        # F4: the pinned net and the partners alike are judged on the
        # physical witness — a committed wire whose only crossing is an
        # unrepairable bigon is a failure, never clean copper.
        k_stats = _wit_stats(occ2, att_k)
        if k_stats[1]:
            return None
        # the witness stats once per attempt — each costs one
        # crossings(witness=True) pass per built branch
        wits = {n: k_stats if n == k else _wit_stats(occ2, atts[n]) for n in S}
        if not _accept_stats(
            _sum_stats(st.stats(n) for n in S),
            _sum_stats(wits[n] for n in S),
            {n for n in S if st.fails(n)},
            {n for n in S if wits[n][0]},
        ):
            return None
        if not _preserves_other_nets(st.occupancy, occ2, st.builts, S):
            return None
        candidate = self._apply(st, k, P, atts, occ2, branches)
        if not self._preserves_guard(st, candidate, S):
            return None
        return candidate

    def _preserves_guard(
        self,
        st: _ShoveState,
        candidate: _ShoveState,
        changed: set[int],
    ) -> bool:
        """A mutation must survive the emitter's actual clearance predicate.

        Changed nets must all clear it; untouched nets may retain an existing
        guard failure but cannot lose another branch. Shaping reads the full copper
        set, since removing distant pieces can change its decisions. The guard
        already computes all marks; keep them all, including a nearby barrel
        whose centreline never intersects the changed track.
        """
        branches = [b.branch for bl in candidate.builts.values() for b in bl]
        affected = set(st.builts) | set(candidate.builts) | changed
        after = self._guarded(candidate, branches, affected)
        if after.keys() & changed:
            return False
        others = set(after) - changed
        if not others:
            return True
        before = [b.branch for bl in st.builts.values() for b in bl]
        previous = self._guarded(st, before, others)
        return all(after[n] <= previous.get(n, set()) for n in others)

    def _guarded(
        self, st: _ShoveState, branches: list[WirePath], focus: set[int]
    ) -> dict[int, set[WirePath]]:
        """Final branch withdrawals, keyed by concrete descendant identity.

        Plan indices shift when another net is withdrawn or regrown. Keeping
        each descendant distinct detects a second withdrawal even when both
        share all input ancestors. An unchanged descendant can acquire more
        ancestors without becoming a new offence. A branch dropped for a
        crossing or missing via never reaches the guard; preserve it too.
        """
        from precis.pcb.topoplace.realise import _guarded_branches

        plan = self._plan(st, branches, shaped=True)
        identities = plan.branch_ids or tuple(branches)

        def identity(i: int) -> WirePath:
            branch = identities[i]
            # Shove plans are freshly planned, never hand-built multipass
            # copies with unknown identity rows.
            assert branch is not None
            return branch

        guarded = {
            n: {identity(i) for i in indices}
            for n, indices in _guarded_branches(
                plan,
                self.d,
                self.ir,
                self.n_layers,
                np.asarray(
                    st.occupancy.pose if st.occupancy.pose is not None else self.d.pose0
                ),
                self.net_rules,
            ).items()
            if n in focus
        }
        for i in set(plan.no_via) | {i for pair in plan.crossings for i in pair}:
            branch = identity(i)
            if branch.net in focus:
                guarded.setdefault(branch.net, set()).add(branch)
        return guarded

    def _tries(self, st: _ShoveState, k: int, budget: int) -> Iterable[_ShoveState]:
        """Candidate cells for failed net ``k``, in try order: a clean
        re-grow under the current occupancy (room the earlier accepts
        already made), then the pinned sets {p1}, {p1, p2}, {p1, p2,
        p3}, {p1, .., p4} — a set with no new partner is skipped.
        Yields only the accepted ones."""
        kept_k = [(kk, b) for kk, bl in st.builts.items() if kk != k for b in bl]
        branches_k = [b.branch for _kk, b in kept_k]
        occ_k = st.occupancy.rebuild(kept_k)
        att = self._grow(k, occ_k, branches_k, budget)
        if att is None:
            return
        if not _wit_stats(occ_k, att)[0]:
            # F4: the clean re-grow is judged on the physical witness
            # too — a path whose only crossing is an unrepairable
            # bigon is refused here, never committed (crossings()
            # excludes same-net lines, so the check reads the same
            # before att's copper joins occ_k).
            for b in att.built:
                occ_k.add(k, b)
            branches = [*branches_k, *(b.branch for b in att.built)]
            if _preserves_other_nets(st.occupancy, occ_k, st.builts, {k}):
                candidate = self._apply(
                    st,
                    k,
                    (),
                    {k: att},
                    occ_k,
                    branches,
                )
                if self._preserves_guard(st, candidate, {k}):
                    yield candidate
                    return
            # refused: fall through — the probe may still name swap
            # partners that clear the bigon.
            occ_k = st.occupancy.rebuild(kept_k)
        probe = self._grow(k, occ_k, branches_k, budget, probe=True)
        if probe is None:
            return
        cross = _witness_crossings(occ_k, probe.built)
        partners = self._ranked(occ_k, cross) or list(st.partners.get(k, ()))
        prev = 0
        for size in range(1, _SHOVE_PARTNERS + 1):
            P = partners[:size]
            if len(P) <= prev:
                break
            prev = len(P)
            new = self._swap(st, k, tuple(P), kept_k, budget)
            if new is not None:
                yield new

    def _strip(self, st: _ShoveState, k: int) -> _ShoveState:
        """F1's drop: a refused net's partial copper leaves the cell —
        later grows price the freed channel instead of the dead
        tree's ledger. The drop is permanent, not ledger-only: the
        channel it frees may be re-occupied by a partner's re-grow,
        so emitting the stale tree at the end could stack a via onto
        copper planted after the strip (the sort tie in
        ``realise._spoke_offsets`` is where it surfaced first). The
        fail mark stays in ``keys`` — the net still counts failing,
        its Unreachable rows and cut demand still named — so a later
        round can retry it on the freed board. Only the shipped
        multi-start path (``run_greedy``) strips. Withdrawal can change the
        surviving copper's shaping, so it owes the same preservation check
        as a swap; a refusal returns the original snapshot.
        """
        kept = [(kk, b) for kk, bl in st.builts.items() if kk != k for b in bl]
        builts = dict(st.builts)
        builts[k] = []
        # The entire net was withdrawn, including pins in its formerly
        # connected component. A crossing-only failure had no stranded row.
        # Keep a certificate even when no copper remains for realise to see.
        old_rows = [u for u in st.unreachable if u.net == k]
        pins = tuple(int(p) for p in self.d.net_pins[k])
        row = Unreachable(
            net=k,
            pin=old_rows[0].pin if old_rows else pins[0],
            pins=pins,
            owners=tuple(sorted({o for u in old_rows for o in u.owners})),
            layered=bool(st.keys[k][1]) or any(u.layered for u in old_rows),
            blocked_by=tuple(st.partners.get(k, ()))
            or tuple(dict.fromkeys(p for u in old_rows for p in u.blocked_by)),
        )
        occ2 = st.occupancy.rebuild(kept)
        keys = dict(st.keys)
        partners = dict(st.partners)
        for n in sorted(_affected_nets(st.occupancy, occ2, {k}) - {k}):
            cross = _witness_crossings(occ2, builts.get(n, ()))
            keys[n] = (keys.get(n, (1, 0))[0], len(cross))
            if cross:
                partners[n] = self._ranked(occ2, cross)
            elif keys[n][0] == 1:
                partners.pop(n, None)
        # Copper withdrawal must remain a failure even after fresh scoring.
        keys[k] = (max(2, keys[k][0]), keys[k][1])
        candidate = _ShoveState(
            builts=builts,
            keys=keys,
            used=self.corridor.used_of(b.branch for _kk, b in kept),
            occupancy=occ2,
            collisions=[c for c in st.collisions if c[0] != k],
            unreachable=[u for u in st.unreachable if u.net != k] + [row],
            extra=st.extra,
            negotiated=st.negotiated,
            partners=partners,
        )
        if not _preserves_other_nets(st.occupancy, occ2, st.builts, {k}):
            return st
        return candidate if self._preserves_guard(st, candidate, {k}) else st

    def run_greedy(
        self,
        st0: _ShoveState,
        order: Sequence[int] | None = None,
        budget: int | None = None,
    ) -> tuple[_ShoveState, list[int]]:
        """The doc's mechanism: rounds over the still-failing set in
        net order (a seeded order under multi-start), the first
        accepted try per net kept; a round with no accept — or the
        spent budget — ends the phase."""
        st = st0
        budget = self.budget if budget is None else budget
        order = self.order if order is None else order
        yields: list[int] = []
        self.exhaustive_noop = False
        tried: set[int] = set()
        stripped = False
        initial_failed = {k for k in order if st0.fails(k)}
        for _rnd in range(_SHOVE_ROUNDS):
            if self.grows >= budget:
                break
            accepted = 0
            for k in [k for k in order if st.fails(k)]:
                if self.grows >= budget:
                    break
                if not st.fails(k):
                    continue
                grows_before = self.grows
                for new in self._tries(st, k, budget):
                    st = new
                    accepted += 1
                    break
                else:
                    # At the boundary _tries may have stopped mid-partner set.
                    # Conservatively retain shuffled starts in that case.
                    if self.grows < budget or self.grows == grows_before:
                        tried.add(k)
                    if _SHOVE_DROP_PARTIAL and st.fails(k) and st.builts.get(k):
                        new = self._strip(st, k)
                        stripped |= new is not st
                        st = new
            yields.append(accepted)
            if not accepted:
                break
        self.exhaustive_noop = (
            not any(yields) and not stripped and initial_failed <= tried
        )
        return st, yields

    def run_beam(
        self, st0: _ShoveState, budget: int | None = None
    ) -> tuple[_ShoveState, list[int]]:
        """Variant (b): the best ``_SHOVE_BEAM`` cell states kept
        across rounds — every live state's next untried failed net
        expands into its accepted tries plus the skip, the pool
        re-sorted to K by the cell metric each step."""
        budget = self.budget if budget is None else budget
        # (state, still-untried failed nets this round, accepted-yet flag)
        beam: list[tuple[_ShoveState, list[int], bool]] = [
            (st0, [k for k in self.order if st0.fails(k)], False)
        ]
        yields: list[int] = []
        best = st0
        for _rnd in range(_SHOVE_ROUNDS):
            if self.grows >= budget or not beam:
                break
            accepted = 0
            while self.grows < budget and any(q for _s, q, _a in beam):
                cand: list[tuple[_ShoveState, list[int], bool]] = []
                for st, queue, acc in beam:
                    if not queue:
                        cand.append((st, queue, acc))
                        continue
                    k, rest = queue[0], queue[1:]
                    cand.append((st, rest, acc))
                    if st.fails(k):
                        news = list(self._tries(st, k, budget))
                        accepted += len(news)
                        cand.extend((new, rest, True) for new in news)
                cand.sort(key=lambda e: e[0].metric())
                beam = cand[:_SHOVE_BEAM]
            yields.append(accepted)
            for st, _q, _acc in beam:
                if st.metric() < best.metric():
                    best = st
            beam = [
                (st, [k for k in self.order if st.fails(k)], False)
                for st, _q, acc in beam
                if acc
            ]
        return best, yields

    def run_multi(
        self, st0: _ShoveState, budget: int | None = None
    ) -> tuple[_ShoveState, list[tuple[int, ...]]]:
        """Variant (m): the greedy phase re-run from the post-loop
        cell under the net order and ``_SHOVE_SEEDS`` shuffles of it —
        each start an equal share of the budget — the best final cell
        under failure-set inclusion stays. A strict subset wins, equal sets
        compare crossings/vias/length, and incomparable sets retain the earlier
        incumbent (as does an exact tie)."""
        budget = self.budget if budget is None else budget
        share = max(1, budget // (_SHOVE_SEEDS + 1))
        best = st0
        best_metric = self._plan_metric(st0)
        yields: list[tuple[int, ...]] = []
        for s in range(_SHOVE_SEEDS + 1):
            order = (
                self.order
                if s == 0
                else random.Random(s).sample(self.order, len(self.order))
            )
            st, y = self.run_greedy(st0, order, min(budget, self.grows + share))
            yields.append(tuple(y))
            metric = self._plan_metric(st)
            if metric[0] <= best_metric[0] and (
                metric[0] != best_metric[0] or metric[1:] < best_metric[1:]
            ):
                best = st
                best_metric = metric
            if s == 0 and self.exhaustive_noop:
                break
        return best, yields

    def _plan_metric(self, st: _ShoveState) -> tuple[frozenset[int], int, int, float]:
        """Score the actual plan failures, never a snapshot of crossing keys.

        The final layer planner can resolve a crossing or discover a missing
        via. Compare its failure set before the secondary copper costs, using
        exactly the rules and pose with which the branches were grown.
        """
        from precis.pcb.topoplace.realise import _plan_failed_nets

        branches = [b.branch for bl in st.builts.values() for b in bl]
        plan = self._plan(st, branches)
        return (
            frozenset(_plan_failed_nets(plan)),
            len(plan.crossings),
            len(plan.vias),
            sum(b.length_mm for b in branches),
        )

    def _plan(
        self, st: _ShoveState, branches: list[WirePath], *, shaped: bool = False
    ) -> RoutePlan:
        """Plan a cell with its growth rules, optionally through emit shaping."""
        from precis.pcb.topoplace.realise import (
            _emit_shapes,
            _spoke_offsets,
            plan_route,
        )

        order, order_pos, residual = _order(self.ctx, self.mesh, tuple(branches))
        wires = Wires(
            branches=tuple(branches),
            order=order,
            order_pos=order_pos,
            order_residual=residual,
            capacity_mm=self.corridor.capacity_mm,
            gap_free_mm=self.corridor.gap_free,
            gap_cap=self.corridor.gap_cap,
            net_layers=tuple(
                _net_layers(self.d, k, self.n_layers)
                for k in range(len(self.d.net_pins))
            ),
            unreachable=tuple(st.unreachable),
            via_collisions=tuple(st.collisions),
            overflow=self.corridor.cut(branches, order, extra=st.extra),
        )
        paths = (
            _spoke_offsets(
                self.d,
                self.mesh,
                wires,
                self.n_layers,
                net_rules=self.net_rules,
                pose=st.occupancy.pose,
            )
            if shaped
            else None
        )
        plan = plan_route(
            self.d,
            self.mesh,
            wires,
            self.n_layers,
            net_rules=self.net_rules,
            pose=st.occupancy.pose,
            paths=paths,
            masks=self.masks,
        )
        if shaped:
            bad = {b for pair in plan.crossings for b in pair}
            plan = _emit_shapes(
                plan,
                self.d,
                self.ir,
                self.n_layers,
                np.asarray(
                    st.occupancy.pose if st.occupancy.pose is not None else self.d.pose0
                ),
                self.net_rules,
                skip=bad,
            )
            if bad:
                plan = dataclasses.replace(
                    plan,
                    pieces=tuple(p for p in plan.pieces if p.branch not in bad),
                    vias=tuple(v for v in plan.vias if v.branch not in bad),
                )
        return plan

    def run(self, st: _ShoveState) -> _ShoveState:
        """The phase, dispatched on ``_SHOVE_VARIANT``; a no-op when
        nothing fails (the near-zero F = ∅ rows)."""
        if not any(st.fails(k) for k in st.keys):
            return st
        if _SHOVE_VARIANT == "b":
            st, rounds = self.run_beam(st)
            self.round_yield = [tuple(rounds)]
        elif _SHOVE_VARIANT == "m":
            st, self.round_yield = self.run_multi(st)
        else:
            st, rounds = self.run_greedy(st)
            self.round_yield = [tuple(rounds)]
        return st


def build_wires(
    mesh: TopoMesh,
    d: TopoDesign,
    n_layers: int,
    net_rules: tuple[tuple[float, ...], ...] | None = None,
    pose: np.ndarray | None = None,
    *,
    shove: bool = True,
    net_clearance_by_name: Mapping[str, float] | None = None,
    masks: BoardMasks | None = None,
    negotiate: int = _NEGOTIATE_ITERS,
) -> Wires:
    """Every net's wire tree on the mesh — spec §2's wires. Returns a
    :class:`Wires`; a component of a net no path can link to the net's
    main component on any candidate layer is wired internally and
    named once on :attr:`Wires.unreachable` for the colour pass to
    report. ``shove=False`` skips the S2 rescue phase — the rate test
    guards the router's wire rate, not the shove's (s2 round-3
    ruling). ``net_rules`` is ``(trace_mm, clearance_mm[, via_dia_mm])``
    per net (resolved ``NetRules`` by the caller when the board has
    classes); ``None`` uses the default pair, under which foreign
    pad-to-pad gaps price open or closed by geometry (Reto 2026-10-25),
    and the default via. ``pose`` is the
    pose the mesh was built at (defaults ``d.pose0``): the corridor
    capacity reads each endpoint pad's extent through it, so a caller
    routing a placed pose passes it — the authored pose prices a
    rotated or flipped pad's shadow wrong (gr520904 follow-up).
    ``net_clearance_by_name`` also covers frozen-copper nets without a
    routed tree (a plane or singleton); their rules cannot be recovered
    from the per-routed-net tuples. ``masks`` carries the final planner's
    crossing-resolution tie-breaks into shove validation and selection.
    ``negotiate`` is the negotiated-congestion pass count (td569540):
    ``1`` is the single hard pass; more runs soft priced passes before
    the final hard one and emits the best ledger. Above
    ``_NEG_ITERS_MAX`` it refuses — the 1.4x present-factor ramp is
    float-saturated long before that many passes."""
    if int(negotiate) > _NEG_ITERS_MAX:
        raise ValueError(
            f"negotiate={negotiate} exceeds _NEG_ITERS_MAX={_NEG_ITERS_MAX}: "
            "the present-factor ramp is float-saturated long before that "
            "many passes — clamp the count"
        )
    pose_a = np.asarray(d.pose0 if pose is None else pose)
    ctx = _make_ctx(mesh, d, n_layers, pose_a)
    pin_vids = frozenset(int(v) for v in mesh.pin_vertex)
    n_e = len(mesh.edges)
    branches: list[WirePath] = []
    net_layers: list[frozenset[int]] = [frozenset() for _ in d.net_pins]
    unreachable: list[Unreachable] = []
    given = [
        tuple(net_rules[k])
        if net_rules is not None and k < len(net_rules)
        else (_DEFAULT_TRACE_MM, _DEFAULT_CLEAR_MM)
        for k in range(len(d.net_pins))
    ]
    rules_used: list[tuple[float, float]] = [(r[0], r[1]) for r in given]
    via_dia: list[float] = [r[2] if len(r) > 2 else _DEFAULT_VIA_MM for r in given]
    via_drill: list[float] = [r[3] if len(r) > 3 else 0.0 for r in given]
    collisions: list[tuple[int, int, int]] = []
    # slice-4 channel record at the finest resolved rule — the most
    # slack a channel ever shows; coarser nets just see less free.
    # Priced BEFORE routing (gr520904): it feeds both the A* occupancy
    # term and the demand-vs-capacity cut on the routed wires.
    corridor = _corridor(
        ctx, mesh, d, n_layers, rules_used, d.pose0 if pose is None else pose
    )
    gap_free = corridor.gap_free
    gap_cap = corridor.gap_cap
    capacity_mm = corridor.capacity_mm
    used = np.zeros(n_e)  # occupied finest-rule slots per edge

    occupancy = _Occupancy(
        corridor,
        mesh,
        n_layers,
        pose_a,
        net_clearance=tuple(c for _t, c in rules_used),
        net_clearance_by_name=net_clearance_by_name,
    )
    negotiated: list[tuple[int, int, bool]] = []
    builts: dict[int, list[_Built]] = {}
    keys: dict[int, tuple[int, int]] = {}
    extra: dict[int, list[tuple[int, list[int]]]] = {}
    partners_of: dict[int, list[int]] = {}
    route_order = _net_order(mesh, d, n_layers)
    rules_full = [
        (*rules_used[k], via_dia[k], via_drill[k]) for k in range(len(d.net_pins))
    ]
    shv = _Shove(
        ctx,
        mesh,
        d,
        n_layers,
        rules_full,
        pin_vids,
        corridor,
        route_order,
        net_clearance_by_name=net_clearance_by_name,
        masks=masks,
    )

    def preserves_retry(
        k: int,
        j: int,
        best: _NetAttempt,
        retry_k: _NetAttempt,
        retry_j: _NetAttempt,
        occ_after: _Occupancy,
    ) -> bool:
        # Include the current net's original attempt in the before cell:
        # negotiating changes that attempt and j, not the already grown prefix.
        before_builts = {**builts, k: best.built}
        before = _ShoveState(
            builts=before_builts,
            keys=keys,
            used=used,
            occupancy=occupancy,
            collisions=collisions,
            unreachable=unreachable,
            extra=extra,
            negotiated=negotiated,
            partners=partners_of,
        )
        after = dataclasses.replace(
            before,
            builts={**before_builts, j: retry_j.built, k: retry_k.built},
            occupancy=occ_after,
        )
        return shv._preserves_guard(before, after, {k, j})

    n_iters = max(1, int(negotiate))
    hist_e = np.zeros((n_e, n_layers))
    hist_t = np.zeros((len(mesh.tris), n_layers))
    hist_c = np.zeros((len(mesh.tris), n_layers))
    p_fac = _NEG_P0
    prev_over = math.inf  # last pass's overuse total — the stall signal
    stall_n = 0  # consecutive passes the overuse total did not fall
    prev_keys: dict[int, tuple[int, int]] = {}
    prev_touch: set[int] = set()
    prev_unreach: list[Unreachable] = []
    prev_coll: list[tuple[int, int, int]] = []
    prev_extra: dict[int, list[tuple[int, list[int]]]] = {}
    prev_partners: dict[int, list[int]] = {}
    prev_neg: list[tuple[int, int, bool]] = []
    best_it: tuple | None = None

    def _span_of(k: int) -> float:
        pvs = [int(mesh.pin_vertex[p]) for p in d.net_pins[k]]
        if not pvs:
            return 0.0
        vv = mesh.verts[np.asarray(pvs, dtype=int)]
        return float(np.hypot(*(vv.max(axis=0) - vv.min(axis=0))))

    spans = [_span_of(k) for k in range(len(d.net_pins))]
    for it in range(n_iters):
        soft = n_iters > 1 and it < n_iters - 1
        if it == 0:
            order = route_order
        else:
            # last pass's failed or oversubscribed nets grow first,
            # then span — the contested resources go to the nets that
            # need them most (td569540)
            order = sorted(
                range(len(d.net_pins)),
                key=lambda kk: (0 if kk in prev_touch else 1, spans[kk], kk),
            )
        keys = dict(prev_keys)
        if it == 0:
            grow_set: set[int] | None = None
            unreachable = []
            collisions = []
            extra = {}
            partners_of = {}
            negotiated = []
        else:
            # selective re-route (td569540): a net whose wires satisfy
            # every resource keeps them; re-growing it against an
            # unchanged neighbourhood can only replay the same search.
            # _neg_grow_set decides — touched nets plus partners, or
            # every net once (the stall escape, a conflicted final
            # pass, or an empty touch set). The escape fires once then
            # rearms on two fresh flat passes.
            escape = stall_n >= 2
            if escape:
                stall_n = 0
            grow_set = _neg_grow_set(
                prev_touch,
                prev_partners,
                final=it == n_iters - 1,
                stall=escape,
            )
            if grow_set is None:
                unreachable = []
                collisions = []
                extra = {}
                partners_of = {}
                negotiated = []
            else:
                unreachable = [u for u in prev_unreach if u.net not in grow_set]
                collisions = [c for c in prev_coll if c[0] not in grow_set]
                extra = {
                    e: [r for r in rows if r[0] not in grow_set]
                    for e, rows in prev_extra.items()
                }
                extra = {e: rows for e, rows in extra.items() if rows}
                partners_of = {
                    kk: v for kk, v in prev_partners.items() if kk not in grow_set
                }
                negotiated = [
                    t for t in prev_neg if t[0] not in grow_set and t[1] not in grow_set
                ]
        neg_cost = (
            _NegCost(
                soft=soft,
                p=p_fac,
                edge_hist=hist_e,
                tri_hist=hist_t,
                chord_hist=hist_c,
            )
            if n_iters > 1
            else None
        )
        for k in order:
            if grow_set is not None and k not in grow_set:
                continue
            # rip the net's previous wires out before re-growing: the
            # occupancy below holds every OTHER net's current wires
            builts.pop(k, None)
            kept_it = [(j, b) for j in route_order for b in builts.get(j, ())]
            occupancy = occupancy.rebuild(kept_it)
            used = corridor.used_of(b.branch for _jj, b in kept_it)
            pins_arr = d.net_pins[k]
            cand = _net_layers(d, k, n_layers)
            net_layers[k] = cand
            trace_mm, clearance_mm = rules_used[k]
            cand_mask = _cand_mask(cand, n_layers)
            occ = corridor.occ(used)

            def attempt(
                v: _OccView,
                k: int = k,
                occ: np.ndarray = occ,
                occupancy: _Occupancy = occupancy,
            ) -> _NetAttempt:
                return _grow_net(
                    ctx,
                    mesh,
                    d,
                    n_layers,
                    k,
                    (*rules_used[k], via_dia[k], via_drill[k]),
                    occ,
                    pin_vids,
                    occupancy,
                    v,
                )

            view = occupancy.view(k, neg=neg_cost)
            best = attempt(view)
            blocked_by: tuple[int, ...] = ()
            comps, net_masks = best.comps, best.masks
            pins_l: list[int] = []
            regions: list[set[int]] = []
            main_i = 0
            if len(comps) > 1:
                # no way under the earlier wires' chords and closures: ONE
                # earlier net may be re-grown once to make room (G2h). The
                # probe drops only the chord legality — a path under it was
                # chord-blocked (the wires it still crosses are the
                # partners); a probe that still fails is closure-blocked and
                # the cut the regions share names them.
                pins_l = [int(p) for p in pins_arr]
                regions = [
                    _flood_region(
                        ctx, mesh, [pins_l[j] for j in comp], net_masks.base_pass
                    )
                    for comp in comps
                ]
                main_i = max(
                    range(len(comps)),
                    key=lambda ci: (len(regions[ci]), len(comps[ci])),
                )
                probe = attempt(dataclasses.replace(view, chords=None, splits=None))
                wall_built: list[_Built] = []
                cut: set[int] = set()
                need: dict[int, set[int]] = {}
                if len(probe.comps) == 1:
                    probe.cross = _witness_crossings(occupancy, probe.built)
                    partners = [
                        n
                        for n, _c in Counter(
                            int(occupancy.lines[j][0].net)
                            for _i, _lo, j, _s0, _s1 in probe.cross
                        ).most_common()
                    ]
                    wall_built = probe.built
                else:
                    # the probe fails too — closures, not chords, wall it:
                    # flood each comp's reach over the (tri, layer) states
                    # the effective passability leaves; the need to wall for
                    # the negotiation is the comp's blocked boundary that an
                    # earlier wire crosses on this net's layers — the
                    # consumed channel — and it names the partner to ask.
                    # The boundary keeps the layer: the capacity a refused
                    # negotiation names is the slots on the layers the net
                    # could actually be on there (``corridor.cut``'s extra).
                    open_v = net_masks.open & ~view.closed
                    cand_l = [ly for ly in range(n_layers) if cand_mask >> ly & 1]
                    wire_edges: set[int] = set()
                    for _b, _ln, _g, lays in occupancy.lines:
                        if not lays:
                            continue
                        for i, e_ in enumerate(_b.edges):
                            if cand_mask >> int(lays[i]) & 1:
                                wire_edges.add(int(e_))
                    boundary: set[tuple[int, int]] = set()
                    for ci, comp in enumerate(comps):
                        if ci == main_i:
                            continue
                        reg = _flood_states(
                            ctx,
                            mesh,
                            [pins_l[j2] for j2 in comp],
                            [
                                _pin_layers(d, pins_l[j2], n_layers, cand_mask)
                                for j2 in comp
                            ],
                            open_v,
                            net_masks.fit,
                            cand_l,
                        )
                        for t, ly in reg:
                            for e in (int(x) for x in mesh.tri_edges[t]):
                                t2 = int(mesh.edge_tris[e, 0])
                                t2 = int(mesh.edge_tris[e, 1]) if t2 == t else t2
                                if (t2, ly) not in reg:
                                    boundary.add((e, ly))
                    for e, ly in boundary:
                        if e in wire_edges:
                            need.setdefault(e, set()).add(ly)
                    cut |= set(need)
                    partners = [
                        n for n, _c in occupancy.nets_on(cut, cand_mask).most_common()
                    ]
                partners_of[k] = partners
                j = partners[0] if partners and partners[0] in builts else None
                kept = False
                if j is not None:
                    blocked_by = (j,)
                # Soft negotiated passes skip the one-shot partner swap:
                # rip-up/reroute IS the negotiation there — the partners
                # join next pass's grow set through partners_of and yield
                # by price, not by a guarded swap whose emit-shape check
                # costs a full plan per try (td569540). The final hard
                # pass keeps the G2h negotiation exactly as before.
                if j is not None and not view.neg_soft:
                    kept_pairs = [
                        (kk, bb) for kk, bl in builts.items() if kk != j for bb in bl
                    ]
                    occ_j = occupancy.rebuild(
                        [*kept_pairs, *[(k, b) for b in wall_built]]
                    )
                    view_j = occ_j.view(j, neg=neg_cost)
                    if cut:
                        cut_closed = np.zeros(n_e, dtype=np.int64)
                        cut_closed[sorted(cut)] = cand_mask
                        view_j = dataclasses.replace(
                            view_j, closed=view_j.closed | cut_closed
                        )
                    used_j = corridor.used_of(
                        b.branch for _kk, bl in builts.items() if _kk != j for b in bl
                    )
                    retry_j = _grow_net(
                        ctx,
                        mesh,
                        d,
                        n_layers,
                        j,
                        (*rules_used[j], via_dia[j], via_drill[j]),
                        corridor.occ(used_j),
                        pin_vids,
                        occ_j,
                        view_j,
                    )
                    # Keep the order loop's connected-partner requirement.
                    # Its prefix guard cannot know which existing offence later
                    # ordinary grows would heal: retaining a disconnected partner
                    # here can prevent that healing on an untouched net. Broader
                    # failed-partner rescue belongs to the complete-state shove.
                    fail_j = 1 if keys[j][0] > 1 or keys[j][1] else 0
                    if len(retry_j.comps) == 1 and (
                        not _wit_stats(occ_j, retry_j)[0] or fail_j
                    ):
                        occ_k = occupancy.rebuild(
                            [*kept_pairs, *[(j, b) for b in retry_j.built]]
                        )
                        used_k = corridor.used_of(
                            itertools.chain(
                                (
                                    b.branch
                                    for _kk, bl in builts.items()
                                    if _kk != j
                                    for b in bl
                                ),
                                (b.branch for b in retry_j.built),
                            )
                        )
                        retry_k = _grow_net(
                            ctx,
                            mesh,
                            d,
                            n_layers,
                            k,
                            (*rules_used[k], via_dia[k], via_drill[k]),
                            corridor.occ(used_k),
                            pin_vids,
                            occ_k,
                            occ_k.view(k, neg=neg_cost),
                        )
                        # Judge both attempts in the complete proposed cell.
                        # In particular j must see k, which is not yet in occ_k.
                        occ_after = occ_k.rebuild(
                            [
                                *kept_pairs,
                                *((j, b) for b in retry_j.built),
                                *((k, b) for b in retry_k.built),
                            ]
                        )
                        wit_k = _wit_stats(occ_after, retry_k)
                        wit_j = _wit_stats(occ_after, retry_j)
                        if (
                            len(retry_k.comps) == 1
                            and not wit_k[1]
                            and _accept_stats(
                                _sum_stats(
                                    (
                                        _wit_stats(occupancy, best),
                                        _key_stats(keys, builts, j),
                                    )
                                ),
                                _sum_stats((wit_k, wit_j)),
                                {k} | ({j} if fail_j else set()),
                                ({k} if wit_k[0] else set())
                                | ({j} if wit_j[0] else set()),
                            )
                            and _preserves_other_nets(
                                occupancy, occ_after, builts, {k, j}
                            )
                            and preserves_retry(k, j, best, retry_k, retry_j, occ_after)
                        ):
                            builts[j] = retry_j.built
                            keys[j] = (len(retry_j.comps), wit_j[1])
                            occupancy, used, best = occ_k, used_k, retry_k
                            # j is replaced wholesale: its earlier
                            # collisions and Unreachable rows go with it —
                            # a retry_j that fails under the tie-break is
                            # named again from its own attempt (S2)
                            collisions[:] = [c for c in collisions if c[0] != j]
                            collisions.extend(retry_j.collisions)
                            unreachable[:] = [u for u in unreachable if u.net != j]
                            cross_j = _witness_crossings(occ_after, retry_j.built)
                            blocked_j = tuple(
                                n
                                for n, _c in sorted(
                                    Counter(
                                        int(occ_after.lines[x][0].net)
                                        for _i, _lo, x, _s0, _s1 in cross_j
                                    ).items(),
                                    key=lambda kv: (-kv[1], kv[0]),
                                )
                            )
                            if blocked_j:
                                partners_of[j] = list(blocked_j)
                            elif not wit_j[0]:
                                partners_of.pop(j, None)
                            if len(retry_j.comps) > 1:
                                blocked_j = blocked_j or (k,)
                                unreachable += _unreachable_rows(
                                    ctx,
                                    mesh,
                                    d,
                                    j,
                                    n_layers,
                                    retry_j.comps,
                                    retry_j.masks,
                                    rules_used[j][0] / 2.0
                                    + rules_used[j][1]
                                    + FAB_QUANTUM_MM,
                                    blocked_j,
                                )
                                partners_of[j] = list(blocked_j)
                            kept = True
                    negotiated.append((k, j, kept))
                if not kept and len(probe.comps) == 1:
                    if not probe.cross or view.neg_soft:
                        # The chord/split gate is conservative: adopt a
                        # connected probe only after its physical witness
                        # check above, including unrepairable bigons. A
                        # soft negotiated pass adopts it WITH the
                        # crossings — they are priced resources this
                        # iteration, the history pass makes them yield
                        # (td569540).
                        best = probe
                    else:
                        # the crossing the chords forbid is the later net's own
                        # failure (S1): the earlier wires stand untouched, the
                        # stranded comps name the crossing partners below — the
                        # chord-free probe is a witness, never adopted copper
                        blocked_by = tuple(partners)
                elif not kept and cut:
                    # the probe failed too: closures wall the net, a
                    # capacity fact — feed its demand on the cut into the
                    # corridor cut so the refusal names the channel, its
                    # owners and every demanding net (G2h review-2 A)
                    for e in cut:
                        extra.setdefault(e, []).append((k, sorted(need[e])))
                comps, net_masks = best.comps, best.masks
            collisions.extend(best.collisions)
            cross_k = _witness_crossings(occupancy, best.built)
            keys[k] = (len(best.comps), len(cross_k))
            builts[k] = best.built
            if cross_k:
                # a first-attempt taut crossing is a strict failure too —
                # the shove takes it with the nets it crosses as partners
                partners_of[k] = [
                    n
                    for n, _c in sorted(
                        Counter(
                            int(occupancy.lines[j][0].net)
                            for _i, _lo, j, _s0, _s1 in cross_k
                        ).items(),
                        key=lambda kv: (-kv[1], kv[0]),
                    )
                ]
            if len(comps) > 1:
                unreachable += _unreachable_rows(
                    ctx,
                    mesh,
                    d,
                    k,
                    n_layers,
                    comps,
                    net_masks,
                    trace_mm / 2.0 + clearance_mm + FAB_QUANTUM_MM,
                    blocked_by,
                )
        # canonical ledger order — rebuilds and the emit read it
        builts = {j: builts[j] for j in route_order if j in builts}
        all_pairs = [(j, b) for j in route_order for b in builts.get(j, ())]
        occupancy = occupancy.rebuild(all_pairs)
        used = corridor.used_of(b.branch for _jj, b in all_pairs)
        branches_it = [b.branch for bl in builts.values() for b in bl]
        ord_it, _ord_pos_it, _res_it = _order(ctx, mesh, tuple(branches_it))
        cuts_it = corridor.cut(branches_it, ord_it, extra=extra)
        legal = all(c.refused for c in cuts_it)
        # td569540: a soft pass's priced crossings are negotiable
        # intermediate state, never emittable copper — a soft ledger is
        # a best candidate only when it carries none. The hard pass is
        # always eligible: its crossings are the honest named failures
        # _Shove and the plan already handle.
        cross_free = not any(v[1] for v in keys.values())
        coll_nets = {c[0] for c in collisions}
        metric_it = _sum_stats(
            _wire_stats(
                int(
                    keys.get(kk, (1, 0))[0] > 1
                    or keys.get(kk, (1, 0))[1] > 0
                    or kk in coll_nets
                ),
                keys.get(kk, (1, 0))[1],
                builts.get(kk, ()),
            )
            for kk in order
        )
        if (
            legal
            and (not soft or cross_free)
            and (best_it is None or metric_it < best_it[0])
        ):
            best_it = (
                metric_it,
                {j: list(bl) for j, bl in builts.items()},
                dict(keys),
                list(unreachable),
                list(collisions),
                {e: list(rows) for e, rows in extra.items()},
                dict(partners_of),
                list(negotiated),
                occupancy,
                used,
            )
        if soft:
            over_e = np.maximum(0.0, occupancy.used - occupancy.cap)
            over_t = np.maximum(0.0, occupancy.tri_used - _NEG_TRI_CAP)
            over_c = occupancy.cross_used
            hist_e += _NEG_H_EDGE * over_e
            hist_t += _NEG_H_TRI * over_t
            hist_c += _NEG_H_CROSS * over_c
            over_total = float(over_e.sum() + over_t.sum() + over_c.sum())
            if over_total <= 1e-9 and not any(keys.get(kk, (0, 0))[1] for kk in keys):
                # converged: nothing oversubscribed, nothing crossing —
                # this pass is legal and cannot improve (td569540)
                break
            # the stall signal (td569540 ruling): the total not falling
            # two passes running means negotiation is frozen — the next
            # pass re-grows every net once
            stall_n = stall_n + 1 if over_total >= prev_over - 1e-9 else 0
            prev_over = over_total
            p_fac *= _NEG_PRAMP
            prev_keys = keys
            prev_touch = _neg_touch(occupancy, builts, keys, collisions)
            prev_unreach = unreachable
            prev_coll = collisions
            prev_extra = extra
            prev_partners = partners_of
            prev_neg = negotiated
    if best_it is not None:
        (
            _metric_best,
            builts,
            keys,
            unreachable,
            collisions,
            extra,
            partners_of,
            negotiated,
            occupancy,
            used,
        ) = best_it
    # S2: the shove phase — bounded rounds of pinned swaps over the
    # still-failing set, grow-count budgeted, deterministic.
    shove_grows = 0
    shove_rounds: tuple[tuple[int, ...], ...] = ()
    if shove:
        st = shv.run(
            _ShoveState(
                builts=builts,
                keys=keys,
                used=used,
                occupancy=occupancy,
                collisions=collisions,
                unreachable=unreachable,
                extra=extra,
                negotiated=negotiated,
                partners=partners_of,
            )
        )
        builts, keys, used = st.builts, st.keys, st.used
        collisions, unreachable, extra = st.collisions, st.unreachable, st.extra
        negotiated = st.negotiated
        shove_grows, shove_rounds = shv.grows, tuple(shv.round_yield)
    branches = [b.branch for bl in builts.values() for b in bl]
    emit_order, order_pos, residual = _order(ctx, mesh, tuple(branches))
    overflow = corridor.cut(branches, emit_order, extra=extra)
    return Wires(
        branches=tuple(branches),
        order=emit_order,
        order_pos=order_pos,
        capacity_mm=capacity_mm,
        net_layers=tuple(net_layers),
        unreachable=tuple(unreachable),
        gap_free_mm=gap_free,
        gap_cap=gap_cap,
        order_residual=residual,
        overflow=overflow,
        via_collisions=tuple(collisions),
        negotiated=tuple(negotiated),
        shove_grows=shove_grows,
        shove_rounds=shove_rounds,
    )

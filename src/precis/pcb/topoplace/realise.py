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

Via SITING is keepout-aware (topo S3, gr520901): a site is never minted
on a constrained ring edge or within a barrel's keep-out of an endpoint
vertex — the walk moves along the branch's crossed edges to the first
whose pad-channel record (:attr:`Wires.gap_free_mm`) leaves the via plus
its clearance on both outer layers, clears every pad polygon on those
layers by the barrel's keep-out (the channel record is trace-buffered —
a site "free" for a 0.15 mm wire can still drill a solder land),
does not crowd an already placed via, and keeps the via clearance off
every other net's polyline — the barrel spans every layer, so a foreign
track on any layer vetoes the site (gr520902 s4d). A branch with no qualifying edge
mints no via; the plan records it on :attr:`RoutePlan.no_via` so the
caller reports the net ``failed: no via site`` instead of committing
keepout-breaking copper.

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
import itertools
import logging
import math
from collections import Counter
from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from shapely.geometry import LineString, Point, Polygon

from precis.pcb.ratsnest import PLANE_CLASSES
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import proxy as topo_proxy
from precis.pcb.topoplace import wires as _wires
from precis.pcb.topoplace.design import TopoDesign, _world_to_local
from precis.pcb.topoplace.masks import BoardMasks, corridor_score
from precis.pcb.topoplace.mesh import TopoMesh
from precis.pcb.topoplace.wires import (
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
    """A via at ``(edge, t)`` on the mesh — world ``(x, y)`` sits on an
    edge the wire crosses, at or near the taut wire's own crossing point
    (the siting walk may move it to a keepout-clean neighbour edge and
    clamps ``t`` off the endpoint vertices — gr520901). ``dia_mm`` is
    the barrel the site was qualified against — the fab-minimum fallback
    when the net-rule via found no site (gr524334); ``0.0`` means the
    net rule's own via_dia (the comfortable house default). ``drill_mm``
    is the drill paired with that barrel (the fab-minimum drill under a
    fallback diameter — the house drill would leave a sub-floor annular
    ring); ``0.0`` means the net rule's own drill."""

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
    #: Nets (net_pins indices) whose emitted copper failed the geometric
    #: anti-short guard — withdrawn whole (all pieces and vias), never
    #: committed as shorting copper (gr524239). ``sketch_rows`` reports
    #: them ``failed: short-risk``. The plan's own ``pieces`` keep the
    #: withdrawn geometry for the record, exactly like ``crossings``.
    #: Off-angle emit vetoes (``off_angle``) are ALSO folded in here, so
    #: the route stage's failure count and shove loop see them without
    #: a new field to read — ``off_angle`` carries the true reason.
    guarded: tuple[int, ...] = ()
    #: Nets whose emitted copper is not one connected piece joining all
    #: their pads — the emit-connectivity check (gr537831) withdraws
    #: them whole like ``guarded``; ``sketch_rows`` reports them
    #: ``failed: unconnected``. A falsely realized broken net must never
    #: reach DRC.
    severed: tuple[int, ...] = ()
    #: Nets whose octilinear redraw kept an authored leg no L-bend could
    #: replace (gr543679): the emit 45° check in ``realize_result``
    #: refuses the piece's track — never committed, since the DRC's
    #: octilinear rule would only flag it AFTER commit. The net is
    #: folded into ``guarded`` for the route stage's accounting; this
    #: field names the real reason so ``sketch_rows`` reports
    #: ``failed: non-octilinear`` instead of ``short-risk``.
    off_angle: tuple[int, ...] = ()

    @property
    def n_routed_nets(self) -> int:
        return len({p.net for p in self.pieces})


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


def _legalise_open(
    d: TopoDesign,
    b: WirePath,
    n_layers: int,
    open_e: np.ndarray,
    cuts: list[int],
    runs: list[int],
) -> None:
    """Repair a ``(cuts, runs)`` pair in place so every run sits on a
    layer open on all its edges (gr520902 review): relabel to a layer
    in the run's open-set intersection when one covers it, else split
    at the first edge where the open set can no longer extend the run —
    the cut is a via. Terminal pieces prefer their pad-forced layer."""
    n = len(b.edges)
    la = _pin_forced(d, b.pin_a, n_layers)
    lb = _pin_forced(d, b.pin_b, n_layers)

    def prefer(lo: int, hi: int, mask: int, old: int) -> int:
        if mask >> old & 1:
            return old
        if lo <= 0 and la is not None and mask >> la & 1:
            return la
        if hi >= n - 1 and lb is not None and mask >> lb & 1:
            return lb
        for layer in (0, n_layers - 1):
            if mask >> layer & 1:
                return layer
        return (mask & -mask).bit_length() - 1

    new_cuts: list[int] = []
    new_runs: list[int] = []
    bounds = _run_bounds(cuts, n)
    for p, (lo, hi) in enumerate(bounds):
        acc = -1
        seg_lo = lo
        for i in range(lo, hi + 1):
            oe = int(open_e[int(b.edges[i])])
            if acc & oe:
                acc &= oe
            else:
                new_runs.append(prefer(seg_lo, i - 1, acc, runs[p]))
                new_cuts.append(i - 1)
                seg_lo, acc = i, oe
        new_runs.append(prefer(seg_lo, hi, acc, runs[p]))
        if p < len(bounds) - 1:
            # an existing run boundary is a cut too — re-emit it after
            # any splits the open set forced inside the run
            new_cuts.append(hi)
    cuts[:] = new_cuts
    runs[:] = new_runs


def _initial_segment(
    d: TopoDesign,
    wires: Wires,
    b: WirePath,
    n_layers: int,
    open_e: np.ndarray | None = None,
) -> tuple[list[int], list[int]]:
    """One branch's start ``(runs, cuts)`` — ``_initial_segments``'
    per-branch case, also used to segment a re-threaded branch. With
    ``open_e`` (the net's per-edge open-layer mask) the nominal runs
    are repaired to the corridor's layer rule: relabel where a covering
    layer exists, split where no layer carries the run."""
    la = _pin_forced(d, b.pin_a, n_layers)
    lb = _pin_forced(d, b.pin_b, n_layers)
    n_edges = len(b.edges)
    if la is not None and lb is not None and la != lb and n_edges >= 2:
        runs, cuts = [la, lb], [n_edges // 2]
    else:
        if la is not None:
            layer = la
        elif lb is not None:
            layer = lb
        else:
            cand = (
                set(wires.net_layers[b.net]) if b.net < len(wires.net_layers) else set()
            )
            layer = min(cand & {0, n_layers - 1} or cand or {0})
        runs, cuts = [layer], []
    if open_e is not None and n_edges:
        _legalise_open(d, b, n_layers, open_e, cuts, runs)
        _fix_terminals(d, b, n_layers, cuts, runs)
        _merge_same(cuts, runs)
        _legalise_open(d, b, n_layers, open_e, cuts, runs)
    return runs, cuts


def _initial_segments(
    d: TopoDesign,
    wires: Wires,
    n_layers: int,
    open_layers: dict[int, np.ndarray] | None = None,
) -> tuple[list[list[int]], list[list[int]]]:
    """Per-branch start runs/cuts — the segment model's form of the old
    initial layer + join derivation: both ends forcing opposite outers
    (F↔B) splits mid-branch (cut ``E//2`` — a single-edge branch takes
    the cut-0 stub form); anything else is one run on the forced end's
    layer, else the lowest OUTER candidate (inner-only candidates fall
    back to the inner). ``open_layers`` is the per-net corridor mask —
    ``None`` on synthetic meshes, where the layer rule is vacuous."""
    runs: list[list[int]] = []
    cuts: list[list[int]] = []
    for b in wires.branches:
        oe = open_layers.get(int(b.net)) if open_layers is not None else None
        r, c = _initial_segment(d, wires, b, n_layers, open_e=oe)
        runs.append(r)
        cuts.append(c)
    return runs, cuts


def _cdirs(mesh: TopoMesh, wires: Wires) -> list[list[float]]:
    """The seam-test's corridor-direction sign per (branch, edge index):
    ``sign(corridor_travel × edge u→v)`` — the normalization that turns
    an edge's raw ``t`` order into the wire's TRANSVERSE order. On a
    fan (edges of different orientations through one vertex field) the
    raw order inverts without any braid — the phantom pairs that
    dropped nets mid-branch to via dodges around nothing (gr520900).
    A branch without corridor tris (synthetic wires) reads +1 — raw
    order is then the only signal it carries."""
    cent = [mesh.verts[list(t)].mean(axis=0) for t in mesh.tris]
    out: list[list[float]] = []
    for br in wires.branches:
        row: list[float] = []
        for i, e in enumerate(br.edges):
            if i + 1 >= len(br.tris):
                row.append(1.0)
                continue
            ca, cb = cent[br.tris[i]], cent[br.tris[i + 1]]
            ux, uy = mesh.verts[mesh.edges[e, 0]]
            vx, vy = mesh.verts[mesh.edges[e, 1]]
            cr = (cb[0] - ca[0]) * (vy - uy) - (cb[1] - ca[1]) * (vx - ux)
            row.append(-1.0 if cr < -1e-12 else 1.0)
        out.append(row)
    return out


def _pair_sign(
    wires: Wires, cdirs: Sequence[Sequence[float]], e: int, a: int, b: int
) -> int:
    """Pair ``(a, b)``'s normalized relative side on edge ``e`` — the
    seam convention (``_pair_regions``' sign): position in ``order[e]``
    signed by the SECOND branch's corridor direction."""
    seq = wires.order[e]
    s = 1 if seq.index(a) < seq.index(b) else -1
    eb = {int(x): i for i, x in enumerate(wires.branches[b].edges)}
    if not cdirs or int(e) not in eb:
        return s
    return s * int(cdirs[b][eb[int(e)]])


def _crossing_pairs(
    wires: Wires,
    cdirs: Sequence[Sequence[float]],
    obstacles: Container[int] | None = None,
    edge_ends: Sequence[tuple[int, int]] | None = None,
) -> set[tuple[int, int]]:
    """Branch pairs whose relative order flips between two shared edges
    (Decision 3's list comparison — NORMALIZED by corridor direction:
    raw edge-parameter order is a spatial axis, and on a fan it inverts
    between edges of different orientation without a single wire ever
    crossing another). Same-net pairs are kept — the caller filters, a
    braid of the same net is not a short but two same-net wires can
    still cross on the board's geometry.

    gr536410: a flip is a real braid only if the side choice is LOCKED.
    When ``obstacles`` is given, a flip whose shared run between the two
    witness edges passes only through FREE ring vertices (a vertex that
    is neither a pad/pin vertex nor a keepout/outline corner — see
    :func:`precis.pcb.topoplace.wires.obstacle_vertices`) is a fan, not
    a crossing: the two wires passed opposite sides of a vertex that
    blocks nothing and can be slid apart. A flip with no shared vertex
    between the witnesses (disjoint edges or a wedge) stays a crossing —
    the corridors genuinely interleave there."""
    epos = [{int(e): i for i, e in enumerate(b.edges)} for b in wires.branches]
    seen: dict[tuple[int, int], int] = {}
    witness: dict[tuple[int, int], int] = {}
    crossed: set[tuple[int, int]] = set()
    for e, seq in enumerate(wires.order):
        pos = {b: i for i, b in enumerate(seq)}
        items = sorted(pos)
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                key = (a, b)
                cdir = cdirs[b][epos[b][e]] if cdirs else 1.0
                sign = (pos[a] - pos[b]) * int(cdir)
                if key in seen and seen[key] * sign < 0:
                    if (
                        obstacles is None
                        or edge_ends is None
                        or _flip_is_locked(
                            wires, obstacles, a, b, witness[key], e, edge_ends
                        )
                    ):
                        crossed.add(key)
                else:
                    seen[key] = sign
                    witness[key] = e
    return crossed


def _pair_regions(
    wires: Wires,
    cdirs: Sequence[Sequence[float]] | None = None,
    obstacles: Container[int] | None = None,
    edge_ends: Sequence[tuple[int, int]] | None = None,
) -> dict[tuple[int, int], list[tuple[int, int, int, int]]]:
    """Crossing pairs and their bracketed regions (Decision 3's list
    comparison): two wires sharing edges where their relative order
    flips cross; each flipped shared-edge pair brackets ``(min..max]``
    on each branch's own edge-index space — the span a mid-piece flip
    moves. Same-net pairs stay — the caller filters.

    The flip test is the braid-merge's, not the raw list comparison:
    signs are NORMALIZED by corridor direction like
    :func:`_crossing_pairs` (raw edge-parameter order inverts on a fan
    without a single crossing), and when ``obstacles``/``edge_ends``
    are given a flipped pair whose runs between the witness edges pass
    only FREE ring vertices is dropped — a fan, not a crossing
    (:func:`_flip_is_locked`). Synthetic callers with no mesh pass
    neither and get the raw comparison."""
    epos = [{int(e): i for i, e in enumerate(b.edges)} for b in wires.branches]
    shared: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for e, seq in enumerate(wires.order):
        pos = {b: i for i, b in enumerate(seq)}
        items = sorted(pos)
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                cdir = cdirs[b][epos[b][e]] if cdirs and e in epos[b] else 1.0
                shared.setdefault((a, b), []).append((e, (pos[a] - pos[b]) * int(cdir)))
    regions: dict[tuple[int, int], list[tuple[int, int, int, int]]] = {}
    for (a, b), lst in shared.items():
        ia_of = epos[a]
        ib_of = epos[b]
        regs: list[tuple[int, int, int, int]] = []
        if obstacles is not None and edge_ends is not None:
            # Real mesh — the braid convention: each inversion is checked
            # against the RUNNING witness (the last edge of the prevailing
            # sign), so only the actual flip point is judged — a longer
            # span that happens to cover an obstacle elsewhere does not
            # lock a fan. A flip never becomes the next witness.
            s_run, w = lst[0]
            for e, s in lst[1:]:
                if s * s_run < 0:
                    if (
                        w in ia_of
                        and w in ib_of
                        and e in ia_of
                        and e in ib_of
                        and _flip_is_locked(wires, obstacles, a, b, w, e, edge_ends)
                    ):
                        ia1, ia2 = sorted((ia_of[w], ia_of[e]))
                        ib1, ib2 = sorted((ib_of[w], ib_of[e]))
                        regs.append((ia1, ia2, ib1, ib2))
                else:
                    s_run, w = s, e
        else:
            for i, (e1, s1) in enumerate(lst):
                for e2, s2 in lst[i + 1 :]:
                    if s1 * s2 >= 0 or not all(
                        e in ia_of and e in ib_of for e in (e1, e2)
                    ):
                        continue
                    ia1, ia2 = sorted((ia_of[e1], ia_of[e2]))
                    ib1, ib2 = sorted((ib_of[e1], ib_of[e2]))
                    regs.append((ia1, ia2, ib1, ib2))
        if regs:
            regions[(a, b)] = regs
    return regions


def _pair_live(
    wires: Wires,
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
    ea = len(wires.branches[a].edges)
    eb = len(wires.branches[b].edges)
    return any(
        _span_layers(cuts[a], runs[a], ea, ia1, ia2)
        & _span_layers(cuts[b], runs[b], eb, ib1, ib2)
        for ia1, ia2, ib1, ib2 in regions
    )


def _flip_is_locked(
    wires: Wires,
    obstacles: Container[int],
    a: int,
    b: int,
    e_from: int,
    e_to: int,
    edge_ends: Sequence[tuple[int, int]],
) -> bool:
    """Whether the order flip of pair ``(a, b)`` between witness edges
    ``e_from``/``e_to`` is locked by an obstacle (gr536410). BOTH
    branches' runs between the witnesses are scanned for the mesh
    vertices they pass through (endpoints shared by consecutive run
    edges) — a pad on either side of the wedge locks the side choice.
    A flip with NO shared vertex on either run stays a crossing — the
    corridors genuinely interleave there (the _span_fixture shape). A
    flip AT shared vertices is a braid only if ANY of them is an
    obstacle; all-free means the wires passed opposite sides of vertices
    that block nothing and can be slid apart — a fan, not a crossing."""
    shared_vs: list[int] = []
    scanned = False
    for br in (wires.branches[a], wires.branches[b]):
        pos = {int(e): i for i, e in enumerate(br.edges)}
        if e_from not in pos or e_to not in pos:
            continue
        scanned = True
        i_from, i_to = sorted((pos[e_from], pos[e_to]))
        span = [int(e) for e in br.edges[i_from : i_to + 1]]
        for j in range(len(span) - 1):
            u, v = edge_ends[span[j]]
            x, y = edge_ends[span[j + 1]]
            shared_vs.extend({u, v} & {x, y})
    if not scanned:
        # the order lists name a witness edge neither branch itself
        # crosses (inconsistent synth fixtures) — legacy verdict.
        return True
    if not shared_vs:
        return True
    return any(w in obstacles for w in shared_vs)


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
) -> tuple[Wires, list[list[int]], list[list[int]], tuple[tuple[int, int], ...]]:
    """Segment-aware crossing resolution as a bounded local search
    (gr520902 stage b). Per branch a runs/cuts model replaces the
    single outer layer — a cut at edge index ``c`` mints a via on
    ``edges[c]``, run p covers ``(cuts[p-1] .. cuts[p]]``, so the piece
    between two vias can take ANY candidate layer, inners included.

    Each round every live pair offers its moves: a whole-branch flip
    of either side to the other outer (a through-hole branch moves
    free — no cut, no via), the Decision-6 yielder's bracketed span to
    each of its net's candidate layers, and — only once every flip
    stalls, and only on a real mesh — a re-thread of either side's net
    with the pair's shared corridor masked, so the wire regrows around
    the wrapped pad's other side. The cheapest move by ``(live pairs,
    via count)`` wins; a round with no strictly improving move ends the
    search. Returns ``(wires, runs, cuts, remaining pairs)`` — the
    wires carry any kept re-thread's new branches and a rebuilt
    ``order``.

    ``event`` (gr513870) receives one progress line per search round —
    ``None`` (the default) keeps the search silent.

    ``masks`` (pcb-layer-argmin) — the two-kernel mask set at the routed
    pose: move keys gain a fifth element, the corridor mask cost of the
    moved branch under the proposed runs/cuts (length-weighted
    route-block share + ``_MASK_CROSS_K`` per width-aware spine
    crossing).  ``None`` (the default) scores every move 0.0 — the
    search order is then bit-identical to the four-element key."""
    branches = list(wires.branches)
    cur_wires = wires
    # Braid merge (gr520900/gr536410): on a real mesh the flip test
    # normalizes signs by corridor direction and drops flips locked by
    # nothing — the phantom-fan fix. Synthetic fixtures (no corridor
    # tris) get the raw comparison.
    edge_ends = [(int(u), int(v)) for u, v in mesh.edges]
    obstacles = obstacle_vertices(mesh, d, n_layers) if len(mesh.tris) else None
    pair_regs = _pair_regions(cur_wires, _cdirs(mesh, cur_wires), obstacles, edge_ends)
    rules = net_rules or {}
    outers = frozenset((0, n_layers - 1))
    n_e = len(mesh.edges)
    tried_thread: set[tuple[int, frozenset[int]]] = set()
    # The corridor's per-edge open-layer masks (gr520902 review): on a
    # real mesh a run may only sit on a layer every one of its edges
    # leaves open — the masks are the passability the routing search
    # priced, now enforced as a hard rule rather than assumed. Synthetic
    # fixtures carry no corridor data: the rule is vacuous there
    # (``open_e=None`` everywhere).
    ctx: Any = None
    corridor: Any = None
    open_layers: dict[int, np.ndarray] = {}
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
        corridor = _wires._corridor(
            ctx, mesh, d, n_layers, rules_used, d.pose0 if pose is None else pose
        )
        for k in {int(b.net) for b in wires.branches}:
            cand_k = wires.net_layers[k] if k < len(wires.net_layers) else frozenset()
            name_k = str(d.net_name[k]) if k < len(d.net_name) else str(k)
            nr_k = rules.get(name_k, _DEFAULT)
            open_layers[k] = _wires._open_layers(
                ctx,
                d,
                _wires._cand_mask(cand_k, n_layers),
                name_k,
                frozenset(int(p) for p in d.net_pins[k]),
                float(nr_k.track_width_mm),
                float(nr_k.clearance_mm),
            )
    runs, cuts = _initial_segments(d, wires, n_layers, open_layers or None)

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
                    _edge_xy(
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

    mask_tot = _state_mask_cost(branches, runs, cuts)

    def live_pairs(
        w: Wires,
        rs: list[list[int]],
        cs: list[list[int]],
        regs: dict[tuple[int, int], list[tuple[int, int, int, int]]],
    ) -> set[tuple[int, int]]:
        return {
            (a, b)
            for (a, b), rg in regs.items()
            if w.branches[a].net != w.branches[b].net
            and _pair_live(w, rs, cs, rg, a, b)
        }

    def n_vias(cs: list[list[int]]) -> int:
        return sum(len(c) for c in cs)

    def cand_layers(k: int) -> Iterable[int]:
        c = cur_wires.net_layers[k] if k < len(cur_wires.net_layers) else frozenset()
        return sorted(c) if c else sorted(outers)

    def flip_key(
        bb: int, r2: list[int], c2: list[int]
    ) -> tuple[int, int, int, float, float] | None:
        """The move's ``(live pairs, overflow, vias, length, mask cost)``
        cost — a flip never touches the wires so overflow and length are
        the current constants; the mask element is the board total with
        this branch's proposed runs/cuts swapped in (0.0 with no masks)
        — or ``None`` when a run lands on a layer one of its edges
        closes: the hard open rule, never a cost (gr520902 review)."""
        if not _runs_open(branches[bb], open_layers.get(int(branches[bb].net)), c2, r2):
            return None
        rs2 = list(runs)
        rs2[bb] = r2
        cs2 = list(cuts)
        cs2[bb] = c2
        return (
            len(live_pairs(cur_wires, rs2, cs2, pair_regs)),
            len(cur_wires.overflow),
            n_vias(cs2),
            sum(x.length_mm for x in branches),
            mask_tot
            - _bb_mask_cost(bb, runs[bb], cuts[bb])
            + _bb_mask_cost(bb, r2, c2),
        )

    def rethread(
        bb: int, shared: frozenset[int]
    ) -> tuple[tuple[int, int, int, float, float], tuple[Any, ...]] | None:
        """Net ``bb``'s tree regrown with the pair's shared corridor
        masked — ``None`` when the regrow strands a component, yields
        no branch, or its demand-vs-capacity cut grows the board's
        overflow record (gr520902 review: the regrowth pays
        build_wires' own occupancy and faces the same cut)."""
        k = int(branches[bb].net)
        name = str(d.net_name[k]) if k < len(d.net_name) else str(k)
        nr = rules.get(name, _DEFAULT)
        mask = np.zeros(n_e, dtype=bool)
        mask[sorted(shared)] = True
        occ = corridor.occ(corridor.used_of(x for x in branches if x.net != k))
        found, comps, base_pass = _wires._net_tree(
            ctx,
            mesh,
            d,
            k,
            n_layers,
            float(nr.track_width_mm),
            float(nr.clearance_mm),
            occ,
            extra_blocked=mask,
        )
        if not found or len(comps) > 1:
            return None
        passable = _wires._make_passable(base_pass)
        pin_vids = frozenset(int(v) for v in mesh.pin_vertex)
        newb = [
            _wires._build_branch(ctx, mesh, k, pa, pb, t2, e2, passable, pin_vids)[0]
            for pa, pb, t2, e2 in found
        ]
        idx = [i for i, x in enumerate(branches) if x.net == k]
        lo_i, hi_i = idx[0], idx[-1] + 1
        # the splice below assumes a net's branches sit contiguously —
        # a rethread interleaves them only if a caller broke that
        assert idx == list(range(lo_i, hi_i))
        cbr = branches[:lo_i] + newb + branches[hi_i:]
        order2, residual2 = _wires._order(ctx, mesh, tuple(cbr))
        cut2 = corridor.cut(cbr, order2)
        if len(cut2) > len(cur_wires.overflow):
            return None
        cw = dataclasses.replace(
            cur_wires,
            branches=tuple(cbr),
            order=order2,
            order_residual=residual2,
            overflow=cut2,
        )
        regs2 = _pair_regions(cw, _cdirs(mesh, cw), obstacles, edge_ends)
        rs2 = list(runs[:lo_i])
        cs2 = list(cuts[:lo_i])
        for nb_ in newb:
            r0, c0 = _initial_segment(d, cw, nb_, n_layers, open_e=open_layers.get(k))
            rs2.append(r0)
            cs2.append(c0)
        rs2 += runs[hi_i:]
        cs2 += cuts[hi_i:]
        key = (
            len(live_pairs(cw, rs2, cs2, regs2)),
            len(cut2),
            n_vias(cs2),
            sum(x.length_mm for x in cbr),
            _state_mask_cost(cbr, rs2, cs2),
        )
        return key, (cbr, rs2, cs2, cw, regs2)

    def state_key() -> tuple[int, int, int, float, float]:
        return (
            len(cur_live),
            len(cur_wires.overflow),
            n_vias(cuts),
            sum(x.length_mm for x in branches),
            mask_tot,
        )

    cur_live = live_pairs(cur_wires, runs, cuts, pair_regs)
    best_key = state_key()
    best = (
        cur_wires,
        [list(r) for r in runs],
        [list(c) for c in cuts],
        pair_regs,
    )
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
        for a, b in sorted(cur_live):
            regs = pair_regs[(a, b)]
            y = (
                a
                if _yield_rank(d, branches[a], n_layers)
                <= _yield_rank(d, branches[b], n_layers)
                else b
            )
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
                key = flip_key(bb, r2, c2)
                if (
                    key is not None
                    and key < cur_key
                    and (chosen_key is None or key < chosen_key)
                ):
                    chosen_key, chosen = key, ("flip", (bb, r2, c2))
            # the yielder's bracketed span to each candidate layer —
            # the union of the regions this pair marks on its own
            # edge-index space
            k = int(branches[y].net)
            j = 0 if y == a else 2
            lo = min(r_[j] for r_ in regs) + 1
            hi = max(r_[j + 1] for r_ in regs)
            for ly in cand_layers(k):
                c2, r2 = list(cuts[y]), list(runs[y])
                _set_span(d, branches[y], n_layers, c2, r2, lo, hi, ly)
                key = flip_key(y, r2, c2)
                if (
                    key is not None
                    and key < cur_key
                    and (chosen_key is None or key < chosen_key)
                ):
                    chosen_key, chosen = key, ("flip", (y, r2, c2))
        if chosen is None and ctx is not None:
            # flips stalled — regrow either side's tree around the
            # masked shared corridor (ctx is None only on synthetic
            # fixtures, which carry no tris/pin_vertex and never reach
            # here)
            for a, b in sorted(cur_live):
                shared: set[int] = set()
                for ia1, ia2, ib1, ib2 in pair_regs[(a, b)]:
                    shared |= {int(e) for e in branches[a].edges[ia1 : ia2 + 1]} & {
                        int(e) for e in branches[b].edges[ib1 : ib2 + 1]
                    }
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
            break
        kind, payload = chosen
        if kind == "flip":
            bb, r2, c2 = payload
            mask_tot += _bb_mask_cost(bb, r2, c2) - _bb_mask_cost(
                bb, runs[bb], cuts[bb]
            )
            runs[bb], cuts[bb] = r2, c2
        else:
            branches, runs, cuts, cur_wires, pair_regs = payload
            mask_tot = _state_mask_cost(branches, runs, cuts)
        cur_live = live_pairs(cur_wires, runs, cuts, pair_regs)
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
        tuple(sorted(live_pairs(w_fin, r_fin, c_fin, regs_fin))),
    )


def _edge_xy(mesh: TopoMesh, edge: int, t: float) -> tuple[float, float]:
    """World point at parameter ``t`` along mesh edge ``edge``."""
    u, v = mesh.edges[edge]
    a, b_ = mesh.verts[int(u)], mesh.verts[int(v)]
    return (float(a[0] + t * (b_[0] - a[0])), float(a[1] + t * (b_[1] - a[1])))


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


def _seg_dist(
    xy: tuple[float, float], p: tuple[float, float], q: tuple[float, float]
) -> float:
    """Point-to-segment distance — ``xy`` to ``p→q``."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    d2 = dx * dx + dy * dy
    f = (
        0.0
        if d2 == 0
        else min(1.0, max(0.0, ((xy[0] - p[0]) * dx + (xy[1] - p[1]) * dy) / d2))
    )
    return math.dist(xy, (p[0] + f * dx, p[1] + f * dy))


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


#: A candidate via site's per-outer-layer pad roster: ``(part index,
#: pin-local centre x/y, half w, half h)`` rows, read back into the part
#: frame per candidate (:func:`design._world_to_local` — the inverse of
#: the pose transform, so the rect distance is exact, mirror included).
_Pads = tuple[tuple[int, float, float, float, float], ...]

#: The 2D siting stage's stub-length cap (mm): a via site up to one
#: triangle's reach off the wire polyline joins through a short stub
#: the piece runs already draw (:func:`_piece_run`); past this the jog
#: is a re-thread, not a site, and the branch honestly reports no_via
#: (pcb-topo-place.md §2D — free-point jogs are a separate slice).
_VIA_2D_STUB_CAP_MM = 1.0


def _tri_circumcenter(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
) -> tuple[float, float] | None:
    """The triangle's circumcenter, or None for a (near-)collinear
    triple — the 2D via candidate seeded where the CDT's local Voronoi
    vertex maximizes clearance from every mesh vertex at once."""
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    cx, cy = float(c[0]), float(c[1])
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) <= 1e-12:
        return None
    a2 = ax * ax + ay * ay
    b2 = bx * bx + by * by
    c2 = cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return ux, uy


def _point_in_tri(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> bool:
    """Barycentric-side containment (boundary counts) — an obtuse
    triangle's circumcenter falls outside and is no via site."""
    px, py = float(p[0]), float(p[1])

    def _cross(
        o: tuple[float, float], u: tuple[float, float], v: tuple[float, float]
    ) -> float:
        return (u[0] - o[0]) * (v[1] - o[1]) - (u[1] - o[1]) * (v[0] - o[0])

    d1 = _cross((px, py), a, b)
    d2 = _cross((px, py), b, c)
    d3 = _cross((px, py), c, a)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def _seg_rect_clear(
    a: tuple[float, float],
    b: tuple[float, float],
    pads: _Pads,
    pose: np.ndarray,
    margin: float,
) -> bool:
    """Whether the segment ``a→b`` keeps ``margin`` off every pad rect —
    the 2D via stub's keep-out: the stub is track copper on both new
    runs' layers, so it must clear pads exactly like a drawn leg
    (the pointwise :func:`_pad_clear` alone would let a stub graze a
    pad its endpoint cleared). Exact segment-to-AABB distance in the
    part frame, both endpoints transformed."""
    for part, px, py, hw, hh in pads:
        lax, lay = _world_to_local(a[0], a[1], pose[part])
        lbx, lby = _world_to_local(b[0], b[1], pose[part])
        if (
            _seg_aabb_dist(
                (lax, lay), (lbx, lby), (px - hw, py - hh), (px + hw, py + hh)
            )
            < margin
        ):
            return False
    return True


def _pad_clear(
    x: float, y: float, pads: _Pads, pose: np.ndarray, margin: float
) -> bool:
    """Whether the point keeps ``margin`` off every pad rect in ``pads``
    — the via barrel's keep-out against pad polygons, which
    ``gap_free_mm`` cannot see: its reach is buffered for a trace, not
    a barrel, and −1 ("no pad reaches the edge for a trace") still
    admits a site inside a pad's via keep-out (gr520901's esp32c3
    residual)."""
    for part, px, py, hw, hh in pads:
        lx, ly = _world_to_local(x, y, pose[part])
        dx = max(abs(lx - px) - hw, 0.0)
        dy = max(abs(ly - py) - hh, 0.0)
        if math.hypot(dx, dy) < margin:
            return False
    return True


def _poly_project(
    pts: Sequence[tuple[float, float]], seg: Sequence[float], q: tuple[float, float]
) -> float:
    """Arc length along the polyline of the point nearest ``q``."""
    best_d, best_dist, acc = 0.0, math.inf, 0.0
    for i in range(len(seg)):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        dx, dy = x1 - x0, y1 - y0
        l2 = dx * dx + dy * dy
        u = (
            0.0
            if l2 <= 0.0
            else min(max(((q[0] - x0) * dx + (q[1] - y0) * dy) / l2, 0.0), 1.0)
        )
        dd = math.dist((x0 + u * dx, y0 + u * dy), q)
        if dd < best_dist:
            best_dist, best_d = dd, acc + u * seg[i]
        acc += seg[i]
    return best_d


def _poly_point_at(
    pts: Sequence[tuple[float, float]], seg: Sequence[float], d: float
) -> tuple[float, float]:
    """The polyline point at arc length ``d`` (clamped to the ends)."""
    acc = 0.0
    for i in range(len(seg)):
        if d <= acc + seg[i] or i == len(seg) - 1:
            u = 0.0 if seg[i] <= 0.0 else min(max((d - acc) / seg[i], 0.0), 1.0)
            (x0, y0), (x1, y1) = pts[i], pts[i + 1]
            return x0 + u * (x1 - x0), y0 + u * (y1 - y0)
        acc += seg[i]
    return pts[-1]


def _nearest_edge(mesh: TopoMesh, q: tuple[float, float]) -> tuple[int, float]:
    """The mesh edge (and parameter on it) nearest world point ``q`` —
    the ViaSite's informational edge/t; realize reads x/y only."""
    best, best_d = (0, 0.5), math.inf
    for e in range(len(mesh.edges)):
        u, v = mesh.edges[e]
        (ax, ay), (bx, by) = (
            (float(c[0]), float(c[1])) for c in (mesh.verts[int(u)], mesh.verts[int(v)])
        )
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = (
            0.0
            if l2 <= 0.0
            else min(max(((q[0] - ax) * dx + (q[1] - ay) * dy) / l2, 0.0), 1.0)
        )
        dd = math.dist((ax + t * dx, ay + t * dy), q)
        if dd < best_d:
            best, best_d = (e, t), dd
    return best


def _site_via(
    mesh: TopoMesh,
    b: WirePath,
    ei: int,
    n_layers: int,
    via_dia: float,
    clearance: float,
    placed: list[tuple[float, float, float]],
    pad_sets: tuple[_Pads, _Pads],
    pose: np.ndarray,
    *,
    outline: Any = None,
    keepouts: Sequence[Any] = (),
    fallback_dia: float | None = None,
    foreign: Iterable[tuple[Sequence[tuple[float, float]], float]] = (),
    track_w: float | None = None,
) -> tuple[int, float, float, float, float] | None:
    """One keepout-clean via site near the branch's join index ``ei``
    (topo S3, gr520901), or ``None`` when no point qualifies.

    The candidate space is the branch's taut polyline, walked in ≈0.05 mm
    steps outward from the join anchor. The pre-gr524239 walk demanded
    ONE whole mesh edge hold the via window — and the mesh splits edges
    at pad channels (sub-mm on a real board), so no edge ever qualified
    and the stage minted zero vias on taz-leaf. The window is a property
    of the POINT: it qualifies when the barrel's keep-out
    (``via_dia/2 + clearance``) clears every pad rect on both outer
    layers, every already placed via, the board outline, every
    keepout/mounting-hole zone — the same bodies the mesh keeps wires
    out of, which the old walk never checked (a site could land in a
    keepout or off the board) — and keeps every OTHER net's polyline at
    its own ``(via+track)/2 + clearance``: the barrel spans every layer,
    so a foreign track on ANY layer (inners included) vetoes the site
    (gr520902 s4d).

    ``fallback_dia`` (gr524334): when the net-rule via (the comfortable
    house default, 0.75 mm on 4layer) finds no site anywhere on the
    branch, the walk retries at the fab-minimum diameter — a signal via
    carries no current, and a smaller legal barrel beats an unconnected
    branch. The returned site carries the diameter it was qualified
    against so realize and the emit guard measure the same geometry.

    When the on-wire walk finds nothing at a diameter, the 2D stage
    (``walk2d`` below — the join edge's adjacent-triangle
    circumcenters, pcb-topo-place.md §2D) gets the same diameter next:
    a qualifying site one triangle's reach off the centreline joins by
    the short stub the piece machinery already draws, before the walk
    degrades to the smaller barrel. ``track_w`` sizes that stub's
    clearance (the stub is track copper); callers with the net rule at
    hand should pass it, the legacy default prices the stub at the
    barrel radius (conservative).
    """
    track_w = float(track_w) if track_w is not None else via_dia / 2.0
    pts = [(float(x), float(y)) for x, y in b.points]
    if len(pts) < 2 or not len(b.edges):
        return None
    ej = min(max(int(ei), 0), len(b.edges) - 1)
    anchor = _edge_xy(mesh, int(b.edges[ej]), float(b.t[ej]) if ej < len(b.t) else 0.5)
    seg = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    total = sum(seg)
    if total <= 0.0:
        return None
    anchor_d = _poly_project(pts, seg, anchor)

    def qualifies(x: float, y: float, dia: float, margin: float) -> bool:
        """The barrel keep-out at one point — every body the 1D walk
        has always checked, shared by the on-wire walk and the 2D
        stage so both site against identical geometry."""
        if any(
            math.dist((x, y), (px, py)) < (dia + pd) / 2.0 + clearance
            for px, py, pd in placed
        ):
            return False
        if not all(_pad_clear(x, y, pads, pose, margin) for pads in pad_sets):
            return False
        pt = Point(x, y)
        # inside the board AND the barrel keep-out off the outline
        # — a polygon's distance() to an interior point is 0, so
        # the boundary is what measures edge clearance.
        if outline is not None and (
            not outline.covers(pt) or outline.boundary.distance(pt) < margin
        ):
            return False
        if any(ko.distance(pt) < margin for ko in keepouts):
            return False
        if any(
            _seg_dist((x, y), p, q) < m
            for fpts, m in foreign
            for p, q in itertools.pairwise(fpts)
        ):
            return False
        e, _t = _nearest_edge(mesh, (x, y))
        if mesh.constrained[e]:
            return False  # a constrained ring edge never holds a via
        # the barrel's keep-out off the edge's endpoint
        # vertices — the graze-at-a-vertex case (gr520901)
        if any(
            math.dist(
                (x, y),
                (float(mesh.verts[int(v)][0]), float(mesh.verts[int(v)][1])),
            )
            < margin
            for v in mesh.edges[e]
        ):
            return False
        return True

    def walk(dia: float) -> tuple[int, float, float, float] | None:
        margin = dia / 2.0 + clearance
        step = 0.05
        for k in range(int(total / step) + 1):
            for s in (0.0,) if k == 0 else (k * step, -k * step):
                d = anchor_d + s
                if d < 0.0 or d > total:
                    continue
                x, y = _poly_point_at(pts, seg, d)
                if not qualifies(x, y, dia, margin):
                    continue
                e, t = _nearest_edge(mesh, (x, y))
                return e, t, x, y
        return None

    def walk2d(dia: float) -> tuple[int, float, float, float] | None:
        """The 2D siting stage (pcb-topo-place.md §2D, gr524334): when
        the on-wire walk finds nothing, try the finite candidate set
        off the centreline — the JOIN EDGE's adjacent-triangle
        circumcenters. A circumcenter is the CDT's local Voronoi
        vertex: the point farthest from every mesh vertex at once, so
        in a channel jammed on the wire it is where a legal barrel
        most likely lives, one triangle's reach from the polyline.
        The piece machinery already joins an off-path via by its
        short stub (:func:`_piece_run`), so a qualifying circumcenter
        mints a site where the 1D walk honestly reported none — the
        stub itself is vetted like a drawn leg (pad rects on both
        outers, zones, the outline, foreign tracks, placed vias) at
        track width, and capped at :data:`_VIA_2D_STUB_CAP_MM`."""
        margin = dia / 2.0 + clearance
        stub_margin = track_w / 2.0 + clearance

        def _edge_arc(k: int) -> float:
            e = int(b.edges[k])
            t = float(b.t[k]) if k < len(b.t) else 0.5
            return _poly_project(pts, seg, _edge_xy(mesh, e, t))

        # the crossed edges nearest the join anchor first — a site far
        # from the join still splits correctly (the piece builder sorts
        # via arcs, gr520902 a.2) but the nearer site keeps the join
        # where the yielder wanted it
        for ek in sorted(
            range(len(b.edges)), key=lambda k: abs(_edge_arc(k) - anchor_d)
        ):
            e0 = int(b.edges[ek])
            seen: set[int] = set()
            for t in (int(x) for x in mesh.edge_tris[e0]):
                if t in seen or t < 0 or t >= len(mesh.tris):
                    continue
                seen.add(t)
                tri = mesh.tris[t]
                a, bb, c = (
                    (float(mesh.verts[int(v)][0]), float(mesh.verts[int(v)][1]))
                    for v in tri
                )
                cc = _tri_circumcenter(a, bb, c)
                if cc is None or not _point_in_tri(cc, a, bb, c):
                    continue  # collinear, or obtuse — the center falls outside
                s_arc = _poly_project(pts, seg, cc)
                proj = _poly_point_at(pts, seg, s_arc)
                stub = math.dist(cc, proj)
                if stub > _VIA_2D_STUB_CAP_MM or stub <= 1e-9:
                    continue
                if not qualifies(*cc, dia, margin):
                    continue
                sline = LineString([cc, proj])
                if any(
                    sline.distance(Point(px, py)) < (dia + pd) / 2.0 + clearance
                    for px, py, pd in placed
                ):
                    continue
                if not all(
                    _seg_rect_clear(cc, proj, pads, pose, stub_margin)
                    for pads in pad_sets
                ):
                    continue
                if any(ko.distance(sline) < stub_margin for ko in keepouts):
                    continue
                if outline is not None and (
                    not outline.covers(sline)
                    or outline.boundary.distance(sline) < stub_margin
                ):
                    continue
                if any(
                    _seg_seg_dist(cc, proj, p, q) < m
                    for fpts, m in foreign
                    for p, q in itertools.pairwise(fpts)
                ):
                    continue
                e, tt = _nearest_edge(mesh, cc)
                return e, tt, float(cc[0]), float(cc[1])
        return None

    dia = float(via_dia)
    site = walk(dia)
    if site is None:
        site = walk2d(dia)
    if site is None and fallback_dia is not None and fallback_dia < via_dia:
        dia = float(fallback_dia)
        site = walk(dia)
        if site is None:
            site = walk2d(dia)
    if site is None:
        return None
    e, t, x, y = site
    return e, t, x, y, dia


def _seg_box_interval(
    a: tuple[float, float],
    b: tuple[float, float],
    lo: np.ndarray,
    hi: np.ndarray,
) -> tuple[float, float] | None:
    """Parameter interval of segment ``a→b`` inside the AABB ``[lo, hi]``."""
    ent, ext = 0.0, 1.0
    for ax in (0, 1):
        dax = b[ax] - a[ax]
        if abs(dax) <= 1e-12:
            if a[ax] < lo[ax] - 1e-12 or a[ax] > hi[ax] + 1e-12:
                return None
            continue
        t0 = (lo[ax] - a[ax]) / dax
        t1 = (hi[ax] - a[ax]) / dax
        ent = max(ent, min(t0, t1))
        ext = min(ext, max(t0, t1))
        if ent >= ext - 1e-12:
            return None
    return (ent, ext)


def _free_span(
    a: tuple[float, float],
    b: tuple[float, float],
    boxes: list[tuple[np.ndarray, np.ndarray]],
) -> tuple[float, float]:
    """The largest parameter interval of ``a→b`` left uncovered by
    ``boxes`` — the stretch the spoke model may spread crossings over
    (``gap_free_mm``'s position, recomputed)."""
    ints = sorted(
        iv
        for iv in (_seg_box_interval(a, b, lo, hi) for lo, hi in boxes)
        if iv is not None
    )
    # ``best`` must start EMPTY — seeded at the whole segment it can
    # never be beaten, and the spread then packs crossings inside the
    # buffered pad boxes the span was meant to exclude (gr520900).
    best, cur = (0.0, 0.0), 0.0
    for ent, ext in ints:
        ent = max(ent, cur)
        if ent > cur and ent - cur > best[1] - best[0]:
            best = (cur, ent)
        cur = max(cur, ext)
    if 1.0 - cur > best[1] - best[0]:
        best = (cur, 1.0)
    return best


def _seg_seg_dist(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> float:
    """Distance between segments ``p1→p2`` and ``p3→p4`` (closest-points-
    on-segments, Ericson 5.1.9 — pure floats, no geometry objects: this
    sits inside the relaxer's per-evaluation cost)."""
    d1x, d1y = p2[0] - p1[0], p2[1] - p1[1]
    d2x, d2y = p4[0] - p3[0], p4[1] - p3[1]
    rx, ry = p1[0] - p3[0], p1[1] - p3[1]
    a = d1x * d1x + d1y * d1y
    e = d2x * d2x + d2y * d2y
    f = d2x * rx + d2y * ry
    if a <= 1e-12 and e <= 1e-12:
        return math.hypot(rx, ry)
    if a <= 1e-12:
        s, t = 0.0, min(1.0, max(0.0, f / e))
    elif e <= 1e-12:
        c_ = d1x * rx + d1y * ry
        s, t = min(1.0, max(0.0, -c_ / a)), 0.0
    else:
        b = d1x * d2x + d1y * d2y
        c_ = d1x * rx + d1y * ry
        den = a * e - b * b
        s = 0.0 if den <= 1e-12 else min(1.0, max(0.0, (b * f - c_ * e) / den))
        t = (b * s + f) / e
        if t < 0.0:
            t = 0.0
            s = min(1.0, max(0.0, -c_ / a))
        elif t > 1.0:
            t = 1.0
            s = min(1.0, max(0.0, (b - c_) / a))
    cx = p1[0] + s * d1x - (p3[0] + t * d2x)
    cy = p1[1] + s * d1y - (p3[1] + t * d2y)
    return math.hypot(cx, cy)


def _seg_aabb_dist(
    a: tuple[float, float],
    b: tuple[float, float],
    lo: tuple[float, float],
    hi: tuple[float, float],
) -> float:
    """Distance between segment ``a→b`` and the axis-aligned box
    ``[lo, hi]`` — 0 when the segment touches or crosses it."""
    if (lo[0] <= a[0] <= hi[0] and lo[1] <= a[1] <= hi[1]) or (
        lo[0] <= b[0] <= hi[0] and lo[1] <= b[1] <= hi[1]
    ):
        return 0.0
    c1 = (lo[0], lo[1])
    c2 = (hi[0], lo[1])
    c3 = (hi[0], hi[1])
    c4 = (lo[0], hi[1])
    return min(
        _seg_seg_dist(a, b, c1, c2),
        _seg_seg_dist(a, b, c2, c3),
        _seg_seg_dist(a, b, c3, c4),
        _seg_seg_dist(a, b, c4, c1),
    )


#: gr520900 fix round 1 — per-cluster relaxation caps. The joint
#: relaxation solves ONE Powell per connected cluster of violating
#: branches, never the whole board: a cluster with more free interior
#: vertices than this is skipped entirely (the pre-relaxation output
#: stands, exactly as a refused acceptance leaves it), because one
#: unbounded solve over thousands of coordinates was the scalability
#: bug (minutes on a 200+ branch board with one tight slit).
_RELAX_FREE_VERTEX_CAP = 60
#: maxfev budget per cluster solve — 200 per free coordinate, capped
#: absolutely. maxiter alone never bounded Powell's function
#: evaluations (each sweep runs 2N line searches), so a tight board
#: could run effectively forever; maxfev caps the work directly.
_RELAX_MAXFEV_PER_COORD = 200
_RELAX_MAXFEV_ABS = 20000
#: The relax aims a hair ABOVE each hinge's strict bound — the emit
#: guard measures a strict ``<``, and fp noise plus the octilinear
#: redraw can dip a boundary-exact solve inside it (gr537726: the
#: 3-pair slit relaxed to 0.2998/0.2248 against 0.3/0.225 and was
#: withdrawn whole).
_RELAX_SLACK_MM = 0.005
#: The shared-gap unwind's displacement cap, as a multiple of the
#: pair's gap — a vertex whose slot-side target sits further than this
#: is a weave, not a fan the unwind can lift; the move is skipped and
#: the relax/emit guard owns the vertex instead (gr537831 review: an
#: unbounded push could drag a leg across the board).
_UNWIND_MOVE_CAP = 4.0


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
    with the wires that share an obstacle vertex or an edge crossing
    fanned out to legal spacing.

    The taut funnel bends every wire at the SAME mesh vertex (pad
    centres and pad-ring corners are the obstacles the corridor wraps)
    and threads every shared edge at whatever parameter the legs
    happen to take — so two wires through one gap can leave the
    realiser coincident copper (0.000 mm clearance). For each vertex
    more than one wire wraps, the wraps are ranked by ``wires.order``
    on a shared spoke edge — nearest-to-vertex first, the tighter wrap
    — and each bend is moved out along the bend's bisector by the pad
    margin plus the sum of the inner wires' ``(width + clearance)``
    slots (``sum(w+s) + w/2 + s`` off the pad obstacle). Interior
    crossings of one edge are spread inside the edge's free stretch
    (``gap_free_mm``'s complement, recomputed) at pairwise
    ``(w_i+w_j)/2 + clearance`` spacing, in ``wires.order`` order; a
    moved crossing becomes a polyline vertex on that edge, which keeps
    the wire's homotopy verbatim — each new leg stays inside the
    corridor tri it was threaded through. Same-net wires still
    separate — a braid's copper may not overlap itself."""
    from precis.pcb.topoplace.proxy import zone_world_ring
    from precis.pcb.topoplace.wires import guard_pad_rects

    rules = net_rules or {}
    pose_a = np.asarray(d.pose0 if pose is None else pose)
    # Authored keepout/mounting/fixed-copper zones as world-frame
    # polygons — leg-clearance candidates for the bend scan below.
    # label_keepout is VIA-only (silk over traces): the mesh already
    # ignores it and so does the bend pass.
    zone_polys = [
        (Polygon(np.asarray(zone_world_ring(d, z, pose_a))), z)
        for z in d.zones
        if z.kind != "label_keepout"
    ]

    def nrules(net: int) -> NetRules:
        name = str(d.net_name[net]) if net < len(d.net_name) else str(net)
        return rules.get(name, _DEFAULT)

    def ws(bi: int) -> tuple[float, float]:
        nr = nrules(wires.branches[bi].net)
        return float(nr.track_width_mm), float(nr.clearance_mm)

    pads = guard_pad_rects(mesh, d, n_layers, pose_a)
    vid_of = {
        (round(float(x), 6), round(float(y), 6)): i
        for i, (x, y) in enumerate(mesh.verts)
    }
    eu = mesh.verts[mesh.edges[:, 0]]
    ev = mesh.verts[mesh.edges[:, 1]]
    tri_verts = [tuple(int(v) for v in t) for t in mesh.tris]
    cent = [tuple(mesh.verts[list(t)].mean(axis=0)) for t in tri_verts]
    spokes: dict[int, list[int]] = {}
    for e, (u, v) in enumerate(mesh.edges):
        spokes.setdefault(int(u), []).append(e)
        spokes.setdefault(int(v), []).append(e)

    # ---- interior crossings: spread shared-edge crossings over the
    # free span when their natural parameters violate pairwise spacing
    # — BEFORE the bend offsets: a moved crossing becomes a polyline
    # vertex and the bend solve must see the legs it actually splits
    edge_pos = [{int(e): i for i, e in enumerate(b.edges)} for b in wires.branches]
    moved: dict[int, dict[int, tuple[float, float]]] = {}
    for e in range(len(mesh.edges)):
        inner_hits = [
            (bi, edge_pos[bi][e], float(wires.branches[bi].t[edge_pos[bi][e]]))
            for bi in wires.order[e]
            if e in edge_pos[bi]
            and 1e-9 < wires.branches[bi].t[edge_pos[bi][e]] < 1.0 - 1e-9
        ]
        if not inner_hits:
            continue
        a_, b_ = eu[e], ev[e]
        seglen = float(np.hypot(*(b_ - a_)))
        if seglen <= 1e-9:
            continue
        seq_bi = [h[0] for h in inner_hits]
        rules_t = [ws(bi) for bi in seq_bi]
        m_max = max(w / 2.0 + s for w, s in rules_t)
        boxes = [
            (pads.lo[pi] - m_max, pads.hi[pi] + m_max)
            for pi in range(len(pads.lo))
            if _seg_box_interval(
                tuple(a_), tuple(b_), pads.lo[pi] - m_max, pads.hi[pi] + m_max
            )
            is not None
            and not (
                0 <= int(pads.pin[pi]) < len(d.pin_net)
                and int(d.pin_net[int(pads.pin[pi])])
                in {wires.branches[bi].net for bi in seq_bi}
            )
        ]
        lo_t, hi_t = _free_span(tuple(a_), tuple(b_), boxes)
        nat = [h[2] for h in inner_hits]
        ok = all(lo_t <= t <= hi_t for t in nat)
        for k in range(len(nat) - 1):
            need = (rules_t[k][0] + rules_t[k + 1][0]) / 2.0 + max(
                rules_t[k][1], rules_t[k + 1][1]
            )
            if (nat[k + 1] - nat[k]) * seglen < need - 1e-9:
                ok = False
        if ok:
            continue
        reqs = [
            (rules_t[k][0] + rules_t[k + 1][0]) / 2.0
            + max(rules_t[k][1], rules_t[k + 1][1])
            for k in range(len(seq_bi) - 1)
        ]
        p = lo_t * seglen + max(0.0, ((hi_t - lo_t) * seglen - sum(reqs)) / 2.0)
        for k, (bi, i, _t) in enumerate(inner_hits):
            t_new = min(max(p / seglen, 0.0), 1.0)
            moved.setdefault(bi, {})[i] = (
                float(a_[0] + t_new * (b_[0] - a_[0])),
                float(a_[1] + t_new * (b_[1] - a_[1])),
            )
            if k < len(reqs):
                p += reqs[k]

    # ---- insert the moved crossings (each inserted point sits on its
    # edge, so the legs between consecutive vertices stay inside the
    # corridor's tris — homotopy kept); bends then solve on the REAL
    # neighbour legs of this polyline.
    pts_out: list[list[tuple[float, float]]] = []
    for bi, b in enumerate(wires.branches):
        mv = moved.get(bi)
        if not mv:
            pts_out.append([(float(x), float(y)) for x, y in b.points])
            continue
        by_leg: dict[int, list[tuple[float, tuple[float, float], int]]] = {}
        for i, pt in mv.items():
            e = int(b.edges[i])
            t = float(b.t[i])
            q = (
                float(eu[e][0] + t * (ev[e][0] - eu[e][0])),
                float(eu[e][1] + t * (ev[e][1] - eu[e][1])),
            )
            bj, bs, bd = 0, 0.0, math.inf
            for j in range(len(b.points) - 1):
                dd = _seg_dist(q, b.points[j], b.points[j + 1])
                if dd < bd:
                    bd, bj = dd, j
                    dx = b.points[j + 1][0] - b.points[j][0]
                    dy = b.points[j + 1][1] - b.points[j][1]
                    bs = (
                        (q[0] - b.points[j][0]) * dx + (q[1] - b.points[j][1]) * dy
                    ) / max(dx * dx + dy * dy, 1e-12)
            by_leg.setdefault(bj, []).append((bs, pt, e))
        newp = [(float(b.points[0][0]), float(b.points[0][1]))]
        for j in range(len(b.points) - 1):
            for _s, pt, e in sorted(by_leg.get(j, [])):
                newp.append(pt)
            newp.append((float(b.points[j + 1][0]), float(b.points[j + 1][1])))
        pts_out.append(newp)

    # ---- bend offsets: wires wrapping one obstacle vertex fan by rank.
    # Every bend that coincides with a mesh vertex qualifies for the
    # clearance scan below — the scan itself, not vertex class, decides
    # whether anything must move: no candidate pad or zone claims the
    # bend → it stays verbatim. That replaces the old mandatory
    # no-candidates shift, which shoved bends around with nothing to
    # clear — the micro-wall fixture's free-vertex bend slid 0.225 mm
    # along its bisector INTO a keepout wall (0.042 mm graze, copper
    # protruding 0.033 mm into the zone).
    wraps: dict[int, list[tuple[int, int]]] = {}
    bend_tasks: list[
        tuple[
            int,
            int,
            np.ndarray,
            np.ndarray,
            float,
            list[tuple[Any, Any, float]],
            list[tuple[Any, float]],
        ]
    ] = []
    for bi, mid in enumerate(pts_out):
        for j in range(1, len(mid) - 1):
            v = vid_of.get((round(mid[j][0], 6), round(mid[j][1], 6)))
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
        if not lst:
            continue
        bis = {bi for bi, _ in lst}
        seq: list[int] = []
        for e in spokes.get(vid, ()):
            hits = [bi for bi in wires.order[e] if bi in bis]
            if len(hits) >= 2:
                seq = hits if int(mesh.edges[e, 0]) == vid else hits[::-1]
                break
        if not seq:
            seq = [
                bi
                for bi, _ in sorted(
                    lst, key=lambda k: (tightness(k[0], k[1]), k[0], k[1])
                )
            ]
        # a vertex wrapped by one wire once or by a braid keeps the
        # taut bend — offsets only fan COINCIDENT wraps of distinct
        # branches; the bend scan below then clears each bend of the
        # pads and zone keepouts its legs actually reach, in whichever
        # direction clears them.
        rank = {
            bi: r for r, bi in enumerate(dict.fromkeys(seq + [bi for bi, _ in lst]))
        }
        w = np.asarray(mesh.verts[vid], dtype=float)
        for bi, j in lst:
            r = rank.get(bi, 0)
            w_k, s_k = ws(bi)
            inner = sum(
                float(nrules(wires.branches[ob].net).track_width_mm)
                + max(s_k, float(nrules(wires.branches[ob].net).clearance_mm))
                for ob in seq[:r]
                if ob != bi
            )
            need = s_k + w_k / 2.0 + inner
            own_net = wires.branches[bi].net
            # The foreign pads this vertex sits on (a pad corner, a
            # ring-edge vertex, a pad centre) — the spoke direction
            # pushes AWAY from each — plus the foreign pads the bend
            # could drift toward, which claim only the plain margin.
            # The legs' bisector of a convex-corner wrap points INTO
            # the pad face: the wire's corridor wedges on the reflex
            # side.
            cands: list[tuple[Any, Any, float]] = []
            pushes: list[np.ndarray] = []
            for pi in range(len(pads.lo)):
                pin = int(pads.pin[pi])
                if 0 <= pin < len(d.pin_net) and int(d.pin_net[pin]) == own_net:
                    continue  # the wire's own copper — never an obstacle
                plo, phi = pads.lo[pi], pads.hi[pi]
                on_box = (
                    plo[0] - 1e-9 <= w[0] <= phi[0] + 1e-9
                    and plo[1] - 1e-9 <= w[1] <= phi[1] + 1e-9
                )
                near = (
                    plo[0] - need - 0.6 <= w[0] <= phi[0] + need + 0.6
                    and plo[1] - need - 0.6 <= w[1] <= phi[1] + need + 0.6
                )
                if not near:
                    continue
                inside = plo[0] < w[0] < phi[0] and plo[1] < w[1] < phi[1]
                # a pad holding the vertex claims the full spoke margin;
                # another pad the bend may drift toward needs only the
                # plain centreline clearance — and only the held pads
                # steer the push, a pad merely nearby must not bend the
                # spoke off its arc
                cands.append((plo, phi, need if on_box else s_k + w_k / 2.0))
                if on_box and not inside:
                    v = w - (plo + phi) / 2.0
                    nv = float(np.hypot(*v))
                    if nv > 1e-9:
                        pushes.append(v / nv)
            bpts = pts_out[bi]
            a = np.asarray(bpts[j - 1], dtype=float) - w
            bb = np.asarray(bpts[j + 1], dtype=float) - w
            la, lb = float(np.hypot(*a)), float(np.hypot(*bb))
            dirv = None
            if pushes:
                s_ = np.sum(np.asarray(pushes), axis=0)
                ns = float(np.hypot(*s_))
                if ns > 1e-9:
                    dirv = s_ / ns
            if dirv is None and la > 1e-9 and lb > 1e-9:
                dv = a / la + bb / lb
                dn = float(np.hypot(*dv))
                if dn > 1e-9:
                    dirv = dv / dn
            if dirv is None:
                leg = bb / lb if lb > 1e-9 else np.asarray([1.0, 0.0])
                perp = np.asarray([-leg[1], leg[0]])
                cents = [
                    cent[t] for t in wires.branches[bi].tris if vid in tri_verts[t]
                ]
                if cents:
                    c = np.mean(np.asarray(cents), axis=0) - w
                    s = float(perp @ c)
                    if abs(s) > 1e-12:
                        perp = perp * math.copysign(1.0, s)
                dirv = perp
            # zone keepouts the two LEGS could reach — judged by bbox
            # overlap with the legs themselves, NOT a vertex-distance
            # gate: a long leg tilts under a small bend move (~0.2 mm
            # over 18 mm here), so a wall 4 mm from the bend still
            # claims the leg. The same-net fixed-copper zone a wire may
            # pass (TopoZone.net) is never an obstacle.
            req_z = s_k + w_k / 2.0
            net_name = (
                str(d.net_name[own_net]) if own_net < len(d.net_name) else str(own_net)
            )
            lx0 = min(float(w[0]), bpts[j - 1][0], bpts[j + 1][0]) - req_z
            lx1 = max(float(w[0]), bpts[j - 1][0], bpts[j + 1][0]) + req_z
            ly0 = min(float(w[1]), bpts[j - 1][1], bpts[j + 1][1]) - req_z
            ly1 = max(float(w[1]), bpts[j - 1][1], bpts[j + 1][1]) + req_z
            leg_zones: list[tuple[Any, float]] = []
            for zp, z in zone_polys:
                if z.net and z.net == net_name:
                    continue
                zx0, zy0, zx1, zy1 = zp.bounds
                if zx1 < lx0 or zx0 > lx1 or zy1 < ly0 or zy0 > ly1:
                    continue
                leg_zones.append((zp, req_z))
            bend_tasks.append((bi, j, w, dirv, need, cands, leg_zones))

    # the smallest step along each bend's direction that puts the bend
    # AND both adjacent legs clear of every candidate — pads (the
    # near-gate collected them) and zone keepouts (the leg-bbox gate) —
    # offsetting the vertex alone can leave a leg grazing the box, and a
    # moved neighbour changes a leg, so the pass runs twice. No
    # candidate at all: the taut bend stays verbatim — nothing has
    # claimed it. When the preferred ray never clears (an obstacle
    # blocks the bisector side), the opposite ray is tried before the
    # legacy need-length fallback: the fallback direction is a guess and
    # may be the side the obstacle sits on.
    for _pass in range(2):
        for bi, j, w, dirv, need, cands, leg_zones in bend_tasks:
            boxes = [
                (
                    Polygon(
                        np.asarray(
                            [
                                [plo[0], plo[1]],
                                [phi[0], plo[1]],
                                [phi[0], phi[1]],
                                [plo[0], phi[1]],
                            ]
                        )
                    ),
                    req,
                )
                for plo, phi, req in cands
            ]
            boxes.extend(leg_zones)
            if not boxes:
                continue

            def clears(
                t: float,
                bi: int = bi,
                j: int = j,
                w: np.ndarray = w,
                dirv: np.ndarray = dirv,
                boxes: list = boxes,
            ) -> bool:
                q = (float(w[0] + t * dirv[0]), float(w[1] + t * dirv[1]))
                legs = (
                    LineString([pts_out[bi][j - 1], q]),
                    LineString([q, pts_out[bi][j + 1]]),
                )
                for bx, req in boxes:
                    if legs[0].distance(bx) < req - 1e-9:
                        return False
                    if legs[1].distance(bx) < req - 1e-9:
                        return False
                return True

            t_found = None
            for t in np.linspace(0.0, need * 8.0 + 1.0, 96):
                if clears(float(t)):
                    t_found = float(t) + 0.01
                    break
            if t_found is None:
                # the preferred ray is blocked all the way (an obstacle
                # sits on the bisector side): try the opposite ray
                # before the legacy need-length fallback — the fallback
                # direction is a guess and may be the blocked side
                # (a keepout wall behind the bend's back)
                for t in np.linspace(0.0, need * 8.0 + 1.0, 96):
                    if clears(-float(t)):
                        t_found = -float(t) - 0.01
                        break
            dist = t_found if t_found is not None else need
            pts_out[bi][j] = (
                float(w[0] + dist * dirv[0]),
                float(w[1] + dist * dirv[1]),
            )

    # ---- joint relaxation (topo S2, gr520900): the spread and the bend
    # pass only ever asked a leg to clear PADS; a moved crossing can
    # still sit inside a pad's margin ring or on another wire's slot —
    # both emit shorting copper. The former one-vertex-at-a-time leg
    # sweep against frozen neighbours stuck in local minima (it shoved
    # b2 over the slit top on the shared-gap/shared-corner fixture —
    # proven local minimum, hinge cost 14.65). gr520900 fix round 1:
    # the first joint cut ran ONE Powell pass over ALL interior
    # vertices, so a single violating leg paid for every branch on the
    # board — each cost evaluation rebuilt every polyline and swept
    # every pad/pair candidate, and with maxiter the only cap each
    # sweep ran 2N line searches with maxfev unset. A 200+ branch
    # board with one tight slit was one N-in-the-thousands solve for
    # minutes. Now: find the legs whose hinge terms violate at the
    # seed, grow each into the connected component of the interaction
    # graph seeded there (pair_gap adjacency — a pad margin or pair
    # gap's reach), and run ONE small bounded Powell per cluster with
    # only that cluster's vertices free; everything else stays frozen.
    # Clusters process in a fixed order (least branch id first) and
    # per-cluster pad/pair candidate lists are precomputed once,
    # outside the cost closure. Cost = sum of squared hinge terms —
    # leg→foreign pad against s+w/2; leg→leg for different nets
    # against (w_i+w_j)/2+max(s), braided pairs skipped (real
    # crossings, resolved by layer assignment — no slide can separate
    # them) — plus 0.01·|x−x0|² to stay near the seed. Bounds ±0.8 mm
    # per coordinate keep each vertex inside corridor reach (±0.6 was
    # tested and stalls a hair short of the 0.3 pair gap on this
    # fixture); Powell is deterministic, and each solve is bounded by
    # maxfev (200 per free coordinate, capped absolutely). A cluster
    # past :data:`_RELAX_FREE_VERTEX_CAP` free vertices is skipped and
    # keeps the old output. Each cluster's relaxed result is accepted
    # ONLY if no leg intersects a foreign pad polygon and no
    # non-braided pair of polylines crosses — otherwise that cluster's
    # pre-relaxation output stands; the emit guard remains the judge.
    braided = _crossing_pairs(
        wires,
        _cdirs(mesh, wires),
        obstacle_vertices(mesh, d, n_layers),
        [(int(u), int(v)) for u, v in mesh.edges],
    )
    pads_poly = [
        Polygon(
            np.asarray(
                [
                    [pads.lo[pi][0], pads.lo[pi][1]],
                    [pads.hi[pi][0], pads.lo[pi][1]],
                    [pads.hi[pi][0], pads.hi[pi][1]],
                    [pads.lo[pi][0], pads.hi[pi][1]],
                ]
            )
        )
        for pi in range(len(pads.lo))
    ]

    def foreign_pads(bi: int) -> list[int]:
        own = wires.branches[bi].net
        return [
            pi
            for pi in range(len(pads.lo))
            if not (
                0 <= int(pads.pin[pi]) < len(d.pin_net)
                and int(d.pin_net[int(pads.pin[pi])]) == own
            )
        ]

    interior = [
        (bi, j) for bi, pts in enumerate(pts_out) for j in range(1, len(pts) - 1)
    ]
    if interior:
        from scipy.optimize import Bounds, minimize

        bound = 0.8
        # bbox reach prefilters — a leg endpoint can move at most
        # bound·sqrt(2); a pad or another branch beyond the inflated
        # hull can never be violated and is left out of the cost
        max_move = bound * math.sqrt(2.0) + 0.05
        hull = [
            (
                min(p[0] for p in pts),
                min(p[1] for p in pts),
                max(p[0] for p in pts),
                max(p[1] for p in pts),
            )
            for pts in pts_out
        ]
        pad_req = {bi: ws(bi)[1] + ws(bi)[0] / 2.0 for bi in range(len(pts_out))}
        near_pads = {}
        for bi in range(len(pts_out)):
            infl = pad_req[bi] + max_move
            near_pads[bi] = [
                pi
                for pi in foreign_pads(bi)
                if not (
                    pads.hi[pi][0] < hull[bi][0] - infl
                    or pads.lo[pi][0] > hull[bi][2] + infl
                    or pads.hi[pi][1] < hull[bi][1] - infl
                    or pads.lo[pi][1] > hull[bi][3] + infl
                )
            ]
        pair_gap: dict[tuple[int, int], float] = {}
        for i in range(len(pts_out)):
            w_i, s_i = ws(i)
            for j in range(i + 1, len(pts_out)):
                if wires.branches[i].net == wires.branches[j].net or (i, j) in braided:
                    continue
                w_j, s_j = ws(j)
                gap = (w_i + w_j) / 2.0 + max(s_i, s_j)
                infl = gap + 2.0 * max_move
                if not (
                    hull[i][2] < hull[j][0] - infl
                    or hull[j][2] < hull[i][0] - infl
                    or hull[i][3] < hull[j][1] - infl
                    or hull[j][3] < hull[i][1] - infl
                ):
                    pair_gap[(i, j)] = gap

        # ── shared-gap unwind (gr537726) ─────────────────────────────
        # The spread + bend seed can still interleave a NON-braided
        # pair: the taut funnel wraps FREE mesh vertices, so a wire's
        # interior vertices land on the far side of a peer's lane and
        # the polylines cross. The bounded cluster solve cannot cross
        # that barrier — escaping means moving a vertex THROUGH the
        # peer's polyline, which the hinge cost never rewards — so the
        # relax stalls touching (the 3-pair slit fan ended
        # 0.0/0.105/0.0) and the emit guard withdraws the nets. Unwind
        # globally before the local relax: the plan's order gives each
        # pair a slot side (majority normalized sign over the shared
        # edges both cross — a lone free-vertex wedge flip does not
        # outvote it); the +side wire is pushed outward, re-placing
        # every interior vertex on the wrong side of the peer's
        # polyline at the peer's closest point plus the gap along the
        # slot-side normal — a vertex already on its slot side is the
        # local relax's own remit, not the unwind's. A pair whose
        # own endpoints straddle the peer is a real weave, not a fan
        # the unwind can lift — it is left to the relax/guard. Two
        # sweeps: a pushed vertex can newly crowd the next pair.
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
                        max(
                            ((p[0] - a_[0]) * dx + (p[1] - a_[1]) * dy) / l2,
                            0.0,
                        ),
                        1.0,
                    )
                )
                dd = math.dist(p, (a_[0] + u * dx, a_[1] + u * dy))
                if dd < d_out:
                    d_out = dd
                    q_out = (a_[0] + u * dx, a_[1] + u * dy)
                    dir_out = (dx, dy)
            return q_out, dir_out, d_out

        def _pad_intr(pnt: tuple[float, float], bi: int) -> float:
            """Deepest foreign-pad margin intrusion of a POINT — the
            unwind never pushes a vertex deeper into a pad than it
            already sits (the relax's pad hinges own those cases)."""
            req = pad_req[bi]
            worst = 0.0
            for pi in near_pads[bi]:
                dx = max(
                    float(pads.lo[pi][0]) - pnt[0],
                    pnt[0] - float(pads.hi[pi][0]),
                    0.0,
                )
                dy = max(
                    float(pads.lo[pi][1]) - pnt[1],
                    pnt[1] - float(pads.hi[pi][1]),
                    0.0,
                )
                worst = max(worst, req - math.hypot(dx, dy))
            return worst

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

        edge_sets = [set(ep) for ep in edge_pos]
        unwind_cdirs = _cdirs(mesh, wires)
        # the displaced-vertex checks' statics: blocking keep-out rings
        # (same-net exempt, layer-masked like the wire builder's
        # zone_block) and the board outline.
        unwind_zones: list[tuple[Any, Any]] = []
        for z in d.zones:
            ring = np.asarray(topo_proxy.zone_world_ring(d, z, pose_a))
            if len(ring) < 3:
                continue
            zp = Polygon(ring)
            if not zp.is_empty and zp.area > 0.0:
                unwind_zones.append((z, zp))
        outline_poly = (
            Polygon(np.asarray(d.outline, dtype=float)) if len(d.outline) >= 3 else None
        )

        def _unwind_ok(bi: int, jj: int, cand: tuple[float, float]) -> bool:
            """The accept()-style pass a displaced vertex's two NEW legs
            must clear before the unwind may commit them — foreign pad
            margins, blocking keep-out rings, the board outline and
            every non-braided other polyline (``accept``'s own terms). A
            move that fails keeps its old vertex."""
            mpts = pts_out[bi]
            own = wires.branches[bi].net
            own_name = str(d.net_name[own]) if own < len(d.net_name) else ""
            cand_layers = (
                set(wires.net_layers[own])
                if own < len(wires.net_layers)
                else set(range(n_layers))
            )
            legs = (
                LineString([mpts[jj - 1], cand]),
                LineString([cand, mpts[jj + 1]]),
            )
            old_legs = (
                LineString([mpts[jj - 1], mpts[jj]]),
                LineString([mpts[jj], mpts[jj + 1]]),
            )
            # every check compares against the OLD legs: a move may
            # keep a violation it already carried (another pair's sweep
            # or the relax owns it — a leg lifting OFF a pad corner is
            # exactly the move the unwind exists for) but never mints a
            # fresh one — and never DEEPENS one: per foreign pad the new
            # legs' margin intrusion (clearance + half-width minus
            # leg-to-pad distance, clamped at 0 — a leg still outside
            # the ring carries no intrusion, so merely approaching a pad
            # is legal) must stay at or under the old legs'. A leg
            # ending a hair inside a margin ring the old legs already
            # grazed passes and the relax polishes it; a leg that
            # crosses a pad the old legs never touched is refused
            # outright — the polygon-touch test waved both through.
            req = pad_req[bi]
            for pi in foreign_pads(bi):
                plo = (float(pads.lo[pi][0]), float(pads.lo[pi][1]))
                phi = (float(pads.hi[pi][0]), float(pads.hi[pi][1]))
                was = max(
                    0.0,
                    req
                    - min(
                        _seg_aabb_dist(mpts[jj - 1], mpts[jj], plo, phi),
                        _seg_aabb_dist(mpts[jj], mpts[jj + 1], plo, phi),
                    ),
                )
                now = max(
                    0.0,
                    req
                    - min(
                        _seg_aabb_dist(mpts[jj - 1], cand, plo, phi),
                        _seg_aabb_dist(cand, mpts[jj + 1], plo, phi),
                    ),
                )
                if now > was + 1e-9:
                    return False
            for leg in legs:
                for z, zp in unwind_zones:
                    if z.net and z.net == own_name:
                        continue
                    if z.layers and not (set(z.layers) & cand_layers):
                        continue
                    if leg.intersects(zp) and not any(
                        old.intersects(zp) for old in old_legs
                    ):
                        return False
                if (
                    outline_poly is not None
                    and not outline_poly.covers(leg)
                    and any(outline_poly.covers(old) for old in old_legs)
                ):
                    return False
                for j in range(len(pts_out)):
                    if j == bi or (min(bi, j), max(bi, j)) in braided:
                        continue
                    other = LineString(pts_out[j])
                    if leg.crosses(other) and not any(
                        old.crosses(other) for old in old_legs
                    ):
                        return False
            return True

        for _sweep in range(2):
            for (i, j), gap in sorted(pair_gap.items()):
                signs = [
                    _pair_sign(wires, unwind_cdirs, e, i, j)
                    for e in sorted(edge_sets[i] & edge_sets[j])
                    if len(wires.order[e]) >= 2
                ]
                sig = sum(1 if s > 0 else -1 for s in signs)
                if sig > 0:
                    mover, peer = j, i
                elif sig < 0:
                    mover, peer = i, j
                else:
                    # no order signal (no common shared edge, or a tie)
                    # — push the higher-index wire to ITS endpoint side
                    mover, peer = j, i
                m_pts = pts_out[mover]
                side = _end_side(mover, peer)
                if side == 0:
                    continue  # straddling ends: a weave, not a fan
                for jj in range(1, len(m_pts) - 1):
                    pv = m_pts[jj]
                    q, ld, _dd = _nearest(pv, pts_out[peer])
                    ln = math.hypot(*ld)
                    if ln <= 1e-9:
                        continue
                    s = ((ld[0] * (pv[1] - q[1]) - ld[1] * (pv[0] - q[0])) / ln) * side
                    if s >= 0.0:
                        continue  # slot side held — crowding is the
                        # local relax's own remit, not the unwind's
                    cand = (
                        q[0] + (gap + 0.05) * (-ld[1] / ln) * side,
                        q[1] + (gap + 0.05) * (ld[0] / ln) * side,
                    )
                    # the move is bounded — a far-side vertex is a weave,
                    # not a fan the unwind can lift — and both new legs
                    # must clear the accept()-style check, not just the
                    # point its old pad intrusion compared against
                    if math.dist(pv, cand) > _UNWIND_MOVE_CAP * gap:
                        continue
                    if _pad_intr(cand, mover) <= _pad_intr(pv, mover) + 1e-9 and (
                        _unwind_ok(mover, jj, cand)
                    ):
                        m_pts[jj] = cand

        def accept(q: list[list[tuple[float, float]]]) -> bool:
            lines = {
                bi: LineString([(float(x), float(y)) for x, y in q[bi]])
                for bi in range(len(q))
            }
            for bi in range(len(q)):
                for k in range(len(q[bi]) - 1):
                    leg = LineString([q[bi][k], q[bi][k + 1]])
                    for pi in foreign_pads(bi):
                        # the MARGIN, not just the touch: a relaxed leg
                        # sitting inside a foreign pad's s+w/2 ring is
                        # sub-clearance copper — the emit guard would
                        # withdraw the whole net for it (gr537726's
                        # 0.2248 lesson). Refuse so the pre-relaxation
                        # seed stands instead of a polished violation.
                        if leg.distance(pads_poly[pi]) < pad_req[bi] - 1e-3:
                            return False
            for i in range(len(q)):
                for j in range(i + 1, len(q)):
                    if (i, j) in braided:
                        continue
                    if lines[i].crosses(lines[j]):
                        return False
            # pair gaps, the cost's other hinge: a trial that fixes
            # pads but lets a non-braided pair close under its gap is
            # sub-clearance copper too (track-to-track this time) —
            # refuse it like the pad-margin case so the seed stands.
            for (i, j), gap in pair_gap.items():
                if (i, j) in braided:
                    continue
                if lines[i].distance(lines[j]) < gap - 1e-3:
                    return False
            return True

        # ── cluster the violations (gr520900 fix round 1) ──────────────
        def seed_violators() -> set[int]:
            """Branches with a hinge violation at the seed — a leg inside
            a foreign pad's margin ring, or a non-braided pair closer
            than its pair gap. This is ONE hinge sweep at the seed,
            the same terms the cost uses; a clean board returns empty
            and skips the optimizer entirely."""

            def pad_v(bi: int, k: int) -> float:
                a_, b_ = pts_out[bi][k], pts_out[bi][k + 1]
                req = pad_req[bi]
                return max(
                    (
                        req
                        - _seg_aabb_dist(
                            a_,
                            b_,
                            (float(pads.lo[pi][0]), float(pads.lo[pi][1])),
                            (float(pads.hi[pi][0]), float(pads.hi[pi][1])),
                        )
                        for pi in near_pads[bi]
                    ),
                    default=0.0,
                )

            def pair_v(i: int, j: int, gap: float) -> float:
                return max(
                    (
                        gap
                        - _seg_seg_dist(
                            pts_out[i][k],
                            pts_out[i][k + 1],
                            pts_out[j][m],
                            pts_out[j][m + 1],
                        )
                        for k in range(len(pts_out[i]) - 1)
                        for m in range(len(pts_out[j]) - 1)
                    ),
                    default=0.0,
                )

            bad: set[int] = set()
            for bi in range(len(pts_out)):
                if any(pad_v(bi, k) > 0.0 for k in range(len(pts_out[bi]) - 1)):
                    bad.add(bi)
            for (i, j), gap in pair_gap.items():
                if pair_v(i, j, gap) > 0.0:
                    bad.add(i)
                    bad.add(j)
            return bad

        # Union–find over the pair_gap interaction graph; a pad-margin-
        # only violation has no pair edges and stays a singleton. Each
        # violating branch's component is its cluster: every branch a
        # violating leg could interact with (pair-gap reach), grown
        # transitively. find/union order is deterministic, so the
        # components are too.
        parent = list(range(len(pts_out)))

        def find(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for i, j in pair_gap:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[max(ri, rj)] = min(ri, rj)

        clusters: dict[int, set[int]] = {}
        for bi in sorted(seed_violators()):
            clusters.setdefault(find(bi), set()).add(bi)
        ordered = sorted(
            ({b for b in range(len(pts_out)) if find(b) == root} for root in clusters),
            key=min,
        )

        for cluster in ordered:
            # this cluster's free variables and precomputed candidate
            # lists — built ONCE here, outside the cost closure: the
            # cost evaluation must only hinge, never filter.
            free = [(bi, j) for bi, j in interior if bi in cluster]
            if not free or len(free) > _RELAX_FREE_VERTEX_CAP:
                if len(free) > _RELAX_FREE_VERTEX_CAP:
                    # gr536410 review nit: a skipped cluster used to be
                    # silent — the relaxation quietly left its violations
                    # in the output. The emit guard still judges the
                    # result; this line is the trace that a cluster was
                    # over the solve budget.
                    log.info(
                        "topo S2 relaxation: skipping cluster of %d free "
                        "vertices (cap %d); violations left for the emit "
                        "guard",
                        len(free),
                        _RELAX_FREE_VERTEX_CAP,
                    )
                continue  # oversized cluster: the old output stands
            cluster_pad = {bi: near_pads[bi] for bi in cluster}
            cluster_pairs = [
                ((i, j), gap)
                for (i, j), gap in pair_gap.items()
                if i in cluster or j in cluster
            ]
            x0 = np.asarray([pts_out[bi][j] for bi, j in free], dtype=float).ravel()

            def cluster_cost(
                x: np.ndarray,
                *,
                _free: list[tuple[int, int]] = free,
                _x0: np.ndarray = x0,
                _cluster: set[int] = cluster,
                _cluster_pad: dict[int, list[int]] = cluster_pad,
                _cluster_pairs: list[tuple[tuple[int, int], float]] = cluster_pairs,
            ) -> float:
                cur: dict[int, list[tuple[float, float]]] = {}
                for n, (bi, j) in enumerate(_free):
                    pts = cur.get(bi)
                    if pts is None:
                        pts = cur[bi] = list(pts_out[bi])
                    pts[j] = (float(x[2 * n]), float(x[2 * n + 1]))
                c = 0.0
                for bi in _cluster:
                    pts = cur.get(bi, pts_out[bi])
                    req = pad_req[bi] + _RELAX_SLACK_MM
                    for k in range(len(pts) - 1):
                        a_, b_ = pts[k], pts[k + 1]
                        for pi in _cluster_pad[bi]:
                            v = req - _seg_aabb_dist(
                                a_,
                                b_,
                                (float(pads.lo[pi][0]), float(pads.lo[pi][1])),
                                (float(pads.hi[pi][0]), float(pads.hi[pi][1])),
                            )
                            if v > 0.0:
                                c += v * v * 100.0
                for (i, j), gap in _cluster_pairs:
                    qi = cur.get(i, pts_out[i])
                    qj = cur.get(j, pts_out[j])
                    gap = gap + _RELAX_SLACK_MM
                    for k in range(len(qi) - 1):
                        a_, b_ = qi[k], qi[k + 1]
                        for m in range(len(qj) - 1):
                            v = gap - _seg_seg_dist(a_, b_, qj[m], qj[m + 1])
                            if v > 0.0:
                                c += v * v * 100.0
                return c + 0.01 * float(np.sum((x - _x0) ** 2))

            # only worth solving when this cluster still violates at the
            # seed — an earlier cluster's commit may have cleared it
            if cluster_cost(x0) <= 0.0:
                continue
            bounds = Bounds(x0 - bound, x0 + bound)
            res = minimize(
                cluster_cost,
                x0,
                method="Powell",
                bounds=bounds,
                options={
                    "maxiter": 400,
                    "maxfev": min(_RELAX_MAXFEV_PER_COORD * len(x0), _RELAX_MAXFEV_ABS),
                    "xtol": 1e-4,
                    "ftol": 1e-9,
                },
            )
            if res.fun < cluster_cost(x0):
                trial = [list(p) for p in pts_out]
                for n, (bi, j) in enumerate(free):
                    trial[bi][j] = (float(res.x[2 * n]), float(res.x[2 * n + 1]))
                if accept(trial):
                    for n, (bi, j) in enumerate(free):
                        pts_out[bi][j] = trial[bi][j]

    return [tuple(p) for p in pts_out]


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
) -> RoutePlan:
    """Layer assignment → pieces + via sites for one routed pose.

    Per branch the runs/cuts model drives the joins directly: every
    cut mints a via site (an F↔B mid-branch join, a crossing's
    bracketed piece, an end via where a forced pad side must be the
    terminal layer), every run emits one :class:`RoutePiece` on its
    layer. Each cut's site comes from :func:`_site_via`'s keepout walk
    — ``net_rules`` (net NAME → :class:`NetRules`, as
    :func:`realize_result` resolves them) sizes the barrel, with
    ``min_via_dia`` (the fab minimum) as the fallback when the rule
    via finds no site (gr524334); a join with no qualifying point at
    either diameter mints no via and lands on
    :attr:`RoutePlan.no_via`. A fallback-sited via records
    ``min_via_drill`` as its drill — the fallback is the fab-minimum
    dia/drill PAIR, so the emitted barrel's annular ring stays fab-legal
    (a 0.45 mm pad over the house 0.25 mm drill leaves a 0.10 mm ring,
    under JLC's 0.15 floor)."""
    wires, runs, cuts, crossings = assign_layers(
        d,
        mesh,
        wires,
        n_layers,
        net_rules=net_rules,
        pose=pose,
        event=event,
        masks=masks,
    )
    rules = net_rules or {}
    pose_a = np.asarray(d.pose0 if pose is None else pose)
    # pad rosters per outer layer: top-SMD pads live on layer 0, bottom
    # on n−1, TH (and anything unclassed) on both — the via barrel spans
    # the board, so both lists gate every site.
    pad_sets: list[list[tuple[int, float, float, float, float]]] = [[], []]
    if len(d.pin_wh):
        for p in range(len(d.pin_xy)):
            part = int(d.pin_part[p])
            if part >= len(pose_a) or not np.isfinite(pose_a[part, :2]).all():
                continue
            w, h = (float(v) for v in d.pin_wh[p])
            row = (
                part,
                float(d.pin_xy[p][0]),
                float(d.pin_xy[p][1]),
                w / 2.0,
                h / 2.0,
            )
            side = int(d.pin_side[p])
            if side == 0:
                pad_sets[0].append(row)
            elif side == 1:
                pad_sets[1].append(row)
            else:
                pad_sets[0].append(row)
                pad_sets[1].append(row)
    # Non-primary footprint pads (gr524392) — the via keep-out must
    # clear the same lands the DRC flashes: a pin's second same-named
    # pad, a split slug half, an unpinned land. Same part-local rect
    # rows, same side→roster rule.
    for k in range(len(d.pad_extra_xy)):
        part = int(d.pad_extra_part[k])
        if not 0 <= part < len(pose_a) or not np.isfinite(pose_a[part, :2]).all():
            continue
        row = (
            part,
            float(d.pad_extra_xy[k, 0]),
            float(d.pad_extra_xy[k, 1]),
            float(d.pad_extra_wh[k, 0]) / 2.0,
            float(d.pad_extra_wh[k, 1]) / 2.0,
        )
        side = int(d.pad_extra_side[k])
        if side == 0:
            pad_sets[0].append(row)
        elif side == 1:
            pad_sets[1].append(row)
        else:
            pad_sets[0].append(row)
            pad_sets[1].append(row)
    # Via siting keeps the barrel out of the same bodies the mesh keeps
    # wires out of (gr524239): the board outline and every
    # keepout/mounting-hole zone touching an outer layer — a barrel is
    # plated through, so an unscoped zone (``layers == ()``) blocks it
    # too. The pre-gr524239 walk checked none of these: a site could
    # land in a keepout or off the board.
    outline_poly = (
        Polygon(np.asarray(d.outline, dtype=float)) if len(d.outline) >= 3 else None
    )
    keepout_geoms: list[Any] = []
    outer_idx = {0, n_layers - 1}
    for z in d.zones:
        # label_keepout zones ride along: a via's annulus conflicts with
        # refdes-label ink exactly the way a pad's does, so the siting
        # walk honors them even though the wire mesh ignores them.
        if z.kind not in ("keepout", "mounting_hole", "label_keepout"):
            continue
        zl = {int(l) for l in (z.layers or ())}
        if zl and not (zl & outer_idx):
            continue
        g = Polygon(np.asarray(topo_proxy.zone_world_ring(d, z, pose_a), dtype=float))
        if not g.is_empty and g.area > 0.0:
            keepout_geoms.append(g)
    # every net's polyline rows for the foreign-track veto (gr520902
    # s4d): the via barrel spans the whole stackup, so a site within
    # ``(via_dia + track_w)/2 + max clearance`` of another net's copper
    # — on ANY layer, inners included — is refused.
    track_rows: dict[
        int, list[tuple[tuple[tuple[float, float], ...], float, float]]
    ] = {}
    for fb in wires.branches:
        fname = str(d.net_name[fb.net]) if fb.net < len(d.net_name) else str(fb.net)
        fnr = rules.get(fname, _DEFAULT)
        track_rows.setdefault(int(fb.net), []).append(
            (fb.points, float(fnr.track_width_mm), float(fnr.clearance_mm))
        )
    pieces: list[RoutePiece] = []
    vias: list[ViaSite] = []
    placed: list[tuple[float, float, float]] = []
    no_via: list[int] = []
    for bi, b in enumerate(wires.branches):
        joins = cuts[bi]
        run_layers = runs[bi]
        # ``paths`` carries the spoke-offset polyline (gr520900) for the
        # emitted geometry; via siting and the edge/t bookkeeping stay
        # on ``b``'s corridor-true fields.
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
                # Report the join as unsited like a walk that found
                # nothing rather than emitting copper that cannot
                # connect.
                no_via.append(bi)
            continue
        name = str(d.net_name[b.net]) if b.net < len(d.net_name) else str(b.net)
        nr = rules.get(name, _DEFAULT)
        via_dia = float(nr.via_dia_mm or _VIA_DIA_MM)
        clearance = float(nr.clearance_mm)
        foreign = tuple(
            (fpts, via_dia / 2.0 + fw / 2.0 + max(clearance, fc))
            for fk, rows in track_rows.items()
            if fk != int(b.net)
            for fpts, fw, fc in rows
        )
        pts = list(bpts)
        cut_xy: list[tuple[float, float]] = []
        sited = True
        site_edges: set[int] = set()
        for ei in joins:
            site = _site_via(
                mesh,
                b,
                ei,
                n_layers,
                via_dia,
                clearance,
                placed,
                (tuple(pad_sets[0]), tuple(pad_sets[1])),
                pose_a,
                outline=outline_poly,
                keepouts=keepout_geoms,
                fallback_dia=min_via_dia,
                foreign=foreign,
                track_w=float(nr.track_width_mm),
            )
            if site is not None and site[0] in site_edges:
                # refuse two cuts on one edge — the second join gets no
                # via (gr520902 review a.2)
                site = None
            if site is None:
                sited = False
                # keep the split at the intended crossing: the pieces
                # still describe where the join was wanted — it is the
                # via that is not minted.
                if b.edges:
                    e = int(b.edges[min(ei, len(b.edges) - 1)])
                    t = float(b.t[ei]) if ei < len(b.t) else 0.5
                    cut_xy.append(_edge_xy(mesh, e, t))
                else:
                    cut_xy.append(pts[len(pts) // 2])
                continue
            e, t, x, y, via_d = site
            site_edges.add(e)
            via_drill = (
                float(min_via_drill)
                if min_via_drill is not None and via_d != via_dia
                else 0.0
            )
            vias.append(ViaSite(b.net, bi, e, t, x, y, via_d, via_drill))
            placed.append((x, y, via_d))
            cut_xy.append((x, y))
        if not sited:
            no_via.append(bi)
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
        layers=tuple(r[0] for r in runs),
        crossings=crossings,
        unreachable=unrouted,
        stranded=wires.unreachable,
        no_via=tuple(no_via),
    )


def _octilinear(
    points: tuple[tuple[float, float], ...],
    clear: (
        Callable[[tuple[float, float], tuple[float, float], tuple[float, float]], bool]
        | None
    ) = None,
    jog: (
        Callable[
            [tuple[float, float], tuple[float, float]],
            tuple[tuple[float, float], tuple[float, float]] | None,
        ]
        | None
    ) = None,
) -> tuple[tuple[float, float], ...]:
    """The piece's taut path re-drawn in multiples of 45° — the DRC's
    rectilinear rule admits no other angle (``every drawn wire must be
    a multiple of 45deg``). Each off-angle leg becomes an L bend;
    ``clear`` (gr520900) is called as ``clear(p, c, q)`` and vetoes an
    L bend whose legs touch copper they must avoid — the
    horizontal-first corner is preferred, the vertical-first corner is
    taken when the first's legs are blocked, and when NEITHER clears
    the new ``jog(p, q)`` (the S-detour fallback) gets a chance to
    route around both blockers with an axis-aligned jog pair before
    the leg keeps its authored line — a kept authored leg is the one
    shape that can leave this redraw off-angle, and the emit 45° check
    in ``realize_result`` then refuses the piece's track (gr543679)."""
    out: list[tuple[float, float]] = [(float(points[0][0]), float(points[0][1]))]
    for p, q in itertools.pairwise(points):
        dx, dy = q[0] - p[0], q[1] - p[1]
        if dx == 0 or dy == 0 or abs(abs(dx) - abs(dy)) <= 1e-9:
            out.append((float(q[0]), float(q[1])))
            continue
        c1 = (float(q[0]), float(p[1]))
        c2 = (float(p[0]), float(q[1]))
        qq = (float(q[0]), float(q[1]))
        pp = (float(p[0]), float(p[1]))
        if clear is None or clear(pp, c1, qq):
            out.extend((c1, qq))
        elif clear(pp, c2, qq):
            out.extend((c2, qq))
        else:
            js = jog(pp, qq) if jog is not None else None
            if js is not None:
                out.extend((js[0], js[1], qq))
            else:
                out.append(qq)
    return tuple(out)


def _axis_jog(
    p: tuple[float, float],
    q: tuple[float, float],
    path_clear: Callable[[LineString], bool],
    blocks: Iterable[Polygon],
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """The S/Z fallback for an octilinear leg whose BOTH L corners are
    vetoed: jog horizontally (``p → (p.x, y_j) → (q.x, y_j) → q``) or
    vertically at a candidate coordinate, first candidate whose full
    4-point path clears wins. Candidates are harvested from the
    blocking polygons' bbox edges inside the leg's span — the obstacles
    that vetoed the L's leave their footprints there — plus the span
    midpoint, preferred nearest the middle. Axis jogs keep the redraw
    on the 45° grid. Returns None when no jog clears (the caller keeps
    the authored leg and the emit guard judges)."""
    px, py = float(p[0]), float(p[1])
    qx, qy = float(q[0]), float(q[1])
    if abs(px - qx) <= 1e-9 or abs(py - qy) <= 1e-9:
        return None
    xs_lo, xs_hi = min(px, qx), max(px, qx)
    ys_lo, ys_hi = min(py, qy), max(py, qy)
    mid_y = ys_lo + (ys_hi - ys_lo) / 2.0
    mid_x = xs_lo + (xs_hi - xs_lo) / 2.0
    y_cands = {mid_y}
    x_cands = {mid_x}
    for b_ in blocks:
        bx0, by0, bx1, by1 = b_.bounds
        if bx0 <= xs_hi and bx1 >= xs_lo:
            y_cands.add(float(by0))
            y_cands.add(float(by1))
        if by0 <= ys_hi and by1 >= ys_lo:
            x_cands.add(float(bx0))
            x_cands.add(float(bx1))
    for yj in sorted(y_cands, key=lambda y: (abs(y - mid_y), y)):
        if abs(yj - py) <= 1e-9 or abs(yj - qy) <= 1e-9:
            continue
        j1, j2 = (px, yj), (qx, yj)
        if path_clear(LineString([p, j1, j2, q])):
            return (j1, j2)
    for xj in sorted(x_cands, key=lambda x: (abs(x - mid_x), x)):
        if abs(xj - px) <= 1e-9 or abs(xj - qx) <= 1e-9:
            continue
        j1, j2 = (xj, py), (xj, qy)
        if path_clear(LineString([p, j1, j2, q])):
            return (j1, j2)
    return None


#: Emit-side vertex dedupe reach (gr543719): the octilinear corner pass
#: and the piece-split projections can mint near-zero-length stubs
#: (stored copper carried 0.01 mm segments on taz-leaf-i). Vertices
#: closer than this to their predecessor coalesce before segments are
#: built. A vertex whose removal would break the 45° discipline of the
#: merged segment is KEPT — coalescing may shrink geometry by the
#: epsilon, never redraw it off-angle.
_COALESCE_EPS_MM = 1e-3


def _seg_off_45(p: tuple[float, float], q: tuple[float, float]) -> bool:
    """Whether segment ``p→q`` is neither axis-aligned nor a true 45°
    diagonal — the emit check's strict verdict (fp-exact: the redraw
    just constructed these points, so no import-rounding tolerance)."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    return not (dx == 0 or dy == 0 or abs(abs(dx) - abs(dy)) <= 1e-9)


def _coalesce_micro(
    pts: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    """Drop interior vertices closer than ``_COALESCE_EPS_MM`` to the
    last kept vertex (gr543719), keep-first. The endpoints stay verbatim
    — they are pad joins. A micro-vertex is dropped only when the merged
    segment stays axis-aligned or 45°: a corner absorbed against the
    rule would turn one legal sliver into one illegal leg."""
    if len(pts) < 3:
        return pts
    out: list[tuple[float, float]] = [(float(pts[0][0]), float(pts[0][1]))]
    for i in range(1, len(pts) - 1):
        v = (float(pts[i][0]), float(pts[i][1]))
        if math.dist(out[-1], v) < _COALESCE_EPS_MM and not _seg_off_45(
            out[-1], pts[i + 1]
        ):
            continue
        out.append(v)
    out.append((float(pts[-1][0]), float(pts[-1][1])))
    return tuple(out)


def realize_result(
    plan: RoutePlan,
    d: TopoDesign,
    ir: Any,
    n_layers: int,
    *,
    net_rules: Mapping[str, NetRules] | None = None,
    pose: Any = None,
    off_angle: list[int] | None = None,
) -> Any:
    """The plan as a :class:`precis.pcb.realize.RealizeResult` — the one
    shape the store path, ``result_copper_rows`` and the DRC gate all
    read. ``ir`` maps the design's ``net_name`` back to ir net ids.
    ``pose`` (gr520900) enables the pad-aware octilinear corner choice —
    a corner only lands where it clears foreign copper; absent, the
    legacy horizontal-first redraw runs (synthetic callers without a
    placed pose).

    Two emit-stage geometry vetoes run on the redrawn points (gr543679,
    gr543719): micro-vertices coalesce (``_coalesce_micro``), and a
    piece whose redraw still carries an off-angle segment — a kept
    authored leg no L-bend cleared — has its track REFUSED, never
    committed for the DRC's octilinear rule to flag after the fact. The
    piece's net is appended to ``off_angle`` (an out-list the caller
    turns into the plan's withdrawal record); the rest of the net's
    pieces still emit, like ``severed``'s per-component withdrawal."""
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
    pad_polys = (
        _pad_polys(d, ir, n_layers, np.asarray(pose)) if pose is not None else None
    )
    # zone keepouts as world-frame polygons — the octilinear redraw's
    # clearance candidates (its L corners were pad-only: an L could be
    # drawn clean of every pad yet straight through a keepout). layer
    # filtering happens per piece; label_keepout is VIA-only (silk over
    # traces) and never blocks copper.
    zone_polys: list[tuple[Polygon, Any]] = []
    if pad_polys is not None:
        from precis.pcb.topoplace.proxy import zone_world_ring

        pose_a = np.asarray(pose)
        for z in d.zones:
            if z.kind == "label_keepout":
                continue
            ring = zone_world_ring(d, z, pose_a)
            if len(ring) >= 3:
                zone_polys.append((Polygon(np.asarray(ring)), z))

    def nm(k: int) -> str:
        return str(d.net_name[k]) if 0 <= k < len(d.net_name) else str(k)

    taut = {
        i: LineString([(float(x), float(y)) for x, y in pc.points])
        for i, pc in enumerate(plan.pieces)
    }
    for pi_, p in enumerate(plan.pieces):
        name = nm(p.net)
        nr = rules.get(name, _DEFAULT)
        others = [
            (
                taut[j],
                float(rules.get(nm(oc.net), _DEFAULT).track_width_mm),
                float(rules.get(nm(oc.net), _DEFAULT).clearance_mm),
            )
            for j, oc in enumerate(plan.pieces)
            if j != pi_ and oc.layer == p.layer
        ]
        piece_zones = [
            (zp, z.net)
            for zp, z in zone_polys
            if (not z.layers or p.layer in z.layers) and (not z.net or z.net != name)
        ]
        corner_clear, path_clear = _piece_clear_ctx(
            p,
            pad_polys if pad_polys is not None else {},
            nm,
            lambda n: rules.get(n, _DEFAULT),
            others,
            zones=piece_zones,
        )
        if pad_polys is None:
            pts = _octilinear(p.points)
        else:
            blocks: list[Polygon] = [
                poly
                for pname, poly, _pin in pad_polys.get(p.layer, [])
                if not pname or pname != name
            ]
            blocks.extend(zp for zp, _znet in piece_zones)

            def _jog(
                pp: tuple[float, float],
                qq: tuple[float, float],
                _pc: Callable[[LineString], bool] = path_clear,
                _bl: list[Polygon] = blocks,
            ) -> tuple[tuple[float, float], tuple[float, float]] | None:
                return _axis_jog(pp, qq, _pc, _bl)

            pts = _octilinear(p.points, corner_clear, _jog)
        pts = _coalesce_micro(pts)
        if any(_seg_off_45(a, b) for a, b in itertools.pairwise(pts)):
            # gr543679: a kept authored leg survived the redraw — refuse
            # the piece's track rather than commit copper the DRC's
            # octilinear rule would flag after the fact.
            if off_angle is not None and p.net not in off_angle:
                off_angle.append(p.net)
            continue
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


def _pad_polys(
    d: TopoDesign, ir: Any, n_layers: int, pose: np.ndarray
) -> dict[int, list[tuple[str, Any, int]]]:
    """World-frame pad geometry per layer, tagged ``(net name, geom,
    pin id)`` — the SAME pad set the DRC flashes: each pin's pad plus
    every non-primary footprint land (``pad_extra_*``: a pin's second
    same-named pad, a split slug half, an unpinned land — gr524239; the
    pin-only roster passed a track through copper the checker then
    errored on). ``NO_NET``/unpinned lands carry the empty name (foreign
    to every net) and pin ``-1``. SMD pads live on their mount side, TH
    on all layers.

    Geometry follows :func:`precis.pcb.connectivity._pad_primitives`'s
    shape convention — a ``'circle'`` pad is its inscribed disk, a
    ``'polygon'`` pad its authored ring (a missing ring falls back to
    the circumscribed disk), everything else the ``*_wh`` rect — exact
    where the design carries the shape, so the same set serves both the
    foreign-copper guard and the emit-join check (a corner touch on a
    round land is not a join — gr537831 review)."""
    from precis.pcb.ir import NO_NET

    pad_polys: dict[int, list[tuple[str, Any, int]]] = {i: [] for i in range(n_layers)}

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
    ) -> None:
        if part >= len(pose) or not np.isfinite(pose[part, :2]).all():
            return
        if hw <= 0.0 or hh <= 0.0:
            return
        if shape == "circle":
            (cx, cy) = topo_proxy.local_to_world(
                np.asarray([[px, py]], dtype=float), pose[part]
            )[0]
            poly: Any = Point(float(cx), float(cy)).buffer(min(hw, hh))
        elif ring:
            pts = np.asarray(ring, dtype=np.float64) + np.asarray([px, py])
            poly = Polygon(
                np.asarray(topo_proxy.local_to_world(pts, pose[part]), float)
            )
        elif shape == "polygon":
            # the authored ring is unresolvable — connectivity's
            # circumscribed-disk fallback for the same case.
            (cx, cy) = topo_proxy.local_to_world(
                np.asarray([[px, py]], dtype=float), pose[part]
            )[0]
            poly = Point(float(cx), float(cy)).buffer(math.hypot(hw, hh))
        else:
            corners = np.asarray(
                [
                    [px - hw, py - hh],
                    [px + hw, py - hh],
                    [px + hw, py + hh],
                    [px - hw, py + hh],
                ]
            )
            poly = Polygon(
                np.asarray(topo_proxy.local_to_world(corners, pose[part]), float)
            )
        if poly.is_empty or poly.area <= 0.0:
            return
        layers: Iterable[int] = (
            range(n_layers) if side == 2 else (0,) if side == 0 else (n_layers - 1,)
        )
        for lay in layers:
            pad_polys[lay].append((pname, poly, pin))

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
        )
    return pad_polys


def _piece_clear_ctx(
    piece: RoutePiece,
    pad_polys: dict[int, list[tuple[str, Polygon, int]]],
    nm: Callable[[int], str],
    nrules: Callable[[str], NetRules],
    others: Iterable[tuple[LineString, float, float]] = (),
    zones: Iterable[tuple[Polygon, str]] = (),
) -> tuple[
    Callable[[tuple[float, float], tuple[float, float], tuple[float, float]], bool],
    Callable[[LineString], bool],
]:
    """The octilinear redraw's clearance tests for one piece (gr520900),
    now over ALL blocking geometry: foreign pads on its layer
    (:func:`_pad_polys`), zone keepouts blocking its layer (same-net
    fixed-copper zones a wire may pass, ``TopoZone.net``, stay clear),
    and the pairwise spacing off every other piece's spoke path on the
    same layer. Zone blindness was the S-detour defect: the redraw
    chose an L corner inside a keepout stub because only pads could
    veto it. Returns ``(corner_clear, path_clear)`` — the 3-arg L-bend
    verdict, and the underlying arbitrary-path verdict the S/Z jog
    search composes 4-point candidates against. A candidate clears
    when BOTH legs keep the track's half-width plus clearance off
    every blocker — a clear corner point is not enough, the stub leg
    from the corner onward can still graze, and a redrawn path across
    a neighbour's channel crosses it even though the taut paths never
    did."""
    nr = nrules(nm(piece.net))
    wi, ci = float(nr.track_width_mm), float(nr.clearance_mm)
    own = nm(piece.net)

    def path_clear(path: LineString) -> bool:
        for pname, poly, _pin in pad_polys.get(piece.layer, []):
            if pname and pname == own:
                continue
            cp = float(nrules(pname).clearance_mm) if pname else ci
            if poly.distance(path) < wi / 2.0 + max(ci, cp) - 1e-9:
                return False
        for zp, znet in zones:
            if znet and znet == own:
                continue
            if zp.distance(path) < wi / 2.0 + ci - 1e-9:
                return False
        for oline, wo, co in others:
            if path.distance(oline) < (wi + wo) / 2.0 + max(ci, co) - 1e-9:
                return False
        return True

    def clear(
        p: tuple[float, float], c: tuple[float, float], q: tuple[float, float]
    ) -> bool:
        return path_clear(
            LineString(
                [
                    (float(p[0]), float(p[1])),
                    (float(c[0]), float(c[1])),
                    (float(q[0]), float(q[1])),
                ]
            )
        )

    return clear, path_clear


def _guarded_nets(
    emit: RoutePlan,
    d: TopoDesign,
    ir: Any,
    n_layers: int,
    pose: np.ndarray,
    net_rules: Mapping[str, NetRules] | None,
) -> set[int]:
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
    same figures the DRC gate measures, so a pass here is a pass there."""
    from shapely.geometry import LineString

    rules = net_rules or {}
    names = [str(n) for n in d.net_name]

    def nm(k: int) -> str:
        return names[k] if 0 <= k < len(names) else str(k)

    def nrules(name: str) -> NetRules:
        return rules.get(name, _DEFAULT)

    pad_polys = _pad_polys(d, ir, n_layers, pose)
    taut = {
        i: LineString([(float(x), float(y)) for x, y in pc.points])
        for i, pc in enumerate(emit.pieces)
    }
    lines = {}
    for i, pc in enumerate(emit.pieces):
        others = [
            (
                taut[j],
                float(nrules(nm(oc.net)).track_width_mm),
                float(nrules(nm(oc.net)).clearance_mm),
            )
            for j, oc in enumerate(emit.pieces)
            if j != i and oc.layer == pc.layer
        ]
        lines[i] = LineString(
            _octilinear(
                pc.points, _piece_clear_ctx(pc, pad_polys, nm, nrules, others)[0]
            )
        )
    by_layer: dict[int, list[int]] = {}
    for i, pc in enumerate(emit.pieces):
        by_layer.setdefault(pc.layer, []).append(i)

    guarded: set[int] = set()
    for lay, idxs in by_layer.items():
        for i in idxs:
            pi = emit.pieces[i]
            if pi.net in guarded:
                continue
            nri = nrules(nm(pi.net))
            wi, ci = float(nri.track_width_mm), float(nri.clearance_mm)
            for pname, poly, _pin in pad_polys.get(lay, []):
                if pname and pname == nm(pi.net):
                    continue  # own-net pads are where terminals land
                cp = float(nrules(pname).clearance_mm) if pname else ci
                if lines[i].distance(poly) < wi / 2.0 + max(ci, cp):
                    guarded.add(pi.net)
                    break
            if pi.net in guarded:
                continue
            for j in idxs:
                if j <= i:
                    continue
                pj = emit.pieces[j]
                if pj.net == pi.net or pj.net in guarded:
                    continue
                nrj = nrules(nm(pj.net))
                gap = (wi + float(nrj.track_width_mm)) / 2.0 + max(
                    ci, float(nrj.clearance_mm)
                )
                if lines[i].distance(lines[j]) < gap:
                    guarded.add(pi.net)
                    guarded.add(pj.net)
    # A via barrel spans every layer: vs foreign pads and foreign pieces.
    for v in emit.vias:
        if v.net in guarded:
            continue
        nrv = nrules(nm(v.net))
        dia, cv = (
            float(v.dia_mm or nrv.via_dia_mm or _VIA_DIA_MM),
            float(nrv.clearance_mm),
        )
        pt = Point(v.x, v.y)
        hit = False
        for lay in range(n_layers):
            for pname, poly, _pin in pad_polys[lay]:
                if pname and pname == nm(v.net):
                    continue
                cp = float(nrules(pname).clearance_mm) if pname else cv
                if poly.distance(pt) < dia / 2.0 + max(cv, cp):
                    hit = True
                    break
            if hit:
                break
            for j in by_layer.get(lay, []):
                pj = emit.pieces[j]
                if pj.net == v.net:
                    continue
                cj = float(nrules(nm(pj.net)).clearance_mm)
                if lines[j].distance(pt) < dia / 2.0 + max(cv, cj):
                    hit = True
                    break
            if hit:
                break
        if hit:
            guarded.add(v.net)
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
    committed board by, over the same pad shapes :func:`_pad_polys`
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

    pad_polys = _pad_polys(d, ir, n_layers, pose)
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
        for lay, entries in pad_polys.items():
            for pname, poly, pin in entries:
                if pname == own:
                    members.append((frozenset({lay}), poly, 0.0, pin, "pad", -1))
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


def route_design(
    d: TopoDesign,
    ir: Any,
    *,
    pose: Any = None,
    net_rules: Mapping[str, NetRules] | None = None,
    min_via_dia: float | None = None,
    min_via_drill: float | None = None,
    event: Callable[[str], None] | None = None,
    masks: BoardMasks | None = None,
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
    one call); ``None`` keeps the legacy search order."""
    from precis.pcb.currents import require_net_currents
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import build_wires

    require_net_currents(zip(ir.net_name, ir.net_current_a))

    mesh_pose = d.pose0 if pose is None else pose
    mesh = build_mesh(d, mesh_pose)
    if event is not None:
        event(f"route: mesh — {len(mesh.tris)} tris, {len(mesh.edges)} edges")
    rules = net_rules or {}
    rules_t = tuple(
        (
            float(rules[d.net_name[k]].track_width_mm),
            float(rules[d.net_name[k]].clearance_mm),
        )
        if k < len(d.net_name) and d.net_name[k] in rules
        else (_DEFAULT.track_width_mm, _DEFAULT.clearance_mm)
        for k in range(len(d.net_pins))
    )
    wires = build_wires(mesh, d, ir.n_layers, rules_t, pose=mesh_pose)
    if wires.overflow:
        # honest refusal (gr520904): a channel whose demand exceeds
        # capacity is named with its owners — never silently crammed.
        raise CorridorOverflow(wires.overflow)
    if event is not None:
        event(
            f"route: wires — {len(wires.branches)} branch(es) "
            f"over {len(d.net_pins)} net(s)"
        )
    # gr520900: the taut paths share obstacle vertices and edge
    # crossings — the spoke offsets fan coincident copper to legal
    # spacing before the plan freezes it.
    paths = _spoke_offsets(
        d, mesh, wires, ir.n_layers, net_rules=net_rules, pose=mesh_pose
    )
    plan = plan_route(
        d,
        mesh,
        wires,
        ir.n_layers,
        net_rules=net_rules,
        pose=mesh_pose,
        min_via_dia=min_via_dia,
        min_via_drill=min_via_drill,
        paths=paths,
        event=event,
        masks=masks,
    )
    if event is not None:
        event(
            f"route: plan — {len(plan.pieces)} piece(s), "
            f"{len(plan.vias)} via(s), {len(plan.crossings)} unresolved "
            f"crossing(s), {len(plan.unreachable)} unreachable"
        )
    emit = plan
    if plan.crossings:
        bad = {b for pair in plan.crossings for b in pair}
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
    guarded = _guarded_nets(emit, d, ir, ir.n_layers, np.asarray(mesh_pose), net_rules)
    if guarded:
        emit = dataclasses.replace(
            emit,
            pieces=tuple(p for p in emit.pieces if p.net not in guarded),
            vias=tuple(v for v in emit.vias if v.net not in guarded),
        )
        plan = dataclasses.replace(plan, guarded=tuple(sorted(guarded)))
        if event is not None:
            event(f"route: guard — {len(guarded)} net(s) withdrawn (short risk)")
    # gr537831: a net is "realized" only when its committed copper —
    # pieces, vias and the pads they must reach — is ONE connected
    # piece. A severed emit (the edgeless F↔B hop ending on the wrong
    # layer) is withdrawn whole like the anti-short guard, never
    # committed as a falsely routed net.
    severed = _severed_nets(emit, d, ir, ir.n_layers, np.asarray(mesh_pose), net_rules)
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
    off_angle: list[int] = []
    rres = realize_result(
        emit,
        d,
        ir,
        ir.n_layers,
        net_rules=net_rules,
        pose=mesh_pose,
        off_angle=off_angle,
    )
    if off_angle:
        # gr543679: the emit 45° check refused pieces whose redraw kept
        # an off-angle authored leg. Fold the nets into ``guarded`` so
        # the caller's failure count and shove loop see them through the
        # existing channel; ``off_angle`` names the real reason for
        # ``sketch_rows``.
        plan = dataclasses.replace(
            plan,
            guarded=tuple(sorted({*plan.guarded, *off_angle})),
            off_angle=tuple(sorted(set(off_angle))),
        )
        if event is not None:
            event(
                f"route: emit guard — {len(set(off_angle))} net(s) "
                "withdrawn (non-45° leg kept)"
            )
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
    short-risk`` (gr524239), and a net whose octilinear redraw kept an
    off-angle leg as ``failed: non-octilinear`` (gr543679); a net with
    emitted pieces is ``realized``;
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
        for a, b in plan.crossings:
            if net_by_branch.get(a) != k and net_by_branch.get(b) != k:
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
        # guarded/off_angle fields — absent means nothing was withdrawn.
        # off_angle is checked first: its nets sit in ``guarded`` too
        # (folded in for the route stage's failure count), and the true
        # reason is the kept off-angle leg, not a short risk.
        if k in getattr(plan, "off_angle", ()):
            problems.append(
                {
                    "reason": "non-octilinear",
                    "message": "track withdrawn: no 45° redraw clears "
                    "neighbouring copper (emit guard)",
                }
            )
        elif k in getattr(plan, "guarded", ()):
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
            trial_errors = _error_keys(
                drc_findings(dataclasses.replace(result, tracks=trial))
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

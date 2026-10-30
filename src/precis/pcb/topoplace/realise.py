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
its wire spanning the shared edges flips to the other outer layer and
two via sites — one at each end's ``(edge_id, t)`` crossing parameter —
bracket it. A crossing that survives the flip bound is reported on
:attr:`RoutePlan.crossings` for the caller to count as DRC, not hidden.

Via SITING is keepout-aware (topo S3, gr520901): a site is never minted
on a constrained ring edge or within a barrel's keep-out of an endpoint
vertex — the walk moves along the branch's crossed edges to the first
whose pad-channel record (:attr:`Wires.gap_free_mm`) leaves the via plus
its clearance on both outer layers, clears every pad polygon on those
layers by the barrel's keep-out (the channel record is trace-buffered —
a site "free" for a 0.15 mm wire can still drill a solder land), and
does not crowd an already placed via. A branch with no qualifying edge
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
from precis.pcb.topoplace.design import TopoDesign, _world_to_local
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
    net rule's own via_dia (the comfortable house default)."""

    net: int
    branch: int
    edge: int
    t: float
    x: float
    y: float
    dia_mm: float = 0.0


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
    guarded: tuple[int, ...] = ()

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


def _initial_layers(d: TopoDesign, wires: Wires, n_layers: int) -> list[int]:
    """Per-branch start layer: both ends forcing the same side wins it;
    an F↔B pair takes pin_a's side (the free via lands mid-branch);
    anything free takes the lowest candidate the class rule allows."""
    out: list[int] = []
    outers = {0, n_layers - 1}
    for b in wires.branches:
        la = _pin_forced(d, b.pin_a, n_layers)
        lb = _pin_forced(d, b.pin_b, n_layers)
        cand = (
            set(wires.net_layers[b.net]) & outers
            if b.net < len(wires.net_layers)
            else set(outers)
        )
        if la is not None:
            out.append(la)
        elif lb is not None:
            out.append(lb)
        else:
            out.append(min(cand) if cand else 0)
    return out


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
    seam convention (``_repair_order.norm``): position in ``order[e]``
    signed by the SECOND branch's corridor direction."""
    seq = wires.order[e]
    s = 1 if seq.index(a) < seq.index(b) else -1
    eb = {int(x): i for i, x in enumerate(wires.branches[b].edges)}
    if not cdirs:
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
    d: TopoDesign, wires: Wires, n_layers: int, mesh: TopoMesh | None = None
) -> tuple[list[int], tuple[tuple[int, int], ...]]:
    """Per-branch outer layer after crossing resolution: pass-based
    yielding — each pass walks every unresolved same-layer pair and
    yields the lower-ranked branch to the other outer layer, keeping
    the fewest-crossing state ever seen. Returns ``(layers, remaining
    crossing branch pairs)``.

    Why passes: the pre-gr524239 loop yielded the single minimum pair
    and stopped the moment that flip failed to improve the count. On a
    dense board the first yield almost always worsens before it
    improves — the flipped branch meets new neighbours on the far side
    before it clears the old ones — so the loop exited after ONE flip
    with nearly every crossing still live (taz-leaf: 2 branches
    flipped, 48 of 50 pairs left). A pass resolves each pair in turn,
    accepting a locally-worse state mid-pass, and the run ends when a
    full pass finds no new best (bounded by :data:`_MAX_FLIPS` flips,
    a cap like shove, not the mechanism). Each pair's yield target is
    the better of the two branches' trials — the higher-ranked side is
    not always the cheaper move — with the Decision-6 rank breaking
    ties."""
    branches = wires.branches
    layers = _initial_layers(d, wires, n_layers)
    if mesh is not None:
        cross = _crossing_pairs(
            wires,
            _cdirs(mesh, wires),
            obstacle_vertices(mesh, d, n_layers),
            [(int(u), int(v)) for u, v in mesh.edges],
        )
    else:
        cross = _crossing_pairs(wires, [])

    def live(ls: list[int]) -> set[tuple[int, int]]:
        return {
            (a, b)
            for a, b in cross
            if branches[a].net != branches[b].net and ls[a] == ls[b]
        }

    def flip(ls: list[int], y: int) -> None:
        ls[y] = n_layers - 1 if ls[y] == 0 else 0

    best, best_n = list(layers), len(live(layers))
    flips = 0
    for _ in range(_MAX_FLIPS):
        cur = live(layers)
        if not cur or flips >= _MAX_FLIPS:
            break
        improved_this_pass = False
        for a, b in sorted(cur):
            if flips >= _MAX_FLIPS:
                break
            if branches[a].net == branches[b].net or layers[a] != layers[b]:
                continue  # resolved (or same-net) since the pass snapshot
            rank_a = _yield_rank(d, branches[a], n_layers)
            rank_b = _yield_rank(d, branches[b], n_layers)
            y = a if rank_a <= rank_b else b
            other = b if y == a else a
            trial_y, trial_o = list(layers), list(layers)
            flip(trial_y, y)
            flip(trial_o, other)
            n_y, n_o = len(live(trial_y)), len(live(trial_o))
            layers, n = (trial_o, n_o) if n_o < n_y else (trial_y, n_y)
            flips += 1
            if n < best_n:
                best, best_n = list(layers), n
                improved_this_pass = True
            if n == 0:
                break
        if best_n == 0 or not improved_this_pass:
            break
    return best, tuple(sorted(live(best)))


def _span_toggles(
    d: TopoDesign,
    wires: Wires,
    layers: list[int],
    crossings: Sequence[tuple[int, int]],
    n_layers: int,
    cdirs: Sequence[Sequence[float]],
) -> tuple[dict[int, dict[int, int]], set[tuple[int, int]]]:
    """Crossing-span flips for the pairs the whole-branch yielder left
    live (gr524239): only the shared, order-inverting run of mesh edges
    changes layer — bracketed by vias at its ends — never the whole
    branch. A span flip moves strictly less copper than a whole-branch
    flip and cannot create a crossing with any branch the shared edges
    do not already see, so it resolves pairs whose wholesale flip the
    yielder refused for the new crossings that flip would create.

    Returns ``(branch -> {edge_seq_pos: layer_from_that_pos}, flipped)``
    — the layer dict feeds :func:`plan_route`'s switch resolver, keyed
    by edge-sequence position (0 = before the first edge, n = after the
    last); ``flipped`` is the subset of ``crossings`` resolved here,
    excluded from the plan's reported crossings. The yielding branch of
    each pair follows Decision 6's rank, ties to the lower index."""

    def other(layer: int) -> int:
        return n_layers - 1 if layer == 0 else 0

    out: dict[int, dict[int, int]] = {}
    flipped: set[tuple[int, int]] = set()
    for a, b in sorted(crossings):
        if layers[a] != layers[b]:
            continue  # resolved since the layer pass
        if wires.branches[a].net == wires.branches[b].net:
            continue  # a same-net braid is not a short
        yielder = (
            a
            if _yield_rank(d, wires.branches[a], n_layers)
            <= _yield_rank(d, wires.branches[b], n_layers)
            else b
        )
        by = wires.branches[yielder]
        peer_edges = {int(e) for e in wires.branches[b if yielder == a else a].edges}
        pos_of = {int(e): i for i, e in enumerate(by.edges)}
        shared = [(pos_of[int(e)], int(e)) for e in by.edges if int(e) in peer_edges]
        if len(shared) < 2:
            continue  # Decision 3 needs two shared edges to call a cross

        def sign(e: int, a: int = a, b: int = b) -> int:
            return _pair_sign(wires, cdirs, e, a, b)

        ref = sign(shared[0][1])
        # maximal runs of shared edges whose order inverts the reference
        runs: list[tuple[int, int]] = []
        for pos, e in shared:
            if sign(e) == ref:
                continue
            if runs and pos == runs[-1][1]:
                runs[-1] = (runs[-1][0], pos + 1)
            else:
                runs.append((pos, pos + 1))
        if not runs:
            continue
        tgt = out.setdefault(yielder, {})
        for start, end in runs:
            tgt[start] = other(layers[yielder])
            tgt[end] = layers[yielder]
        flipped.add((a, b))
    return out, flipped


def _edge_xy(mesh: TopoMesh, edge: int, t: float) -> tuple[float, float]:
    """World point at parameter ``t`` along mesh edge ``edge``."""
    u, v = mesh.edges[edge]
    a, b_ = mesh.verts[int(u)], mesh.verts[int(v)]
    return (float(a[0] + t * (b_[0] - a[0])), float(a[1] + t * (b_[1] - a[1])))


#: A candidate via site's per-outer-layer pad roster: ``(part index,
#: pin-local centre x/y, half w, half h)`` rows, read back into the part
#: frame per candidate (:func:`design._world_to_local` — the inverse of
#: the pose transform, so the rect distance is exact, mirror included).
_Pads = tuple[tuple[int, float, float, float, float], ...]


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
    layers, every already placed via, the board outline, and every
    keepout/mounting-hole zone — the same bodies the mesh keeps wires
    out of, which the old walk never checked (a site could land in a
    keepout or off the board).

    ``fallback_dia`` (gr524334): when the net-rule via (the comfortable
    house default, 0.75 mm on 4layer) finds no site anywhere on the
    branch, the walk retries at the fab-minimum diameter — a signal via
    carries no current, and a smaller legal barrel beats an unconnected
    branch. The returned site carries the diameter it was qualified
    against so realize and the emit guard measure the same geometry."""
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

    def walk(dia: float) -> tuple[int, float, float, float] | None:
        margin = dia / 2.0 + clearance
        step = 0.05
        for k in range(int(total / step) + 1):
            for s in (0.0,) if k == 0 else (k * step, -k * step):
                d = anchor_d + s
                if d < 0.0 or d > total:
                    continue
                x, y = _poly_point_at(pts, seg, d)
                if any(
                    math.dist((x, y), (px, py)) < (dia + pd) / 2.0 + clearance
                    for px, py, pd in placed
                ):
                    continue
                if not all(_pad_clear(x, y, pads, pose, margin) for pads in pad_sets):
                    continue
                pt = Point(x, y)
                # inside the board AND the barrel keep-out off the outline
                # — a polygon's distance() to an interior point is 0, so
                # the boundary is what measures edge clearance.
                if outline is not None and (
                    not outline.covers(pt) or outline.boundary.distance(pt) < margin
                ):
                    continue
                if any(ko.distance(pt) < margin for ko in keepouts):
                    continue
                e, t = _nearest_edge(mesh, (x, y))
                return e, t, x, y
        return None

    site = walk(via_dia)
    dia = via_dia
    if site is None and fallback_dia is not None and fallback_dia < via_dia:
        site = walk(float(fallback_dia))
        dia = float(fallback_dia)
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


def _seg_dist(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Distance from ``p`` to segment ``a→b``."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    dd = dx * dx + dy * dy
    t = (
        0.0
        if dd <= 1e-12
        else min(1.0, max(0.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / dd))
    )
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


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
    from precis.pcb.topoplace.wires import guard_pad_rects

    rules = net_rules or {}
    pose_a = np.asarray(d.pose0 if pose is None else pose)

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

    # ---- bend offsets: wires wrapping one obstacle vertex fan by rank
    wraps: dict[int, list[tuple[int, int]]] = {}
    bend_tasks: list[
        tuple[int, int, np.ndarray, np.ndarray, float, list[tuple[Any, Any, float]]]
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
        # branches; a lone wrap still needs its pad margin (the bend
        # sits ON the pad vertex) so it always shifts out.
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
            bend_tasks.append((bi, j, w, dirv, need, cands))

    # the smallest step along each bend's direction that puts the bend
    # AND both adjacent legs clear of its candidate pads — offsetting
    # the vertex alone can leave a leg grazing the box, and a moved
    # neighbour changes a leg, so the pass runs twice
    for _pass in range(2):
        for bi, j, w, dirv, need, cands in bend_tasks:
            if not cands:
                pts_out[bi][j] = (
                    float(w[0] + need * dirv[0]),
                    float(w[1] + need * dirv[1]),
                )
                continue
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

        def accept(q: list[list[tuple[float, float]]]) -> bool:
            lines = {
                bi: LineString([(float(x), float(y)) for x, y in q[bi]])
                for bi in range(len(q))
            }
            for bi in range(len(q)):
                for k in range(len(q[bi]) - 1):
                    leg = LineString([q[bi][k], q[bi][k + 1]])
                    for pi in foreign_pads(bi):
                        if leg.intersects(pads_poly[pi]):
                            return False
            for i in range(len(q)):
                for j in range(i + 1, len(q)):
                    if (i, j) in braided:
                        continue
                    if lines[i].crosses(lines[j]):
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
                    req = pad_req[bi]
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
    paths: Sequence[Sequence[tuple[float, float]]] | None = None,
) -> RoutePlan:
    """Layer assignment → pieces + via sites for one routed pose.

    Joins per branch: an F↔B SMD pair splits mid-branch onto its two
    forced layers (the free via, Decision 6); a forced end that disagrees
    with the assigned layer (a crossing yielded the branch to the other
    side) gets an end via at that end's first/last edge so the pad's
    mount side stays the terminal layer; and a crossing the whole-branch
    yielder left live is settled by a SPAN flip — only the shared,
    order-inverting run of edges changes layer, via-bracketed, never the
    whole branch (gr524239). Each join's site comes from
    :func:`_site_via`'s keepout walk — ``net_rules`` (net NAME →
    :class:`NetRules`, as :func:`realize_result` resolves them) sizes
    the barrel, with ``min_via_dia`` (the fab minimum) as the fallback
    when the rule via finds no site (gr524334); a join with no
    qualifying point at either diameter mints no via and lands on
    :attr:`RoutePlan.no_via`."""
    cdirs = _cdirs(mesh, wires)
    layers, crossings = assign_layers(d, wires, n_layers, mesh)
    # Span-flip the pairs the whole-branch pass could not take (gr524239)
    # and report only what survives both.
    span_toggles, span_flipped = _span_toggles(
        d, wires, layers, crossings, n_layers, cdirs
    )
    crossings = tuple(p for p in crossings if p not in span_flipped)
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
    pieces: list[RoutePiece] = []
    vias: list[ViaSite] = []
    placed: list[tuple[float, float, float]] = []
    no_via: list[int] = []
    for bi, b in enumerate(wires.branches):
        L = layers[bi]
        la = _pin_forced(d, b.pin_a, n_layers)
        lb = _pin_forced(d, b.pin_b, n_layers)
        # ``paths`` carries the spoke-offset polyline (gr520900) for the
        # emitted geometry; via siting and the edge/t bookkeeping stay
        # on ``b``'s corridor-true fields.
        bpts = paths[bi] if paths is not None else b.points
        # Layer switches along the branch, in edge-sequence positions
        # (0 = before the first edge, n = after the last). Three sources:
        # the Decision-6 F<->B mid split; pad-side stitches at the ends
        # when the assigned layer disagrees with a forced pad (gr524239 —
        # EVERY disagreeing end gets its via, else the far piece hangs
        # unconnected); and crossing-span flips, where only the shared
        # inverting run changes layer, never the whole branch.
        n_e = len(b.edges)
        start_layer = la if la is not None else L
        end_layer = lb if lb is not None else L
        targets: dict[int, int] = {}
        if la is not None and lb is not None and la != lb and n_e >= 2:
            targets[n_e // 2] = lb
        elif n_e >= 1:
            if la is not None and la != L:
                targets[0] = L
            if lb is not None and lb != L:
                targets[n_e] = end_layer
        for pos, tgt in span_toggles.get(bi, {}).items():
            targets[pos] = tgt
        if not targets:
            pieces.append(
                RoutePiece(b.net, bi, start_layer, tuple(bpts), b.pin_a, b.pin_b)
            )
            continue
        # Resolve switches in position order: a target equal to the
        # running layer is a no-op, and the branch must END on end_layer,
        # so a short running layer forces a final stitch at the last edge.
        layer = start_layer
        cuts: list[tuple[int, int]] = []
        for pos in sorted(targets):
            tgt = targets[pos]
            if tgt == layer:
                continue
            anchor = min(max(pos, 0), n_e - 1)
            if cuts and cuts[-1][0] == anchor:
                continue
            cuts.append((anchor, tgt))
            layer = tgt
        if layer != end_layer and (not cuts or cuts[-1][0] != n_e - 1):
            cuts.append((n_e - 1, end_layer))
        name = str(d.net_name[b.net]) if b.net < len(d.net_name) else str(b.net)
        nr = rules.get(name, _DEFAULT)
        via_dia = float(nr.via_dia_mm or _VIA_DIA_MM)
        clearance = float(nr.clearance_mm)
        pts = list(bpts)
        cut_xy: list[tuple[float, float]] = []
        run_lays = [start_layer, *(t for _, t in cuts)]
        sited = True
        for ei, _tgt in cuts:
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
            )
            if site is None:
                sited = False
                # keep the split at the intended crossing: the pieces
                # still describe where the join was wanted — it is the
                # via that is not minted.
                e = int(b.edges[min(ei, len(b.edges) - 1)])
                t = float(b.t[ei]) if ei < len(b.t) else 0.5
                cut_xy.append(_edge_xy(mesh, e, t))
                continue
            e, t, x, y, via_d = site
            vias.append(ViaSite(b.net, bi, e, t, x, y, via_d))
            placed.append((x, y, via_d))
            cut_xy.append((x, y))
        if not sited:
            no_via.append(bi)
        # split the polyline at the vertex nearest each via site — the
        # taut path crosses that edge at exactly the via point
        split = sorted(
            min(range(len(pts)), key=lambda i: math.dist(pts[i], xy)) for xy in cut_xy
        )
        bounds = [0, *split, len(pts) - 1]
        for pi in range(len(bounds) - 1):
            lo, hi = bounds[pi], bounds[pi + 1]
            run = list(pts[lo : hi + 1])
            if pi > 0:
                run.insert(0, cut_xy[pi - 1])
            if pi < len(cut_xy):
                run.append(cut_xy[pi])
            if len(run) >= 2:
                pieces.append(
                    RoutePiece(b.net, bi, run_lays[pi], tuple(run), b.pin_a, b.pin_b)
                )
    unrouted = tuple(sorted({u.net for u in wires.unreachable}))
    return RoutePlan(
        pieces=tuple(pieces),
        vias=tuple(vias),
        layers=tuple(layers),
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
) -> tuple[tuple[float, float], ...]:
    """The piece's taut path re-drawn in multiples of 45° — the DRC's
    rectilinear rule admits no other angle (``every drawn wire must be
    a multiple of 45deg``). Each off-angle leg becomes an L bend;
    ``clear`` (gr520900) is called as ``clear(p, c, q)`` and vetoes an
    L bend whose legs touch copper they must avoid — the
    horizontal-first corner is preferred, the vertical-first corner is
    taken when the first's legs are blocked, and when NEITHER clears
    the leg keeps its authored line rather than minting a bend across
    a pad (the emit guard remains the judge)."""
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
            out.append(qq)
    return tuple(out)


def realize_result(
    plan: RoutePlan,
    d: TopoDesign,
    ir: Any,
    n_layers: int,
    *,
    net_rules: Mapping[str, NetRules] | None = None,
    pose: Any = None,
) -> Any:
    """The plan as a :class:`precis.pcb.realize.RealizeResult` — the one
    shape the store path, ``result_copper_rows`` and the DRC gate all
    read. ``ir`` maps the design's ``net_name`` back to ir net ids.
    ``pose`` (gr520900) enables the pad-aware octilinear corner choice —
    a corner only lands where it clears foreign copper; absent, the
    legacy horizontal-first redraw runs (synthetic callers without a
    placed pose)."""
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
        pts = (
            _octilinear(p.points)
            if pad_polys is None
            else _octilinear(
                p.points,
                _piece_corner_clear(
                    p,
                    pad_polys,
                    nm,
                    lambda n: rules.get(n, _DEFAULT),
                    others,
                ),
            )
        )
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
                # a fallback-sited barrel keeps its qualified diameter
                # (gr524334) — 0.0 means the net rule's own via
                dia_mm=float(v.dia_mm or nr.via_dia_mm or _VIA_DIA_MM),
                drill_mm=float(nr.via_drill_mm or _VIA_DRILL_MM),
                layer_lo=lo,
                layer_hi=hi,
                endpoint="a",
            )
        )
    return RealizeResult(tuple(tracks), tuple(vias), ())


def _pad_polys(
    d: TopoDesign, ir: Any, n_layers: int, pose: np.ndarray
) -> dict[int, list[tuple[str, Polygon]]]:
    """World-frame pad polygons per layer, tagged by net name — the
    SAME pad set the DRC flashes: each pin's pad plus every non-primary
    footprint land (``pad_extra_*``: a pin's second same-named pad, a
    split slug half, an unpinned land — gr524239; the pin-only roster
    passed a track through copper the checker then errored on).
    ``NO_NET``/unpinned lands carry the empty name — foreign to every
    net. SMD pads live on their mount side, TH on all layers."""
    from precis.pcb.ir import NO_NET

    pad_polys: dict[int, list[tuple[str, Polygon]]] = {i: [] for i in range(n_layers)}

    def add_pad(
        part: int, px: float, py: float, hw: float, hh: float, pname: str, side: int
    ) -> None:
        if part >= len(pose) or not np.isfinite(pose[part, :2]).all():
            return
        if hw <= 0.0 or hh <= 0.0:
            return
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
            pad_polys[lay].append((pname, poly))

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
        )
    return pad_polys


def _piece_corner_clear(
    piece: RoutePiece,
    pad_polys: dict[int, list[tuple[str, Polygon]]],
    nm: Callable[[int], str],
    nrules: Callable[[str], NetRules],
    others: Iterable[tuple[LineString, float, float]] = (),
) -> Callable[[tuple[float, float], tuple[float, float], tuple[float, float]], bool]:
    """The octilinear corner test for one piece (gr520900): a candidate
    L bend ``p → c → q`` clears when BOTH legs keep the track's
    half-width plus the pairwise clearance off every foreign pad on
    its layer and the pairwise centreline spacing off every other
    piece's spoke path on the same layer — a clear corner point is
    not enough, the stub leg from the corner to ``q`` can still graze
    a pad, and an L redrawn across a neighbour's channel crosses it
    even though the taut paths never did."""
    nr = nrules(nm(piece.net))
    wi, ci = float(nr.track_width_mm), float(nr.clearance_mm)
    own = nm(piece.net)

    def clear(
        p: tuple[float, float], c: tuple[float, float], q: tuple[float, float]
    ) -> bool:
        path = LineString(
            [
                (float(p[0]), float(p[1])),
                (float(c[0]), float(c[1])),
                (float(q[0]), float(q[1])),
            ]
        )
        for pname, poly in pad_polys.get(piece.layer, []):
            if pname and pname == own:
                continue
            cp = float(nrules(pname).clearance_mm) if pname else ci
            if poly.distance(path) < wi / 2.0 + max(ci, cp) - 1e-9:
                return False
        for oline, wo, co in others:
            if path.distance(oline) < (wi + wo) / 2.0 + max(ci, co) - 1e-9:
                return False
        return True

    return clear


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
                pc.points, _piece_corner_clear(pc, pad_polys, nm, nrules, others)
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
            for pname, poly in pad_polys.get(lay, []):
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
            for pname, poly in pad_polys[lay]:
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


def route_design(
    d: TopoDesign,
    ir: Any,
    *,
    pose: Any = None,
    net_rules: Mapping[str, NetRules] | None = None,
    min_via_dia: float | None = None,
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
    on it."""
    from precis.pcb.topoplace.mesh import build_mesh
    from precis.pcb.topoplace.wires import build_wires

    mesh_pose = d.pose0 if pose is None else pose
    mesh = build_mesh(d, mesh_pose)
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
        paths=paths,
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
    return plan, realize_result(
        emit, d, ir, ir.n_layers, net_rules=net_rules, pose=mesh_pose
    )


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
        # guarded field — absent means nothing was withdrawn.
        if k in getattr(plan, "guarded", ()):
            problems.append(
                {
                    "reason": "short-risk",
                    "message": "copper withdrawn: would short another net (emit guard)",
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

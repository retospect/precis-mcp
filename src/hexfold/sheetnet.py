"""sheetnet — the one primitive layer for graphene-strip construction.

The DRY rule (docs/backlog/hexfold-multicell-prism.md, Reto 2026-10-22):
a wall, a fin, a rib, a rail arc and a cell wall are all one thing — a
graphene strip, possibly pored, placed in a frame, joined to other
strips at junctions.  ``strip`` mints the sheet (zigzag- or
armchair-edged), ``pore`` removes ring cells, ``place`` puts a strip in
a frame, ``junction`` joins rims (k2 fuse, k3 seam of flavour ``sp2`` or
``sp3-z``), and ``assemble`` is the only assembly loop over the parts.
Every tube builder in this package is a layout over these — new
vocabulary arrives only as a parameter of a primitive or a layout,
never as a new builder module.

Signatures are the contract: the builders above keep atom ordinal order
(strips, then walls, then seams), ``AtomPath`` names and seed
coordinates byte-identical (tests/hexfold/test_sha_goldens.py enforces
it), so the internals are the code they replace — ``y_junction._sheet``,
``join._seam_k3``, the railtube pore machinery, the sp3 armchair sheet —
moved here.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from .build import Atom, JunctionRecord, Net
from .ids import AtomPath
from .join import (
    Block,
    _adjacency,
    _seam_k3,
    _ZigzagSegment,
)
from .lattice import Lattice, Site
from .report import Report
from .text import Spec

try:  # construction-trace frames; absent under a standalone hexfold install
    from precis_geom.trace import record as _trace_record
    from precis_geom.trace import traced as _traced
except ImportError:  # pragma: no cover

    def _traced(
        fn: Callable[..., Any] | None = None,
        *,
        kind: str = "core",
        name: str | None = None,
    ) -> Any:
        return fn if fn is not None else (lambda f: f)

    def _trace_record(
        *, made: dict[str, int] | None = None, touched: dict[str, Any] | None = None
    ) -> None:
        return None


#: an inward strip's tip must keep this much clearance from whatever it
#: approaches (the tube axis, a far wall, a neighbouring rib) — one gate,
#: once (was ``fin.INWARD_TIP_CLEARANCE_A`` and the inline copies).
INWARD_TIP_CLEARANCE_A = 2.0

#: the GFN2-xTB-relaxed k3-sp3-z dimer length: paired seam atoms sit
#: ``(a - DIMER_A)/2`` toward each other along the axis (was
#: ``sp3_seam._place_k3z``'s 1.60 and channel's inline ``DIMER_A``).
DIMER_A = 1.60


@dataclass(frozen=True)
class _ArmchairSegment:
    """Private straight open armchair rim: a DDSS-...-DDSS boundary walk
    (moved from ``sp3_seam`` — the rim type belongs to the strip layer).

    Each armchair period contributes a degree-2 dimer pair:
    ``dangling_a`` atoms whose missing bond tilts -x along the rim and
    ``dangling_b`` tilting +x; the intervening S sites have degree 3.
    Neither a cyclic Port nor a new authored port/rail grammar — the same
    private status as :class:`join._ZigzagSegment`.
    """

    name: str
    atoms: tuple[int, ...]
    dangling_a: tuple[int, ...]
    dangling_b: tuple[int, ...]
    endpoints: tuple[int, int]


def _count(value: object, lo: int, hi: int, what: str, label: str = "seam tube") -> int:
    """Integer parameter bound, shared by every layout (was ``fin._count``
    and ``fin120._count``; ``label`` carries each builder's error prefix —
    "seam tube", "fin")."""
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"{label} {what} must be an integer in [{lo},{hi}]")
    return value


def strip_height(rows: int, sigma: float) -> float:
    """A strip's height across the rows: ``(1.5 (rows - 1) + 0.5) sigma``."""
    return (1.5 * (rows - 1) + 0.5) * sigma


def arc_radius(y: float, sigma: float, span: float) -> tuple[float, float]:
    """``(r, theta0)``: the circle radius an arc of cross-section height
    ``y`` is seeded on so each end's dangling atoms sit ``sigma`` from
    their seam atoms at the +-60-degree positions — the shared fixed
    point of ``fin120.wall_radius`` (``span = pi``, ``y = y_max +
    0.5 sigma``) and ``railtube.rail_radius`` (``span = 2 pi/N``,
    ``y = y_max``)."""
    r = y / span
    theta0 = 0.0
    for _ in range(60):
        theta0 = math.asin(min(1.0, 0.5 * math.sqrt(3.0) * sigma / r))
        r = y / (span - 2.0 * theta0)
    return r, theta0


# ---------------------------------------------------------------- strip


def _ord(periods: int, j: int, i: int, s: int) -> int:
    """Ordinal of a ``_sheet`` site ``(j, i, s)`` in its site order
    ``(j, i, s)`` for ``j`` in ``0..rows-1``, ``i`` in ``-1..periods``."""
    return (j * (periods + 2) + (i + 1)) * 2 + s


def _cell_ring(periods: int, u: int, v: int) -> tuple[int, ...]:
    """Ordinals of the hexagon a zigzag ``strip`` mints at cell ``(u,
    v)`` — ``u`` the cell index along the axis, ``v`` along the rows."""
    j, i = v, u
    if j % 2 == 0:
        face = (
            (j, i, 1),
            (j, i + 1, 0),
            (j, i + 1, 1),
            (j + 1, i + 1, 0),
            (j + 1, i + 1, 1),
            (j + 1, i, 0),
        )
    else:
        face = (
            (j, i, 1),
            (j, i, 0),
            (j, i + 1, 1),
            (j + 1, i + 1, 0),
            (j + 1, i, 1),
            (j + 1, i, 0),
        )
    return tuple(_ord(periods, *site) for site in face)


def _zigzag_block(
    periods: int, rows: int, alive: set[tuple[int, int, int]], sigma: float
) -> Block:
    """The zigzag sheet over ``alive`` sites — ``y_junction._sheet``'s
    emission with atoms/bonds/rings restricted to surviving sites, so the
    ``drop`` + stub-prune path (fin's graft strip) reuses the same loop."""
    period = math.sqrt(3) * sigma
    sites = sorted(alive)
    ordinal = {site: n for n, site in enumerate(sites)}
    coords = np.asarray(
        [
            ((i + (0.5 if s != j % 2 else 0)) * period, (1.5 * j + 0.5 * s) * sigma, 0)
            for j, i, s in sites
        ],
        dtype=float,
    )
    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    for j in range(rows):
        for i in range(-1, periods + 1):
            pairs: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = [
                ((j, i, 0), (j, i, 1))
            ]
            if i < periods:
                pairs.append(((j, i, 1 - j % 2), (j, i + 1, j % 2)))
            if j + 1 < rows:
                pairs.append(((j, i, 1), (j + 1, i, 0)))
            for p, q in pairs:
                if p in ordinal and q in ordinal:
                    bonds.append((ordinal[p], ordinal[q], 1))
            if j + 1 < rows and i < periods:
                face = (
                    (
                        (j, i, 1),
                        (j, i + 1, 0),
                        (j, i + 1, 1),
                        (j + 1, i + 1, 0),
                        (j + 1, i + 1, 1),
                        (j + 1, i, 0),
                    )
                    if j % 2 == 0
                    else (
                        (j, i, 1),
                        (j, i, 0),
                        (j, i + 1, 1),
                        (j + 1, i + 1, 0),
                        (j + 1, i, 1),
                        (j + 1, i, 0),
                    )
                )
                if all(site in ordinal for site in face):
                    rings.append(tuple(ordinal[site] for site in face))
    return Block(("C",) * len(sites), coords, tuple(bonds), tuple(rings), {}, sigma)


def _pruned(
    alive: set[tuple[int, int, int]], periods: int, rows: int
) -> set[tuple[int, int, int]]:
    """Iteratively drop degree<2 stubs until the open ends are clean
    boundaries (the fin graft's guard-column deletion; the armchair
    sheet's own open ends use its site-bond variant inline)."""
    while True:
        degree: dict[tuple[int, int, int], int] = dict.fromkeys(alive, 0)
        for j in range(rows):
            for i in range(-1, periods + 1):
                for p, q in (
                    ((j, i, 0), (j, i, 1)),
                    ((j, i, 1 - j % 2), (j, i + 1, j % 2)),
                    ((j, i, 1), (j + 1, i, 0)),
                ):
                    if p in alive and q in alive:
                        degree[p] += 1
                        degree[q] += 1
        stubs = {site for site, d in degree.items() if d < 2}
        if not stubs:
            return alive
        alive -= stubs


def _segment_sites(
    kind: str, periods: int, rows: int
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """``(walk, dangling)`` of a zigzag strip's named rim in lattice
    sites: ``"rim"`` the bottom S-D-...-D-S edge, ``"top"`` the far edge
    (dangling ``(rows-1, i, 1)``, in phase when ``rows`` is even),
    ``"top_full"`` the whole top zigzag row including guard stubs (the
    k2 fuse face)."""
    j = 0 if kind == "rim" else rows - 1
    if kind == "top_full":
        walk = [(j, i, 1) for i in range(-1, periods + 1)]
        return walk, list(walk)
    if kind == "rim":
        walk = [(0, -1, 1)]
        for i in range(periods):
            walk.extend(((0, i, 0), (0, i, 1)))
        return walk, [(0, i, 0) for i in range(periods)]
    # kind == "top"
    walk = [(rows - 1, -1, 0)]
    for i in range(periods):
        walk.extend(((rows - 1, i, 1), (rows - 1, i, 0)))
    return walk, [(rows - 1, i, 1) for i in range(periods)]


def _zigzag_rims(
    sites: dict[tuple[int, int, int], int], periods: int, rows: int
) -> dict[str, _ZigzagSegment | _ArmchairSegment]:
    """The named zigzag rim segments through a site->ordinal map; a rim
    whose walk or dangling sites did not survive is absent."""
    out: dict[str, _ZigzagSegment | _ArmchairSegment] = {}
    for kind in ("rim", "top", "top_full"):
        walk_s, dangling_s = _segment_sites(kind, periods, rows)
        if any(site not in sites for site in walk_s + dangling_s):
            continue
        walk = tuple(sites[site] for site in walk_s)
        dangling = (
            walk if kind == "top_full" else tuple(sites[site] for site in dangling_s)
        )
        out[kind] = _ZigzagSegment("rim", walk, dangling, (walk[0], walk[-1]))
    return out


def _top_segment(periods: int, rows: int) -> _ZigzagSegment:
    """The top zigzag rim of a full ``strip`` sheet as a bondable
    segment — the shared top-rim lookup (was ``railtube._top_segment``).
    Dangling atoms ``(rows-1, i, 1)`` for ``i`` in ``0..periods-1``,
    walked S-D-...-D-S with the degree-3 ``(rows-1, i, 0)`` sites as S;
    in phase with the bottom rim's seam lattice when ``rows`` is even."""
    walk_s, dangling_s = _segment_sites("top", periods, rows)
    ordinal = {
        (j, i, s): _ord(periods, j, i, s)
        for j in range(rows)
        for i in range(-1, periods + 1)
        for s in (0, 1)
    }
    walk = tuple(ordinal[site] for site in walk_s)
    return _ZigzagSegment(
        "rim",
        walk,
        tuple(ordinal[site] for site in dangling_s),
        (walk[0], walk[-1]),
    )


@dataclass(frozen=True)
class Strip:
    """One sheet: a ``join.Block`` plus named rim segments, the
    lattice-site -> ordinal map, the sheet's ``rows``/``periods``/``edge``
    and the open-valence rim atoms around each pore."""

    block: Block
    rows: int
    periods: int
    edge: str
    rims: dict[str, _ZigzagSegment | _ArmchairSegment]
    sites: dict[tuple[int, int, int], int]
    pore_rims: tuple[tuple[int, ...], ...] = ()


@_traced
def strip(
    rows: int,
    periods: int,
    edge: str = "zigzag",
    drop: Iterable[tuple[int, int, int]] = (),
    sigma: float | None = None,
) -> Strip:
    """A flat graphene ribbon of ``rows`` row pairs along the axis with
    ``periods`` lattice periods, ``edge`` ``"zigzag"`` or ``"armchair"``.
    ``drop`` removes lattice sites before the degree<2 stub prune — the
    fin graft's guard-column deletion.  ``sigma`` overrides the seed bond
    length (a graft seats on its host tube's lattice).  Rim names:
    ``"rim"`` the open edge, ``"top"`` the far zigzag edge,
    ``"top_full"`` that whole row (zigzag only); armchair exposes
    ``"rim"`` only."""
    if rows < 1:
        raise ValueError(f"strip rows must be >= 1; got {rows}")
    if periods < 1:
        raise ValueError(f"strip periods must be >= 1; got {periods}")
    if edge == "armchair":
        return _armchair_strip(periods, rows)
    if edge != "zigzag":
        raise ValueError(f"strip edge must be 'zigzag' or 'armchair'; got {edge!r}")
    drops = set(drop)
    alive = {
        (j, i, s) for j in range(rows) for i in range(-1, periods + 1) for s in (0, 1)
    } - drops
    if drops:
        alive = _pruned(alive, periods, rows)
    sigma = Lattice().sigma_A if sigma is None else sigma
    block = _zigzag_block(periods, rows, alive, sigma)
    sites = {site: n for n, site in enumerate(sorted(alive))}
    out = Strip(block, rows, periods, edge, _zigzag_rims(sites, periods, rows), sites)
    _trace_record(made={"atoms": len(block.elements), "bonds": len(block.bonds)})
    return out


def _six_rings(adj: dict[int, list[int]]) -> list[tuple[int, ...]]:
    """All simple 6-cycles of a bipartite bond graph, each once (moved
    from ``sp3_seam`` — the armchair sheet's ring census)."""
    out: list[tuple[int, ...]] = []
    nbrs = {a: sorted(b) for a, b in adj.items()}
    for s in sorted(nbrs):
        stack: list[tuple[int, tuple[int, ...]]] = [(s, (s,))]
        while stack:
            v, path = stack.pop()
            if len(path) == 6:
                if s in nbrs.get(v, ()) and path[1] < path[5]:
                    out.append(path)
                continue
            for w in nbrs.get(v, ()):
                if w > s and w not in path:
                    stack.append((w, path + (w,)))
    return sorted(out)


def _boundary_atoms(
    bonds: tuple[tuple[int, int, int], ...],
    rings: tuple[tuple[int, ...], ...],
) -> tuple[int, ...]:
    """Atoms on the open boundary of a sheet subgraph (moved from
    ``sp3_seam`` — the seam-net rim bookkeeping shares it)."""
    used: dict[frozenset[int], int] = {}
    for ring in rings:
        for a, b in zip(ring, ring[1:] + ring[:1]):
            key = frozenset((a, b))
            used[key] = used.get(key, 0) + 1
    out: set[int] = set()
    for a, b, _k in bonds:
        if used.get(frozenset((a, b)), 0) < 2:
            out.add(a)
            out.add(b)
    return tuple(sorted(out))


def _armchair_strip(periods: int, levels: int) -> Strip:
    """``strip(edge="armchair")``: the moved ``sp3_seam._armchair_sheet``
    — a DDSS-...-DDSS bottom rim of ``periods`` dimer cells, ``levels``
    atom rows high, open ends stub-pruned."""
    sigma = Lattice().sigma_A
    cells = range(-1, periods + 1)
    grid = {(k, i, s) for k in range(levels) for i in cells for s in (0, 1)}

    def site_bonds(
        keep: set[tuple[int, int, int]],
    ) -> set[frozenset[tuple[int, int, int]]]:
        edges: set[frozenset[tuple[int, int, int]]] = set()
        for k, i, s in keep:
            if s != 0:
                continue
            shift = 1 if k % 2 == 0 else 0
            for q in ((k, i, 1), (k + 1, i - shift, 1), (k - 1, i - shift, 1)):
                if q in keep:
                    edges.add(frozenset(((k, i, 0), q)))
        return edges

    alive = set(grid)
    while True:
        edges = site_bonds(alive)
        deg = {s: 0 for s in alive}
        for e in edges:
            a, b = sorted(e)
            deg[a] += 1
            deg[b] += 1
        dead = {s for s in alive if deg[s] < 2}
        if not dead:
            break
        alive -= dead
    sites_l = sorted(alive)
    ordinal = {site: n for n, site in enumerate(sites_l)}
    coords = np.asarray(
        [
            (
                3 * sigma * i + 1.5 * sigma * (k % 2) + sigma * s,
                k * sigma * math.sqrt(3) / 2,
                0.0,
            )
            for k, i, s in sites_l
        ]
    )
    bonds = tuple(
        (ordinal[a], ordinal[b], 1)
        for a, b in sorted(tuple(e) for e in site_bonds(alive))
    )
    adj = _adjacency(bonds)
    rings = tuple(_six_rings(adj))
    rim_sites = [
        site
        for i in cells
        for site in ((0, i, 0), (0, i, 1), (1, i, 0), (1, i, 1))
        if site in alive
    ]
    walk = [ordinal[site] for site in rim_sites]
    segment = _ArmchairSegment(
        "rim",
        tuple(walk),
        tuple(ordinal[0, i, 0] for i in range(periods)),
        tuple(ordinal[0, i, 1] for i in range(periods)),
        (walk[0], walk[-1]),
    )
    return Strip(
        Block(("C",) * len(sites_l), coords, bonds, rings, {}, sigma),
        levels,
        periods,
        "armchair",
        {"rim": segment},
        {site: n for n, site in enumerate(sites_l)},
    )


# ----------------------------------------------------------------- pore


def pore_cells(
    pores: Sequence[tuple[int, int, int]] | None,
    periods: int,
    rows: int,
    parts: int,
    *,
    label: str,
    what: str,
) -> dict[int, list[tuple[int, int]]]:
    """Validate ``(part, u, v)`` pore cells — the shared railtube/prism
    guard: a pore touching a seam rim or a guard column is refused
    (touching pores are refused inside :func:`pore`).  ``label``/``what``
    carry the layout's error wording ("prism tube"/"wall",
    "rail tube"/"arc", ...)."""
    cells: dict[int, list[tuple[int, int]]] = {}
    for p, u, v in pores or ():
        if type(p) is not int or not 0 <= p < parts:
            raise ValueError(
                f"{label} pore {what} must be an integer in [0,{parts}), got {p!r}"
            )
        if (
            type(u) is not int
            or type(v) is not int
            or not 0 <= u <= periods - 2
            or not 2 <= v <= rows - 4
        ):
            raise ValueError(
                f"{label} pore cell ({u!r},{v!r}) on {what} {p} touches a "
                f"seam rim or guard column; cells must have u in "
                f"[0,{periods - 2}] and v in [2,{rows - 4}]"
            )
        cells.setdefault(p, []).append((u, v))
    return cells


def _without_atoms(block: Block, drop: set[int]) -> tuple[Block, np.ndarray]:
    """A copy of ``block`` with ``drop`` ordinals removed (atoms, the
    bonds and rings touching them) plus the old->new remap."""
    remap = np.full(len(block.elements), -1, dtype=np.int64)
    keep = np.array([i for i in range(len(block.elements)) if i not in drop])
    remap[keep] = np.arange(len(keep))
    return replace(
        block,
        elements=tuple(block.elements[i] for i in keep),
        coords=np.asarray(block.coords)[keep],
        bonds=tuple(
            (int(remap[i]), int(remap[j]), o)
            for i, j, o in block.bonds
            if i not in drop and j not in drop
        ),
        rings=tuple(
            tuple(int(remap[a]) for a in ring)
            for ring in block.rings
            if drop.isdisjoint(ring)
        ),
    ), remap


@_traced
def pore(
    st: Strip,
    cells: Sequence[tuple[int, int]],
    *,
    label: str = "sheet",
    part: str = "wall",
) -> Strip:
    """``st`` with the listed ``(u, v)`` ring cells removed — one hexagon
    each, leaving six open-valence rim atoms on an 18-atom boundary walk
    — with rim segments and ordinals remapped.  A pore sharing atoms
    with or touching another is refused; cell-range validation is
    :func:`pore_cells` and ``_segment_frame`` validation is the caller's
    (as before)."""
    block = st.block
    adjacency: dict[int, set[int]] = {}
    for i, j, _o in block.bonds:
        adjacency.setdefault(i, set()).add(j)
        adjacency.setdefault(j, set()).add(i)
    drops: set[int] = set()
    rims: list[list[int]] = []
    seen: list[tuple[int, int, set[int], set[int]]] = []
    for u, v in cells:
        ring = set(_cell_ring(st.periods, u, v))
        rim = {x for at in ring for x in adjacency.get(at, ())} - ring
        for pu, pv, prev_ring, prev_rim in seen:
            shared = ring & prev_ring
            if shared:
                raise ValueError(
                    f"{label} pores ({u},{v}) and ({pu},{pv}) on {part} share "
                    f"atoms {sorted(shared)}"
                )
            if (ring | rim) & (prev_ring | prev_rim) or any(
                adjacency.get(at, set()) & prev_rim for at in rim
            ):
                raise ValueError(
                    f"{label} pores ({u},{v}) and ({pu},{pv}) on {part} touch; "
                    "pores must sit at least one ring apart"
                )
        seen.append((u, v, ring, rim))
        rims.append(sorted(rim))
        drops |= ring
    if not drops:
        return st
    new_block, remap = _without_atoms(block, drops)
    sites = {site: int(remap[o]) for site, o in st.sites.items() if remap[o] >= 0}
    seg: dict[str, _ZigzagSegment | _ArmchairSegment] = {}
    for name, s in st.rims.items():
        if isinstance(s, _ArmchairSegment):
            seg[name] = _ArmchairSegment(
                s.name,
                tuple(int(remap[o]) for o in s.atoms),
                tuple(int(remap[o]) for o in s.dangling_a),
                tuple(int(remap[o]) for o in s.dangling_b),
                (int(remap[s.endpoints[0]]), int(remap[s.endpoints[1]])),
            )
        else:
            seg[name] = _ZigzagSegment(
                s.name,
                tuple(int(remap[o]) for o in s.atoms),
                tuple(int(remap[o]) for o in s.dangling),
                (int(remap[s.endpoints[0]]), int(remap[s.endpoints[1]])),
            )
    _trace_record(touched={"atoms": len(drops)})
    return Strip(
        new_block,
        st.rows,
        st.periods,
        st.edge,
        seg,
        sites,
        tuple(tuple(int(remap[o]) for o in rim) for rim in rims),
    )


# ---------------------------------------------------------------- place


@dataclass(frozen=True)
class Flat:
    """Strip height along ``direction`` from ``origin``: ``xy = origin +
    (sigma + y) dir`` — the first row lands ``sigma`` off the seam line
    and every dangling atom ``sigma`` off its seam atom."""

    origin: tuple[float, float]
    direction: tuple[float, float]


@dataclass(frozen=True)
class Arc:
    """Strip wrapped on a circle: ``theta = sign (phi + theta0 +
    y/radius)``, ``xy = radius (cos, sin) theta``.  ``sign = -1`` wraps
    the mirrored way — fin120's second wall half."""

    radius: float
    phi: float
    theta0: float
    sign: float = 1.0


@dataclass(frozen=True)
class HalfPlane:
    """Strip in the half-plane through the axis along ``radial``:
    ``depth = base + sign*sigma + sign*y``, ``xy = depth * radial`` —
    the flat ``(sigma + y)`` association written out.  ``sign = -1``
    points the strip at the axis (``base`` the seam radius), ``+1``
    outward.  ``nested=True`` keeps the fin120 seed's
    ``base + sign*(sigma + y)`` association instead (same maths, the
    seed's own float order)."""

    radial: tuple[float, float]
    base: float
    sign: float
    nested: bool = False


@_traced
def place(st: Strip | Block, frame: Flat | Arc | HalfPlane) -> np.ndarray:
    """World ``(N, 3)`` coordinates of a strip block in ``frame``: the
    local ``x`` becomes the axial ``z``, ``y`` the cross-section
    direction."""
    block = st.block if isinstance(st, Strip) else st
    c = np.asarray(block.coords)
    sigma = block.sigma
    if isinstance(frame, Flat):
        xy = np.asarray(frame.origin) + (sigma + c[:, 1])[:, None] * np.asarray(
            frame.direction
        )
    elif isinstance(frame, Arc):
        theta = frame.sign * (frame.phi + frame.theta0 + c[:, 1] / frame.radius)
        xy = np.stack(
            [frame.radius * np.cos(theta), frame.radius * np.sin(theta)], axis=1
        )
    elif isinstance(frame, HalfPlane):
        if frame.nested:
            depth = frame.base + frame.sign * (sigma + c[:, 1])
        else:
            depth = frame.base + frame.sign * sigma + frame.sign * c[:, 1]
        xy = depth[:, None] * np.asarray(frame.radial)
    else:
        raise ValueError(f"unknown frame {frame!r}")
    out = np.zeros((len(c), 3))
    out[:, :2] = xy
    out[:, 2] = c[:, 0]
    return out


# ------------------------------------------------------------- junction


@dataclass(frozen=True)
class Junction:
    """One junction spec — what :func:`junction` returns and
    :func:`assemble` resolves.  ``rims`` are ``(part index, rim name)``;
    ``seam`` is the minted column's part index; ``snap`` maps a junction
    rim index to a 2-D direction so the rim's dangling atoms are
    registered ``sigma`` off the seam line; ``record`` carries the
    ``JunctionRecord`` name when the layout reports one."""

    rims: tuple[tuple[int, str], ...]
    k: int
    flavour: str
    side: str | None
    seam: int
    seam_xy: tuple[float, float] | None = None
    snap: tuple[tuple[int, tuple[float, float]], ...] = ()
    record: str | None = None


@_traced
def junction(
    rims: Sequence[tuple[int, str]],
    k: int,
    flavour: str,
    side: str | None = None,
    *,
    seam: int,
    seam_xy: tuple[float, float] | None = None,
    snap: Sequence[tuple[int, tuple[float, float]]] = (),
    record: str | None = None,
) -> Junction:
    """Join rims at a junction: ``k = 2`` fuses two rim atom lists
    directly (the ``fin120.seam_tube`` far-side fuse, generalised — a
    k2 fold's minted face set in slice 3), ``k = 3`` mints one seam atom
    column through ``join._seam_k3`` (``flavour="sp2"`` the equal-120
    row, ``"sp3-z"`` the same row plus the dimer chain at
    :data:`DIMER_A`).  ``side`` selects fin/rib direction and, for
    folds, the H side.  Returns the spec ``assemble`` resolves into
    bonds, seam atoms and the pinned zone."""
    if k == 2 and len(rims) != 2:
        raise ValueError(f"a k2 junction takes exactly two rims; got {len(rims)}")
    if k == 3 and len(rims) != 3:
        raise ValueError(f"a k3 junction takes exactly three rims; got {len(rims)}")
    if k not in (2, 3):
        raise ValueError(f"junction k must be 2 or 3 here; got {k}")
    if k == 3 and flavour not in ("sp2", "sp3-z"):
        raise ValueError(
            f"junction k3 flavour must be 'sp2' or 'sp3-z'; got {flavour!r}"
        )
    return Junction(tuple(rims), k, flavour, side, seam, seam_xy, tuple(snap), record)


# ------------------------------------------------------------- assemble


@dataclass(frozen=True)
class Part:
    """One assembled part: ``inst`` the ``AtomPath`` instance, ``region``
    the net region name, ``block`` a strip's block (``strip`` alongside,
    for rim resolution and pore regions) or ``count`` minted atoms (a
    seam column) with ``element``/``hyb``."""

    inst: str
    region: str
    block: Block | None = None
    count: int = 0
    strip: Strip | None = None
    element: str = "C"
    hyb: str = "sp2"


@dataclass(frozen=True)
class Assembly:
    """What :func:`assemble` returns beside the net: part offsets, per-
    junction seam ordinals, the pinned registration zone, junction
    records and the rim-snap overrides the layout writes into its seed
    coordinates."""

    net: Net
    offsets: tuple[int, ...]
    seam_atoms: tuple[tuple[int, ...], ...]
    pinned: tuple[int, ...]
    junctions: tuple[JunctionRecord, ...]
    rim_overrides: tuple[tuple[int, tuple[float, float]], ...]


def _rim_seg(part: Part, name: str) -> _ZigzagSegment | _ArmchairSegment:
    if part.strip is None or name not in part.strip.rims:
        raise ValueError(f"part {part.inst!r} has no rim {name!r}")
    return part.strip.rims[name]


def _rim_block(part: Part) -> Block:
    if part.strip is None:
        raise ValueError(f"part {part.inst!r} carries no strip block")
    return part.strip.block


def _zig(seg: _ZigzagSegment | _ArmchairSegment) -> _ZigzagSegment:
    if not isinstance(seg, _ZigzagSegment):
        raise ValueError("a k3 junction and rim snaps take zigzag rims")
    return seg


@_traced
def assemble(
    parts: Sequence[Part],
    junctions: Sequence[Junction],
    coords: np.ndarray,
) -> Assembly:
    """The one assembly loop: ordinal offsets in part order, part bonds
    and rings, then each junction's minted bonds/rings, the pinned
    registration zone (seam atoms and their neighbours), ``AtomPath``
    atoms, regions (part regions then ``pore<N>``), junction records,
    and the rim-snap overrides.  ``coords`` is the layout's seed array
    (``place`` output per part plus seam columns); the snap overrides
    are written into it in place."""
    offsets: list[int] = []
    pos = 0
    for p in parts:
        offsets.append(pos)
        pos += len(p.block.elements) if p.block is not None else p.count
    if len(coords) != pos:
        raise ValueError(f"assemble coords have {len(coords)} rows for {pos} atoms")

    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    for p, off in zip(parts, offsets):
        if p.block is None:
            continue
        bonds.extend((i + off, j + off, o) for i, j, o in p.block.bonds)
        rings.extend(tuple(a + off for a in ring) for ring in p.block.rings)

    seam_atoms: list[tuple[int, ...]] = []
    pending: list[
        tuple[
            str,
            tuple[tuple[int, int], ...],
            tuple[tuple[int, ...], ...],
            tuple[int, ...],
        ]
    ] = []
    overrides: list[tuple[int, tuple[float, float]]] = []
    sigma = parts[0].block.sigma if parts and parts[0].block else Lattice().sigma_A
    for jspec in junctions:
        rim_parts = [parts[pi] for pi, _ in jspec.rims]
        rim_segs = [
            _rim_seg(part, name) for part, (_pi, name) in zip(rim_parts, jspec.rims)
        ]
        rim_offs = [offsets[pi] for pi, _ in jspec.rims]
        if jspec.k == 3:
            seam = tuple(
                range(
                    offsets[jspec.seam], offsets[jspec.seam] + parts[jspec.seam].count
                )
            )
            seam_atoms.append(seam)
            walks = tuple(
                tuple(o + off for o in seg.atoms)
                for seg, off in zip(rim_segs, rim_offs)
            )
            dangling = tuple(
                tuple(o + off for o in _zig(seg).dangling)
                for seg, off in zip(rim_segs, rim_offs)
            )
            sb, sr = _seam_k3(walks, dangling, seam)
            bonds.extend((int(i), int(j), int(o)) for i, j, o in sb)
            rings.extend(tuple(int(a) for a in ring) for ring in sr)
            if jspec.flavour == "sp3-z":
                for m in range(len(seam) // 2):
                    bonds.append((seam[2 * m], seam[2 * m + 1], 1))
            minted = (tuple((int(i), int(j)) for i, j, _o in sb), tuple(sr))
            minted_seam = seam
        elif jspec.k == 2:
            off_a, off_b = rim_offs
            top_a = tuple(o + off_a for o in rim_segs[0].atoms)
            top_b = tuple(o + off_b for o in rim_segs[1].atoms)
            if len(top_a) != len(top_b):
                raise ValueError("a k2 junction needs equal rim atom counts")
            adjacency = _adjacency(_rim_block(rim_parts[0]).bonds)
            fb: list[tuple[int, int, int]] = []
            fr: list[tuple[int, ...]] = []
            for ta, tb in zip(top_a, top_b):
                fb.append((ta, tb, 1))
            for t0, t1 in zip(rim_segs[0].atoms, rim_segs[0].atoms[1:]):
                mid: set[int] = set(adjacency.get(t0, ())) & set(adjacency.get(t1, ()))
                if len(mid) != 1:
                    continue
                m = next(iter(mid))
                fr.append(
                    (
                        off_a + t0,
                        off_a + m,
                        off_a + t1,
                        off_b + t1,
                        off_b + m,
                        off_b + t0,
                    )
                )
            bonds.extend(fb)
            rings.extend(fr)
            minted = (tuple((i, j) for i, j, _o in fb), tuple(fr))
            minted_seam = ()
        else:
            raise ValueError(f"junction k {jspec.k} unsupported")
        if jspec.record is not None:
            pending.append((jspec.record, minted[0], minted[1], minted_seam))
        if jspec.seam_xy is not None:
            for rim_idx, direction in jspec.snap:
                seg = rim_segs[rim_idx]
                off = rim_offs[rim_idx]
                for d in _zig(seg).dangling:
                    overrides.append(
                        (
                            off + d,
                            (
                                jspec.seam_xy[0] + sigma * direction[0],
                                jspec.seam_xy[1] + sigma * direction[1],
                            ),
                        )
                    )

    adjacency_map: dict[int, set[int]] = {}
    for i, j, _ in bonds:
        adjacency_map.setdefault(i, set()).add(j)
        adjacency_map.setdefault(j, set()).add(i)
    pinned: set[int] = set()
    zones: dict[tuple[int, ...], tuple[int, ...]] = {}
    for seam in seam_atoms:
        zone: set[int] = set(seam)
        for s in seam:
            pinned.add(s)
            zone.update(adjacency_map.get(s, ()))
        pinned.update(zone)
        zones[seam] = tuple(sorted(zone))
    jrecords = [
        JunctionRecord(
            name=name,
            fused=fused,
            faces=faces,
            atoms=zones.get(seam, seam),
        )
        for name, fused, faces, seam in pending
    ]

    atoms: list[Atom] = []
    regions: list[tuple[str, tuple[int, ...]]] = []
    n_pores = 0
    for p, off in zip(parts, offsets):
        n = len(p.block.elements) if p.block is not None else p.count
        atoms += [
            Atom(AtomPath(p.inst, Site(o, 0, 0)), off + o, p.element, p.hyb, p.inst)
            for o in range(n)
        ]
        regions.append((p.region, tuple(range(off, off + n))))
    for p, off in zip(parts, offsets):
        if p.strip is None:
            continue
        for rim in p.strip.pore_rims:
            regions.append((f"pore{n_pores}", tuple(o + off for o in rim)))
            n_pores += 1

    for idx, xy in overrides:
        coords[idx, :2] = xy
    net = Net(
        atoms=tuple(atoms),
        bonds=tuple(bonds),
        rings=tuple(rings),
        ports=(),
        regions=tuple(regions),
        report=Report(),
        spec=Spec(),
        lattice=Lattice(),
        seed3=tuple((float(r[0]), float(r[1]), float(r[2])) for r in coords),
        seed_kind="mixed",
        junctions=tuple(jrecords),
    )
    _trace_record(made={"atoms": len(atoms), "bonds": len(bonds)})
    return Assembly(
        net,
        tuple(offsets),
        tuple(seam_atoms),
        tuple(sorted(pinned)),
        tuple(jrecords),
        tuple(overrides),
    )

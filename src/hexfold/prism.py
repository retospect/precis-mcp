"""A closed tube of flat zigzag-edged graphene strips joined at equal-120
sp2 seams whose third sheet is an outward fin or an inward rib.

The prism-tube cross-section (docs/backlog/hexfold-prism-tube.md,
nanoreactor T2): ``len(seams)`` flat ``_sheet`` wall strips of ``rows``
zigzag rows span the edges of a polygon; every vertex is an equal-120 k3
seam of the two adjacent walls plus a third sheet of ``strip_rows`` rows
-- an outward fin (``+``) or an inward rib (``-``).  Like
:mod:`hexfold.railtube` the sheets are assembled first and each seam is
minted in place by :func:`hexfold.join._seam_k3`, because every wall
enters two seams.

Where the rail tube pays the cross-section's turning with wall curvature,
flat walls carry none -- it all sits in the seam signs.  Walking the
polygon as a turtle with equal side vectors, a ``+`` seam turns the
heading +60 degrees (interior angle 120, the fin bisects the exterior)
and a ``-`` seam turns -60 (reflex 240 interior, the rib bisects it).
Closure therefore needs net turn 360, i.e. ``n_plus - n_minus = 6`` --
necessary, not sufficient: the side vectors must also sum to zero and
the polygon must be simple.  Refusals are named ``prism.closure`` with
the measured reason.

Seed: the vertex-to-vertex side length is ``y_max + 2*sigma`` with
``y_max`` the strip height :mod:`~hexfold.railtube` computes from
``rows``; at each seam the three sheets sit 120 degrees apart around the
seam line, so every sheet's dangling atoms land sigma from the seam atom
along their own sheet direction (the wall directions are +-120 off the
third-sheet bisector).  The third-sheet direction is the bisector of the
two wall directions with the sign the geometry picks: ``-normalise(u+v)``
points outside the polygon for ``+`` and inside for ``-``.  No arcs, no
``r_wall``; ``radius_A`` is the polygon's mean vertex radius, for the
viewer only.  Inward ribs carry a clearance gate: a rib must keep
``INWARD_TIP_CLEARANCE_A`` from every wall and every other rib
(``prism.rib.clearance``).

``rows`` must be even, as for the rail tube.  Pores are authored per wall
at a lattice cell before composition, refused on a seam rim, a guard
column or another pore -- the rail tube's rule with ``wall`` for ``arc``.

Analytic preview topology only: no public grammar, no relaxation here
(the SE adapter relaxes with every seam registration pinned, fins and
ribs tethered to their half-planes, walls free).  Units are Angstrom.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np

from .build import Atom, JunctionRecord, Net
from .fin import INWARD_TIP_CLEARANCE_A
from .fin120 import _count
from .ids import AtomPath
from .join import Block, _seam_k3, _segment_frame, _ZigzagSegment
from .lattice import Lattice, Site
from .railtube import _cell_ring, _top_segment, _without_atoms
from .report import Report
from .text import Spec
from .y_junction import _sheet

#: ``seams`` length bound: a triangle is the smallest prism; past 24 the
#: seam string is noise, not a design.
SEAM_COUNT = (3, 24)
#: the walk is on the triangular lattice, so closure/simplicity tests are
#: exact to float noise; tolerance scales with the side length.
_CLOSE_TOL = 1e-6


@dataclass(frozen=True)
class PrismTube:
    """The bookkeeping :func:`prism_tube` returns beside the net."""

    seams: int
    #: ``'+'`` (fin, third sheet outward) / ``'-'`` (rib, inward) per seam
    signs: tuple[str, ...]
    #: cross-section vertices (the seam lines): centred on the vertex
    #: centroid, seam 0 on +x; (seams, 2)
    vertices: np.ndarray
    #: unit direction of each seam's third sheet in the cross-section
    strip_dirs: np.ndarray
    #: mean vertex distance from the centroid (viewer only)
    radius_A: float
    #: vertex-to-vertex wall side length
    side_A: float
    strip_height_A: float
    #: seam atoms per seam, in axial order
    seam_atoms: tuple[tuple[int, ...], ...]
    strip_atoms: tuple[tuple[int, ...], ...]
    wall_atoms: tuple[tuple[int, ...], ...]
    #: the open-valence rim atoms around each pore, composite ordinals
    pore_rims: tuple[tuple[int, ...], ...]
    #: seam atoms and their three neighbours: the pinned registration
    pinned: tuple[int, ...]
    coords: np.ndarray


def _signs(seams: object) -> tuple[str, ...]:
    """Normalise the seam string: ``+`` fins, ``-`` and U+2212 ribs."""
    if not isinstance(seams, str):
        raise ValueError(
            f"prism.seams: seams must be one string of '+'/'-' signs "
            f"(one per seam); got {seams!r}"
        )
    text = seams.replace("−", "-")
    if any(c not in "+-" for c in text):
        raise ValueError(f"prism.seams: seams takes only '+', '-', '−'; got {seams!r}")
    if not SEAM_COUNT[0] <= len(text) <= SEAM_COUNT[1]:
        raise ValueError(
            f"prism.seams: a prism takes {SEAM_COUNT[0]}..{SEAM_COUNT[1]} "
            f"seams; got {len(text)}"
        )
    return tuple("+" if c == "+" else "-" for c in text)


def _cross2(o: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    return float((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))


def _pt_seg(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    ap, ab = p - a, b - a
    t = float(np.clip((ap @ ab) / max(float(ab @ ab), 1e-30), 0.0, 1.0))
    return float(np.linalg.norm(p - (a + t * ab)))


def _seg_seg_dist(
    p1: np.ndarray, p2: np.ndarray, p3: np.ndarray, p4: np.ndarray
) -> float:
    """Minimum distance between two closed 2-D segments (0 on a proper
    crossing)."""
    d1, d2 = _cross2(p3, p4, p1), _cross2(p3, p4, p2)
    d3, d4 = _cross2(p1, p2, p3), _cross2(p1, p2, p4)
    if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0):
        return 0.0
    return min(
        _pt_seg(p1, p3, p4),
        _pt_seg(p2, p3, p4),
        _pt_seg(p3, p1, p2),
        _pt_seg(p4, p1, p2),
    )


def _polygon(
    signs: tuple[str, ...], side: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Walk the cross-section: ``(vertices, edge_dirs, strip_dirs)`` after
    centring on the vertex centroid and putting seam 0 on +x, or the
    named ``prism.closure`` refusal."""
    n = len(signs)
    net_deg = (signs.count("+") - signs.count("-")) * 60
    if net_deg != 360:
        raise ValueError(
            f"prism.closure: net turn {net_deg}°, not 360° — a closed "
            f"prism cross-section needs #plus − #minus = 6 (got "
            f"{signs.count('+')}−{signs.count('-')} in {''.join(signs)!r})"
        )
    headings = np.zeros(n)
    for k in range(1, n):
        headings[k] = headings[k - 1] + math.radians(60 if signs[k] == "+" else -60)
    edges = np.stack([np.cos(headings), np.sin(headings)], axis=1)
    verts = np.zeros((n, 2))
    for k in range(n - 1):
        verts[k + 1] = verts[k] + side * edges[k]
    gap = float(np.linalg.norm(verts[0] - (verts[-1] + side * edges[-1])))
    if gap > _CLOSE_TOL * side:
        raise ValueError(
            f"prism.closure: open polygon (gap {gap:.2f} A) — the "
            f"{n} side vectors do not sum to zero for {''.join(signs)!r}"
        )
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if (
                _seg_seg_dist(
                    verts[i], verts[(i + 1) % n], verts[j], verts[(j + 1) % n]
                )
                <= _CLOSE_TOL * side
            ):
                raise ValueError(
                    f"prism.closure: self-intersecting — walls {i} and {j} "
                    f"meet away from a seam for {''.join(signs)!r}"
                )
    # third-sheet directions: -(u+v)/|u+v| of the wall directions u=-e[k-1],
    # v=e[k] is the exterior bisector at '+' and the interior at '-'
    dirs = np.zeros((n, 2))
    for k in range(n):
        s = edges[k] - edges[(k - 1) % n]
        dirs[k] = -s / np.linalg.norm(s)
    centre = verts.mean(axis=0)
    verts = verts - centre
    phi0 = math.atan2(verts[0, 1], verts[0, 0])
    rot = np.array(
        [[math.cos(phi0), math.sin(phi0)], [-math.sin(phi0), math.cos(phi0)]]
    )
    return verts @ rot.T, edges @ rot.T, dirs @ rot.T


def _rib_clearance(
    signs: tuple[str, ...],
    verts: np.ndarray,
    dirs: np.ndarray,
    strip_height: float,
    sigma: float,
) -> None:
    """``prism.rib.clearance``: an inward rib keeps
    :data:`INWARD_TIP_CLEARANCE_A` from every non-incident wall and every
    other rib (tips and shafts alike -- a crossing is a clash, not a
    design)."""
    n = len(signs)
    ribs = [k for k in range(n) if signs[k] == "-"]
    starts = {k: verts[k] + sigma * dirs[k] for k in ribs}
    tips = {k: verts[k] + (sigma + strip_height) * dirs[k] for k in ribs}
    for k in ribs:
        for j in range(n):
            if j == k or j == (k - 1) % n:
                continue
            d = _seg_seg_dist(starts[k], tips[k], verts[j], verts[(j + 1) % n])
            if d < INWARD_TIP_CLEARANCE_A:
                raise ValueError(
                    f"prism.rib.clearance: rib at seam {k} comes within "
                    f"{d:.2f} A of wall {j}, under "
                    f"{INWARD_TIP_CLEARANCE_A:.1f} A — a longer "
                    "strip_rows or a narrower polygon puts the rib through "
                    "the far wall"
                )
    for a_, k in enumerate(ribs):
        for m in ribs[a_ + 1 :]:
            d = _seg_seg_dist(starts[k], tips[k], starts[m], tips[m])
            if d < INWARD_TIP_CLEARANCE_A:
                raise ValueError(
                    f"prism.rib.clearance: ribs at seams {k} and {m} come "
                    f"within {d:.2f} A of each other, under "
                    f"{INWARD_TIP_CLEARANCE_A:.1f} A"
                )


def _pore_cells(
    pores: Sequence[tuple[int, int, int]] | None,
    periods: int,
    rows: int,
    walls: int,
) -> dict[int, list[tuple[int, int]]]:
    """Validate ``(wall, u, v)`` pore cells -- :func:`railtube._pore_cells`
    with the wall count for the arc count; a pore touching a seam rim, a
    guard column or another pore is refused."""
    cells: dict[int, list[tuple[int, int]]] = {}
    for wall, u, v in pores or ():
        if type(wall) is not int or not 0 <= wall < walls:
            raise ValueError(
                f"prism tube pore wall must be an integer in [0,{walls}), got {wall!r}"
            )
        if (
            type(u) is not int
            or type(v) is not int
            or not 0 <= u <= periods - 2
            or not 2 <= v <= rows - 4
        ):
            raise ValueError(
                f"prism tube pore cell ({u!r},{v!r}) on wall {wall} touches a "
                f"seam rim or guard column; cells must have u in "
                f"[0,{periods - 2}] and v in [2,{rows - 4}]"
            )
        cells.setdefault(wall, []).append((u, v))
    return cells


def prism_tube(
    periods: int,
    rows: int,
    strip_rows: int,
    seams: str,
    pores: Sequence[tuple[int, int, int]] | None = None,
) -> tuple[Net, PrismTube]:
    """``periods`` seam periods along the axis, ``len(seams)`` flat wall
    strips of ``rows`` row pairs each, and one fin/rib strip of
    ``strip_rows`` row pairs per seam (outward on ``+``, inward on ``-``),
    the walls meeting at the signed equal-120 seam lines of the
    cross-section polygon.  ``pores`` are ``(wall, u, v)`` cells of each
    wall's ``_sheet``; each removes one hexagon before composition."""
    periods = _count(periods, 2, 60, "periods")
    rows = _count(rows, 4, 40, "wall rows")
    strip_rows = _count(strip_rows, 2, 12, "strip rows")
    if rows % 2:
        raise ValueError(
            "prism wall rows must be even so the top zigzag rim lands "
            "in phase with the seam lattice"
        )
    signs = _signs(seams)
    n = len(signs)
    lattice = Lattice()
    sigma = lattice.sigma_A
    a = math.sqrt(3.0) * sigma
    y_max = (1.5 * (rows - 1) + 0.5) * sigma
    side = y_max + 2.0 * sigma
    strip_height = (1.5 * (strip_rows - 1) + 0.5) * sigma
    verts, edges, dirs = _polygon(signs, side)
    _rib_clearance(signs, verts, dirs, strip_height, sigma)

    cells = _pore_cells(pores, periods, rows, n)

    strip_block, strip_rim = _sheet(periods, strip_rows)
    wall_proto, wall_bot = _sheet(periods, rows)
    wall_top = _top_segment(periods, rows)
    if _segment_frame(strip_block, strip_rim) is None:
        raise ValueError("prism strip rim failed segment validation")
    if _segment_frame(wall_proto, wall_bot) is None:
        raise ValueError("prism wall bottom rim failed segment validation")
    if _segment_frame(wall_proto, wall_top) is None:
        raise ValueError("prism wall top rim failed segment validation")

    adjacency: dict[int, set[int]] = {}
    for i, j, _ in wall_proto.bonds:
        adjacency.setdefault(i, set()).add(j)
        adjacency.setdefault(j, set()).add(i)

    # pores: refuse touching rings, then drop the hexagon atoms per wall
    wall_blocks: list[Block] = []
    wall_tops: list[_ZigzagSegment] = []
    wall_bots: list[_ZigzagSegment] = []
    wall_pore_rims: list[list[list[int]]] = []  # per wall, per pore
    for k in range(n):
        drops: set[int] = set()
        rims: list[list[int]] = []
        seen: list[tuple[int, int, set[int], set[int]]] = []
        for u, v in cells.get(k, []):
            ring = set(_cell_ring(periods, u, v))
            rim = {x for at in ring for x in adjacency.get(at, ())} - ring
            for pu, pv, prev_ring, prev_rim in seen:
                shared = ring & prev_ring
                if shared:
                    raise ValueError(
                        f"prism tube pores ({u},{v}) and ({pu},{pv}) on wall "
                        f"{k} share atoms {sorted(shared)}"
                    )
                if (ring | rim) & (prev_ring | prev_rim) or any(
                    adjacency.get(at, set()) & prev_rim for at in rim
                ):
                    raise ValueError(
                        f"prism tube pores ({u},{v}) and ({pu},{pv}) on wall "
                        f"{k} touch; pores must sit at least one ring apart"
                    )
            seen.append((u, v, ring, rim))
            rims.append(sorted(rim))
            drops |= ring
        if drops:
            block, remap = _without_atoms(wall_proto, drops)
            top = _ZigzagSegment(
                wall_top.name,
                tuple(int(remap[o]) for o in wall_top.atoms),
                tuple(int(remap[o]) for o in wall_top.dangling),
                (
                    int(remap[wall_top.endpoints[0]]),
                    int(remap[wall_top.endpoints[1]]),
                ),
            )
            bot = _ZigzagSegment(
                wall_bot.name,
                tuple(int(remap[o]) for o in wall_bot.atoms),
                tuple(int(remap[o]) for o in wall_bot.dangling),
                (
                    int(remap[wall_bot.endpoints[0]]),
                    int(remap[wall_bot.endpoints[1]]),
                ),
            )
            rims = [[int(remap[o]) for o in rim] for rim in rims]
        else:
            block = wall_proto
            top, bot = wall_top, wall_bot
        if _segment_frame(block, top) is None or _segment_frame(block, bot) is None:
            raise ValueError(f"prism wall {k} rim failed segment validation")
        wall_blocks.append(block)
        wall_tops.append(top)
        wall_bots.append(bot)
        wall_pore_rims.append(rims)

    n_s = len(strip_block.elements)
    s_off = [k * n_s for k in range(n)]
    w_off: list[int] = []
    offset = n * n_s
    for k in range(n):
        w_off.append(offset)
        offset += len(wall_blocks[k].elements)
    y_off = [offset + k * periods for k in range(n)]
    total = offset + n * periods

    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    for k in range(n):
        off = s_off[k]
        bonds.extend((i + off, j + off, o) for i, j, o in strip_block.bonds)
        rings.extend(tuple(a_ + off for a_ in ring) for ring in strip_block.rings)
    for k in range(n):
        off = w_off[k]
        blk = wall_blocks[k]
        bonds.extend((i + off, j + off, o) for i, j, o in blk.bonds)
        rings.extend(tuple(a_ + off for a_ in ring) for ring in blk.rings)
    seam_atoms: list[tuple[int, ...]] = []
    junctions: list[JunctionRecord] = []
    for k in range(n):
        seam = tuple(range(y_off[k], y_off[k] + periods))
        seam_atoms.append(seam)
        walks = (
            tuple(o + s_off[k] for o in strip_rim.atoms),
            tuple(o + w_off[(k - 1) % n] for o in wall_tops[(k - 1) % n].atoms),
            tuple(o + w_off[k] for o in wall_bots[k].atoms),
        )
        dangling = (
            tuple(o + s_off[k] for o in strip_rim.dangling),
            tuple(o + w_off[(k - 1) % n] for o in wall_tops[(k - 1) % n].dangling),
            tuple(o + w_off[k] for o in wall_bots[k].dangling),
        )
        sb, sr = _seam_k3(walks, dangling, seam)
        bonds.extend(sb)
        rings.extend(sr)
        junctions.append(
            JunctionRecord(
                name=f"seam:{k}:{signs[k]}",
                fused=tuple((i, j) for i, j, _o in sb),
                faces=sr,
                atoms=(),
            )
        )

    # the seed: strip k in the half-plane along its seam's third-sheet
    # direction, wall k flat between vertices k and k+1, every dangling
    # atom exactly sigma off its seam atom along its own sheet direction,
    # seam atoms on the polygon vertices
    coords = np.zeros((total, 3))
    for k in range(n):
        sl = slice(s_off[k], s_off[k] + n_s)
        xy = verts[k] + (sigma + strip_block.coords[:, 1])[:, None] * dirs[k]
        coords[sl, :2] = xy
        coords[sl, 2] = strip_block.coords[:, 0]
    for k in range(n):
        blk = wall_blocks[k]
        sl = slice(w_off[k], w_off[k] + len(blk.elements))
        xy = verts[k] + (sigma + blk.coords[:, 1])[:, None] * edges[k]
        coords[sl, :2] = xy
        coords[sl, 2] = blk.coords[:, 0]
    for k in range(n):
        for i, atom in enumerate(seam_atoms[k]):
            coords[atom] = (verts[k, 0], verts[k, 1], i * a)

    adjacency_map: dict[int, set[int]] = {}
    for i, j, _ in bonds:
        adjacency_map.setdefault(i, set()).add(j)
        adjacency_map.setdefault(j, set()).add(i)
    pinned: set[int] = set()
    zones: list[tuple[int, ...]] = []
    for k in range(n):
        zone: set[int] = set(seam_atoms[k])
        for s in seam_atoms[k]:
            pinned.add(s)
            zone.update(adjacency_map.get(s, ()))
        pinned.update(zone)
        zones.append(tuple(sorted(zone)))
    junctions = [replace(j, atoms=z) for j, z in zip(junctions, zones, strict=True)]

    def paths(inst: str, start: int, count: int) -> tuple[Atom, ...]:
        return tuple(
            Atom(AtomPath(inst, Site(o, 0, 0)), start + o, "C", "sp2", inst)
            for o in range(count)
        )

    atoms: list[Atom] = []
    for k in range(n):
        atoms += paths(f"s{k}", s_off[k], n_s)
    for k in range(n):
        atoms += paths(f"w{k}", w_off[k], len(wall_blocks[k].elements))
    for k in range(n):
        atoms += paths(f"y{k}", y_off[k], periods)

    regions: list[tuple[str, tuple[int, ...]]] = []
    for k in range(n):
        regions.append((f"strip{k}", tuple(range(s_off[k], s_off[k] + n_s))))
    for k in range(n):
        regions.append(
            (
                f"wall{k}",
                tuple(range(w_off[k], w_off[k] + len(wall_blocks[k].elements))),
            )
        )
    for k in range(n):
        regions.append((f"seam{k}", seam_atoms[k]))
    pore_ordinals: list[tuple[int, ...]] = []
    for k in range(n):
        for rim_atoms in wall_pore_rims[k]:
            ords = tuple(o + w_off[k] for o in rim_atoms)
            pore_ordinals.append(ords)
            regions.append((f"pore{len(pore_ordinals) - 1}", ords))

    net = Net(
        atoms=tuple(atoms),
        bonds=tuple(bonds),
        rings=tuple(rings),
        ports=(),
        regions=tuple(regions),
        report=Report(),
        spec=Spec(),
        lattice=lattice,
        seed3=tuple((float(r[0]), float(r[1]), float(r[2])) for r in coords),
        seed_kind="mixed",
        junctions=tuple(junctions),
    )
    return net, PrismTube(
        n,
        signs,
        verts,
        dirs,
        float(np.linalg.norm(verts, axis=1).mean()),
        side,
        strip_height,
        tuple(seam_atoms),
        tuple(tuple(range(s_off[k], s_off[k] + n_s)) for k in range(n)),
        tuple(
            tuple(range(w_off[k], w_off[k] + len(wall_blocks[k].elements)))
            for k in range(n)
        ),
        tuple(pore_ordinals),
        tuple(sorted(pinned)),
        coords,
    )

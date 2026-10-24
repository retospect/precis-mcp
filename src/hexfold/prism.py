"""A closed tube of flat zigzag-edged graphene strips joined at equal-120
sp2 seams whose third sheet is an outward fin or an inward rib.

The prism-tube cross-section (docs/backlog/hexfold-prism-tube.md,
nanoreactor T2): ``len(seams)`` flat ``strip`` wall strips of ``rows``
zigzag rows span the edges of a polygon; every vertex is an equal-120 k3
junction of the two adjacent walls plus a third sheet of ``strip_rows``
rows -- an outward fin (``+``) or an inward rib (``-``).  A **polygon
layout over :mod:`hexfold.sheetnet`**: like :mod:`hexfold.railtube` the
sheets are assembled first and each seam is minted in place by
``junction(k=3)``, because every wall enters two seams.

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
``y_max`` the strip height :func:`~hexfold.sheetnet.strip_height`
computes from ``rows``; at each seam the three sheets sit 120 degrees
apart around the seam line, so every sheet's dangling atoms land sigma
from the seam atom along their own sheet direction (the wall directions
are +-120 off the third-sheet bisector).  The third-sheet direction is
the bisector of the two wall directions with the sign the geometry
picks: ``-normalise(u+v)`` points outside the polygon for ``+`` and
inside for ``-``.  No arcs, no ``r_wall``; ``radius_A`` is the
polygon's mean vertex radius, for the viewer only.  Inward ribs carry a
clearance gate: a rib must keep ``INWARD_TIP_CLEARANCE_A`` from every
wall and every other rib (``prism.rib.clearance``).

``rows`` must be even, as for the rail tube.  Pores are authored per
wall at a lattice cell before composition, refused on a seam rim, a
guard column or another pore -- ``pore``'s rule with ``wall`` for the
part name.

Analytic preview topology only: no public grammar, no relaxation here
(the SE adapter relaxes with every seam registration pinned, fins and
ribs tethered to their half-planes, walls free).  Units are Angstrom.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .build import Net
from .join import _segment_frame
from .lattice import Lattice
from .sheetnet import (
    INWARD_TIP_CLEARANCE_A,
    Flat,
    Part,
    _count,
    _zig,
    assemble,
    junction,
    place,
    pore,
    pore_cells,
    strip,
    strip_height,
)

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
    wall's ``strip``; each removes one hexagon before composition."""
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
    side = strip_height(rows, sigma) + 2.0 * sigma
    height = strip_height(strip_rows, sigma)
    verts, edges, dirs = _polygon(signs, side)
    _rib_clearance(signs, verts, dirs, height, sigma)

    cells = pore_cells(pores, periods, rows, n, label="prism tube", what="wall")

    strips = [strip(strip_rows, periods) for _ in range(n)]
    walls = [
        pore(
            strip(rows, periods), cells.get(k, ()), label="prism tube", part=f"wall {k}"
        )
        for k in range(n)
    ]
    proto = strip(rows, periods)
    if _segment_frame(strips[0].block, _zig(strips[0].rims["rim"])) is None:
        raise ValueError("prism strip rim failed segment validation")
    if _segment_frame(proto.block, _zig(proto.rims["rim"])) is None:
        raise ValueError("prism wall bottom rim failed segment validation")
    if _segment_frame(proto.block, _zig(proto.rims["top"])) is None:
        raise ValueError("prism wall top rim failed segment validation")
    for k in range(n):
        if (
            _segment_frame(walls[k].block, _zig(walls[k].rims["top"])) is None
            or _segment_frame(walls[k].block, _zig(walls[k].rims["rim"])) is None
        ):
            raise ValueError(f"prism wall {k} rim failed segment validation")

    parts = (
        [Part(f"s{k}", f"strip{k}", strips[k].block, strip=strips[k]) for k in range(n)]
        + [Part(f"w{k}", f"wall{k}", walls[k].block, strip=walls[k]) for k in range(n)]
        + [Part(f"y{k}", f"seam{k}", count=periods) for k in range(n)]
    )
    junctions = [
        junction(
            (
                (k, "rim"),
                (n + (k - 1) % n, "top"),
                (n + k, "rim"),
            ),
            3,
            "sp2",
            seam=2 * n + k,
            record=f"seam:{k}:{signs[k]}",
        )
        for k in range(n)
    ]

    # the seed: strip k in the half-plane along its seam's third-sheet
    # direction, wall k flat between vertices k and k+1, every dangling
    # atom exactly sigma off its seam atom along its own sheet direction,
    # seam atoms on the polygon vertices
    offs: list[int] = []
    pos = 0
    for p in parts:
        offs.append(pos)
        pos += len(p.block.elements) if p.block is not None else p.count
    coords = np.zeros((pos, 3))
    for k in range(n):
        blk = strips[k].block
        coords[offs[k] : offs[k] + len(blk.elements)] = place(
            strips[k],
            Flat(
                (float(verts[k, 0]), float(verts[k, 1])),
                (float(dirs[k, 0]), float(dirs[k, 1])),
            ),
        )
    for k in range(n):
        blk = walls[k].block
        coords[offs[n + k] : offs[n + k] + len(blk.elements)] = place(
            walls[k],
            Flat(
                (float(verts[k, 0]), float(verts[k, 1])),
                (float(edges[k, 0]), float(edges[k, 1])),
            ),
        )
    for k in range(n):
        for i in range(periods):
            coords[offs[2 * n + k] + i] = (verts[k, 0], verts[k, 1], i * a)

    asm = assemble(parts, junctions, coords)
    return asm.net, PrismTube(
        n,
        signs,
        verts,
        dirs,
        float(np.linalg.norm(verts, axis=1).mean()),
        side,
        height,
        asm.seam_atoms,
        tuple(
            tuple(range(offs[k], offs[k] + len(strips[k].block.elements)))
            for k in range(n)
        ),
        tuple(
            tuple(range(offs[n + k], offs[n + k] + len(walls[k].block.elements)))
            for k in range(n)
        ),
        tuple(
            tuple(o + offs[n + k] for o in rim)
            for k in range(n)
            for rim in walls[k].pore_rims
        ),
        asm.pinned,
        coords,
    )

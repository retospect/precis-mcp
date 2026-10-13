"""A closed tube whose wall is N graphene arcs meeting at equal-120 sp2 seams.

The nanoreactor's reactive-tunnel cross-section (docs/backlog/
hexfold-nanoreactor-tube.md, slice 1): N ``_sheet`` wall arcs span between
N seam lines at 360/N spacing along the axis, every seam an equal-120 k3
junction of the two adjacent arcs plus an inward strip -- a rib whose
free edge points at the axis and whose open-edge carbons are the
attachment sites. ``compose_k3`` cannot apply directly -- it composes
three disjoint blocks while every arc here enters two seams -- so the
sheets are assembled first and each seam is minted in place by
:func:`hexfold.join._seam_k3`, the seam-minting half of ``_place_k3``.
With N = 2 this is fin120's inward ``seam_tube`` topology with the
far-side zigzag fuse replaced by a second seam and strip; no far-side
fuse remains.

Seed geometry generalises :func:`hexfold.fin120.wall_radius`: each arc
must span 2*pi/N between two seams' 120-degree positions (the dangling
atoms land sigma from their seam atoms, tilted 60 degrees off the
radial), so with ``y_max`` the sheet height and ``theta0`` the polar
offset of a dangling atom off its seam direction,

    r_wall * sin(theta0) = sqrt(3)/2 * sigma
    r_wall * (2*pi/N - 2*theta0) = y_max

and the seam line sits at ``r_seam = r_wall - 0.5 * sigma`` so that the
dangling position ``r_seam * radial + sigma * dir(phi +- 60 deg)`` has
radial component exactly ``r_wall``.

``rows`` must be even: ``_sheet``'s top zigzag rim is in phase with the
bottom rim's seam lattice only when ``rows - 1`` is odd; an odd count
would seat the top rim's bonds a half period off the seam atoms.

Pores are authored per arc at a lattice cell of that arc's ``_sheet``
before composition: each removes one hexagon's six atoms, leaving six
open-valence rim atoms on an 18-atom boundary walk (a real vacancy, open
to H2 -- not a heptagon), refused when it touches a seam rim, a guard
column or another pore.

Analytic preview topology only: no public grammar, no relaxation here
(the SE adapter relaxes it with every seam registration pinned, strips
tethered to their half-planes, arcs free). Units are Angstrom.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np

from .build import Atom, Net
from .fin import INWARD_TIP_CLEARANCE_A
from .fin120 import _count
from .ids import AtomPath
from .join import Block, _seam_k3, _segment_frame, _ZigzagSegment
from .lattice import Lattice, Site
from .report import Report
from .text import Spec
from .y_junction import _sheet


@dataclass(frozen=True)
class RailTube:
    """The bookkeeping :func:`rail_tube` returns beside the net."""

    rails: int
    #: wall seed radius (the circle the arcs are seeded on)
    radius_A: float
    #: radius of each seam line
    seam_radius_A: float
    #: polar offset of a dangling atom off its seam direction
    theta0: float
    strip_height_A: float
    #: seam atoms per seam, in axial order
    seam_atoms: tuple[tuple[int, ...], ...]
    strip_atoms: tuple[tuple[int, ...], ...]
    arc_atoms: tuple[tuple[int, ...], ...]
    #: the open-valence rim atoms around each pore, composite ordinals
    pore_rims: tuple[tuple[int, ...], ...]
    #: seam atoms and their three neighbours: the pinned registration
    pinned: tuple[int, ...]
    coords: np.ndarray


def _ord(periods: int, j: int, i: int, s: int) -> int:
    """Ordinal of a :func:`_sheet` site ``(j, i, s)`` in its site order
    ``(j, i, s)`` for ``j`` in ``0..rows-1``, ``i`` in ``-1..periods``."""
    return (j * (periods + 2) + (i + 1)) * 2 + s


def _top_segment(periods: int, rows: int) -> _ZigzagSegment:
    """The top zigzag rim of a :func:`_sheet` with even ``rows`` as a
    bondable segment: dangling atoms ``(rows-1, i, 1)`` for ``i`` in
    ``0..periods-1`` (in phase with the bottom rim's seam lattice because
    ``rows - 1`` is odd), walked S-D-...-D-S along the edge with the
    degree-3 ``(rows-1, i, 0)`` sites as S."""
    j = rows - 1
    walk = [_ord(periods, j, -1, 0)]
    for i in range(periods):
        walk.extend((_ord(periods, j, i, 1), _ord(periods, j, i, 0)))
    return _ZigzagSegment(
        "rim",
        tuple(walk),
        tuple(_ord(periods, j, i, 1) for i in range(periods)),
        (walk[0], walk[-1]),
    )


def _cell_ring(periods: int, u: int, v: int) -> tuple[int, ...]:
    """Ordinals of the hexagon :func:`_sheet` mints at cell ``(i, j)`` =
    ``(u, v)`` -- the face patterns of its ring loop."""
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


def rail_radius(rows: int, sigma: float, rails: int) -> tuple[float, float]:
    """``(R_wall, theta0)``: the circle radius the ``rails`` arcs of
    ``rows`` row pairs are seeded on so each arc's sheet height spans the
    2*pi/N angular gap between two seams' 120-degree positions (module
    docstring), and the polar angle of a dangling atom off its seam."""
    y_max = (1.5 * (rows - 1) + 0.5) * sigma
    span = 2.0 * math.pi / rails
    r = y_max / span
    theta0 = 0.0
    for _ in range(60):
        theta0 = math.asin(min(1.0, 0.5 * math.sqrt(3.0) * sigma / r))
        r = y_max / (span - 2.0 * theta0)
    return r, theta0


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


def _pore_cells(
    pores: Sequence[tuple[int, int, int]] | None,
    periods: int,
    rows: int,
    rails: int,
) -> dict[int, list[tuple[int, int]]]:
    """Validate ``(arc, u, v)`` pore cells: ``u`` is the cell index along
    the axis and ``v`` along the rows of that arc's ``_sheet``; a pore
    touching a seam rim, a guard column or another pore is refused."""
    cells: dict[int, list[tuple[int, int]]] = {}
    for arc, u, v in pores or ():
        if type(arc) is not int or not 0 <= arc < rails:
            raise ValueError(
                f"rail tube pore arc must be an integer in [0,{rails}), got {arc!r}"
            )
        if (
            type(u) is not int
            or type(v) is not int
            or not 0 <= u <= periods - 2
            or not 2 <= v <= rows - 4
        ):
            raise ValueError(
                f"rail tube pore cell ({u!r},{v!r}) on arc {arc} touches a "
                f"seam rim or guard column; cells must have u in "
                f"[0,{periods - 2}] and v in [2,{rows - 4}]"
            )
        cells.setdefault(arc, []).append((u, v))
    return cells


def rail_tube(
    periods: int,
    rows: int,
    strip_rows: int,
    rails: int,
    pores: Sequence[tuple[int, int, int]] | None = None,
) -> tuple[Net, RailTube]:
    """``periods`` seam periods along the axis, a wall of ``rails`` arcs
    of ``rows`` row pairs each, and ``rails`` inward strips of
    ``strip_rows`` row pairs, the arcs meeting at ``rails`` equal-120 sp2
    seam lines at 360/N spacing. ``pores`` are ``(arc, u, v)`` cells of
    each arc's ``_sheet``; each removes one hexagon before composition."""
    periods = _count(periods, 2, 60, "periods")
    rows = _count(rows, 4, 40, "wall rows")
    strip_rows = _count(strip_rows, 2, 12, "strip rows")
    rails = _count(rails, 2, 6, "rails")
    if rows % 2:
        raise ValueError(
            "rail tube wall rows must be even so the top zigzag rim lands "
            "in phase with the seam lattice"
        )
    lattice = Lattice()
    sigma = lattice.sigma_A
    a = math.sqrt(3.0) * sigma
    r_wall, theta0 = rail_radius(rows, sigma, rails)
    r_seam = r_wall - 0.5 * sigma
    strip_height = (1.5 * (strip_rows - 1) + 0.5) * sigma
    r_tip = r_seam - sigma - strip_height
    tip_gap = 2.0 * r_tip * math.sin(math.pi / rails)
    if r_tip < INWARD_TIP_CLEARANCE_A or tip_gap < INWARD_TIP_CLEARANCE_A:
        least = rows + (rows % 2)
        while least <= 40:
            r_seam_l = rail_radius(least, sigma, rails)[0] - 0.5 * sigma
            r_tip_l = r_seam_l - sigma - strip_height
            if (
                r_tip_l >= INWARD_TIP_CLEARANCE_A
                and 2.0 * r_tip_l * math.sin(math.pi / rails) >= INWARD_TIP_CLEARANCE_A
            ):
                break
            least += 2
        suggestion = (
            f"the smallest wall that seats it is {least} row pairs"
            if least <= 40
            else "no wall under 40 row pairs seats it"
        )
        if r_tip < INWARD_TIP_CLEARANCE_A:
            raise ValueError(
                f"inward strip of {strip_rows} row pairs ({strip_height:.2f} A) "
                f"does not fit a {rails}-rail tube of {rows} wall row pairs "
                f"(seam radius {r_seam:.2f} A) with "
                f"{INWARD_TIP_CLEARANCE_A:.1f} A left at the axis; {suggestion}"
            )
        raise ValueError(
            f"adjacent strip tips on {rails} rails sit {tip_gap:.2f} A apart, "
            f"under {INWARD_TIP_CLEARANCE_A:.1f} A, on a wall of {rows} row "
            f"pairs; {suggestion}"
        )

    cells = _pore_cells(pores, periods, rows, rails)

    strip_block, strip_rim = _sheet(periods, strip_rows)
    arc_proto, arc_bot = _sheet(periods, rows)
    arc_top = _top_segment(periods, rows)
    if _segment_frame(strip_block, strip_rim) is None:
        raise ValueError("rail tube strip rim failed segment validation")
    if _segment_frame(arc_proto, arc_bot) is None:
        raise ValueError("rail tube arc bottom rim failed segment validation")
    if _segment_frame(arc_proto, arc_top) is None:
        raise ValueError("rail tube arc top rim failed segment validation")

    adjacency: dict[int, set[int]] = {}
    for i, j, _ in arc_proto.bonds:
        adjacency.setdefault(i, set()).add(j)
        adjacency.setdefault(j, set()).add(i)

    # pores: refuse touching rings, then drop the hexagon atoms per arc
    arc_blocks: list[Block] = []
    arc_tops: list[_ZigzagSegment] = []
    arc_bots: list[_ZigzagSegment] = []
    arc_pore_rims: list[list[list[int]]] = []  # per arc, per pore
    for k in range(rails):
        drops: set[int] = set()
        rims: list[list[int]] = []
        seen: list[tuple[int, int, set[int], set[int]]] = []
        for u, v in cells.get(k, []):
            ring = set(_cell_ring(periods, u, v))
            rim = {x for a in ring for x in adjacency.get(a, ())} - ring
            for pu, pv, prev_ring, prev_rim in seen:
                shared = ring & prev_ring
                if shared:
                    raise ValueError(
                        f"rail tube pores ({u},{v}) and ({pu},{pv}) on arc "
                        f"{k} share atoms {sorted(shared)}"
                    )
                if (ring | rim) & (prev_ring | prev_rim) or any(
                    adjacency.get(a, set()) & prev_rim for a in rim
                ):
                    raise ValueError(
                        f"rail tube pores ({u},{v}) and ({pu},{pv}) on arc "
                        f"{k} touch; pores must sit at least one ring apart"
                    )
            seen.append((u, v, ring, rim))
            rims.append(sorted(rim))
            drops |= ring
        if drops:
            block, remap = _without_atoms(arc_proto, drops)
            top = _ZigzagSegment(
                arc_top.name,
                tuple(int(remap[o]) for o in arc_top.atoms),
                tuple(int(remap[o]) for o in arc_top.dangling),
                (
                    int(remap[arc_top.endpoints[0]]),
                    int(remap[arc_top.endpoints[1]]),
                ),
            )
            bot = _ZigzagSegment(
                arc_bot.name,
                tuple(int(remap[o]) for o in arc_bot.atoms),
                tuple(int(remap[o]) for o in arc_bot.dangling),
                (
                    int(remap[arc_bot.endpoints[0]]),
                    int(remap[arc_bot.endpoints[1]]),
                ),
            )
            rims = [[int(remap[o]) for o in rim] for rim in rims]
        else:
            block = arc_proto
            top, bot = arc_top, arc_bot
        if _segment_frame(block, top) is None or _segment_frame(block, bot) is None:
            raise ValueError(f"rail tube arc {k} rim failed segment validation")
        arc_blocks.append(block)
        arc_tops.append(top)
        arc_bots.append(bot)
        arc_pore_rims.append(rims)

    n_s = len(strip_block.elements)
    s_off = [k * n_s for k in range(rails)]
    a_off: list[int] = []
    offset = rails * n_s
    for k in range(rails):
        a_off.append(offset)
        offset += len(arc_blocks[k].elements)
    y_off = [offset + k * periods for k in range(rails)]
    total = offset + rails * periods

    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    for k in range(rails):
        off = s_off[k]
        bonds.extend((i + off, j + off, o) for i, j, o in strip_block.bonds)
        rings.extend(tuple(a_ + off for a_ in ring) for ring in strip_block.rings)
    for k in range(rails):
        off = a_off[k]
        blk = arc_blocks[k]
        bonds.extend((i + off, j + off, o) for i, j, o in blk.bonds)
        rings.extend(tuple(a_ + off for a_ in ring) for ring in blk.rings)
    seam_atoms: list[tuple[int, ...]] = []
    for k in range(rails):
        seam = tuple(range(y_off[k], y_off[k] + periods))
        seam_atoms.append(seam)
        walks = (
            tuple(o + s_off[k] for o in strip_rim.atoms),
            tuple(o + a_off[(k - 1) % rails] for o in arc_tops[(k - 1) % rails].atoms),
            tuple(o + a_off[k] for o in arc_bots[k].atoms),
        )
        dangling = (
            tuple(o + s_off[k] for o in strip_rim.dangling),
            tuple(
                o + a_off[(k - 1) % rails] for o in arc_tops[(k - 1) % rails].dangling
            ),
            tuple(o + a_off[k] for o in arc_bots[k].dangling),
        )
        sb, sr = _seam_k3(walks, dangling, seam)
        bonds.extend(sb)
        rings.extend(sr)

    # the seed: strip k in the half-plane at angle 2*pi*k/N pointing at the
    # axis, its seam atoms on the generatrix at r_seam; arc k wrapped on the
    # r_wall circle from seam k's +60-degree position to seam k+1's -60,
    # both rims' dangling atoms exactly sigma from their seam atoms
    coords = np.zeros((total, 3))
    phi = [2.0 * math.pi * k / rails for k in range(rails)]
    radial = [np.array([math.cos(p), math.sin(p)]) for p in phi]
    for k in range(rails):
        sl = slice(s_off[k], s_off[k] + n_s)
        depth = r_seam - sigma - strip_block.coords[:, 1]
        coords[sl, 0] = depth * radial[k][0]
        coords[sl, 1] = depth * radial[k][1]
        coords[sl, 2] = strip_block.coords[:, 0]
    for k in range(rails):
        blk = arc_blocks[k]
        sl = slice(a_off[k], a_off[k] + len(blk.elements))
        theta = phi[k] + theta0 + blk.coords[:, 1] / r_wall
        coords[sl, 0] = r_wall * np.cos(theta)
        coords[sl, 1] = r_wall * np.sin(theta)
        coords[sl, 2] = blk.coords[:, 0]
        for d in arc_bots[k].dangling:
            dirv = np.array(
                [math.cos(phi[k] + math.pi / 3), math.sin(phi[k] + math.pi / 3)]
            )
            coords[a_off[k] + d, :2] = r_seam * radial[k] + sigma * dirv
        for d in arc_tops[k].dangling:
            kp = (k + 1) % rails
            dirv = np.array(
                [
                    math.cos(phi[kp] - math.pi / 3),
                    math.sin(phi[kp] - math.pi / 3),
                ]
            )
            coords[a_off[k] + d, :2] = r_seam * radial[kp] + sigma * dirv
    for k in range(rails):
        for i, atom in enumerate(seam_atoms[k]):
            coords[atom] = (
                r_seam * radial[k][0],
                r_seam * radial[k][1],
                i * a,
            )

    adjacency_map: dict[int, set[int]] = {}
    for i, j, _ in bonds:
        adjacency_map.setdefault(i, set()).add(j)
        adjacency_map.setdefault(j, set()).add(i)
    pinned: set[int] = set()
    for k in range(rails):
        for s in seam_atoms[k]:
            pinned.add(s)
            pinned.update(adjacency_map.get(s, ()))

    def paths(inst: str, start: int, count: int) -> tuple[Atom, ...]:
        return tuple(
            Atom(AtomPath(inst, Site(o, 0, 0)), start + o, "C", "sp2", inst)
            for o in range(count)
        )

    atoms: list[Atom] = []
    for k in range(rails):
        atoms += paths(f"s{k}", s_off[k], n_s)
    for k in range(rails):
        atoms += paths(f"a{k}", a_off[k], len(arc_blocks[k].elements))
    for k in range(rails):
        atoms += paths(f"y{k}", y_off[k], periods)

    regions: list[tuple[str, tuple[int, ...]]] = []
    for k in range(rails):
        regions.append((f"strip{k}", tuple(range(s_off[k], s_off[k] + n_s))))
    for k in range(rails):
        regions.append(
            (f"arc{k}", tuple(range(a_off[k], a_off[k] + len(arc_blocks[k].elements))))
        )
    for k in range(rails):
        regions.append((f"seam{k}", seam_atoms[k]))
    pore_ordinals: list[tuple[int, ...]] = []
    for k in range(rails):
        for rim_atoms in arc_pore_rims[k]:
            ords = tuple(o + a_off[k] for o in rim_atoms)
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
    )
    return net, RailTube(
        rails,
        r_wall,
        r_seam,
        theta0,
        strip_height,
        tuple(seam_atoms),
        tuple(tuple(range(s_off[k], s_off[k] + n_s)) for k in range(rails)),
        tuple(
            tuple(range(a_off[k], a_off[k] + len(arc_blocks[k].elements)))
            for k in range(rails)
        ),
        tuple(pore_ordinals),
        tuple(sorted(pinned)),
        coords,
    )

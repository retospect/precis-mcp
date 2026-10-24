"""A closed tube whose wall is N graphene arcs meeting at equal-120 sp2 seams.

The nanoreactor's reactive-tunnel cross-section (docs/backlog/
hexfold-nanoreactor-tube.md, slice 1): N ``strip`` wall arcs span between
N seam lines at 360/N spacing along the axis, every seam an equal-120 k3
junction of the two adjacent arcs plus an inward strip -- a rib whose
free edge points at the axis and whose open-edge carbons are the
attachment sites.  A **circle layout over :mod:`hexfold.sheetnet`**: the
arcs and ribs are strips, ``pore`` removes authored vacancy-ring cells,
``junction(k=3)`` mints every seam in place (each arc enters two, so
``compose_k3``'s disjoint-block placement cannot apply), and
``assemble`` is the assembly loop.

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

Pores are authored per arc at a lattice cell of that arc's ``strip``
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
from dataclasses import dataclass

import numpy as np

from .build import Net
from .join import _segment_frame
from .lattice import Lattice
from .sheetnet import (
    INWARD_TIP_CLEARANCE_A,
    Arc,
    HalfPlane,
    Part,
    _count,
    _zig,
    arc_radius,
    assemble,
    junction,
    place,
    pore,
    pore_cells,
    strip,
    strip_height,
)


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


def rail_radius(rows: int, sigma: float, rails: int) -> tuple[float, float]:
    """``(R_wall, theta0)``: the circle radius the ``rails`` arcs of
    ``rows`` row pairs are seeded on so each arc's sheet height spans the
    2*pi/N angular gap between two seams' 120-degree positions (module
    docstring), and the polar angle of a dangling atom off its seam — the
    shared fixed point in :func:`hexfold.sheetnet.arc_radius`."""
    return arc_radius(strip_height(rows, sigma), sigma, 2.0 * math.pi / rails)


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
    each arc's ``strip``; each removes one hexagon before composition."""
    periods = _count(periods, 2, 60, "periods")
    rows = _count(rows, 4, 40, "wall rows")
    strip_rows = _count(strip_rows, 2, 12, "strip rows")
    rails = _count(rails, 2, 6, "rails")
    if rows % 2:
        raise ValueError(
            f"odd_rows: rail tube wall rows must be even (got {rows}) so "
            "the top zigzag rim lands in phase with the seam lattice"
        )
    lattice = Lattice()
    sigma = lattice.sigma_A
    a = math.sqrt(3.0) * sigma
    r_wall, theta0 = rail_radius(rows, sigma, rails)
    r_seam = r_wall - 0.5 * sigma
    height = strip_height(strip_rows, sigma)
    r_tip = r_seam - sigma - height
    tip_gap = 2.0 * r_tip * math.sin(math.pi / rails)
    if r_tip < INWARD_TIP_CLEARANCE_A or tip_gap < INWARD_TIP_CLEARANCE_A:
        least = rows + (rows % 2)
        while least <= 40:
            r_seam_l = rail_radius(least, sigma, rails)[0] - 0.5 * sigma
            r_tip_l = r_seam_l - sigma - height
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
                f"inward strip of {strip_rows} row pairs ({height:.2f} A) "
                f"does not fit a {rails}-rail tube of {rows} wall row pairs "
                f"(seam radius {r_seam:.2f} A) with "
                f"{INWARD_TIP_CLEARANCE_A:.1f} A left at the axis; {suggestion}"
            )
        raise ValueError(
            f"adjacent strip tips on {rails} rails sit {tip_gap:.2f} A apart, "
            f"under {INWARD_TIP_CLEARANCE_A:.1f} A, on a wall of {rows} row "
            f"pairs; {suggestion}"
        )

    cells = pore_cells(pores, periods, rows, rails, label="rail tube", what="arc")

    strips = [strip(strip_rows, periods) for _ in range(rails)]
    walls = [
        pore(strip(rows, periods), cells.get(k, ()), label="rail tube", part=f"arc {k}")
        for k in range(rails)
    ]
    if _segment_frame(strips[0].block, _zig(strips[0].rims["rim"])) is None:
        raise ValueError("rail tube strip rim failed segment validation")
    if _segment_frame(walls[0].block, _zig(walls[0].rims["rim"])) is None:
        raise ValueError("rail tube arc bottom rim failed segment validation")
    if _segment_frame(walls[0].block, _zig(walls[0].rims["top"])) is None:
        raise ValueError("rail tube arc top rim failed segment validation")
    for k in range(rails):
        if (
            _segment_frame(walls[k].block, _zig(walls[k].rims["top"])) is None
            or _segment_frame(walls[k].block, _zig(walls[k].rims["rim"])) is None
        ):
            raise ValueError(f"rail tube arc {k} rim failed segment validation")

    parts = (
        [
            Part(f"s{k}", f"strip{k}", strips[k].block, strip=strips[k])
            for k in range(rails)
        ]
        + [
            Part(f"a{k}", f"arc{k}", walls[k].block, strip=walls[k])
            for k in range(rails)
        ]
        + [Part(f"y{k}", f"seam{k}", count=periods) for k in range(rails)]
    )

    phi = [2.0 * math.pi * k / rails for k in range(rails)]
    radial = [np.array([math.cos(p), math.sin(p)]) for p in phi]
    junctions = []
    for k in range(rails):
        seam_xy = (float(r_seam * radial[k][0]), float(r_seam * radial[k][1]))
        junctions.append(
            junction(
                (
                    (k, "rim"),
                    (rails + (k - 1) % rails, "top"),
                    (rails + k, "rim"),
                ),
                3,
                "sp2",
                seam=2 * rails + k,
                seam_xy=seam_xy,
                snap=(
                    (
                        1,
                        (
                            math.cos(phi[k] - math.pi / 3),
                            math.sin(phi[k] - math.pi / 3),
                        ),
                    ),
                    (
                        2,
                        (
                            math.cos(phi[k] + math.pi / 3),
                            math.sin(phi[k] + math.pi / 3),
                        ),
                    ),
                ),
            )
        )

    # the seed: strip k in the half-plane at angle 2*pi*k/N pointing at the
    # axis, its seam atoms on the generatrix at r_seam; arc k wrapped on the
    # r_wall circle from seam k's +60-degree position to seam k+1's -60,
    # both rims' dangling atoms exactly sigma from their seam atoms (the
    # junction snaps)
    offs: list[int] = []
    pos = 0
    for p in parts:
        offs.append(pos)
        pos += len(p.block.elements) if p.block is not None else p.count
    coords = np.zeros((pos, 3))
    for k in range(rails):
        blk = strips[k].block
        coords[offs[k] : offs[k] + len(blk.elements)] = place(
            strips[k],
            HalfPlane((float(radial[k][0]), float(radial[k][1])), r_seam, -1.0),
        )
    for k in range(rails):
        blk = walls[k].block
        coords[offs[rails + k] : offs[rails + k] + len(blk.elements)] = place(
            walls[k], Arc(r_wall, phi[k], theta0)
        )
    for k in range(rails):
        for i in range(periods):
            coords[offs[2 * rails + k] + i] = (
                r_seam * radial[k][0],
                r_seam * radial[k][1],
                i * a,
            )

    asm = assemble(parts, junctions, coords)
    return asm.net, RailTube(
        rails,
        r_wall,
        r_seam,
        theta0,
        height,
        asm.seam_atoms,
        tuple(
            tuple(range(offs[k], offs[k] + len(strips[k].block.elements)))
            for k in range(rails)
        ),
        tuple(
            tuple(
                range(offs[rails + k], offs[rails + k] + len(walls[k].block.elements))
            )
            for k in range(rails)
        ),
        tuple(
            tuple(o + offs[rails + k] for o in rim)
            for k in range(rails)
            for rim in walls[k].pore_rims
        ),
        asm.pinned,
        coords,
    )

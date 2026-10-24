"""The channel junction: N wall arcs on an open channel, each corner a k3
seam whose third sheet runs outward (or inward) as a strip.

:mod:`hexfold.railtube`'s construction with the strip direction and the
corner-seam hybridisation opened up — and like it, a **circle layout over
:mod:`hexfold.sheetnet`**: the arcs and strips are ``strip``s, each corner
a ``junction(k=3)`` minted in place by ``assemble``.  The distinction
matters: a free GFN2-xTB relaxation of equal-120 sp2 corner seams
dimerises them into the ``k3-sp3-z`` chain (the 2026-10-14 Mac campaign's
``b-r4`` channel converged to the pre-paired ``b-sp3`` structure within
0.1 eV on 354 carbons), so ``paired=True`` — the only row the public
adapter offers — authors the dimer chain directly
(``flavour="sp3-z"``).

Seam registration follows the strip side.  An inward strip keeps the
rail-tube placement: seam atoms half a sigma inside the wall circle, the
arc rims' dangling atoms approaching at +-60 degrees off the seam radial.
An outward strip needs the seam half a sigma *outside* the wall with the
rims approaching at +-120 degrees — the only registration keeping the
equal-120 motif; placed the rail-tube way the outward junction collapses
to 60/60/120.

Analytic preview topology only: no public grammar, no relaxation here.
Units are Angstrom.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .build import Net
from .lattice import Lattice
from .railtube import rail_radius
from .sheetnet import (
    DIMER_A,
    INWARD_TIP_CLEARANCE_A,
    Arc,
    HalfPlane,
    Part,
    _count,
    assemble,
    junction,
    place,
    strip,
    strip_height,
)

SIDES = ("in", "out")


@dataclass(frozen=True)
class ChannelJunction:
    """The bookkeeping :func:`channel_junction` returns beside the net."""

    rails: int
    radius_A: float
    theta0: float
    strip_height_A: float
    #: per-seam radius off the channel axis (inside or outside the wall
    #: circle, following the strip's side)
    seam_radius_A: tuple[float, ...]
    #: seam atoms per seam, in axial order
    seam_atoms: tuple[tuple[int, ...], ...]
    strip_atoms: tuple[tuple[int, ...], ...]
    arc_atoms: tuple[tuple[int, ...], ...]
    #: seam atoms and their neighbours: the pinned registration
    pinned: tuple[int, ...]
    coords: np.ndarray


def channel_junction(
    periods: int,
    rows: int,
    strip_rows: int,
    sides: tuple[str, ...],
    *,
    paired: bool = True,
) -> tuple[Net, ChannelJunction]:
    """``periods`` seam periods along the axis, ``len(sides)`` arcs of
    ``rows`` row pairs each on the wall, and one strip of ``strip_rows``
    row pairs per seam pointing by ``sides`` ('in' / 'out').  ``paired``
    authors the k3-sp3-z dimer chain on every corner seam (even period
    counts); False authors the equal-120 sp2 registration — preview only,
    free relaxation dimerises it (module docstring)."""
    periods = _count(periods, 2, 60, "periods")
    rows = _count(rows, 4, 40, "wall rows")
    strip_rows = _count(strip_rows, 2, 12, "strip rows")
    if rows % 2:
        raise ValueError(
            "channel wall rows must be even so the top zigzag rim lands "
            "in phase with the seam lattice"
        )
    if any(s not in SIDES for s in sides) or len(sides) not in (2, 3, 4, 5, 6):
        raise ValueError("channel junction takes 2-6 strip sides, each 'in' or 'out'")
    if paired and periods % 2:
        raise ValueError(
            "paired channel seams carry the dimer chain; needs an even period count"
        )
    rails = len(sides)
    sigma = Lattice().sigma_A
    a = math.sqrt(3.0) * sigma
    r_wall, theta0 = rail_radius(rows, sigma, rails)
    height = strip_height(strip_rows, sigma)
    # inward strips keep rail_tube's registration; outward strips seat
    # the seam half a sigma outside the wall
    r_seam = tuple(
        r_wall - 0.5 * sigma if s == "in" else r_wall + 0.5 * sigma for s in sides
    )
    # an inward strip's free edge must clear the axis and its neighbours'
    r_tips = [r_seam[k] - sigma - height for k in range(rails)]
    for k, side in enumerate(sides):
        if side != "in":
            continue
        gap = math.inf
        for m in range(1, rails):
            if sides[(k + m) % rails] == "in":
                gap = 2.0 * r_tips[k] * math.sin(math.pi * m / rails)
                break
        if r_tips[k] < INWARD_TIP_CLEARANCE_A or gap < INWARD_TIP_CLEARANCE_A:
            raise ValueError(
                f"inward strip {k} of {strip_rows} row pairs "
                f"({height:.2f} A) does not fit a {rails}-rail "
                f"channel of {rows} wall row pairs (seam radius "
                f"{r_seam[k]:.2f} A, tip at {r_tips[k]:.2f} A from the "
                f"axis) with {INWARD_TIP_CLEARANCE_A:.1f} A clearance"
            )

    st = strip(strip_rows, periods)
    arc = strip(rows, periods)
    n_s, n_a = len(st.block.elements), len(arc.block.elements)
    hyb = "sp3" if paired else "sp2"

    parts = (
        [Part(f"s{k}", f"strip{k}", st.block, strip=st) for k in range(rails)]
        + [Part(f"a{k}", f"arc{k}", arc.block, strip=arc) for k in range(rails)]
        + [Part(f"y{k}", f"seam{k}", count=periods, hyb=hyb) for k in range(rails)]
    )

    phi = [2.0 * math.pi * k / rails for k in range(rails)]
    radial = [np.array([math.cos(p), math.sin(p)]) for p in phi]
    junctions = []
    for k in range(rails):
        half = math.pi / 3 if sides[k] == "in" else 2.0 * math.pi / 3
        junctions.append(
            junction(
                (
                    (k, "rim"),
                    (rails + (k - 1) % rails, "top"),
                    (rails + k, "rim"),
                ),
                3,
                "sp3-z" if paired else "sp2",
                seam=2 * rails + k,
                seam_xy=(
                    float(r_seam[k] * radial[k][0]),
                    float(r_seam[k] * radial[k][1]),
                ),
                snap=(
                    (1, (math.cos(phi[k] - half), math.sin(phi[k] - half))),
                    (2, (math.cos(phi[k] + half), math.sin(phi[k] + half))),
                ),
            )
        )

    # the seed: strip k in the half-plane at angle 2*pi*k/N on its side,
    # arc k wrapped on the r_wall circle between the two adjacent seam
    # registrations, seam atoms on the corner radial (offset half a dimer
    # along the axis when paired); the junction snaps seat both arc rims'
    # dangling atoms sigma off their seam atoms
    offs: list[int] = []
    pos = 0
    for p in parts:
        offs.append(pos)
        pos += len(p.block.elements) if p.block is not None else p.count
    coords = np.zeros((pos, 3))
    for k in range(rails):
        coords[offs[k] : offs[k] + n_s] = place(
            st,
            HalfPlane(
                (float(radial[k][0]), float(radial[k][1])),
                r_seam[k],
                -1.0 if sides[k] == "in" else 1.0,
            ),
        )
    for k in range(rails):
        coords[offs[rails + k] : offs[rails + k] + n_a] = place(
            arc, Arc(r_wall, phi[k], theta0)
        )
    for k in range(rails):
        hdx = (a - DIMER_A) / 2 if paired else 0.0
        for i in range(periods):
            coords[offs[2 * rails + k] + i] = (
                r_seam[k] * radial[k][0],
                r_seam[k] * radial[k][1],
                i * a + (hdx if i % 2 == 0 else -hdx),
            )

    asm = assemble(parts, junctions, coords)
    return asm.net, ChannelJunction(
        rails,
        r_wall,
        theta0,
        height,
        r_seam,
        asm.seam_atoms,
        tuple(tuple(range(offs[k], offs[k] + n_s)) for k in range(rails)),
        tuple(
            tuple(range(offs[rails + k], offs[rails + k] + n_a)) for k in range(rails)
        ),
        asm.pinned,
        coords,
    )

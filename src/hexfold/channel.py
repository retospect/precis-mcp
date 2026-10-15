"""The channel junction: N wall arcs on an open channel, each corner a k3
seam whose third sheet runs outward (or inward) as a strip.

:mod:`hexfold.railtube`'s construction with the strip direction and the
corner-seam hybridisation opened up.  The distinction matters: a free
GFN2-xTB relaxation of equal-120 sp2 corner seams dimerises them into the
``k3-sp3-z`` chain (the 2026-10-14 Mac campaign's ``b-r4`` channel
converged to the pre-paired ``b-sp3`` structure within 0.1 eV on 354
carbons), so ``paired=True`` — the only row the public adapter offers —
authors the dimer chain directly.

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

from .build import Atom, Net
from .fin import INWARD_TIP_CLEARANCE_A
from .fin120 import _count
from .ids import AtomPath
from .join import _seam_k3
from .lattice import Lattice, Site
from .railtube import _top_segment, rail_radius
from .report import Report
from .text import Spec
from .y_junction import _sheet

SIDES = ("in", "out")

#: the GFN2-xTB-relaxed k3-sp3-z dimer length: paired seam atoms sit
#: (a - 1.60)/2 toward each other along the axis (sp3_seam._place_k3z)
DIMER_A = 1.60


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
    lattice = Lattice()
    sigma = lattice.sigma_A
    a = math.sqrt(3.0) * sigma
    r_wall, theta0 = rail_radius(rows, sigma, rails)
    strip_height = (1.5 * (strip_rows - 1) + 0.5) * sigma
    # inward strips keep rail_tube's registration; outward strips seat
    # the seam half a sigma outside the wall
    r_seam = tuple(
        r_wall - 0.5 * sigma if s == "in" else r_wall + 0.5 * sigma for s in sides
    )
    # an inward strip's free edge must clear the axis and its neighbours'
    r_tips = [r_seam[k] - sigma - strip_height for k in range(rails)]
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
                f"({strip_height:.2f} A) does not fit a {rails}-rail "
                f"channel of {rows} wall row pairs (seam radius "
                f"{r_seam[k]:.2f} A, tip at {r_tips[k]:.2f} A from the "
                f"axis) with {INWARD_TIP_CLEARANCE_A:.1f} A clearance"
            )

    strip_block, strip_rim = _sheet(periods, strip_rows)
    arc_block, arc_bot = _sheet(periods, rows)
    arc_top = _top_segment(periods, rows)
    n_s, n_a = len(strip_block.elements), len(arc_block.elements)
    s_off = [k * n_s for k in range(rails)]
    a_off = [rails * n_s + k * n_a for k in range(rails)]
    y_off = [rails * (n_s + n_a) + k * periods for k in range(rails)]
    total = rails * (n_s + n_a + periods)

    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    for k in range(rails):
        off = s_off[k]
        bonds.extend((i + off, j + off, o) for i, j, o in strip_block.bonds)
        rings.extend(tuple(a_ + off for a_ in ring) for ring in strip_block.rings)
    for k in range(rails):
        off = a_off[k]
        bonds.extend((i + off, j + off, o) for i, j, o in arc_block.bonds)
        rings.extend(tuple(a_ + off for a_ in ring) for ring in arc_block.rings)
    seam_atoms: list[tuple[int, ...]] = []
    for k in range(rails):
        seam = tuple(range(y_off[k], y_off[k] + periods))
        seam_atoms.append(seam)
        walks = (
            tuple(o + s_off[k] for o in strip_rim.atoms),
            tuple(o + a_off[(k - 1) % rails] for o in arc_top.atoms),
            tuple(o + a_off[k] for o in arc_bot.atoms),
        )
        dangling = (
            tuple(o + s_off[k] for o in strip_rim.dangling),
            tuple(o + a_off[(k - 1) % rails] for o in arc_top.dangling),
            tuple(o + a_off[k] for o in arc_bot.dangling),
        )
        sb, sr = _seam_k3(walks, dangling, seam)
        bonds.extend((int(i), int(j), int(o)) for i, j, o in sb)
        rings.extend(tuple(int(i) for i in ring) for ring in sr)
        if paired:
            for m in range(periods // 2):
                bonds.append((seam[2 * m], seam[2 * m + 1], 1))

    # the seed: strip k in the half-plane at angle 2*pi*k/N on its side,
    # arc k wrapped on the r_wall circle between the two adjacent seam
    # registrations, seam atoms on the corner radial (offset half a dimer
    # along the axis when paired)
    coords = np.zeros((total, 3))
    phi = [2.0 * math.pi * k / rails for k in range(rails)]
    radial = [np.array([math.cos(p), math.sin(p)]) for p in phi]
    for k in range(rails):
        sl = slice(s_off[k], s_off[k] + n_s)
        if sides[k] == "in":
            depth = r_seam[k] - sigma - strip_block.coords[:, 1]
        else:
            depth = r_seam[k] + sigma + strip_block.coords[:, 1]
        coords[sl, 0] = depth * radial[k][0]
        coords[sl, 1] = depth * radial[k][1]
        coords[sl, 2] = strip_block.coords[:, 0]
    for k in range(rails):
        sl = slice(a_off[k], a_off[k] + n_a)
        theta = phi[k] + theta0 + arc_block.coords[:, 1] / r_wall
        coords[sl, 0] = r_wall * np.cos(theta)
        coords[sl, 1] = r_wall * np.sin(theta)
        coords[sl, 2] = arc_block.coords[:, 0]
    for k in range(rails):
        half = math.pi / 3 if sides[k] == "in" else 2.0 * math.pi / 3
        for d in arc_bot.dangling:
            dirv = np.array([math.cos(phi[k] + half), math.sin(phi[k] + half)])
            coords[a_off[k] + d, :2] = r_seam[k] * radial[k] + sigma * dirv
        for d in arc_top.dangling:
            dirv = np.array([math.cos(phi[k] - half), math.sin(phi[k] - half)])
            coords[a_off[(k - 1) % rails] + d, :2] = (
                r_seam[k] * radial[k] + sigma * dirv
            )
        hdx = (a - DIMER_A) / 2 if paired else 0.0
        for i, atom in enumerate(seam_atoms[k]):
            coords[atom] = (
                r_seam[k] * radial[k][0],
                r_seam[k] * radial[k][1],
                i * a + (hdx if i % 2 == 0 else -hdx),
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

    hyb = "sp3" if paired else "sp2"

    def paths(inst: str, start: int, count: int) -> tuple[Atom, ...]:
        return tuple(
            Atom(AtomPath(inst, Site(o, 0, 0)), start + o, "C", hyb, inst)
            for o in range(count)
        )

    atoms: list[Atom] = []
    for k in range(rails):
        atoms += tuple(
            Atom(AtomPath(f"s{k}", Site(o, 0, 0)), s_off[k] + o, "C", "sp2", f"s{k}")
            for o in range(n_s)
        )
    for k in range(rails):
        atoms += tuple(
            Atom(AtomPath(f"a{k}", Site(o, 0, 0)), a_off[k] + o, "C", "sp2", f"a{k}")
            for o in range(n_a)
        )
    for k in range(rails):
        atoms += paths(f"y{k}", y_off[k], periods)

    regions: list[tuple[str, tuple[int, ...]]] = []
    for k in range(rails):
        regions.append((f"strip{k}", tuple(range(s_off[k], s_off[k] + n_s))))
    for k in range(rails):
        regions.append((f"arc{k}", tuple(range(a_off[k], a_off[k] + n_a))))
    for k in range(rails):
        regions.append((f"seam{k}", seam_atoms[k]))

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
    return net, ChannelJunction(
        rails,
        r_wall,
        theta0,
        strip_height,
        r_seam,
        tuple(seam_atoms),
        tuple(tuple(range(s_off[k], s_off[k] + n_s)) for k in range(rails)),
        tuple(tuple(range(a_off[k], a_off[k] + n_a)) for k in range(rails)),
        tuple(sorted(pinned)),
        coords,
    )

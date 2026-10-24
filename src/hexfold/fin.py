"""A finite graphene fin grafted along one axial zigzag chain of an armchair
tube: the sp3 ``bond`` attachment of SPEC 11.1, built privately.

An ``(n, n)`` tube carries straight zigzag chains along its axis; on one of
them every second atom lies exactly on a generatrix, one per lattice period
``a``. Each of those wall atoms takes a fourth, radial C-C bond to the
dangling atom of a zigzag strip rim (the same ``a`` period), so the strip
stands perpendicular to the wall as a single layer, outward or inward. The
grafted wall atoms are degree four and recorded ``sp3`` (derived, as for any
``bond`` host); consecutive grafts close one six-cycle each, with two sp3
vertices. This is not the unequal-dihedral k3 sp2 seam the catalogue
refuses (a 180/90/90 sp2 atom is T-shaped): the wall keeps its own three
bonds and the strip is a graft.

Units are Angstrom; no relaxation occurs here. The seed is analytic: the
tube's cylinder seed and the strip placed in the half-plane through the
graft line, every strip and graft bond exactly sigma.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from .build import Atom, Net, build
from .ids import AtomPath
from .lattice import Site
from .report import Report
from .sheetnet import INWARD_TIP_CLEARANCE_A, _count, strip
from .text import Spec


@dataclass(frozen=True)
class _Strip:
    """A finite honeycomb strip, ``rows`` row pairs high, whose bottom
    zigzag edge has exactly ``grafts`` dangling atoms at ``x = i a``,
    ``y = 0``; the guard columns closing both ends start one half row up,
    so no ungrafted atom sits on the graft line."""

    coords: np.ndarray
    bonds: tuple[tuple[int, int, int], ...]
    rings: tuple[tuple[int, ...], ...]
    #: dangling bottom-edge ordinals, i = 0 .. grafts - 1
    dangling: tuple[int, ...]
    #: the bottom-edge atom between dangling i and i + 1
    between: tuple[int, ...]


def _strip(grafts: int, rows: int, sigma: float) -> _Strip:
    # a ``sheetnet`` zigzag strip minus the two guard-column atoms on the
    # graft line and whatever that leaves hanging by one bond (the
    # lattice parity makes the two ends differ)
    st = strip(rows, grafts, drop=((0, -1, 0), (0, grafts, 0)), sigma=sigma)
    return _Strip(
        st.block.coords,
        st.block.bonds,
        st.block.rings,
        tuple(st.sites[(0, i, 0)] for i in range(grafts)),
        tuple(st.sites[(0, i, 1)] for i in range(grafts - 1)),
    )


SIDES = ("out", "in")


@dataclass(frozen=True)
class FinGraft:
    """The graft bookkeeping :func:`fin_tube` returns beside the net."""

    radius_A: float
    side: str
    #: grafted wall ordinals (sp3), in axial order; one per period
    wall_atoms: tuple[int, ...]
    #: the strip rim atom bonded to ``wall_atoms[i]``
    rim_atoms: tuple[int, ...]
    #: the six-cycles closed by consecutive grafts
    graft_rings: tuple[tuple[int, ...], ...]
    tube_atoms: tuple[int, ...]
    fin_atoms: tuple[int, ...]
    coords: np.ndarray
    #: the strip's height above the graft line, (1.5 (rows - 1) + 0.5) sigma
    fin_height_A: float


def fin_height(rows: int, sigma: float) -> float:
    """Strip height above the graft line for ``rows`` honeycomb row pairs."""
    return (1.5 * (rows - 1) + 0.5) * sigma


def smallest_inward_tube(rows: int, sigma: float, a: float) -> int:
    """The smallest ``n`` whose ``(n, n)`` radius seats an inward fin of
    ``rows`` row pairs with :data:`INWARD_TIP_CLEARANCE_A` left at the axis."""
    need = INWARD_TIP_CLEARANCE_A + sigma + fin_height(rows, sigma)
    return math.ceil(need * 2.0 * math.pi / (a * math.sqrt(3.0)))


def fin_tube(n: int, periods: int, rows: int, side: str) -> tuple[Net, FinGraft]:
    """An ``(n, n)`` tube of ``periods`` axial periods with a ``rows``-row-pair
    strip grafted along one axial zigzag chain, standing ``side`` ("out" or
    "in") of the wall. Counts are checked before any graph is built; an
    inward fin that would reach the axis is refused naming the smallest
    tube that seats it."""
    n = _count(n, 4, 40, "tube n", "fin")
    periods = _count(periods, 2, 60, "tube periods", "fin")
    rows = _count(rows, 2, 12, "rows", "fin")
    if side not in SIDES:
        raise ValueError(f"fin side must be one of {SIDES}, got {side!r}")
    tube = build(f"hexfold 0.2\nt: tube({n},{n}, len={periods})\n")
    if tube.seed3 is None:
        raise ValueError("tube seed unavailable")
    sigma = tube.lattice.sigma_A
    a = tube.lattice.a
    tpos = np.asarray(tube.seed3, dtype=float)
    rho = np.hypot(tpos[:, 0], tpos[:, 1])
    radius = float(rho.mean())
    if not np.allclose(rho, radius, atol=1e-6):
        raise ValueError("tube seed is not a cylinder about z")
    height = fin_height(rows, sigma)
    if side == "in" and radius - sigma - height < INWARD_TIP_CLEARANCE_A:
        least = smallest_inward_tube(rows, sigma, a)
        raise ValueError(
            f"inward fin of {rows} row pairs ({height:.2f} A) does not fit a "
            f"({n},{n}) tube of radius {radius:.2f} A with "
            f"{INWARD_TIP_CLEARANCE_A:.1f} A left at the axis; the smallest "
            f"armchair tube that seats it is ({least},{least})"
        )
    # the graft line: the wall atoms exactly on the generatrix at angle 0,
    # one per period (the armchair tube's axial zigzag chain alternates
    # between that line and a parallel one a half period along)
    t_adj: dict[int, set[int]] = {}
    for i, j, _ in tube.bonds:
        t_adj.setdefault(i, set()).add(j)
        t_adj.setdefault(j, set()).add(i)
    angle = np.arctan2(tpos[:, 1], tpos[:, 0])
    line = sorted(
        (int(i) for i in np.flatnonzero(np.abs(angle) < 1e-7)),
        key=lambda i: float(tpos[i, 2]),
    )
    if len(line) != periods:
        raise ValueError(
            f"axial graft line has {len(line)} atoms for {periods} periods"
        )
    # only a wall atom with its three lattice bonds takes the graft as a
    # fourth: a line atom on an open end rim has two, and bonding it would
    # make a trivalent rim atom, not an sp3 host -- so the end rims stay
    # ungrafted and the strip spans the interior periods
    line = [i for i in line if len(t_adj[i]) == 3]
    grafts = len(line)
    if grafts < 2:
        raise ValueError(
            f"a ({n},{n}) tube of {periods} periods has {grafts} interior graft "
            "sites on the axial line; at least 2 are needed"
        )
    z0 = float(tpos[line[0], 2])
    if not np.allclose(tpos[line, 2], z0 + a * np.arange(grafts), atol=1e-6):
        raise ValueError("axial graft line is not one atom per period")

    st = _strip(grafts, rows, sigma)
    T = len(tube.atoms)
    sign = 1.0 if side == "out" else -1.0
    fpos = np.empty_like(st.coords)
    fpos[:, 0] = radius + sign * (sigma + st.coords[:, 1])
    fpos[:, 1] = 0.0
    fpos[:, 2] = z0 + st.coords[:, 0]

    wall = tuple(line)
    rim_atoms = tuple(T + r for r in st.dangling)
    graft_bonds = tuple((w, r, 1) for w, r in zip(wall, rim_atoms))
    graft_rings: list[tuple[int, ...]] = []
    for i in range(grafts - 1):
        between = t_adj[wall[i]] & t_adj[wall[i + 1]]
        if len(between) != 1:
            raise ValueError("graft line atoms do not share one chain neighbour")
        graft_rings.append(
            (
                rim_atoms[i],
                T + st.between[i],
                rim_atoms[i + 1],
                wall[i + 1],
                next(iter(between)),
                wall[i],
            )
        )
    wall_set = set(wall)
    atoms = tuple(
        replace(atom, hyb="sp3") if atom.ord in wall_set else atom
        for atom in tube.atoms
    ) + tuple(
        Atom(AtomPath("f", Site(k, 0, 0)), T + k, "C", "sp2", "f")
        for k in range(len(st.coords))
    )
    bonds = (
        tuple(tube.bonds)
        + tuple((T + i, T + j, o) for i, j, o in st.bonds)
        + graft_bonds
    )
    rings = (
        tuple(tube.rings)
        + tuple(tuple(T + v for v in ring) for ring in st.rings)
        + tuple(graft_rings)
    )
    coords = np.vstack([tpos, fpos])
    tube_atoms = tuple(range(T))
    fin_atoms = tuple(range(T, T + len(st.coords)))
    net = Net(
        atoms=atoms,
        bonds=bonds,
        rings=rings,
        ports=(),
        regions=(("tube", tube_atoms), ("fin", fin_atoms)),
        report=Report(),
        spec=Spec(),
        lattice=tube.lattice,
        seed3=tuple((float(r[0]), float(r[1]), float(r[2])) for r in coords),
        seed_kind="mixed",
        attach=tuple((w, r) for w, r, _ in graft_bonds),
    )
    return net, FinGraft(
        radius,
        side,
        wall,
        rim_atoms,
        tuple(graft_rings),
        tube_atoms,
        fin_atoms,
        coords,
        height,
    )

"""A tube whose wall meets a lengthwise strip in an equal-120 sp2 k3 seam.

The seam of :func:`hexfold.join.compose_k3` run along a tube axis: the
strip is one of the three half-sheets, the two halves of the tube wall are
the other two, and they close on the far side by a zigzag fuse in direct
register (two zigzag rims, one bond per atom, a seamless honeycomb) — a
``junction(k=2)``. Each wall half leaves the seam at 120 degrees from the
strip, so the cross section closes with a cusp at the seam -- a teardrop
for an outward strip, a notch (the heart shape) for an inward one --
instead of a circle. The zigzag seam along the axis forces zigzag chains
along the axis, so the wall is the armchair family: two halves of
``rows`` row pairs each close to about an ``(rows, rows)`` tube plus the
seam's own period.

A **circle layout over :mod:`hexfold.sheetnet`**: the strip sits in a
``HalfPlane`` (``nested`` association — the seed's own float order), each
wall half wraps on an ``Arc`` (the far one mirrored, ``sign=-1``), the k3
seam mints in place with both wall rims snapped to their 120-degree
positions, and the far-side fuse is the generalised k2 junction.

This is analytic preview topology on the Y's private segments: no public
grammar, no relaxation here (the SE adapter relaxes it with the seam
registration pinned and the wall free). Units are Angstrom.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import numpy as np

from .build import Net
from .fin import SIDES
from .lattice import Lattice
from .sheetnet import (
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


@dataclass(frozen=True)
class SeamTube:
    """The bookkeeping :func:`seam_tube` returns beside the net."""

    side: str
    #: wall seed radius (the circle the wall halves are seeded on)
    radius_A: float
    #: radius of the seam line
    seam_radius_A: float
    seam_atoms: tuple[int, ...]
    #: the far-side fuse, (wall A atom, wall B atom) per period column
    fuse_pairs: tuple[tuple[int, int], ...]
    fuse_rings: tuple[tuple[int, ...], ...]
    strip_atoms: tuple[int, ...]
    wall_a_atoms: tuple[int, ...]
    wall_b_atoms: tuple[int, ...]
    #: seam atoms and their three neighbours: the pinned registration
    pinned: tuple[int, ...]
    coords: np.ndarray
    strip_height_A: float


def wall_radius(rows: int, sigma: float) -> tuple[float, float]:
    """``(R_wall, theta0)``: the circle radius the two wall halves of
    ``rows`` row pairs are seeded on so that their first rows sit at the
    seam's 120-degree positions and their top rims meet across one bond on
    the far side, and the angle of that first row off the seam."""
    y_max = (1.5 * (rows - 1) + 0.5) * sigma
    r = (y_max + 0.5 * sigma) / math.pi
    for _ in range(20):
        theta0 = math.asin(min(1.0, 0.5 * math.sqrt(3.0) * sigma / r))
        r = (y_max + 0.5 * sigma) / (math.pi - theta0)
    return r, theta0


def seam_tube(
    periods: int, rows: int, strip_rows: int, side: str
) -> tuple[Net, SeamTube]:
    """``periods`` seam periods along the axis, two wall halves of ``rows``
    row pairs and a strip of ``strip_rows`` row pairs standing ``side``
    ("out" or "in") of the wall, all three on one equal-120 seam."""
    periods = _count(periods, 2, 60, "periods", "seam tube")
    rows = _count(rows, 4, 40, "wall rows", "seam tube")
    strip_rows = _count(strip_rows, 2, 12, "strip rows", "seam tube")
    if side not in SIDES:
        raise ValueError(f"seam tube side must be one of {SIDES}, got {side!r}")
    lattice = Lattice()
    sigma = lattice.sigma_A
    a = math.sqrt(3.0) * sigma
    r_wall, theta0 = wall_radius(rows, sigma)
    sign = 1.0 if side == "out" else -1.0
    r_seam = r_wall + sign * 0.5 * sigma
    height = strip_height(strip_rows, sigma)
    if side == "in" and r_seam - sigma - height < INWARD_TIP_CLEARANCE_A:
        need = INWARD_TIP_CLEARANCE_A + sigma + height
        least = rows
        while wall_radius(least, sigma)[0] - 0.5 * sigma < need:
            least += 1
        raise ValueError(
            f"inward strip of {strip_rows} row pairs ({height:.2f} A) does "
            f"not fit a seam tube of {rows} wall row pairs (seam radius "
            f"{r_seam:.2f} A) with {INWARD_TIP_CLEARANCE_A:.1f} A left at the "
            f"axis; the smallest wall that seats it is {least} row pairs"
        )
    st = strip(strip_rows, periods)
    wall = strip(rows, periods)
    n_s, n_w = len(st.block.elements), len(wall.block.elements)
    off_a, off_b = n_s, n_s + n_w

    parts = [
        Part("f", "fin", st.block, strip=st),
        Part("wa", "wall_a", wall.block, strip=wall),
        Part("wb", "wall_b", wall.block, strip=wall),
        Part("y", "seam", count=periods),
    ]
    junctions = [
        junction(
            ((0, "rim"), (1, "rim"), (2, "rim")),
            3,
            "sp2",
            seam=3,
            seam_xy=(r_seam, 0.0),
            snap=tuple(
                (idx, (-sign * 0.5, s * 0.5 * math.sqrt(3.0)))
                for idx, s in ((1, 1.0), (2, -1.0))
            ),
        ),
        # the far-side fuse: wall A's top zigzag row (guard stubs
        # included) onto wall B's, column for column; seam is inert for
        # k2 — it mints no atom column
        junction(((1, "top_full"), (2, "top_full")), 2, "fuse", seam=3),
    ]

    # the seed: strip in the half-plane y = 0, seam on the generatrix at
    # angle 0, each wall half wrapped on the circle from its 120-degree
    # first row round to the far side; the junction snaps seat both wall
    # rims' dangling atoms at the 120-degree positions sigma off the seam
    coords = np.zeros((off_b + n_w + periods, 3))
    z0 = 0.0
    coords[:n_s] = place(st, HalfPlane((1.0, 0.0), r_seam, sign, nested=True))
    coords[off_a : off_a + n_w] = place(wall, Arc(r_wall, 0.0, theta0))
    coords[off_b : off_b + n_w] = place(wall, Arc(r_wall, 0.0, theta0, sign=-1.0))
    for i in range(periods):
        coords[off_b + n_w + i] = (r_seam, 0.0, z0 + i * a)

    asm = assemble(parts, junctions, coords)

    # the composite's fuse bookkeeping: the same pairs and minted rings
    # the k2 junction wrote, re-derived from the top rims
    top = wall.rims["top_full"].atoms
    w_adj: dict[int, set[int]] = {}
    for i, j, _ in wall.block.bonds:
        w_adj.setdefault(i, set()).add(j)
        w_adj.setdefault(j, set()).add(i)
    fuse_pairs = tuple((off_a + t, off_b + t) for t in top)
    fuse_rings: list[tuple[int, ...]] = []
    for t0, t1 in itertools.pairwise(top):
        mid = w_adj[t0] & w_adj[t1]
        if len(mid) != 1:
            continue
        m = next(iter(mid))
        fuse_rings.append(
            (off_a + t0, off_a + m, off_a + t1, off_b + t1, off_b + m, off_b + t0)
        )

    strip_atoms = tuple(range(n_s))
    wall_a = tuple(range(off_a, off_a + n_w))
    wall_b = tuple(range(off_b, off_b + n_w))
    return asm.net, SeamTube(
        side,
        r_wall,
        r_seam,
        asm.seam_atoms[0],
        fuse_pairs,
        tuple(fuse_rings),
        strip_atoms,
        wall_a,
        wall_b,
        asm.pinned,
        coords,
        height,
    )

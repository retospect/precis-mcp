"""A tube whose wall meets a lengthwise strip in an equal-120 sp2 k3 seam.

The seam of :func:`hexfold.join.compose_k3` run along a tube axis: the
strip is one of the three half-sheets, the two halves of the tube wall are
the other two, and they close on the far side by a zigzag fuse in direct
register (two zigzag rims, one bond per atom, a seamless honeycomb). Each
wall half leaves the seam at 120 degrees from the strip, so the cross
section closes with a cusp at the seam -- a teardrop for an outward strip,
a notch (the heart shape) for an inward one -- instead of a circle. The
zigzag seam along the axis forces zigzag chains along the axis, so the
wall is the armchair family: two halves of ``rows`` row pairs each close
to about an ``(rows, rows)`` tube plus the seam's own period.

This is analytic preview topology on the Y's private segments: no public
grammar, no relaxation here (the SE adapter relaxes it with the seam
registration pinned and the wall free). Units are Angstrom.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

import numpy as np

from .build import Atom, Net
from .fin import INWARD_TIP_CLEARANCE_A, SIDES
from .ids import AtomPath
from .join import compose_k3
from .lattice import Lattice, Site
from .report import Report
from .text import Spec
from .y_junction import _sheet


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


def _count(value: object, lo: int, hi: int, what: str) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"seam tube {what} must be an integer in [{lo},{hi}]")
    return value


def _top_dangling(periods: int, rows: int) -> list[int]:
    """Ordinals of a :func:`_sheet` block's top zigzag rim, column order
    (``(rows - 1, i, 1)`` for ``i`` in ``-1 .. periods``), from the
    block's site order ``(j, i, s)``."""
    j = rows - 1
    return [((j * (periods + 2)) + (i + 1)) * 2 + 1 for i in range(-1, periods + 1)]


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
    periods = _count(periods, 2, 60, "periods")
    rows = _count(rows, 4, 40, "wall rows")
    strip_rows = _count(strip_rows, 2, 12, "strip rows")
    if side not in SIDES:
        raise ValueError(f"seam tube side must be one of {SIDES}, got {side!r}")
    lattice = Lattice()
    sigma = lattice.sigma_A
    a = math.sqrt(3.0) * sigma
    r_wall, theta0 = wall_radius(rows, sigma)
    sign = 1.0 if side == "out" else -1.0
    r_seam = r_wall + sign * 0.5 * sigma
    strip_height = (1.5 * (strip_rows - 1) + 0.5) * sigma
    if side == "in" and r_seam - sigma - strip_height < INWARD_TIP_CLEARANCE_A:
        need = INWARD_TIP_CLEARANCE_A + sigma + strip_height
        least = rows
        while wall_radius(least, sigma)[0] - 0.5 * sigma < need:
            least += 1
        raise ValueError(
            f"inward strip of {strip_rows} row pairs ({strip_height:.2f} A) does "
            f"not fit a seam tube of {rows} wall row pairs (seam radius "
            f"{r_seam:.2f} A) with {INWARD_TIP_CLEARANCE_A:.1f} A left at the "
            f"axis; the smallest wall that seats it is {least} row pairs"
        )
    strip, s_rim = _sheet(periods, strip_rows)
    wall, w_rim = _sheet(periods, rows)
    result = compose_k3(
        (strip, wall, wall),
        (s_rim, w_rim, w_rim),
        seam_type="k3-sp2-120-z",
        dihedrals_deg=(120, 120, 120),
    )
    if result.findings:
        raise ValueError(Report(tuple(result.findings)).render())
    n_s, n_w = len(strip.elements), len(wall.elements)
    off_a, off_b = n_s, n_s + n_w
    seam = result.seam_atoms
    assert len(seam) == periods and seam[0] == n_s + 2 * n_w

    # the far-side fuse: wall A's top rim onto wall B's, column for column
    top = _top_dangling(periods, rows)
    w_adj: dict[int, set[int]] = {}
    for i, j, _ in wall.bonds:
        w_adj.setdefault(i, set()).add(j)
        w_adj.setdefault(j, set()).add(i)
    y_top = (1.5 * (rows - 1) + 0.5) * sigma
    # every top-row atom takes the fuse bond, the guard column's own top
    # atom (hanging by one bond, the Y's guard stub) included: it meets
    # its mirror image at bond distance on the far side either way
    for t in top:
        assert abs(wall.coords[t, 1] - y_top) < 1e-9
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

    # the seed: strip in the half-plane y = 0, seam on the generatrix at
    # angle 0, each wall half wrapped on the circle from its 120-degree
    # first row round to the far side
    coords = np.zeros((len(result.elements), 3))
    z0 = 0.0
    coords[:n_s, 0] = r_seam + sign * (sigma + strip.coords[:, 1])
    coords[:n_s, 2] = z0 + strip.coords[:, 0]
    for off, s in ((off_a, 1.0), (off_b, -1.0)):
        theta = s * (theta0 + wall.coords[:, 1] / r_wall)
        coords[off : off + n_w, 0] = r_wall * np.cos(theta)
        coords[off : off + n_w, 1] = r_wall * np.sin(theta)
        coords[off : off + n_w, 2] = z0 + wall.coords[:, 0]
        # the first row's dangling atoms exactly at the 120-degree
        # positions, sigma from the seam atom
        for d in w_rim.dangling:
            coords[off + d, 0] = r_seam - sign * 0.5 * sigma
            coords[off + d, 1] = s * 0.5 * math.sqrt(3.0) * sigma
    for i, atom in enumerate(seam):
        coords[atom] = (r_seam, 0.0, z0 + i * a)

    pinned = set(seam)
    for i, j, _ in result.bonds:
        if i in pinned or j in pinned:
            pinned.add(i)
            pinned.add(j)

    def paths(inst: str, start: int, count: int) -> tuple[Atom, ...]:
        return tuple(
            Atom(AtomPath(inst, Site(k, 0, 0)), start + k, "C", "sp2", inst)
            for k in range(count)
        )

    atoms = (
        paths("f", 0, n_s)
        + paths("wa", off_a, n_w)
        + paths("wb", off_b, n_w)
        + paths("y", seam[0], periods)
    )
    strip_atoms = tuple(range(n_s))
    wall_a = tuple(range(off_a, off_a + n_w))
    wall_b = tuple(range(off_b, off_b + n_w))
    net = Net(
        atoms=atoms,
        bonds=tuple(result.bonds) + tuple((p, q, 1) for p, q in fuse_pairs),
        rings=tuple(result.rings) + tuple(fuse_rings),
        ports=(),
        regions=(
            ("fin", strip_atoms),
            ("wall_a", wall_a),
            ("wall_b", wall_b),
            ("seam", tuple(seam)),
        ),
        report=Report(),
        spec=Spec(),
        lattice=lattice,
        seed3=tuple((float(r[0]), float(r[1]), float(r[2])) for r in coords),
        seed_kind="mixed",
    )
    return net, SeamTube(
        side,
        r_wall,
        r_seam,
        tuple(seam),
        fuse_pairs,
        tuple(fuse_rings),
        strip_atoms,
        wall_a,
        wall_b,
        tuple(sorted(pinned)),
        coords,
        strip_height,
    )

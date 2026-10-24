"""Finite straight equal-120 Y sheets using join's private zigzag segments.

The two guard columns close segment endpoints without extending public
cyclic Port grammar. This is analytic preview topology, not a T2 rail or
an unequal-dihedral solver. Units are Angstrom; no relaxation occurs here.
"""

from __future__ import annotations

from .build import Atom, Net
from .ids import AtomPath
from .join import Block, _K3Composite, _ZigzagSegment, compose_k3
from .lattice import Lattice, Site
from .report import Report
from .sheetnet import _zig, strip
from .text import Spec


def _sheet(periods: int, row_pairs: int) -> tuple[Block, _ZigzagSegment]:
    """The zigzag sheet and its bottom rim — a :func:`sheetnet.strip`
    layout call (kept name; the caller vocabulary moved)."""
    st = strip(row_pairs, periods)
    return st.block, _zig(st.rims["rim"])


def straight_y(periods: int, row_pairs: int) -> tuple[Net, _K3Composite]:
    """Three identical open sheets; counts describe primitive row pairs.

    Public admission belongs to the SE adapter; this helper also rejects
    invalid counts before allocating the local graph.
    """
    if any(type(n) is not int or not 2 <= n <= 30 for n in (periods, row_pairs)):
        raise ValueError("straight Y counts must be integers in [2,30]")
    block, rim = _sheet(periods, row_pairs)
    result = compose_k3(
        (block, block, block),
        (rim, rim, rim),
        seam_type="k3-sp2-120-z",
        dihedrals_deg=(120, 120, 120),
    )
    if result.findings:
        raise ValueError(Report(tuple(result.findings)).render())
    size = len(block.elements)
    net = Net(
        atoms=tuple(
            Atom(AtomPath("y", Site(i, 0, 0)), i, element, "sp2", "y")
            for i, element in enumerate(result.elements)
        ),
        bonds=result.bonds,
        rings=result.rings,
        ports=(),
        regions=tuple(
            (f"sheet{j}", tuple(range(j * size, (j + 1) * size))) for j in range(3)
        ),
        report=Report(),
        spec=Spec(),
        lattice=Lattice(),
        seed3=tuple(
            (float(row[0]), float(row[1]), float(row[2])) for row in result.coords
        ),
        seed_kind="mixed",
    )
    return net, result

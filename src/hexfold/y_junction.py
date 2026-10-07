"""Finite straight equal-120 Y sheets using join's private zigzag segments.

The two guard columns close segment endpoints without extending public
cyclic Port grammar. This is analytic preview topology, not a T2 rail or
an unequal-dihedral solver. Units are Angstrom; no relaxation occurs here.
"""

from __future__ import annotations

import math

import numpy as np

from .build import Atom, Net
from .ids import AtomPath
from .join import Block, _K3Composite, _ZigzagSegment, compose_k3
from .lattice import Lattice, Site
from .report import Report
from .text import Spec


def _sheet(periods: int, row_pairs: int) -> tuple[Block, _ZigzagSegment]:
    sigma = Lattice().sigma_A
    period = math.sqrt(3) * sigma
    sites = [
        (j, i, s)
        for j in range(row_pairs)
        for i in range(-1, periods + 1)
        for s in (0, 1)
    ]
    ordinal = {site: n for n, site in enumerate(sites)}
    coords = np.asarray(
        [
            ((i + (0.5 if s != j % 2 else 0)) * period, (1.5 * j + 0.5 * s) * sigma, 0)
            for j, i, s in sites
        ],
        dtype=float,
    )
    bonds: list[tuple[int, int, int]] = []
    rings: list[tuple[int, ...]] = []
    for j in range(row_pairs):
        for i in range(-1, periods + 1):
            bonds.append((ordinal[j, i, 0], ordinal[j, i, 1], 1))
            if i < periods:
                bonds.append((ordinal[j, i, 1 - j % 2], ordinal[j, i + 1, j % 2], 1))
            if j + 1 < row_pairs:
                bonds.append((ordinal[j, i, 1], ordinal[j + 1, i, 0], 1))
                if i < periods:
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
                    rings.append(tuple(ordinal[site] for site in face))
    walk = [ordinal[0, -1, 1]]
    for i in range(periods):
        walk.extend((ordinal[0, i, 0], ordinal[0, i, 1]))
    segment = _ZigzagSegment(
        "rim",
        tuple(walk),
        tuple(ordinal[0, i, 0] for i in range(periods)),
        (walk[0], walk[-1]),
    )
    return Block(
        ("C",) * len(sites),
        coords,
        tuple(bonds),
        tuple(rings),
        {},
        sigma,
    ), segment


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

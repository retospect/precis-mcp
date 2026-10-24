"""gr503025: seam-side wedge dedupe and the junction-atom ideal.

A seam face and the face across it traverse the same (vertex, edge
pair); the corner angle is measured once, not once per ring side — which
used to duplicate every seam ``geom.angle.dev`` row. A vertex whose
EVERY wedge is multi-claimed is a seam junction atom: it answers to the
authored equal-120 contract (``ideal_angle_deg(6, hyb)``), not the ideal
a wedge's own ring size would suggest (an 8-cycle's 135°).
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import numpy as np

from hexfold.check import Relaxed, geometry_findings
from hexfold.report import Profile

# A 7-cycle and an 8-cycle sharing the path 0-1-2: at vertex 1 the wedge
# {0, 2} is claimed by both faces (one measurement, not two), and since
# it is vertex 1's only wedge — all multi-claimed — vertex 1 is a seam
# junction atom answering to 120 deg, not the 7- or 8-cycle ideals
# (128.6 / 135 deg, each > 10 deg off a real 120).
_RINGS = ((0, 1, 2, 3, 4, 5, 6), (0, 1, 2, 7, 8, 9, 10, 11))


def _net(coords: np.ndarray) -> Any:
    atoms = tuple(
        SimpleNamespace(ord=i, element="C", hyb="sp2", instance="t", path=None)
        for i in range(len(coords))
    )
    return SimpleNamespace(
        atoms=atoms,
        bonds=((0, 1, 1), (1, 2, 1)),
        rings=_RINGS,
        lattice=SimpleNamespace(sigma_A=1.42, sigma_CH_A=1.09, elements=("C", "C")),
        seed3=None,
    )


def test_a_shared_wedge_is_measured_once_and_junctions_answer_to_120() -> None:
    coords = np.zeros((12, 3))
    # vertex 1 at the apex of a true 120-degree wedge
    coords[0] = (1.42, 0.0, 0.0)
    coords[1] = (0.0, 0.0, 0.0)
    coords[2] = (
        1.42 * math.cos(math.radians(120)),
        1.42 * math.sin(math.radians(120)),
        0.0,
    )
    # everyone else far away and uninvolved
    for i in range(3, 12):
        coords[i] = (i * 5.0, 10.0, 0.0)
    findings = geometry_findings(
        _net(coords), Profile.DEFAULT, relaxed=Relaxed(coords, 0.0, "synthetic")
    )
    # the junction vertex: 120 deg measured against the seam contract, so
    # it does not flag (pre-fix it read 15 deg off the 8-cycle's ideal)
    assert not [f for f in findings if f.code == "geom.angle.dev" and f.where == "1"]
    summary = next(f for f in findings if f.code == "geom.summary")
    # 7 + 8 ring vertices, the shared wedge counted once: not 15
    assert dict(summary.data)["angle_count"] == 14

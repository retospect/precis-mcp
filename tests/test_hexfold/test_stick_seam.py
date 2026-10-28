"""gr347187: k>=3 seam atoms have no pre-seam seed row -- ``stick()`` used
to crash on every net with a seam (``_place_seeds`` computes ``seed3``
from the pre-seam ``net``, but the returned ``Net.atoms`` already carries
the seam atoms).  Covers the ``build.py`` seed-extension fix and the
``stick_info`` length guard that turns the next such gap into a one-line
``ValueError`` instead of a numpy broadcast crash.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import pytest

from hexfold.build import build
from hexfold.cli import main
from hexfold.stick import stick, stick_info

ROOT = Path(__file__).resolve().parents[2] / "hexfold"
_SHEET_PILL_BUMP = (ROOT / "examples" / "sheet_pill_bump.hx").read_text(
    encoding="utf-8"
)
_DOUGHNUT_PATH = ROOT / "examples" / "flanged_doughnut.hx"
_DOUGHNUT = _DOUGHNUT_PATH.read_text(encoding="utf-8")


def test_stick_sheet_pill_bump_shape() -> None:
    net = build(_SHEET_PILL_BUMP, strict=False)
    assert stick(net).shape == (len(net.atoms), 3)


def test_stick_flanged_doughnut_shape() -> None:
    net = build(_DOUGHNUT, strict=False)
    assert stick(net).shape == (len(net.atoms), 3)


def test_seam_atom_bond_lengths_sane() -> None:
    # each of the 24 `outer/s<i>` seam atoms bonds to one atom in each
    # of its 3 rims -- after relaxation every such bond should sit near
    # sigma, well inside 1.5*sigma (bare interpolated-mean seeding, not
    # a bonded ideal, so some spring slack is expected).
    net = build(_DOUGHNUT, strict=False)
    pos = stick(net)
    sigma = net.lattice.sigma_A
    (seam,) = net.seams
    seam_ords = set(seam.atoms)
    nbrs: dict[int, list[int]] = {o: [] for o in seam_ords}
    for i, j, _order in net.bonds:
        if i in seam_ords:
            nbrs[i].append(j)
        if j in seam_ords:
            nbrs[j].append(i)
    assert all(len(ns) == 3 for ns in nbrs.values())
    lengths = [
        float(np.linalg.norm(pos[o] - pos[n])) for o, ns in nbrs.items() for n in ns
    ]
    assert len(lengths) == 24 * 3
    assert max(lengths) < 1.5 * sigma


def test_stick_info_raises_on_truncated_seed3() -> None:
    net = build(_DOUGHNUT, strict=False)
    assert net.seed3 is not None
    truncated = dataclasses.replace(net, seed3=net.seed3[:-1])
    with pytest.raises(ValueError, match="seed3 has .* rows for .* atoms"):
        stick_info(truncated)


def test_cli_xyz_flanged_doughnut(tmp_path: Path) -> None:
    out = tmp_path / "doughnut.xyz"
    assert main(["xyz", str(_DOUGHNUT_PATH), "-o", str(out)]) == 0
    net = build(_DOUGHNUT, strict=False)
    first_line = out.read_text(encoding="utf-8").splitlines()[0].strip()
    assert first_line == str(len(net.atoms))

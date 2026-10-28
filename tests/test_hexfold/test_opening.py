"""Armchair-rim opening solver: cut a sheet hole, stitch a tube rim in.

Slice H1 of the nanoreactor tunnel: small instances only -- a (6,6) tube
end on a modest ``_sheet`` patch covers refusal, count matching, the
defect ring and dangling bookkeeping.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from hexfold.build import build
from hexfold.join import block_from_net
from hexfold.lattice import Lattice
from hexfold.opening import (
    _adjacency,
    armchair_opening,
    rim_loop_end,
    rim_loop_port,
)
from hexfold.railtube import rail_tube
from hexfold.y_junction import _sheet

SIGMA = Lattice().sigma_A


def _placed_tube(n: int, periods: int, sheet: Any) -> tuple:
    """A ``tube(n,n)`` block placed with its 'in' rim dangling atoms one
    sigma below the sheet plane at the sheet's centre."""
    net = build(f"hexfold 0.2\nt: tube({n},{n}, len={periods})\n")
    blk = block_from_net(net, np.asarray(net.seed3))
    rim = rim_loop_port(net, "in")
    pos = np.asarray(blk.coords, dtype=float).copy()
    rc = pos[list(rim.atoms)].mean(axis=0)
    pos[:, 2] = -pos[:, 2] - SIGMA
    pos[:, :2] += np.asarray(sheet.coords).mean(axis=0)[:2] - rc[:2]
    return net, blk, rim, pos


def test_armchair_rim_loop_has_walk_order() -> None:
    net = build("hexfold 0.2\nt: tube(6,6, len=2)\n")
    rim = rim_loop_port(net, "in")
    assert len(rim.atoms) == 12
    assert rim.walk is not None and len(rim.walk) == 24
    assert len(rim.band) > len(rim.atoms)


@pytest.mark.parametrize("nm", [(6, 0), (7, 3), (9, 2)])
def test_non_armchair_rim_refused(nm: tuple[int, int]) -> None:
    net = build(f"hexfold 0.2\nt: tube({nm[0]},{nm[1]}, len=2)\n")
    with pytest.raises(ValueError, match="armchair"):
        rim_loop_port(net, "in")


def test_opening_stitches_all_rim_atoms() -> None:
    sheet, _rim = _sheet(30, 30)
    net, blk, rim, pos = _placed_tube(6, 3, sheet)
    cut, placed, op = armchair_opening(sheet, blk, rim, pos)
    assert len(op.fused) == 12
    assert len(op.faces) == 12
    assert not op.unbonded_sheet and not op.unbonded_rim
    # defect ring: minted faces are real ring sizes
    assert all(4 <= len(f) <= 12 for f in op.faces)
    # every face member bond-connected in the composite
    ns = len(cut.elements)
    adj = _adjacency(
        tuple(
            list(cut.bonds)
            + [(i + ns, j + ns, o) for i, j, o in blk.bonds]
            + [(h, r + ns, 1) for h, r in op.fused]
        ),
        ns + len(blk.elements),
    )
    for f in op.faces:
        ords = [a if s == "s" else a + ns for s, a in f]
        for k in range(len(ords)):
            assert ords[(k + 1) % len(ords)] in adj[ords[k]]


def test_opening_reports_unbonded_when_counts_mismatch() -> None:
    # (4,4) has 8 rim atoms; cuts into a small patch may still seat it,
    # but the unbonded bookkeeping must be consistent
    sheet, _rim = _sheet(30, 30)
    net, blk, rim, pos = _placed_tube(4, 3, sheet)
    cut, placed, op = armchair_opening(sheet, blk, rim, pos)
    fused_r = {r for _, r in op.fused}
    fused_s = {h for h, _ in op.fused}
    assert set(op.unbonded_sheet) == set(op.hole_bondable) - fused_s
    assert set(op.unbonded_rim) == set(rim.atoms) - fused_r
    assert len(op.faces) <= len(op.fused)


def test_rail_end_rim_is_planar_boundary() -> None:
    net, _rt = rail_tube(4, 14, 3, 2)
    pos = np.asarray(net.seed3)
    for end in ("low", "high"):
        rim = rim_loop_end(pos, net.bonds, np.array([0.0, 0.0, 1.0]), end)
        z = pos[list(rim.atoms), 2]
        assert np.ptp(z) < 0.2 * SIGMA
        assert len(rim.atoms) > 10
        assert rim.walk is None

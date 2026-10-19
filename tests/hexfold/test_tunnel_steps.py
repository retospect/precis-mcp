"""hexfold.tunnel.tunnel_steps: sheet | rail | washer | rail | sheet.

The stepped rail tunnel of docs/backlog/hexfold-nanoreactor-tube.md H2:
solver-minted washers (the fuse{reducer} role) between star-ended rail
segments, Junction records named reducer:seg<w>->seg<n>:outer|inner.
"""

import pytest

from hexfold.tunnel import tunnel_steps

SEG_W = (18, 4, 2, 3, None)  # rows, periods, strip_rows, rails, pores
SEG_N = (8, 4, 2, 3, None)


def test_stepped_rail_tunnel_builds() -> None:
    net, tun = tunnel_steps((SEG_W, SEG_N), (44, 44))
    assert len(tun.junctions) == 2
    assert [j.name for j in tun.reducers] == [
        "reducer:seg0->seg1:outer",
        "reducer:seg0->seg1:inner",
    ]
    assert len(tun.segment_atoms) == 2
    assert len(tun.strip_segment) == len(tun.strip_atoms)
    assert len(tun.seam_segment) == len(tun.seam_atoms)
    # every reducer seam fused pairs on both sides, and unclosed seams are
    # reported -- never silently accepted
    for j in tun.reducers:
        assert j.fused
    codes = {f.code for f in net.report.findings}
    assert "opening.defects" in codes


def test_stepped_rail_up_step_registers_wide_side() -> None:
    net, tun = tunnel_steps((SEG_N, SEG_W), (44, 44))
    assert [j.name for j in tun.reducers] == [
        "reducer:seg1->seg0:outer",
        "reducer:seg1->seg0:inner",
    ]


def test_step_throat_refused_by_name() -> None:
    # seg 12's rib tips dip to ~7.7 A but seg 8's wall sits at 8.6 A --
    # the washer annulus would pinch shut at the star notches
    with pytest.raises(ValueError, match="pinch"):
        tunnel_steps(((8, 4, 2, 3, None), (12, 4, 2, 3, None)), (44, 44))


def test_step_rail_count_mismatch_refused() -> None:
    with pytest.raises(ValueError, match="rail count"):
        tunnel_steps((SEG_W, (8, 4, 2, 4, None)), (44, 44))


def test_single_segment_refused() -> None:
    with pytest.raises(ValueError, match="two segments"):
        tunnel_steps((SEG_W,), (44, 44))

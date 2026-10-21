"""hexfold.tunnel.tunnel_steps: sheet | rail | washer | rail | sheet.

The stepped rail tunnel of docs/backlog/hexfold-nanoreactor-tube.md H2:
solver-minted washers (the fuse{reducer} role) between star-ended rail
segments, Junction records named reducer:seg<w>->seg<n>:outer|inner.
"""

from collections import Counter

import pytest

from hexfold.tunnel import tunnel_steps

SEG_W = (18, 4, 2, 3, None)  # rows, periods, strip_rows, rails, pores
SEG_N = (8, 4, 2, 3, None)

#: gr502249: a same-ray near-tie in rim_loop_end's angular order once
#: flipped between macOS Accelerate and Linux OpenBLAS, minting a
#: different twist, cut and defect census per platform.  The fixture
#: below is the gripe's 18 -> 8 washer; the pins are topology (counts,
#: census, seam atom ids), never geometry floats, so the build is
#: pinned deterministic -- any platform that reproduces these ids used
#: the same branch at every near-tie.
_WASHER_RING_CENSUS = {4: 20, 5: 30, 6: 4088, 7: 37, 8: 71, 9: 2}
_WASHER_INNER_ATOMS = (
    4272,
    4273,
    4284,
    4285,
    4296,
    4297,
    4308,
    4309,
    4320,
    4321,
    4332,
    4333,
    4344,
    4345,
    4356,
    4357,
    4368,
    4369,
    4380,
    4381,
    4392,
    4393,
    4404,
    4405,
    4416,
    4417,
    4428,
    4429,
    4440,
    4441,
    4452,
    4453,
    4464,
    4465,
    4476,
    4477,
    4488,
    4489,
    4500,
    4501,
    4512,
    4513,
    4524,
    4525,
    4536,
    4537,
    4548,
    4549,
    4560,
    4561,
    4572,
    4573,
    4584,
    4585,
    4596,
    4597,
    4608,
    4609,
    4620,
    4621,
    4708,
    4709,
    4710,
    4711,
    4712,
    4713,
    4714,
    4733,
    4734,
    4735,
    4736,
    4737,
    4738,
    4739,
    4741,
    4755,
    4756,
    4757,
    4758,
    4759,
    4760,
    4774,
    4776,
    4777,
    4779,
    4792,
    4793,
    4794,
    4808,
    4809,
    4811,
    4824,
    4825,
    4826,
    4838,
    4840,
    4842,
    4855,
    4856,
    4857,
    4858,
    4870,
    4872,
    4873,
    4874,
    4875,
    4876,
    4892,
    4893,
    4894,
    4895,
    4896,
    4897,
    4898,
    4899,
    4917,
    4919,
    4920,
    4921,
    4922,
    4923,
    4924,
)
_WASHER_INNER_FUSED = (
    (4712, 4560),
    (4737, 4573),
    (4739, 4584),
    (4759, 4597),
    (4777, 4608),
    (4794, 4621),
    (4811, 4272),
    (4826, 4285),
    (4842, 4344),
    (4857, 4357),
    (4876, 4368),
    (4897, 4381),
    (4896, 4392),
    (4921, 4405),
    (4919, 4416),
    (4895, 4429),
    (4894, 4296),
    (4874, 4309),
    (4872, 4440),
    (4856, 4453),
    (4840, 4464),
    (4825, 4477),
    (4808, 4488),
    (4793, 4501),
    (4776, 4512),
    (4757, 4525),
    (4758, 4320),
    (4736, 4333),
    (4735, 4536),
    (4710, 4549),
)
_WASHER_INNER_FACE_CENSUS = {4: 4, 5: 4, 6: 2, 7: 4, 8: 8, 9: 2}
_WASHER_OUTER_FUSED_COUNT = 60
_WASHER_OUTER_FACE_CENSUS = {4: 9, 5: 14, 6: 16, 7: 12, 8: 3}


def test_stepped_rail_tunnel_topology_is_deterministic() -> None:
    net, tun = tunnel_steps((SEG_W, SEG_N), (44, 44))
    assert len(net.atoms) == 8940
    assert len(net.bonds) == 13194
    assert dict(Counter(len(r) for r in net.rings)) == _WASHER_RING_CENSUS
    assert tun.twist_deg == 48.0
    inner = next(j for j in tun.reducers if j.name.endswith(":inner"))
    outer = next(j for j in tun.reducers if j.name.endswith(":outer"))
    assert inner.atoms == _WASHER_INNER_ATOMS
    assert inner.fused == _WASHER_INNER_FUSED
    assert dict(Counter(len(f) for f in inner.faces)) == _WASHER_INNER_FACE_CENSUS
    assert len(outer.fused) == _WASHER_OUTER_FUSED_COUNT
    assert dict(Counter(len(f) for f in outer.faces)) == _WASHER_OUTER_FACE_CENSUS


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

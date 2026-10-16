"""S2 of docs/backlog/hexfold-ideal-surface-then-tile.md: the Gauss–Bonnet
defect census of an authored surface of revolution, and the 3+3 foot
planner that realises it (anchors pinned before and after the census)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from hexfold.build import build
from hexfold.lattice import tube_radius
from hexfold.stick import stick_info
from precis_se.atomic.generators import authored_foot as af
from precis_surface import revolution as rv


def _frustum(
    wide: tuple[int, int], narrow: tuple[int, int], turn_deg: float, fillet: float = 1.0
) -> rv.Meridian:
    """Sheet -> fillet -> ``wide`` tube -> fillet ``turn_deg`` toward the axis
    -> cone (a flat step at 90) -> fillet back -> ``narrow`` tube."""
    r1, r2 = tube_radius(*wide), tube_radius(*narrow)
    t = math.radians(turn_deg)
    run = ((r1 - r2) - 2.0 * fillet * (1.0 - math.cos(t))) / math.sin(t)
    return rv.authored_meridian(
        r1 + fillet + 10.0,
        [
            ("line", 10.0),
            ("arc", fillet, -90.0),
            ("line", 12.0),
            ("arc", fillet, turn_deg),
            ("line", run),
            ("arc", fillet, -turn_deg),
            ("line", 12.0),
        ],
    )


def _between_tubes(c: af.Census) -> list[af.Annulus]:
    """The bands from the first tube to the last: the frustum alone."""
    tubes = [i for i, a in enumerate(c.annuli) if a.tube is not None]
    return list(c.annuli[tubes[0] : tubes[-1] + 1])


def test_a_sheet_to_tube_fillet_carries_six_heptagons_in_two_c3_rows() -> None:
    c = af.census(af.foot_meridian(12, 8.0), 3, [(12, 0)])
    assert (c.pentagons, c.heptagons) == (0, 6)
    flat, fillet, tube = c.annuli
    assert fillet.charge == pytest.approx(-6.0, abs=1e-12)
    assert [r.k for r in fillet.rows] == [0, 1]
    assert {r.sign for r in fillet.rows} == {-1}
    assert tube.tube == (12, 0)
    assert flat.rows == tube.rows == ()
    # each row sits at the charge midpoint of its half, on the arc
    centre_r = tube_radius(12, 0) + 8.0
    for row in fillet.rows:
        assert math.hypot(row.r - centre_r, row.z - 8.0) == pytest.approx(8.0)
    assert [r.z for r in fillet.rows] == pytest.approx([2.0, 6.0])


def test_the_defect_list_writes_each_row_as_its_c_k_orbit() -> None:
    c = af.census(af.foot_meridian(12, 8.0), 3, [(12, 0)])
    lines = c.defect_list()
    assert len(lines) == 6
    assert all(line.startswith("heptagon@(") for line in lines)
    assert [line.rsplit(",", 1)[1] for line in lines[:3]] == ["0)", "120)", "240)"]
    assert len({line.rsplit(",", 1)[0] for line in lines}) == 2  # two rows


def test_a_cap_carries_six_pentagons() -> None:
    rt = tube_radius(12, 0)
    m = rv.authored_meridian(
        rt + 13.0,
        [("line", 10.0), ("arc", 3.0, -90.0), ("line", 8.0), ("arc", rt, 90.0)],
    )
    c = af.census(m, 6, [(12, 0)])
    assert (c.pentagons, c.heptagons) == (6, 6)
    assert c.annuli[-1].ring == 5
    assert len(c.annuli[-1].rows) == 1


@pytest.mark.parametrize(
    ("turn", "order", "each"), [(90.0, 6, 6), (30.0, 3, 3), (30.0, 1, 3)]
)
def test_a_filleted_frustum_between_tubes_has_as_many_pentagons_as_heptagons(
    turn: float, order: int, each: int
) -> None:
    """Euler: a tube-to-tube annulus has chi = 0, so the census of the
    frustum alone balances, whatever the cone angle."""
    c = af.census(_frustum((12, 12), (6, 6), turn), order, [(12, 12), (6, 6)])
    assert c.tileable
    band = _between_tubes(c)
    p = sum(a.defects for a in band if a.ring == 5)
    h = sum(a.defects for a in band if a.ring == 7)
    assert p == h == each
    assert sum(a.charge for a in band) == pytest.approx(0.0, abs=1e-9)
    assert band[0].tube == (12, 12) and band[-1].tube == (6, 6)
    for a in band:
        assert len(a.rows) * order == a.defects


def test_a_cone_angle_that_is_not_whole_defects_is_reported_not_snapped() -> None:
    c = af.census(_frustum((12, 12), (6, 6), 20.0), 3, [(12, 12), (6, 6)])
    assert not c.tileable
    bad = [a for a in c.annuli if a.misfit]
    assert len(bad) == 2
    assert all(a.rows == () for a in bad)
    assert c.pentagons == 0  # a misfit band places no defects
    assert bad[0].charge == pytest.approx(6.0 * math.sin(math.radians(20.0)))


def test_rows_that_do_not_split_by_the_order_are_a_misfit() -> None:
    # six heptagons do not make C_4 rows
    c = af.census(af.foot_meridian(12, 8.0), 4, [(12, 0)])
    assert not c.tileable
    assert "C_4" in (c.annuli[1].misfit or "")


def test_a_cylinder_must_be_the_exact_tube_it_names() -> None:
    with pytest.raises(ValueError, match=r"not the \(9,0\) tube"):
        af.census(af.foot_meridian(12, 8.0), 3, [(9, 0)])
    with pytest.raises(ValueError, match="1 cylinder pieces"):
        af.census(af.foot_meridian(12, 8.0), 3, [])
    with pytest.raises(ValueError, match=r"C_6 rows need 6 \| gcd\(9,0\)"):
        af.census(af.foot_meridian(9, 8.0), 6, [(9, 0)])


def test_an_arc_through_a_flat_heading_splits_into_bands_of_each_sign() -> None:
    # sheet -> tube up -> a 180 turn over the top and back down: the arc's
    # heading crosses pi once, convex over the top (pentagons), concave on
    # the way down inside it (heptagons)
    rt = tube_radius(12, 0)
    m = rv.authored_meridian(
        rt + 14.0,
        [("line", 10.0), ("arc", 4.0, -90.0), ("line", 6.0), ("arc", 2.0, 180.0)],
    )
    c = af.census(m, 3, [(12, 0)])
    over = [a for a in c.annuli if a.segment == c.annuli[-1].segment]
    assert len(over) == 2
    assert [round(a.charge) for a in over] == [6, -6]


def test_plan_foot_refuses_a_census_its_3_plus_3_build_cannot_realise() -> None:
    c = af.census(_frustum((12, 12), (6, 6), 90.0), 6, [(12, 12), (6, 6)])
    with pytest.raises(ValueError, match="the 3\\+3 foot builds"):
        af._foot_census(c)
    af._foot_census(af.census(af.foot_meridian(12, 8.0), 3, [(12, 0)]))


# --- the three foot anchors, pinned before S2 routed plan_foot through the
# census (tests/hexfold/data/foot_anchors.json).  In-session the refactor was
# byte-identical (sha256 of the positions); across platforms the relax is
# compared to 1e-6 relative.

_GOLDEN = json.loads(
    (Path(__file__).parent / "data" / "foot_anchors.json").read_text(encoding="utf-8")
)


@pytest.mark.slow
@pytest.mark.parametrize("anchor", sorted(_GOLDEN))
def test_foot_anchor_reproduces(anchor: str) -> None:
    g = _GOLDEN[anchor]
    plan = af.plan_foot(g["n"], g["radius"], candidates=tuple(g["candidates"]))
    assert plan.k == g["k"]
    assert plan.text == g["text"]
    assert list(plan.positions.shape) == g["shape"]
    assert plan.centre == pytest.approx(g["centre"], rel=1e-6)
    assert plan.positions.sum(axis=0) == pytest.approx(g["sum"], rel=1e-6, abs=1e-6)
    assert (plan.positions**2).sum(axis=0) == pytest.approx(g["sumsq"], rel=1e-6)
    assert len(plan.rows) == len(g["rows"])
    for row, want in zip(plan.rows, g["rows"], strict=True):
        assert row.k == want["k"]
        assert list(row.misses) == want["misses"]
        assert list(row.errors) == want["errors"]
        assert row.passes == want["passes"]
        for col in _ROW_FLOATS:
            assert getattr(row, col) == pytest.approx(want[col], rel=1e-6, abs=1e-9)
    assert plan.census is not None and plan.census.heptagons == 6
    assert not build(plan.text, strict=False).report.errors()
    assert np.isfinite(plan.positions).all()


_ROW_FLOATS = (
    "fillet_mean",
    "fillet_p95",
    "fillet_max",
    "bond_min",
    "bond_max",
    "angle_rms",
    "angle_max",
    "pyramid_max",
    "rim_r",
)


_REDUCER = (
    "hexfold 0.2\norigin wide\n"
    "wide: tube(12,12, len=3)\n"
    "narrow: tube(6,6, len=3)\n"
    "wide.out --fuse{reducer}--> narrow.in\n"
)


def test_the_census_step_matches_the_coaxial_5_7_reducer() -> None:
    """The census of a (12,12) -> (6,6) flat step against hexfold's explicit
    reducer: both balance (P = H), the reducer's extra rings are C_6
    symmetric 5/7 dislocation pairs (Gauss–Bonnet does not see them), and
    its relaxed bonds sit inside the S3 bond bars."""
    net = build(_REDUCER, strict=False)
    (found,) = [f for f in net.report.findings if f.code == "reducer.rings"]
    assert not net.report.errors()
    d = dict(found.data)
    c = af.census(_frustum((12, 12), (6, 6), 90.0), 6, [(12, 12), (6, 6)])
    band = _between_tubes(c)
    p = sum(a.defects for a in band if a.ring == 5)
    h = sum(a.defects for a in band if a.ring == 7)
    assert p == h == 6
    assert d["pentagons"] == d["heptagons"]
    assert d["pentagons"] >= p and (d["pentagons"] - p) % 6 == 0
    pos, _ = stick_info(net)
    pos = np.asarray(pos, dtype=float)
    deg = np.bincount([a for i, j, *_ in net.bonds for a in (i, j)], minlength=len(pos))
    # the open tube ends are not the reducer's: judge bonds between
    # three-coordinated atoms
    b = np.array([(i, j) for i, j, *_ in net.bonds if deg[i] == deg[j] == 3])
    bl = np.linalg.norm(pos[b[:, 0]] - pos[b[:, 1]], axis=1)
    assert bl.min() >= af.BOND_MIN_A and bl.max() <= af.BOND_MAX_A

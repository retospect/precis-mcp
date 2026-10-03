"""The authored surface and the deviation judge
(docs/backlog/hexfold-ideal-surface-then-tile.md, S1)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from precis_surface import revolution as rv
from precis_surface.deviation import Feature, summary, surface_distance, surface_foot


def _foot(r_tube: float = 4.7, r_f: float = 3.0, cap: bool = False) -> rv.Meridian:
    """Sheet -> concave fillet r_f -> tube, optionally closed by a
    hemisphere."""
    pieces: list = [("line", 10.0), ("arc", r_f, -90.0), ("line", 8.0)]
    if cap:
        pieces.append(("arc", r_tube, 90.0))
    return rv.authored_meridian(r_tube + r_f + 10.0, pieces)


def test_authored_meridian_keeps_the_authors_radii() -> None:
    m = _foot(4.7, 3.0)
    assert [s.kind for s in m.segments] == ["flat", "fillet", "cylinder"]
    assert m.segments[1].sign == -1  # sheet into tube: a saddle
    r, z = m.segments[-1].end
    assert r == pytest.approx(4.7) and z == pytest.approx(3.0 + 8.0)
    # the fillet is a true quarter circle of the authored radius
    pts = m.segments[1].at(np.linspace(0.0, 1.0, 33))
    centre = np.array([4.7 + 3.0, 0.0 + 3.0])
    assert np.allclose(np.linalg.norm(pts - centre, axis=1), 3.0)
    # a different radius is a different surface, not a snapped one
    assert _foot(4.7, 5.5).segments[1].end != m.segments[1].end


def test_a_cap_is_convex_and_closes_on_the_axis() -> None:
    m = _foot(4.7, 3.0, cap=True)
    cap = m.segments[-1]
    assert cap.kind == "fillet" and cap.sign == +1
    assert cap.end[0] == pytest.approx(0.0, abs=1e-9)
    assert cap.end[1] == pytest.approx(3.0 + 8.0 + 4.7)
    assert m.max_curvature_sum == pytest.approx(2.0 / 4.7, rel=1e-3)


@pytest.mark.parametrize(
    "pieces",
    [[("line", 0.0)], [("arc", -1.0, 90.0)], [("line", 20.0)], [("bend", 1.0)]],
)
def test_authored_meridian_refuses_bad_pieces(pieces: list) -> None:
    with pytest.raises(ValueError):
        rv.authored_meridian(10.0, pieces)


def _on_surface(m: rv.Meridian, centre: tuple[float, float], n: int) -> np.ndarray:
    rz = m.sample(0.05)[0]
    rng = np.random.default_rng(3)
    pick = rz[rng.integers(0, len(rz), n)]
    th = rng.uniform(0.0, 2.0 * math.pi, n)
    return np.column_stack(
        [
            centre[0] + pick[:, 0] * np.cos(th),
            centre[1] + pick[:, 0] * np.sin(th),
            pick[:, 1],
        ]
    )


def test_points_on_the_surface_measure_zero_and_offsets_measure_themselves() -> None:
    f = Feature("bump", (5.0, -2.0), _foot(cap=True))
    pts = _on_surface(f.meridian, f.centre, 400)
    dist, owner = surface_distance(pts, [f], ds=0.02)
    assert np.all(owner == 0)
    assert dist.max() < 1e-3
    # off the tube wall by 0.3 along the radius
    wall = np.array([[5.0 + 4.7 + 0.3, -2.0, 6.0]])
    assert surface_distance(wall, [f], ds=0.02)[0][0] == pytest.approx(0.3, abs=1e-3)
    # past the feature's reach the flat sheet judges: |z|
    far = np.array([[60.0, 0.0, -0.25]])
    d, o = surface_distance(far, [f], ds=0.02)
    assert o[0] == -1 and d[0] == pytest.approx(0.25)


def _analytic_foot(
    rt: float, rf: float, flat: float, wall: float, n: int, shift: float
) -> np.ndarray:
    """Points on sheet -> fillet rf -> cylinder rt, written from the closed
    form (not from the meridian code), moved ``shift`` along the unit
    normal, at random azimuths about the z axis."""
    rng = np.random.default_rng(11)
    cr, cz = rt + rf, rf
    out = []
    for _ in range(n):
        piece = rng.integers(3)
        if piece == 0:  # flat sheet, normal +z
            r, z, nr, nz = rng.uniform(cr, cr + flat), 0.0, 0.0, 1.0
        elif piece == 1:  # fillet: r = cr - rf sin(t), z = rf (1 - cos t)
            t = rng.uniform(0.0, math.pi / 2.0)
            r, z = cr - rf * math.sin(t), rf * (1.0 - math.cos(t))
            nr, nz = (r - cr) / rf, (z - cz) / rf
        else:  # cylinder wall, normal -r
            r, z, nr, nz = rt, rng.uniform(rf, rf + wall), -1.0, 0.0
        r, z = r + shift * nr, z + shift * nz
        th = rng.uniform(0.0, 2.0 * math.pi)
        out.append((r * math.cos(th), r * math.sin(th), z))
    return np.array(out)


@pytest.mark.parametrize("shift", [0.0, 0.37, -0.37])
def test_the_judge_is_exact_on_an_analytic_foot(shift: float) -> None:
    rt, rf, flat, wall = 4.7, 3.0, 10.0, 8.0
    m = rv.authored_meridian(
        rt + rf + flat, [("line", flat), ("arc", rf, -90.0), ("line", wall)]
    )
    pts = _analytic_foot(rt, rf, flat, wall, 600, shift)
    # keep the shifted points inside the feature's disc
    pts = pts[np.hypot(pts[:, 0], pts[:, 1]) <= rt + rf + flat]
    dist, owner = surface_distance(pts, [Feature("foot", (0.0, 0.0), m)], ds=1.0)
    assert np.all(owner == 0)
    assert np.allclose(dist, abs(shift), atol=1e-9, rtol=0.0)


@pytest.mark.parametrize("shift", [0.37, -0.37])
def test_surface_foot_recovers_the_point_and_its_normal(shift: float) -> None:
    rt, rf, flat, wall = 4.7, 3.0, 10.0, 8.0
    m = rv.authored_meridian(
        rt + rf + flat, [("line", flat), ("arc", rf, -90.0), ("line", wall)]
    )
    f = [Feature("foot", (1.0, -2.0), m)]
    on = _analytic_foot(rt, rf, flat, wall, 300, 0.0)
    off = _analytic_foot(rt, rf, flat, wall, 300, shift)
    keep = np.hypot(off[:, 0], off[:, 1]) <= rt + rf + flat
    on, off = on[keep] + [1.0, -2.0, 0.0], off[keep] + [1.0, -2.0, 0.0]
    foot, nrm = surface_foot(off, f, ds=1.0)
    assert np.allclose(foot, on, atol=1e-9)
    assert np.allclose(np.linalg.norm(nrm, axis=1), 1.0)
    # the point is its foot plus the shift along the (signed) normal
    along = ((off - foot) * nrm).sum(axis=1)
    assert np.allclose(np.abs(along), 0.37, atol=1e-9)
    assert np.allclose(off, foot + along[:, None] * nrm, atol=1e-9)


def test_z_offset_is_the_only_alignment() -> None:
    f = Feature("bump", (0.0, 0.0), _foot())
    pts = _analytic_foot(4.7, 3.0, 10.0, 8.0, 100, 0.0) + np.array([0.0, 0.0, 1.714])
    assert surface_distance(pts, [f], ds=1.0)[0].max() > 1.0
    assert surface_distance(pts, [f], ds=1.0, z_offset=1.714)[0].max() < 1e-9


def test_arc_curvatures_carry_their_sign() -> None:
    m = _foot(4.7, 3.0, cap=True)
    foot, cap = (rv.arc_curvature(s) for s in m.segments if s.arc is not None)
    assert foot.sign == -1 and foot.k1 == pytest.approx(1 / 3.0)
    assert foot.k2_min == pytest.approx(0.0, abs=1e-12)
    assert foot.k2_max == pytest.approx(1 / 4.7)
    assert foot.gaussian_range == pytest.approx((-1 / (3.0 * 4.7), 0.0), abs=1e-12)
    assert cap.sign == +1 and cap.k_sum_max == pytest.approx(2 / 4.7, rel=1e-3)
    assert min(cap.gaussian_range) > 0.0
    # only convex arcs feed the bound
    assert m.max_curvature_sum == pytest.approx(cap.k_sum_max)


def test_summary_reports_each_region() -> None:
    f = Feature("pillar", (0.0, 0.0), _foot())
    pts = np.vstack([_on_surface(f.meridian, f.centre, 50), [[40.0, 0.0, 0.1]]])
    dist, owner = surface_distance(pts, [f], ds=0.02)
    s = summary(dist, owner, [f])
    assert s["sheet"]["atoms"] == 1 and s["sheet"]["max"] == pytest.approx(0.1)
    assert s["pillar"]["atoms"] == 50 and s["pillar"]["max"] < 1e-3


def test_overlapping_features_are_refused() -> None:
    a = Feature("a", (0.0, 0.0), _foot())
    b = Feature("b", (10.0, 0.0), _foot())
    with pytest.raises(ValueError, match="overlap"):
        surface_distance(np.zeros((1, 3)), [a, b], ds=0.1)

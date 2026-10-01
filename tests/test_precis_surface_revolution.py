"""Smooth drum targets (docs/backlog/precis-surface-kernel.md "Slice -- smooth
drum"): catenoid bends, table-picked fillets, defect rows, revolve."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from hexfold import radii
from hexfold.lattice import tube_radius
from precis_surface import revolution as rv
from precis_surface.curvature import gaussian_curvature, mean_curvature


def _drum(**kw: float) -> rv.Meridian:
    args: dict[str, Any] = {
        "neck": tube_radius(24, 0),
        "wall_radius": tube_radius(90, 0),
        "stalk_length": 15.0,
        "wall_height": 30.0,
        "sheet_radius": 60.0,
        "fillet_candidates": radii.fillet_radii(),
        "curvature_sum_max": radii.curvature_sum_bound(),
    }
    args.update(kw)
    return rv.drum_meridian(**args)


def test_c60_calibrates_the_tables() -> None:
    r60 = radii.fullerene_radius(60)
    assert r60 == pytest.approx(3.54, abs=0.02)
    assert radii.icosahedral_sizes(600)[:4] == [60, 80, 140, 180]
    # theta_p from mean curvature reproduces C60's POAV1 angle to 0.1 deg
    assert radii.theta_p_deg(2.0 / r60) == pytest.approx(radii.THETA_P_C60_DEG, abs=0.1)
    # and the bound is the C60 sphere's k1 + k2
    assert radii.curvature_sum_bound() == pytest.approx(2.0 / r60, rel=0.01)


def test_catenoid_rows_spread_from_the_neck() -> None:
    got = [r / 1.0 for r in rv.catenoid_row_radii(1.0)]
    assert got == pytest.approx([2.502, 1.512, 1.231, 1.100, 1.033, 1.0035], abs=2e-3)
    assert rv.catenoid_cut_radius(1.0) == pytest.approx(got[0])


def test_drum_segments_join_and_pick_the_largest_fillet() -> None:
    m = _drum()
    names = [s.name for s in m.segments]
    assert names == [
        "sheet",
        "foot",
        "stalk",
        "flare",
        "floor",
        "bottom",
        "wall",
        "top",
        "lid",
    ]
    for prev, nxt in zip(m.segments[:-1], m.segments[1:], strict=True):
        assert nxt.start == pytest.approx(prev.end, abs=1e-9)
    fits = [
        rho
        for rho in radii.fillet_radii()
        if tube_radius(90, 0) - rho >= rv.catenoid_cut_radius(tube_radius(24, 0))
    ]
    assert m.fillet_radius == max(fits)
    # the lid ends on the axis
    assert m.segments[-1].end[0] == pytest.approx(0.0, abs=1e-12)
    # 6 heptagon rows per catenoid, 6 pentagon rows per fillet
    signs = [d.sign for d in m.rows]
    assert signs.count(-1) == 12 and signs.count(+1) == 12


def test_min_flat_shrinks_the_fillet() -> None:
    narrowed, default = _drum(min_flat=5.0).fillet_radius, _drum().fillet_radius
    assert narrowed is not None and default is not None
    assert narrowed < default


def test_too_narrow_a_drum_refuses() -> None:
    # drum33's sizes: a (36,0) wall cannot hold a smooth (12,0) flare plus C60
    with pytest.raises(ValueError, match="no fillet fits"):
        _drum(neck=tube_radius(12, 0), wall_radius=tube_radius(36, 0))
    with pytest.raises(ValueError, match="curvature bound"):
        _drum(curvature_sum_max=0.05)


def test_revolved_surface_has_the_target_curvature() -> None:
    m = _drum()
    pts, tilt, seg = m.sample(0.25)
    verts, tris = rv.revolve(pts, 240)
    # closed at the pole, open at the sheet edge: a disc, chi = 1
    edges = {
        tuple(sorted(e))
        for t in tris
        for e in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0]))
    }
    assert len(verts) - len(edges) + len(tris) == 1
    h = mean_curvature(verts, tris)
    k = gaussian_curvature(verts, tris)
    names = [s.name for s in m.segments]
    ring = len(pts) - 1  # pole vertex is last; rings precede it
    per_point_h = h[: ring * 240].reshape(ring, 240).mean(axis=1)
    per_point_k = k[: ring * 240].reshape(ring, 240).sum(axis=1)
    inner = slice(3, -3)
    foot = np.where(seg[:ring] == names.index("foot"))[0][inner]
    assert np.abs(per_point_h[foot]).max() < 0.02  # catenoid: H = 0
    assert (per_point_k[foot] < 0).all()
    bottom = np.where(seg[:ring] == names.index("bottom"))[0][inner]
    assert (per_point_k[bottom] > 0).all()
    # each bend turns a quarter: total curvature of the foot is -2 pi
    # minus the truncated tail, i.e. -(q - cut_quanta)/q of it
    foot_all = np.where(seg[:ring] == names.index("foot"))[0][1:-1]
    assert per_point_k[foot_all].sum() == pytest.approx(
        -2 * math.pi * (1 - 0.5 / 6), rel=0.03
    )
    assert tilt[0] == pytest.approx(0.0, abs=1e-6)

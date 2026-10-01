"""Row fit of a hex-lattice net to a smooth drum (docs/backlog/
precis-surface-kernel.md "Slice -- smooth drum", step 3): the planned
defects and only those, spread apart, near the smooth radii."""

from __future__ import annotations

import math
from collections import Counter
from itertools import pairwise

import numpy as np
import pytest

from hexfold import radii
from hexfold.lattice import tube_radius
from precis_surface import revolution as rv
from precis_surface import rowfit

EDGE = math.sqrt(3.0) * 1.42


def _interior_degrees(counts: list[int]) -> Counter[int]:
    _angles, tris, _defects = rowfit.loft(counts)
    deg = np.bincount(np.asarray(tris).ravel())
    off = np.cumsum([0] + [max(n, 1) for n in counts])
    inner = range(off[1], off[-2])  # skip the first ring/pole and the open last ring
    return Counter(int(deg[i]) for i in inner) + (
        Counter({int(deg[0]): 1}) if counts[0] == 0 else Counter()
    )


def test_loft_flat_disc_is_all_hexagons() -> None:
    assert _interior_degrees([0, 6, 12, 18, 24]) == Counter({6: 1 + 6 + 12 + 18})


def test_loft_cone_puts_its_defect_at_the_pole() -> None:
    got = _interior_degrees([0, 5, 10, 15, 20])
    assert got[5] == 1 and set(got) == {5, 6}


def test_loft_cylinder_and_one_step() -> None:
    assert set(_interior_degrees([24, 24, 24, 24])) == {6}
    # a single count step 24 -> 25 is a lone defect pair-free row change
    got = _interior_degrees([24, 24, 25, 26, 27, 27])
    assert sum(got[d] for d in got if d != 6) == 2  # enters and leaves state 1


@pytest.fixture(scope="module")
def drum() -> tuple[rv.Meridian, rowfit.RowFit]:
    m = rv.drum_meridian(
        neck=tube_radius(24, 0),
        wall_radius=tube_radius(90, 0),
        stalk_length=10.0,
        wall_height=24.0,
        sheet_radius=40.0,
        fillet_candidates=radii.fillet_radii(),
        curvature_sum_max=radii.curvature_sum_bound(),
        min_flat=2.46,
    )
    rows = rowfit.fit_rows(m, {"stalk": 24, "wall": 90}, edge=EDGE)
    return m, rowfit.realise(m, rows)


def test_drum_has_exactly_the_planned_defects(
    drum: tuple[rv.Meridian, rowfit.RowFit],
) -> None:
    _m, f = drum
    deg = f.degree()
    assert Counter(int(deg[i]) for i in f.defects) == Counter({5: 12, 7: 12})
    # the anchors hold their tube counts
    assert 24 in f.counts and 90 in f.counts and f.counts[0] == 0


def test_drum_defects_never_touch(drum: tuple[rv.Meridian, rowfit.RowFit]) -> None:
    _m, f = drum
    deg = f.degree()
    bad = set(f.defects)
    edges = {
        (min(a, b), max(a, b))
        for t in f.tris
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0]))
    }
    assert not [(a, b) for a, b in edges if a in bad and b in bad], "adjacent defects"
    assert {int(deg[i]) for i in f.defects} <= {5, 7}


def test_drum_defects_sit_near_the_smooth_rows(
    drum: tuple[rv.Meridian, rowfit.RowFit],
) -> None:
    m, f = drum
    targets = np.array([(d.r, d.z) for d in m.rows])
    for i in f.defects:
        x, y, z = f.verts[i]
        gap = np.hypot(targets[:, 0] - math.hypot(x, y), targets[:, 1] - z).min()
        # row quantisation: about one lattice row (edge) plus the closure move
        assert gap < 2.0 * EDGE, (i, gap)


def test_dual_is_a_trivalent_disc(drum: tuple[rv.Meridian, rowfit.RowFit]) -> None:
    _m, f = drum
    coord = np.bincount(f.bonds.ravel(), minlength=len(f.atoms))
    assert set(coord.tolist()) <= {2, 3}
    assert (coord == 2).sum() == f.counts[-1]  # dangling atoms only at the sheet edge
    length = np.linalg.norm(f.atoms[f.bonds[:, 0]] - f.atoms[f.bonds[:, 1]], axis=1)
    assert 1.3 < length.mean() < 1.6


def test_rows_are_deterministic(drum: tuple[rv.Meridian, rowfit.RowFit]) -> None:
    m, f = drum
    again = rowfit.realise(m, rowfit.fit_rows(m, {"stalk": 24, "wall": 90}, edge=EDGE))
    assert again.counts == f.counts
    assert np.array_equal(again.tris, f.tris)


def test_anchor_must_be_a_cylinder(drum: tuple[rv.Meridian, rowfit.RowFit]) -> None:
    m, _f = drum
    with pytest.raises(ValueError, match="not a cylinder"):
        rowfit.fit_rows(m, {"lid": 6}, edge=EDGE)


def test_counts_change_by_at_most_q(drum: tuple[rv.Meridian, rowfit.RowFit]) -> None:
    _m, f = drum
    assert all(abs(b - a) <= 6 for a, b in pairwise(f.counts))

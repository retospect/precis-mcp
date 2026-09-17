"""Lattice arithmetic: directions, roll-up numbers, tube geometry."""

from __future__ import annotations

import math

import pytest

from hexfold.lattice import (
    DIR_AXIAL,
    SP3_IDEAL_DEG,
    Lattice,
    Site,
    cell_index,
    dr,
    ideal_angle_deg,
    n_cells,
    rotate_axial,
    tube_radius,
    tube_sites,
    wrap_tube,
)


def test_dirs_are_six_axial_steps() -> None:
    assert len(DIR_AXIAL) == 6
    assert DIR_AXIAL[0] == (1, 0)
    # all six are the triangular-lattice unit vectors
    assert set(DIR_AXIAL) == {(1, 0), (0, 1), (-1, 1), (-1, 0), (0, -1), (1, -1)}


def test_rotate_axial_c6() -> None:
    p = (1, 0)
    seen = {p}
    for _ in range(6):
        p = rotate_axial(*p, 1)
        seen.add(p)
    assert len(seen) == 6
    assert p == (1, 0)


def test_site_is_frozen() -> None:
    s = Site(1, 2, 0)
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        s.u = 5  # type: ignore[misc]


def test_cart_sublattice_offset() -> None:
    lat = Lattice()
    a = lat.cart(Site(0, 0, 0))
    b = lat.cart(Site(0, 0, 1))
    assert abs(float(math.dist(a, b)) - lat.sigma_A) < 1e-9


def test_dr_and_cells() -> None:
    assert dr(5, 5) == 15
    assert dr(10, 0) == 10
    assert n_cells(5, 5) == 10
    assert n_cells(10, 0) == 20


@pytest.mark.parametrize(
    ("n", "m", "expected"),
    [(5, 5, 3.39), (10, 0, 3.91)],
)
def test_tube_radius(n: int, m: int, expected: float) -> None:
    assert abs(tube_radius(n, m) - expected) < 0.02


def test_wrap_tube_identifies_chiral_vector() -> None:
    n, m = 5, 5
    for s in tube_sites(n, m, 2):
        w = wrap_tube(s, n, m)
        assert w in set(tube_sites(n, m, 2))
    # a chiral step must land back in the unit strip
    from hexfold.lattice import chiral_vector

    cu, cv = chiral_vector(n, m)
    s0 = Site(0, 0, 0)
    w = wrap_tube(Site(s0.u + cu, s0.v + cv, 0), n, m)
    assert w == s0


def test_cell_index_last_cell() -> None:
    n, m, length = 5, 5, 4
    sites = tube_sites(n, m, length)
    assert len(sites) == 2 * n_cells(n, m) * length
    for s in sites:
        assert 0 <= cell_index(s, n, m) < length


def test_ideal_angle_deg_sp2_is_ring_ideal() -> None:
    assert ideal_angle_deg(6, "sp2") == pytest.approx(120.0)
    assert ideal_angle_deg(5, "sp2") == pytest.approx(108.0)
    assert ideal_angle_deg(7, "sp2") == pytest.approx(900.0 / 7.0)


def test_ideal_angle_deg_sp3_is_tetrahedral_regardless_of_ring() -> None:
    assert pytest.approx(109.47) == SP3_IDEAL_DEG
    for n in (4, 5, 6, 7, 8):
        assert ideal_angle_deg(n, "sp3") == SP3_IDEAL_DEG

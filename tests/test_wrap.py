"""Shared cylinder-wrap mapping — slice 4 of
``docs/backlog/se-cylinder-wrap-mold.md``.

``hexfold.wrap`` is the dimension-free core both realisations use: the macro
height relief (``precis.cad``) and hexfold's nano roll-up. These pin that the
one object actually unifies them — the forward/inverse map round-trips, the
rewired cad call sites reproduce the old inline formula exactly, and a lattice
site's ``(u, v)`` is the same wrap frame the relief leaf wraps.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from hexfold import lattice
from hexfold.wrap import WrapSpec, canonical_u, seam_error


def test_repeat_must_be_positive() -> None:
    with pytest.raises(ValueError, match="repeat must be >= 1"):
        WrapSpec(repeat=0)


def test_canonical_u_folds_into_unit_interval() -> None:
    xs = np.array([-2.25, -0.1, 0.0, 0.3, 1.0, 1.75, 3.5])
    out = canonical_u(xs)
    assert np.all((out >= 0.0) & (out < 1.0))
    # periodic: x and x+1 land on the same point
    assert np.allclose(canonical_u(xs), canonical_u(xs + 1.0))


@pytest.mark.parametrize("repeat", [1, 2, 5])
@pytest.mark.parametrize("twist", [0.0, 0.7, -1.3])
def test_theta_u_of_round_trip(repeat: int, twist: float) -> None:
    """``u_of`` is the exact inverse of ``theta`` (mod the u-period)."""
    spec = WrapSpec(repeat=repeat, twist=twist)
    u = np.linspace(0.0, 1.0, 17, endpoint=False)
    v = np.linspace(0.0, 1.0, 13)
    uu, vv = np.meshgrid(u, v, indexing="ij")
    theta = spec.theta(uu, vv)
    back = spec.u_of(theta / (2.0 * math.pi), vv)
    # u is periodic: compare on the circle, so float rounding that folds
    # u = 0 to ~1.0 across the seam is not a spurious mismatch.
    circ = np.abs((back - uu + 0.5) % 1.0 - 0.5)
    assert np.all(circ < 1e-9)


@pytest.mark.parametrize("repeat", [1, 3])
@pytest.mark.parametrize("twist", [0.0, 1.1])
def test_theta_matches_legacy_inline_formula(repeat: int, twist: float) -> None:
    """The cad call sites (hmap/mold) were ``(2π·u − twist·v)/repeat`` inline;
    the shared method must reproduce it bit-for-bit."""
    u = np.linspace(0.0, 1.0, 11, endpoint=False)[:, None]
    v = np.linspace(0.0, 1.0, 7)[None, :]
    legacy = (2.0 * math.pi * u - twist * v) / repeat
    assert np.array_equal(WrapSpec(repeat, twist).theta(u, v), legacy)


def test_seam_error_zero_for_periodic_map_and_measures_the_cliff() -> None:
    nu, nv = 8, 5
    # a map continuous across the u-seam: cos(2π u) sampled at cell centres,
    # first and last rows are adjacent samples, not identical — use a map
    # whose edge rows match exactly to pin the zero case.
    flat = np.ones((nu, nv))
    assert seam_error(flat) == 0.0
    cliff = flat.copy()
    cliff[-1, :] += 0.4  # last u-row jumps away from the first
    assert seam_error(cliff) == pytest.approx(0.4)


def test_cad_seam_error_is_the_shared_one() -> None:
    """``precis.cad.hmap.seam_error`` is re-exported from here, so the macro
    side and this module cannot drift apart."""
    from precis.cad import hmap

    assert hmap.seam_error is seam_error


@pytest.mark.parametrize("nm", [(6, 0), (5, 5), (6, 2)])
def test_lattice_site_uv_is_the_wrap_frame(nm: tuple[int, int]) -> None:
    """The nano realisation: a lattice site's ``(u, v)`` is the same wrap
    frame the relief leaf wraps. ``u`` is periodic circumferential, and the
    tube azimuth is ``WrapSpec().theta(u, v)`` with repeat=1, twist=0."""
    n, m = nm
    sites = lattice.tube_sites(n, m, length=2)
    spec = WrapSpec(repeat=1, twist=0.0)
    for site in sites:
        u, v = lattice.site_uv(site, n, m)
        assert 0.0 <= u < 1.0
        # u is exactly the circumferential coordinate
        assert u == pytest.approx(lattice.circumferential(site, n, m))
        # cell_index is the floor of the shared axial coordinate
        assert lattice.cell_index(site, n, m) == math.floor(v + 1e-9)
        # the tube azimuth of the site is the wrap map's theta at (u, v)
        azimuth = spec.theta(u, v)
        assert canonical_u(azimuth / (2.0 * math.pi)) == pytest.approx(u)

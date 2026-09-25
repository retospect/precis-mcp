"""``surface_report`` is the slice's measuring instrument, so it gets
tested harder than the things it measures: a wrong number here is worse
than a missing one, because it carries the authority of having been
measured.

The welded-geometry bug these tests were written after is the case in
point -- angles read across welded coordinates gave per-vertex defects
that were wrong by three orders of magnitude while the Gauss-Bonnet
total stayed correct to 1e-13, because that total is a combinatorial
identity and cannot see coordinates at all."""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pytest
from numpy.typing import NDArray

from precis_surface import level_set as L
from precis_surface.periodic_mesh import periodic_mesh
from precis_surface.report import surface_report

FieldFn = Callable[[NDArray[np.float64], float], NDArray[np.float64]]
GradFn = Callable[[NDArray[np.float64], float], NDArray[np.float64]]

_FAMILIES: list[tuple[str, FieldFn, GradFn, int]] = [
    ("P", L.schwarz_p, L.schwarz_p_grad, -4),
    ("D", L.schwarz_d, L.schwarz_d_grad, -16),
    ("gyroid", L.gyroid, L.gyroid_grad, -8),
]


def _at(fn: FieldFn, a: float) -> Callable[[NDArray[np.float64]], NDArray[np.float64]]:
    """Bind the cell edge, leaving a one-argument field/gradient.

    A ``lambda p, f=fn: f(p, a)`` default-argument closure would do the
    same at runtime, but mypy cannot infer a lambda with defaults
    (``Cannot infer type of lambda``), and silencing that with an ignore
    would hide any genuine signature drift here."""

    def bound(pts: NDArray[np.float64]) -> NDArray[np.float64]:
        return fn(pts, a)

    return bound


@pytest.mark.parametrize(("name", "field", "grad", "want_chi"), _FAMILIES)
@pytest.mark.parametrize("n", [17, 25])
def test_report_invariants(
    name: str, field: FieldFn, grad: GradFn, want_chi: int, n: int
) -> None:
    a = 1.0
    pm = periodic_mesh(_at(field, a), a, n, grad=_at(grad, a))
    r = surface_report(pm, grad=_at(grad, a))

    assert r.chi == want_chi
    assert r.edge_manifold_closed
    assert r.misoriented == 0
    assert r.gauss_bonnet_err < 1e-5

    # Edge lengths must be measured on unwelded coordinates. If welded
    # coordinates leak back in, boundary edges span the cell and the mean
    # roughly doubles -- so bound it against the grid pitch it must track.
    pitch = a / n
    assert 0.5 * pitch < r.edge_len_mean < 1.5 * pitch, (
        f"{name} n={n}: edge_len_mean {r.edge_len_mean:.4f} is not near the "
        f"grid pitch {pitch:.4f} -- welded coordinates leaking into geometry?"
    )
    assert r.edge_len_cv < 1.0


@pytest.mark.parametrize(("name", "field", "grad", "want_chi"), _FAMILIES)
def test_positive_defect_is_noise_not_structure(
    name: str, field: FieldFn, grad: GradFn, want_chi: int
) -> None:
    """A TPMS has K <= 0 everywhere, so every positive angle defect is a
    discretisation artifact. Assert they stay *small relative to the real
    negative curvature* and shrink under refinement -- the property that
    distinguishes noise from the spike vertices an earlier, buggy
    measurement appeared to show."""
    a = 1.0
    maxima = []
    for n in (17, 25, 33):
        pm = periodic_mesh(_at(field, a), a, n, grad=_at(grad, a))
        r = surface_report(pm)
        assert r.max_defect < 0.2 * abs(r.min_defect), (
            f"{name} n={n}: max positive defect {r.max_defect:.3e} is not "
            f"small against the typical negative {r.min_defect:.3e}"
        )
        maxima.append(r.max_defect)
    assert maxima[-1] < maxima[0], (
        f"{name}: positive defect does not shrink with refinement "
        f"({maxima}) -- that would make it structure, not noise"
    )


def test_gauss_bonnet_total_cannot_detect_coordinate_error() -> None:
    """Guards the trap directly: scrambling every vertex position leaves
    the angle-defect total at 2*pi*chi, because the identity is
    combinatorial. Anyone tempted to treat that total as a geometry check
    should read this test."""
    a = 1.0
    pm = periodic_mesh(_at(L.schwarz_p, a), a, 17)
    good = surface_report(pm)

    rng = np.random.default_rng(0)
    scrambled = type(pm)(
        verts=pm.verts + rng.normal(0.0, 0.05, pm.verts.shape),
        tris=pm.tris,
        wrap=pm.wrap,
        cell=pm.cell,
        orientation_fallback_count=pm.orientation_fallback_count,
    )
    bad = surface_report(scrambled)

    assert bad.chi == good.chi
    assert bad.gauss_bonnet_err < 1e-5, "total is combinatorial: still holds"
    assert abs(bad.defect_sum - 2.0 * math.pi * good.chi) < 1e-5
    # ... while the distribution moves, which is what actually matters.
    assert bad.positive_defect_frac > 2.0 * good.positive_defect_frac

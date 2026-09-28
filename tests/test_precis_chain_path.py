"""precis_chain.path — interpolation and arc-length reparametrisation theorems.

Every assertion here is recomputed from the returned arrays: the Catmull-Rom
interpolation property is checked by indexing the samples where the waypoints
must be, and the resample's uniformity by differencing the returned ``s``.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis_chain.path import (
    Path,
    catmull_rom,
    hermite,
    polyline,
    resample_arc_length,
    sample_at,
)

_WAYPOINTS = np.array(
    [
        [0.0, 0.0, 0.0],
        [3.0, 1.0, 0.0],
        [6.0, -1.0, 2.0],
        [9.0, 0.0, 0.0],
        [12.0, 2.0, -1.0],
    ]
)


def test_polyline_tangents_are_unit_and_arc_length_is_cumulative_chord() -> None:
    pts = np.array([[0.0, 0.0, 0.0], [3.0, 4.0, 0.0], [3.0, 4.0, 12.0]])
    path = polyline(pts)
    assert np.allclose(np.linalg.norm(path.tangents, axis=1), 1.0)
    assert np.allclose(path.s, [0.0, 5.0, 17.0])
    assert path.length == pytest.approx(17.0)
    # One-sided ends: the first tangent is the first chord's direction.
    assert np.allclose(path.tangents[0], [0.6, 0.8, 0.0])
    assert np.allclose(path.tangents[-1], [0.0, 0.0, 1.0])


def test_catmull_rom_passes_through_every_waypoint() -> None:
    for spacing in (1, 4, 16):
        path = catmull_rom(_WAYPOINTS, samples_per_span=spacing)
        assert len(path) == (len(_WAYPOINTS) - 1) * spacing + 1
        assert np.allclose(path.points[::spacing], _WAYPOINTS, atol=1e-12)


def test_catmull_rom_closed_returns_to_the_first_waypoint() -> None:
    path = catmull_rom(_WAYPOINTS, closed=True, samples_per_span=8)
    assert len(path) == len(_WAYPOINTS) * 8 + 1
    assert np.allclose(path.points[::8][:-1], _WAYPOINTS, atol=1e-12)
    assert np.allclose(path.points[-1], _WAYPOINTS[0], atol=1e-12)


def test_catmull_rom_alpha_changes_the_shape_but_not_the_waypoints() -> None:
    uniform = catmull_rom(_WAYPOINTS, samples_per_span=8, alpha=0.0)
    centripetal = catmull_rom(_WAYPOINTS, samples_per_span=8, alpha=0.5)
    assert np.allclose(uniform.points[::8], centripetal.points[::8], atol=1e-12)
    assert not np.allclose(uniform.points, centripetal.points, atol=1e-6)


def test_resample_arc_length_spacing_is_uniform_to_1e_9_relative() -> None:
    path = catmull_rom(_WAYPOINTS, samples_per_span=32)
    for step in (0.05, 0.2, 1.0):
        out = resample_arc_length(path, step)
        gaps = np.diff(out.s)
        assert np.ptp(gaps) / gaps.mean() < 1e-9
        assert out.s[0] == 0.0
        assert out.s[-1] == pytest.approx(path.length, rel=1e-12)
        # The chosen spacing is the nearest divisor of the total length.
        assert gaps.mean() == pytest.approx(path.length / round(path.length / step))


def test_resample_arc_length_keeps_the_endpoints_exactly() -> None:
    path = catmull_rom(_WAYPOINTS, samples_per_span=16)
    out = resample_arc_length(path, 0.37)
    assert np.allclose(out.points[0], path.points[0])
    assert np.allclose(out.points[-1], path.points[-1])


def test_hermite_hits_its_endpoints_and_tangent_directions() -> None:
    p0 = np.array([0.0, 0.0, 0.0])
    p1 = np.array([4.0, 0.0, 0.0])
    m0 = np.array([0.0, 6.0, 0.0])
    m1 = np.array([0.0, -6.0, 0.0])
    path = hermite(p0, m0, p1, m1, samples=64)
    assert np.allclose(path.points[0], p0)
    assert np.allclose(path.points[-1], p1)
    assert np.allclose(path.tangents[0], m0 / np.linalg.norm(m0))
    assert np.allclose(path.tangents[-1], m1 / np.linalg.norm(m1))


def test_sample_at_clamps_outside_the_path_and_interpolates_inside() -> None:
    path = polyline(np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]]))
    pts, tans = sample_at(path, np.array([-5.0, 2.5, 10.0, 99.0]))
    assert np.allclose(pts[:, 0], [0.0, 2.5, 10.0, 10.0])
    assert np.allclose(tans, np.tile([1.0, 0.0, 0.0], (4, 1)))


def test_path_rejects_mismatched_arrays() -> None:
    pts = np.zeros((4, 3))
    with pytest.raises(ValueError, match="tangents"):
        Path(pts, np.zeros((3, 3)), np.zeros(4))
    with pytest.raises(ValueError, match="Path.s"):
        Path(pts, np.zeros((4, 3)), np.zeros(5))
    with pytest.raises(ValueError, match=r"Path.points must be"):
        Path(np.zeros((4, 2)), np.zeros((4, 2)), np.zeros(4))


def test_degenerate_inputs_raise_rather_than_returning_a_useless_path() -> None:
    flat = polyline(np.zeros((5, 3)))
    with pytest.raises(ValueError, match="zero arc length"):
        resample_arc_length(flat, 0.1)
    with pytest.raises(ValueError, match="step > 0"):
        resample_arc_length(polyline(np.array([[0.0] * 3, [1.0, 0.0, 0.0]])), 0.0)
    with pytest.raises(ValueError, match=">= 2 waypoints"):
        catmull_rom(np.zeros((1, 3)))
    with pytest.raises(ValueError, match="samples >= 2"):
        hermite(np.zeros(3), np.ones(3), np.ones(3), np.ones(3), samples=1)

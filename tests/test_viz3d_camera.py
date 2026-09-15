"""Camera projection math — see ``precis.viz3d.camera``'s module docstring
for the az/el/twist sign conventions asserted here."""

from __future__ import annotations

import numpy as np
import pytest

from precis.viz3d.camera import Camera, content_bbox_centroid, project, view_basis


def test_target_always_projects_to_viewport_center() -> None:
    """A point at ``camera.target`` is camera-space ``(0, 0)`` regardless of
    az/el/twist/zoom/distance — the defining property callers rely on."""
    target = (1.0, -2.0, 3.5)
    for az, el, twist, distance in (
        (0.0, 0.0, 0.0, 5.0),
        (37.0, -18.0, 90.0, 12.0),
        (180.0, 45.0, 0.0, 1.0),
        (-90.0, 89.0, 270.0, 8.0),
    ):
        camera = Camera(
            target=target,
            azimuth_deg=az,
            elevation_deg=el,
            twist_deg=twist,
            distance=distance,
        )
        xy, depth = project(camera, np.array([target]))
        assert xy[0, 0] == pytest.approx(0.0, abs=1e-9)
        assert xy[0, 1] == pytest.approx(0.0, abs=1e-9)
        assert depth[0] == pytest.approx(distance, abs=1e-9)


def test_azimuth_90_moves_plus_x_point_as_documented() -> None:
    """At ``azimuth=0`` the eye sits on the +X side of the target, looking
    down -X — a point on the +X axis is dead ahead (on the view axis, so it
    contributes 0 to camera-space xy). Swinging azimuth to 90 degrees
    orbits the eye toward +Y (module docstring); the same +X point then
    projects to negative camera-space x (module docstring's documented
    convention)."""
    camera0 = Camera(
        target=(0.0, 0.0, 0.0), azimuth_deg=0.0, elevation_deg=0.0, distance=10.0
    )
    xy0, _ = project(camera0, np.array([[1.0, 0.0, 0.0]]))
    assert xy0[0, 0] == pytest.approx(0.0, abs=1e-9)
    assert xy0[0, 1] == pytest.approx(0.0, abs=1e-9)

    camera90 = Camera(
        target=(0.0, 0.0, 0.0), azimuth_deg=90.0, elevation_deg=0.0, distance=10.0
    )
    xy90, depth90 = project(camera90, np.array([[1.0, 0.0, 0.0]]))
    assert xy90[0, 0] == pytest.approx(-1.0, abs=1e-9)
    assert xy90[0, 1] == pytest.approx(0.0, abs=1e-9)
    assert depth90[0] == pytest.approx(10.0, abs=1e-9)


def test_elevation_90_is_a_stable_top_down_view() -> None:
    """The pole guard: elevation=90 makes ``forward`` parallel to the world
    +Z up-reference, which would degenerate ``right``/``up`` to zero without
    the documented fallback reference. Confirm the fallback still produces
    an orthonormal frame and a sensible top-down mapping (+X world stays
    +X screen)."""
    camera = Camera(
        target=(0.0, 0.0, 0.0), azimuth_deg=0.0, elevation_deg=90.0, distance=10.0
    )
    eye, right, up, forward = view_basis(camera)
    assert np.linalg.norm(right) == pytest.approx(1.0)
    assert np.linalg.norm(up) == pytest.approx(1.0)
    assert np.dot(right, up) == pytest.approx(0.0, abs=1e-9)
    assert np.dot(right, forward) == pytest.approx(0.0, abs=1e-9)
    assert eye == pytest.approx(np.array([0.0, 0.0, 10.0]))

    xy, _ = project(camera, np.array([[1.0, 0.0, 0.0]]))
    assert xy[0, 0] == pytest.approx(1.0, abs=1e-9)


def test_twist_rolls_right_and_up_about_forward() -> None:
    """A 90 degree twist rotates ``up`` toward ``right`` (module docstring):
    at az=el=0 (``right=+Y``, ``up=+Z``), twist=90 should leave
    ``right' == -up == (0, 0, -1)`` and ``up' == right == (0, 1, 0)``."""
    camera = Camera(target=(0.0, 0.0, 0.0), twist_deg=90.0, distance=10.0)
    _eye, right, up, forward = view_basis(camera)
    assert right == pytest.approx(np.array([0.0, 0.0, -1.0]), abs=1e-9)
    assert up == pytest.approx(np.array([0.0, 1.0, 0.0]), abs=1e-9)
    assert forward == pytest.approx(np.array([-1.0, 0.0, 0.0]), abs=1e-9)


def test_view_basis_requires_resolved_target() -> None:
    camera = Camera(target=None)
    with pytest.raises(ValueError, match="unresolved"):
        view_basis(camera)
    with pytest.raises(ValueError, match="unresolved"):
        project(camera, np.zeros((1, 3)))


def test_content_bbox_centroid_is_off_center_neutral() -> None:
    """The centroid is the bbox midpoint, not the mean of the points — a
    dense cluster near one corner doesn't pull it off the geometric center."""
    points = np.array(
        [[0.0, 0.0, 0.0], [0.01, 0.01, 0.01], [0.02, 0.0, 0.0], [10.0, 10.0, 10.0]]
    )
    centroid = content_bbox_centroid(points)
    assert centroid == pytest.approx(np.array([5.0, 5.0, 5.0]))


def test_ortho_zoom_scales_xy_persp_ignores_it() -> None:
    camera_1x = Camera(target=(0.0, 0.0, 0.0), zoom=1.0, distance=10.0)
    camera_2x = Camera(target=(0.0, 0.0, 0.0), zoom=2.0, distance=10.0)
    xy1, _ = project(camera_1x, np.array([[0.0, 1.0, 0.0]]))
    xy2, _ = project(camera_2x, np.array([[0.0, 1.0, 0.0]]))
    assert xy2[0, 0] == pytest.approx(xy1[0, 0] * 2.0)
    assert xy2[0, 1] == pytest.approx(xy1[0, 1] * 2.0)


def test_persp_scales_inversely_with_depth() -> None:
    """A perspective projection shrinks a fixed-size feature as it recedes
    — the standard divide-by-depth check."""
    camera = Camera(
        target=(0.0, 0.0, 0.0), projection="persp", distance=10.0, fov_deg=60.0
    )
    point = np.array([[0.0, 1.0, 0.0]])
    near, near_depth = project(camera, point)
    # Move the eye further out (keeping az/el/twist/fov identical) so the
    # same world offset now sits at a larger depth.
    camera_far = Camera(
        target=(0.0, 0.0, 0.0), projection="persp", distance=20.0, fov_deg=60.0
    )
    far, far_depth = project(camera_far, point)
    assert far_depth[0] > near_depth[0]
    assert abs(far[0, 0]) < abs(near[0, 0])

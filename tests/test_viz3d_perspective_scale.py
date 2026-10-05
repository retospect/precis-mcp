"""Perspective figures keep pixels per unit at target depth and relative depth."""

import re

import pytest

from precis.viz3d.camera import Camera
from precis.viz3d.primitives import Ball, Scene3
from precis.viz3d.render import Style, render_svg


@pytest.mark.parametrize("refine", [0, 1, 2])
@pytest.mark.parametrize("distance,fov", [(16.0, 30.0), (160.0, 60.0)])
def test_focus_plane_pixels(refine: int, distance: float, fov: float) -> None:
    scene = Scene3(
        primitives=[Ball(center=(0.0, 2.0, 0.0), radius=0.5, color="#404040")],
        unit_label="Å",
    )
    camera = Camera(
        target=(0.0, 0.0, 0.0), projection="persp", distance=distance, fov_deg=fov
    )
    svg = render_svg(
        scene,
        camera,
        refine=refine,
        style=Style(px_per_unit=28, halo=False, fog=False, scalebar=False),
    )
    circles = re.findall(r'<circle cx="([^"]+)" cy="([^"]+)" r="([^"]+)"', svg)
    assert circles
    cx, cy, radius = map(float, circles[0])
    assert abs(cx) == pytest.approx(56)
    assert cy == pytest.approx(0)
    assert radius == pytest.approx(14)


def test_depth_and_qualified_scale() -> None:
    scene = Scene3(
        primitives=[
            Ball(center=(5.0, 2.0, 0.0), radius=0.5, color="#404040"),
            Ball(center=(-10.0, 2.0, 0.0), radius=0.5, color="#808080"),
        ],
        unit_label="nm",
    )
    camera = Camera(target=(0.0, 0.0, 0.0), projection="persp", distance=10.0)
    svg = render_svg(
        scene, camera, refine=0, style=Style(px_per_unit=20, scalebar={"length": 2.0})
    )
    circles = re.findall(r'<circle cx="([^"]+)" cy="([^"]+)" r="([^"]+)"', svg)
    near, far = [tuple(map(float, c)) for c in circles]
    assert abs(near[0]) == pytest.approx(80)
    assert near[2] == pytest.approx(20)
    assert abs(far[0]) == pytest.approx(20)
    assert far[2] == pytest.approx(5)
    assert "2 nm (at target depth)" in svg
    bar = re.search(r'<line x1="([^"]+)" y1="[^"]+" x2="([^"]+)"', svg)
    assert bar is not None
    assert float(bar.group(2)) - float(bar.group(1)) == pytest.approx(40)

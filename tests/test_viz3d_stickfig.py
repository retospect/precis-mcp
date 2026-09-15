"""``stick_scene``/``render_svg`` composed on a tiny methane-ish molecule."""

from __future__ import annotations

import re

import numpy as np
import pytest

from precis.structure.elements import covalent_radius_A
from precis.viz3d.camera import Camera
from precis.viz3d.primitives import Ball, Stick
from precis.viz3d.render import Style as RenderStyle
from precis.viz3d.render import render_svg
from precis.viz3d.stickfig import Style, stick_scene

_ELEMENTS = ["C", "H", "H", "H", "H"]
_COORDS = np.array(
    [
        [0.0, 0.0, 0.0],
        [0.63, 0.63, 0.63],
        [-0.63, -0.63, 0.63],
        [-0.63, 0.63, -0.63],
        [0.63, -0.63, -0.63],
    ]
)
_BONDS = [(0, 1), (0, 2), (0, 3), (0, 4)]


def test_stick_scene_methane_has_5_balls_4_bonds() -> None:
    scene = stick_scene(_ELEMENTS, _COORDS, _BONDS, style=Style())
    balls = [p for p in scene.primitives if isinstance(p, Ball)]
    sticks = [p for p in scene.primitives if isinstance(p, Stick)]
    assert len(balls) == 5
    assert len(sticks) == 4
    assert scene.unit_label == "Å"


def test_stick_scene_uses_cpk_colors_and_shared_covalent_radii() -> None:
    scene = stick_scene(_ELEMENTS, _COORDS, _BONDS, style=Style())
    balls = [p for p in scene.primitives if isinstance(p, Ball)]
    carbon = balls[0]
    hydrogens = balls[1:]
    assert carbon.color == "#909090"  # CPK carbon (Jmol standard)
    assert all(h.color == "#ffffff" for h in hydrogens)  # CPK hydrogen
    # ball radius is covalent_radius_A(element) * ball_scale — the SAME
    # table precis.structure uses for bond detection, not a second one.
    style = Style(ball_scale=0.25)
    assert carbon.radius == pytest.approx(covalent_radius_A("C") * 0.25)
    assert hydrogens[0].radius == pytest.approx(covalent_radius_A("H") * 0.25)


def test_stick_scene_split_bond_colors_at_each_end() -> None:
    scene = stick_scene(_ELEMENTS, _COORDS, _BONDS, style=Style())
    sticks = [p for p in scene.primitives if isinstance(p, Stick)]
    for stick in sticks:
        assert stick.color_a == "#909090"  # carbon end
        assert stick.color_b == "#ffffff"  # hydrogen end


def test_stick_scene_methane_renders_8_half_sticks_at_r1() -> None:
    scene = stick_scene(_ELEMENTS, _COORDS, _BONDS, style=Style())
    camera = Camera(azimuth_deg=20.0, elevation_deg=10.0)
    svg = render_svg(
        scene, camera, refine=1, style=RenderStyle(halo=False, scalebar=False)
    )
    assert svg.count("<line") == 2 * len(_BONDS)
    assert svg.count("<circle") == len(_ELEMENTS)


def test_stick_scene_mono_color_ignores_element() -> None:
    scene = stick_scene(_ELEMENTS, _COORDS, _BONDS, style=Style(color="mono"))
    balls = [p for p in scene.primitives if isinstance(p, Ball)]
    assert {b.color for b in balls} == {"#606060"}


def test_stick_scene_color_by_overrides_present_atoms_only() -> None:
    color_by = {0: 0.0, 2: 1.0}  # carbon (idx 0) low, one hydrogen (idx 2) high
    scene = stick_scene(_ELEMENTS, _COORDS, _BONDS, style=Style(color_by=color_by))
    balls = [p for p in scene.primitives if isinstance(p, Ball)]
    assert balls[0].color != "#909090"  # carbon recolored by the map
    assert balls[2].color != "#ffffff"  # this hydrogen recolored
    # atoms with no entry keep their normal CPK color — never invented.
    assert balls[1].color == "#ffffff"
    assert balls[3].color == "#ffffff"
    assert balls[4].color == "#ffffff"


def test_stick_scene_rejects_coords_length_mismatch() -> None:
    with pytest.raises(ValueError, match="coords"):
        stick_scene(_ELEMENTS, _COORDS[:3], _BONDS, style=Style())


def test_offcenter_molecule_auto_targets_bbox_centroid_and_centers() -> None:
    """Translate the whole molecule far from the origin; with no explicit
    camera.target the render pipeline should still frame it centered (bbox
    centroid auto-target — the camera module's contract).

    Uses an axis-aligned camera (az=el=0) so the tetrahedral hydrogen
    positions project to a screen-space-symmetric square around the
    target — an oblique camera would still auto-target correctly (the
    target itself always projects to (0, 0) — see the camera module's
    tests) but the overall PADDED viewBox of an asymmetric point cloud
    isn't generally screen-symmetric under an oblique projection, which
    isn't what this test is checking.
    """
    offset = np.array([50.0, -30.0, 12.0])
    coords = _COORDS + offset
    scene = stick_scene(_ELEMENTS, coords, _BONDS, style=Style())
    camera = Camera(azimuth_deg=0.0, elevation_deg=0.0)  # target left unset
    svg = render_svg(scene, camera, refine=0, style=RenderStyle(scalebar=False))

    m = re.search(r'viewBox="([\d.eE+-]+) ([\d.eE+-]+) ([\d.eE+-]+) ([\d.eE+-]+)"', svg)
    assert m is not None
    minx, miny, w, h = (float(g) for g in m.groups())
    # The viewBox is centered near (0, 0) regardless of the huge world-space
    # offset — proof the camera actually auto-targeted the moved content.
    # 4-decimal coordinate formatting (render.py's determinism contract)
    # introduces a little rounding noise on top of exact centering.
    assert abs(minx + w / 2.0) < 1e-3
    assert abs(miny + h / 2.0) < 1e-3

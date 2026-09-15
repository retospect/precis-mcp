"""``render_svg``'s pipeline: depth sort, midpoint split, scalebar math,
determinism, and the r0/r1/r2 refine ladder."""

from __future__ import annotations

import math
import re

import pytest

from precis.viz3d.camera import Camera
from precis.viz3d.primitives import Ball, Scene3, Stick
from precis.viz3d.render import _HALO_PAD_PX, Style, pick_scalebar_length, render_svg


def _line_starts(svg: str) -> list[int]:
    return [m.start() for m in re.finditer(r"<line\b", svg)]


def _circle_starts(svg: str) -> list[int]:
    return [m.start() for m in re.finditer(r"<circle\b", svg)]


def test_r0_has_no_depth_sort_and_preserves_scene_order() -> None:
    """r0 draws in SCENE order even when a later primitive is the NEARER
    one — no painter's-algorithm sort happens at r0."""
    near = Ball(
        center=(0.0, 0.0, 5.0), radius=0.5, color="#111111"
    )  # closer to a +z-ish eye
    far = Ball(center=(0.0, 0.0, -5.0), radius=0.5, color="#eeeeee")
    scene = Scene3(
        primitives=[far, near], unit_label="Å"
    )  # far listed FIRST in the scene
    camera = Camera(azimuth_deg=0.0, elevation_deg=0.0, distance=20.0)
    svg = render_svg(scene, camera, refine=0, style=Style(scalebar=False))
    starts = _circle_starts(svg)
    assert len(starts) == 2
    # scene order preserved: the far ball's fill still comes first in the markup
    assert svg.index("#eeeeee") < svg.index("#111111")


def test_r1_depth_sorts_back_to_front() -> None:
    """A stick behind another emits EARLIER in the SVG (painter's algorithm:
    far first, near drawn on top). At az=el=0 the view axis is world X (the
    camera module's convention — the eye sits on +X, looking -X), so a
    LARGER x is CLOSER to the eye."""
    near = Stick(
        a=(5.0, -1.0, 0.0),
        b=(5.0, 1.0, 0.0),
        radius=0.2,
        color_a="#111111",
        color_b="#111111",
    )
    far = Stick(
        a=(-5.0, -1.0, 0.0),
        b=(-5.0, 1.0, 0.0),
        radius=0.2,
        color_a="#eeeeee",
        color_b="#eeeeee",
    )
    # Scene lists the NEAR stick first — the sort must still put it last.
    scene = Scene3(primitives=[near, far], unit_label="Å")
    camera = Camera(azimuth_deg=0.0, elevation_deg=0.0, distance=20.0)
    svg = render_svg(scene, camera, refine=1, style=Style(scalebar=False, halo=False))
    assert svg.index("#eeeeee") < svg.index("#111111")


def test_midpoint_split_doubles_stick_count_at_r1_plus() -> None:
    bonds = [
        Stick(
            a=(0.0, 0.0, 0.0),
            b=(1.0, 0.0, 0.0),
            radius=0.1,
            color_a="#111111",
            color_b="#222222",
        ),
        Stick(
            a=(0.0, 1.0, 0.0),
            b=(1.0, 1.0, 0.0),
            radius=0.1,
            color_a="#333333",
            color_b="#444444",
        ),
        Stick(
            a=(0.0, 2.0, 0.0),
            b=(1.0, 2.0, 0.0),
            radius=0.1,
            color_a="#555555",
            color_b="#666666",
        ),
    ]
    scene = Scene3(primitives=list(bonds), unit_label="Å")
    camera = Camera(azimuth_deg=25.0, elevation_deg=10.0, distance=20.0)
    style = Style(scalebar=False, halo=False)  # no halo => one <line> per half-stick

    svg_r0 = render_svg(scene, camera, refine=0, style=style)
    assert len(_line_starts(svg_r0)) == len(bonds)  # unsplit at r0

    for refine in (1, 2):
        svg = render_svg(scene, camera, refine=refine, style=style)
        assert len(_line_starts(svg)) == 2 * len(bonds), f"refine={refine}"


def test_halo_shortened_only_at_the_seam_not_the_outer_end() -> None:
    """Each half's halo must stop bulging exactly where the fill's own
    (intentional) seam overlap already ends, not `_HALO_PAD_PX` further —
    that extra bulge used to gouge a pad-wide white notch out of the
    sibling half's fill (two dashes with a white gash between them,
    instead of one continuous stick). The genuine silhouette (non-seam)
    end of each half keeps its full round-capped halo bulge, untouched."""
    stick = Stick(
        a=(5.0, -2.0, 0.0),
        b=(5.0, 2.0, 0.0),
        radius=0.2,
        color_a="#111111",
        color_b="#eeeeee",
    )
    scene = Scene3(primitives=[stick], unit_label="Å")
    camera = Camera(azimuth_deg=0.0, elevation_deg=0.0, distance=20.0)
    svg = render_svg(scene, camera, refine=1, style=Style(scalebar=False, halo=True))

    line_re = re.compile(
        r'<line x1="([\d.eE+-]+)" y1="([\d.eE+-]+)" x2="([\d.eE+-]+)" y2="([\d.eE+-]+)"'
    )
    lines = [tuple(float(g) for g in m.groups()) for m in line_re.finditer(svg)]
    # Emission order per half is halo-then-fill; the two halves are at equal
    # depth (the split stick sits perpendicular to the view axis here) so the
    # stable sort keeps scene order: half_a (seam at its "b"/midpoint end)
    # then half_b (seam at its "a"/midpoint end).
    assert len(lines) == 4
    halo_a, fill_a, halo_b, fill_b = lines

    # Half A: "a" (index 0-1) is the real silhouette end -- untouched.
    # "b" (index 2-3) is the midpoint seam -- shortened toward "a".
    assert halo_a[0:2] == pytest.approx(fill_a[0:2])
    ax, ay, bx, by = fill_a
    length = math.hypot(bx - ax, by - ay)
    ux, uy = (bx - ax) / length, (by - ay) / length
    assert halo_a[2] == pytest.approx(bx - ux * _HALO_PAD_PX)
    assert halo_a[3] == pytest.approx(by - uy * _HALO_PAD_PX)

    # Half B: "b" is the real silhouette end -- untouched. "a" is the
    # midpoint seam -- shortened toward "b".
    assert halo_b[2:4] == pytest.approx(fill_b[2:4])
    ax, ay, bx, by = fill_b
    length = math.hypot(bx - ax, by - ay)
    ux, uy = (bx - ax) / length, (by - ay) / length
    assert halo_b[0] == pytest.approx(ax + ux * _HALO_PAD_PX)
    assert halo_b[1] == pytest.approx(ay + uy * _HALO_PAD_PX)


def test_refine_ladder_gradients_only_from_r2() -> None:
    scene = Scene3(
        primitives=[
            Ball(center=(0.0, 0.0, 0.0), radius=0.3, color="#404040"),
            Stick(
                a=(0.0, 0.0, 0.0),
                b=(1.0, 0.0, 0.0),
                radius=0.1,
                color_a="#404040",
                color_b="#e6e6e6",
            ),
        ],
        unit_label="Å",
    )
    camera = Camera(azimuth_deg=25.0, elevation_deg=15.0)
    style = Style(scalebar=False)
    svg0 = render_svg(scene, camera, refine=0, style=style)
    svg1 = render_svg(scene, camera, refine=1, style=style)
    svg2 = render_svg(scene, camera, refine=2, style=style)
    assert "Gradient" not in svg0
    assert "Gradient" not in svg1
    assert "linearGradient" in svg2
    assert "radialGradient" in svg2
    # r0 draws exactly one primary shape per scene primitive (no halo dup)
    assert len(_circle_starts(svg0)) == 1
    assert len(_line_starts(svg0)) == 1


def test_render_svg_is_deterministic() -> None:
    scene = Scene3(
        primitives=[
            Ball(center=(0.3, -0.2, 0.7), radius=0.4, color="#404040"),
            Ball(center=(-1.1, 0.9, -0.4), radius=0.3, color="#e6e6e6"),
            Stick(
                a=(0.3, -0.2, 0.7),
                b=(-1.1, 0.9, -0.4),
                radius=0.12,
                color_a="#404040",
                color_b="#e6e6e6",
            ),
        ],
        unit_label="Å",
    )
    camera = Camera(azimuth_deg=37.0, elevation_deg=-12.0, twist_deg=8.0)
    for refine in (0, 1, 2):
        a = render_svg(scene, camera, refine=refine)
        b = render_svg(scene, camera, refine=refine)
        assert a == b, f"refine={refine} not byte-identical across runs"


def test_pick_scalebar_length_is_a_nice_round_value() -> None:
    assert pick_scalebar_length(4.0) == 1.0  # target=1.0 exactly -> 1
    assert pick_scalebar_length(8.0) == 2.0  # target=2.0 exactly -> 2
    assert pick_scalebar_length(20.0) == 5.0  # target=5.0 exactly -> 5
    assert pick_scalebar_length(0.0) == 0.0
    assert pick_scalebar_length(-3.0) == 0.0


def test_scalebar_pixel_length_matches_the_ortho_scale_exactly() -> None:
    """Two balls straddling the Y axis at az=el=0 (so world-Y maps 1:1 onto
    camera-space x — see the camera module's convention) with radius=1.0
    (large enough that render.py's px-floor never triggers) give an exactly
    computable viewBox, hence an exactly computable expected scalebar."""
    scene = Scene3(
        primitives=[
            Ball(center=(0.0, -2.0, 0.0), radius=1.0, color="#404040"),
            Ball(center=(0.0, 2.0, 0.0), radius=1.0, color="#e6e6e6"),
        ],
        unit_label="Å",
    )
    camera = Camera(azimuth_deg=0.0, elevation_deg=0.0, zoom=1.0, distance=10.0)
    style = Style(margin=0.0, px_per_unit=40.0, halo=False, fog=False, scalebar="auto")
    svg = render_svg(scene, camera, refine=0, style=style)

    m = re.search(r'viewBox="([\d.eE+-]+) ([\d.eE+-]+) ([\d.eE+-]+) ([\d.eE+-]+)"', svg)
    assert m is not None
    _minx, _miny, width, _height = (float(g) for g in m.groups())
    assert width == 240.0  # (radius 1 + |y| 2) * 2 sides * px_per_unit 40, no margin

    expected_value = pick_scalebar_length(width / (camera.zoom * style.px_per_unit))
    assert expected_value == 2.0
    expected_bar_px = expected_value * camera.zoom * style.px_per_unit

    line = re.search(
        r'<g stroke="#000000" fill="#000000"><line x1="([\d.eE+-]+)"[^>]*x2="([\d.eE+-]+)"',
        svg,
    )
    assert line is not None
    x0, x1 = float(line.group(1)), float(line.group(2))
    assert (x1 - x0) == expected_bar_px
    assert f">{2:g} Å<" in svg


def test_scalebar_omitted_under_perspective() -> None:
    scene = Scene3(
        primitives=[Ball(center=(0.0, 0.0, 0.0), radius=0.5, color="#404040")],
        unit_label="Å",
    )
    camera = Camera(target=(0.0, 0.0, 0.0), projection="persp")
    svg = render_svg(scene, camera, refine=1)
    assert 'stroke="#000000" fill="#000000"' not in svg


def test_scalebar_explicit_length_and_false() -> None:
    scene = Scene3(
        primitives=[Ball(center=(0.0, 0.0, 0.0), radius=0.5, color="#404040")],
        unit_label="Å",
    )
    camera = Camera(target=(0.0, 0.0, 0.0), azimuth_deg=0.0, elevation_deg=0.0)

    svg_explicit = render_svg(
        scene, camera, refine=0, style=Style(scalebar={"length": 3.0})
    )
    assert ">3 Å<" in svg_explicit

    svg_off = render_svg(scene, camera, refine=0, style=Style(scalebar=False))
    assert " Å<" not in svg_off


def test_invalid_refine_raises() -> None:
    scene = Scene3(primitives=[], unit_label="Å")
    camera = Camera(target=(0.0, 0.0, 0.0))
    with pytest.raises(ValueError, match="refine"):
        render_svg(scene, camera, refine=3)

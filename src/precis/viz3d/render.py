"""``Scene3`` + ``Camera`` → SVG. The r0/r1/r2 refine ladder, painter's
algorithm, gradient shading, and the ortho scalebar all live here — see the
package docstring's refine contract (quality only, never geometry).

**Refine ladder**:

- ``r0`` — orthographic wireframe. Every primitive drawn once, in SCENE
  order (no split, no depth sort, no halo, no gradient) — the fast
  thumbnail path.
- ``r1`` — painter's algorithm. Every :class:`~precis.viz3d.primitives.Stick`
  is split at its midpoint into two flat-colored half-sticks (so two
  crossing bonds occlude correctly half-by-half), every primitive is
  depth-sorted back-to-front, sticks get real projected stroke width with
  round caps, and (if ``style.halo``) a white halo stroke/circle is painted
  underneath each shape.
- ``r2`` — ``r1`` plus shading: each half-stick gets a
  ``linearGradient`` perpendicular to its screen-space axis faking cylinder
  lighting from ``style.lighting``, each ball gets a ``radialGradient``,
  and (if ``style.fog``) color blends toward a fog tone with depth.
  ``style.lighting.ambient`` is the shading floor — no facet ever renders
  darker than ``ambient`` of its base color.

**Determinism.** Every coordinate is formatted through :func:`_fmt`
(fixed 4-decimal, ``-0.0`` normalized to ``0.0``); draw order is a Python
stable sort on depth, so primitives at equal depth keep their original
scene-list position — same inputs always produce a byte-identical string,
with no reliance on dict/set iteration order or object identity anywhere
in the pipeline.

**Scalebar.** Ortho only: perspective scale is depth-dependent (a bar drawn
at one depth doesn't represent the same real length at another), so there
is no correct single bar to draw — ``style.scalebar`` is silently ignored
under ``projection="persp"`` rather than drawing a misleading one.
:func:`pick_scalebar_length` is exposed standalone (pure function of a
world-unit span) so a caller — or a test — can compute the expected auto
value directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from precis.viz3d.camera import Camera, content_bbox_centroid, project, view_basis
from precis.viz3d.primitives import (
    Ball,
    Label,
    Point3,
    Polyline,
    Primitive,
    Scene3,
    Stick,
)

__all__ = [
    "Lighting",
    "Style",
    "pick_scalebar_length",
    "render_svg",
]

#: ``style.scalebar`` — ``"auto"`` picks a round value via
#: :func:`pick_scalebar_length`, a ``{"length": ...}`` dict pins an exact
#: value, ``False`` omits the bar even under ``ortho``.
ScalebarSpec = Literal["auto"] | dict[str, float] | Literal[False]

#: Pixels added on each side of a halo stroke/circle beyond the shape's own
#: projected radius — a fixed device-pixel constant, independent of zoom.
_HALO_PAD_PX = 1.5

#: Depth-fog blend strength at the far end of the scene's own depth range
#: (``t=1``) — ``0`` = no visible fog, ``1`` = fully faded to ``_FOG_COLOR``.
_FOG_STRENGTH = 0.6
_FOG_COLOR = "#e8e8ec"

#: Fixed pixel allowance around a text :class:`~precis.viz3d.primitives.Label`
#: anchor for the viewBox fit — this module never measures glyph metrics
#: (no font backend), so a label's true rendered width can exceed this and
#: clip at the frame edge; acceptable for the stick figures this slice
#: targets (short element/atom-index labels).
_LABEL_PAD_PX = 24.0

#: {1, 2, 5} x 10^k — the classic "nice" scalebar multiplier set.
_NICE_MULTIPLIERS = (1.0, 2.0, 5.0)


@dataclass(frozen=True)
class Lighting:
    """The r2 key light.

    ``key_dir``'s first two components are the light direction in this
    module's own coordinate convention: **screen space, y-down** (matching
    every other 2D quantity here, including the projected geometry itself)
    — so the default ``(-1.0, -1.0, 2.0)`` reads as "upper-left", the
    documented default key placement. ``frame="camera"`` (default) uses
    ``(key_dir[0], key_dir[1])`` directly — a light that stays fixed on the
    frame under orbit. ``frame="world"`` instead treats the full 3-vector as
    a world-space direction, projected into the active camera's
    (``right``, ``up``) basis (then y-flipped to match screen convention) —
    a light that orbits WITH the content instead of the camera. The third
    component only matters for ``frame="world"``.
    """

    key_dir: tuple[float, float, float] = (-1.0, -1.0, 2.0)
    ambient: float = 0.35
    frame: Literal["camera", "world"] = "camera"


@dataclass(frozen=True)
class Style:
    """Render-quality knobs this module consumes directly.

    Contrast :mod:`.stickfig`'s own ``Style`` (stick radius, ball size,
    CPK vs. mono) — that one builds SCENE geometry one layer up; by the
    time a :class:`~precis.viz3d.primitives.Scene3` reaches this module,
    every primitive already carries its own concrete radius/color.
    """

    margin: float = 0.15
    px_per_unit: float = 40.0
    halo: bool = True
    fog: bool = False
    lighting: Lighting = field(default_factory=Lighting)
    scalebar: ScalebarSpec = "auto"
    background: str | None = None


def pick_scalebar_length(span: float, target_fraction: float = 0.25) -> float:
    """The round ``{1, 2, 5} x 10^k`` value nearest ``span * target_fraction``
    (nearest in log-scale; an exact tie prefers the smaller candidate, so
    the result is a pure, order-independent function of the inputs).

    Returns ``0.0`` for a non-positive span/target (degenerate scene).
    """
    target = span * target_fraction
    if target <= 0.0:
        return 0.0
    exp = math.floor(math.log10(target))
    candidates = [
        m * (10.0**k) for k in (exp - 1, exp, exp + 1) for m in _NICE_MULTIPLIERS
    ]
    log_target = math.log10(target)

    def _score(c: float) -> tuple[float, float]:
        return (abs(math.log10(c) - log_target), c)

    return min(candidates, key=_score)


def _fmt(x: float) -> str:
    """Fixed 4-decimal formatting; ``-0.0`` normalizes to ``0.0`` (Python's
    ``f"{-0.0:.4f}"`` is the string ``"-0.0000"``, which would otherwise make
    two geometrically-identical renders differ byte-for-byte on sign)."""
    if x == 0.0:
        x = 0.0
    return f"{x:.4f}"


def _format_number(value: float) -> str:
    """A clean, non-scientific label for a scalebar value — every value this
    module ever picks is ``m x 10^k`` with ``m`` in ``{1, 2, 5}``, which
    ``%.10g`` always renders without an exponent at any figure-realistic
    magnitude."""
    return f"{value:.10g}"


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _shade(hex_color: str, factor: float) -> str:
    """Scale RGB channels by ``factor`` (clamped to a valid byte on output)
    — ``factor < 1`` darkens toward black, ``factor > 1`` brightens toward
    (clipped) white."""
    r, g, b = _hex_to_rgb(hex_color)
    return _rgb_to_hex((r * factor, g * factor, b * factor))


def _apply_fog(hex_color: str, t: float) -> str:
    """Blend ``hex_color`` toward :data:`_FOG_COLOR` by ``t`` in ``[0, 1]``."""
    r, g, b = _hex_to_rgb(hex_color)
    fr, fg, fb = _hex_to_rgb(_FOG_COLOR)
    t = max(0.0, min(1.0, t))
    return _rgb_to_hex((r + (fr - r) * t, g + (fg - g) * t, b + (fb - b) * t))


def _norm_depth(d: float, dmin: float, dmax: float) -> float:
    span = dmax - dmin
    if span < 1e-9:
        return 0.0
    return (d - dmin) / span


def _brightness(u: float, t: float, ambient: float) -> float:
    """Lambertian-ish falloff: ``u`` is the sample's position across the
    perpendicular gradient (``-1``..``1``), ``t`` is how aligned the light
    is with the ``+u`` direction (``-1``..``1``). ``ambient`` floors the
    result — no facet renders below it."""
    lit = max(0.0, min(1.0, (1.0 + u * t) / 2.0))
    return ambient + (1.0 - ambient) * lit


def _light_dir_2d(
    lighting: Lighting,
    right_up: tuple[NDArray[np.float64], NDArray[np.float64]] | None,
) -> tuple[float, float]:
    """The light direction as a unit ``(x, y)`` in this module's screen
    convention (y-down) — see :class:`Lighting`."""
    if lighting.frame == "world" and right_up is not None:
        right, up = right_up
        kd = np.asarray(lighting.key_dir, dtype=np.float64)
        x = float(np.dot(kd, right))
        y = -float(np.dot(kd, up))  # math-up camera basis -> screen y-down
    else:
        x, y = float(lighting.key_dir[0]), float(lighting.key_dir[1])
    norm = math.hypot(x, y)
    if norm < 1e-9:
        return 0.0, -1.0  # default: light from directly above
    return x / norm, y / norm


def _resolve_camera(camera: Camera, scene: Scene3) -> Camera:
    """Fill an unset :attr:`Camera.target` with the scene's own bbox
    centroid — the "pipeline resolves it" step promised by :mod:`.camera`."""
    if camera.target is not None:
        return camera
    verts = _all_vertices(scene)
    centroid = (
        content_bbox_centroid(verts)
        if verts.shape[0]
        else np.zeros(3, dtype=np.float64)
    )
    return replace(
        camera, target=(float(centroid[0]), float(centroid[1]), float(centroid[2]))
    )


def _all_vertices(scene: Scene3) -> NDArray[np.float64]:
    pts: list[Point3] = []
    for prim in scene.primitives:
        if isinstance(prim, Ball):
            pts.append(prim.center)
        elif isinstance(prim, Stick):
            pts.append(prim.a)
            pts.append(prim.b)
        elif isinstance(prim, Polyline):
            pts.extend(prim.points)
        elif isinstance(prim, Label):
            pts.append(prim.anchor3d)
    if not pts:
        return np.zeros((0, 3), dtype=np.float64)
    return np.asarray(pts, dtype=np.float64)


@dataclass(frozen=True)
class _Half:
    """One half of a split :class:`Stick` (or, degenerately, an unsplit
    stick that never went through :func:`_split_stick`), plus which of its
    own endpoints — if any — is the midpoint seam abutting its sibling
    half. ``seam_end`` tells :func:`_emit_stick` which end's halo must NOT
    bulge past the seam into the sibling's territory (a real silhouette
    end still gets the full round-capped halo bulge); ``None`` means both
    ends are genuine silhouette (the unsplit-stick case)."""

    stick: Stick
    seam_end: Literal["a", "b"] | None


def _split_stick(stick: Stick) -> tuple[_Half, _Half]:
    """Split ``stick`` at its midpoint into two flat-colored half-sticks —
    the geometric basis of the split-bond occlusion convention (see the
    module docstring). Each half records which of ITS OWN endpoints sits
    at that shared midpoint (see :class:`_Half`)."""
    mid = (
        (stick.a[0] + stick.b[0]) / 2.0,
        (stick.a[1] + stick.b[1]) / 2.0,
        (stick.a[2] + stick.b[2]) / 2.0,
    )
    half_a = _Half(
        Stick(stick.a, mid, stick.radius, stick.color_a, stick.color_a), seam_end="b"
    )
    half_b = _Half(
        Stick(mid, stick.b, stick.radius, stick.color_b, stick.color_b), seam_end="a"
    )
    return half_a, half_b


def _project_one(camera: Camera, style: Style, p: Point3) -> tuple[float, float, float]:
    """Project one world point to ``(screen_x, screen_y, depth)`` — the SVG
    pixel position (y flipped for SVG's y-down axis, scaled by
    ``style.px_per_unit``) plus its camera-space depth."""
    xy, depth = project(camera, np.asarray([p], dtype=np.float64))
    sx = float(xy[0, 0]) * style.px_per_unit
    sy = -float(xy[0, 1]) * style.px_per_unit
    return sx, sy, float(depth[0])


def _projected_radius(
    camera: Camera, style: Style, world_radius: float, depth: float
) -> float:
    if camera.projection == "ortho":
        return world_radius * camera.zoom * style.px_per_unit
    f = 1.0 / math.tan(math.radians(camera.fov_deg) / 2.0)
    d = depth if depth != 0.0 else 1e-9
    return world_radius * f / d * style.px_per_unit


# --------------------------------------------------------------------- r0 --


def _build_r0(
    scene: Scene3, camera: Camera, style: Style
) -> tuple[list[str], list[str], list[tuple[float, float, float]]]:
    """Orthographic wireframe: SCENE order, no split, no sort, no shading."""
    fragments: list[str] = []
    bbox_pts: list[tuple[float, float, float]] = []
    for prim in scene.primitives:
        if isinstance(prim, Ball):
            sx, sy, d = _project_one(camera, style, prim.center)
            r = max(_projected_radius(camera, style, prim.radius, d), 0.5)
            fragments.append(
                f'<circle cx="{_fmt(sx)}" cy="{_fmt(sy)}" r="{_fmt(r)}" fill="{prim.color}"/>'
            )
            bbox_pts.append((sx, sy, r))
        elif isinstance(prim, Stick):
            ax, ay, ad = _project_one(camera, style, prim.a)
            bx, by, bd = _project_one(camera, style, prim.b)
            r = max(
                _projected_radius(camera, style, prim.radius, (ad + bd) / 2.0), 0.25
            )
            fragments.append(
                f'<line x1="{_fmt(ax)}" y1="{_fmt(ay)}" x2="{_fmt(bx)}" y2="{_fmt(by)}" '
                f'stroke="{prim.color_a}" stroke-width="{_fmt(max(r * 0.4, 0.5))}"/>'
            )
            bbox_pts.append((ax, ay, r))
            bbox_pts.append((bx, by, r))
        elif isinstance(prim, Polyline):
            pts = [_project_one(camera, style, p) for p in prim.points]
            pts_str = " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y, _ in pts)
            fragments.append(
                f'<polyline points="{pts_str}" fill="none" stroke="{prim.color}" '
                f'stroke-width="{_fmt(prim.width)}"/>'
            )
            bbox_pts.extend((x, y, prim.width) for x, y, _ in pts)
        elif isinstance(prim, Label):
            sx, sy, _d = _project_one(camera, style, prim.anchor3d)
            fragments.append(
                f'<text x="{_fmt(sx)}" y="{_fmt(sy)}" font-size="10" fill="{prim.color}">'
                f"{_xml_escape(prim.text)}</text>"
            )
            bbox_pts.append((sx, sy, _LABEL_PAD_PX))
    return fragments, [], bbox_pts


# ------------------------------------------------------------------ r1/r2 --


@dataclass
class _Item:
    kind: Literal["ball", "stick", "polyline", "label"]
    prim: Primitive | _Half
    depth: float


def _gather_items(scene: Scene3, camera: Camera, style: Style) -> list[_Item]:
    items: list[_Item] = []
    for prim in scene.primitives:
        if isinstance(prim, Ball):
            _, _, d = _project_one(camera, style, prim.center)
            items.append(_Item("ball", prim, d))
        elif isinstance(prim, Stick):
            for half in _split_stick(prim):
                _, _, da = _project_one(camera, style, half.stick.a)
                _, _, db = _project_one(camera, style, half.stick.b)
                items.append(_Item("stick", half, (da + db) / 2.0))
        elif isinstance(prim, Polyline):
            ds = [_project_one(camera, style, p)[2] for p in prim.points]
            items.append(_Item("polyline", prim, (sum(ds) / len(ds)) if ds else 0.0))
        elif isinstance(prim, Label):
            _, _, d = _project_one(camera, style, prim.anchor3d)
            items.append(_Item("label", prim, d))
    return items


def _emit_ball(
    ball: Ball,
    camera: Camera,
    style: Style,
    refine: int,
    dmin: float,
    dmax: float,
    right_up: tuple[NDArray[np.float64], NDArray[np.float64]] | None,
    counter: int,
) -> tuple[str, str, list[tuple[float, float, float]]]:
    sx, sy, d = _project_one(camera, style, ball.center)
    r = max(_projected_radius(camera, style, ball.radius, d), 0.5)
    color = ball.color
    if style.fog:
        color = _apply_fog(color, _norm_depth(d, dmin, dmax) * _FOG_STRENGTH)

    frag: list[str] = []
    defs: list[str] = []
    pad = 0.0
    if style.halo:
        pad = _HALO_PAD_PX
        frag.append(
            f'<circle cx="{_fmt(sx)}" cy="{_fmt(sy)}" r="{_fmt(r + pad)}" fill="#ffffff"/>'
        )

    if refine == 1:
        frag.append(
            f'<circle cx="{_fmt(sx)}" cy="{_fmt(sy)}" r="{_fmt(r)}" fill="{color}"/>'
        )
    else:
        gid = f"rb{counter}"
        lx, ly = _light_dir_2d(style.lighting, right_up)
        fx = 50.0 + 28.0 * lx
        fy = 50.0 + 28.0 * ly
        bright = _shade(color, 1.15)
        dark = _shade(color, style.lighting.ambient)
        defs.append(
            f'<radialGradient id="{gid}" cx="50%" cy="50%" r="65%" '
            f'fx="{_fmt(fx)}%" fy="{_fmt(fy)}%">'
            f'<stop offset="0%" stop-color="{bright}"/>'
            f'<stop offset="100%" stop-color="{dark}"/>'
            f"</radialGradient>"
        )
        frag.append(
            f'<circle cx="{_fmt(sx)}" cy="{_fmt(sy)}" r="{_fmt(r)}" fill="url(#{gid})"/>'
        )

    return "".join(frag), "".join(defs), [(sx, sy, r + pad)]


def _emit_stick(
    half: _Half,
    camera: Camera,
    style: Style,
    refine: int,
    dmin: float,
    dmax: float,
    right_up: tuple[NDArray[np.float64], NDArray[np.float64]] | None,
    counter: int,
) -> tuple[str, str, list[tuple[float, float, float]]]:
    stick = half.stick
    ax, ay, ad = _project_one(camera, style, stick.a)
    bx, by, bd = _project_one(camera, style, stick.b)
    mean_d = (ad + bd) / 2.0
    r = max(_projected_radius(camera, style, stick.radius, mean_d), 0.5)
    width = r * 2.0
    color = stick.color_a  # a split half is single-color by construction
    if style.fog:
        color = _apply_fog(color, _norm_depth(mean_d, dmin, dmax) * _FOG_STRENGTH)

    frag: list[str] = []
    defs: list[str] = []
    if style.halo:
        # The halo's round cap bulges _HALO_PAD_PX beyond the fill's own
        # (intentional) overlap at a seam end — shorten that end of the
        # HALO line by _HALO_PAD_PX along the bond's projected direction so
        # its bulge stops where the fill's own overlap already ends,
        # instead of gouging a pad-wide white band into the sibling half's
        # already-drawn fill (the two-dashes-with-a-notch defect). A
        # genuine silhouette end (no seam, or the non-seam end of a split
        # half) keeps its full round-capped bulge untouched.
        hax, hay, hbx, hby = ax, ay, bx, by
        dx, dy = bx - ax, by - ay
        seam_len = math.hypot(dx, dy)
        if seam_len > 1e-9:
            ux, uy = dx / seam_len, dy / seam_len
            if half.seam_end == "a":
                hax, hay = ax + ux * _HALO_PAD_PX, ay + uy * _HALO_PAD_PX
            elif half.seam_end == "b":
                hbx, hby = bx - ux * _HALO_PAD_PX, by - uy * _HALO_PAD_PX
        frag.append(
            f'<line x1="{_fmt(hax)}" y1="{_fmt(hay)}" x2="{_fmt(hbx)}" y2="{_fmt(hby)}" '
            f'stroke="#ffffff" stroke-width="{_fmt(width + 2 * _HALO_PAD_PX)}" '
            f'stroke-linecap="round"/>'
        )

    if refine == 1:
        frag.append(
            f'<line x1="{_fmt(ax)}" y1="{_fmt(ay)}" x2="{_fmt(bx)}" y2="{_fmt(by)}" '
            f'stroke="{color}" stroke-width="{_fmt(width)}" stroke-linecap="round"/>'
        )
    else:
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        axis_x, axis_y = (dx / length, dy / length) if length > 1e-9 else (1.0, 0.0)
        perp_x, perp_y = -axis_y, axis_x
        lx, ly = _light_dir_2d(style.lighting, right_up)
        t = perp_x * lx + perp_y * ly
        mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
        p0x, p0y = mx - perp_x * r, my - perp_y * r
        p1x, p1y = mx + perp_x * r, my + perp_y * r
        ambient = style.lighting.ambient
        c0 = _shade(color, _brightness(-1.0, t, ambient))
        c50 = _shade(color, _brightness(0.0, t, ambient))
        c100 = _shade(color, _brightness(1.0, t, ambient))
        gid = f"sg{counter}"
        defs.append(
            f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" '
            f'x1="{_fmt(p0x)}" y1="{_fmt(p0y)}" x2="{_fmt(p1x)}" y2="{_fmt(p1y)}">'
            f'<stop offset="0%" stop-color="{c0}"/>'
            f'<stop offset="50%" stop-color="{c50}"/>'
            f'<stop offset="100%" stop-color="{c100}"/>'
            f"</linearGradient>"
        )
        frag.append(
            f'<line x1="{_fmt(ax)}" y1="{_fmt(ay)}" x2="{_fmt(bx)}" y2="{_fmt(by)}" '
            f'stroke="url(#{gid})" stroke-width="{_fmt(width)}" stroke-linecap="round"/>'
        )

    pad = width / 2.0 + (_HALO_PAD_PX if style.halo else 0.0)
    return "".join(frag), "".join(defs), [(ax, ay, pad), (bx, by, pad)]


def _emit_polyline(
    prim: Polyline, camera: Camera, style: Style
) -> tuple[str, str, list[tuple[float, float, float]]]:
    pts = [_project_one(camera, style, p) for p in prim.points]
    pts_str = " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y, _ in pts)
    frag = (
        f'<polyline points="{pts_str}" fill="none" stroke="{prim.color}" '
        f'stroke-width="{_fmt(prim.width)}" stroke-linecap="round" stroke-linejoin="round"/>'
    )
    bbox = [(x, y, prim.width) for x, y, _ in pts]
    return frag, "", bbox


def _emit_label(
    prim: Label, camera: Camera, style: Style
) -> tuple[str, str, list[tuple[float, float, float]]]:
    sx, sy, _d = _project_one(camera, style, prim.anchor3d)
    frag = (
        f'<text x="{_fmt(sx)}" y="{_fmt(sy)}" font-size="10" fill="{prim.color}">'
        f"{_xml_escape(prim.text)}</text>"
    )
    return frag, "", [(sx, sy, _LABEL_PAD_PX)]


def _build_r1_r2(
    scene: Scene3, camera: Camera, style: Style, refine: int
) -> tuple[list[str], list[str], list[tuple[float, float, float]]]:
    items = _gather_items(scene, camera, style)
    items.sort(key=lambda it: -it.depth)  # far-to-near, stable => deterministic ties

    depths = [it.depth for it in items]
    dmin = min(depths) if depths else 0.0
    dmax = max(depths) if depths else 0.0

    right_up: tuple[NDArray[np.float64], NDArray[np.float64]] | None = None
    if refine == 2 and style.lighting.frame == "world":
        _eye, right, up, _forward = view_basis(camera)
        right_up = (right, up)

    fragments: list[str] = []
    defs: list[str] = []
    bbox_pts: list[tuple[float, float, float]] = []
    for counter, item in enumerate(items, start=1):
        if item.kind == "ball":
            assert isinstance(item.prim, Ball)
            frag, d, bp = _emit_ball(
                item.prim, camera, style, refine, dmin, dmax, right_up, counter
            )
        elif item.kind == "stick":
            assert isinstance(item.prim, _Half)
            frag, d, bp = _emit_stick(
                item.prim, camera, style, refine, dmin, dmax, right_up, counter
            )
        elif item.kind == "polyline":
            assert isinstance(item.prim, Polyline)
            frag, d, bp = _emit_polyline(item.prim, camera, style)
        else:
            assert isinstance(item.prim, Label)
            frag, d, bp = _emit_label(item.prim, camera, style)
        fragments.append(frag)
        if d:
            defs.append(d)
        bbox_pts.extend(bp)
    return fragments, defs, bbox_pts


# ---------------------------------------------------------------- frame ----


def _viewbox(
    bbox_pts: list[tuple[float, float, float]], margin_frac: float
) -> tuple[float, float, float, float]:
    if not bbox_pts:
        return (-1.0, -1.0, 2.0, 2.0)
    minx = min(x - r for x, _y, r in bbox_pts)
    maxx = max(x + r for x, _y, r in bbox_pts)
    miny = min(y - r for _x, y, r in bbox_pts)
    maxy = max(y + r for _x, y, r in bbox_pts)
    w, h = maxx - minx, maxy - miny
    if w <= 0.0:
        minx -= 1.0
        w = 2.0
    if h <= 0.0:
        miny -= 1.0
        h = 2.0
    pad = margin_frac * max(w, h)
    return (minx - pad, miny - pad, w + 2 * pad, h + 2 * pad)


def _scalebar_fragment(
    scene: Scene3,
    camera: Camera,
    style: Style,
    viewbox: tuple[float, float, float, float],
) -> str | None:
    """Ortho only — see the module docstring for why ``persp`` never draws
    one. Bar length spans the world-unit value against the SAME
    ``zoom * px_per_unit`` scale factor used for every other pixel in the
    frame, so it's exact by construction, not a calibrated overlay."""
    if camera.projection != "ortho" or style.scalebar is False:
        return None
    scale = camera.zoom * style.px_per_unit
    if scale <= 0.0:
        return None
    minx, miny, w, h = viewbox
    if isinstance(style.scalebar, dict):
        value = float(style.scalebar["length"])
    elif scene.scale_hint is not None:
        value = float(scene.scale_hint)
    else:
        value = pick_scalebar_length(w / scale)
    if value <= 0.0:
        return None
    bar_px = value * scale
    x0 = minx + 0.06 * w
    y0 = miny + h - 0.06 * h
    x1 = x0 + bar_px
    label = f"{_format_number(value)} {scene.unit_label}".strip()
    return (
        '<g stroke="#000000" fill="#000000">'
        f'<line x1="{_fmt(x0)}" y1="{_fmt(y0)}" x2="{_fmt(x1)}" y2="{_fmt(y0)}" stroke-width="2"/>'
        f'<text x="{_fmt((x0 + x1) / 2.0)}" y="{_fmt(y0 - 6.0)}" font-size="10" '
        f'text-anchor="middle" stroke="none">{_xml_escape(label)}</text>'
        "</g>"
    )


def _assemble_svg(
    viewbox: tuple[float, float, float, float],
    style: Style,
    defs: list[str],
    fragments: list[str],
    scalebar_frag: str | None,
) -> str:
    minx, miny, w, h = viewbox
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_fmt(minx)} {_fmt(miny)} {_fmt(w)} {_fmt(h)}">'
    ]
    if defs:
        parts.append("<defs>" + "".join(defs) + "</defs>")
    if style.background:
        parts.append(
            f'<rect x="{_fmt(minx)}" y="{_fmt(miny)}" width="{_fmt(w)}" height="{_fmt(h)}" '
            f'fill="{style.background}"/>'
        )
    parts.append("<g>" + "".join(fragments) + "</g>")
    if scalebar_frag:
        parts.append(scalebar_frag)
    parts.append("</svg>")
    return "".join(parts)


def render_svg(
    scene: Scene3, camera: Camera, *, refine: int, style: Style | None = None
) -> str:
    """Render ``scene`` from ``camera`` at ``refine`` quality (0/1/2 in this
    slice — ``r3`` raytrace is reserved, not implemented) to an SVG string.

    ``camera.target`` is resolved to the scene's own bbox centroid when
    unset (see :func:`~precis.viz3d.camera.content_bbox_centroid`) — an
    off-origin structure frames correctly without the caller pre-computing
    anything.
    """
    if style is None:
        style = Style()
    if refine not in (0, 1, 2):
        raise ValueError(f"refine must be 0, 1, or 2 in this slice, got {refine!r}")

    camera = _resolve_camera(camera, scene)

    if refine == 0:
        fragments, defs, bbox_pts = _build_r0(scene, camera, style)
    else:
        fragments, defs, bbox_pts = _build_r1_r2(scene, camera, style, refine)

    viewbox = _viewbox(bbox_pts, style.margin)
    scalebar_frag = _scalebar_fragment(scene, camera, style, viewbox)
    return _assemble_svg(viewbox, style, defs, fragments, scalebar_frag)

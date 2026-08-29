"""Render a board FROM ITS GERBERS — a reader, and an SVG with a layer
selector.

**Why not render the model.** :mod:`precis.pcb.svg` draws the realizer's
own data structures, and a view that is stylistically different from the
artefact cannot verify the artefact. That module applies a per-layer
``stroke-dasharray`` as a colour-independent layer cue, so continuous B.Cu
copper renders as dashes — a session was then spent proving the copper was
continuous (it was) because the picture could not distinguish decoration
from a real gap. Rendering the gerbers removes the class of question: the
only thing between this picture and the fab's is the fab's own reader.

That is also what it is *for*. Gerber is where the board stops being an
in-memory model — the pad geometry, the polarity of a plane's antipads, the
aperture a trace was actually stroked with. Bugs that reach manufacturing
live in that translation, and nothing that reads the model can see them.

**What this reader handles**, which is the RS-274X subset
:mod:`precis.pcb.gerber` emits plus the neighbouring common cases:
``%FS`` (leading-zero-omitted absolute), ``%MO`` mm/inch, ``%ADD`` for C/R/O
apertures, aperture select ``Dnn``, ``G01/G02/G03`` with ``G75``
multi-quadrant arcs, ``D01/D02/D03``, ``G36/G37`` regions, and ``%LPD/%LPC``
polarity. Modal coordinates (an omitted X or Y repeats the previous value)
are honoured, because omitting them is legal and a reader that assumed zero
would silently draw a board folded onto its own axes.

**What it does not handle is raised, not skipped**: macro apertures
(``%AM``), step-and-repeat (``%SR``), and polygon apertures raise
:class:`UnsupportedGerber`. A viewer that quietly drops what it cannot read
would show a clean board with a missing feature, which is the failure mode
this whole module exists to avoid.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

#: Layer name -> (fill colour, default visibility). Anything unrecognised
#: falls through to a neutral grey and is still listed in the selector —
#: an unknown layer is a layer you especially want to see.
_LAYER_STYLE: dict[str, tuple[str, bool]] = {
    "F_Cu": ("#c8781e", True),
    "In1_Cu": ("#1e8f4e", True),
    "In2_Cu": ("#8f1e8f", True),
    "B_Cu": ("#2f6fd0", True),
    "F_Mask": ("#7a2d8f", False),
    "B_Mask": ("#8f2d5e", False),
    "F_Silkscreen": ("#e8e8e8", True),
    "B_Silkscreen": ("#a8a8a8", False),
    "Edge_Cuts": ("#f0d000", True),
    "PTH": ("#101010", True),
    "NPTH": ("#404040", True),
}
_DEFAULT_STYLE = ("#9a9a9a", True)

#: Rendering order, back to front. Board outline and drills sit on top of
#: copper so a hole is never hidden by the annulus around it.
_STACK_ORDER = [
    "B_Cu",
    "In2_Cu",
    "In1_Cu",
    "F_Cu",
    "B_Mask",
    "F_Mask",
    "B_Silkscreen",
    "F_Silkscreen",
    "PTH",
    "NPTH",
    "Edge_Cuts",
]

_ARC_MAX_SEG_DEG = 6.0


class UnsupportedGerber(ValueError):
    """A construct this reader will not guess at."""


@dataclass(frozen=True, slots=True)
class Aperture:
    shape: str  # "C" | "R" | "O"
    sizes: tuple[float, ...]


@dataclass
class Stroke:
    """A polyline drawn with a round aperture of ``width`` mm."""

    points: list[tuple[float, float]]
    width: float


@dataclass
class Flash:
    aperture: Aperture
    x: float
    y: float


@dataclass
class Region:
    ring: list[tuple[float, float]]
    #: ``False`` for a ``%LPC*%`` region — an antipad, not copper.
    solid: bool = True


@dataclass
class LayerArt:
    strokes: list[Stroke] = field(default_factory=list)
    flashes: list[Flash] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)


_FS_RE = re.compile(r"%FSLA?X(\d)(\d)Y(\d)(\d)\*%")
_AD_RE = re.compile(r"%ADD(\d+)([A-Za-z]+),([0-9.X]+)\*%")
_COORD_RE = re.compile(r"([XYIJ])(-?\d+)")


def parse_gerber(text: str) -> LayerArt:
    """One gerber file's drawable geometry, in millimetres."""
    art = LayerArt()
    scale = 10.0**6  # overwritten by %FS; the emitter's own X46Y46
    to_mm = 1.0
    apertures: dict[int, Aperture] = {}
    current: Aperture | None = None
    x = y = 0.0
    interp = "G01"
    in_region = False
    region_pts: list[tuple[float, float]] = []
    polarity_solid = True
    stroke: Stroke | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("G04"):
            continue
        if line.startswith("%AM") or line.startswith("%SR"):
            raise UnsupportedGerber(
                f"aperture macro / step-and-repeat not supported: {line!r}"
            )
        m = _FS_RE.match(line)
        if m:
            scale = 10.0 ** int(m.group(2))
            continue
        if line.startswith("%MO"):
            to_mm = 25.4 if "IN" in line else 1.0
            continue
        m = _AD_RE.match(line)
        if m:
            shape = m.group(2).upper()
            if shape not in ("C", "R", "O"):
                raise UnsupportedGerber(f"aperture shape {shape!r} not supported")
            sizes = tuple(float(v) for v in m.group(3).split("X") if v)
            apertures[int(m.group(1))] = Aperture(shape, sizes)
            continue
        if line == "%LPC*%":
            polarity_solid = False
            continue
        if line == "%LPD*%":
            polarity_solid = True
            continue
        if line.startswith("%"):
            continue  # an attribute or other non-drawing extended command
        if line == "M02*":
            break

        body = line[:-1] if line.endswith("*") else line
        for gcode in ("G36", "G37", "G01", "G02", "G03", "G74", "G75"):
            if gcode in body:
                if gcode == "G36":
                    in_region, region_pts = True, []
                elif gcode == "G37":
                    in_region = False
                    if len(region_pts) >= 3:
                        art.regions.append(Region(region_pts, polarity_solid))
                    region_pts = []
                elif gcode in ("G01", "G02", "G03"):
                    interp = gcode
                body = body.replace(gcode, "")
        d_match = re.search(r"D(\d+)$", body)
        op: int | None = None
        if d_match:
            code = int(d_match.group(1))
            body = body[: d_match.start()]
            if code in (1, 2, 3):
                op = code
            else:
                current = apertures.get(code)
                if current is None:
                    raise UnsupportedGerber(f"select of undefined aperture D{code}")
        if op is None:
            continue

        coords = {k: int(v) for k, v in _COORD_RE.findall(body)}
        # Modal: an omitted axis repeats. Assuming zero here would fold the
        # whole board onto its axes, quietly.
        nx = coords["X"] / scale * to_mm if "X" in coords else x
        ny = coords["Y"] / scale * to_mm if "Y" in coords else y

        if op == 2:  # move
            if stroke is not None and len(stroke.points) > 1:
                art.strokes.append(stroke)
            stroke = None
            if in_region:
                region_pts = [(nx, ny)]
            x, y = nx, ny
            continue
        if op == 3:  # flash
            if current is None:
                raise UnsupportedGerber("flash with no aperture selected")
            art.flashes.append(Flash(current, nx, ny))
            x, y = nx, ny
            continue

        # op == 1: draw
        if interp in ("G02", "G03"):
            i = coords.get("I", 0) / scale * to_mm
            j = coords.get("J", 0) / scale * to_mm
            pts = _arc_points((x, y), (nx, ny), (x + i, y + j), cw=interp == "G02")
        else:
            pts = [(nx, ny)]
        if in_region:
            if not region_pts:
                region_pts = [(x, y)]
            region_pts.extend(pts)
        else:
            width = current.sizes[0] if current is not None else 0.1
            if stroke is None:
                stroke = Stroke([(x, y)], width)
            stroke.points.extend(pts)
        x, y = nx, ny

    if stroke is not None and len(stroke.points) > 1:
        art.strokes.append(stroke)
    return art


def _arc_points(
    start: tuple[float, float],
    end: tuple[float, float],
    center: tuple[float, float],
    *,
    cw: bool,
) -> list[tuple[float, float]]:
    """Flatten an arc to a polyline. The SVG could carry a real ``A``
    command, but a flattened arc renders identically and keeps every shape
    in this module a polyline — which is what makes the bounding box, the
    stroke and the region path one code path instead of three."""
    r = math.dist(start, center)
    if r < 1e-12:
        return [end]
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    sweep = a1 - a0
    if cw:
        while sweep > 0:
            sweep -= 2 * math.pi
        if sweep < -2 * math.pi:
            sweep += 2 * math.pi
    else:
        while sweep < 0:
            sweep += 2 * math.pi
        if sweep > 2 * math.pi:
            sweep -= 2 * math.pi
    if abs(sweep) < 1e-12:  # full circle, not a zero-length arc
        sweep = -2 * math.pi if cw else 2 * math.pi
    steps = max(2, int(abs(math.degrees(sweep)) / _ARC_MAX_SEG_DEG) + 1)
    return [
        (
            center[0] + r * math.cos(a0 + sweep * k / steps),
            center[1] + r * math.sin(a0 + sweep * k / steps),
        )
        for k in range(1, steps + 1)
    ]


def _layer_key(filename: str) -> str:
    """``esp32c3-F_Cu.gbr`` -> ``F_Cu``; ``esp32c3-PTH.drl`` -> ``PTH``."""
    stem = filename.rsplit(".", 1)[0]
    return stem.rsplit("-", 1)[-1] if "-" in stem else stem


def parse_excellon(text: str) -> list[tuple[float, float, float]]:
    """``(x, y, diameter)`` per hole, from the Excellon this project writes
    (METRIC, decimal-point coordinates — so no zero-suppression ambiguity
    to get wrong)."""
    tools: dict[int, float] = {}
    holes: list[tuple[float, float, float]] = []
    cur = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^T(\d+)C([\d.]+)$", line)
        if m:
            tools[int(m.group(1))] = float(m.group(2))
            continue
        m = re.match(r"^T(\d+)$", line)
        if m:
            cur = tools.get(int(m.group(1)), 0.0)
            continue
        m = re.match(r"^X(-?[\d.]+)Y(-?[\d.]+)$", line)
        if m:
            holes.append((float(m.group(1)), float(m.group(2)), cur))
    return holes


def _flash_svg(flash: Flash, colour: str) -> str:
    ap = flash.aperture
    if ap.shape == "C":
        return (
            f'<circle cx="{flash.x:.4f}" cy="{flash.y:.4f}" '
            f'r="{ap.sizes[0] / 2:.4f}" fill="{colour}"/>'
        )
    w = ap.sizes[0]
    h = ap.sizes[1] if len(ap.sizes) > 1 else w
    rx = f' rx="{min(w, h) / 2:.4f}"' if ap.shape == "O" else ""
    return (
        f'<rect x="{flash.x - w / 2:.4f}" y="{flash.y - h / 2:.4f}" '
        f'width="{w:.4f}" height="{h:.4f}"{rx} fill="{colour}"/>'
    )


def _path_d(points: list[tuple[float, float]], *, close: bool = False) -> str:
    if not points:
        return ""
    parts = [f"M {points[0][0]:.4f} {points[0][1]:.4f}"]
    parts += [f"L {p[0]:.4f} {p[1]:.4f}" for p in points[1:]]
    if close:
        parts.append("Z")
    return " ".join(parts)


def _bounds(
    arts: dict[str, LayerArt], drills: dict[str, list[tuple[float, float, float]]]
) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for art in arts.values():
        for s in art.strokes:
            xs += [p[0] for p in s.points]
            ys += [p[1] for p in s.points]
        for f in art.flashes:
            xs.append(f.x)
            ys.append(f.y)
        for r in art.regions:
            xs += [p[0] for p in r.ring]
            ys += [p[1] for p in r.ring]
    for holes in drills.values():
        xs += [h[0] for h in holes]
        ys += [h[1] for h in holes]
    if not xs or not ys:
        return (0.0, 0.0, 10.0, 10.0)
    return (min(xs), min(ys), max(xs), max(ys))


def render_fab_svg(
    files: dict[str, str], *, title: str = "board", margin_mm: float = 2.0
) -> str:
    """An SVG of a whole fab set, one toggleable group per layer.

    The y axis is flipped (``scale(1,-1)``) because gerber's origin is
    bottom-left and SVG's is top-left. Getting this wrong renders a mirrored
    board that looks entirely plausible — which is the kind of error that
    reaches a fab.

    The layer selector is inline: legend rows toggle a CSS class on their
    group. A viewer that does not run script still shows every layer that
    starts visible, so the file is never blank in a static renderer.
    """
    arts: dict[str, LayerArt] = {}
    drills: dict[str, list[tuple[float, float, float]]] = {}
    for name, text in sorted(files.items()):
        key = _layer_key(name)
        if name.endswith(".drl"):
            drills[key] = parse_excellon(text)
        else:
            arts[key] = parse_gerber(text)

    x0, y0, x1, y1 = _bounds(arts, drills)
    x0, y0 = x0 - margin_mm, y0 - margin_mm
    x1, y1 = x1 + margin_mm, y1 + margin_mm
    w, h = max(1e-6, x1 - x0), max(1e-6, y1 - y0)

    keys = [k for k in _STACK_ORDER if k in arts or k in drills]
    keys += sorted(k for k in {*arts, *drills} if k not in _STACK_ORDER)

    body: list[str] = []
    for key in keys:
        colour, visible = _LAYER_STYLE.get(key, _DEFAULT_STYLE)
        cls = "layer" if visible else "layer off"
        body.append(f'<g id="layer-{key}" class="{cls}" fill="{colour}">')
        art = arts.get(key)
        if art is not None:
            for region in art.regions:
                # A clear-polarity region is an antipad: it is where copper
                # ISN'T. Painting it in the board background rather than
                # the layer colour is the whole point — a plane rendered
                # without its holes is indistinguishable from a short.
                fill = colour if region.solid else "#12141a"
                body.append(
                    f'<path d="{_path_d(region.ring, close=True)}" fill="{fill}"/>'
                )
            for s in art.strokes:
                body.append(
                    f'<path d="{_path_d(s.points)}" fill="none" stroke="{colour}" '
                    f'stroke-width="{s.width:.4f}" stroke-linecap="round" '
                    'stroke-linejoin="round"/>'
                )
            for f in art.flashes:
                body.append(_flash_svg(f, colour))
        for hx, hy, dia in drills.get(key, ()):
            body.append(
                f'<circle cx="{hx:.4f}" cy="{hy:.4f}" r="{dia / 2:.4f}" '
                f'fill="{colour}"/>'
            )
        body.append("</g>")

    rows: list[str] = []
    for i, key in enumerate(keys):
        colour, visible = _LAYER_STYLE.get(key, _DEFAULT_STYLE)
        ty = 18 + i * 20
        mark = "on" if visible else "off"
        rows.append(
            f'<g class="legend-row {mark}" data-layer="{key}" '
            f"onclick=\"(function(g){{g.classList.toggle('off');"
            f"document.getElementById('layer-{key}').classList.toggle('off');}})"
            '(this)">'
            f'<rect x="8" y="{ty - 11}" width="152" height="18" class="hit"/>'
            f'<rect x="12" y="{ty - 8}" width="12" height="12" fill="{colour}" '
            'class="swatch"/>'
            f'<text x="32" y="{ty + 2}">{key}</text>'
            "</g>"
        )
    legend_h = 20 * len(keys) + 12

    # The whole document is in PIXELS, not millimetres, and the board is
    # scaled into it. Working in mm would be tidier for the geometry and
    # makes the legend unreadable: its 11px type would be 11 MILLIMETRES,
    # so on a 300mm board outline (which the reference fixture has, around
    # 44mm of parts) the legend renders larger than the board. Chrome
    # scaled to fit, so a picture meant to show a board showed a caption.
    px = _view_scale(w, h)
    vw, vh = w * px, h * px
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vw:.1f} {vh:.1f}"
 width="{vw:.0f}" height="{vh:.0f}" role="img" aria-label="{title}">
<title>{title} — rendered from gerbers</title>
<style>
  .layer.off {{ display: none; }}
  .legend {{ font: 11px ui-monospace, monospace; fill: #d8d8d8; }}
  .legend .hit {{ fill: transparent; cursor: pointer; }}
  .legend-row.off text {{ fill: #6a6a6a; }}
  .legend-row.off .swatch {{ fill-opacity: 0.25; }}
</style>
<rect x="0" y="0" width="{vw:.1f}" height="{vh:.1f}" fill="#12141a"/>
<g transform="translate({-x0 * px:.4f},{y1 * px:.4f}) scale({px:.6f},-{px:.6f})">
{chr(10).join(body)}
</g>
<g class="legend">
<rect x="4" y="4" width="160" height="{legend_h}" fill="#000000cc" rx="4"/>
{chr(10).join(rows)}
</g>
</svg>
"""


#: Pixels per millimetre, and the box the result is kept inside. A board is
#: anywhere from a 10mm coupon to a 300mm panel; a fixed mm-to-px factor
#: gives either a postage stamp or a 20-megapixel document.
_TARGET_PX = 1600.0
_MAX_PX_PER_MM = 20.0


def _view_scale(w: float, h: float) -> float:
    return min(_MAX_PX_PER_MM, _TARGET_PX / max(w, h, 1e-6))


__all__ = [
    "Aperture",
    "Flash",
    "LayerArt",
    "Region",
    "Stroke",
    "UnsupportedGerber",
    "parse_excellon",
    "parse_gerber",
    "render_fab_svg",
]

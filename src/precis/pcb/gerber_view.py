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

``%LPC*%`` polarity is honoured on a STROKE, not just a region: a
clear-polarity stroke (e.g. a knocked-out "S/N" letter cut into a filled
silk patch) is reshaped into a hole ring and rendered through the exact
same solid-fill-plus-holes cutout path a copper pour's antipads already
use (:func:`_finalize_stroke`, :func:`_region_els`) — a reader that
tracked ``%LPC*%`` for regions only would show that knockout as either
solid ink (polarity silently ignored) or nothing at all (the whole
stroke silently dropped), while the gerber itself is correct; either is
the exact gerber/picture divergence this module exists to eliminate.

**What it does not handle is raised, not skipped**: macro apertures
(``%AM``), step-and-repeat (``%SR``), and polygon apertures raise
:class:`UnsupportedGerber`. A viewer that quietly drops what it cannot read
would show a clean board with a missing feature, which is the failure mode
this whole module exists to avoid.

**Hover identity comes from the gerber, never the model.** :mod:`precis.pcb
.gerber` writes X2 object attributes (``%TO.N,<net>*%`` on copper it knows
the net of, ``%TO.P,<refdes>,<pin>*%`` on pads the model gave a component
pin) and clears them with ``%TD*%`` right after the object they describe.
This reader tracks that same attribute state and stamps it onto the
:class:`Flash`/:class:`Stroke`/:class:`Region` it produces, and
:func:`render_fab_svg` turns it into an SVG ``<title>`` — the browser's own
native tooltip, so it works with no script and survives the file being
opened straight off disk. Every title reads as a HIERARCHY, outermost
first: layer, then where on the board, then what the shape physically is,
then (last, because it is the one piece of it optional per-object) what
it belongs to on the schematic — e.g. ``F_Cu · (12.3000, 4.5000) mm · pad
1 of Q4 · net OUT4``. An object with no ``%TO...*%`` gets no identity
clause, only layer/coordinates/kind: this reader never fills that gap
by cross-referencing anything outside the gerber/Excellon text, because
doing so is exactly the divergence-from-the-artefact this module exists to
eliminate. A recognised ``%TO.N``/``%TO.P`` that fails to parse raises
:class:`UnsupportedGerber` rather than silently carrying no identity — the
same discipline as ``%AM``/``%SR`` above; any *other* ``%TO...`` (component-
level attributes such as ``%TO.C``, which this writer does not emit) falls
through the same harmless catch-all as an unrecognised ``%TF...`` file
attribute always has.

**A soldermask layer is rendered as what it physically is: a film with
holes**, not as the holes themselves. ``F_Mask``/``B_Mask`` gerbers carry
only the OPENINGS (see :func:`precis.pcb.gerber.soldermask_gerber`) — the
mask material itself is everywhere else on the board, implicit, the same
way the copper a track is stroked on is implicit in a copper gerber.
Rendering just the opening flashes (the earlier approach) paints only a
handful of small shapes sitting exactly on top of the pads they open,
which is nearly indistinguishable from the copper beneath it — toggling
the layer on reads as toggling nothing. :func:`render_fab_svg` instead
draws a translucent film across the whole board (the parsed Edge_Cuts
outline when available, else the viewBox rect) and cuts the openings out
of it with an SVG ``<mask>`` — the same paint-semantics trick
:func:`_region_els` already uses for antipads/knockouts, so overlapping
openings all stay open. The openings still carry their titles: each one
also gets an all-but-invisible hit target on top of the film, in the same
shape, so hovering a pad's mask opening still tells you which pad it is.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

#: A drill is an absence of material, not a coloured feature — painting
#: PTH/NPTH the same way as a filled copper/mask/legend layer (dark fill on
#: this document's dark background) is exactly how a real render's 26
#: holes went invisible. Render a hole as a VOID instead — light fill + a
#: stroked edge, the same convention :mod:`precis.pcb.svg`'s ``_via_el``
#: (white centre punched out of the annulus) and ``_drill_el`` (white
#: fill, stroked so it survives against any background) already use;
#: matching it here rather than inventing a third "what does a hole look
#: like" convention. PTH and NPTH share this fill — a fab drills them on
#: separate passes and they mean different things (a soldered lead/via vs
#: a mechanical hole), but the distinguishing cue is the STROKE, exactly
#: as ``_drill_el`` already draws it: solid for plated, dashed for
#: unplated — so it stays legible whether the layer under it is toggled
#: on (against copper) or off (against the bare document background).
_DRILL_FILL = "#eef2f7"
_DRILL_STROKE = "#0a0a0a"
_DRILL_STROKE_WIDTH = 0.05
_DRILL_DASH_NPTH = "0.3,0.2"  # same pattern precis.pcb.svg._drill_el uses

#: The document background — also the paint an orphan clear-polarity region
#: (see ``_region_els``) falls back to when it has no preceding solid ring
#: to be cut out of.
_BOARD_BG = "#12141a"

#: Copper layers get GROUP opacity (not per-element) so a stacked board with
#: full-plane pours doesn't hide every layer beneath the topmost one — the
#: real defect: F.Cu carrying a board-covering GND fill made In1/In2/B.Cu
#: (and the vias passing through them) invisible with no way to see a via's
#: relationship to the copper it passes through short of toggling layers
#: one at a time. ~KiCad's own copper alpha. Applied to the <g>, not to
#: individual fills/strokes, so a trace crossing its own pad composites as
#: one flat colour first and is alpha-blended against the layers below only
#: once — per-element alpha would double-darken that overlap into a fake
#: feature. Drills, silkscreen and the board outline stay fully opaque (a
#: hole is an absence of material, not a see-through one; silkscreen is
#: physically ink on top) — see the per-key check in render_fab_svg.
_COPPER_LAYER_OPACITY = 0.7

#: Legend text for the two drill layers — terse enough to keep the 152px
#: legend row width, but says "drill" so a reader doesn't mistake this row
#: for a gerber layer (it's the one row in the legend that isn't one).
_DRILL_LABEL: dict[str, str] = {"PTH": "PTH (drill)", "NPTH": "NPTH (drill)"}

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
    # Paste is the stencil, not the board — off by default because it sits
    # exactly on top of the pads it opens and would otherwise read as a
    # recolouring of F_Cu rather than as its own layer.
    "F_Paste": ("#b0b6bd", False),
    "B_Paste": ("#7d838a", False),
    "F_Silkscreen": ("#e8e8e8", True),
    "B_Silkscreen": ("#a8a8a8", False),
    "Edge_Cuts": ("#f0d000", True),
    "PTH": (_DRILL_FILL, True),
    "NPTH": (_DRILL_FILL, True),
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
    "B_Paste",
    "F_Paste",
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
    #: X2 object identity in force when this object was drawn — ``None``
    #: when the gerber carried no ``%TO...*%`` for it. Never guessed.
    net: str | None = None
    refdes: str | None = None
    pin: str | None = None
    #: ``False`` for a stroke drawn under ``%LPC*%`` — a knockout, not
    #: ink. Snapshotted from the polarity in force when the stroke
    #: started, same as :attr:`Region.solid`. A clear-polarity stroke
    #: never survives into :attr:`LayerArt.strokes` (see
    #: :func:`_finalize_stroke`) — this field exists so a caller can tell
    #: the difference if it ever inspects a :class:`Stroke` before that
    #: conversion runs (e.g. a future direct consumer of
    #: :func:`parse_gerber`), not because a clear stroke is meant to be
    #: rendered as one.
    solid: bool = True


@dataclass
class Flash:
    aperture: Aperture
    x: float
    y: float
    net: str | None = None
    refdes: str | None = None
    pin: str | None = None


@dataclass
class Region:
    """A ``G36``/``G37`` filled ring — a copper pour, OR (see
    :func:`_finalize_stroke`) a clear-polarity STROKE reshaped into a hole
    ring so it can ride the exact same solid+holes cutout rendering
    (:func:`_region_els`) a pour's antipad already uses. Both are "a solid
    fill with zero or more holes cut into it that immediately follow it in
    the file" — a silk knockout letter is geometrically the same shape of
    fact as an antipad, just authored as a stroke instead of a region in
    the gerber text."""

    ring: list[tuple[float, float]]
    #: ``False`` for a ``%LPC*%`` region (or clear stroke) — a hole, not
    #: ink/copper.
    solid: bool = True
    #: Never set on a hole ring — see :func:`precis.pcb.gerber
    #: ._emit_region`'s docstring for why a hole must not inherit the
    #: pour's net.
    net: str | None = None


@dataclass
class LayerArt:
    strokes: list[Stroke] = field(default_factory=list)
    flashes: list[Flash] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)


_FS_RE = re.compile(r"%FSLA?X(\d)(\d)Y(\d)(\d)\*%")
_AD_RE = re.compile(r"%ADD(\d+)([A-Za-z]+),([0-9.X]+)\*%")
_COORD_RE = re.compile(r"([XYIJ])(-?\d+)")
#: X2 object attributes this reader resolves into hover identity — see the
#: module docstring for why any *other* ``%TO...`` falls through unparsed
#: instead of raising.
_TO_N_RE = re.compile(r"%TO\.N,([^*,]*)\*%")
_TO_P_RE = re.compile(r"%TO\.P,([^*,]*),([^*,]*)\*%")


@dataclass
class _ObjectAttrs:
    """Attribute state in force at the current point in the file — reset by
    ``%TD*%``, set by ``%TO.N``/``%TO.P``. Snapshotted (not shared) onto
    every :class:`Flash`/:class:`Stroke`/:class:`Region` produced while it
    is in force, so a later ``%TD*%`` can never retroactively change an
    already-emitted object's identity."""

    net: str | None = None
    refdes: str | None = None
    pin: str | None = None


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
    attrs = _ObjectAttrs()
    #: identity in force when G36 opened this region — snapshotted there
    #: because %TD*% (or a fresh %TO...*%) can legally appear before G37.
    region_attrs = _ObjectAttrs()

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
        if line == "%TD*%":
            attrs = _ObjectAttrs()
            continue
        if line.startswith("%TO.N"):
            m = _TO_N_RE.match(line)
            if not m:
                raise UnsupportedGerber(f"malformed net object attribute: {line!r}")
            attrs = _ObjectAttrs(net=m.group(1), refdes=attrs.refdes, pin=attrs.pin)
            continue
        if line.startswith("%TO.P"):
            m = _TO_P_RE.match(line)
            if not m:
                raise UnsupportedGerber(f"malformed pin object attribute: {line!r}")
            attrs = _ObjectAttrs(net=attrs.net, refdes=m.group(1), pin=m.group(2))
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
                    region_attrs = attrs
                elif gcode == "G37":
                    in_region = False
                    if len(region_pts) >= 3:
                        art.regions.append(
                            Region(region_pts, polarity_solid, net=region_attrs.net)
                        )
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
            _finalize_stroke(stroke, art)
            stroke = None
            if in_region:
                region_pts = [(nx, ny)]
            x, y = nx, ny
            continue
        if op == 3:  # flash
            if current is None:
                raise UnsupportedGerber("flash with no aperture selected")
            art.flashes.append(
                Flash(
                    current, nx, ny, net=attrs.net, refdes=attrs.refdes, pin=attrs.pin
                )
            )
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
                stroke = Stroke(
                    [(x, y)],
                    width,
                    net=attrs.net,
                    refdes=attrs.refdes,
                    pin=attrs.pin,
                    solid=polarity_solid,
                )
            stroke.points.extend(pts)
        x, y = nx, ny

    _finalize_stroke(stroke, art)
    return art


def _finalize_stroke(stroke: Stroke | None, art: LayerArt) -> None:
    """Commit one finished stroke into ``art`` — a dark stroke as a real
    :class:`Stroke` (rendered as ink), a CLEAR one reshaped into a
    :class:`Region` hole ring instead (see that class's own docstring) so
    it rides :func:`_region_els`'s existing solid+holes cutout rendering
    rather than a second, silk-specific rendering path. A clear stroke
    with nothing solid before it on this layer is an orphan hole — see
    :func:`_region_els` for how that (never emitted by this project's own
    writer, but legal in a foreign file) falls back."""
    if stroke is None or len(stroke.points) <= 1:
        return
    if stroke.solid:
        art.strokes.append(stroke)
        return
    ring = _stroke_ring(stroke.points, stroke.width)
    if ring:
        art.regions.append(Region(ring, solid=False, net=None))


def _stroke_ring(
    points: list[tuple[float, float]], width: float
) -> list[tuple[float, float]]:
    """Approximate a stroked polyline as ONE filled ring: each point
    offset perpendicular to its segment(s) by half the stroke width, left
    side out and right side back — a cheap stroke-to-fill conversion (no
    real miter/bevel join, square caps at the two true ends only, no cap
    padding at internal joints so two adjacent segments' offsets don't
    overlap and repaint a sliver of ink back into what is meant to be a
    hole). Good enough for a viewer reconstructing what a knocked-out
    glyph looks like, the same "cheap eyes" spirit
    :mod:`precis.pcb.silk`'s own SAT/segment-box primitives already use
    for their overlap checks — not a fab-precision offset curve."""
    if len(points) < 2:
        return []
    half = width / 2.0
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    last = len(points) - 2
    for i in range(len(points) - 1):
        ax, ay = points[i]
        bx, by = points[i + 1]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        ux, uy = dx / length, dy / length
        nx, ny = -uy, ux
        ex, ey = (ux * half, uy * half) if i == 0 else (0.0, 0.0)
        fx, fy = (ux * half, uy * half) if i == last else (0.0, 0.0)
        left.append((ax - ex + nx * half, ay - ey + ny * half))
        left.append((bx + fx + nx * half, by + fy + ny * half))
        right.append((ax - ex - nx * half, ay - ey - ny * half))
        right.append((bx + fx - nx * half, by + fy - ny * half))
    if not left:
        return []
    return [*left, *reversed(right)]


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


def _esc(text: str) -> str:
    """Minimal XML text escaping — a net/refdes carrying ``&``/``<``/``>``
    would otherwise land inside an SVG ``<title>`` and break the document,
    for the sake of a hover label."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _pt(x: float, y: float) -> str:
    return f"({x:.4f}, {y:.4f}) mm"


def _net_clause(net: str | None) -> str:
    """The trailing "belongs to" clause every title ends with when the
    gerber carried a ``%TO.N``/net — empty (and so dropped, see
    :func:`_title`) when it didn't. Built ONLY from what
    :func:`parse_gerber` read off the file; there is no fallback that
    looks anywhere else."""
    return f"net {net}" if net else ""


def _title(layer: str, coords: str, kind: str, belongs_to: str = "") -> str:
    """Every hover title reads as one hierarchy, outermost first: which
    LAYER, then WHERE on the board, then WHAT the shape physically is,
    then (last, and only when the gerber said so) what it BELONGS TO on
    the schematic — see the module docstring's own example."""
    parts = [layer, coords, kind, *([belongs_to] if belongs_to else [])]
    return _esc(" · ".join(parts))


def _flash_title(layer: str, flash: Flash) -> str:
    # "pad N of REFDES" only when the gerber itself said so via %TO.P — a
    # round flash with just a net (a via barrel, or a pad the model never
    # gave a pin) is called a "flash", not guessed to be one or the other.
    if flash.refdes and flash.pin:
        kind = f"pad {flash.pin} of {flash.refdes}"
    else:
        kind = "flash"
    return _title(layer, _pt(flash.x, flash.y), kind, _net_clause(flash.net))


def _stroke_title(layer: str, stroke: Stroke) -> str:
    p0, p1 = stroke.points[0], stroke.points[-1]
    coords = f"{_pt(*p0)} → {_pt(*p1)}"
    return _title(layer, coords, f"track {stroke.width:.4f}mm", _net_clause(stroke.net))


def _region_title(layer: str, region: Region) -> str:
    xs = [p[0] for p in region.ring]
    ys = [p[1] for p in region.ring]
    coords = _pt((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
    # A clear-polarity region is an antipad/knockout, not copper/ink — it
    # never carries a net (see the Region.net docstring), so this branch
    # is never a missed identity, it is the correct absence of one.
    # "pour"/"antipad" is copper vocabulary; a silk layer's solid fill and
    # its holes (real regions OR knocked-out strokes, see Region's own
    # docstring) get the silk-appropriate words instead of borrowing
    # copper's.
    is_silk = "Silkscreen" in layer
    if region.solid:
        kind = "silk fill" if is_silk else "pour"
    else:
        kind = "silk knockout" if is_silk else "antipad"
    return _title(layer, coords, kind, _net_clause(region.net))


def _drill_title(layer: str, x: float, y: float, dia: float) -> str:
    # Drills come from Excellon, which this project's writer never gives an
    # X2 attribute — there is no identity to show, by construction, not by
    # omission.
    return _title(layer, _pt(x, y), f"drill Ø{dia:.4f}mm")


def _flash_geometry(flash: Flash) -> tuple[str, str]:
    """``(tag, shape-attrs)`` for one flash's aperture — the ONE place that
    turns a :class:`Flash` into an SVG shape, shared by the normal
    coloured render (:func:`_flash_svg`), the mask-film cutout shape
    (:func:`_flash_mask_shape`) and the mask-film hit target
    (:func:`_flash_hit_target`) — three different fills of the exact same
    outline must never risk drifting into three different outlines."""
    ap = flash.aperture
    if ap.shape == "C":
        return "circle", (
            f'cx="{flash.x:.4f}" cy="{flash.y:.4f}" r="{ap.sizes[0] / 2:.4f}"'
        )
    w = ap.sizes[0]
    h = ap.sizes[1] if len(ap.sizes) > 1 else w
    rx = f' rx="{min(w, h) / 2:.4f}"' if ap.shape == "O" else ""
    return "rect", (
        f'x="{flash.x - w / 2:.4f}" y="{flash.y - h / 2:.4f}" '
        f'width="{w:.4f}" height="{h:.4f}"{rx}'
    )


def _flash_svg(flash: Flash, colour: str, title: str) -> str:
    tag, attrs = _flash_geometry(flash)
    return f'<{tag} {attrs} fill="{colour}"><title>{title}</title></{tag}>'


def _flash_mask_shape(flash: Flash) -> str:
    """One soldermask opening, painted BLACK into a mask film's ``<mask>``
    — see :func:`_mask_film_els`. Each opening is its own shape (never
    unioned into one compound path), so two overlapping openings both stay
    cut out, the same paint-semantics reasoning :func:`_region_els`'s own
    docstring lays out for antipads/knockouts."""
    tag, attrs = _flash_geometry(flash)
    return f'<{tag} {attrs} fill="#000"/>'


def _flash_hit_target(flash: Flash, title: str) -> str:
    """An almost-invisible copy of one soldermask opening, painted ON TOP
    of the film (not cut into it) purely so the opening stays hoverable —
    the film itself has no per-opening ``<title>`` (it is one compound
    ``<path>``), and this is cheaper than reshaping the film into N
    separate paths just to hang a tooltip off each one."""
    tag, attrs = _flash_geometry(flash)
    return (
        f'<{tag} {attrs} fill="#fff" fill-opacity="0.01"><title>{title}</title></{tag}>'
    )


def _rect_ring(bounds: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    x0, y0, x1, y1 = bounds
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _edge_cuts_outline(art: LayerArt | None) -> list[tuple[float, float]] | None:
    """The board outline as THIS reader saw it drawn — Edge_Cuts' own
    stroke polyline(s), concatenated in file order — used as a mask
    film's own shape (see :func:`_mask_film_els`) so a non-rectangular
    board doesn't get a rectangular film spilling past its own edge.
    ``None`` when Edge_Cuts didn't parse or drew nothing, in which case the
    caller falls back to the viewBox rect."""
    if art is None or not art.strokes:
        return None
    ring: list[tuple[float, float]] = []
    for s in art.strokes:
        ring.extend(s.points)
    return ring if len(ring) >= 3 else None


def _mask_film_els(
    art: LayerArt | None, layer: str, colour: str, film_ring: list[tuple[float, float]]
) -> list[str]:
    """A soldermask layer rendered as what it physically is — see the
    module docstring's "a film with holes" note. ``art.flashes`` are the
    openings (this project's own writer, :func:`precis.pcb.gerber
    .soldermask_gerber`, never emits anything else on a mask layer — see
    that function's own docstring); everything else about this render is
    the paint-semantics mask trick :func:`_region_els` already uses."""
    openings = art.flashes if art is not None else []
    if not openings:
        return [
            f'<path d="{_path_d(film_ring, close=True)}" '
            f'fill="{colour}" fill-opacity="0.55"/>'
        ]
    mask_id = f"masklayer-mask-{layer}"
    mask_shapes = "".join(_flash_mask_shape(f) for f in openings)
    out = [
        f'<mask id="{mask_id}" maskUnits="userSpaceOnUse" '
        f'x="-1e6" y="-1e6" width="2e6" height="2e6">'
        f'<path d="{_path_d(film_ring, close=True)}" fill="#fff"/>'
        f"{mask_shapes}</mask>",
        f'<path d="{_path_d(film_ring, close=True)}" fill="{colour}" '
        f'fill-opacity="0.55" mask="url(#{mask_id})"/>',
    ]
    out.extend(_flash_hit_target(f, _flash_title(layer, f)) for f in openings)
    return out


def _region_els(regions: list[Region], layer: str, colour: str) -> list[str]:
    """SVG for one layer's regions, pairing each solid fill with the
    clear-polarity holes that immediately follow it — exactly how
    :func:`precis.pcb.gerber._emit_region` always writes a pour's antipads:
    a solid ring, then zero or more ``%LPC*%`` holes, before the next
    pour. A hole entry here may be a REAL region (an antipad) or a
    clear-polarity stroke :func:`_finalize_stroke` reshaped into a ring
    (a silk knockout letter, e.g. :func:`precis.pcb.silk.build_sn_patch`'s
    "S/N") — both arrive here as plain :class:`Region` objects and are
    indistinguishable to this function, which is the point: one cutout
    renderer, not a copper one and a silk-specific second one.

    Each pair becomes a solid ring paired with an SVG ``<mask>`` cut from
    the clear rings, NOT a single compound path with ``fill-rule="evenodd"``
    (the earlier technique, and still what :func:`precis.pcb.svg._pour_el`
    uses for a plain pour with a single, non-overlapping antipad set). A
    silk knockout letter's clear rings come from :func:`_stroke_ring` one
    PER STROKE, and two strokes of the same glyph legitimately overlap —
    e.g. an "N"'s diagonal meeting its vertical at the corner joint. Real
    gerber clear polarity is idempotent (erasing an already-erased area is
    still erased); ``evenodd`` parity is not — a second overlapping clear
    ring flips the overlap's winding count back to even, i.e. back to
    FILLED, which is exactly the white wedge this function exists to stop
    rendering. A mask does not have this failure mode: each clear ring is
    painted BLACK into the mask, and overlapping black paint is still
    black, so any area covered by one or more clear rings stays cut out
    regardless of how many of them cover it. The solid ring is painted
    WHITE into the same mask (visible), then the mask is applied to the
    visible path via ``mask="url(#...)"``. A mask is also why this still
    works now that the layer group is translucent
    (:data:`_COPPER_LAYER_OPACITY`): unlike the older "paint the hole in
    the board background colour" trick, where an opaque background-
    coloured patch would itself become a translucent grey smear over
    whatever is underneath, a masked-out area is genuinely transparent —
    real absence of paint, not paint the same colour as the paper.
    A solid ring with no following clear rings is left as a plain
    ``<path>`` with no mask at all — the common case (a pour or silk fill
    with no antipad/knockout) shouldn't pay for machinery it doesn't need.
    """
    out: list[str] = []
    i = 0
    while i < len(regions):
        region = regions[i]
        if region.solid:
            rings = [region.ring]
            j = i + 1
            while j < len(regions) and not regions[j].solid:
                rings.append(regions[j].ring)
                j += 1
            title = f"<title>{_region_title(layer, region)}</title>"
            if len(rings) == 1:
                out.append(
                    f'<path d="{_path_d(region.ring, close=True)}" '
                    f'fill="{colour}">{title}</path>'
                )
            else:
                # Unique across the whole document: this function runs once
                # per LAYER (see render_fab_svg), and `i` is this pair's
                # position within that layer's own region list.
                mask_id = f"region-mask-{layer}-{i}"
                mask_paths = "".join(
                    f'<path d="{_path_d(r, close=True)}" '
                    f'fill="{"#fff" if k == 0 else "#000"}"/>'
                    for k, r in enumerate(rings)
                )
                # userSpaceOnUse + an oversized fixed box, rather than the
                # default objectBoundingBox: a clear ring is expected to sit
                # inside the solid ring's bounds, but the default's -10%/
                # +120% padding is a bounding-box guess this function has no
                # reason to depend on being enough.
                out.append(
                    f'<mask id="{mask_id}" maskUnits="userSpaceOnUse" '
                    f'x="-1e6" y="-1e6" width="2e6" height="2e6">'
                    f"{mask_paths}</mask>"
                )
                out.append(
                    f'<path d="{_path_d(region.ring, close=True)}" '
                    f'fill="{colour}" mask="url(#{mask_id})">{title}</path>'
                )
            i = j
        else:
            # A clear-polarity region with no preceding solid ring to cut a
            # hole in — this project's writer never emits one on its own
            # (see the docstring above), so this is only reached on a
            # foreign/malformed file. Fall back to the old paint-over
            # rendering rather than dropping it: SOMETHING at full opacity
            # beats a silently missing antipad.
            out.append(
                f'<path d="{_path_d(region.ring, close=True)}" fill="{_BOARD_BG}" '
                'fill-opacity="1">'
                f"<title>{_region_title(layer, region)}</title></path>"
            )
            i += 1
    return out


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

    # The mask FILM's own shape — the real Edge_Cuts outline when this file
    # set parsed one, else the same rect the viewBox itself is built from.
    # Computed once, outside the per-layer loop: F_Mask and B_Mask are the
    # same physical board, so they share one film shape.
    film_ring = _edge_cuts_outline(arts.get("Edge_Cuts")) or _rect_ring(
        (x0, y0, x1, y1)
    )

    body: list[str] = []
    for key in keys:
        colour, visible = _LAYER_STYLE.get(key, _DEFAULT_STYLE)
        cls = "layer" if visible else "layer off"
        # GROUP opacity, copper layers only — see _COPPER_LAYER_OPACITY's
        # docstring. Drills/silkscreen/mask/paste/outline stay opaque:
        # a drill is a void (rendering it translucent would show copper
        # THROUGH a hole, exactly backwards), and silkscreen is physically
        # ink sitting on top, not something to see other layers through.
        op_attr = f' opacity="{_COPPER_LAYER_OPACITY}"' if key.endswith("_Cu") else ""
        body.append(f'<g id="layer-{key}" class="{cls}" fill="{colour}"{op_attr}>')
        art = arts.get(key)
        if key.endswith("_Mask"):
            # A film with holes, not the holes themselves — see the module
            # docstring's "rendered as what it physically is" note.
            body.extend(_mask_film_els(art, key, colour, film_ring))
        elif art is not None:
            body.extend(_region_els(art.regions, key, colour))
            for s in art.strokes:
                body.append(
                    f'<path d="{_path_d(s.points)}" fill="none" stroke="{colour}" '
                    f'stroke-width="{s.width:.4f}" stroke-linecap="round" '
                    'stroke-linejoin="round">'
                    f"<title>{_stroke_title(key, s)}</title></path>"
                )
            for f in art.flashes:
                body.append(_flash_svg(f, colour, _flash_title(key, f)))
        # A drill is a void, not a coloured feature -- see _DRILL_FILL's
        # docstring. Rendered here, on top of the copper/mask stack (this
        # key's position in _STACK_ORDER), so it reads as a hole PUNCHED
        # OUT of whatever is beneath, same as precis.pcb.svg's _via_el.
        dash = f' stroke-dasharray="{_DRILL_DASH_NPTH}"' if key == "NPTH" else ""
        for hx, hy, dia in drills.get(key, ()):
            body.append(
                f'<circle cx="{hx:.4f}" cy="{hy:.4f}" r="{dia / 2:.4f}" '
                f'fill="{_DRILL_FILL}" stroke="{_DRILL_STROKE}" '
                f'stroke-width="{_DRILL_STROKE_WIDTH}"{dash}>'
                f"<title>{_drill_title(key, hx, hy, dia)}</title></circle>"
            )
        body.append("</g>")

    rows: list[str] = []
    for i, key in enumerate(keys):
        colour, visible = _LAYER_STYLE.get(key, _DEFAULT_STYLE)
        ty = 18 + i * 20
        mark = "on" if visible else "off"
        label = _DRILL_LABEL.get(key, key)
        rows.append(
            f'<g class="legend-row {mark}" data-layer="{key}" '
            f"onclick=\"(function(g){{g.classList.toggle('off');"
            f"document.getElementById('layer-{key}').classList.toggle('off');}})"
            '(this)">'
            f'<rect x="8" y="{ty - 11}" width="152" height="18" class="hit"/>'
            f'<rect x="12" y="{ty - 8}" width="12" height="12" fill="{colour}" '
            'class="swatch"/>'
            f'<text x="32" y="{ty + 2}">{label}</text>'
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
<rect x="0" y="0" width="{vw:.1f}" height="{vh:.1f}" fill="{_BOARD_BG}"/>
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

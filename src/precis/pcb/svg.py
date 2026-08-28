"""SVG rendering of PCB designs — the sibling export of :mod:`precis.pcb.
gerber` off the SAME copper model (see that module's docstring for the
model shape, and ``tests/test_pcb_gerber.py``'s ``_MODEL`` for the exact
fixture both modules share). Also renders the L3 rubber-band sketch off a
:class:`precis.pcb.ir.PcbIR` — the "money figure" for a paper about
topological place+route: nets as straight connections with layer
assignment, before any geometry is realized.

**Why a model, not a Gerber parse** — see
``docs/backlog/pcb-svg-render.md``: rendering from the same structured
model Gerber is written from means both outputs verify the model
independently; parsing our own Gerber back would only reproduce the
writer's bugs.

**Publication-quality requirements this module exists to satisfy:**
- True vector, never rasterized — plain SVG text, no embedded raster.
- Layers are distinguishable WITHOUT colour: every copper layer gets both
  a hue (:data:`_LAYER_PALETTE`, colourblind-safe Okabe-Ito order) and a
  second, colour-independent cue — a ``stroke-dasharray`` for tracks/silk,
  a hatch angle for pour fills (:func:`_hatch_defs`) — so a greyscale
  printout or a colourblind reader keeps the distinction.
- Deterministic element order: every drawing function iterates its input
  in an EXPLICIT sort key (never raw dict/list order, never a ``set``,
  whose iteration order isn't guaranteed stable across a process — see
  each ``_sorted_*`` helper), so re-rendering an unchanged model or IR
  byte-for-byte reproduces the same SVG text — a regenerated paper figure
  diffs cleanly.
- Renderable subsets (:data:`DEFAULT_INCLUDE`, the ``layers=`` filter):
  single layer, copper-only, silk-only, for multi-panel figures.
- An mm scale bar (:func:`_scale_bar`) and a configurable palette
  (``palette=`` on both entry points).

**Coordinate convention — deliberately NOT flipped.** SVG coordinates here
are the model's mm coordinates verbatim (no ``scale(1,-1)``): the simplest
choice that keeps arc sweep-flag arithmetic (:func:`_arc_flags`) and text
placement both trivially correct, at the cost of SVG's native y-down axis
reading as "board y increases toward the bottom of the image" rather than
mathematical "up". A caller wanting the conventional top-down view can
wrap the returned markup in its own ``<g transform="scale(1,-1)
translate(0,-H)">`` — a presentation choice, not a geometry one, so it
belongs at the call site, not baked into every element here.

**Pad and silkscreen geometry are intentionally out of scope for the
handler-wired board render** (not this module — :func:`render_board` DOES
support both generically, exercised by ``tests/test_pcb_svg.py`` against a
synthetic model). The store has no data source for either yet: no
component in a real design carries resolved, instance-transformed pad
shapes (:mod:`precis.pcb.export`'s own DSN writer only ever emits
placeholder-pitch or footprint-CENTROID pin offsets, never rotated real
pad geometry — see its ``_pin_offsets``), and there is no silkscreen table
at all. ``handlers/pcb.py``'s ``view='svg'`` board render therefore
renders outline + realized copper (tracks/vias/pours) only, honestly
matching what the store can supply today.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from precis.pcb import ir as pcb_ir

# ── palette + non-colour layer cues ─────────────────────────────────────
# Okabe-Ito colourblind-safe palette, reordered so the two layers compared
# most often (an outer pair, e.g. F.Cu/B.Cu) land maximally apart in hue;
# pure black is last (reserved for outline/silk, so an index-0 layer never
# visually collides with them).
_LAYER_PALETTE: tuple[str, ...] = (
    "#E69F00",  # orange
    "#0072B2",  # blue
    "#009E73",  # bluish green
    "#D55E00",  # vermillion
    "#CC79A7",  # reddish purple
    "#56B4E9",  # sky blue
    "#F0E442",  # yellow
    "#000000",  # black (only reached past 7 layers)
)
#: Per-layer stroke style for tracks/silk — the colour-independent cue.
_DASH_PATTERNS: tuple[str, ...] = ("", "5,2", "1,2", "6,2,1,2", "2,2", "8,1,1,1")
#: Per-layer pour-fill hatch angle (degrees) + spacing (mm) — likewise
#: colour-independent; cycling BOTH means adjacent layer indices never
#: share an angle+spacing pair even past one cycle length.
_HATCH_ANGLES: tuple[float, ...] = (45.0, -45.0, 0.0, 90.0, 22.5, -22.5)
_HATCH_SPACING: tuple[float, ...] = (1.0, 1.4, 0.7, 1.8)

#: Every renderable board-level part; ``include=`` restricts to a subset
#: (``{'copper'}`` → copper-only, ``{'silk'}`` → silk-only, etc).
DEFAULT_INCLUDE: frozenset[str] = frozenset(
    {"outline", "copper", "pours", "pads", "vias", "silk"}
)

_UNASSIGNED_STROKE = "#999999"
_UNASSIGNED_DASH = "1,1.5"


# ─────────────────────────────────────────────────────────────────────
# low-level formatting — deterministic, byte-stable across re-renders
# ─────────────────────────────────────────────────────────────────────
def _fmt(v: float) -> str:
    """Fixed 4-decimal (0.1 µm) formatting, trailing zeros trimmed, never
    scientific notation — the same numeric input always yields the same
    string, which is what makes :func:`render_board`/:func:`render_sketch`
    reproducible byte-for-byte."""
    s = f"{float(v):.4f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _pt(p: Iterable[float]) -> str:
    x, y = p
    return f"{_fmt(x)},{_fmt(y)}"


def _esc(s: str) -> str:
    return _xml_escape(str(s))


# ─────────────────────────────────────────────────────────────────────
# arcs — true SVG elliptical-arc commands, never a polyline approximation
# ─────────────────────────────────────────────────────────────────────
def _arc_flags(
    start: tuple[float, float],
    end: tuple[float, float],
    center: tuple[float, float],
    cw: bool,
) -> tuple[float, int, int]:
    """(radius, large-arc-flag, sweep-flag) for the SVG ``A`` command.

    SVG's sweep-flag is defined purely by the raw coordinate values passed
    to the path (positive-angle = the ``x=cx+r·cosθ, y=cy+r·sinθ``
    parametrization increasing θ) — independent of any transform later
    applied to the element, so this can be computed directly in the
    model's own mm coordinates. In that (unflipped, y-up-reads-as-"up")
    frame, increasing θ is the standard-math CCW direction, which is
    exactly the *opposite* of Gerber's ``cw`` flag (G02) semantics — hence
    ``sweep_flag = 0 if cw else 1``.
    """
    sx, sy = start
    ex, ey = end
    cx, cy = center
    r = math.hypot(sx - cx, sy - cy)
    sweep_flag = 0 if cw else 1
    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    delta = (a1 - a0) % (2 * math.pi) if sweep_flag else (a0 - a1) % (2 * math.pi)
    large_arc_flag = 1 if delta > math.pi + 1e-9 else 0
    return r, large_arc_flag, sweep_flag


def _stroke_path_d(segments: list[dict[str, Any]]) -> str:
    """One SVG path ``d`` string for a line/arc-chain draw (a track or a
    silkscreen stroke) — mirrors :func:`precis.pcb.gerber._emit_stroke`'s
    walk (a fresh ``M`` only when the next segment doesn't continue from
    the current point), so the same draw list always yields the same
    topology in both exports."""
    parts: list[str] = []
    cur: tuple[float, float] | None = None
    for seg in segments:
        start = (float(seg["start"][0]), float(seg["start"][1]))
        end = (float(seg["end"][0]), float(seg["end"][1]))
        if cur != start:
            parts.append(f"M {_pt(start)}")
        if seg.get("shape") == "arc":
            center = (float(seg["center"][0]), float(seg["center"][1]))
            cw = bool(seg.get("cw", True))
            r, large, sweep = _arc_flags(start, end, center, cw)
            parts.append(f"A {_fmt(r)} {_fmt(r)} 0 {large} {sweep} {_pt(end)}")
        else:
            parts.append(f"L {_pt(end)}")
        cur = end
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────
# element builders
# ─────────────────────────────────────────────────────────────────────
def _stroke_el(
    draw: dict[str, Any], *, stroke: str, dasharray: str, default_width_mm: float
) -> str:
    d = _stroke_path_d(draw.get("segments") or [])
    if not d:
        return ""
    width = float(draw.get("width_mm", default_width_mm))
    dash = f' stroke-dasharray="{dasharray}"' if dasharray else ""
    return (
        f'<path d="{d}" fill="none" stroke="{stroke}" '
        f'stroke-width="{_fmt(width)}" stroke-linecap="round" '
        f'stroke-linejoin="round"{dash}/>'
    )


def _pad_el(pad: dict[str, Any], *, fill: str) -> str:
    shape = pad.get("shape", "circle")
    x, y = float(pad["x"]), float(pad["y"])
    if shape == "circle":
        r = float(pad["w"]) / 2
        return f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="{_fmt(r)}" fill="{fill}"/>'
    w = float(pad["w"])
    h = float(pad.get("h", pad["w"]))
    rx = min(w, h) / 2 if shape == "obround" else 0.0
    return (
        f'<rect x="{_fmt(x - w / 2)}" y="{_fmt(y - h / 2)}" '
        f'width="{_fmt(w)}" height="{_fmt(h)}" rx="{_fmt(rx)}" ry="{_fmt(rx)}" '
        f'fill="{fill}"/>'
    )


def _via_el(item: dict[str, Any]) -> str:
    x, y = float(item["x"]), float(item["y"])
    r_pad = float(item["dia_mm"]) / 2
    r_drill = float(item["drill_mm"]) / 2
    return (
        f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="{_fmt(r_pad)}" '
        f'fill="#808080" stroke="#000000" stroke-width="0.02"/>'
        f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="{_fmt(r_drill)}" fill="#ffffff"/>'
    )


def _pour_el(item: dict[str, Any], *, pattern_id: str, edge_color: str) -> str:
    poly = item.get("polygon") or []
    if not poly:
        return ""
    pts = " ".join(_pt((float(p[0]), float(p[1]))) for p in poly)
    return (
        f'<polygon points="{pts}" fill="url(#{pattern_id})" '
        f'stroke="{edge_color}" stroke-width="0.05"/>'
    )


def _outline_el(outline: Any) -> str:
    if isinstance(outline, dict):
        segments = list(outline.get("segments") or [])
    else:
        pts = [(float(p[0]), float(p[1])) for p in outline]
        if pts and pts[0] != pts[-1]:
            pts = [*pts, pts[0]]
        segments = [
            {"shape": "line", "start": pts[i], "end": pts[i + 1]}
            for i in range(len(pts) - 1)
        ]
    d = _stroke_path_d(segments)
    if not d:
        return ""
    return f'<path d="{d}" fill="none" stroke="#000000" stroke-width="0.1"/>'


_NICE_LENGTHS_MM: tuple[float, ...] = (
    0.5,
    1,
    2,
    5,
    10,
    20,
    50,
    100,
    200,
    500,
)


def _scale_bar(vb_x: float, vb_y: float, vb_w: float, vb_h: float) -> str:
    target = vb_w * 0.2
    length = min(_NICE_LENGTHS_MM, key=lambda n: abs(n - target))
    x0 = vb_x + vb_w * 0.05
    y0 = vb_y + vb_h - vb_h * 0.05
    x1 = x0 + length
    tick = max(vb_h * 0.01, 0.2)
    return (
        '<g class="scale-bar">'
        f'<line x1="{_fmt(x0)}" y1="{_fmt(y0)}" x2="{_fmt(x1)}" y2="{_fmt(y0)}" '
        'stroke="#000000" stroke-width="0.1"/>'
        f'<line x1="{_fmt(x0)}" y1="{_fmt(y0 - tick)}" x2="{_fmt(x0)}" '
        f'y2="{_fmt(y0 + tick)}" stroke="#000000" stroke-width="0.1"/>'
        f'<line x1="{_fmt(x1)}" y1="{_fmt(y0 - tick)}" x2="{_fmt(x1)}" '
        f'y2="{_fmt(y0 + tick)}" stroke="#000000" stroke-width="0.1"/>'
        f'<text x="{_fmt((x0 + x1) / 2)}" y="{_fmt(y0 - tick - 0.3)}" '
        'font-size="1.5" text-anchor="middle" '
        f'font-family="sans-serif">{_fmt(length)} mm</text>'
        "</g>"
    )


def _wrap_svg(vb_x: float, vb_y: float, vb_w: float, vb_h: float, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{_fmt(vb_x)} {_fmt(vb_y)} {_fmt(vb_w)} {_fmt(vb_h)}" '
        f'width="{_fmt(vb_w)}mm" height="{_fmt(vb_h)}mm">\n'
        f"{body}\n"
        "</svg>\n"
    )


def _resolve_palette(
    layer_names: list[str], palette: dict[str, str] | None
) -> dict[str, str]:
    out: dict[str, str] = {}
    for idx, name in enumerate(layer_names):
        out[name] = (palette or {}).get(name) or _LAYER_PALETTE[
            idx % len(_LAYER_PALETTE)
        ]
    return out


def _hatch_defs(indexed_layers: list[tuple[int, str]], colors: dict[str, str]) -> str:
    """``indexed_layers`` is ``[(layer_index_in_the_full_stackup, name),
    ...]`` — NOT ``enumerate()`` over whatever subset is being drawn, since
    :func:`_pattern_id`/:func:`_pour_el` key patterns by each layer's
    STACKUP position so a ``layers=`` subset render still references the
    same pattern id a full render would (byte-stable per layer, not
    per-subset)."""
    if not indexed_layers:
        return ""
    parts = ["<defs>"]
    for idx, name in indexed_layers:
        color = colors[name]
        angle = _HATCH_ANGLES[idx % len(_HATCH_ANGLES)]
        spacing = _HATCH_SPACING[idx % len(_HATCH_SPACING)]
        pid = _pattern_id(idx)
        parts.append(
            f'<pattern id="{pid}" patternUnits="userSpaceOnUse" '
            f'width="{_fmt(spacing)}" height="{_fmt(spacing)}" '
            f'patternTransform="rotate({_fmt(angle)})">'
            f'<rect width="{_fmt(spacing)}" height="{_fmt(spacing)}" '
            f'fill="{color}" fill-opacity="0.15"/>'
            f'<line x1="0" y1="0" x2="0" y2="{_fmt(spacing)}" '
            f'stroke="{color}" stroke-width="{_fmt(spacing * 0.4)}"/>'
            "</pattern>"
        )
    parts.append("</defs>")
    return "".join(parts)


def _pattern_id(layer_index: int) -> str:
    return f"pcb-hatch-{layer_index}"


# ─────────────────────────────────────────────────────────────────────
# deterministic sort keys — never rely on input list/dict order
# ─────────────────────────────────────────────────────────────────────
def _copper_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    segs = item.get("segments")
    if segs:
        first = segs[0]["start"]
    else:
        poly = item.get("polygon")
        first = poly[0] if poly else (item.get("x", 0.0), item.get("y", 0.0))
    return (str(item.get("net", "")), float(first[0]), float(first[1]))


def _pad_sort_key(pad: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(pad.get("layer", "")),
        str(pad.get("net", "")),
        float(pad["x"]),
        float(pad["y"]),
    )


def _via_layer_names(item: dict[str, Any], all_layers: list[str]) -> list[str]:
    """Copper layers a via flashes on — mirrors :func:`precis.pcb.gerber.
    _via_layers` (duplicated, not imported: that helper is private to its
    own module, same "duplicated on purpose" call :mod:`precis.pcb.drc`
    already made for the identical reason)."""
    if item.get("layers"):
        return list(item["layers"])
    span = item.get("span")
    if span:
        i0, i1 = all_layers.index(span[0]), all_layers.index(span[1])
        lo, hi = min(i0, i1), max(i0, i1)
        return all_layers[lo : hi + 1]
    return list(all_layers)


def _bbox_mm(model: dict[str, Any]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    outline = model.get("outline")
    if outline:
        if isinstance(outline, dict):
            for seg in outline.get("segments") or []:
                xs += [float(seg["start"][0]), float(seg["end"][0])]
                ys += [float(seg["start"][1]), float(seg["end"][1])]
        else:
            for p in outline:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
    if not xs:
        for item in model.get("copper") or []:
            for seg in item.get("segments") or []:
                xs += [float(seg["start"][0]), float(seg["end"][0])]
                ys += [float(seg["start"][1]), float(seg["end"][1])]
            for p in item.get("polygon") or []:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
            if item.get("ctype") == "via":
                xs.append(float(item["x"]))
                ys.append(float(item["y"]))
        for pad in model.get("pads") or []:
            xs.append(float(pad["x"]))
            ys.append(float(pad["y"]))
    if not xs:
        return (0.0, 0.0, 10.0, 10.0)
    return (min(xs), min(ys), max(xs), max(ys))


# ─────────────────────────────────────────────────────────────────────
# L5: the realized-copper board render
# ─────────────────────────────────────────────────────────────────────
def render_board(
    model: dict[str, Any],
    *,
    layers: Iterable[str] | None = None,
    include: Iterable[str] | None = None,
    palette: dict[str, str] | None = None,
    scale_bar: bool = True,
    title: str | None = None,
    margin_mm: float = 2.0,
) -> str:
    """Render realized copper (:mod:`precis.pcb.gerber`'s model shape) as
    publication-quality SVG. ``layers=`` restricts to a layer-name subset
    (single-layer figures); ``include=`` restricts to a subset of
    :data:`DEFAULT_INCLUDE` (``{'copper'}`` for copper-only, ``{'silk'}``
    for silk-only, etc). Element order is fixed regardless of input list
    order (see the ``_sorted_*``/``_*_sort_key`` helpers) so re-rendering
    an unchanged model is byte-identical."""
    all_layers = [str(x) for x in model.get("layers") or []]
    layer_filter = set(layers) if layers is not None else None
    sel_layers = (
        all_layers
        if layer_filter is None
        else [n for n in all_layers if n in layer_filter]
    )
    inc = DEFAULT_INCLUDE if include is None else frozenset(include)
    colors = _resolve_palette(all_layers, palette)

    minx, miny, maxx, maxy = _bbox_mm(model)
    vb_x = minx - margin_mm
    vb_y = miny - margin_mm
    vb_w = (maxx - minx) + 2 * margin_mm
    vb_h = (maxy - miny) + 2 * margin_mm

    # Pattern defs are the only element referencing per-layer hue outside
    # the "pours" bucket; scope them to the SAME set the pour loop below
    # actually draws so an unrelated subset render (e.g. include={'silk'})
    # never leaks another layer's colour into the SVG text.
    body: list[str] = (
        [_hatch_defs([(all_layers.index(n), n) for n in sel_layers], colors)]
        if "pours" in inc
        else []
    )

    if "outline" in inc and model.get("outline"):
        body.append(_outline_el(model["outline"]))

    copper_items = list(model.get("copper") or [])
    if "copper" in inc:
        for layer in sel_layers:
            idx = all_layers.index(layer)
            tracks = sorted(
                (
                    c
                    for c in copper_items
                    if c.get("ctype") == "track" and c.get("layer") == layer
                ),
                key=_copper_sort_key,
            )
            for item in tracks:
                el = _stroke_el(
                    item,
                    stroke=colors[layer],
                    dasharray=_DASH_PATTERNS[idx % len(_DASH_PATTERNS)],
                    default_width_mm=0.2,
                )
                if el:
                    body.append(el)

    if "pours" in inc:
        for layer in sel_layers:
            idx = all_layers.index(layer)
            pours = sorted(
                (
                    c
                    for c in copper_items
                    if c.get("ctype") == "pour" and c.get("layer") == layer
                ),
                key=_copper_sort_key,
            )
            for item in pours:
                el = _pour_el(
                    item, pattern_id=_pattern_id(idx), edge_color=colors[layer]
                )
                if el:
                    body.append(el)

    if "pads" in inc:
        pads = sorted(
            (
                p
                for p in model.get("pads") or []
                if str(p.get("layer")) in set(sel_layers)
            ),
            key=_pad_sort_key,
        )
        for pad in pads:
            body.append(_pad_el(pad, fill=colors.get(str(pad.get("layer")), "#808080")))

    if "vias" in inc:
        vias = sorted(
            (c for c in copper_items if c.get("ctype") == "via"),
            key=_copper_sort_key,
        )
        for item in vias:
            via_layers = _via_layer_names(item, all_layers)
            if any(vl in sel_layers for vl in via_layers):
                body.append(_via_el(item))

    if "silk" in inc:
        silk = model.get("silkscreen") or {}
        for side in ("top", "bottom"):
            draws = sorted(
                (list(silk.get(side) or [])), key=lambda d: _copper_sort_key(d)
            )
            dash = "" if side == "top" else "1,1"
            for draw in draws:
                el = _stroke_el(
                    draw, stroke="#000000", dasharray=dash, default_width_mm=0.15
                )
                if el:
                    body.append(el)

    if scale_bar:
        body.append(_scale_bar(vb_x, vb_y, vb_w, vb_h))
    if title:
        body.append(
            f'<text x="{_fmt(vb_x + 1)}" y="{_fmt(vb_y + 3)}" font-size="2.5" '
            f'font-family="sans-serif">{_esc(title)}</text>'
        )

    return _wrap_svg(vb_x, vb_y, vb_w, vb_h, "".join(p for p in body if p))


# ─────────────────────────────────────────────────────────────────────
# L3: the rubber-band sketch — the paper's "money figure"
# ─────────────────────────────────────────────────────────────────────
def render_sketch(
    ir: pcb_ir.PcbIR,
    *,
    palette: dict[str, str] | None = None,
    scale_bar: bool = True,
    title: str | None = None,
    margin_mm: float = 3.0,
) -> str:
    """Render a :class:`precis.pcb.ir.PcbIR` as a minimal L3 rubber-band
    sketch: placed components as labelled markers + straight-line
    connections coloured/dashed by :attr:`PcbIR.seg_layer` (grey dotted
    where no layer is assigned yet). **Not** the full L2 combinatorial
    embedding (rotation order / side choices aren't drawn) — see the
    module docstring and this build's report for what a fuller L2/L3
    figure would still need.

    Deterministic order: segments iterate ``range(ir.n_segments)`` (the
    IR's own construction order, already stable — see
    :mod:`precis.pcb.ir`'s array-not-object discipline); components
    iterate sorted by refdes so the input graph's own (frequently
    ORDER-BY-less, per :mod:`precis.pcb.session`'s docstring) instance
    order can't perturb the figure.
    """
    layer_names = [str(layer.get("name") or i) for i, layer in enumerate(ir.stackup)]
    colors = _resolve_palette(layer_names, palette)

    xs: list[float] = []
    ys: list[float] = []
    for i in range(ir.n_instances):
        x, y = float(ir.inst_x[i]), float(ir.inst_y[i])
        if x == x and y == y:  # NaN != NaN
            xs.append(x)
            ys.append(y)
    if not xs:
        xs, ys = [0.0], [0.0]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    vb_x, vb_y = minx - margin_mm, miny - margin_mm
    vb_w = max(maxx - minx, 0.0) + 2 * margin_mm
    vb_h = max(maxy - miny, 0.0) + 2 * margin_mm

    body: list[str] = []
    for seg_id in range(ir.n_segments):
        points = pcb_ir.segment_points(ir, seg_id)
        if points is None:
            continue
        (x0, y0), (x1, y1) = points
        layer = int(ir.seg_layer[seg_id])
        if layer == pcb_ir.UNSET_LAYER:
            stroke, dash = _UNASSIGNED_STROKE, _UNASSIGNED_DASH
        else:
            stroke = colors.get(layer_names[layer], _UNASSIGNED_STROKE)
            dash = _DASH_PATTERNS[layer % len(_DASH_PATTERNS)]
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        body.append(
            f'<line x1="{_fmt(x0)}" y1="{_fmt(y0)}" x2="{_fmt(x1)}" y2="{_fmt(y1)}" '
            f'stroke="{stroke}" stroke-width="0.15"{dash_attr}/>'
        )

    marker = max(0.6, min(vb_w, vb_h) * 0.02)
    for i in sorted(range(ir.n_instances), key=lambda i: str(ir.instance_refdes[i])):
        x, y = float(ir.inst_x[i]), float(ir.inst_y[i])
        if x != x or y != y:
            continue
        refdes = str(ir.instance_refdes[i])
        body.append(
            f'<rect x="{_fmt(x - marker / 2)}" y="{_fmt(y - marker / 2)}" '
            f'width="{_fmt(marker)}" height="{_fmt(marker)}" '
            'fill="#ffffff" stroke="#000000" stroke-width="0.08"/>'
        )
        body.append(
            f'<text x="{_fmt(x + marker)}" y="{_fmt(y)}" '
            f'font-size="{_fmt(marker * 1.5)}" font-family="sans-serif">'
            f"{_esc(refdes)}</text>"
        )

    if scale_bar:
        body.append(_scale_bar(vb_x, vb_y, vb_w, vb_h))
    if title:
        body.append(
            f'<text x="{_fmt(vb_x + 1)}" y="{_fmt(vb_y + 3)}" font-size="2.5" '
            f'font-family="sans-serif">{_esc(title)}</text>'
        )

    return _wrap_svg(vb_x, vb_y, vb_w, vb_h, "".join(p for p in body if p))


__all__ = ["DEFAULT_INCLUDE", "render_board", "render_sketch"]

"""SVG of a routed sanity board — outline, pads, tracks, vias, one colour
per copper layer — for looking at a variant without the viewer. Pure
string building; the geometry is the realize result as-is."""

from __future__ import annotations

import math
from typing import Any

from precis.pcb import padplace
from precis.pcb.ir import UNSET_LAYER

#: F.Cu, In1.Cu, In2.Cu, B.Cu.
_LAYER_COLOURS = ("#d33", "#d90", "#3a3", "#36c")
_SCALE = 8.0  # px per mm


def _arc_path(seg: dict[str, Any], flip: float) -> str:
    (sx, sy), (ex, ey), (cx, cy) = seg["start"], seg["end"], seg["center"]
    r = math.hypot(sx - cx, sy - cy)
    # y is flipped for the picture, so a counterclockwise (math) arc is
    # clockwise on screen: sweep-flag 1.
    sweep = 0 if seg["cw"] else 1
    return (
        f"M{sx:.3f} {flip - sy:.3f} A{r:.3f} {r:.3f} 0 0 {sweep} "
        f"{ex:.3f} {flip - ey:.3f}"
    )


def _segment_path(seg: dict[str, Any], flip: float) -> str:
    if seg["shape"] == "arc":
        return _arc_path(seg, flip)
    (sx, sy), (ex, ey) = seg["start"], seg["end"]
    return f"M{sx:.3f} {flip - sy:.3f} L{ex:.3f} {flip - ey:.3f}"


def svg(
    *,
    outline: list[list[float]],
    instances: list[dict[str, Any]],
    footprint: dict[str, Any],
    tracks: list[Any],
    vias: list[Any],
    title: str,
) -> str:
    """``instances`` are the placed ``{refdes, x, y, rot, layer}`` dicts;
    ``tracks``/``vias`` are ``RealizeResult.tracks``/``.vias``."""
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    flip = max(ys)
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w * _SCALE:.0f}" '
        f'height="{h * _SCALE + 14:.0f}" viewBox="0 -4 {w:.3f} {h + 7:.3f}">',
        f'<text x="1" y="-1.2" font-size="2.2" font-family="monospace">{title}</text>',
        f'<rect x="0" y="0" width="{w:.3f}" height="{h:.3f}" fill="#fafafa" '
        'stroke="#000" stroke-width="0.2"/>',
    ]
    for inst in instances:
        pads, _ = padplace.place_footprint_pads(
            footprint["pads"], inst, layers=["F.Cu"], pin_map=footprint["pin_map"]
        )
        for p in pads:
            out.append(
                f'<rect x="{p["x"] - p["w"] / 2:.3f}" '
                f'y="{flip - p["y"] - p["h"] / 2:.3f}" width="{p["w"]:.3f}" '
                f'height="{p["h"]:.3f}" fill="#c9a" opacity="0.8"/>'
            )
    for t in tracks:
        layer = 0 if t.layer == UNSET_LAYER else int(t.layer)
        colour = _LAYER_COLOURS[layer % len(_LAYER_COLOURS)]
        d = " ".join(_segment_path(s, flip) for s in t.segments)
        out.append(
            f'<path d="{d}" fill="none" stroke="{colour}" '
            f'stroke-width="{t.width_mm:.3f}" stroke-linecap="round" opacity="0.8"/>'
        )
    for v in vias:
        out.append(
            f'<circle cx="{v.x:.3f}" cy="{flip - v.y:.3f}" r="{v.dia_mm / 2:.3f}" '
            f'fill="#fff" stroke="#000" stroke-width="0.1"/>'
        )
    out.append("</svg>")
    return "\n".join(out)

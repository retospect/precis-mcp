"""Vector routing masks + pad-bank obstacle wires (topoplace).

The board minus its obstacles, as GEOMETRY, not logs: per layer, the
route-block mask (what a wire centerline may not cross) and the
via-prevent mask (where a via center may not sit), plus the immobile
obstacle WIRES — pad-bank spines a net's corridor is checked against
with the same width-aware crossing/order predicate the 2b detangler
uses for net-vs-net.  No rasterization anywhere: seeds are pad outline
polygons, kernels are Minkowski growths, fusion is shapely union,
filtering is a vector morphological opening (buffer out-and-back).

Two kernels over the same seeds (Reto's mask formalization):
- route-block = min_clearance + min_wire/2 grown pad outline, per layer
  (SMD pads seed their mount side only, TH pads every layer), plus the
  zones blocking that layer — the "pad blocking bars" debug layer.
- via-prevent = wicking(class) + margin + via_annulus_radius grown pad
  outline on BOTH outers (the barrel spans the stackup), plus zones,
  eroded from the board outline — the "via prohibited" debug layer.

Consumers: the topo route stage (files payload + siting pre-filter,
later the placement objective via real-estate measures) and the web
pcb view's hidden support layers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

from precis.pcb.topoplace.design import TopoDesign

#: Wicking-distance table, keyed by pad size class (the package class the
#: pad belongs to is not modeled in TopoDesign, so the pad's own extent
#: is the proxy — small pads are small parts, and solder wicking under a
#: 0402 is the DFM risk that motivates the kernel).  Values are the
#: distance a via's annulus must keep from the pad to avoid wicking.
_WICKING_BY_PAD_MM = (
    (1.2, 0.35),  # <= ~0402 pads: no vias under the part at all
    (2.5, 0.25),  # 0603-ish
    (math.inf, 0.15),  # 1206+ and anything bigger keeps a via-able middle
)

_ANNULUS_MARGIN_MM = 0.05


def _wicking_mm(pad_w: float, pad_h: float) -> float:
    for limit, w in _WICKING_BY_PAD_MM:
        if max(pad_w, pad_h) <= limit:
            return w
    return 0.15


@dataclass(frozen=True, slots=True)
class Spine:
    """One immobile obstacle wire: a polyline + the width it blocks."""

    pts: tuple[tuple[float, float], ...]
    width_mm: float
    refdes: str
    layer: int  # the layer index this spine obstructs (SMD side; TH -> -1 = all)


@dataclass(frozen=True, slots=True)
class LayerMasks:
    route_block: tuple[tuple[tuple[float, float], ...], ...]
    spines: tuple[Spine, ...]
    route_block_free: float  # fraction of the outline NOT route-blocked
    via_prevent_free: float  # fraction of the outline a via center may use


@dataclass(frozen=True, slots=True)
class BoardMasks:
    layers: tuple[LayerMasks, ...]
    via_prevent: tuple[tuple[tuple[float, float], ...], ...]  # board-level rings


def _pad_polys(
    d: TopoDesign, pose_a: np.ndarray
) -> list[tuple[int, int, Polygon, float, float]]:
    """World pad outlines at the pose: ``(part, side, poly, w, h)`` per
    pin, plus the non-primary footprint pads (the realise stage's
    pad_sets construction, refactored — same rows, same side rule)."""
    rows: list[tuple[int, int, Polygon, float, float]] = []
    for src in ("pin", "extra"):
        if src == "pin":
            xy, wh, part_of, side_of = d.pin_xy, d.pin_wh, d.pin_part, d.pin_side
            n = len(d.pin_xy)
        else:
            xy, wh = d.pad_extra_xy, d.pad_extra_wh
            part_of, side_of = d.pad_extra_part, d.pad_extra_side
            n = len(d.pad_extra_xy)
        for p in range(n):
            part = int(part_of[p])
            if part >= len(pose_a) or not np.isfinite(pose_a[part, :2]).all():
                continue
            x, y = float(xy[p][0]), float(xy[p][1])
            px = float(pose_a[part, 0]) + x  # part-frame offsets are
            py = float(pose_a[part, 1]) + y  # translation-only here (rot
            # folds into pin_xy at design build); refine if rot != 0.
            w, h = float(wh[p][0]), float(wh[p][1])
            rows.append((part, int(side_of[p]), _rect(px, py, w, h), w, h))
    return rows


def _rect(cx: float, cy: float, w: float, h: float) -> Polygon:
    return Polygon(
        [
            (cx - w / 2, cy - h / 2),
            (cx + w / 2, cy - h / 2),
            (cx + w / 2, cy + h / 2),
            (cx - w / 2, cy + h / 2),
        ]
    )


def _rings(geom: Any) -> tuple[tuple[tuple[float, float], ...], ...]:
    """Exterior rings of a (multi)polygon, open (no repeated closer)."""
    polys: list[Any] = []
    if geom is None or geom.is_empty:
        return ()
    if geom.geom_type == "Polygon":
        polys = [geom]
    elif geom.geom_type == "MultiPolygon":
        polys = list(geom.geoms)
    out = []
    for g in polys:
        coords = tuple((float(x), float(y)) for x, y in g.exterior.coords[:-1])
        if len(coords) >= 3:
            out.append(coords)
    return tuple(out)


def _opening(geom: Any, radius: float) -> Any:
    """Vector morphological opening: erode then dilate.  Absorbs features
    narrower than ``radius`` (sub-kernel slivers between fused pads),
    keeping the mask's effective corridors."""
    if geom is None or geom.is_empty or radius <= 0:
        return geom
    return geom.buffer(-radius).buffer(radius)


def build_masks(
    d: TopoDesign,
    pose: Any,
    *,
    n_layers: int,
    min_wire_mm: float = 0.15,
    min_clearance_mm: float = 0.15,
    via_dia_mm: float = 0.6,
    zone_polys: tuple[tuple[Any, tuple[int, ...]], ...] = (),
) -> BoardMasks:
    """The two-kernel mask set + pad-bank spines for one pose.

    ``zone_polys`` carries ``(world_ring_polygon, layer_tuple)`` for the
    zones that obstruct routing (keepout/mounting_hole/label_keepout) —
    the caller builds them from ``d.zones`` at the routed pose, exactly
    the rings the via-siting walk already honors."""
    pose_a = np.asarray(pose, dtype=float)
    rb_growth = min_clearance_mm + min_wire_mm / 2.0
    annulus = via_dia_mm / 2.0 + _ANNULUS_MARGIN_MM
    pads = _pad_polys(d, pose_a)
    outline = (
        Polygon(np.asarray(d.outline, dtype=float)) if len(d.outline) >= 3 else None
    )
    outers = {0, n_layers - 1}

    # ── via-prevent: every pad on BOTH outers (the barrel spans), grown
    # by wicking + annulus; unioned with zones; eroded from the outline.
    vp_seeds: list[Any] = []
    for _part, _side, poly, w, h in pads:
        vp_seeds.append(poly.buffer(_wicking_mm(w, h) + annulus))
    for zg, zl in zone_polys:
        if set(zl) & outers or not zl:
            vp_seeds.append(zg)
    via_prevent = unary_union(vp_seeds) if vp_seeds else None
    if via_prevent is not None and outline is not None:
        via_prevent = via_prevent.intersection(outline)
    via_prevent = _opening(via_prevent, via_dia_mm / 2.0)

    layers: list[LayerMasks] = []
    for layer in range(n_layers):
        rb_seeds: list[Any] = []
        spines: list[Spine] = []
        banks: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
        for part, side, poly, w, h in pads:
            if side == 0 and layer != 0:
                continue  # top SMD pad: only F.Cu is its layer
            if side == 1 and layer != n_layers - 1:
                continue  # bottom SMD pad: only B.Cu
            rb_seeds.append(poly.buffer(rb_growth))
            key = (part, -1 if side == 2 else layer)
            banks.setdefault(key, []).append(
                (float(poly.centroid.x), float(poly.centroid.y), w, h)
            )
        for zg, zl in zone_polys:
            if zl and layer not in zl:
                continue
            rb_seeds.append(zg)
        route_block = unary_union(rb_seeds) if rb_seeds else None
        if route_block is not None and outline is not None:
            route_block = route_block.intersection(outline)
        route_block = _opening(route_block, min_wire_mm / 2.0)
        # pad-bank spines: collinear pad rows become one immobile wire
        for (part, _side), row in banks.items():
            for bank in _collinear_banks(row):
                xs = [p[0] for p in bank]
                ys = [p[1] for p in bank]
                width = max(p[3] for p in bank) + 2 * rb_growth
                spines.append(
                    Spine(
                        pts=(
                            (min(xs), sum(ys) / len(ys)),
                            (max(xs), sum(ys) / len(ys)),
                        ),
                        width_mm=round(width, 4),
                        refdes=str(d.parts[part]),
                        layer=layer,
                    )
                )
        area = float(outline.area) if outline is not None else 0.0
        rb_free = (
            1.0 - float(route_block.area) / area
            if route_block is not None and area
            else 1.0
        )
        vp_free = (
            1.0 - float(via_prevent.area) / area
            if via_prevent is not None and area
            else 1.0
        )
        layers.append(
            LayerMasks(
                route_block=_rings(route_block),
                spines=tuple(spines),
                route_block_free=round(rb_free, 4),
                via_prevent_free=round(vp_free, 4),
            )
        )
    return BoardMasks(layers=tuple(layers), via_prevent=_rings(via_prevent))


def _collinear_banks(
    row: list[tuple[float, float, float, float]], tol_mm: float = 0.6
) -> list[list[tuple[float, float, float, float]]]:
    """Group same-y pads into banks: centers within ``tol_mm`` vertically
    and within 3 mm of the previous pad horizontally chain together."""
    row = sorted(row)
    banks: list[list[tuple[float, float, float, float]]] = []
    cur: list[tuple[float, float, float, float]] = []
    for p in row:
        if cur:
            prev = cur[-1]
            if (
                abs(p[1] - prev[1]) > tol_mm
                or p[0] - prev[0] > 3.0 + (p[2] + prev[2]) / 2.0
            ):
                banks.append(cur)
                cur = []
        cur.append(p)
    if cur:
        banks.append(cur)
    return [b for b in banks if len(b) >= 1]


_DBG_ROUTE_BLOCK = ("#d62728", "#9467bd", "#8c564b", "#e377c2")
_DBG_VIA_PREVENT = "#7f7f7f"
_DBG_SPINE = "#111111"


def masks_svg(
    masks: BoardMasks,
    outline: Any,
    layer_names: tuple[str, ...],
    *,
    spines: bool = True,
) -> str:
    """One debug SVG: per layer a named <g id="dbg-route-block-<name>">
    with the route-block rings + pad-bank spines, then a
    ``dbg-via-prevent`` group — the hidden support layers the web view
    will mirror.  Groups carry data-free attributes only; toggling is
    the viewer's job."""
    out_arr = np.asarray(outline, dtype=float) if len(outline) >= 3 else None
    if out_arr is not None:
        lo, hi = out_arr.min(axis=0) - 2.0, out_arr.max(axis=0) + 2.0
    else:
        lo, hi = np.zeros(2), np.full(2, 50.0)

    def _poly(ring: tuple[tuple[float, float], ...], color: str, opacity: float) -> str:
        s = " ".join(f"{x:.2f},{y:.2f}" for x, y in ring)
        return (
            f'<polygon points="{s}" fill="{color}" fill-opacity="{opacity}" '
            f'stroke="{color}" stroke-width="0.05"/>'
        )

    body: list[str] = []
    if out_arr is not None:
        s = " ".join(f"{x:.2f},{y:.2f}" for x, y in out_arr)
        body.append(
            f'<polygon points="{s}" fill="none" stroke="#000" stroke-width="0.3"/>'
        )
    for i, lm in enumerate(masks.layers):
        lname = layer_names[i] if i < len(layer_names) else f"L{i}"
        color = _DBG_ROUTE_BLOCK[i % len(_DBG_ROUTE_BLOCK)]
        parts = [f'<g id="dbg-route-block-{lname}" data-layer="{lname}">']
        parts.extend(_poly(r, color, 0.25) for r in lm.route_block)
        if spines:
            for sp in lm.spines:
                s = " ".join(f"{x:.2f},{y:.2f}" for x, y in sp.pts)
                parts.append(
                    f'<polyline points="{s}" fill="none" stroke="{_DBG_SPINE}" '
                    f'stroke-width="{sp.width_mm:.2f}" stroke-opacity="0.5"/>'
                )
        parts.append("</g>")
        body.extend(parts)
    body.append('<g id="dbg-via-prevent" data-layer="board">')
    body.extend(_poly(r, _DBG_VIA_PREVENT, 0.2) for r in masks.via_prevent)
    body.append("</g>")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{lo[0]:.2f} '
        f'{lo[1]:.2f} {hi[0] - lo[0]:.2f} {hi[1] - lo[1]:.2f}">'
        + "".join(body)
        + "</svg>\n"
    )


__all__ = ["BoardMasks", "LayerMasks", "Spine", "build_masks", "masks_svg"]

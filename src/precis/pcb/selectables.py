"""The catalogue of board objects a viewer can select — pure, no DB, no I/O.

One selection model for the fab-SVG viewer and the design chat: every
selectable thing (component instance, pad, via, silkscreen group, board-outline
segment, fiducial, net) is a :class:`SelectObject` carrying

* ``identity`` — a JSON-safe dict with ``kind`` and ``key`` plus kind fields.
  The client echoes it verbatim in the chat context and the server rebuilds
  the catalogue for the shown snapshot and requires exact equality, so an
  identity is an equality-checked echo, never a lookup handle someone can
  forge. ``key`` is unique only **within one snapshot token**: a pad's
  ``#2`` ordinal or a via's coordinates mean nothing across board edits.
* ``hits`` — the shapes the viewer lays over the picture to catch clicks
  (``circle`` / ``rect`` / ``poly`` / ``stroke``), each tagged with the
  ``layer-<key>`` group of :func:`precis.pcb.gerber_view.render_fab_svg` it
  belongs to (``""`` for an instance body, which spans layers). Hit geometry
  is deliberately NOT part of the identity: full polygons would bloat every
  chat context.

Keys never use SQL-returned list positions (copper/silk order is not
guaranteed): vias sort before collisions get ordinals; the only ordinal keys
are outline segments (from the stored feature path) and fiducials (grouped
by position in model order).

Numbers in identities are rounded to 4 dp so the JSON echo compares equal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from precis.pcb import gerber, gerber_view

#: Rendering/priority order of the kinds, bottom to top. The viewer paints its
#: hit overlay in this order and the client picks the LAST kind present under
#: the pointer, so a pad beats the part body and the net it sits on.
KIND_ORDER = ("instance", "net", "outline", "silk", "pad", "via", "fiducial")

#: Minimum clickable stroke width (mm) — a 0.1 mm board edge is not hittable.
MIN_HIT_WIDTH_MM = 0.3


@dataclass
class SelectObject:
    identity: dict[str, Any]
    hits: list[dict[str, Any]] = field(default_factory=list)


def _r(v: Any) -> Any:
    # Non-finite floats would serialise as bare NaN/Infinity, which the
    # client's JSON.parse rejects; None keeps the catalogue loadable.
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(float(v), 4) if math.isfinite(v) else None
    return v


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return round(f, 4) if math.isfinite(f) else None


def layer_key(model_layer: str) -> str:
    """``F.Cu`` -> ``F_Cu``: the ``layer-<key>`` group id of the fab SVG."""
    return model_layer.replace(".", "_")


def _unique(base: str, seen: dict[str, int]) -> str:
    n = seen.get(base, 0) + 1
    seen[base] = n
    return base if n == 1 else f"{base}#{n}"


def _poly_hit(layer: str, pts: list[Any]) -> dict[str, Any]:
    return {
        "shape": "poly",
        "layer": layer,
        "points": [[float(p[0]), float(p[1])] for p in pts],
    }


def _stroke_hit(layer: str, pts: list[Any], width: float) -> dict[str, Any]:
    return {
        "shape": "stroke",
        "layer": layer,
        "points": [[float(p[0]), float(p[1])] for p in pts],
        "width": float(width),
    }


def _draw_points(draw: dict[str, Any]) -> list[tuple[float, float]]:
    """A stroke draw's polyline: segment starts + the last end, arcs
    flattened with the same routine the gerber reader uses."""
    pts: list[tuple[float, float]] = []
    for seg in draw.get("segments") or []:
        start = (float(seg["start"][0]), float(seg["start"][1]))
        end = (float(seg["end"][0]), float(seg["end"][1]))
        if not pts or pts[-1] != start:
            pts.append(start)
        if seg.get("shape") == "arc":
            centre = (float(seg["center"][0]), float(seg["center"][1]))
            pts.extend(
                gerber_view._arc_points(
                    start, end, centre, cw=bool(seg.get("cw", True))
                )
            )
        else:
            pts.append(end)
    return pts


def _pad_hit(pad: dict[str, Any], layer: str) -> dict[str, Any]:
    x, y = float(pad["x"]), float(pad["y"])
    if pad.get("shape") == "polygon" and pad.get("poly"):
        return _poly_hit(layer, pad["poly"])
    w = float(pad.get("w") or 0.0)
    if pad.get("shape", "circle") == "circle":
        return {"shape": "circle", "layer": layer, "cx": x, "cy": y, "r": w / 2}
    h = float(pad.get("h", w))
    return {
        "shape": "rect",
        "layer": layer,
        "x": x - w / 2,
        "y": y - h / 2,
        "w": w,
        "h": h,
    }


def _pad_extent(pad: dict[str, Any]) -> tuple[float, float, float, float]:
    if pad.get("shape") == "polygon" and pad.get("poly"):
        xs = [float(p[0]) for p in pad["poly"]]
        ys = [float(p[1]) for p in pad["poly"]]
        return min(xs), min(ys), max(xs), max(ys)
    x, y = float(pad["x"]), float(pad["y"])
    w = float(pad.get("w") or 0.0)
    h = w if pad.get("shape", "circle") == "circle" else float(pad.get("h", w))
    return x - w / 2, y - h / 2, x + w / 2, y + h / 2


def build(
    model: dict[str, Any],
    *,
    instances: list[dict[str, Any]],
    pins: list[dict[str, Any]],
    nets: list[dict[str, Any]],
    components: list[dict[str, Any]],
    identities: list[dict[str, Any]] | None = None,
) -> list[SelectObject]:
    """Every selectable object of ``model`` (the fab-SVG board model).

    ``instances`` / ``pins`` / ``nets`` / ``components`` are the board's raw
    relational rows (retired rows are skipped here too). ``identities`` are
    the per-instance identity dicts the caller already derived (footprint
    block included — :func:`precis_web.pcb_design.capture_view`); without
    them an instance carries only ``instance_id`` / ``refdes``.

    A silkscreen group's ``content`` is the refdes for role ``refdes`` and
    ``None`` elsewhere: the title block's text includes the export date and
    the draws do not carry text. Nothing is recomputed to fill it.
    """
    live_components = {c["component_id"] for c in components if not c.get("retired_at")}
    live = [
        i
        for i in instances
        if not i.get("retired_at") and i.get("component_id") in live_components
    ]
    by_refdes: dict[str, list[dict[str, Any]]] = {}
    for inst in live:
        by_refdes.setdefault(str(inst["refdes"]), []).append(inst)
    unique_inst = {k: v[0] for k, v in by_refdes.items() if len(v) == 1}
    live_nets = [n for n in nets if not n.get("retired_at")]
    net_names: dict[str, list[int]] = {}
    for n in live_nets:
        net_names.setdefault(str(n["name"]), []).append(int(n["net_id"]))

    def net_id(name: Any) -> int | None:
        ids = net_names.get(str(name or ""), [])
        return ids[0] if len(ids) == 1 else None

    pins_by_comp: dict[Any, list[dict[str, Any]]] = {}
    for p in pins:
        if not p.get("retired_at"):
            pins_by_comp.setdefault(p["component_id"], []).append(p)

    layers: list[str] = [str(x) for x in model.get("layers") or []]
    pads = list(model.get("pads") or [])
    real_pads = [p for p in pads if p.get("role") != "fiducial"]

    # -- instances ------------------------------------------------------
    idents = {int(i["instance_id"]): i for i in identities or []}
    inst_objs: dict[int, SelectObject] = {}
    out: dict[str, list[SelectObject]] = {k: [] for k in KIND_ORDER}
    for inst in sorted(live, key=lambda i: int(i["instance_id"])):
        iid = int(inst["instance_id"])
        base = idents.get(iid) or {"instance_id": iid, "refdes": inst["refdes"]}
        obj = SelectObject({**base, "kind": "instance", "key": f"instance:{iid}"})
        inst_objs[iid] = obj
        out["instance"].append(obj)

    # -- pads -----------------------------------------------------------
    seen: dict[str, int] = {}
    boxes: dict[int, list[float]] = {}
    for pad in real_pads:
        refdes = str(pad.get("refdes") or "")
        pin = str(pad.get("pin") or "")
        owner = unique_inst.get(refdes)
        owner_id = int(owner["instance_id"]) if owner is not None else None
        px, py = _r(pad["x"]), _r(pad["y"])
        if owner_id is not None:
            key = _unique(f"pad:{owner_id}:{pin}", seen)
        else:
            key = _unique(f"pad:{refdes}:{pin}:{px:.4f}:{py:.4f}", seen)
        pin_id = pin_name = pad_number = None
        if owner is not None:
            match = [
                p
                for p in pins_by_comp.get(owner["component_id"], [])
                if p["name"] == pin
            ]
            if len(match) == 1:
                pin_id, pin_name, pad_number = (
                    match[0]["pin_id"],
                    match[0]["name"],
                    match[0].get("pad"),
                )
        geometry: dict[str, Any] = {
            "shape": str(pad.get("shape", "circle")),
            "x": px,
            "y": py,
        }
        if pad.get("w") is not None:
            geometry["w"] = _r(pad["w"])
        if geometry["shape"] != "circle" and pad.get("h") is not None:
            geometry["h"] = _r(pad["h"])
        if _num(pad.get("drill")) is not None:
            geometry["drill"] = _num(pad["drill"])
        if geometry["shape"] == "polygon" and pad.get("poly"):
            geometry["vertices"] = len(pad["poly"])
        name = str(pad.get("net") or "")
        mlayer = str(pad.get("layer") or "")
        lkey = layer_key(mlayer)
        out["pad"].append(
            SelectObject(
                {
                    "kind": "pad",
                    "key": key,
                    "instance_id": owner_id,
                    "refdes": refdes,
                    "pin": pin,
                    "pin_id": pin_id,
                    "pin_name": pin_name,
                    "pad_number": pad_number,
                    "net": name or None,
                    "net_id": net_id(name),
                    "layer": mlayer,
                    "geometry": geometry,
                },
                [_pad_hit(pad, lkey)],
            )
        )
        if owner_id is not None:
            x0, y0, x1, y1 = _pad_extent(pad)
            box = boxes.setdefault(owner_id, [math.inf, math.inf, -math.inf, -math.inf])
            box[:] = [
                min(box[0], x0),
                min(box[1], y0),
                max(box[2], x1),
                max(box[3], y1),
            ]
    for iid, (x0, y0, x1, y1) in boxes.items():
        inst_objs[iid].hits.append(
            {"shape": "rect", "layer": "", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
        )

    # -- vias -----------------------------------------------------------
    copper = list(model.get("copper") or [])
    vias = sorted(
        (c for c in copper if c.get("ctype") == "via" and c.get("role") != "fiducial"),
        key=lambda c: (
            float(c["x"]),
            float(c["y"]),
            str(c.get("net") or ""),
            float(c.get("dia_mm") or 0),
            float(c.get("drill_mm") or 0),
        ),
    )
    seen = {}
    for via in vias:
        vx, vy = _r(via["x"]), _r(via["y"])
        vlayers = gerber._via_layers(via, layers)
        name = str(via.get("net") or "")
        dia = float(via.get("dia_mm") or 0.0)
        out["via"].append(
            SelectObject(
                {
                    "kind": "via",
                    "key": _unique(f"via:{vx:.4f}:{vy:.4f}", seen),
                    "net": name or None,
                    "net_id": net_id(name),
                    "x": vx,
                    "y": vy,
                    "dia_mm": _num(via.get("dia_mm")),
                    "drill_mm": _num(via.get("drill_mm")),
                    "layers": vlayers,
                    "fixed": bool(via.get("fixed")),
                },
                [
                    {
                        "shape": "circle",
                        "layer": layer_key(vl),
                        "cx": float(via["x"]),
                        "cy": float(via["y"]),
                        "r": dia / 2,
                    }
                    for vl in vlayers
                ],
            )
        )

    # -- silkscreen -----------------------------------------------------
    groups: dict[tuple[str, str, str], SelectObject] = {}
    for side, silk_layer in (("top", "F_Silkscreen"), ("bottom", "B_Silkscreen")):
        for draw in (model.get("silkscreen") or {}).get(side, []):
            role = str(draw.get("role") or "")
            refdes = str(draw.get("refdes") or "")
            gkey = (silk_layer, role, refdes)
            if gkey not in groups:
                owner = unique_inst.get(refdes) if refdes else None
                anchor: dict[str, Any] = (
                    {
                        "kind": "instance",
                        "instance_id": int(owner["instance_id"]),
                        "refdes": refdes,
                    }
                    if owner is not None
                    else {"kind": "board"}
                )
                groups[gkey] = SelectObject(
                    {
                        "kind": "silk",
                        "key": f"silk:{silk_layer}:{role}:{refdes}",
                        "layer": silk_layer,
                        "role": role,
                        "content": refdes if role == "refdes" and refdes else None,
                        "anchor": anchor,
                    }
                )
            if draw.get("shape") == "region":
                if draw.get("polygon"):
                    groups[gkey].hits.append(_poly_hit(silk_layer, draw["polygon"]))
                continue
            pts = _draw_points(draw)
            if len(pts) >= 2:
                width = float(draw.get("width_mm", gerber.DEFAULT_SILK_WIDTH_MM))
                groups[gkey].hits.append(_stroke_hit(silk_layer, pts, width))
    out["silk"].extend(groups[k] for k in sorted(groups))

    # -- outline --------------------------------------------------------
    outline = model.get("outline")
    segs: list[tuple[Any, Any]] = []
    if isinstance(outline, dict):
        segs = [(s["start"], s["end"]) for s in outline.get("segments") or []]
    elif outline:
        ring = [tuple(p) for p in outline]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        segs = [(ring[i], ring[i + 1]) for i in range(len(ring) - 1)]
    for i, (a, b) in enumerate(segs):
        out["outline"].append(
            SelectObject(
                {
                    "kind": "outline",
                    "key": f"outline:{i}",
                    "layer": "Edge_Cuts",
                    "index": i,
                    "start": [_r(a[0]), _r(a[1])],
                    "end": [_r(b[0]), _r(b[1])],
                },
                [_stroke_hit("Edge_Cuts", [a, b], gerber.EDGE_CUT_WIDTH_MM)],
            )
        )

    # -- fiducials ------------------------------------------------------
    fids: dict[tuple[float, float], list[dict[str, Any]]] = {}
    for pad in pads:
        if pad.get("role") == "fiducial":
            fids.setdefault((_r(pad["x"]), _r(pad["y"])), []).append(pad)
    for ordinal, ((fx, fy), fpads) in enumerate(fids.items(), start=1):
        out["fiducial"].append(
            SelectObject(
                {
                    "kind": "fiducial",
                    "key": f"fiducial:{ordinal}",
                    "id": f"FID{ordinal}",
                    "x": fx,
                    "y": fy,
                    "copper_dia_mm": _num(fpads[0].get("w")),
                    "layers": [str(p.get("layer") or "") for p in fpads],
                },
                [_pad_hit(p, layer_key(str(p.get("layer") or ""))) for p in fpads],
            )
        )

    # -- nets -----------------------------------------------------------
    net_hits: dict[str, list[dict[str, Any]]] = {}
    for item in copper:
        name = str(item.get("net") or "")
        if not name or item.get("role") == "fiducial":
            continue
        lkey = layer_key(str(item.get("layer") or ""))
        if item.get("ctype") == "track":
            pts = _draw_points(item)
            if len(pts) >= 2:
                net_hits.setdefault(name, []).append(
                    _stroke_hit(lkey, pts, float(item.get("width_mm") or 0.0))
                )
        elif item.get("ctype") == "pour" and item.get("polygon"):
            net_hits.setdefault(name, []).append(_poly_hit(lkey, item["polygon"]))
    for n in sorted(live_nets, key=lambda n: int(n["net_id"])):
        nid = net_id(n["name"])
        if nid is None:
            continue
        out["net"].append(
            SelectObject(
                {
                    "kind": "net",
                    "key": f"net:{nid}",
                    "net_id": nid,
                    "name": str(n["name"]),
                },
                net_hits.get(str(n["name"]), []),
            )
        )

    return [o for kind in KIND_ORDER for o in out[kind]]


__all__ = ["KIND_ORDER", "MIN_HIT_WIDTH_MM", "SelectObject", "build", "layer_key"]

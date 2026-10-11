"""Non-fab mechanical 2D layers — the EWOD stack's cut sheets.

``docs/backlog/threads/ewod-pcb.md`` (gr451662): the ITO top sheet, the
90 um spacer adhesive and their alignment features are 2D geometry for a
laser cutter or blade plotter, and must be computed from the SAME board
model the gerbers come from, or a generator change silently desyncs sheets
that physically register against the board. The pattern is solder paste's:
a layer derived from the board model, emitted for a different machine.

**Never reaches fab.** Nothing here is called by
:func:`precis.pcb.gerber.export_fab` / ``export_gerbers`` and the layers add
no key to the board model, so the fab bundle is byte-identical with or
without this module. Export is its own call, :func:`export_mechanical`.

**Pure.** ``board model dict -> list[MechLayer]`` (:func:`mechanical_layers`)
and ``MechLayer -> SVG text`` (:func:`layer_svg`); no I/O.

Conventions pinned here, each a way these sheets get scrapped:

* **Frame.** Layers are held in board millimetres, Y up, board origin
  unchanged. :func:`layer_svg` flips Y (SVG Y points down) and writes
  ``viewBox`` in board mm, so SVG ``(x, -y)`` is board ``(x, y)``; the
  ``data-board-origin`` attribute records that the origin is the board's.
* **Kerf** is applied at export (``kerf_mm`` of :func:`layer_svg`), never to
  the source geometry: the cut path is the sheet offset outward by half a
  kerf, so cutouts and the kept sheet both come out nominal.
* **Mirror** is an explicit per-layer flag (``MechLayer.mirror``, overridable
  at export), reflecting about the sheet's own bbox centre-x so the part
  stays where it was.
* **Versioning.** :func:`board_fingerprint` hashes the geometry the layers
  derive from (outline, electrode pads, vias); it is embedded in each SVG
  and in its filename, so cut files pair with the fab order they came from.
* **Alignment** is one reusable primitive, :func:`alignment_features` (round
  hole + slot, unequal edge insets so a 180-degree-rotated stack does not
  seat), shared by every sheet and available to board-to-board stacking.
* **Connectivity** is the invariant, not island retention:
  :func:`check_connected` asserts one component whose material surrounds
  every alignment feature.

v1 limits (filed on the thread): the spacer is a frame with one array
window plus optional via covers, so it has no interior islands and needs no
stencil bridges; bridge placement, the channel-cutout style variant and
persisting the layers as generator rows are not built. Cricut importer
behaviour is unconfirmed (SVG is emitted as stroke-only closed paths).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid

# Defaults (mm). The sheet is inset from the board edge so it never
# overhangs the board.
SHEET_INSET_MM = 0.5
WINDOW_MARGIN_MM = 1.0
ALIGN_HOLE_DIA_MM = 2.0
ALIGN_SLOT_LEN_MM = 4.0
ALIGN_INSET_MM = 3.0
VIA_COVER_MARGIN_MM = 0.2
# Minimum intact sheet between an alignment feature and any other cut/edge.
MIN_WEB_MM = 1.0

Ring = list[tuple[float, float]]


@dataclass(frozen=True)
class MechLayer:
    """One cut sheet: ``sheet`` is the material that stays (holes are
    cutouts), in board mm, Y up."""

    name: str
    role: str  # "ito_cut" | "spacer_adhesive"
    sheet: BaseGeometry
    alignment: list[BaseGeometry] = field(default_factory=list)
    mirror: bool = False


@dataclass(frozen=True)
class MechSpec:
    sheet_inset_mm: float = SHEET_INSET_MM
    window_margin_mm: float = WINDOW_MARGIN_MM
    via_covers: bool = False
    via_cover_margin_mm: float = VIA_COVER_MARGIN_MM
    align_hole_dia_mm: float = ALIGN_HOLE_DIA_MM
    align_slot_len_mm: float = ALIGN_SLOT_LEN_MM
    align_inset_mm: float = ALIGN_INSET_MM


def _valid_polygon(geom: BaseGeometry, what: str) -> Polygon:
    """Repair a self-intersecting ring; refuse empty or multi-part results."""
    fixed = make_valid(geom) if not geom.is_valid else geom
    if isinstance(fixed, MultiPolygon):
        fixed = fixed.buffer(0)
    if fixed.is_empty or not isinstance(fixed, Polygon) or fixed.area <= 0:
        raise ValueError(f"{what}: degenerate or self-intersecting geometry")
    return fixed


def _outline_polygon(outline: Any) -> Polygon:
    if isinstance(outline, dict):  # segment-form outline: its bbox
        pts = [
            p
            for seg in outline.get("segments") or []
            for p in (seg["start"], seg["end"])
        ]
        if not pts:
            raise ValueError("board outline: no segments")
        xs, ys = [float(p[0]) for p in pts], [float(p[1]) for p in pts]
        return _valid_polygon(box(min(xs), min(ys), max(xs), max(ys)), "board outline")
    if len(outline) < 3:
        raise ValueError("board outline: fewer than 3 points")
    return _valid_polygon(
        Polygon([(float(p[0]), float(p[1])) for p in outline]), "board outline"
    )


def _pad_geom(pad: dict[str, Any]) -> BaseGeometry:
    poly = pad.get("poly")
    if pad.get("shape") == "polygon" and poly:
        return _valid_polygon(
            Polygon([(float(p[0]), float(p[1])) for p in poly]), "electrode pad"
        )
    x, y = float(pad["x"]), float(pad["y"])
    w = float(pad.get("w", 0.0))
    h = float(pad.get("h", w))
    if pad.get("shape") == "circle":
        return _valid_polygon(Point(x, y).buffer(w / 2), "electrode pad")
    return _valid_polygon(
        box(x - w / 2, y - h / 2, x + w / 2, y + h / 2), "electrode pad"
    )


def board_fingerprint(model: dict[str, Any], spec: MechSpec | None = None) -> str:
    """SHA-256 (hex) of everything the mechanical layers depend on: outline,
    electrode geometry, via diameters and the :class:`MechSpec` values."""
    payload = {
        "outline": model.get("outline"),
        "electrodes": sorted(
            (
                {
                    k: p.get(k)
                    for k in ("x", "y", "w", "h", "shape", "poly")
                    if p.get(k) is not None
                }
                for p in model.get("pads", [])
                if p.get("role") == "electrode"
            ),
            key=lambda d: json.dumps(d, sort_keys=True),
        ),
        "vias": sorted(
            [round(float(c["x"]), 4), round(float(c["y"]), 4), c.get("dia_mm")]
            for c in model.get("copper", [])
            if c.get("ctype") == "via"
        ),
        "spec": asdict(spec or MechSpec()),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def alignment_features(
    bounds: tuple[float, float, float, float],
    *,
    hole_dia_mm: float = ALIGN_HOLE_DIA_MM,
    slot_len_mm: float = ALIGN_SLOT_LEN_MM,
    inset_mm: float = ALIGN_INSET_MM,
) -> list[BaseGeometry]:
    """One round hole (bottom-left, ``inset`` from both edges) and one slot
    (bottom-right, ``2*inset`` from the right edge, ``inset`` from the
    bottom, long axis along X). Round + slot so cutter tolerance cannot
    over-constrain the fit; unequal insets plus unequal shapes so the
    pattern has no 180-degree symmetry."""
    minx, miny, maxx, _maxy = bounds
    r = hole_dia_mm / 2
    hole = Point(minx + inset_mm, miny + inset_mm).buffer(r)
    cx, cy = maxx - 2 * inset_mm, miny + inset_mm
    slot = LineString(
        [(cx - slot_len_mm / 2 + r, cy), (cx + slot_len_mm / 2 - r, cy)]
    ).buffer(r)
    return [hole, slot]


def _union_bounds(geoms: list[BaseGeometry]) -> tuple[float, float, float, float]:
    bs = [g.bounds for g in geoms]
    return (
        min(b[0] for b in bs),
        min(b[1] for b in bs),
        max(b[2] for b in bs),
        max(b[3] for b in bs),
    )


def mechanical_layers(
    model: dict[str, Any], spec: MechSpec | None = None
) -> list[MechLayer]:
    """The ITO cut sheet and spacer-adhesive sheet for ``model``'s board."""
    spec = spec or MechSpec()
    outline = model.get("outline")
    if not outline:
        raise ValueError("mechanical layers need a board outline")
    board = _outline_polygon(outline)
    outer = board.buffer(-spec.sheet_inset_mm, join_style="mitre")
    if outer.is_empty or not isinstance(outer, Polygon):
        raise ValueError("board outline too small for the sheet inset")
    align = alignment_features(
        board.bounds,
        hole_dia_mm=spec.align_hole_dia_mm,
        slot_len_mm=spec.align_slot_len_mm,
        inset_mm=spec.align_inset_mm,
    )
    ito = outer.difference(align[0].union(align[1]))
    adhesive = ito
    window: BaseGeometry | None = None
    electrodes = [
        _pad_geom(p) for p in model.get("pads", []) if p.get("role") == "electrode"
    ]
    if electrodes:
        minx, miny, maxx, maxy = _union_bounds(electrodes)
        window = box(minx, miny, maxx, maxy).buffer(
            spec.window_margin_mm, join_style="mitre"
        )
        adhesive = adhesive.difference(window)
    if spec.via_covers:
        for c in model.get("copper", []):
            if c.get("ctype") != "via":
                continue
            cover = Point(float(c["x"]), float(c["y"])).buffer(
                float(c.get("dia_mm", 0.0)) / 2 + spec.via_cover_margin_mm
            )
            if window is None or not window.intersects(cover):
                adhesive = adhesive.difference(cover)
    return [
        MechLayer("ito_cut", "ito_cut", ito, align),
        MechLayer("spacer_adhesive", "spacer_adhesive", adhesive, align),
    ]


def check_connected(layer: MechLayer) -> None:
    """Raise ``ValueError`` unless the sheet is ONE component and material
    surrounds every alignment feature (so the registered border reaches the
    pins and nothing falls out when cut)."""
    sheet = layer.sheet
    if not isinstance(sheet, Polygon):
        n = len(sheet.geoms) if isinstance(sheet, MultiPolygon) else 0
        raise ValueError(f"{layer.name}: sheet is {n} components, expected 1")
    for i, feat in enumerate(layer.alignment):
        if sheet.intersects(feat.buffer(-1e-6)):
            raise ValueError(f"{layer.name}: alignment feature {i} not cut")
        if not any(Polygon(r).buffer(1e-6).contains(feat) for r in sheet.interiors):
            raise ValueError(f"{layer.name}: alignment feature {i} not a cutout")
        ring = feat.buffer(MIN_WEB_MM).difference(feat)
        if ring.difference(sheet).area > 1e-6:
            raise ValueError(
                f"{layer.name}: alignment feature {i} has less than "
                f"{MIN_WEB_MM} mm of material to a cutout or the sheet edge"
            )


def _fmt(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def cut_paths(
    layer: MechLayer, *, kerf_mm: float = 0.0, mirror: bool | None = None
) -> list[Ring]:
    """Closed rings (board mm, Y up) of the cut path, kerf-compensated and
    mirrored."""
    sheet = layer.sheet
    if kerf_mm:
        sheet = sheet.buffer(kerf_mm / 2, join_style="mitre")
    if layer.mirror if mirror is None else mirror:
        cx = (layer.sheet.bounds[0] + layer.sheet.bounds[2]) / 2
        sheet = affinity.scale(sheet, xfact=-1, yfact=1, origin=(cx, 0))
    polys = list(sheet.geoms) if isinstance(sheet, MultiPolygon) else [sheet]
    rings: list[Ring] = []
    for p in polys:
        rings.append([(x, y) for x, y in p.exterior.coords])
        rings.extend([(x, y) for x, y in r.coords] for r in p.interiors)
    return rings


def layer_svg(
    layer: MechLayer,
    *,
    fingerprint: str,
    kerf_mm: float = 0.0,
    mirror: bool | None = None,
) -> str:
    """One layer as SVG: mm units, Y flipped, stroke-only closed paths."""
    if layer.sheet.is_empty:
        raise ValueError(f"{layer.name}: sheet is empty, nothing to cut")
    rings = cut_paths(layer, kerf_mm=kerf_mm, mirror=mirror)
    if not rings:
        raise ValueError(f"{layer.name}: sheet is empty, nothing to cut")
    xs = [x for r in rings for x, _ in r]
    ys = [y for r in rings for _, y in r]
    margin = 1.0
    vx, vy = min(xs) - margin, -(max(ys) + margin)
    w, h = max(xs) - min(xs) + 2 * margin, max(ys) - min(ys) + 2 * margin
    paths = "\n".join(
        '  <path d="M'
        + " L".join(f"{_fmt(x)} {_fmt(-y)}" for x, y in r)
        + ' Z" fill="none" stroke="#000" stroke-width="0.05"/>'
        for r in rings
    )
    do_mirror = layer.mirror if mirror is None else mirror
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(w)}mm" '
        f'height="{_fmt(h)}mm" viewBox="{_fmt(vx)} {_fmt(vy)} {_fmt(w)} {_fmt(h)}" '
        f'data-board-origin="0,0" data-units="mm" '
        f'data-layer="{layer.name}" data-role="{layer.role}" '
        f'data-mirror="{str(do_mirror).lower()}" data-kerf-mm="{_fmt(kerf_mm)}" '
        f'data-board-fingerprint="{fingerprint}">\n'
        f"{paths}\n</svg>\n"
    )


def export_mechanical(
    model: dict[str, Any],
    *,
    name: str = "design",
    spec: MechSpec | None = None,
    kerf_mm: float = 0.0,
    mirror: dict[str, bool] | None = None,
) -> dict[str, str]:
    """``{filename: svg}`` for every mechanical layer, each file stamped with
    the board fingerprint. Raises if a sheet fails :func:`check_connected`."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.startswith("."):
        raise ValueError(f"unsafe export name {name!r}: use [A-Za-z0-9._-]+")
    fp = board_fingerprint(model, spec)
    out: dict[str, str] = {}
    for layer in mechanical_layers(model, spec):
        check_connected(layer)
        svg = layer_svg(
            layer,
            fingerprint=fp,
            kerf_mm=kerf_mm,
            mirror=(mirror or {}).get(layer.name),
        )
        out[f"{name}-mech-{layer.name}-{fp[:12]}.svg"] = svg
    return out


__all__ = [
    "MechLayer",
    "MechSpec",
    "alignment_features",
    "board_fingerprint",
    "check_connected",
    "cut_paths",
    "export_mechanical",
    "layer_svg",
    "mechanical_layers",
]

"""Copper pours for plane-assigned layers — the module the realizer has
been deferring to since it was written.

**Why this stopped being cosmetic.** ``realize_segment`` gives a
plane-promoted net a dog-bone STUB off each pad and stops there, on the
stated grounds that "the via-to-plane connection is ``planes.py``'s job, a
later module". Nothing then poured the plane, and nothing dropped a via
into it. So a promoted net's realized copper was: some pads, some
millimetre-long stubs, and no plane — a net that is not connected to
itself at all.

That is not a rendering complaint. It was invisible for as long as it was,
because every check the engine had asks about *proximity* (clearance,
width, annular ring, board edge) and a net with no copper passes all of
them comfortably. :func:`precis.pcb.connectivity.net_islands` found it on
its first run: on seed 2 the ``SDA`` net, promoted to ``In1.Cu``, came back
in five pieces.

**What a pour is here.** The board outline, inset by the edge clearance,
minus every *other* net's copper on that layer dilated by clearance —
including an antipad around each foreign via barrel that passes through.
The pour's own net's vias are deliberately not subtracted: that is the
connection. Thermal reliefs are not modelled (a solid connection is
electrically correct and thermally pessimistic for hand soldering, which
is the safe direction for a machine-assembled board).

The result can be disconnected or holed, and both are reported rather than
smoothed away: a pour that a foreign via array has cut in half is a real
electrical fact about the board, and merging the halves in the model would
be the same class of lie as a track that ends *near* its pad.
"""

from __future__ import annotations

import math
from typing import Any

# shapely ships no py.typed marker here — same suppression drc.py carries,
# same reason. Boolean geometry on real polygons is what this module is,
# and hand-rolling it would be a second, worse implementation of a
# dependency the project already takes.
from shapely.geometry import MultiPolygon, Polygon  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.ops import unary_union  # type: ignore[import-untyped]

from precis.pcb.drc import _copper_item_polygon, _via_layer_names

#: Shapely buffer resolution for round caps/joins. Matches drc.py's own
#: choice so a pour's boundary and the clearance check's idea of that
#: boundary are the same polygon, not two approximations of one.
_BUFFER_QUAD_SEGS = 16

#: A pour fragment smaller than this is dropped. It is an artifact of
#: buffering arithmetic (slivers along a cut), not copper a fab would
#: image, and keeping it would report a plane as "in 40 pieces" because of
#: 39 specks. Deliberately far below any real feature: a 0.05mm^2 island is
#: two orders of magnitude under the smallest pad on this board.
MIN_FRAGMENT_MM2 = 0.05


def _as_polygons(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return []


def _ring(coords: Any) -> list[list[float]]:
    return [[float(x), float(y)] for x, y in coords]


def plane_pours(
    *,
    outline: list[list[float]],
    layers: list[str],
    plane_nets: dict[int, str],
    copper: list[dict[str, Any]],
    clearance_mm: float,
    edge_clearance_mm: float,
) -> list[dict[str, Any]]:
    """One ``ctype='pour'`` item per connected fragment of each plane.

    ``plane_nets`` maps a stackup layer INDEX to the net name poured on it
    — the caller resolves that from ``ir.net_plane_layer``, because this
    module deliberately knows nothing about the IR (same boundary
    :mod:`precis.pcb.session` keeps). ``copper`` is the already-realized
    tracks and vias in :mod:`precis.pcb.gerber` model shape.

    Emitted items carry ``polygon`` (the exterior ring) and, when the pour
    has any, ``holes`` — a foreign via through a plane leaves a real hole,
    and an exterior-only pour would claim copper where an antipad is.
    """
    if not outline or len(outline) < 3:
        return []
    board = Polygon([(float(p[0]), float(p[1])) for p in outline])
    if not board.is_valid:
        board = board.buffer(0)
    region0 = board.buffer(
        -abs(edge_clearance_mm), quad_segs=_BUFFER_QUAD_SEGS, join_style=2
    )
    if region0.is_empty:
        return []

    out: list[dict[str, Any]] = []
    for layer_idx, net in sorted(plane_nets.items()):
        if not (0 <= layer_idx < len(layers)):
            continue
        layer_name = layers[layer_idx]
        blockers: list[BaseGeometry] = []
        for item in copper:
            if str(item.get("net", "")) == net:
                continue  # own-net copper is the connection, not an obstacle
            if item.get("ctype") == "via":
                # A barrel is an obstacle on every layer it passes through,
                # which is exactly the case a "layer" key would miss — the
                # prior regression RealizedVia's docstring names.
                if layer_name not in _via_layer_names(item, layers):
                    continue
            elif str(item.get("layer", "")) != layer_name:
                continue
            poly = _copper_item_polygon(item)
            if poly is not None and not poly.is_empty:
                blockers.append(poly.buffer(clearance_mm, quad_segs=_BUFFER_QUAD_SEGS))
        region = region0
        if blockers:
            region = region.difference(unary_union(blockers))
        for frag in _as_polygons(region):
            if frag.area < MIN_FRAGMENT_MM2:
                continue
            item = {
                "ctype": "pour",
                "layer": layer_name,
                "net": net,
                "polygon": _ring(frag.exterior.coords),
            }
            holes = [
                _ring(ring.coords)
                for ring in frag.interiors
                if Polygon(ring).area >= MIN_FRAGMENT_MM2
            ]
            if holes:
                item["holes"] = holes
            out.append(item)
    return out


def point_in_pour(pour: dict[str, Any], x: float, y: float) -> bool:
    """Is ``(x, y)`` on this pour's copper? Exterior ring minus holes.

    Ray casting, no shapely: this is called from
    :mod:`precis.pcb.connectivity`, which exists to catch the geometry
    engine being wrong and so must not be built on it. Boundary cases go
    to "inside" — a via centred exactly on a pour edge is connected, and
    over-reporting connection here would need a real gap elsewhere to
    matter, whereas under-reporting it invents an island.
    """
    rings: list[list[list[float]]] = [pour.get("polygon") or []]
    if not _in_ring(rings[0], x, y):
        return False
    return not any(_in_ring(hole, x, y) for hole in pour.get("holes") or [])


def _in_ring(ring: list[list[float]], x: float, y: float) -> bool:
    if len(ring) < 3:
        return False
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = float(ring[i][0]), float(ring[i][1])
        x2, y2 = float(ring[(i + 1) % n][0]), float(ring[(i + 1) % n][1])
        if math.isclose(y1, y2):
            continue
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > x:
                inside = not inside
    return inside


__all__ = ["MIN_FRAGMENT_MM2", "plane_pours", "point_in_pour"]

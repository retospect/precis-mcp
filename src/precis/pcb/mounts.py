"""Corner mounting nuts with a concentric corner radius.

A board that wants four solder-on nuts (SMTSO family, e.g. Sinhoo
SMTSO3030CTJ, LCSC C2915308, M3 x 3 through-thread) looks best with each
nut centred on the arc that rounds its corner. An arc centred on the nut
and tangent to both edges puts the nut at ``(R, R)`` from the corner, so
the corner radius equals the nut's edge distance. The board area that
rounding removes (``R^2 (1 - pi/4)``, ~5.4 mm^2 at R = 5 mm) lies inside
the nut keep-out anyway, so the fillet costs almost no usable area.
Rounded outlines are routed, not V-scored.

:func:`corner_nut_features` is the pure geometry: it turns a rectangular
outline into the ``outline`` + four ``mounting_hole`` features that
``put(args={'op': 'corner_nuts'})`` stores. A nut is the existing
solder-nut ``mounting_hole`` shape (plated hole, copper land on every
layer, hardware envelope), so the router, pours and DRC clear it with no
new code. The defaults come from the SMTSO M3 table (see below); pass the
part's own datasheet values to override.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

#: PEM-style M3 nuts want 4.8-5.6 mm from the nut centre to the board edge.
DEFAULT_EDGE_DISTANCE_MM = 5.0
#: Clearance added to the nut body radius when sizing the corner radius.
DEFAULT_BODY_CLEARANCE_MM = 0.5
#: M3 SMTSO row of the SMTSO dimension table (datasheet da496401): hole in
#: sheet 4.22 (+0.08), minimum solder pad 6.2, flange OD 5.56. The table
#: text is OCR with a garbled header, so the column mapping is inferred
#: from the M1 row's pattern; confirm against the manufacturer PDF.
DEFAULT_HOLE_DIA_MM = 4.22
DEFAULT_RING_DIA_MM = 6.2
DEFAULT_BODY_DIA_MM = 5.56
DEFAULT_PART_LCSC = "C2915308"
NUT_STYLE = "smtso_m3_corner"


def _rect(path: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    """``(x0, y0, x1, y1)`` of an axis-aligned rectangle outline, else
    ``ValueError``."""
    pts = [(float(p[0]), float(p[1])) for p in path]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    xs, ys = sorted({p[0] for p in pts}), sorted({p[1] for p in pts})
    if len(pts) != 4 or len(xs) != 2 or len(ys) != 2:
        raise ValueError("corner nuts need a plain axis-aligned rectangle outline")
    return xs[0], ys[0], xs[1], ys[1]


def corner_radius_mm(
    *, body_dia_mm: float, clearance_mm: float, edge_distance_mm: float | None
) -> float:
    """The corner radius, equal to the nut's edge distance: the larger of
    the PEM edge distance and body radius + clearance, unless the caller
    sets ``edge_distance_mm``."""
    if edge_distance_mm is not None:
        return float(edge_distance_mm)
    return max(DEFAULT_EDGE_DISTANCE_MM, body_dia_mm / 2.0 + clearance_mm)


def corner_nut_features(
    path: Sequence[Sequence[float]],
    *,
    radius_mm: float | None = None,
    hole_dia_mm: float = DEFAULT_HOLE_DIA_MM,
    ring_dia_mm: float = DEFAULT_RING_DIA_MM,
    body_dia_mm: float = DEFAULT_BODY_DIA_MM,
    clearance_mm: float = DEFAULT_BODY_CLEARANCE_MM,
) -> list[dict[str, Any]]:
    """The rounded ``outline`` feature plus four corner-nut
    ``mounting_hole`` features for the rectangle ``path``. Raises
    ``ValueError`` for a non-rectangle, a non-positive size, or a radius
    that cannot fit (the nut land would cross the board edge)."""
    x0, y0, x1, y1 = _rect(path)
    if min(hole_dia_mm, ring_dia_mm, body_dia_mm) <= 0 or clearance_mm < 0:
        raise ValueError("hole, ring and body diameters must be > 0")
    if ring_dia_mm <= hole_dia_mm:
        raise ValueError("ring_dia_mm must exceed hole_dia_mm")
    r = corner_radius_mm(
        body_dia_mm=body_dia_mm, clearance_mm=clearance_mm, edge_distance_mm=radius_mm
    )
    land = max(ring_dia_mm, body_dia_mm)
    if r <= 0 or r < land / 2.0:
        raise ValueError(
            f"radius {r:g} mm leaves the {land:g} mm nut land over the board "
            "edge; raise radius_mm"
        )
    if 2.0 * r > min(x1 - x0, y1 - y0):
        raise ValueError(
            f"radius {r:g} mm does not fit a {x1 - x0:g} x {y1 - y0:g} mm board "
            "(two corner nuts would overlap)"
        )
    features: list[dict[str, Any]] = [
        {
            "ftype": "outline",
            "geom": {
                "path": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
                "corner_radius_mm": r,
            },
        }
    ]
    for x, y in (
        (x0 + r, y0 + r),
        (x1 - r, y0 + r),
        (x1 - r, y1 - r),
        (x0 + r, y1 - r),
    ):
        features.append(
            {
                "ftype": "mounting_hole",
                "x": x,
                "y": y,
                "geom": {
                    "diameter": hole_dia_mm,
                    "ring_dia_mm": ring_dia_mm,
                    "head_dia_mm": body_dia_mm,
                    "plated": True,
                    "style": NUT_STYLE,
                    "lcsc": DEFAULT_PART_LCSC,
                },
            }
        )
    return features

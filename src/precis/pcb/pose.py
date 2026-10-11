"""The one footprint-local -> world transform.

Why: footprint-owned geometry (pads, polygon vertices, holes, courtyard,
silk, text) is stored footprint-local and only mapped to world through this
module, so it follows its instance; never persist a part-owned absolute
position.

Order is fixed: mirror-X (bottom side only), rotate clockwise (our internal
0deg-natural, CW-positive frame, ``[[cos, sin], [-sin, cos]]``), translate.
Dependency-free on purpose so every pcb module can import it.
"""

from __future__ import annotations

import math
from typing import Any


def rotate_cw(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate ``(x, y)`` clockwise by ``deg`` degrees."""
    theta = math.radians(deg)
    c, s = math.cos(theta), math.sin(theta)
    return x * c + y * s, -x * s + y * c


def rel_to_world(
    lx: float, ly: float, rot_deg: float, *, mirrored: bool = False
) -> tuple[float, float]:
    """A footprint-local vector/vertex, mirrored then rotated, NOT translated."""
    if mirrored:
        lx = -lx
    return rotate_cw(float(lx), float(ly), float(rot_deg))


def point_to_world(
    lx: float,
    ly: float,
    x: float,
    y: float,
    rot_deg: float,
    *,
    mirrored: bool = False,
) -> tuple[float, float]:
    """A footprint-local point placed at instance pose ``(x, y, rot_deg)``."""
    rx, ry = rel_to_world(lx, ly, rot_deg, mirrored=mirrored)
    return float(x) + rx, float(y) + ry


def to_world(lx: float, ly: float, inst: dict[str, Any]) -> tuple[float, float]:
    """Like :func:`point_to_world`, reading the pose off an instance dict
    (``x``/``y``/``rot``/``layer``)."""
    return point_to_world(
        lx,
        ly,
        float(inst["x"]),
        float(inst["y"]),
        float(inst.get("rot") or 0.0),
        mirrored=is_bottom(inst),
    )


def rel_to_world_inst(
    lx: float, ly: float, inst: dict[str, Any]
) -> tuple[float, float]:
    """Instance-dict form of :func:`rel_to_world`."""
    return rel_to_world(lx, ly, float(inst.get("rot") or 0.0), mirrored=is_bottom(inst))


def is_bottom(inst: dict[str, Any]) -> bool:
    """``pcb_instances.layer`` string convention (top/bottom/bot/b)."""
    return str(inst.get("layer") or "top").lower() in ("bottom", "bot", "b")

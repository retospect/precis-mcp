"""Per-net routed copper length against its straight-line lower bound
(gr451277).

DRC does not care that a net detours and ``route-status`` reads ``realized``
either way, so a wasteful path (a via escape that crosses the field and
comes back, a bottom run retracing the top wire) was only ever noticed by
eye. This puts one number beside each net: ``routed_mm / bound_mm``.

- ``routed_mm``: summed track segment length (arcs at radius x sweep);
  vias add none. Malformed segments are skipped.
- ``bound_mm``: the Euclidean minimum spanning tree over the net's distinct
  pad positions. Steiner points can beat it by up to ~13%, so a ratio near
  1.0 means "no excursion", not "exactly minimal"; 1.0 is not a floor.

A net that also carries pour/zone copper is skipped: the pour supplies
connectivity the tracks do not, so the ratio would be meaninglessly small.

Pure: flat copper/pad items in (the shape :mod:`precis.pcb.drc` reads).
Measures only; it changes no routing decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: Pad positions closer than this are one terminal (a through-hole pad is
#: listed once per copper layer).
_MERGE_MM = 1e-3


@dataclass(frozen=True, slots=True)
class NetLength:
    net: str
    routed_mm: float
    bound_mm: float

    @property
    def ratio(self) -> float | None:
        """``routed / bound``; ``None`` when the bound is zero (fewer than
        two distinct pad positions) so a ratio is never invented."""
        return self.routed_mm / self.bound_mm if self.bound_mm > 0 else None


_POUR_CTYPES = frozenset({"pour", "zone"})


def _seg_length(seg: dict[str, Any]) -> float | None:
    """Length of one line/arc segment, ``None`` if malformed."""
    try:
        sx, sy = (float(v) for v in seg["start"][:2])
        ex, ey = (float(v) for v in seg["end"][:2])
        if seg.get("shape") != "arc":
            return math.hypot(ex - sx, ey - sy)
        cx, cy = (float(v) for v in seg["center"][:2])
    except (KeyError, TypeError, ValueError):
        return None
    # Same sweep convention as precis.pcb.drc._arc_points.
    diff = (math.atan2(ey - cy, ex - cx) - math.atan2(sy - cy, sx - cx)) % (2 * math.pi)
    sweep = diff if not seg.get("cw", True) else diff - 2 * math.pi
    return math.hypot(sx - cx, sy - cy) * abs(sweep)


def _mst_mm(points: list[tuple[float, float]]) -> float:
    """Euclidean MST length, O(n^2) Prim with a best-distance-to-tree array."""
    n = len(points)
    if n < 2:
        return 0.0
    best = [math.dist(points[0], p) for p in points]
    done = [False] * n
    done[0] = True
    total = 0.0
    for _ in range(n - 1):
        j = min((i for i in range(n) if not done[i]), key=best.__getitem__)
        total += best[j]
        done[j] = True
        for i in range(n):
            if not done[i]:
                d = math.dist(points[j], points[i])
                if d < best[i]:
                    best[i] = d
    return total


def net_lengths(
    copper: list[dict[str, Any]], pads: list[dict[str, Any]]
) -> dict[str, NetLength]:
    """``{net: NetLength}`` for every net with routed track copper and no
    pour/zone copper."""
    routed: dict[str, float] = {}
    poured: set[str] = set()
    for item in copper:
        net = str(item.get("net") or "")
        if not net:
            continue
        ctype = item.get("ctype")
        if ctype in _POUR_CTYPES:
            poured.add(net)
        elif ctype == "track":
            total = routed.get(net, 0.0)
            for seg in item.get("segments") or []:
                length = _seg_length(seg)
                if length is not None:
                    total += length
            routed[net] = total
    for net in poured:
        routed.pop(net, None)

    terminals: dict[str, dict[tuple[int, int], tuple[float, float]]] = {}
    for pad in pads:
        net = str(pad.get("net") or "")
        if net not in routed:
            continue
        x, y = float(pad["x"]), float(pad["y"])
        key = (round(x / _MERGE_MM), round(y / _MERGE_MM))
        terminals.setdefault(net, {}).setdefault(key, (x, y))

    return {
        net: NetLength(
            net=net,
            routed_mm=total,
            bound_mm=_mst_mm(list(terminals.get(net, {}).values())),
        )
        for net, total in routed.items()
    }

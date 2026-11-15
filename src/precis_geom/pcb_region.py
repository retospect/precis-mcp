"""Lazy geometry references for pcb intent (pcb-intent-api slice 2).

A :class:`Region` is an immutable expression — ``u1.courtyard.buffer(1.5)``,
``pcb.between(u1["SW"].center, l15.center).buffer(1.0)``,
``pcb.box(0, 0, 20, 10) - j1.courtyard`` — that names a polygon without
computing it. Resolution happens later, against a *pose snapshot* (the
authored ``at()`` poses, or the poses a solver is evaluating), so a rule
that names a movable part follows the part (pcb-intent-api Decision 4).

* Building an expression needs no shapely (the builder stays
  dependency-light); :meth:`Region.resolve` imports it lazily
  (``precis-geom[pcb-kernel]``).
* :meth:`Region.ast` is the canonical, JSON-able form the manifest records
  (expressions as ASTs, not polygons); :meth:`Region.refs` lists the
  refdes the expression reads, so a caller knows which poses matter.
* Pose convention: ``(x, y, rot_deg, side)``; a part is rotated CCW about
  its origin, a bottom-side part is mirrored in x first, then translated.

A snapshot is anything with ``part_poly(refdes, kind)`` (kind
``outline``/``courtyard``, a footprint-local ``[x0, y0, x1, y1]`` or
``None``), ``pose(refdes)``, ``pad_xy(refdes, pin)`` and ``board_outline()``
— :meth:`precis_geom.pcb.Board.region_snapshot` is the board's.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

Pose = tuple[float, float, float, str]


class RegionError(ValueError):
    """An expression that cannot resolve (unknown refdes, unplaced part,
    empty intersection) — names the sub-expression that failed."""


class Snapshot(Protocol):
    def part_poly(self, refdes: str, kind: str) -> Sequence[float] | None: ...
    def pose(self, refdes: str) -> Pose | None: ...
    def pad_xy(self, refdes: str, pin: str) -> tuple[float, float] | None: ...
    def board_outline(self) -> Any: ...


def _xf(pose: Pose, x: float, y: float) -> tuple[float, float]:
    px, py, rot, side = pose
    if side == "bottom":
        x = -x
    rad = math.radians(rot)
    c, s = math.cos(rad), math.sin(rad)
    return (px + x * c - y * s, py + x * s + y * c)


class Region:
    """One node of a geometry expression; see the module docstring."""

    __slots__ = ("args", "op")

    def __init__(self, op: str, *args: Any) -> None:
        self.op = op
        self.args = args

    # -- combinators -------------------------------------------------
    def buffer(self, mm: float) -> Region:
        return Region("buffer", self, float(mm))

    def offset(self, dx: float, dy: float) -> Region:
        return Region("offset", self, float(dx), float(dy))

    def hull(self) -> Region:
        return Region("hull", self)

    def __or__(self, other: Region) -> Region:
        return Region("union", self, _as_region(other))

    def __and__(self, other: Region) -> Region:
        return Region("intersect", self, _as_region(other))

    def __sub__(self, other: Region) -> Region:
        return Region("diff", self, _as_region(other))

    def __repr__(self) -> str:
        return f"Region({self.op}{''.join(', ' + repr(a) for a in self.args)})"

    # -- introspection -----------------------------------------------
    def ast(self) -> dict[str, Any]:
        """Canonical JSON form (key-sorted by the manifest writer)."""
        return {
            "op": self.op,
            "args": [a.ast() if isinstance(a, Region) else _json(a) for a in self.args],
        }

    def refs(self) -> set[str]:
        """Refdes this expression reads — the poses that move it."""
        out: set[str] = set()
        if self.op in ("part", "pin"):
            out.add(self.args[0])
        for a in self.args:
            if isinstance(a, Region):
                out |= a.refs()
        return out

    # -- resolution --------------------------------------------------
    def resolve(self, snap: Snapshot, poses: Mapping[str, Pose] | None = None) -> Any:
        """The shapely geometry against ``snap``; ``poses`` overrides the
        snapshot's poses for the named refdes (a solver's current
        iterate). Raises :class:`RegionError` naming what failed."""
        try:
            from shapely import affinity
            from shapely.geometry import MultiPoint, Point, Polygon, box
        except ImportError as exc:  # pragma: no cover - extra not installed
            raise RegionError(
                "resolving a region needs shapely — install precis-geom[pcb-kernel]"
            ) from exc

        def pose_of(refdes: str) -> Pose:
            p = (poses or {}).get(refdes) or snap.pose(refdes)
            if p is None:
                raise RegionError(f"{refdes!r}: no pose (unplaced or unknown part)")
            return p

        def go(r: Region) -> Any:
            op, a = r.op, r.args
            if op == "box":
                return box(*a)
            if op == "circle":
                return Point(*a[0]).buffer(a[1], quad_segs=16)
            if op == "poly":
                return Polygon(a[0])
            if op == "board":
                outline = snap.board_outline()
                if outline is None:
                    raise RegionError("the board has no outline")
                return Polygon(outline)
            if op == "part":
                refdes, kind = a
                pose = pose_of(refdes)
                if kind == "center":
                    return Point(pose[0], pose[1])
                ext = snap.part_poly(refdes, kind)
                if ext is None:
                    raise RegionError(f"{refdes}.{kind}: footprint has no known extent")
                x0, y0, x1, y1 = ext
                return Polygon(
                    [
                        _xf(pose, x, y)
                        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
                    ]
                )
            if op == "pin":
                refdes, pin = a
                xy = snap.pad_xy(refdes, pin)
                if xy is None:
                    raise RegionError(f"{refdes}[{pin!r}]: no such pad")
                return Point(*_xf(pose_of(refdes), *xy))
            if op == "buffer":
                g = go(a[0]).buffer(a[1])
            elif op == "offset":
                g = affinity.translate(go(a[0]), a[1], a[2])
            elif op == "hull":
                g = go(a[0]).convex_hull
            elif op == "union":
                g = go(a[0]).union(go(a[1]))
            elif op == "intersect":
                g = go(a[0]).intersection(go(a[1]))
            elif op == "diff":
                g = go(a[0]).difference(go(a[1]))
            elif op == "between":
                g = MultiPoint([go(a[0]), go(a[1])]).convex_hull
            else:  # pragma: no cover - constructors are closed
                raise RegionError(f"unknown region op {op!r}")
            if g.is_empty:
                raise RegionError(f"{op}: empty result")
            return g

        return go(self)


def _json(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return [_json(i) for i in v]
    return v


def _as_region(v: Any) -> Region:
    if isinstance(v, Region):
        return v
    raise TypeError(f"expected a Region expression, got {type(v).__name__}")


# -- constructors (re-exported by ``precis_geom.pcb``) ---------------------


def region_box(x0: float, y0: float, x1: float, y1: float) -> Region:
    """Axis-aligned rectangle, board-frame mm (corners in any order)."""
    return Region("box", min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def circle(center: Sequence[float] | Region, r: float) -> Region:
    """Disc of radius ``r`` about ``center`` (an ``(x, y)`` or a point
    :class:`Region` such as ``r4.center``)."""
    if isinstance(center, Region):
        return center.buffer(r)
    return Region("circle", (float(center[0]), float(center[1])), float(r))


def poly(points: Sequence[Sequence[float]]) -> Region:
    """Polygon from ``[(x, y), …]`` (≥ 3 vertices)."""
    if len(points) < 3:
        raise ValueError("pcb.poly needs at least 3 vertices")
    return Region("poly", tuple((float(x), float(y)) for x, y in points))


def between(a: Region, b: Region) -> Region:
    """Convex hull of two expressions — a corridor once ``.buffer()``-ed."""
    return Region("between", _as_region(a), _as_region(b))


def part_ref(refdes: str, kind: str) -> Region:
    return Region("part", refdes, kind)


def pin_ref(refdes: str, pin: str) -> Region:
    return Region("pin", refdes, str(pin))


def board_ref() -> Region:
    return Region("board")


__all__ = [
    "Region",
    "RegionError",
    "between",
    "board_ref",
    "circle",
    "part_ref",
    "pin_ref",
    "poly",
    "region_box",
]

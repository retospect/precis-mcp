"""The panel model: a box with shelves as 2-D rectilinear outlines with their
fingers, slots and dog-bones, each with its 3-D placement.

Geometry rules (docs/backlog/flatpack-furniture-generator.md, Reto's
rulings se-machine-design-4/-5) — every figure here derives from the sheet
thickness ``t``:

* **Box frame.** Outer ``W × H × D`` along ``x × z × y``; the front
  (``y = 0``) is open, the back panel sits at ``y ∈ [D−t, D]``. Panels: two
  sides (``x ∈ [0,t]`` and ``[W−t, W]``), top, bottom, back, one shelf per
  entry of ``shelves`` (its height = inside bottom to the shelf's underside).
  Every panel's 2-D frame is ``(u, v)`` in its own plane with ``n ∈ [0, t]``
  the thickness; :class:`Frame` maps it to box coordinates. The two sides
  share one outline (the box is mirror-symmetric in ``x``).
* **Finger joints** on side–top, side–bottom, side–back, top–back and
  bottom–back. Each panel's extent, finger tips included, is its box face.
  An edge of length ``L`` gets ``n`` fingers, the odd number nearest
  ``L/3t`` (ties round up), at least 3, each ``L/n`` wide; ``L < 6t`` is
  refused by edge name. The panel ranked first in side > top/bottom > back
  owns the odd positions (the end fingers). Where three panels meet the
  ``t×t×t`` corner cube belongs to the side, so the top's and bottom's end
  fingers on the back edge stop ``t`` short of each end. Finger depth is
  ``t``; every gap a finger enters is widened by ``fit`` (``fit/2`` a side).
* **Straight joints**: plain butt edges. Sides ``D × H``; top and bottom
  ``(W−2t) × D`` between the sides; back ``(W−2t) × (H−2t)`` between the
  sides and between top and bottom, flush with the rear face.
* **Shelves**, both joint types: body ``(W−2t) × (D−t)`` stopping at the
  back; two tabs per side edge, ``3t`` wide and ``t`` long, centred at ¼
  and ¾ of the body depth; each side gets matching through-slots of
  ``(3t+fit) × (t+fit)``. Tabs and slots on every machine, because a laser
  cannot cut a dado. A shelf is refused when its slots would leave less
  than ``t`` of web to a panel edge, a finger gap or another slot.
* **Dog-bones** on every inside corner when ``cutter_d > 0`` (CNC), none at
  ``cutter_d = 0`` (laser, Cricut): a circle of radius ``cutter_d/2`` centred
  on the corner's bisector inside the removed region, its edge through the
  corner point, added to the removed region (:func:`dog_bones`).
* **Kerf** (:func:`kerf_offset`): every cut path moves ``kerf/2`` away from
  the part's material — outlines outward, slots into the opening — with
  shapely's mitre join so rectilinear corners stay square. For a
  rectilinear outline the area grows by exactly ``P·d + 4d²`` (``d =
  kerf/2``); a rectangular slot shrinks by ``P·d − 4d²``.

Ownership is the invariant the tests pin: at ``fit = 0`` the panels'
extruded outlines tile the box shell (plus shelf slabs) exactly — every
point of the shell belongs to one panel, so every finger meets a gap and
every corner cube has one owner (``tests/test_flatpack_panels.py``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from shapely.geometry import Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

Vec = tuple[float, float, float]

JOINTS: tuple[str, ...] = ("finger", "straight")

#: Minimum web of material left between a shelf slot and anything else.
_MIN_WEB_T = 1.0


class FlatpackError(ValueError):
    """A parameter set the generator refuses — always naming the rule."""


def _fmt(v: float) -> str:
    text = f"{v:.3f}".rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class Frame:
    """A panel's placement: ``p = origin + u·eu + v·ev + n·en``."""

    origin: Vec
    eu: Vec
    ev: Vec
    en: Vec

    def to_world(self, u: float, v: float, n: float) -> Vec:
        o, eu, ev, en = self.origin, self.eu, self.ev, self.en
        return (
            o[0] + u * eu[0] + v * ev[0] + n * en[0],
            o[1] + u * eu[1] + v * ev[1] + n * en[1],
            o[2] + u * eu[2] + v * ev[2] + n * en[2],
        )

    def to_local(self, p: Vec) -> Vec:
        d = tuple(p[i] - self.origin[i] for i in range(3))
        return (
            sum(d[i] * self.eu[i] for i in range(3)),
            sum(d[i] * self.ev[i] for i in range(3)),
            sum(d[i] * self.en[i] for i in range(3)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": list(self.origin),
            "u": list(self.eu),
            "v": list(self.ev),
            "n": list(self.en),
        }


_X: Vec = (1.0, 0.0, 0.0)
_Y: Vec = (0.0, 1.0, 0.0)
_Z: Vec = (0.0, 0.0, 1.0)


@dataclass(frozen=True)
class Panel:
    """One flat part: its pre-kerf outline (holes = slots) in the panel frame,
    the frame placing it in the box, and the stock thickness."""

    id: str
    name: str
    polygon: Polygon
    frame: Frame
    thickness: float

    @property
    def size(self) -> tuple[float, float]:
        minx, miny, maxx, maxy = self.polygon.bounds
        return (maxx - minx, maxy - miny)


def finger_count(length: float, t: float, edge: str) -> tuple[int, float]:
    """``(n, width)`` for an edge: the odd ``n`` nearest ``L/3t`` (ties up),
    at least 3. Refuses ``L < 6t`` naming the edge and the minimum."""
    if length < 6 * t - 1e-9:
        raise FlatpackError(
            f"edge {edge} is {_fmt(length)} mm; finger joints need at least "
            f"6t = {_fmt(6 * t)} mm at t = {_fmt(t)} mm"
        )
    x = length / (3 * t)
    n = max(3, 2 * math.floor(x / 2) + 1)
    return n, length / n


def _unowned(length: float, n: int, own_odd: bool) -> list[tuple[float, float]]:
    """Spans along an edge the panel does *not* own (removed as gaps).
    Positions are 1-based; the owner of the odd positions holds both ends."""
    w = length / n
    return [((i - 1) * w, i * w) for i in range(1, n + 1) if (i % 2 == 1) != own_odd]


def _shelf_centres(D: float, t: float) -> tuple[float, float]:
    body = D - t
    return (body / 4, 3 * body / 4)


def _check_shelves(
    shelves: Sequence[float], W: float, H: float, D: float, t: float, fit: float
) -> None:
    if not shelves:
        return
    web = _MIN_WEB_T * t
    lo, hi = web + fit / 2, H - 3 * t - web - fit / 2
    for h in shelves:
        if not (lo - 1e-9 <= h <= hi + 1e-9):
            raise FlatpackError(
                f"shelf height {_fmt(h)} mm must lie within [{_fmt(lo)}, "
                f"{_fmt(hi)}] mm (t of web to the bottom and top edges at "
                f"H = {_fmt(H)}, t = {_fmt(t)})"
            )
    for a, b in pairwise(shelves):
        if b - a < 2 * t - 1e-9:
            raise FlatpackError(
                f"shelves at {_fmt(a)} and {_fmt(b)} mm are closer than 2t = "
                f"{_fmt(2 * t)} mm (one t of web between their slots)"
            )
    front_web = (D - t) / 4 - 1.5 * t - fit / 2
    if front_web < web - 1e-9:
        raise FlatpackError(
            f"depth D = {_fmt(D)} mm is too shallow for shelf slots at t = "
            f"{_fmt(t)}: needs D ≥ 11t + 2·fit = {_fmt(11 * t + 2 * fit)} mm"
        )


def _validate_box(
    W: float, H: float, D: float, t: float, joint: str, fit: float, cutter_d: float
) -> None:
    if joint not in JOINTS:
        raise FlatpackError(f"joint must be one of {', '.join(JOINTS)}, not {joint!r}")
    if t <= 0:
        raise FlatpackError(f"t must be positive, not {_fmt(t)}")
    if min(W, H, D) <= 0:
        raise FlatpackError("W, H and D must be positive")
    if fit < 0 or cutter_d < 0:
        raise FlatpackError("fit and cutter_d cannot be negative")
    for name, L in (("W", W), ("H", H), ("D", D)):
        if 2 * t >= L:
            raise FlatpackError(
                f"{name} = {_fmt(L)} mm leaves no cavity at t = {_fmt(t)} mm "
                f"(needs more than 2t = {_fmt(2 * t)})"
            )


def box_panels(
    W: float,
    H: float,
    D: float,
    *,
    t: float,
    shelves: Sequence[float] = (),
    joint: str = "finger",
    fit: float = 0.0,
    cutter_d: float = 0.0,
) -> list[Panel]:
    """The panels of a ``W × H × D`` open-front box, pre-kerf."""
    shelves = sorted(float(h) for h in shelves)
    _validate_box(W, H, D, t, joint, fit, cutter_d)
    _check_shelves(shelves, W, H, D, t, fit)
    f2 = fit / 2
    slots = [
        box(yc - 1.5 * t - f2, t + h - f2, yc + 1.5 * t + f2, 2 * t + h + f2)
        for h in shelves
        for yc in _shelf_centres(D, t)
    ]
    panels: list[Panel] = []

    def add(
        pid: str, name: str, face: Polygon, removed: list[Polygon], fr: Frame
    ) -> None:
        poly = face.difference(unary_union(removed)) if removed else face
        if not isinstance(poly, Polygon) or poly.is_empty:
            raise FlatpackError(f"panel {pid} falls apart with these parameters")
        panels.append(Panel(pid, name, dog_bones(poly, cutter_d), fr, t))

    side_frames = (
        ("side-l", "left side", Frame((0.0, 0.0, 0.0), _Y, _Z, _X)),
        ("side-r", "right side", Frame((W - t, 0.0, 0.0), _Y, _Z, _X)),
    )
    if joint == "finger":
        nD, _ = finger_count(D, t, "side-top/side-bottom (D)")
        nH, _ = finger_count(H, t, "side-back (H)")
        nW, _ = finger_count(W, t, "top-back/bottom-back (W)")
        # Sides: own the odd positions on every edge.
        removed = []
        for a, b in _unowned(D, nD, own_odd=True):
            removed += [box(a - f2, H - t, b + f2, H), box(a - f2, 0, b + f2, t)]
        for a, b in _unowned(H, nH, own_odd=True):
            removed.append(box(D - t, a - f2, D, b + f2))
        removed += slots
        for pid, name, fr in side_frames:
            add(pid, name, box(0, 0, D, H), removed, fr)
        # Top and bottom: even positions against the sides, odd against the back,
        # end fingers on the back edge stop t short (corner cubes are the side's).
        removed = []
        for a, b in _unowned(D, nD, own_odd=False):
            removed += [box(0, a - f2, t, b + f2), box(W - t, a - f2, W, b + f2)]
        for a, b in _unowned(W, nW, own_odd=True):
            removed.append(box(a - f2, D - t, b + f2, D))
        removed += [box(0, D - t, t, D), box(W - t, D - t, W, D)]
        for pid, name, z0 in (("top", "top", H - t), ("bottom", "bottom", 0.0)):
            add(pid, name, box(0, 0, W, D), removed, Frame((0.0, 0.0, z0), _X, _Y, _Z))
        # Back: even positions on every edge.
        removed = []
        for a, b in _unowned(H, nH, own_odd=False):
            removed += [box(0, a - f2, t, b + f2), box(W - t, a - f2, W, b + f2)]
        for a, b in _unowned(W, nW, own_odd=False):
            removed += [box(a - f2, H - t, b + f2, H), box(a - f2, 0, b + f2, t)]
        add(
            "back",
            "back",
            box(0, 0, W, H),
            removed,
            Frame((0.0, D - t, 0.0), _X, _Z, _Y),
        )
    else:
        for pid, name, fr in side_frames:
            add(pid, name, box(0, 0, D, H), list(slots), fr)
        for pid, name, z0 in (("top", "top", H - t), ("bottom", "bottom", 0.0)):
            add(pid, name, box(0, 0, W - 2 * t, D), [], Frame((t, 0.0, z0), _X, _Y, _Z))
        add(
            "back",
            "back",
            box(0, 0, W - 2 * t, H - 2 * t),
            [],
            Frame((t, D - t, t), _X, _Z, _Y),
        )
    for k, h in enumerate(shelves, start=1):
        body = box(t, 0, W - t, D - t)
        tabs = [
            box(x0, yc - 1.5 * t, x0 + t, yc + 1.5 * t)
            for yc in _shelf_centres(D, t)
            for x0 in (0.0, W - t)
        ]
        shelf = unary_union([body, *tabs])
        if not isinstance(shelf, Polygon):
            raise FlatpackError(f"shelf {k} is not one piece")
        panels.append(
            Panel(
                f"shelf-{k}",
                f"shelf {k} (underside at {_fmt(h)} mm)",
                dog_bones(shelf, cutter_d),
                Frame((0.0, 0.0, t + h), _X, _Y, _Z),
                t,
            )
        )
    return panels


def _unit(dx: float, dy: float) -> tuple[float, float]:
    length = math.hypot(dx, dy)
    return (dx / length, dy / length)


def inside_corners(
    poly: Polygon,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """``(corner point, unit bisector into the removed region)`` for every
    inside (reflex) corner of the material, exterior and holes alike."""
    poly = orient(poly, 1.0)  # exterior CCW, holes CW: material is on the left
    out = []
    for ring in (poly.exterior, *poly.interiors):
        pts = list(ring.coords)[:-1]
        m = len(pts)
        for i in range(m):
            (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[(i + 1) % m]
            d1 = _unit(x1 - x0, y1 - y0)
            d2 = _unit(x2 - x1, y2 - y1)
            cross = d1[0] * d2[1] - d1[1] * d2[0]
            if cross < -1e-9:  # a right turn: the material is reflex here
                out.append(((x1, y1), _unit(d2[0] - d1[0], d2[1] - d1[1])))
    return out


def dog_bones(poly: Polygon, cutter_d: float) -> Polygon:
    """Add a dog-bone (radius ``cutter_d/2``) at every inside corner so a
    square mate seats; identity at ``cutter_d = 0``."""
    if cutter_d <= 0:
        return poly
    r = cutter_d / 2
    circles = [
        Point(px + r * bx, py + r * by).buffer(r)
        for (px, py), (bx, by) in inside_corners(poly)
    ]
    if not circles:
        return poly
    out = poly.difference(unary_union(circles))
    if not isinstance(out, Polygon):
        raise FlatpackError(
            f"dog-bones of cutter_d = {_fmt(cutter_d)} mm sever the part"
        )
    return out


def kerf_offset(poly: Polygon, kerf: float) -> Polygon:
    """The cut path: material grown by ``kerf/2`` (outline out, slots in),
    mitre joins so rectilinear corners stay square."""
    if kerf <= 0:
        return poly
    out = poly.buffer(kerf / 2, join_style="mitre", mitre_limit=10.0)
    if not isinstance(out, Polygon):
        raise FlatpackError(f"kerf {_fmt(kerf)} mm merges or splits the part")
    return out


def rings(poly: Polygon, tol: float = 1e-9) -> list[tuple[tuple[float, float], ...]]:
    """Deterministic rings for output: exterior first (CCW), then holes
    (CW), each starting at its lowest-left vertex, collinear points dropped,
    no repeated closing point."""
    poly = orient(poly.simplify(tol), 1.0)
    out = []
    for ring in (poly.exterior, *poly.interiors):
        pts = [(float(x), float(y)) for x, y in ring.coords[:-1]]
        start = min(
            range(len(pts)), key=lambda i: (round(pts[i][0], 9), round(pts[i][1], 9))
        )
        out.append(tuple(pts[start:] + pts[:start]))
    return out

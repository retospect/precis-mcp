"""Rectangle nesting: a bottom-left skyline packer over each part's
bounding box (after kerf), 90° rotation allowed.

Spacing between parts and the margin to the sheet edge are both exactly
``max(cutter_d, kerf, 1 mm)`` (:func:`spacing_for`). With a sheet given the
packer fits everything or raises :class:`NestingError` naming the shortfall
— the summed bounding-box area of the unplaced parts, in mm². With
``sheet=None`` (Reto's laser bed is no constraint) it packs a strip, trying
one strip width per "k parts side by side", keeps the smallest sheet, and
reports the sheet it used: the placement's bounding box plus the margin.
Rectangle packing is enough for a box; nothing here is a polygon nester.
"""

from __future__ import annotations

from dataclasses import dataclass

from precis_se.flatpack.panels import FlatpackError, _fmt

_EPS = 1e-9


def spacing_for(kerf: float, cutter_d: float) -> float:
    """Part-to-part spacing and sheet margin: ``max(cutter_d, kerf, 1 mm)``."""
    return max(cutter_d, kerf, 1.0)


@dataclass(frozen=True)
class Placement:
    part: str
    x: float
    y: float
    rotated: bool
    w: float
    h: float


@dataclass(frozen=True)
class Nesting:
    sheet: tuple[float, float]
    margin: float
    placements: tuple[Placement, ...]
    #: Whether the sheet was given (``True``) or derived from the strip.
    sheet_given: bool


class NestingError(FlatpackError):
    def __init__(
        self,
        placed: int,
        unplaced: list[str],
        shortfall_mm2: float,
        sheet: tuple[float, float],
    ):
        self.placed = placed
        self.unplaced = unplaced
        self.shortfall_mm2 = shortfall_mm2
        super().__init__(
            f"sheet {_fmt(sheet[0])} x {_fmt(sheet[1])} mm fits {placed} part(s); "
            f"{len(unplaced)} do not ({', '.join(unplaced)}): short by "
            f"{_fmt(shortfall_mm2)} mm² of bounding-box area"
        )


Item = tuple[str, float, float]


def nest(
    items: list[Item], *, spacing: float, sheet: tuple[float, float] | None
) -> Nesting:
    """Place ``(id, w, h)`` bounding boxes. See the module docstring."""
    rank = {it[0]: i for i, it in enumerate(items)}
    order = sorted(
        items, key=lambda it: (-max(it[1], it[2]), -it[1] * it[2], rank[it[0]])
    )
    if sheet is not None:
        inner_w = sheet[0] - 2 * spacing + spacing
        inner_h = sheet[1] - 2 * spacing + spacing
        placed, unplaced = _pack(order, inner_w, inner_h, spacing)
        if unplaced:
            short = sum(w * h for _, w, h in unplaced)
            raise NestingError(len(placed), [i for i, _, _ in unplaced], short, sheet)
        return Nesting(sheet, spacing, tuple(_shift(placed, spacing)), True)
    best: (
        tuple[tuple[float, float, float], list[Placement], tuple[float, float]] | None
    ) = None
    widths = sorted(
        {sum(w + spacing for _, w, _ in order[:k]) for k in range(1, len(order) + 1)}
    )
    for width in widths:
        placed, unplaced = _pack(order, width, None, spacing)
        if unplaced:
            continue
        used_w = max(p.x + p.w for p in placed) + 2 * spacing
        used_h = max(p.y + p.h for p in placed) + 2 * spacing
        key = (used_w * used_h, max(used_w, used_h), width)
        if best is None or key < best[0]:
            best = (key, placed, (used_w, used_h))
    if best is None:
        return Nesting((2 * spacing, 2 * spacing), spacing, (), False)
    _, placed, used = best
    return Nesting(used, spacing, tuple(_shift(placed, spacing)), False)


def _shift(placed: list[Placement], by: float) -> list[Placement]:
    return [Placement(p.part, p.x + by, p.y + by, p.rotated, p.w, p.h) for p in placed]


def _pack(
    items: list[Item], width: float, height: float | None, s: float
) -> tuple[list[Placement], list[Item]]:
    skyline: list[list[float]] = [[0.0, 0.0, width]]
    placed: list[Placement] = []
    unplaced: list[Item] = []
    for pid, w, h in items:
        best: tuple[float, float, int, bool] | None = None
        for rotated, (rw, rh) in ((False, (w, h)), (True, (h, w))):
            for idx in range(len(skyline)):
                y = _fits(skyline, idx, rw + s, rh + s, height)
                if y is None:
                    continue
                cand = (y + rh + s, skyline[idx][0], idx, rotated)
                if best is None or cand < best:
                    best = cand
        if best is None:
            unplaced.append((pid, w, h))
            continue
        top, x, _idx, rotated = best
        rw, rh = (h, w) if rotated else (w, h)
        placed.append(Placement(pid, x, top - rh - s, rotated, rw, rh))
        _raise(skyline, x, top, rw + s)
    return placed, unplaced


def _fits(
    skyline: list[list[float]], idx: int, w: float, h: float, height: float | None
) -> float | None:
    """The y at which a ``w × h`` rect sits when its left edge is at segment
    ``idx``'s x, or ``None`` if it runs off the strip."""
    end = skyline[idx][0] + w
    y = 0.0
    i = idx
    while True:
        sx, sy, sw = skyline[i]
        y = max(y, sy)
        if height is not None and y + h > height + _EPS:
            return None
        if sx + sw >= end - _EPS:
            return y
        i += 1
        if i >= len(skyline):
            return None


def _raise(skyline: list[list[float]], x: float, top: float, w: float) -> None:
    """Lift the skyline to ``top`` over ``[x, x+w)`` and merge level runs."""
    out: list[list[float]] = []
    for sx, sy, sw in skyline:
        if sx < x - _EPS:
            out.append([sx, sy, min(sw, x - sx)])
        if sx + sw > x + w + _EPS:
            start = max(sx, x + w)
            out.append([start, sy, sx + sw - start])
    out.append([x, top, w])
    out.sort(key=lambda seg: seg[0])
    merged = [out[0]]
    for seg in out[1:]:
        if abs(seg[1] - merged[-1][1]) < _EPS:
            merged[-1][2] += seg[2]
        else:
            merged.append(seg)
    skyline[:] = merged

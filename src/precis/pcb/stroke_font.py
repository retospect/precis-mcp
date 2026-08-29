"""A minimal single-stroke (Hershey-style) vector font for silkscreen text.

**No font file, no new dependency.** This is a plain data table — one
entry per glyph, a tuple of polylines drawn in a unit EM box — plus
:func:`layout_text`, which places a string's glyphs at a caller-chosen
anchor/height/rotation/mirror. There was no font of any kind anywhere in
this repo before this module (a full sweep found zero hershey/glyph-table
hits under ``src/``); :mod:`precis.pcb.svg`'s only text is a
browser-rendered ``<text>`` diagram title, which cannot reach a Gerber
file — silk needs real vector strokes, so this table is genuinely the only
path, not a shortcut around an existing one.

**Rotation/mirror is the ONE existing implementation, reused.**
:func:`precis.pcb.landpattern.rotate_offset` is the transform every placed
pad offset in this subsystem already goes through (mirror the local X
coordinate for a bottom-side instance, THEN rotate clockwise-from-north,
per that function's own docstring). :func:`layout_text` and
:func:`text_bbox_corners` route every glyph point and every bbox corner
through that SAME function, so silk text rotates/mirrors identically to
the part it labels — a second, textually-similar-but-different rotation
formula here is exactly the "one rule, two call sites" defect class this
codebase's docstrings keep calling out.

**Coordinate convention.** Each glyph is authored on an integer design
grid (:data:`_GRID_W` columns x :data:`_GRID_H` rows) and normalized so
cap height is exactly 1.0 EM unit (baseline at y=0, cap top at y=1) —
see :data:`GLYPHS`. :func:`layout_text` scales EM units to a caller's
``height_mm``; nothing outside this module ever sees the raw grid.

**Uppercase-only.** Lowercase input folds to uppercase before lookup —
the same case-insensitivity classic Hershey "simplex" stroke fonts use.
An unsupported character (anything not in :data:`GLYPHS`) draws nothing
but still advances the cursor, so surrounding text doesn't collide with
where it would have been.
"""

from __future__ import annotations

from typing import Final

from precis.pcb.landpattern import rotate_offset

Point = tuple[float, float]

# ─────────────────────────────────────────────────────────────────────
# raw glyph data — integer design-grid units, x:[0,_GRID_W] y:[0,_GRID_H]
# ─────────────────────────────────────────────────────────────────────
_GRID_W: Final[int] = 4
_GRID_H: Final[int] = 6

_O = (  # 0/O's shared octagon-ish loop, reused by 0, O, Q
    (0, 1),
    (0, 5),
    (1, 6),
    (3, 6),
    (4, 5),
    (4, 1),
    (3, 0),
    (1, 0),
    (0, 1),
)
_P_BODY = ((0, 0), (0, 6), (3, 6), (4, 5), (3, 4), (0, 4))  # spine + top loop

_RAW_GLYPHS: Final[dict[str, tuple[tuple[tuple[float, float], ...], ...]]] = {
    "A": (((0, 0), (2, 6), (4, 0)), ((1, 2), (3, 2))),
    "B": (
        ((0, 6), (3, 6), (4, 5), (3, 4), (0, 4)),
        ((0, 3), (3, 3), (4, 2), (3, 0), (0, 0)),
        ((0, 0), (0, 6)),
    ),
    "C": (((4, 5), (2, 6), (0, 5), (0, 1), (2, 0), (4, 1)),),
    "D": (((0, 0), (0, 6)), ((0, 6), (2, 6), (4, 4), (4, 2), (2, 0), (0, 0))),
    "E": (((4, 6), (0, 6), (0, 0), (4, 0)), ((0, 3), (3, 3))),
    "F": (((0, 0), (0, 6), (4, 6)), ((0, 3), (3, 3))),
    "G": (((4, 5), (2, 6), (0, 5), (0, 1), (2, 0), (4, 1), (4, 3), (2, 3)),),
    "H": (((0, 0), (0, 6)), ((4, 0), (4, 6)), ((0, 3), (4, 3))),
    "I": (((2, 0), (2, 6)),),
    "J": (((3, 6), (3, 1), (2, 0), (1, 0), (0, 1)),),
    "K": (((0, 0), (0, 6)), ((4, 6), (0, 3)), ((0, 3), (4, 0))),
    "L": (((0, 6), (0, 0), (4, 0)),),
    "M": (((0, 0), (0, 6), (2, 3), (4, 6), (4, 0)),),
    "N": (((0, 0), (0, 6), (4, 0), (4, 6)),),
    "O": (_O,),
    "P": (_P_BODY,),
    "Q": (_O, ((2, 2), (4, 0))),
    "R": (_P_BODY, ((0, 4), (4, 0))),
    "S": (
        (
            (4, 5),
            (3, 6),
            (1, 6),
            (0, 5),
            (0, 4),
            (1, 3),
            (3, 3),
            (4, 2),
            (4, 1),
            (3, 0),
            (1, 0),
            (0, 1),
        ),
    ),
    "T": (((0, 6), (4, 6)), ((2, 6), (2, 0))),
    "U": (((0, 6), (0, 1), (1, 0), (3, 0), (4, 1), (4, 6)),),
    "V": (((0, 6), (2, 0), (4, 6)),),
    "W": (((0, 6), (1, 0), (2, 3), (3, 0), (4, 6)),),
    "X": (((0, 0), (4, 6)), ((0, 6), (4, 0))),
    "Y": (((0, 6), (2, 3), (4, 6)), ((2, 3), (2, 0))),
    "Z": (((0, 6), (4, 6), (0, 0), (4, 0)),),
    "0": (_O, ((0, 0), (4, 6))),
    "1": (((1, 5), (2, 6), (2, 0)), ((1, 0), (3, 0))),
    "2": (((0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (0, 0), (4, 0)),),
    "3": (
        ((0, 6), (3, 6), (4, 5), (3, 3), (0, 3)),
        ((0, 3), (3, 3), (4, 2), (3, 0), (0, 0)),
    ),
    "4": (((3, 6), (0, 2), (4, 2)), ((3, 5), (3, 0))),
    "5": (((4, 6), (0, 6), (0, 3), (3, 3), (4, 2), (4, 1), (3, 0), (0, 0)),),
    "6": (
        (
            (4, 6),
            (1, 6),
            (0, 5),
            (0, 1),
            (1, 0),
            (3, 0),
            (4, 1),
            (4, 3),
            (3, 4),
            (1, 4),
        ),
    ),
    "7": (((0, 6), (4, 6), (1, 0)),),
    "8": (
        ((1, 6), (3, 6), (4, 5), (4, 4), (3, 3), (1, 3), (0, 4), (0, 5), (1, 6)),
        ((1, 3), (3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1), (0, 2), (1, 3)),
    ),
    "9": (
        (
            (0, 0),
            (3, 0),
            (4, 1),
            (4, 5),
            (3, 6),
            (1, 6),
            (0, 5),
            (0, 3),
            (1, 2),
            (3, 2),
        ),
    ),
    "-": (((1, 3), (3, 3)),),
    "_": (((0, 0), (4, 0)),),
    ".": (((1.8, 0), (2.2, 0.4), (1.8, 0.4), (1.8, 0)),),
    "+": (((2, 1), (2, 5)), ((0, 3), (4, 3))),
    "/": (((0, 0), (4, 6)),),
}


def _normalize(
    raw: tuple[tuple[tuple[float, float], ...], ...],
) -> tuple[tuple[Point, ...], ...]:
    return tuple(tuple((x / _GRID_H, y / _GRID_H) for x, y in stroke) for stroke in raw)


#: char -> polylines, in EM units (cap height 1.0, baseline y=0). ``" "``
#: is present with zero strokes — a supported glyph that draws nothing but
#: still advances the cursor (see module docstring).
GLYPHS: Final[dict[str, tuple[tuple[Point, ...], ...]]] = {
    ch: _normalize(strokes) for ch, strokes in _RAW_GLYPHS.items()
}
GLYPHS[" "] = ()

#: Monospace advance per glyph cell, in EM units — the raw grid width plus
#: a small inter-character gutter (also expressed in EM units, so it scales
#: with ``height_mm`` exactly like every glyph stroke does).
ADVANCE_EM: Final[float] = _GRID_W / _GRID_H + 0.15


def supported(ch: str) -> bool:
    """Whether ``ch`` (case-folded) has real strokes — i.e. is drawable,
    not just advance-only like an unrecognized character or space."""
    return ch.upper() in GLYPHS


def text_width_mm(text: str, height_mm: float) -> float:
    """The full advance width of ``text`` at ``height_mm`` cap height,
    left-to-right, before any rotation/mirror — every character (including
    an unsupported one, which still advances) counts once."""
    return len(text) * ADVANCE_EM * height_mm


def _h_shift(total_w_mm: float, h_align: str) -> float:
    if h_align == "left":
        return 0.0
    if h_align == "center":
        return -total_w_mm / 2.0
    if h_align == "right":
        return -total_w_mm
    raise ValueError(f"h_align must be 'left'/'center'/'right', got {h_align!r}")


def _v_shift(height_mm: float, v_align: str) -> float:
    if v_align == "baseline":
        return 0.0
    if v_align == "middle":
        return -height_mm / 2.0
    if v_align == "top":
        return -height_mm
    raise ValueError(f"v_align must be 'baseline'/'middle'/'top', got {v_align!r}")


def _local_bounds(
    text: str, height_mm: float, h_align: str, v_align: str
) -> tuple[float, float, float, float]:
    """``(x0, y0, x1, y1)`` of ``text``'s advance box in the LOCAL frame —
    before rotate/mirror/anchor — shared by :func:`layout_text` and
    :func:`text_bbox_corners` so the two can never disagree about where the
    text actually sits."""
    total_w = text_width_mm(text, height_mm)
    x0 = _h_shift(total_w, h_align)
    y0 = _v_shift(height_mm, v_align)
    return x0, y0, x0 + total_w, y0 + height_mm


def layout_text(
    text: str,
    *,
    anchor: Point,
    height_mm: float,
    rotation_deg: float = 0.0,
    mirror: bool = False,
    h_align: str = "left",
    v_align: str = "baseline",
) -> list[list[Point]]:
    """Lay ``text`` out into board-space polylines.

    ``anchor`` is a BOARD-space point (already placed — this function does
    not itself know about an instance's own position, only the transform
    applied around it); ``h_align``/``v_align`` choose which point of the
    text's local advance box sits at ``anchor`` before the mirror+rotate
    (``'left'``/``'baseline'`` — the default — is the classic typesetting
    origin; ``'center'``/``'middle'`` is what a caller centering a label on
    a part wants). Returns one list of ``(x, y)`` mm points per stroke —
    the caller turns each into a ``{"shape":"line", "start":.., "end":..}``
    chain (see :mod:`precis.pcb.silk`), never a font concern.
    """
    x0, y0, _x1, _y1 = _local_bounds(text, height_mm, h_align, v_align)
    ax, ay = anchor
    strokes_out: list[list[Point]] = []
    cursor = x0
    for ch in text:
        glyph = GLYPHS.get(ch.upper())
        if glyph is None:
            cursor += ADVANCE_EM * height_mm
            continue
        for stroke in glyph:
            pts: list[Point] = []
            for gx, gy in stroke:
                lx = cursor + gx * height_mm
                ly = y0 + gy * height_mm
                rx, ry = rotate_offset(lx, ly, rotation_deg, mirrored=mirror)
                pts.append((ax + rx, ay + ry))
            if len(pts) >= 2:
                strokes_out.append(pts)
        cursor += ADVANCE_EM * height_mm
    return strokes_out


def text_bbox_corners(
    text: str,
    *,
    anchor: Point,
    height_mm: float,
    rotation_deg: float = 0.0,
    mirror: bool = False,
    h_align: str = "left",
    v_align: str = "baseline",
) -> list[Point]:
    """The 4 board-space corners of ``text``'s advance box, through the
    SAME transform :func:`layout_text` uses — so a caller checking "does
    this text's footprint clear a pad" is checking exactly the box the
    glyphs are drawn inside, not an approximation of it."""
    x0, y0, x1, y1 = _local_bounds(text, height_mm, h_align, v_align)
    ax, ay = anchor
    corners: list[Point] = []
    for lx, ly in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        rx, ry = rotate_offset(lx, ly, rotation_deg, mirrored=mirror)
        corners.append((ax + rx, ay + ry))
    return corners


__all__ = [
    "ADVANCE_EM",
    "GLYPHS",
    "Point",
    "layout_text",
    "supported",
    "text_bbox_corners",
    "text_width_mm",
]

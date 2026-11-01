"""Board-outline containment — the one test every pose mover shares.

A part is on the board when its COURTYARD ring
(:func:`~.proxy._ring_geom`) lies inside the outline POLYGON: the notion
:func:`~.legalize.legalize_pose` pulls escapees back by. The movers'
centre clamps (``_legal_xy``) know only the outline's bounding box,
which on a concave outline (taz-leaf's tip and neck) admits centres
whose courtyard is off the board; the shove LP's no-inversion guard
does not cover the gap either — a courtyard already TOUCHING the edge
has ring vertices on the boundary with no triangle beyond them, so
nothing in the mesh resists the outward step. The anneal's legality
debt prices pin centres and body clearance, not the courtyard, and only
as a tradeable total.

Graded as the courtyard area outside the outline, so a mover holds
each part to "no further off the board than it already is"
(:func:`stays_on_board`): an on-board part stays on, an overhanging one
(an edge connector, a jittered restart) may only come in. "On" allows
``ON_BOARD_TOL_MM2`` off: a ring sliding along an edge — exactly where
an LP optimum parks it — drifts off the line by float noise, which is
not an escape (a strict ``covers`` flips on it, and legalize would pull
back, even teleport, a part that sits on the edge).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import shapely
from shapely.geometry import Polygon

from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.proxy import _EPS, _ring_geom

#: Courtyard area (mm²) off the outline that still counts as on the
#: board — a decade under the floor ``mesh`` reports an outline
#: conflict above (``proxy._EPS``), so a part this module calls on the
#: board is never one the mesh calls off it.
ON_BOARD_TOL_MM2 = _EPS / 10.0


def board_polygon(d: TopoDesign) -> Any | None:
    """The outline as a PREPARED shapely polygon (repeated ``covers``
    calls are the hot path) — ``None`` when the design carries no
    outline, where nothing is contained."""
    if len(d.outline) < 3:
        return None
    g = Polygon(np.asarray(d.outline, dtype=np.float64))
    if g.is_empty:
        return None
    shapely.prepare(g)
    return g


def outside_mm2(board: Any | None, ring: Any | None) -> float:
    """Courtyard area off the board in mm² — 0.0 when the outline
    covers it, for a ringless part, or with no outline."""
    if board is None or ring is None or board.covers(ring):
        return 0.0
    return float(ring.difference(board).area)


def on_board(board: Any | None, ring: Any | None) -> bool:
    """Whether ``ring`` lies inside the outline (boundary contact and
    ``ON_BOARD_TOL_MM2`` of float drift allowed)."""
    return outside_mm2(board, ring) <= ON_BOARD_TOL_MM2


def part_outside_mm2(
    d: TopoDesign, board: Any | None, row: np.ndarray, part: int
) -> float:
    """:func:`outside_mm2` of ``part``'s courtyard at pose ``row``."""
    if board is None:
        return 0.0
    return outside_mm2(board, _ring_geom(d, row, part))


def stays_on_board(before_mm2: float, after_mm2: float) -> bool:
    """The movers' rule over two :func:`outside_mm2` reads: a part that
    was on the board stays on it; one already overhanging may not
    overhang more. Holds at a zero step by construction, so a mover can
    always fall back to not moving the part."""
    if before_mm2 <= ON_BOARD_TOL_MM2:
        return after_mm2 <= ON_BOARD_TOL_MM2
    return after_mm2 <= before_mm2 + ON_BOARD_TOL_MM2


__all__ = [
    "ON_BOARD_TOL_MM2",
    "board_polygon",
    "on_board",
    "outside_mm2",
    "part_outside_mm2",
    "stays_on_board",
]

"""The incremental straight-airwire proxy (Decision 7): crossings plus
length, delta-scored for the one part a move touches.

An airwire is one edge of a net's MST over its PINS' board positions —
pin-level, not centroid, because 1b's pin assignment moves the wire
endpoints the crossings are counted on. ``try_move`` rebuilds only the
moved part's nets' MSTs and re-counts crossings vectorised numpy,
bounding boxes first, so an eval is O(the part's wires × all wires)
instead of O(all wires²).

Exactness contract: ``try_move`` + ``commit`` is bit-for-bit the full
recompute — same crossing predicate as
:func:`precis.pcb.geom.segments_cross` (proper straddle only:
shared-endpoint, collinear and touch-only do not count; same-net pairs
never count), same deterministic Prim order for the MST. The
1000-random-move parity test pins that, because a placer optimising a
metric that drifts from its recompute is optimising a ghost.

Cost is the share-weighted, reference-normalised sum — at the seed
pose each term contributes exactly its share of J (ObjectiveShares /
Decision 18), which is what makes "0.8 crossings / 0.2 ratsnest" a
statement about priority rather than unit conversion. The third term,
``overlap``, is the same-side courtyard overlap AREA in mm² plus pad
containment violation in mm — a real violation the placer must price
between rung-2 evals (Reto ruling 2026-10-25), counted with the
``bottom_by_refdes`` split DRC applies (parts on opposite faces overlap
legally) and, since 2026-10-27, the distance of any pad outside the
outline polygon (the DRC's ``outline_containment`` measure — the
mover's centre clamp only sees the outline bbox, which on taz leaf's
non-rectangular outline lets pads slip out through the neck). The
fourth term, ``spread``,
is a board-fill repulsion (the loop's whole-board-usage ask): part
pairs sharing NO net that the same collision predicate admits repel
each other through a Gaussian on part origins, so unconnected clumps
disperse over the outline while net-connected parts still cluster
under the ratsnest pull. The slice-5 rung adds three route proxies
over the same wires. ``vias`` is the via-count ESTIMATE: per net, the
distinct board sides spanned by its non-TH pins minus 1 (a TH pin is a
wildcard — the plated barrel exists on every layer — so it forces
nothing, the ``_net_pin_force`` mirror); on a 2-layer board that is
0/1 per net. ``backtrack`` is fold-back mileage: per net and axis,
Σ|Δ| over the net's wires minus the pin set's extent — "ran north,
then doubled back south" priced exactly; always ≥ 0 because any tree
spans min..max. ``direction`` is the inner-layer ask: wire pairs of
different nets that run at right angles AND pass close score
(1−|cos θ|)·exp(−d²/2s²) on segment midpoints, so the anneal steers
placements whose proxy wires share a compass heading; nets carrying an
authored power width (:attr:`~.design.TopoDesign.net_direction_exempt`)
skip the term — a rail is poured post-route, its heading is nobody's
target. ``silkroom`` is the draw-side silk rule priced at placement:
same-side courtyard pairs whose separation falls short of
:func:`precis.pcb.silk.outline_pair_separation_mm` (two full outline
rings plus the ruled outline-to-outline gap, minus the clearance the
courtyard polygons already carry) pay the shortfall in mm — so the
anneal spreads parts before the silk pass must bend an outline, share
one wall, or push a refdes label out along its relocation ladder.
``margin`` is the maximin twin (Reto 2026-10-29 — "the minimal margin
around each part is at least X; maximize that"): per part, the WORST of
four margin axes — the courtyard ring's gap to the board edge against
the flat ``silk.edge_req_mm`` (the full outline's room: any ring that
merely overlaps the edge-margin band loses its outer wall to the edge —
a ring-radius-aware want priced big rings 0 and let run 34's R23 sit
squished flat against the leaf outline), the ring's separation to its
nearest same-side neighbour against the silk-room requirement (the
SAME pair scan ``silkroom`` prices), the pad-union body's distance to
the mounting-hole keep-outs against ``HOLE_WANT_MM``, and the LABEL
axis — the distance the refdes label will have to walk: 0 while one of
silk's eight canonical LABEL_SPOT_ORDER boxes sits clear of every
colliding ring, the edge-margin-eroded outline and the hole keep-outs,
else the first free relocation-ladder rung's excess over the canonical
gap (capped at ``_LBL_NO_SPOT_MM`` — the graded excess is what gives
the anneal a gradient to unstick a boxed-in part; a flat constant
saturates the softmin and prices nothing) — then a softmin over parts
of those worst shortfalls. Where ``silkroom``'s SUM lets one cramped
part hide in the total, the softmin always prices the worst-off part
(taz-leaf run 31: two courtyards 0.25–0.37mm off the board edge, an
L2–U1 pair inside the silk rule, a refdes label inside a mounting
hole; runs 34-38: 23-33 labels >3mm off their parts until the axis was
graded and the room capability-priced). The settled pose's label pins
ride :func:`label_spots_for` — the same canonical-spot greedy the silk
pass runs, persisted as instance ``meta.label_spot`` so the draw honors
the space the placer preserved; the route stage keeps vias and wires
out of the pinned boxes (:func:`label_boxes_for` keepout zones). All
four silk figures come from :class:`TopoDesign.silk` — resolved from
the board's fab capability by :func:`design_from_ir`, the SAME
functions the silk pass draws with; pricing with the module fallbacks
on a capability board shared an outline wall across every tight pair
(runs 34-37).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import shapely
from shapely.geometry import Point, Polygon

from precis.pcb.silk import (
    DEFAULT_REFDES_HEIGHT_MM,
    LABEL_SPOT_ORDER,
)
from precis.pcb.stroke_font import text_width_mm
from precis.pcb.topoplace.design import ObjectiveShares, SilkRoom, TopoDesign

#: Same straddle epsilon as ``geom.segments_cross`` — parity between the
#: incremental count and the scalar predicate is literal, not "close".
_EPS = 1e-9


def local_to_world(pts: np.ndarray, pose_row: np.ndarray) -> np.ndarray:
    """Footprint-local points → board space under a ``(4,)`` pose row —
    mirror (side) then quarter-turn, the same order
    :func:`precis.pcb.pose.rel_to_world` fixes, specialised to exact
    quarter turns so no trig enters the hot loop."""
    pts = np.asarray(pts, dtype=np.float64)
    x, y, rot_q, side = (float(v) for v in pose_row[:4])
    mx = np.where(side >= 0.5, -pts[:, 0], pts[:, 0])
    ly = pts[:, 1]
    r = round(rot_q) % 4
    if r == 0:
        rx, ry = mx, ly
    elif r == 1:  # 90° cw-from-north: (x, y) -> (y, -x)
        rx, ry = ly, -mx
    elif r == 2:
        rx, ry = -mx, -ly
    else:  # r == 3
        rx, ry = -ly, mx
    return np.column_stack([x + rx, y + ry])


def pin_world(d: TopoDesign, pose: np.ndarray, part: int) -> np.ndarray:
    """The part's pins in board space under a ``(4,)`` pose row."""
    return local_to_world(d.pin_xy[d.pin_part == part], pose)


def zone_world_ring(d: TopoDesign, z, pose: np.ndarray) -> np.ndarray:
    """The zone's ring in world mm at ``pose`` — a part-owned mounting
    hole's ring is stored in the owner's footprint frame and rides its
    pose (the DRC recomputes the keep-out from the owner's current
    pose); every other zone is already world mm."""
    if z.part_local and 0 <= z.part < len(d.parts):
        return local_to_world(np.asarray(z.ring, dtype=np.float64), pose[z.part])
    return np.asarray(z.ring, dtype=np.float64)


def zone_world_circle(
    d: TopoDesign, z, pose: np.ndarray
) -> tuple[float, float, float] | None:
    """The zone's keep-out ``(cx, cy, r)`` in world mm at ``pose`` — a
    part-owned hole's centre rides its owner's pose like
    :func:`zone_world_ring`'s ring does (mirror + quarter-turn preserve
    the radius). ``None`` for a zone that carries no circle — every kind
    but ``mounting_hole``, which the placer and DRC judge by exact
    body-to-centre distance (gr529963 fix1), never by polygon overlap."""
    if z.circle is None:
        return None
    cx, cy, r = z.circle
    if z.part_local and 0 <= z.part < len(d.parts):
        cx, cy = (
            float(v) for v in local_to_world(np.asarray([[cx, cy]]), pose[z.part])[0]
        )
    return float(cx), float(cy), float(r)


def _ring_geom(d: TopoDesign, row: np.ndarray, part: int):
    """The part's courtyard in board space as a shapely polygon —
    ``None`` for a part with no ring (synthetic designs). Body reads go
    through :func:`_body_geom`, which carries the per-part courtyard
    fallback a mounting-hole test needs."""
    ring = d.part_outline[part] if part < len(d.part_outline) else ()
    pts = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 4:
        return None
    g = Polygon(local_to_world(pts, row)[:-1])
    return g if not g.is_empty and g.area > _EPS else None


def _body_geom(d: TopoDesign, row: np.ndarray, part: int):
    """The part's pad-union BODY in board space as a shapely polygon —
    :func:`_ring_geom`'s twin over :attr:`TopoDesign.part_body`
    (gr529963: the fab bar measures the body). A part without a body
    ring (synthetic designs carry no ``part_body``) falls back to its
    courtyard ring, so a hand-built design prices exactly as before;
    ``None`` for a part with neither."""
    ring = d.part_body[part] if part < len(d.part_body) else ()
    pts = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 4:
        return _ring_geom(d, row, part)
    g = Polygon(local_to_world(pts, row)[:-1])
    return g if not g.is_empty and g.area > _EPS else None


def through_hole_parts(d: TopoDesign) -> np.ndarray:
    """Per-part flag: any pin on a through-hole land (``pin_side == 2``).
    A TH part's pads exist on EVERY copper layer (the plated barrel), so
    an opposite-side pair that includes one still collides — the DRC's
    pad model agrees (``pads_for_ir`` places TH pads on all layers).
    Only an SMD-vs-SMD opposite-side pair is a legal facing."""
    th = np.zeros(len(d.parts), dtype=bool)
    if len(d.pin_side):
        for p in np.unique(d.pin_part[d.pin_side == 2]):
            th[int(p)] = True
    return th


def _collides(sides: np.ndarray, th: np.ndarray, i: int, j: int) -> bool:
    """The courtyard-pair test — same mount side, or either part
    through-hole (its barrel occupies the other face's copper too)."""
    return bool(sides[i] == sides[j] or th[i] or th[j])


def _ring_overlap_total(geoms: list[Any], pose: np.ndarray, th: np.ndarray) -> float:
    """Sum of same-side intersection areas over a placed ring list."""
    sides = np.asarray(pose)[:, 3] >= 0.5
    total = 0.0
    for i, gi in enumerate(geoms):
        if gi is None:
            continue
        x0, y0, x1, y1 = gi.bounds
        for j in range(i + 1, len(geoms)):
            gj = geoms[j]
            if gj is None or not _collides(sides, th, i, j):
                continue
            bx0, by0, bx1, by1 = gj.bounds
            if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                continue
            total += float(gi.intersection(gj).area)
    return total


#: Fallback silk figures for designs built WITHOUT a capability row.
#: Every real design carries :class:`TopoDesign.silk` — resolved from
#: the board's fab-capability row by :func:`design_from_ir`, the SAME
#: functions the silk pass draws with — and the terms read those. These
#: module aliases exist for hand-built synthetic designs (which take
#: ``TopoDesign.silk``'s fallback default) and for the tests that pin
#: the fallback numbers. Under the fallbacks the pair ask is 0.80 mm;
#: JLC 4-layer house defaults want 1.00 mm — pricing with the fallbacks
#: on a capability board shared an outline wall across every tight pair
#: and walked 26-32 of 50 refdes labels (taz-leaf runs 34-37).
_FALLBACK_SILK = SilkRoom.fallback()
_SILK_ROOM_REQ_MM = _FALLBACK_SILK.pair_req_mm
_SILK_EDGE_REQ_MM = _FALLBACK_SILK.edge_req_mm
_LBL_GAP_MM = _FALLBACK_SILK.label_clear_mm  # fallback ring-clearance
(_LBL_ANCHOR_MM, _LBL_CLEAR_MM) = (
    _FALLBACK_SILK.label_anchor_mm,
    _FALLBACK_SILK.label_clear_mm,
)
_LBL_HEIGHT_MM = DEFAULT_REFDES_HEIGHT_MM
#: No canonical spot free ⇒ the silk pass walks the ladder or drops the
#: label — priced well beyond any adjacent outcome (the measure's
#: ">3mm = far" threshold is ~4-5mm on this geometry; the softmin only
#: needs the RELATIVE ordering, and 4mm sits above every achievable
#: walk on a board the anneal can fix).
_LBL_NO_SPOT_MM = 4.0
#: The route-time keep-out ring pads each label box by this much: a via
#: sited near a box bulges the settled courtyard outline by the via
#: annulus + silk clearance (:func:`precis.pcb.silk._via_to_silk_mm`,
#: ~0.3 + 0.15) and the bulge swallows the box at draw time (taz-leaf
#: run 36: pinned boxes intersected by neighbours' via bulges, d=0.000).
#: 0.6 covers the annulus reach with slack.
_LBL_KEEPOUT_PAD_MM = 0.6
#: Bbox slack a moved ring can reach a partner's label candidates:
#: a canonical box extends at most ``(ring half-extent) + gap + text``
#: off its ring — ~6mm for these parts — so a partner farther than this
#: from BOTH the old and new ring bounds cannot have a candidate's
#: free/block status flip. The try_move prescreen uses it the same way
#: the pair axis uses ``_SILK_ROOM_REQ_MM``.
_LBL_TOUCH_REACH_MM = 8.0


def _ring_silkroom_total(
    geoms: list[Any], pose: np.ndarray, th: np.ndarray, req_mm: float
) -> float:
    """Σ same-side ``max(0, req_mm − ring separation)`` over a placed ring
    list — the silk-room shortfall the anneal pays toward zero. The bbox
    prescreen keeps ``req_mm`` of slack: a pair more than ``req_mm`` apart
    cannot score, exactly the margin the shortfall can reach."""
    sides = np.asarray(pose)[:, 3] >= 0.5
    total = 0.0
    for i, gi in enumerate(geoms):
        if gi is None:
            continue
        x0, y0, x1, y1 = gi.bounds
        for j in range(i + 1, len(geoms)):
            gj = geoms[j]
            if gj is None or not _collides(sides, th, i, j):
                continue
            bx0, by0, bx1, by1 = gj.bounds
            if x1 + req_mm < bx0 or bx1 + req_mm < x0:
                continue
            if y1 + req_mm < by0 or by1 + req_mm < y0:
                continue
            total += max(0.0, req_mm - float(gi.distance(gj)))
    return total


#: Margin term, label axis (2026-10-29, taz-leaf run 34: 23/50 refdes
#: labels ended >3mm off their part, R23's 4.9mm — the silk pass walks a
#: relocation ladder when a part's own canonical spots are all blocked,
#: and placement never knew). Per part: 0 when one of silk's eight
#: canonical LABEL_SPOT_ORDER spots has its text box free, else
#: :data:`_LBL_NO_SPOT_MM` — "the label WILL leave the part". The box
#: model mirrors silk's acceptance stricter-than-draw, so a placer-free
#: spot is a silk-free spot (modulo routed vias, which the silk pass
#: resolves by falling back past the pin):
#:   * box ``gap_mm`` clear of every colliding ring (the design's
#:     :class:`SilkRoom.label_gap_mm` — the label gap off the DRAWN
#:     outline plus the draw's own inflation slack);
#:   * box fully inside the outline eroded by the edge-margin ink band
#:     PLUS the glyph overhang (silk measures INK there — the design's
#:     :class:`SilkRoom.edge_erode_mm`);
#:   * box not intersecting a mounting-hole keep-out (R26 printed inside
#:     the zip hole, 2026-10-29).
#: No canonical spot free ⇒ the silk pass walks the ladder or drops the
#: label — priced well beyond any adjacent outcome (the measure's
#: ">3mm = far" threshold is ~4-5mm on this geometry; the softmin only
#: needs the RELATIVE ordering, and 4mm sits above every achievable
#: walk on a board the anneal can fix).
_LBL_NO_SPOT_MM = 4.0
#: The route-time keep-out ring pads each label box by this much: a via
#: sited near a box bulges the settled courtyard outline by the via
#: annulus + silk clearance (:func:`precis.pcb.silk._via_to_silk_mm`,
#: ~0.3 + 0.15) and the bulge swallows the box at draw time (taz-leaf
#: run 36: pinned boxes intersected by neighbours' via bulges, d=0.000).
#: 0.6 covers the annulus reach with slack.
_LBL_KEEPOUT_PAD_MM = 0.6
#: Bbox slack a moved ring can reach a partner's label candidates:
#: a canonical box extends at most ``(ring half-extent) + gap + text``
#: off its ring — ~6mm for these parts — so a partner farther than this
#: from BOTH the old and new ring bounds cannot have a candidate's
#: free/block status flip. The try_move prescreen uses it the same way
#: the pair axis uses ``_SILK_ROOM_REQ_MM``.
_LBL_TOUCH_REACH_MM = 8.0


#: Margin term, hole axis: the body-to-mounting-hole-keep-out distance
#: the softmin asks for. 0.0 — any non-intersection satisfies it (the
#: HARD rule lives in DRC/mesh, ``courtyard_hole``); the axis exists so
#: the softmin SEES holes, and a positive want is the knob that turns
#: "don't touch" into "keep X mm of room".
HOLE_WANT_MM = 0.0

#: The margin softmin's temperature (mm). A hard min would hand the
#: anneal gradient through exactly ONE part — every other part's move
#: scores 0 and the search stalls on the current worst; τ = 0.5 spreads
#: the pull over the worst handful while staying worst-dominated.
_MARGIN_SOFTMIN_TAU_MM = 0.5


def _zone_poly(ring: Any) -> Any | None:
    """A zone ring as a shapely polygon — the same shape
    ``mesh._conflict_data``'s ``_poly`` accepts: closed rings drop their
    duplicate last point; degenerate/empty/non-finite → ``None``."""
    pts = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 4 or not np.isfinite(pts).all():
        return None
    g = Polygon(pts[:-1])
    return g if not g.is_empty and g.area > _EPS else None


def _margin_zone_entries(d: TopoDesign, pose: np.ndarray) -> list[tuple[int, Any]]:
    """``(owner part id, posed polygon)`` per mounting-hole zone — the
    hole axis's keep-out set. A part-owned ring rides its owner's pose
    (:func:`zone_world_ring`), exactly as the mesh's zone pass poses it."""
    out: list[tuple[int, Any]] = []
    for z in d.zones:
        if z.kind != "mounting_hole":
            continue
        g = _zone_poly(zone_world_ring(d, z, pose))
        if g is not None:
            out.append((int(z.part), g))
    return out


def _label_spot_boxes(
    refdes: str, ring: Any, anchor_mm: float
) -> tuple[tuple[float, float, float, float], ...]:
    """The eight canonical :data:`precis.pcb.silk.LABEL_SPOT_ORDER` label
    boxes for a placed ring, as ``(x0, y0, x1, y1)`` — silk's
    ``_outline_spot_candidates`` formulas on the ring's world bbox,
    spaced ``anchor_mm`` (:class:`SilkRoom.label_anchor_mm`) off it:
    where the DRAW puts the box (the label gap off the drawn outline
    plus the drawn outline's target offset the placer ring doesn't
    carry). The text box needs no glyph layout:
    :func:`precis.pcb.stroke_font.text_width_mm` is the exact advance."""
    x0, y0, x1, y1 = ring.bounds
    xc, yc = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    g = anchor_mm
    w = text_width_mm(refdes, _LBL_HEIGHT_MM)
    h = _LBL_HEIGHT_MM
    # (anchor, h_align, v_align) — silk LABEL_SPOT_ORDER, board frame.
    # Keep in exact lockstep with silk._outline_spot_candidates: the
    # greedy zips these boxes with silk.LABEL_SPOT_ORDER by position.
    spots = (
        ((xc, y0 - g), "center", "top"),  # bottom
        ((x0, y0 - g), "left", "top"),  # bottom-left
        ((x1, y0 - g), "right", "top"),  # bottom-right
        ((x0, y1 + g), "left", "baseline"),  # top-left
        ((x1, y1 + g), "right", "baseline"),  # top-right
        ((x0 - g, yc), "right", "middle"),  # left
        ((x1 + g, yc), "left", "middle"),  # right
        ((xc, y1 + g), "center", "baseline"),  # top
    )
    out = []
    for (ax, ay), ha, va in spots:
        dx = {"left": 0.0, "center": -w / 2.0, "right": -w}[ha]
        dy = {"baseline": 0.0, "middle": -h / 2.0, "top": -h}[va]
        out.append((ax + dx, ay + dy, ax + dx + w, ay + dy + h))
    return tuple(out)


def _label_box_free(
    part: int,
    box: tuple[float, float, float, float],
    ring_at: Any,
    side_at: Any,
    n: int,
    th: np.ndarray,
    eroded: Any | None,
    zones: list[tuple[int, Any]],
    bounds: np.ndarray,
    clear_mm: float,
) -> bool:
    """The placement-time acceptance test for one label box — stricter
    than silk's draw-side test, so placer-free ⇒ silk-free. The box must
    sit fully inside the outline eroded by
    :class:`SilkRoom.edge_erode_mm`, keep ``clear_mm``
    (:class:`SilkRoom.label_clear_mm` — the draw's label gap off the
    DRAWN outline, plus the drawn outline's target offset, PLUS the
    draw's settling inflation: decimation, shared-wall bridging, via
    bulges; runs 42-43 priced the formula and every pin sat exactly on
    silk's minimum) off every colliding ring (line or area — one polygon
    distance covers both), and not intersect a hole keep-out. ``bounds``
    is every ring's ``(x0, y0, x1, y1)`` as an ``(n, 4)`` array — the
    numpy broad phase over what the ring loop would prescreen one
    shapely call at a time."""
    bx0, by0, bx1, by1 = box
    if eroded is not None:
        ex0, ey0, ex1, ey1 = eroded.bounds
        if bx0 < ex0 or by0 < ey0 or bx1 > ex1 or by1 > ey1:
            return False
        poly = Polygon(((bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)))
        if not eroded.covers(poly):
            return False
    else:
        poly = Polygon(((bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)))
    g = clear_mm
    side_p = side_at(part)
    th_p = bool(th[part])
    near = (
        (bounds[:, 0] <= bx1 + g)
        & (bounds[:, 2] + g >= bx0)
        & (bounds[:, 1] <= by1 + g)
        & (bounds[:, 3] + g >= by0)
    )
    for idx in np.nonzero(near)[0]:
        j = int(idx)
        if j == part:
            continue
        gj = ring_at(j)
        if gj is None or (side_p != side_at(j) and not (th_p or th[j])):
            continue
        if poly.distance(gj) < g:
            return False
    for owner, zg in zones:
        if owner != part and poly.intersects(zg):
            return False
    return True


def _label_axis(
    part: int,
    refdes: str,
    ring: Any | None,
    ring_at: Any,
    side_at: Any,
    n: int,
    th: np.ndarray,
    eroded: Any | None,
    zones: list[tuple[int, Any]],
    bounds: np.ndarray,
    anchor_mm: float,
    clear_mm: float,
) -> float:
    """The margin term's label axis: 0 when one canonical spot's box is
    free, else :data:`_LBL_NO_SPOT_MM` (the silk pass walks the
    relocation ladder or drops the label). FLAT on purpose: a graded
    walk measured on the anneal's model actively hurt the search
    (taz-leaf run 39b: 69 squished pairs vs run 38's 2 — the walk
    gradient pushes parts toward their label side and tightens their
    pair gaps; and the placer's box model can never match the draw's
    settled-outline geometry exactly, so in-anneal grading optimises a
    ghost). The fix lives in :func:`settle_label_room` — the
    deterministic post-anneal repair that nudges offenders and checks
    against the model with real acceptance. Ringless parts take 0."""
    if ring is None:
        return 0.0
    for box in _label_spot_boxes(refdes, ring, anchor_mm):
        if _label_box_free(
            part, box, ring_at, side_at, n, th, eroded, zones, bounds, clear_mm
        ):
            return 0.0
    return _LBL_NO_SPOT_MM


def _ladder_candidates(
    refdes: str,
    ring: Any,
    origin: tuple[float, float],
    anchor_mm: float,
    *,
    mirror: bool,
) -> tuple[list[tuple[float, float]], ...]:
    """The silk pass's relocation-ladder candidates for a placed ring —
    the quarter-edge anchors, then the radial ring sweep, in silk's
    exact try order (:data:`precis.pcb.silk._CANDIDATES`), with boxes
    from the exact advance width (no glyph layout — the axis grades,
    the draw re-derives). ``box_board`` is the world ring translated
    back to the part origin, the frame the support math works in."""
    from precis.pcb.silk import _CANDIDATES, _LADDER_STEP_CAP_MM

    ox, oy = origin
    pts = [(float(x) - ox, float(y) - oy) for x, y in ring.exterior.coords]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    # silk spaces the ladder off the DRAWN outline; this ring is the
    # reservation (drawn minus the target/clearance delta), so the
    # anchor carries the delta — without it the graded walk sits
    # 0.275 mm closer than the draw's ladder.
    g = max(_LBL_HEIGHT_MM * 0.3, anchor_mm)
    w = text_width_mm(refdes, _LBL_HEIGHT_MM)
    h = _LBL_HEIGHT_MM
    raw: list[tuple[tuple[float, float], str, str]] = []
    for f in (0.25, 0.75):
        mid_x = x0 + f * (x1 - x0)
        mid_y = y0 + f * (y1 - y0)
        raw += [
            ((mid_x, y0 - g), "center", "top"),
            ((x0 - g, mid_y), "right", "middle"),
            ((x1 + g, mid_y), "left", "middle"),
            ((mid_x, y1 + g), "center", "baseline"),
        ]
    for du, dv, h_align, v_align, _spot in _CANDIDATES:
        rn = math.hypot(du, dv)
        if rn > 2.0:
            break  # grading cap: rings 1-2 price the walk; deeper rungs
            # only push the excess toward the NO_SPOT ceiling the axis
            # returns anyway when nothing nearer is free (and each rung
            # costs a full acceptance test per stuck part per eval)
        ux, uy = du / rn, dv / rn
        reach = max(px * ux + py * uy for px, py in pts) + g
        off = reach + (rn - 1.0) * min(reach, _LADDER_STEP_CAP_MM)
        raw.append(((ux * off, uy * off), h_align, v_align))
    out: list[list[tuple[float, float]]] = []
    for (ax, ay), ha, va in raw:
        if mirror:
            ha = {"left": "right", "right": "left"}.get(ha, ha)
        dx = {"left": 0.0, "center": -w / 2.0, "right": -w}[ha]
        dy = {"baseline": 0.0, "middle": -h / 2.0, "top": -h}[va]
        out.append(
            [
                (ax + dx, ay + dy),
                (ax + dx + w, ay + dy),
                (ax + dx + w, ay + dy + h),
                (ax + dx, ay + dy + h),
            ]
        )
    return tuple(out)


def label_boxes_for(
    d: TopoDesign, pose: np.ndarray, spots: dict[str, str]
) -> tuple[tuple[str, np.ndarray], ...]:
    """The pinned label boxes as ``(refdes, closed world ring)`` rows —
    the route stage threads them in as ``keepout`` zones so the router
    never sites a via (or threads a wire) where the placer promised the
    refdes label would sit. Without this the silk pass runs AFTER
    routing and a via on the pin walks the label onto the relocation
    ladder (taz-leaf run 35: R23's pinned right spot carried a via, U2's
    label walked 13 mm). Each ring is padded by
    :data:`_LBL_KEEPOUT_PAD_MM`: a via near (not on) a box still bulges
    the settled courtyard outline into it (run 36: neighbours' bulges
    intersected pinned boxes at d=0), so the reservation must claim the
    bulge's reach, not just the ink's box."""
    if not spots:
        return ()
    pose = np.asarray(pose, dtype=np.float64)
    out: list[tuple[str, np.ndarray]] = []
    for i, refdes in enumerate(d.parts):
        name = spots.get(refdes)
        if name is None:
            continue
        ring = _ring_geom(d, pose[i], i)
        if ring is None:
            continue
        box = dict(
            zip(
                LABEL_SPOT_ORDER,
                _label_spot_boxes(refdes, ring, d.silk.label_anchor_mm),
            )
        ).get(name)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        p = _LBL_KEEPOUT_PAD_MM
        out.append(
            (
                refdes,
                np.asarray(
                    [
                        [x0 - p, y0 - p],
                        [x1 + p, y0 - p],
                        [x1 + p, y1 + p],
                        [x0 - p, y1 + p],
                        [x0 - p, y0 - p],
                    ],
                    dtype=np.float64,
                ),
            )
        )
    return tuple(out)


def label_room_walks(d: TopoDesign, pose: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-part ``(walk_mm, edge_shortfall_mm)`` under the label model —
    the repair pass's scoreboard. ``walk`` is 0 when a canonical spot's
    box is free, else the first free relocation-ladder rung's excess
    over the canonical gap (capped at :data:`_LBL_NO_SPOT_MM`); the
    graded ladder lives HERE, in the deterministic repair, not in the
    anneal term (:func:`_label_axis` is flat on purpose — run 39b).
    ``edge_shortfall`` is the margin edge axis, the "squished by the
    outline" half of the complaint."""
    n = len(d.parts)
    walk = np.zeros(n, dtype=np.float64)
    edge = np.zeros(n, dtype=np.float64)
    if not n:
        return walk, edge
    pose = np.asarray(pose, dtype=np.float64)
    rings = [_ring_geom(d, pose[i], i) for i in range(n)]
    sides = pose[:, 3] >= 0.5
    th = through_hole_parts(d)
    outline = _outline_geom(d)
    eroded = _eroded_outline(outline, d.silk.edge_erode_mm)
    zones = _margin_zone_entries(d, pose)
    ring_at = rings.__getitem__
    side_at = lambda j: bool(sides[j])  # noqa: E731
    bounds = _rings_bounds(rings)
    em = (
        np.asarray(d.part_edge_mate, dtype=bool)
        if len(d.part_edge_mate) == n
        else np.zeros(n, dtype=bool)
    )
    anchor = d.silk.label_anchor_mm
    clear = d.silk.label_clear_mm
    for i in range(n):
        ring = rings[i]
        if ring is None:
            continue
        if outline is not None and not em[i]:
            edge[i] = max(
                0.0, d.silk.edge_req_mm - float(outline.exterior.distance(ring))
            )
        free = any(
            _label_box_free(i, b, ring_at, side_at, n, th, eroded, zones, bounds, clear)
            for b in _label_spot_boxes(d.parts[i], ring, anchor)
        )
        if free:
            continue
        ox, oy = float(pose[i][0]), float(pose[i][1])
        excess = _LBL_NO_SPOT_MM
        for corners in _ladder_candidates(
            d.parts[i], ring, (ox, oy), anchor, mirror=side_at(i)
        ):
            box = (
                min(p[0] for p in corners) + ox,
                min(p[1] for p in corners) + oy,
                max(p[0] for p in corners) + ox,
                max(p[1] for p in corners) + oy,
            )
            if not _label_box_free(
                i, box, ring_at, side_at, n, th, eroded, zones, bounds, clear
            ):
                continue
            poly = Polygon(
                ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))
            )
            excess = min(_LBL_NO_SPOT_MM, max(0.0, float(poly.distance(ring)) - anchor))
            break
        walk[i] = excess
    return walk, edge


def _label_room_legal(
    d: TopoDesign,
    pose: np.ndarray,
    part: int,
    row: np.ndarray,
    rings: list[Any],
    sides: np.ndarray,
    th: np.ndarray,
    outline: Any | None,
    zones: list[tuple[int, Any]],
) -> bool:
    """One candidate nudge's legality: the moved part's PADS stay inside
    the outline (the DRC's ``outline_containment`` measures pad RECTS —
    a pin whose centre is inside but whose pad overhangs still fails;
    run 42's repair committed 4 such errors before this check), its
    ring gains NO same-side overlap, its body stays out of every
    hole keep-out, and its courtyard never moves further OUT along the
    edge line the anneal enforces — the overhang past the outline
    (``outline_containment`` on the part) and the
    :data:`_EDGE_CLEAR_MM` shortfall (:func:`_ring_edge_violation`,
    the same measure the overlap term prices; the DRC's
    ``board_edge_clearance``/``courtyard_edge_clearance`` tier) may not
    grow. gr538618, esp32c3-s2's C7: a label-freeing nudge pushed the
    ring to 0.29 mm off the top edge — the acceptance tuple let the
    walk term dominate the edge axis, so the legality gate now owns the
    line instead. A complying part stays inside; an edge offender's
    only legal direction is inboard, which is the repair the edge axis
    asks for."""
    if outline is not None:
        pins = np.nonzero(d.pin_part == part)[0]
        centers = pin_world(d, row, part)
        if len(d.pin_wh):
            for k, p in enumerate(pins):
                w, h = float(d.pin_wh[p][0]), float(d.pin_wh[p][1])
                if w <= 0.0 and h <= 0.0:
                    if (
                        outline.distance(
                            Point(float(centers[k][0]), float(centers[k][1]))
                        )
                        > 0.0
                    ):
                        return False
                    continue
                base = np.asarray(d.pin_xy[p], dtype=np.float64)
                corners = local_to_world(
                    np.asarray(
                        [
                            base + [-w / 2.0, -h / 2.0],
                            base + [w / 2.0, -h / 2.0],
                            base + [w / 2.0, h / 2.0],
                            base + [-w / 2.0, h / 2.0],
                        ]
                    ),
                    row,
                )
                if not outline.covers(Polygon(corners)):
                    return False
        elif len(centers) and _containment_part(outline, centers) > 0.0:
            return False
    gi = _ring_geom(d, row, part)
    if gi is None:
        return False
    if outline is not None:
        em = d.part_edge_mate
        cur = rings[part]
        if (
            not (len(em) == len(d.parts) and bool(em[part]))
            and cur is not None
            and (
                float(gi.difference(outline).area)
                > float(cur.difference(outline).area) + _EPS
                or _ring_edge_violation(outline, gi)
                > _ring_edge_violation(outline, cur) + _EPS
            )
        ):
            return False
    for j, gj in enumerate(rings):
        if j == part or gj is None or not _collides(sides, th, part, j):
            continue
        bx0, by0, bx1, by1 = gj.bounds
        x0, y0, x1, y1 = gi.bounds
        if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
            continue
        if gi.intersection(gj).area > _EPS:
            return False
    body = _body_geom(d, row, part)
    if body is not None:
        for owner, zg in zones:
            if owner != part and body.intersects(zg):
                return False
    return True


def _label_room_blockers(
    d: TopoDesign,
    part: int,
    rings: list[Any],
    gap_mm: float,
) -> list[int]:
    """Parts whose rings fall inside ``part``'s canonical label boxes
    dilated by the label gap — the neighbours pinning every spot. When
    the offender itself has no legal improving nudge, the repair moves
    THESE instead (nearest first): on a crowded board whoever moves
    easiest, moves."""
    ring = rings[part]
    if ring is None:
        return []
    out: set[int] = set()
    for box in _label_spot_boxes(d.parts[part], ring, gap_mm):
        poly = Polygon(
            ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))
        ).buffer(gap_mm)
        for j, gj in enumerate(rings):
            if j == part or gj is None or j in out:
                continue
            if poly.intersects(gj):
                out.add(j)
    return sorted(out, key=lambda j: rings[j].distance(ring))


def _label_room_tuple(
    d: TopoDesign,
    part: int,
    rings: list[Any],
    sides: np.ndarray,
    th: np.ndarray,
    outline: Any | None,
    zones: list[tuple[int, Any]],
) -> tuple[float, float]:
    """``part``'s ``(walk, edge)`` tuple under the model with a given
    ring list — the acceptance score for both the offender's and the
    blockers' candidate moves."""
    ring = rings[part]
    if ring is None:
        return 0.0, 0.0
    ring_at = rings.__getitem__
    side_at = lambda j: bool(sides[j])  # noqa: E731
    n = len(d.parts)
    bounds = _rings_bounds(rings)
    eroded = _eroded_outline(outline, d.silk.edge_erode_mm)
    anchor = d.silk.label_anchor_mm
    clear = d.silk.label_clear_mm
    walk = 0.0
    free = any(
        _label_box_free(part, b, ring_at, side_at, n, th, eroded, zones, bounds, clear)
        for b in _label_spot_boxes(d.parts[part], ring, anchor)
    )
    if not free:
        walk = _LBL_NO_SPOT_MM
        for corners in _ladder_candidates(
            d.parts[part], ring, (0.0, 0.0), anchor, mirror=side_at(part)
        ):
            box = (
                min(p[0] for p in corners),
                min(p[1] for p in corners),
                max(p[0] for p in corners),
                max(p[1] for p in corners),
            )
            if not _label_box_free(
                part, box, ring_at, side_at, n, th, eroded, zones, bounds, clear
            ):
                continue
            poly = Polygon(
                ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))
            )
            walk = min(_LBL_NO_SPOT_MM, max(0.0, float(poly.distance(ring)) - anchor))
            break
    edge = 0.0
    if outline is not None:
        em = d.part_edge_mate
        if not (len(em) == n and bool(em[part])):
            edge = max(0.0, d.silk.edge_req_mm - float(outline.exterior.distance(ring)))
    return walk, edge


def _label_room_nudges(radius: float) -> tuple[tuple[float, float], ...]:
    return tuple(
        (radius * math.cos(k * math.pi / 4.0), radius * math.sin(k * math.pi / 4.0))
        for k in range(8)
    )


def _try_label_nudges(
    d: TopoDesign,
    pose: np.ndarray,
    who: int,
    score: int,
    rings: list[Any],
    sides: np.ndarray,
    th: np.ndarray,
    outline: Any | None,
    zones: list[tuple[int, Any]],
    nudges: tuple[tuple[tuple[float, float], ...], ...],
) -> bool:
    """Nudge ``who`` (legality-checked); accept the first pose that
    improves ``score``'s ``(walk, edge)`` tuple. The mover must not
    strictly worsen unless it IS the scorer (there the improving tuple
    is the acceptance test). Mutates ``pose``; True when a nudge
    landed. Fixed parts never move — the authored locks are a contract
    (taz-leaf's comb electrodes are ``fixed='both'``; run 42's repair
    dragged one off the outline and the gate refused the board)."""
    if bool(d.fixed[who]):
        return False
    who_cur = _label_room_tuple(d, who, rings, sides, th, outline, zones)
    score_cur = _label_room_tuple(d, score, rings, sides, th, outline, zones)
    for ring_nudges in nudges:
        for dx, dy in ring_nudges:
            row = pose[who].copy()
            row[0] += dx
            row[1] += dy
            if not _label_room_legal(
                d, pose, who, row, rings, sides, th, outline, zones
            ):
                continue
            trial = list(rings)
            trial[who] = _ring_geom(d, row, who)
            if (
                _label_room_tuple(d, score, trial, sides, th, outline, zones)
                >= score_cur
            ):
                continue
            if (
                who != score
                and _label_room_tuple(d, who, trial, sides, th, outline, zones)
                > who_cur
            ):
                continue
            pose[who] = row
            return True
    return False


def settle_label_room(
    d: TopoDesign, pose: np.ndarray, *, max_rounds: int = 4
) -> np.ndarray:
    """Deterministic post-anneal repair: nudge parts whose refdes label
    would walk (canonical spots all blocked) or whose ring sits in the
    edge band, until the model is clean or the round budget ends. This
    is the "labels sit next to their parts and nothing hugs the outline"
    guarantee the anneal cannot give — its box model can never match the
    draw's settled-outline geometry exactly, so the pressure belongs in
    a pass that scores exactly the model the pins and keep-outs use.

    Worst offender first; nudges on a 0.5 mm lattice out to 3 mm in the
    8 board directions (the same local-fixup class as the kernel's own
    ``_settle_pose``, which likewise post-adjusts the best pose without
    re-entering the anneal). A nudge must be legal — pins inside, no new
    ring overlap, no hole intersection — and improve the offender's own
    ``(walk, edge)`` tuple. Rounds repeat until nothing moves or the
    budget ends; the survivors (genuinely no room on a full board) are
    left for the measure and the web view to show honestly.
    """
    n = len(d.parts)
    pose = np.asarray(pose, dtype=np.float64).copy()
    if not n:
        return pose
    th = through_hole_parts(d)
    outline = _outline_geom(d)
    sides = pose[:, 3] >= 0.5
    nudges = tuple(_label_room_nudges(r) for r in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0))
    for _round in range(max_rounds):
        walk, edge = label_room_walks(d, pose)
        offenders = sorted(
            (
                i
                for i in range(n)
                if not bool(d.fixed[i]) and (walk[i] > 0.25 or edge[i] > 0.05)
            ),
            key=lambda i: -(walk[i] + edge[i]),
        )
        if not offenders:
            break
        moved = False
        for i in offenders:
            rings = [_ring_geom(d, pose[k], k) for k in range(n)]
            zones = _margin_zone_entries(d, pose)
            if _try_label_nudges(
                d, pose, i, i, rings, sides, th, outline, zones, nudges
            ):
                moved = True
                continue
            # The offender is boxed in — move whichever neighbour pins
            # its spots (nearest first, movable only), then re-try the
            # offender.
            for j in _label_room_blockers(d, i, rings, d.silk.label_clear_mm):
                if bool(d.fixed[j]):
                    continue
                rings = [_ring_geom(d, pose[k], k) for k in range(n)]
                zones = _margin_zone_entries(d, pose)
                if _try_label_nudges(
                    d, pose, j, i, rings, sides, th, outline, zones, nudges
                ):
                    moved = True
                    rings = [_ring_geom(d, pose[k], k) for k in range(n)]
                    if _try_label_nudges(
                        d, pose, i, i, rings, sides, th, outline, zones, nudges
                    ):
                        break
        if not moved:
            break
    return pose


def _eroded_outline(outline: Any | None, erode_mm: float) -> Any | None:
    """The outline eroded by ``erode_mm`` (:class:`SilkRoom.edge_erode_mm`
    — the edge-margin ink band PLUS the label glyph overhang, since silk
    measures INK, not the advance box), PREPARED for the label axis's
    repeated ``covers`` tests (shapely 2 caches the prep on the
    geometry). ``None`` in/out."""
    if outline is None:
        return None
    g = outline.buffer(-erode_mm)
    shapely.prepare(g)
    return g


def _rings_bounds(rings: list[Any]) -> np.ndarray:
    """Every ring's bbox as an ``(n, 4)`` array (NaN row for ``None``) —
    the label axis's numpy broad phase."""
    out = np.full((len(rings), 4), np.nan)
    for i, g in enumerate(rings):
        if g is not None:
            out[i] = g.bounds
    return out


def label_spots_for(d: TopoDesign, pose: np.ndarray) -> dict[str, str]:
    """The refdes-label spot pins for a settled pose — each part's first
    free canonical spot, chosen greedily in silk's natural refdes order
    (the SAME greedy the silk pass runs, so the pin is what silk would
    pick given the space the placer preserved). Each chosen box also
    blocks later parts, like a committed label does draw-side. Parts with
    no free canonical spot get no pin — the silk pass decides. The job
    persists these as instance ``meta.label_spot``; the silk builder
    honors a pin as the part's first candidate, falling back past it
    only when routed vias or committed silk block it (gr496552)."""
    from precis.pcb.silk import _refdes_sort_key

    n = len(d.parts)
    if not n:
        return {}
    pose = np.asarray(pose, dtype=np.float64)
    rings = [_ring_geom(d, pose[i], i) for i in range(n)]
    sides = pose[:, 3] >= 0.5
    th = through_hole_parts(d)
    outline = _outline_geom(d)
    eroded = _eroded_outline(outline, d.silk.edge_erode_mm)
    zones = _margin_zone_entries(d, pose)
    ring_at = rings.__getitem__
    side_at = lambda j: bool(sides[j])  # noqa: E731
    bounds = _rings_bounds(rings)
    chosen: list[tuple[float, float, float, float]] = []
    out: dict[str, str] = {}
    for i in sorted(range(n), key=lambda k: _refdes_sort_key(d.parts[k])):
        ring = rings[i]
        if ring is None:
            continue
        for name, box in zip(
            LABEL_SPOT_ORDER,
            _label_spot_boxes(d.parts[i], ring, d.silk.label_anchor_mm),
        ):
            if not _label_box_free(
                i,
                box,
                ring_at,
                side_at,
                n,
                th,
                eroded,
                zones,
                bounds,
                d.silk.label_clear_mm,
            ):
                continue
            cx0, cy0, cx1, cy1 = box
            if any(
                cx0 + _EPS < px1
                and px0 + _EPS < cx1
                and cy0 + _EPS < py1
                and py0 + _EPS < cy1
                for px0, py0, px1, py1 in chosen
            ):
                continue
            out[d.parts[i]] = name
            chosen.append(box)
            break
    return out


def _margin_axes(
    rings: list[Any],
    bodies: list[Any],
    pose: np.ndarray,
    th: np.ndarray,
    outline: Any | None,
    eroded: Any | None,
    zones: list[tuple[int, Any]],
    edge_mate: np.ndarray,
    refdes: tuple[str, ...],
    silk: SilkRoom,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Per-part (edge, pair, hole, label) margin shortfalls in mm over
    placed ring/body lists — the full-recompute body ``margin_total``
    and ``ProxyState.__init__`` share. The pair loop is
    :func:`_ring_silkroom_total`'s scan (same rings, same side rule, same
    ``req`` of bbox slack) with the shortfall MAXED per part instead of
    summed; the edge axis measures the COURTYARD ring against the outline
    against the flat ``silk.edge_req_mm`` (the silk ring's room, not
    the 0.4mm fab bar the overlap term prices on the body); the hole axis
    measures the pad-union BODY against the hole keep-outs, the owner
    exempt like the DRC's ``courtyard_hole``; the label axis is
    :func:`_label_axis` over the canonical spot boxes. Edge-mated parts
    pay no edge axis (gr530632 — the mating face is SUPPOSED to sit on
    the outline)."""
    n = len(rings)
    e = np.zeros(n, dtype=np.float64)
    p = np.zeros(n, dtype=np.float64)
    h = np.zeros(n, dtype=np.float64)
    l = np.zeros(n, dtype=np.float64)
    if not n:
        return e, p, h, l
    sides = np.asarray(pose)[:, 3] >= 0.5
    em = (
        np.asarray(edge_mate, dtype=bool)
        if len(edge_mate) == n
        else np.zeros(n, dtype=bool)
    )
    ring_at = rings.__getitem__
    side_at = lambda j: bool(sides[j])  # noqa: E731
    bounds = _rings_bounds(rings)
    req = silk.pair_req_mm
    for i in range(n):
        gi = rings[i]
        if gi is not None:
            if outline is not None and not em[i]:
                e[i] = max(0.0, silk.edge_req_mm - float(outline.exterior.distance(gi)))
            x0, y0, x1, y1 = gi.bounds
            for j in range(n):
                if j == i:
                    continue
                gj = rings[j]
                if gj is None or not _collides(sides, th, i, j):
                    continue
                bx0, by0, bx1, by1 = gj.bounds
                if x1 + req < bx0 or bx1 + req < x0:
                    continue
                if y1 + req < by0 or by1 + req < y0:
                    continue
                p[i] = max(p[i], req - float(gi.distance(gj)))
            p[i] = max(0.0, p[i])
            l[i] = _label_axis(
                i,
                refdes[i],
                gi,
                ring_at,
                side_at,
                n,
                th,
                eroded,
                zones,
                bounds,
                silk.label_anchor_mm,
                silk.label_clear_mm,
            )
        bi = bodies[i]
        if bi is not None:
            best = math.inf
            for owner, zg in zones:
                if owner == i:
                    continue  # a part's own hole is covered by design
                best = min(best, float(bi.distance(zg)))
            if math.isfinite(best):
                h[i] = max(0.0, HOLE_WANT_MM - best)
    return e, p, h, l


def _margin_softmin(exp_sum: float, n: int) -> float:
    """``−τ·log(mean(exp(−worst/τ)))`` from the exp-sum — ONE code path
    for the full recompute and the ProxyState, so parity is structural.
    Every margin satisfied ⇒ ``exp_sum == n`` ⇒ 0."""
    if n <= 0 or exp_sum <= 0.0:
        return 0.0
    return -_MARGIN_SOFTMIN_TAU_MM * math.log(exp_sum / n)


def margin_total(d: TopoDesign, pose: np.ndarray) -> float:
    """The maximin margin term (Reto 2026-10-29), full recompute — the
    reference the ``ProxyState`` parity-check runs against. Per part, the
    worst of the edge / pair / hole / label axis shortfalls
    (:func:`_margin_axes`); the term is the softmin over parts of those
    worsts, ``−τ·log(mean(exp(−worst/τ)))`` with ``τ =
    :data:`_MARGIN_SOFTMIN_TAU_MM` — all margins satisfied ⇒ 0."""
    n = len(d.parts)
    if not n:
        return 0.0
    pose = np.asarray(pose, dtype=np.float64)
    rings = [_ring_geom(d, pose[i], i) for i in range(n)]
    bodies = [_body_geom(d, pose[i], i) for i in range(n)]
    outline = _outline_geom(d)
    eroded = _eroded_outline(outline, d.silk.edge_erode_mm)
    e, p, h, l = _margin_axes(
        rings,
        bodies,
        pose,
        through_hole_parts(d),
        outline,
        eroded,
        _margin_zone_entries(d, pose),
        d.part_edge_mate,
        d.parts,
        d.silk,
    )
    worst = np.maximum(np.maximum(e, p), np.maximum(h, l))
    exp_sum = float(np.exp(-worst / _MARGIN_SOFTMIN_TAU_MM).sum())
    return _margin_softmin(exp_sum, n)


def overlap_area(d: TopoDesign, pose: np.ndarray) -> float:
    """Total SAME-MOUNT-SIDE courtyard overlap in mm² PLUS pad
    containment violation in mm — the count the mesh's ``conflicts``
    reports and placement DRC flags (parts on opposite faces overlap
    legally; ``_ring_geom`` mirroring makes a bottom part's ring its
    physical one). Containment: Σ over all pins of the distance to the
    outline polygon — 0 inside, mm-beyond-the-edge outside (the DRC's
    ``outline_containment`` measure). The mover's centre clamp only
    sees the outline's BBOX, so on a non-rectangular outline (taz
    leaf's neck and tip) only this term keeps pads on the board. Full
    recompute — the ``ProxyState`` overlap reference magnitude."""
    return (
        _ring_overlap_total(
            [_ring_geom(d, pose[i], i) for i in range(len(d.parts))],
            pose,
            through_hole_parts(d),
        )
        + pad_containment_total(d, all_pin_world(d, pose))
        + edge_clearance_total(d, pose)
    )


def _outline_geom(d: TopoDesign) -> Any | None:
    """The outline as a shapely polygon — None when the design carries
    no outline (synthetic designs), where containment prices nothing."""
    if len(d.outline) < 3:
        return None
    g = Polygon(np.asarray(d.outline, dtype=np.float64))
    return g if not g.is_empty else None


#: Body-to-board-edge clearance floor — the DRC's
#: ``courtyard_edge_clearance`` jlc_min bar (JLC vcut minimum), measured
#: against the pad-union BODY since gr529963 (ruling Reto 2026-10-28:
#: the fab bar prices the body; the 0.4mm house courtyard margin applies
#: only between parts). The placer design build sees no fab capability
#: row (the same constraint as net rule widths), so the term holds the
#: flat minimum; the house-default tier and edge-mate exemptions belong
#: to the rung that carries capability.
_EDGE_CLEAR_MM = 0.4


def _ring_edge_violation(g: Any, ring: Any) -> float:
    """``max(0, _EDGE_CLEAR_MM − gap)`` between a part polygon and
    the outline's EXTERIOR ring — the DRC's
    ``courtyard_edge_clearance`` distance (taz-leaf-b's C12: pads
    inside, courtyard 0.13 mm off the V-cut). The polygon passed in is
    the pad-union BODY since gr529963 (the fab bar measures the body);
    the measure itself is unchanged. ``Polygon.distance``
    cannot be used here: it is 0 whenever one polygon contains the
    other, so the gap must be measured against the boundary, exactly
    what :func:`precis.pcb.drc.check_courtyard_edge_clearance` does."""
    if ring is None:
        return 0.0
    return max(0.0, _EDGE_CLEAR_MM - float(g.exterior.distance(ring)))


def edge_clearance_total(d: TopoDesign, pose: np.ndarray) -> float:
    """Σ body-to-edge shortfall over all parts — full recompute,
    the edge half of the overlap term's outline measures, priced against
    the pad-union BODY since gr529963 (ruling Reto 2026-10-28: the fab
    bar measures the body; pricing the courtyard squeezed placement to
    an effective 0.8mm body-to-edge demand and disagreed with the DRC
    rule's own measure). Edge-mated parts are skipped: their mating
    face is SUPPOSED to sit on the outline, and pricing them would push
    them inboard where ``check_edge_mating`` refuses the board
    (gr530632)."""
    g = _outline_geom(d)
    if g is None:
        return 0.0
    em = d.part_edge_mate
    return float(
        sum(
            _ring_edge_violation(g, _body_geom(d, pose[i], i))
            for i in range(len(d.parts))
            if not (len(em) == len(d.parts) and bool(em[i]))
        )
    )


def _containment_part(g: Any, pts: np.ndarray) -> float:
    """Σ ``geom.distance(point)`` over a pin block — 0 inside the
    outline, mm beyond the nearest edge outside."""
    return float(sum(g.distance(Point(float(x), float(y))) for x, y in np.asarray(pts)))


def pad_containment_total(d: TopoDesign, pw: np.ndarray) -> float:
    """The pad-containment violation over all pins at world positions
    ``pw`` — full recompute, the containment half of the overlap term."""
    g = _outline_geom(d)
    if g is None or not len(pw):
        return 0.0
    return _containment_part(g, pw)


def all_pin_world(d: TopoDesign, pose: np.ndarray) -> np.ndarray:
    """``pin_world`` for every part at once — (P, 2)."""
    out = np.empty_like(d.pin_xy)
    for part in range(len(d.parts)):
        out[d.pin_part == part] = pin_world(d, pose[part], part)
    return out


def _part_shared(d: TopoDesign) -> np.ndarray:
    """(N, N) bool — True when two parts share at least one routed net
    (``net_pins``; plane nets are already dropped). The spread term
    exempts connected pairs so net clusters stay clustered."""
    n = len(d.parts)
    shared = np.zeros((n, n), dtype=bool)
    for pins in d.net_pins:
        members = np.unique(d.pin_part[pins])
        shared[np.ix_(members, members)] = True
    return shared


def _spread_scale(d: TopoDesign, pose: np.ndarray) -> float:
    """The repulsion kernel's range: the mean part cell size
    sqrt(board_area / N) — the outline's area when it carries one, the
    pose bounding box otherwise (synthetic designs)."""
    n = max(len(d.parts), 1)
    if len(d.outline) >= 3:
        area = float(Polygon(np.asarray(d.outline, dtype=np.float64)).area)
    elif len(pose):
        span = pose[:, :2].max(axis=0) - pose[:, :2].min(axis=0)
        area = float(span[0] * span[1])
    else:
        area = 0.0
    return float(np.sqrt(area / n)) if area > _EPS else 1.0


def _spread_pairs(
    cent: np.ndarray,
    sides: np.ndarray,
    th: np.ndarray,
    noshare: np.ndarray,
    scale: float,
) -> float:
    """Σ over unordered ``noshare & collidable`` pairs of
    exp(−d²/2s²) on part origins — the full-recompute body shared by
    :func:`spread_total` and ``ProxyState`` init."""
    d2 = ((cent[:, None, :] - cent[None, :, :]) ** 2).sum(axis=2)
    kern = np.exp(-d2 / (2.0 * scale * scale))
    collide = (sides[:, None] == sides[None, :]) | (th[:, None] | th[None, :])
    return float(np.triu(kern * (noshare & collide), 1).sum())


def spread_total(d: TopoDesign, pose: np.ndarray) -> float:
    """Board-fill repulsion, full recompute — the reference the
    ``ProxyState`` spread parity-check runs against."""
    if not len(d.parts):
        return 0.0
    noshare = ~_part_shared(d)
    np.fill_diagonal(noshare, False)
    return _spread_pairs(
        np.asarray(pose, dtype=np.float64)[:, :2],
        np.asarray(pose)[:, 3] >= 0.5,
        through_hole_parts(d),
        noshare,
        _spread_scale(d, pose),
    )


def _net_via_est(d: TopoDesign, net_pins: np.ndarray, flipped: np.ndarray) -> int:
    """One net's via-count estimate: the distinct board sides spanned by
    its non-TH pins, minus 1. ``pin_side`` is the build-time PHYSICAL
    side (``_pin_sides`` bakes the instance side in); a flip since the
    seed toggles it. A TH pin (side 2) is a wildcard — the plated
    barrel exists on every layer — so it forces nothing, the
    ``_net_pin_force`` mirror. On the 2-layer taz board: 0/1 per net."""
    sides: set[int] = set()
    for p in net_pins:
        ps = int(d.pin_side[p])
        if ps == 2:
            continue
        phys = ps ^ (1 if flipped[int(d.pin_part[p])] else 0)
        sides.add(phys)
    return max(0, len(sides) - 1)


def via_estimate_total(d: TopoDesign, pose: np.ndarray) -> int:
    """The via-count estimate over all routable nets — full recompute,
    the reference the ``ProxyState`` parity-check runs against."""
    flipped = (np.asarray(pose)[:, 3] >= 0.5) != (np.asarray(d.pose0)[:, 3] >= 0.5)
    return sum(_net_via_est(d, pins, flipped) for pins in d.net_pins)


def _net_backtrack(wires: np.ndarray, pts: np.ndarray) -> float:
    """One net's fold-back mileage in mm: per axis, the wire mileage
    beyond the pin set's extent. Always ≥ 0 — any tree over the points
    spans min..max, so Σ|Δ| can only exceed the extent. Exactly the
    "ran north, then doubled back south" measure."""
    if not len(wires):
        return 0.0
    run_x = float(np.abs(wires[:, 2] - wires[:, 0]).sum())
    run_y = float(np.abs(wires[:, 3] - wires[:, 1]).sum())
    ext_x = float(pts[:, 0].max() - pts[:, 0].min())
    ext_y = float(pts[:, 1].max() - pts[:, 1].min())
    return max(0.0, run_x - ext_x) + max(0.0, run_y - ext_y)


def backtrack_total(d: TopoDesign, pose: np.ndarray) -> float:
    """Fold-back mileage over all routable nets — full recompute, the
    reference the ``ProxyState`` parity-check runs against."""
    pw = all_pin_world(d, pose)
    total = 0.0
    for k, pins in enumerate(d.net_pins):
        total += _net_backtrack(_net_wires(d, pins, pw, k), _net_points(d, pins, pw, k))
    return total


def _wire_uv_mid(w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Unit direction vectors and midpoints for an (E, 4) wire block."""
    v = w[:, 2:] - w[:, :2]
    L = np.hypot(v[:, 0], v[:, 1])
    L = np.where(L <= 0.0, 1.0, L)
    return v / L[:, None], (w[:, :2] + w[:, 2:]) / 2.0


def _dir_matrix(
    a: np.ndarray,
    an: np.ndarray,
    b: np.ndarray,
    bn: np.ndarray,
    ex: np.ndarray,
    scale: float,
) -> np.ndarray:
    """The (n_a, n_b) direction-pair weight matrix: (1−|cos θ|)·
    exp(−d²/2s²) between segment midpoints. Same-net pairs and
    rule-exempt nets score 0 — the ``_cross_matrix`` mask shape."""
    n_a, n_b = len(a), len(b)
    if n_a == 0 or n_b == 0:
        return np.zeros((n_a, n_b), dtype=np.float64)
    ua, ma = _wire_uv_mid(a)
    ub, mb = _wire_uv_mid(b)
    cos = np.clip(np.abs(ua @ ub.T), 0.0, 1.0)
    d2 = ((ma[:, None, :] - mb[None, :, :]) ** 2).sum(axis=2)
    wgt = (1.0 - cos) * np.exp(-d2 / (2.0 * scale * scale))
    mask = (an[:, None] != bn[None, :]) & ~ex[an][:, None] & ~ex[bn][None, :]
    wgt = wgt * mask
    # Zero-length wires (coincident same-net pins, an authored anchor
    # on a pin) carry no direction: their unit vector is (0,0), which
    # the cosine scores as perpendicular to EVERY neighbour — a phantom
    # direction-mix debt on a wire that cannot point anywhere. Mask to 0
    # (fable-review nit; both this reference path and the staged delta
    # share the helper, so the parity contract is unaffected).
    la = np.hypot(a[:, 2] - a[:, 0], a[:, 3] - a[:, 1]) > 0.0
    lb = np.hypot(b[:, 2] - b[:, 0], b[:, 3] - b[:, 1]) > 0.0
    return wgt * (la[:, None] & lb[None, :])


def _dir_exempt(d: TopoDesign) -> np.ndarray:
    """The per-net exemption flag, defaulting to nothing-exempt for
    hand-built synthetic designs that predate the field."""
    if len(d.net_direction_exempt) == len(d.net_pins):
        return np.asarray(d.net_direction_exempt, dtype=bool)
    return np.zeros(len(d.net_pins), dtype=bool)


def _dir_total(
    flat: np.ndarray, flat_net: np.ndarray, ex: np.ndarray, scale: float
) -> float:
    """Σ direction-pair weights over one flat wire block — strict upper
    triangle, so each cross-net pair counts once. Same-net entries are
    already 0 in the matrix, so the triangle is exactly the pair set."""
    if len(flat) < 2:
        return 0.0
    return float(
        np.triu(_dir_matrix(flat, flat_net, flat, flat_net, ex, scale), 1).sum()
    )


def direction_total(
    d: TopoDesign, pose: np.ndarray, scale: float | None = None
) -> float:
    """The direction-mix penalty over all routable nets — full
    recompute, the reference the ``ProxyState`` parity-check runs
    against. ``scale`` defaults to the outline-derived spread scale at
    ``pose``; a state built earlier pins its seed scale (the spread
    term's convention — pose-independent whenever the outline exists)."""
    pw = all_pin_world(d, pose)
    wires, nets = _concat(
        [_net_wires(d, np_net, pw, k) for k, np_net in enumerate(d.net_pins)],
        list(range(len(d.net_pins))),
    )
    if scale is None:
        scale = _spread_scale(d, pose)
    return _dir_total(wires, nets, _dir_exempt(d), scale)


def _mst_edges(pts: np.ndarray) -> list[tuple[int, int]]:
    """Prim from vertex 0 with a ``(dist, i, j)`` min key — the same
    deterministic order :func:`precis.pcb.ratsnest._mst_edges` produces
    (ties resolve to the lowest-numbered pair, so a rebuilt MST is
    bit-identical to a fresh one)."""
    n = len(pts)
    if n < 2:
        return []
    dmat = np.sqrt(((pts[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2))
    in_tree = np.zeros(n, dtype=bool)
    in_tree[0] = True
    edges: list[tuple[int, int]] = []
    for _ in range(n - 1):
        masked = np.where(in_tree[:, None] & ~in_tree[None, :], dmat, np.inf)
        i, j = np.unravel_index(int(np.argmin(masked)), masked.shape)
        edges.append((int(i), int(j)))
        in_tree[j] = True
    return edges


def _net_points(
    d: TopoDesign, net_pins: np.ndarray, pw: np.ndarray, k: int | None
) -> np.ndarray:
    """A net's MST vertices: real pin positions plus slot ``k``'s
    fixed-via anchors — world-fixed points the airwire must pass
    through (S5; ``net_anchors`` is parallel to ``net_pins``)."""
    pts = pw[net_pins]
    if k is not None and k < len(d.net_anchors) and len(d.net_anchors[k]):
        pts = np.vstack([pts, d.anchor_xy[d.net_anchors[k]]])
    return pts


def _net_wires(
    d: TopoDesign, net_pins: np.ndarray, pw: np.ndarray, k: int | None = None
) -> np.ndarray:
    """One net's MST airwires as (E, 4) ``[x1, y1, x2, y2]`` rows.
    ``k`` is the ``net_pins`` slot index — needed so the net's
    fixed-via anchors join the vertex set."""
    pts = _net_points(d, net_pins, pw, k)
    return np.asarray(
        [(pts[i, 0], pts[i, 1], pts[j, 0], pts[j, 1]) for i, j in _mst_edges(pts)],
        dtype=np.float64,
    ).reshape(-1, 4)


def _cross_matrix(
    a: np.ndarray, b: np.ndarray, net_a: np.ndarray, net_b: np.ndarray
) -> np.ndarray:
    """The (n_a, n_b) proper-straddle bool matrix — vectorised
    :func:`precis.pcb.geom.segments_cross`: AABB first, then
    both-orientation straddle; same-net pairs, shared endpoints,
    collinear/touch excluded."""
    n_a, n_b = len(a), len(b)
    if n_a == 0 or n_b == 0:
        return np.zeros((n_a, n_b), dtype=bool)
    aminx, amaxx = np.minimum(a[:, 0], a[:, 2]), np.maximum(a[:, 0], a[:, 2])
    aminy, amaxy = np.minimum(a[:, 1], a[:, 3]), np.maximum(a[:, 1], a[:, 3])
    bminx, bmaxx = np.minimum(b[:, 0], b[:, 2]), np.maximum(b[:, 0], b[:, 2])
    bminy, bmaxy = np.minimum(b[:, 1], b[:, 3]), np.maximum(b[:, 1], b[:, 3])
    overlap = (
        (aminx[:, None] <= bmaxx[None, :])
        & (bminx[None, :] <= amaxx[:, None])
        & (aminy[:, None] <= bmaxy[None, :])
        & (bminy[None, :] <= amaxy[:, None])
    )
    overlap &= net_a[:, None] != net_b[None, :]
    if not overlap.any():
        return np.zeros((n_a, n_b), dtype=bool)
    p1x, p1y = a[:, 0][:, None], a[:, 1][:, None]
    p2x, p2y = a[:, 2][:, None], a[:, 3][:, None]
    p3x, p3y = b[:, 0][None, :], b[:, 1][None, :]
    p4x, p4y = b[:, 2][None, :], b[:, 3][None, :]
    # shares_endpoint: any exact endpoint equality disqualifies the pair.
    shared = (
        ((p1x == p3x) & (p1y == p3y))
        | ((p1x == p4x) & (p1y == p4y))
        | ((p2x == p3x) & (p2y == p3y))
        | ((p2x == p4x) & (p2y == p4y))
    )
    d1 = (p4x - p3x) * (p1y - p3y) - (p4y - p3y) * (p1x - p3x)
    d2 = (p4x - p3x) * (p2y - p3y) - (p4y - p3y) * (p2x - p3x)
    d3 = (p2x - p1x) * (p3y - p1y) - (p2y - p1y) * (p3x - p1x)
    d4 = (p2x - p1x) * (p4y - p1y) - (p2y - p1y) * (p4x - p1x)
    straddle_a = ((d1 > _EPS) & (d2 < -_EPS)) | ((d1 < -_EPS) & (d2 > _EPS))
    straddle_b = ((d3 > _EPS) & (d4 < -_EPS)) | ((d3 < -_EPS) & (d4 > _EPS))
    return overlap & ~shared & straddle_a & straddle_b


def _cross_ab(
    a: np.ndarray, b: np.ndarray, net_a: np.ndarray, net_b: np.ndarray
) -> int:
    return int(_cross_matrix(a, b, net_a, net_b).sum())


def _within(a: np.ndarray, nets: np.ndarray) -> int:
    """Crossings inside one wire set — the strict upper triangle of the
    full matrix (i<j), so each pair counts once."""
    if len(a) < 2:
        return 0
    return int(np.triu(_cross_matrix(a, a, nets, nets), 1).sum())


def _concat(wires: list[np.ndarray], nets: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """Flatten per-net wire rows into one (W, 4) block + (W,) net ids."""
    if not wires:
        return np.empty((0, 4)), np.empty(0, dtype=np.int64)
    return np.concatenate(wires), np.asarray(
        [n for n, w in zip(nets, wires) for _ in range(len(w))],
        dtype=np.int64,
    )


#: Hard authored measures pull this much harder per violation-mm than a
#: soft one — same "hard dominates" convention
#: ``precis.pcb.optimize._measure_pair_usd`` encodes.
_MEASURE_HARD_W = 10.0


def _measure_violation(spec: Any, row_a: np.ndarray, row_b: np.ndarray | None) -> float:
    """One :class:`TopoMeasure`'s weighted violation in mm — 0 when the
    bound holds. Mirrors ``optimize._measure_pair_usd``'s shape (linear
    in the violation, never a legality rejection) minus its USD scale:
    the proxy normalises by the seed total instead."""
    xa, ya = float(row_a[0]), float(row_a[1])
    if spec.datum is not None:
        xb, yb = spec.datum
    elif row_b is not None:
        xb, yb = float(row_b[0]), float(row_b[1])
    else:
        return 0.0
    if not (
        math.isfinite(xa)
        and math.isfinite(ya)
        and math.isfinite(xb)
        and math.isfinite(yb)
    ):
        return 0.0
    if spec.metric == "align":
        from precis.pcb.eyes import align_residual

        v = max(
            0.0,
            align_residual((xa, ya), (xb, yb), spec.offset, spec.axis) - spec.goal_mm,
        )
    else:
        dist = math.hypot(xa - xb, ya - yb)
        if spec.bound == "lower":
            v = max(0.0, spec.goal_mm - dist)
        elif spec.bound == "upper":
            v = max(0.0, dist - spec.goal_mm)
        else:
            v = abs(dist - spec.goal_mm)
    if v <= 0.0:
        return 0.0
    return spec.weight * (_MEASURE_HARD_W if spec.hard else 1.0) * v


def evaluate(d: TopoDesign, pose: np.ndarray) -> tuple[int, float]:
    """Full recompute — the reference ``try_move`` parity-checks against.
    Returns (crossings, total airwire length mm)."""
    pw = all_pin_world(d, pose)
    wires, nets = _concat(
        [_net_wires(d, np_net, pw, k) for k, np_net in enumerate(d.net_pins)],
        list(range(len(d.net_pins))),
    )
    length = float(np.hypot(wires[:, 0] - wires[:, 2], wires[:, 1] - wires[:, 3]).sum())
    return _within(wires, nets), length


class ProxyState:
    """Incremental proxy cost over one pose (spec §1a signature).

    ``try_move`` stages a candidate pose row for one part and returns the
    Δcost; ``commit`` applies every staged row (two for a swap), ``revert``
    drops them. Staged ``try_move`` calls compose — the second sees the
    first's staged wires, so a two-part swap is one atomic decision."""

    def __init__(self, d: TopoDesign, pose: np.ndarray, w: ObjectiveShares) -> None:
        self.d = d
        self.w = w
        self.pose = np.asarray(pose, dtype=np.float64).copy()
        self._pw = all_pin_world(d, self.pose)
        #: committed wires per net: (E, 4) rows
        self._wires: list[np.ndarray] = [
            _net_wires(d, np_net, self._pw, k) for k, np_net in enumerate(d.net_pins)
        ]
        self._flat, self._flat_net = _concat(self._wires, list(range(len(d.net_pins))))
        self._cross = _within(self._flat, self._flat_net)
        self._len = (
            float(
                np.hypot(
                    self._flat[:, 0] - self._flat[:, 2],
                    self._flat[:, 1] - self._flat[:, 3],
                ).sum()
            )
            if len(self._flat)
            else 0.0
        )
        #: part id -> net indices it touches
        self._nets_of: list[list[int]] = [[] for _ in d.parts]
        for k, np_net in enumerate(d.net_pins):
            for part in np.unique(d.pin_part[np_net]):
                self._nets_of[int(part)].append(k)
        #: placed courtyard polygons per part (None = ringless); the
        #: overlap term re-touches only the moved part's pairs. A zero
        #: overlap share skips the ring work entirely — the polygons
        #: and pair scan were the whole marginal cost (3136→1319
        #: evals/s regression, seam fix2). The silkroom term reads the
        #: same rings (its shortfall is the same pair scan with a
        #: separation margin) and the margin term its pair axis, so any
        #: of the three shares keeps them alive.
        self._rings = (
            [_ring_geom(d, self.pose[i], i) for i in range(len(d.parts))]
            if (self.w.overlap or self.w.silkroom or self.w.margin)
            else []
        )
        #: every committed ring's bbox as an ``(n, 4)`` array — the label
        #: axis's numpy broad phase (:func:`_rings_bounds`). Updated at
        #: commit; try_move overlays staged rings on a per-call copy.
        #: Always an ndarray (empty when no rings are built), so the
        #: label axis never sees an Optional.
        self._ring_bounds = _rings_bounds(self._rings)
        #: the pad-union BODY polygons, parallel to ``_rings`` — what the
        #: edge half of the overlap term measures since gr529963 (the fab
        #: bar prices the body; the courtyard ring is the between-parts
        #: reservation the pair terms still use). The margin term's hole
        #: axis measures the same bodies against the hole keep-outs.
        self._bodies = (
            [_body_geom(d, self.pose[i], i) for i in range(len(d.parts))]
            if (self.w.overlap or self.w.silkroom or self.w.margin)
            else []
        )
        self._th = through_hole_parts(d)
        self._ov = (
            _ring_overlap_total(self._rings, self.pose, self._th)
            + pad_containment_total(d, self._pw)
            + edge_clearance_total(d, self.pose)
            if self.w.overlap
            else 0.0
        )
        #: silk-room margin: same rings, separation shortfall below the
        #: outline-pair requirement. Built only when the share is on.
        self._sk = (
            _ring_silkroom_total(self._rings, self.pose, self._th, d.silk.pair_req_mm)
            if self.w.silkroom
            else 0.0
        )
        #: maximin margin (Reto 2026-10-29): per-part (edge, pair, hole,
        #: label) axis arrays, the exp-sum the softmin reads, and the
        #: hole keep-outs. Static (board-level) hole zones are posed ONCE
        #: here — they never move; part-local hole rings ride their owner
        #: and are posed on use under the owner's staged-or-committed row
        #: (:meth:`_mg_zone_geoms`) — cheaper than re-posing every zone
        #: per eval the way a mesh rebuild does, and exact either way.
        #: The label axis reads the outline eroded by the edge-margin ink
        #: band — built once, the outline never moves.
        self._mg_outline = None
        self._mg_eroded = None
        self._mg_zones_static: list[tuple[int, Any]] = []
        self._mg_zones_local: list[Any] = []
        self._mg_e = np.zeros(len(d.parts), dtype=np.float64)
        self._mg_p = np.zeros(len(d.parts), dtype=np.float64)
        self._mg_h = np.zeros(len(d.parts), dtype=np.float64)
        self._mg_l = np.zeros(len(d.parts), dtype=np.float64)
        self._mg_s = 0.0
        self._mg = 0.0
        if self.w.margin:
            self._mg_outline = _outline_geom(d)
            self._mg_eroded = _eroded_outline(self._mg_outline, d.silk.edge_erode_mm)
            for z in d.zones:
                if z.kind != "mounting_hole":
                    continue
                if z.part_local and 0 <= z.part < len(d.parts):
                    self._mg_zones_local.append(z)
                else:
                    g = _zone_poly(zone_world_ring(d, z, self.pose))
                    if g is not None:
                        self._mg_zones_static.append((int(z.part), g))
            self._mg_e, self._mg_p, self._mg_h, self._mg_l = _margin_axes(
                self._rings,
                self._bodies,
                self.pose,
                self._th,
                self._mg_outline,
                self._mg_eroded,
                self._mg_zone_geoms(),
                d.part_edge_mate,
                d.parts,
                d.silk,
            )
            worst = np.maximum(
                np.maximum(self._mg_e, self._mg_p),
                np.maximum(self._mg_h, self._mg_l),
            )
            self._mg_s = float(np.exp(-worst / _MARGIN_SOFTMIN_TAU_MM).sum())
            self._mg = _margin_softmin(self._mg_s, len(d.parts))
        #: spread term: the no-shared-net pair mask, kernel scale and
        #: committed repulsion total — built only when the share is on
        #: (an (N, N) mask is the whole setup cost).
        self._noshare: np.ndarray | None = None
        self._s_scale = 1.0
        self._sp = 0.0
        if self.w.spread and len(d.parts):
            self._noshare = ~_part_shared(d)
            np.fill_diagonal(self._noshare, False)
            self._s_scale = _spread_scale(d, self.pose)
            self._sp = _spread_pairs(
                self.pose[:, :2],
                self.pose[:, 3] >= 0.5,
                self._th,
                self._noshare,
                self._s_scale,
            )
        #: reference magnitudes for share-normalised cost (floors keep a
        #: zero-crossing seed from dividing by zero — same "one per term
        #: in code" fallback the intent-api spec gives R_i ≈ 0).
        self._ref_cross = max(float(self._cross), 1.0)
        self._ref_len = max(self._len, 1.0)
        self._ref_ov = max(self._ov, 1.0)
        self._ref_sp = max(self._sp, 1.0)
        self._ref_sk = max(self._sk, 1.0)
        self._ref_mg = max(self._mg, 1.0)
        #: authored-measure pull (gr520546): per-part spec index list for
        #: the try_move delta, the committed violation total and its
        #: reference magnitude. Built only when the design carries
        #: measures AND the share is on — like ``_rings``.
        self._meas_of: list[list[int]] = [[] for _ in d.parts]
        if self.w.measures and d.measures:
            for si, spec in enumerate(d.measures):
                self._meas_of[spec.ia].append(si)
                if spec.ib >= 0:
                    self._meas_of[spec.ib].append(si)
        self._meas = (
            sum(
                _measure_violation(
                    spec,
                    self.pose[spec.ia],
                    self.pose[spec.ib] if spec.ib >= 0 else None,
                )
                for spec in d.measures
            )
            if self.w.measures and d.measures
            else 0.0
        )
        self._ref_meas = max(self._meas, 1.0)
        #: slice-5 route proxies (the flow rung): the via estimate, the
        #: fold-back mileage and the direction mix over the same wires.
        #: Each is built only when its share is on — a zero share keeps
        #: every earlier run's cost byte-identical, the overlap/spread
        #: gating convention.
        self._build_bottom = np.asarray(d.pose0)[:, 3] >= 0.5
        self._ex = _dir_exempt(d)
        self._via = via_estimate_total(d, self.pose) if self.w.vias else 0
        self._back = (
            self._backtrack_of(self._wires, self._pw) if self.w.backtrack else 0.0
        )
        self._d_scale = _spread_scale(d, self.pose) if self.w.direction else 1.0
        self._dir = (
            _dir_total(self._flat, self._flat_net, self._ex, self._d_scale)
            if self.w.direction
            else 0.0
        )
        self._ref_via = max(float(self._via), 1.0)
        self._ref_back = max(self._back, 1.0)
        self._ref_dir = max(self._dir, 1.0)
        self._stage: dict[int, np.ndarray] = {}
        self._stage_wires: dict[int, np.ndarray] = {}
        self._stage_rings: dict[int, Any] = {}
        self._stage_bodies: dict[int, Any] = {}
        #: part id → staged (edge, pair, hole, label) axis values — the margin
        #: term's per-part overlay, applied to ``_mg_e/_mg_p/_mg_h`` at
        #: commit like ``_stage_rings`` is to ``_rings``.
        self._stage_mg: dict[int, tuple[float, float, float, float]] = {}
        self._d_cross = 0
        self._d_len = 0.0
        self._d_ov = 0.0
        self._d_sp = 0.0
        self._d_meas = 0.0
        self._d_via = 0
        self._d_back = 0.0
        self._d_dir = 0.0
        self._d_sk = 0.0
        self._d_mg = 0.0
        self._d_mg_s = 0.0

    def _backtrack_of(self, wires: list[np.ndarray], pw: np.ndarray) -> float:
        """Fold-back mileage over a per-net wire list against pin world
        positions ``pw`` — the full-recompute body shared by ``__init__``
        and ``commit``."""
        total = 0.0
        for k, w in enumerate(wires):
            total += _net_backtrack(w, _net_points(self.d, self.d.net_pins[k], pw, k))
        return total

    def _bottom_now(self) -> np.ndarray:
        """Per-part mount side under the current staged-or-committed
        state — the via estimate's flip input."""
        bottom = self.pose[:, 3] >= 0.5
        for p, row in self._stage.items():
            bottom[p] = row[3] >= 0.5
        return bottom

    def _via_of(self, nets: set[int], bottom: np.ndarray) -> int:
        """The via estimate over ``nets`` at mount sides ``bottom``."""
        flipped = bottom != self._build_bottom
        return sum(_net_via_est(self.d, self.d.net_pins[k], flipped) for k in nets)

    @property
    def crossings(self) -> int:
        return self._cross + self._d_cross

    @property
    def length(self) -> float:
        return self._len + self._d_len

    @property
    def overlap(self) -> float:
        """Legality debt: same-side courtyard overlap area (mm²) PLUS
        pad containment shortfall (mm) PLUS board-edge clearance
        shortfall (mm) — three additive pieces, one number the placer
        hard-gates on."""
        return self._ov + self._d_ov

    @property
    def spread(self) -> float:
        """Board-fill repulsion total (dimensionless pair-kernel sum)."""
        return self._sp + self._d_sp

    @property
    def measures_mm(self) -> float:
        """Authored-measure violation total, weighted mm."""
        return self._meas + self._d_meas

    @property
    def vias(self) -> int:
        """Via-count estimate over all routable nets."""
        return self._via + self._d_via

    @property
    def backtrack(self) -> float:
        """Fold-back mileage total, mm."""
        return self._back + self._d_back

    @property
    def direction(self) -> float:
        """Direction-mix penalty total (weighted pair sum)."""
        return self._dir + self._d_dir

    @property
    def silkroom(self) -> float:
        """Silk-room margin total: mm of same-side ring-separation
        shortfall below the outline-pair requirement."""
        return self._sk + self._d_sk

    @property
    def margin(self) -> float:
        """Maximin margin: softmin over parts of each part's worst
        (edge / pair / hole) margin shortfall, mm."""
        return self._mg + self._d_mg

    @property
    def cost(self) -> float:
        """Share-weighted, reference-normalised J."""
        return (
            self.w.crossings * self.crossings / self._ref_cross
            + self.w.ratsnest * self.length / self._ref_len
            + self.w.overlap * self.overlap / self._ref_ov
            + self.w.spread * self.spread / self._ref_sp
            + self.w.measures * self.measures_mm / self._ref_meas
            + self.w.vias * self.vias / self._ref_via
            + self.w.backtrack * self.backtrack / self._ref_back
            + self.w.direction * self.direction / self._ref_dir
            + self.w.silkroom * self.silkroom / self._ref_sk
            + self.w.margin * self.margin / self._ref_mg
        )

    def _rest_view(self, affected: set[int]) -> tuple[np.ndarray, np.ndarray]:
        """Wires NOT in ``affected``, committed ∪ staged — the other leg
        of a staged delta."""
        wires = [
            self._stage_wires.get(k, self._wires[k])
            for k in range(len(self.d.net_pins))
            if k not in affected
        ]
        nets = [k for k in range(len(self.d.net_pins)) if k not in affected]
        return _concat(wires, nets)

    def _aff_view(self, affected: set[int]) -> tuple[np.ndarray, np.ndarray]:
        """Current (committed ∪ staged) wires OF the affected nets."""
        wires = [self._stage_wires.get(k, self._wires[k]) for k in affected]
        return _concat(wires, list(affected))

    def _ring_of(self, part: int):
        """The part's ring under the CURRENT staged-or-committed state."""
        return self._stage_rings.get(part, self._rings[part])

    def _body_of(self, part: int):
        """The part's body polygon under the CURRENT staged-or-committed
        state — :meth:`_ring_of`'s twin over the pad-union bodies the
        edge term prices (gr529963)."""
        return self._stage_bodies.get(part, self._bodies[part])

    def _side_of(self, part: int) -> bool:
        row = self._stage.get(part, self.pose[part])
        return bool(row[3] >= 0.5)

    def _pair_overlap(self, part: int, geom, side: bool) -> float:
        """``part``'s same-side overlap total against every OTHER part
        under the current staged-or-committed state — the unit the
        ``try_move`` delta telescopes on: each staged move replaces its
        own pair sums, so a two-part swap lands ``A_new∩B_new −
        A_old∩B_old`` exactly once."""
        if geom is None:
            return 0.0
        x0, y0, x1, y1 = geom.bounds
        total = 0.0
        part_th = self._th[part]
        for j in range(len(self.d.parts)):
            if j == part:
                continue
            gj = self._ring_of(j)
            if gj is None or (
                side != self._side_of(j) and not (part_th or self._th[j])
            ):
                continue
            bx0, by0, bx1, by1 = gj.bounds
            if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                continue
            total += float(geom.intersection(gj).area)
        return total

    def _pair_room(self, part: int, geom, side: bool) -> tuple[float, float]:
        """``part``'s ring-separation picture against every OTHER part
        under the current staged-or-committed state, ONE scan serving two
        terms: ``(Σ shortfall, max shortfall)`` below the design's
        :class:`SilkRoom.pair_req_mm` — the silkroom term's sum and the
        margin term's pair axis, so a move that prices both never runs a
        second pair loop. ``(0, 0)`` for a ringless part. The telescoping
        unit :meth:`try_move` deltas on, the separation-margin mirror of
        :meth:`_pair_overlap` (same rings, same side rule, ``req_mm`` of
        bbox slack instead of contact)."""
        if geom is None:
            return 0.0, 0.0
        req = self.d.silk.pair_req_mm
        x0, y0, x1, y1 = geom.bounds
        total = 0.0
        worst = 0.0
        part_th = self._th[part]
        for j in range(len(self.d.parts)):
            if j == part:
                continue
            gj = self._ring_of(j)
            if gj is None or (
                side != self._side_of(j) and not (part_th or self._th[j])
            ):
                continue
            bx0, by0, bx1, by1 = gj.bounds
            if x1 + req < bx0 or bx1 + req < x0:
                continue
            if y1 + req < by0 or by1 + req < y0:
                continue
            short = req - float(geom.distance(gj))
            total += max(0.0, short)
            worst = max(worst, short)
        return total, max(0.0, worst)

    def _mg_zone_geoms(
        self, moved: int = -1, moved_row: np.ndarray | None = None
    ) -> list[tuple[int, Any]]:
        """``(owner part id, polygon)`` per hole keep-out under the
        current staged-or-committed state: static zones come pre-posed
        from ``__init__`` (they never move); a part-local ring is posed
        under its owner's CURRENT row — ``moved``/``moved_row`` override
        for the part this ``try_move`` is staging, whose row ``_stage``
        does not carry yet."""
        out = list(self._mg_zones_static)
        for z in self._mg_zones_local:
            row = self._stage.get(z.part, self.pose[z.part])
            if z.part == moved and moved_row is not None:
                row = moved_row
            g = _zone_poly(local_to_world(np.asarray(z.ring, dtype=np.float64), row))
            if g is not None:
                out.append((int(z.part), g))
        return out

    def _mg_edge(self, part: int, ring) -> float:
        """The margin term's edge axis for one part:
        ``max(0, silk.edge_req_mm − courtyard-to-outline gap)`` — the
        FLAT silk-room requirement (a big ring overlapping the edge band
        still loses its outer wall; the retired ring-radius want priced
        that at 0). Edge-mated parts are exempt like the overlap term's
        edge half (gr530632 — the mating face is SUPPOSED to meet the
        outline)."""
        if ring is None or self._mg_outline is None:
            return 0.0
        em = self.d.part_edge_mate
        if len(em) == len(self.d.parts) and bool(em[part]):
            return 0.0
        return max(
            0.0,
            self.d.silk.edge_req_mm - float(self._mg_outline.exterior.distance(ring)),
        )

    def _mg_label(self, part: int, zones: list[tuple[int, Any]]) -> float:
        """The margin term's label axis for one part under the CURRENT
        staged-or-committed state — :func:`_label_axis` over the
        canonical spot boxes, reading staged rings/sides through
        :meth:`_ring_of` / :meth:`_side_of` so a staged move prices its
        own label consequences. The numpy broad phase runs on the
        committed bounds array with staged rings overlaid (a stale row
        could wrongly prescreen the staged ring out). ``zones`` is the
        try_move-shared posed keep-out list — the label axis reads the
        same set the hole axis does."""
        bounds = self._ring_bounds
        if self._stage_rings:
            bounds = bounds.copy()
            for p, g in self._stage_rings.items():
                if g is not None:
                    bounds[p] = g.bounds
        return _label_axis(
            part,
            self.d.parts[part],
            self._ring_of(part),
            self._ring_of,
            self._side_of,
            len(self.d.parts),
            self._th,
            self._mg_eroded,
            zones,
            bounds,
            self.d.silk.label_anchor_mm,
            self.d.silk.label_clear_mm,
        )

    def _mg_hole(self, part: int, body, zones: list[tuple[int, Any]]) -> float:
        """The margin term's hole axis for one part:
        ``max(0, HOLE_WANT_MM − min body-to-keep-out distance)`` — the
        pad-union body against the hole keep-outs, the zone's owner
        exempt like the DRC's ``courtyard_hole`` (a connector's courtyard
        covers its own standoff hole by design)."""
        if body is None:
            return 0.0
        best = math.inf
        for owner, zg in zones:
            if owner == part:
                continue
            best = min(best, float(body.distance(zg)))
        return max(0.0, HOLE_WANT_MM - best) if math.isfinite(best) else 0.0

    def _mg_pair(self, part: int, *, moved: int, moved_ring, moved_side: bool) -> float:
        """``part``'s pair axis — ``max(0, silk.pair_req_mm − min ring
        separation)`` — under the current staged-or-committed state: the
        per-part recompute a touched partner gets when ``moved``'s ring
        enters or leaves its ``req`` neighbourhood. The max half of
        :meth:`_pair_room`'s scan over the same rings; the moved part's
        NEW ring/side arrive as arguments because ``_stage`` does not
        hold this ``try_move``'s row yet."""
        geom = self._ring_of(part)
        if geom is None:
            return 0.0
        req = self.d.silk.pair_req_mm
        side = self._side_of(part)
        x0, y0, x1, y1 = geom.bounds
        worst = 0.0
        part_th = self._th[part]
        for j in range(len(self.d.parts)):
            if j == part:
                continue
            gj = moved_ring if j == moved else self._ring_of(j)
            if gj is None:
                continue
            sj = moved_side if j == moved else self._side_of(j)
            if side != sj and not (part_th or self._th[j]):
                continue
            bx0, by0, bx1, by1 = gj.bounds
            if x1 + req < bx0 or bx1 + req < x0:
                continue
            if y1 + req < by0 or by1 + req < y0:
                continue
            worst = max(worst, req - float(geom.distance(gj)))
        return max(0.0, worst)

    def _pair_spread(self, part: int, cent: np.ndarray, side: bool) -> float:
        """``part``'s repulsion total against every OTHER part under the
        current staged-or-committed state — the same telescoping unit as
        ``_pair_overlap``: O(N) numpy per staged move, each staged move
        replacing its own pair row."""
        assert self._noshare is not None
        c = self.pose[:, :2].copy()
        sides = self.pose[:, 3] >= 0.5
        for q, row in self._stage.items():
            c[q] = row[:2]
            sides[q] = row[3] >= 0.5
        d2 = ((c - cent) ** 2).sum(axis=1)
        kern = np.exp(-d2 / (2.0 * self._s_scale * self._s_scale))
        mask = self._noshare[part] & ((sides == side) | self._th)
        return float(kern[mask].sum())

    def _meas_eval(self, spec, part: int, row: np.ndarray) -> float:
        """The spec's violation with ``part``'s row swapped for ``row`` —
        every other end reads its staged-or-committed row, so staged
        moves telescope (a swap's two ``try_move`` calls land the pair
        delta exactly once)."""
        ra = row if spec.ia == part else self._stage.get(spec.ia, self.pose[spec.ia])
        if spec.ib == part:
            rb = row
        elif spec.ib >= 0:
            rb = self._stage.get(spec.ib, self.pose[spec.ib])
        else:
            rb = None
        return _measure_violation(spec, ra, rb)

    def try_move(self, part: int, pose_row: np.ndarray) -> float:
        """Stage ``pose_row`` for ``part``; return Δcost under the staged
        state (a second ``try_move`` before ``commit`` stacks, which is
        what a two-part swap is). Rebuilds MSTs of the part's nets only."""
        cost_before = self.cost
        staged_pw = self._pw.copy()
        for p, row in self._stage.items():
            staged_pw[self.d.pin_part == p] = pin_world(self.d, row, p)
        # Backtrack deltas price the OLD pins against the pre-move staged
        # state, so the pre-row positions are kept when the share is on
        # (an O(P) copy, the same cost class as the one above it).
        pre_pw = staged_pw.copy() if self.w.backtrack else None
        staged_pw[self.d.pin_part == part] = pin_world(self.d, pose_row, part)

        affected = set(self._nets_of[part])
        rest_w, rest_n = self._rest_view(affected)
        old_w, old_n = self._aff_view(affected)
        old_wires = (
            {k: self._stage_wires.get(k, self._wires[k]) for k in affected}
            if self.w.backtrack
            else {}
        )
        new_wires = {
            k: _net_wires(self.d, self.d.net_pins[k], staged_pw, k) for k in affected
        }
        new_w, new_n = _concat(list(new_wires.values()), list(new_wires))

        d_cross = (
            _cross_ab(new_w, rest_w, new_n, rest_n)
            + _within(new_w, new_n)
            - _cross_ab(old_w, rest_w, old_n, rest_n)
            - _within(old_w, old_n)
        )
        if self.w.direction:
            # The ``d_cross`` telescope with the direction weight matrix:
            # new-vs-rest plus within-new, minus old-vs-rest and
            # within-old — each staged move replaces its own pair sums.
            sc = self._d_scale
            ex = self._ex
            d_dir = (
                _dir_matrix(new_w, new_n, rest_w, rest_n, ex, sc).sum()
                + np.triu(_dir_matrix(new_w, new_n, new_w, new_n, ex, sc), 1).sum()
                - _dir_matrix(old_w, old_n, rest_w, rest_n, ex, sc).sum()
                - np.triu(_dir_matrix(old_w, old_n, old_w, old_n, ex, sc), 1).sum()
            )
            self._d_dir += float(d_dir)
        new_len = (
            float(np.hypot(new_w[:, 0] - new_w[:, 2], new_w[:, 1] - new_w[:, 3]).sum())
            if len(new_w)
            else 0.0
        )
        old_len = (
            float(np.hypot(old_w[:, 0] - old_w[:, 2], old_w[:, 1] - old_w[:, 3]).sum())
            if len(old_w)
            else 0.0
        )
        staged_row = np.asarray(pose_row, dtype=np.float64)
        if self.w.overlap or self.w.silkroom or self.w.margin:
            new_geom = _ring_geom(self.d, staged_row, part)
            old_ring = self._ring_of(part)
            self._stage_rings[part] = new_geom
            new_body = _body_geom(self.d, staged_row, part)
            old_body = self._body_of(part)
            self._stage_bodies[part] = new_body
        if self.w.silkroom or self.w.margin:
            # ONE pair scan per ring state serves both terms: the sum
            # half feeds ``_d_sk``'s telescope, the max half is the moved
            # part's margin pair axis.
            new_scan = self._pair_room(part, new_geom, staged_row[3] >= 0.5)
            old_scan = self._pair_room(part, old_ring, self._side_of(part))
        if self.w.silkroom:
            self._d_sk += new_scan[0] - old_scan[0]
        if self.w.margin:
            new_side = bool(staged_row[3] >= 0.5)
            old_side = self._side_of(part)
            zones = self._mg_zone_geoms(part, staged_row)
            touched = {part}
            # Pair axis: a partner's min-separation can change only when
            # the moved ring WAS or WILL BE within ``req`` of it — a
            # bounds-only prescan over the same staged ring list the pair
            # scan reads (both side variants, so a flip re-scans the face
            # the part leaves and the one it joins).
            req = self.d.silk.pair_req_mm
            for ring, side in ((new_geom, new_side), (old_ring, old_side)):
                if ring is None:
                    continue
                x0, y0, x1, y1 = ring.bounds
                for j in range(len(self.d.parts)):
                    if j == part or j in touched:
                        continue
                    gj = self._ring_of(j)
                    if gj is None or (
                        side != self._side_of(j) and not (self._th[part] or self._th[j])
                    ):
                        continue
                    bx0, by0, bx1, by1 = gj.bounds
                    if (
                        x1 + req < bx0
                        or bx1 + req < x0
                        or y1 + req < by0
                        or by1 + req < y0
                    ):
                        continue
                    touched.add(j)
            # Hole axis: a part-local hole ring RIDES its owner — when the
            # moved part owns one, every part's hole distance may move
            # with it (rare; board-level holes never reach this).
            owns_zones = any(z.part == part for z in self._mg_zones_local)
            if owns_zones:
                touched.update(range(len(self.d.parts)))
            # Label axis: a partner's canonical boxes reach at most
            # ``_LBL_TOUCH_REACH_MM`` off its ring — a partner whose ring
            # sits farther from BOTH the old and new ring bounds cannot
            # have a candidate's free/block status flip. Partners beyond
            # ``req`` keep their pair/edge/hole axes (the moved ring is
            # too far to change the min-separation) and recompute ONLY
            # the label axis — a full ``_mg_pair`` rescan of the annulus
            # was measured at ~3× the whole prior eval cost.
            label_touched: set[int] = set()
            reach = _LBL_TOUCH_REACH_MM
            for ring, side in ((new_geom, new_side), (old_ring, old_side)):
                if ring is None:
                    continue
                x0, y0, x1, y1 = ring.bounds
                for j in range(len(self.d.parts)):
                    if j == part or j in touched or j in label_touched:
                        continue
                    gj = self._ring_of(j)
                    if gj is None or (
                        side != self._side_of(j) and not (self._th[part] or self._th[j])
                    ):
                        continue
                    bx0, by0, bx1, by1 = gj.bounds
                    if (
                        x1 + reach < bx0
                        or bx1 + reach < x0
                        or y1 + reach < by0
                        or by1 + reach < y0
                    ):
                        continue
                    label_touched.add(j)
            for j in sorted(touched | label_touched):
                e_old, p_old, h_old, l_old = self._stage_mg.get(
                    j, (self._mg_e[j], self._mg_p[j], self._mg_h[j], self._mg_l[j])
                )
                if j == part:
                    axes = (
                        self._mg_edge(j, new_geom),
                        new_scan[1],
                        self._mg_hole(j, new_body, zones),
                        self._mg_label(j, zones),
                    )
                    p_old = old_scan[1]
                elif j in touched:
                    axes = (
                        e_old,
                        self._mg_pair(
                            j, moved=part, moved_ring=new_geom, moved_side=new_side
                        ),
                        self._mg_hole(j, self._body_of(j), zones)
                        if owns_zones
                        else h_old,
                        self._mg_label(j, zones),
                    )
                else:
                    # Label-only touch: the moved ring is beyond ``req``
                    # of this partner — pair/edge/hole axes are unchanged.
                    axes = (e_old, p_old, h_old, self._mg_label(j, zones))
                self._stage_mg[j] = axes
                tau = _MARGIN_SOFTMIN_TAU_MM
                self._d_mg_s += math.exp(-max(axes) / tau) - math.exp(
                    -max(e_old, p_old, h_old, l_old) / tau
                )
            self._d_mg = (
                _margin_softmin(self._mg_s + self._d_mg_s, len(self.d.parts)) - self._mg
            )
        if self.w.overlap:
            self._d_ov += self._pair_overlap(
                part, new_geom, staged_row[3] >= 0.5
            ) - self._pair_overlap(part, old_ring, self._side_of(part))
            # Containment and edge clearance telescope per part: only this
            # part's pins and body move. The ring pair delta above prices
            # the courtyard-overlap half; the outline half keeps pads on a
            # non-rectangular board the bbox clamp can't describe, and
            # the edge half keeps BODIES off the V-cut line (the
            # DRC's ``courtyard_edge_clearance``, measured against the
            # pad-union body since gr529963).
            g = _outline_geom(self.d)
            if g is not None:
                old_row = self._stage.get(part, self.pose[part])
                # Edge-mated parts ride free (gr530632): their face is
                # supposed to meet the outline — pricing the meeting here
                # would push them inboard where the DRC refuses them. The
                # total (edge_clearance_total) skips the same parts, so
                # the staged delta stays the parity mirror.
                em = self.d.part_edge_mate
                if not (len(em) == len(self.d.parts) and bool(em[part])):
                    self._d_ov += _ring_edge_violation(
                        g, new_body
                    ) - _ring_edge_violation(g, old_body)
                self._d_ov += _containment_part(
                    g, staged_pw[self.d.pin_part == part]
                ) - _containment_part(g, pin_world(self.d, old_row, part))
        if self.w.spread:
            old_row = self._stage.get(part, self.pose[part])
            self._d_sp += self._pair_spread(
                part, staged_row[:2], bool(staged_row[3] >= 0.5)
            ) - self._pair_spread(part, old_row[:2], bool(old_row[3] >= 0.5))
        if self._meas_of[part]:
            old_row = self._stage.get(part, self.pose[part])
            for si in self._meas_of[part]:
                spec = self.d.measures[si]
                self._d_meas += self._meas_eval(
                    spec, part, staged_row
                ) - self._meas_eval(spec, part, old_row)
        if self.w.backtrack:
            assert pre_pw is not None
            bt_new = 0.0
            bt_old = 0.0
            for k in affected:
                bt_new += _net_backtrack(
                    new_wires[k],
                    _net_points(self.d, self.d.net_pins[k], staged_pw, k),
                )
                bt_old += _net_backtrack(
                    old_wires[k],
                    _net_points(self.d, self.d.net_pins[k], pre_pw, k),
                )
            self._d_back += bt_new - bt_old
        if self.w.vias:
            bottom_old = self._bottom_now()
            bottom_new = bottom_old.copy()
            bottom_new[part] = staged_row[3] >= 0.5
            self._d_via += self._via_of(affected, bottom_new) - self._via_of(
                affected, bottom_old
            )
        self._stage[part] = staged_row.copy()
        self._stage_wires.update(new_wires)
        self._d_cross += d_cross
        self._d_len += new_len - old_len
        return self.cost - cost_before

    def commit(self) -> None:
        """Apply every staged row (one for a move, two for a swap).

        The wire-route proxies (vias, backtrack, direction) and length
        are re-derived from the committed state, never delta-accumulated,
        so the COMMITTED value matches the module-level totals up to
        float summation order (~1e-13 — a different association of the
        same per-net sums); that ulp is the honest contract and sits far
        below the Metropolis noise floor. The geometric proxies (overlap,
        spread, measures, silkroom, margin) delta-accumulate their staged
        deltas instead — a telescoped float sum the parity tests pin
        against the module totals. Margin's committed value is the
        softmin of its accumulated exp-sum over the per-part axis arrays
        its ``_stage_mg`` overlay applies here."""
        for p, row in self._stage.items():
            self.pose[p] = row
            self._pw[self.d.pin_part == p] = pin_world(self.d, row, p)
            if self._rings:
                self._rings[p] = self._stage_rings.get(p, self._rings[p])
                self._bodies[p] = self._stage_bodies.get(p, self._bodies[p])
        if self._rings:
            for p, g in enumerate(self._rings):
                if g is not None:
                    self._ring_bounds[p] = g.bounds
                else:
                    self._ring_bounds[p] = np.nan
        for p, axes in self._stage_mg.items():
            self._mg_e[p], self._mg_p[p], self._mg_h[p], self._mg_l[p] = axes
        self._ov += self._d_ov
        self._sk += self._d_sk
        self._sp += self._d_sp
        self._meas += self._d_meas
        self._mg_s += self._d_mg_s
        self._mg += self._d_mg
        self._wires = [self._stage_wires.get(k, w) for k, w in enumerate(self._wires)]
        self._flat, self._flat_net = _concat(
            self._wires, list(range(len(self.d.net_pins)))
        )
        self._cross += self._d_cross
        # length is re-derived, not delta-accumulated: float sum order
        # would drift ~1e-11 per commit and the parity contract is exact.
        self._len = (
            float(
                np.hypot(
                    self._flat[:, 0] - self._flat[:, 2],
                    self._flat[:, 1] - self._flat[:, 3],
                ).sum()
            )
            if len(self._flat)
            else 0.0
        )
        # Same contract for the wire-route proxies: the committed values
        # are re-derived from the committed state, never delta-accumulated
        # (the vias estimate is an int — delta-accumulation there is
        # exact, and the recompute is the parity reference anyway).
        if self.w.vias:
            self._via = via_estimate_total(self.d, self.pose)
        if self.w.backtrack:
            self._back = self._backtrack_of(self._wires, self._pw)
        if self.w.direction:
            self._dir = _dir_total(self._flat, self._flat_net, self._ex, self._d_scale)
        self.revert()

    def revert(self) -> None:
        """Drop all staged rows — the pre-``try_move`` state."""
        self._stage.clear()
        self._stage_wires.clear()
        self._stage_rings.clear()
        self._stage_bodies.clear()
        self._stage_mg.clear()
        self._d_cross = 0
        self._d_len = 0.0
        self._d_ov = 0.0
        self._d_sp = 0.0
        self._d_meas = 0.0
        self._d_via = 0
        self._d_back = 0.0
        self._d_dir = 0.0
        self._d_sk = 0.0
        self._d_mg = 0.0
        self._d_mg_s = 0.0

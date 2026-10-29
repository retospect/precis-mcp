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
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from shapely.geometry import Point, Polygon

from precis.pcb.ir import COURTYARD_CLEARANCE_MM
from precis.pcb.silk import outline_pair_separation_mm
from precis.pcb.topoplace.design import ObjectiveShares, TopoDesign

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


def _ring_geom(d: TopoDesign, row: np.ndarray, part: int):
    """The part's courtyard in board space as a shapely polygon —
    ``None`` for a part with no ring (synthetic designs)."""
    ring = d.part_outline[part] if part < len(d.part_outline) else ()
    pts = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 4:
        return None
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


#: The separation two courtyard rings must keep so BOTH parts draw their
#: full silk outline with the ruled outline-to-outline gap
#: (:func:`precis.pcb.silk.outline_pair_separation_mm` on the zero-clearance
#: pad hulls, minus the clearance the placer's courtyard polygons already
#: inflate by). 0.80 mm under the module fallbacks (1.60 − 2×0.40) — the
#: same "no capability row in the placer design build" constraint
#: :data:`_EDGE_CLEAR_MM` documents; the rung that carries capability can
#: scale the term without a second formula.
_SILK_ROOM_REQ_MM = max(
    0.0, outline_pair_separation_mm(None) - 2.0 * COURTYARD_CLEARANCE_MM
)


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


#: Courtyard-to-board-edge clearance floor — the DRC's
#: ``courtyard_edge_clearance`` jlc_min bar (JLC vcut minimum). The
#: placer design build sees no fab capability row (the same constraint
#: as net rule widths), so the term holds the flat minimum; the
#: house-default tier and edge-mate exemptions belong to the rung that
#: carries capability.
_EDGE_CLEAR_MM = 0.4


def _ring_edge_violation(g: Any, ring: Any) -> float:
    """``max(0, _EDGE_CLEAR_MM − gap)`` between a courtyard ring and
    the outline's EXTERIOR ring — the DRC's
    ``courtyard_edge_clearance`` distance (taz-leaf-b's C12: pads
    inside, courtyard 0.13 mm off the V-cut). ``Polygon.distance``
    cannot be used here: it is 0 whenever one polygon contains the
    other, so the gap must be measured against the boundary, exactly
    what :func:`precis.pcb.drc.check_courtyard_edge_clearance` does."""
    if ring is None:
        return 0.0
    return max(0.0, _EDGE_CLEAR_MM - float(g.exterior.distance(ring)))


def edge_clearance_total(d: TopoDesign, pose: np.ndarray) -> float:
    """Σ courtyard-to-edge shortfall over all parts — full recompute,
    the edge half of the overlap term's outline measures. Edge-mated
    parts are skipped: their mating face is SUPPOSED to sit on the
    outline, and pricing them would push them inboard where
    ``check_edge_mating`` refuses the board (gr530632)."""
    g = _outline_geom(d)
    if g is None:
        return 0.0
    em = d.part_edge_mate
    return float(
        sum(
            _ring_edge_violation(g, _ring_geom(d, pose[i], i))
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
        #: separation margin), so either share keeps them alive.
        self._rings = (
            [_ring_geom(d, self.pose[i], i) for i in range(len(d.parts))]
            if (self.w.overlap or self.w.silkroom)
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
            _ring_silkroom_total(self._rings, self.pose, self._th, _SILK_ROOM_REQ_MM)
            if self.w.silkroom
            else 0.0
        )
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
        self._d_cross = 0
        self._d_len = 0.0
        self._d_ov = 0.0
        self._d_sp = 0.0
        self._d_meas = 0.0
        self._d_via = 0
        self._d_back = 0.0
        self._d_dir = 0.0
        self._d_sk = 0.0

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

    def _pair_silkroom(self, part: int, geom, side: bool) -> float:
        """``part``'s silk-room shortfall against every OTHER part under
        the current staged-or-committed state — the telescoping unit
        :meth:`try_move` deltas on, the separation-margin mirror of
        :meth:`_pair_overlap` (same rings, same side rule, ``req_mm`` of
        bbox slack instead of contact)."""
        if geom is None:
            return 0.0
        req = _SILK_ROOM_REQ_MM
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
            if x1 + req < bx0 or bx1 + req < x0:
                continue
            if y1 + req < by0 or by1 + req < y0:
                continue
            total += max(0.0, req - float(geom.distance(gj)))
        return total

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
        if self.w.overlap or self.w.silkroom:
            new_geom = _ring_geom(self.d, staged_row, part)
            old_ring = self._ring_of(part)
            self._stage_rings[part] = new_geom
        if self.w.silkroom:
            self._d_sk += self._pair_silkroom(
                part, new_geom, staged_row[3] >= 0.5
            ) - self._pair_silkroom(part, old_ring, self._side_of(part))
        if self.w.overlap:
            self._d_ov += self._pair_overlap(
                part, new_geom, staged_row[3] >= 0.5
            ) - self._pair_overlap(part, old_ring, self._side_of(part))
            # Containment and edge clearance telescope per part: only this
            # part's pins and ring move. The ring pair delta above prices
            # the courtyard-overlap half; the outline half keeps pads on a
            # non-rectangular board the bbox clamp can't describe, and
            # the edge half keeps courtyards off the V-cut line (the
            # DRC's ``courtyard_edge_clearance``).
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
                        g, new_geom
                    ) - _ring_edge_violation(g, old_ring)
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
        spread, measures, silkroom) delta-accumulate their staged deltas
        instead — a telescoped float sum the parity tests pin against the
        module totals."""
        for p, row in self._stage.items():
            self.pose[p] = row
            self._pw[self.d.pin_part == p] = pin_world(self.d, row, p)
            if self._rings:
                self._rings[p] = self._stage_rings.get(p, self._rings[p])
        self._ov += self._d_ov
        self._sk += self._d_sk
        self._sp += self._d_sp
        self._meas += self._d_meas
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
        self._d_cross = 0
        self._d_len = 0.0
        self._d_ov = 0.0
        self._d_sp = 0.0
        self._d_meas = 0.0
        self._d_via = 0
        self._d_back = 0.0
        self._d_dir = 0.0
        self._d_sk = 0.0

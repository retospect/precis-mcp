"""Legalize a pose: drive same-side courtyard overlaps to zero.

The proxy's ``overlap`` share pulls the anneal toward legal poses, but
on a dense real board the best pose still carries collisions the commit
gate counts as pre-route DRC errors. This post-pass walks the worst
same-side overlapping pair per round and separates it exactly — the
minimum-translation vector from SAT on the (convex) courtyard rings —
splitting the push between two free parts, moving only the free one
against a fixed part, and skipping both-fixed pairs (unresolvable;
reported). Parts the push shoves outside the board outline are pulled
back toward the outline centroid between sweep rounds. Returns the
legalized pose plus a report; phase 3's corridor LP replaces this with
a global minimum-displacement solve when slice 4 lands.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from shapely import box as shapely_box
from shapely.geometry import Polygon

from precis.pcb.topoplace.design import TopoDesign, TopoZone
from precis.pcb.topoplace.proxy import (
    _EPS,
    _ring_geom,
    local_to_world,
    through_hole_parts,
    zone_world_ring,
)


@dataclass(frozen=True, slots=True)
class LegalizeResult:
    """``pose`` is the legalized pose; ``resolved`` means zero same-side
    overlap remains. ``remaining`` lists unresolved (a, b, area) pairs —
    both-fixed collisions and any budget exhaustion; ``moved_mm`` the
    total translation applied."""

    pose: np.ndarray
    resolved: bool
    rounds: int
    moved_mm: float
    remaining: tuple[tuple[int, int, float], ...] = field(default_factory=tuple)


def _axes(ring: np.ndarray) -> list[np.ndarray]:
    """Unit outward-ish normals of a polygon ring's edges (SAT axes)."""
    pts = np.asarray(ring, dtype=np.float64)
    if len(pts) >= 2 and np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    edges = np.roll(pts, -1, axis=0) - pts
    n = np.hypot(edges[:, 0], edges[:, 1])
    n[n == 0] = 1.0
    normals = np.column_stack([-edges[:, 1] / n, edges[:, 0] / n])
    return [row for row in normals]


def _mtv(ga: np.ndarray, gb: np.ndarray) -> tuple[np.ndarray, float]:
    """Minimum translation vector separating ``gb`` from ``ga`` — axis
    with the smallest projection overlap, direction pushing ``gb`` away
    from ``ga``'s centroid. Returns (vector, depth); depth 0 = already
    separated. Exact for convex polygons."""
    ca = np.asarray(ga, dtype=np.float64)
    cb = np.asarray(gb, dtype=np.float64)
    if np.allclose(ca[0], ca[-1]):
        ca = ca[:-1]
    if np.allclose(cb[0], cb[-1]):
        cb = cb[:-1]
    cen_a, cen_b = ca.mean(axis=0), cb.mean(axis=0)
    best_axis = np.zeros(2)
    best_ov = np.inf
    for ring in (ca, cb):
        for n in _axes(ring):
            pa, pb = ca @ n, cb @ n
            ov = min(pa.max(), pb.max()) - max(pa.min(), pb.min())
            if ov < best_ov:
                best_ov = ov
                best_axis = n if (cen_b - cen_a) @ n > 0 else -n
    if not np.isfinite(best_ov) or best_ov <= 0:
        return np.zeros(2), 0.0
    return best_axis * (best_ov + 1e-6), float(best_ov)


def _zone_blocks_part(z: TopoZone, side: bool, th: bool, n_layers: int) -> bool:
    """Whether the part's body intrudes on zone ``z`` — a layer-scoped
    keepout bites only when the part's outer face is on it; a TH barrel
    or a non-scoped zone blocks every layer."""
    if not z.layers:
        return True
    if th:
        return True
    outer = (n_layers - 1) if side else 0
    return outer in z.layers


def _pin_net_names(d: TopoDesign) -> dict[int, str]:
    """pin id → net name — the shared resolver ``_part_nets`` and the
    per-pad fixed-via exemption read.

    Routed nets come from the compacted ``net_pins`` slots:
    ``net_name[k]`` names slot ``k``'s pins. Plane and singleton nets
    are dropped from ``net_pins``, so their pins fall back to the 1b
    decl tables — the pin's ir net id (``pin_net``) resolves through
    ``decl_net_ir`` to ``decl_net_name``. A pin on a net no declaration
    names gets no name and cannot match a zone's ``net``."""
    name_of_ir = {
        int(ir_id): str(d.decl_net_name[j])
        for j, ir_id in enumerate(d.decl_net_ir)
        if int(ir_id) >= 0
    }
    # ``d.pin_net`` carries IR net ids; ``net_name`` is indexed by the
    # compacted ``net_pins`` slot (plane and singleton nets are dropped).
    # A part whose pins' ir ids exceed the routed set crashed the lookup
    # — and on id-aligned boards it silently read the WRONG net's name.
    net_of_pin: dict[int, str] = {}
    for k, pins in enumerate(d.net_pins):
        for p in pins:
            net_of_pin[int(p)] = str(d.net_name[k])
    for p in range(len(d.pin_part)):
        if int(d.pin_part[p]) < 0 or int(p) in net_of_pin:
            continue
        name = name_of_ir.get(int(d.pin_net[p]))
        if name:
            net_of_pin[int(p)] = name
    return net_of_pin


def _part_nets(d: TopoDesign, part: int) -> set[str]:
    """The net names under a part's pins — the same-net fixed-copper
    exemption the DRC gives an authored via/copper the part belongs to.
    For ``fixed_via`` zones this per-PART set is NOT enough — the
    per-pad rule lives in :func:`_via_zone_exempt`."""
    net_of_pin = _pin_net_names(d)
    out: set[str] = set()
    for p in np.flatnonzero(d.pin_part == part):
        name = net_of_pin.get(int(p))
        if name:
            out.add(name)
    return out


def _pad_polys(d: TopoDesign, pose_row: np.ndarray, part: int) -> list[tuple[int, Any]]:
    """(pin id, world-frame rect) for the part's REAL pads under
    ``pose_row`` — the per-pad geometry the fixed-via exemption
    judges. ``pin_wh`` supplies (w, h); a missing/zero size degrades
    to a 0.1 mm point rather than vanishing. Quarter-turn poses keep
    the rects axis-aligned, so the transformed-corner bbox is exact."""
    out: list[tuple[int, Any]] = []
    for p in np.flatnonzero(d.pin_part == part):
        w = float(d.pin_wh[p, 0]) if p < len(d.pin_wh) else 0.0
        h = float(d.pin_wh[p, 1]) if p < len(d.pin_wh) else 0.0
        hw, hh = max(w, 0.1) / 2.0, max(h, 0.1) / 2.0
        px, py = float(d.pin_xy[p, 0]), float(d.pin_xy[p, 1])
        corners = np.asarray(
            [
                [px - hw, py - hh],
                [px + hw, py - hh],
                [px + hw, py + hh],
                [px - hw, py + hh],
            ],
            dtype=np.float64,
        )
        world = local_to_world(corners, pose_row)
        lo = world.min(axis=0)
        hi = world.max(axis=0)
        out.append(
            (
                int(p),
                shapely_box(float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])),
            )
        )
    return out


def _via_zone_exempt(
    d: TopoDesign,
    pose_row: np.ndarray,
    part: int,
    z: TopoZone,
    zg: Any,
    net_of_pin: Mapping[int, str],
    pads: list[tuple[int, Any]] | None = None,
) -> bool:
    """Per-PAD fixed-via exemption — the mirror of the placement gate's
    per-pad rule (``check_via_pad_keepout``), replacing the old per-part
    one (gr520903): covering an authored via is legal only where an
    ON-NET pad lands on it. Both sides evaluate the SAME predicate —
    ``drc._via_anchor_ok``: an on-net pad on the ring (edge within
    ``_VIA_ANCHOR_TOL_MM``) and every pad that close on the via's net —
    so legalize never accepts a cover the gate then reports
    ``via_in_pad`` (an ordinary pad overlapping the annulus; only a
    copper-field pad — ``d.pin_role`` — may swallow it, gr496850)."""
    from precis.pcb.drc import (
        _COPPER_FIELD_PAD_ROLES,
        _VIA_ANCHOR_TOL_MM,
        _via_anchor_ok,
    )

    pads = _pad_polys(d, pose_row, part) if pads is None else pads
    hits = []
    for p, g in pads:
        dist = float(g.distance(zg))
        if dist > _VIA_ANCHOR_TOL_MM:
            continue
        hits.append(
            (
                net_of_pin.get(int(p)),
                dist,
                g.intersection(zg).area > _EPS,
                int(p) < len(d.pin_role)
                and str(d.pin_role[int(p)]) in _COPPER_FIELD_PAD_ROLES,
            )
        )
    return _via_anchor_ok(str(z.net), hits)


def _legalize_zones(
    d: TopoDesign,
    pose: np.ndarray,
    geoms: list[Any],
    sides: np.ndarray,
    th: np.ndarray,
    zone_geoms: dict[int, Any],
    part_nets: list[set[str]],
) -> tuple[float, tuple[int, int, float] | None]:
    """One zone sweep: the worst part-on-zone overlap (mounting hole,
    keepout, foreign fixed copper) gets the full MTV on the part — the
    zone cannot move. A part on its OWN net's copper is legal (the
    same-net authored-via exemption) and skipped; for ``fixed_via``
    zones the exemption is per PAD — only an on-net pad on the ring is
    a legal cover."""
    n_layers = max((max(z.layers) for z in d.zones if z.layers), default=0) + 1
    pin_names: dict[int, str] | None = None
    pads_of: dict[int, list[tuple[int, Any]]] = {}
    worst: tuple[int, int, float] | None = None
    worst_area = _EPS
    for i, gi in enumerate(geoms):
        if gi is None:
            continue
        x0, y0, x1, y1 = gi.bounds
        for z_i, zg in zone_geoms.items():
            z = d.zones[z_i]
            if z.part == i:
                continue
            if not _zone_blocks_part(z, bool(sides[i]), bool(th[i]), n_layers):
                continue
            bx0, by0, bx1, by1 = zg.bounds
            if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                continue
            if z.kind == "fixed_via":
                if pin_names is None:
                    pin_names = _pin_net_names(d)
                if i not in pads_of:
                    pads_of[i] = _pad_polys(d, pose[i], i)
                if _via_zone_exempt(d, pose[i], i, z, zg, pin_names, pads_of[i]):
                    continue
            elif z.net and z.net in part_nets[i]:
                continue
            area = float(gi.intersection(zg).area)
            if area > worst_area:
                worst, worst_area = (i, z_i, area), area
    if worst is None:
        return 0.0, None
    i, z_i, _area = worst
    if d.fixed[i]:
        return 0.0, (i, len(d.parts) + z_i, worst_area)
    vec, _depth = _mtv(
        np.asarray(zone_geoms[z_i].exterior.coords),
        np.asarray(geoms[i].exterior.coords),
    )
    vec = vec + np.sign(vec + 1e-12) * 1e-3
    moved = float(np.hypot(*vec))
    pose[i, 0] += vec[0]
    pose[i, 1] += vec[1]
    geoms[i] = _ring_geom(d, pose[i], i)
    _refresh_owned_zones(d, pose, zone_geoms, i)
    return moved, None


def _legalize_once(
    d: TopoDesign,
    pose: np.ndarray,
    geoms: list[Any],
    sides: np.ndarray,
    th: np.ndarray,
    dead: set[tuple[int, int]],
) -> tuple[float, tuple[int, int, float] | None]:
    """One sweep: find the worst same-side overlapping pair, apply the
    MTV split/moved as fixed bits allow. Returns (mm moved, worst pair
    unresolved-or-None). Both-fixed pairs can't move — they join
    ``dead`` so later sweeps stop re-finding them."""
    n = len(d.parts)
    worst: tuple[int, int, float] | None = None
    worst_area = _EPS
    for i in range(n):
        gi = geoms[i]
        if gi is None:
            continue
        x0, y0, x1, y1 = gi.bounds
        for j in range(i + 1, n):
            if (i, j) in dead:
                continue
            gj = geoms[j]
            if gj is None or (sides[i] != sides[j] and not (th[i] or th[j])):
                continue
            bx0, by0, bx1, by1 = gj.bounds
            if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                continue
            area = float(gi.intersection(gj).area)
            if area > worst_area:
                worst, worst_area = (i, j, area), area
    if worst is None:
        return 0.0, None
    i, j, _area = worst
    free_i, free_j = not d.fixed[i], not d.fixed[j]
    if not (free_i or free_j):
        dead.add((i, j))
        return 0.0, worst  # unresolvable
    # MTV on WORLD rings (rot+mirror applied) via the placed polygons.
    vec, _depth = _mtv(
        np.asarray(geoms[i].exterior.coords), np.asarray(geoms[j].exterior.coords)
    )
    vec = vec + np.sign(vec + 1e-12) * 1e-3  # clearance epsilon
    if free_i and free_j:
        di, dj = -vec / 2.0, vec / 2.0
    elif free_i:
        di, dj = -vec, np.zeros(2)
    else:
        di, dj = np.zeros(2), vec
    moved = float(np.hypot(*di) + np.hypot(*dj))
    pose[i, 0] += di[0]
    pose[i, 1] += di[1]
    pose[j, 0] += dj[0]
    pose[j, 1] += dj[1]
    geoms[i] = _ring_geom(d, pose[i], i)
    geoms[j] = _ring_geom(d, pose[j], j)
    return moved, None


def _zone_geoms(d: TopoDesign, pose: np.ndarray) -> dict[int, Any]:
    """Zone polygons at ``pose``, keyed by zone index — part-owned hole
    rings follow their owner's pose row, so the sweep re-derives them
    each round rather than keeping the entry-pose snapshot."""
    return {
        z_i: Polygon(np.asarray(zone_world_ring(d, z, pose), dtype=np.float64)[:-1])
        for z_i, z in enumerate(d.zones)
        if len(z.ring) >= 4
    }


def _refresh_owned_zones(
    d: TopoDesign, pose: np.ndarray, zgs: dict[int, Any], part: int
) -> None:
    """Re-derive the hole rings ``part`` owns at its NEW pose, in place —
    the round checks other parts against zone rings snapshotted at its
    start, but a moved owner's own ring rides it within the same sweep
    (a connector pushed off one hole must not leave its standoff ring
    behind for the pull-back to test against)."""
    for z_i, z in enumerate(d.zones):
        if z.part == part and z_i in zgs:
            zgs[z_i] = Polygon(
                np.asarray(zone_world_ring(d, z, pose), dtype=np.float64)[:-1]
            )


def legalize_pose(
    d: TopoDesign, pose: np.ndarray, *, rounds: int = 2000
) -> LegalizeResult:
    """Drive every same-side courtyard pair apart (worst-first, MTV
    exact per round), then pull outline escapees back inside, repeating
    until clean or ``rounds`` pair-sweeps are spent."""
    pose = np.asarray(pose, dtype=np.float64).copy()
    n = len(d.parts)
    if not len(d.part_outline):
        return LegalizeResult(pose=pose, resolved=True, rounds=0, moved_mm=0.0)
    sides = pose[:, 3] >= 0.5
    th = through_hole_parts(d)
    geoms = [_ring_geom(d, pose[i], i) for i in range(n)]
    zone_geoms = _zone_geoms(d, pose)
    part_nets = [_part_nets(d, i) for i in range(n)] if zone_geoms else []
    n_layers = max((max(z.layers) for z in d.zones if z.layers), default=0) + 1

    pin_names_hit: dict[int, str] | None = None

    def _zone_hit(geom, part: int, zgs: dict[int, Any], test_row: np.ndarray) -> bool:
        """Whether ``geom`` intrudes on any blocking zone — the pull-back
        must not land a part on a hole/keepout while fixing its outline
        escape (the loop-8 residual: R11 on the mounting hole)."""
        nonlocal pin_names_hit
        for z_i, zg in zgs.items():
            z = d.zones[z_i]
            if z.part == part:
                continue
            if not _zone_blocks_part(z, bool(sides[part]), bool(th[part]), n_layers):
                continue
            if not geom.intersects(zg):
                continue
            if z.kind == "fixed_via":
                if pin_names_hit is None:
                    pin_names_hit = _pin_net_names(d)
                if _via_zone_exempt(d, test_row, part, z, zg, pin_names_hit):
                    continue
            elif z.net and z.net in part_nets[part]:
                continue
            return True
        return False

    outline_geom = (
        Polygon(np.asarray(d.outline, dtype=np.float64))
        if len(d.outline) >= 3
        else None
    )
    # Pull-back anchors: the outline centroid AND its representative
    # point (guaranteed interior even for concave outlines); containment
    # along the path is sampled, not bisected — on a concave board the
    # line can leave and re-enter, which bisection reads wrongly.
    anchors: list[np.ndarray] = []
    if outline_geom is not None:
        anchors = [
            np.asarray(outline_geom.centroid.coords[0]),
            np.asarray(outline_geom.representative_point().coords[0]),
        ]
    dead: set[tuple[int, int]] = set()
    total_moved = 0.0
    spent = 0
    zgs = zone_geoms
    for _ in range(rounds):
        moved, _skipped = _legalize_once(d, pose, geoms, sides, th, dead)
        # zone sweeps run every round — a pull or a pair fix can land a
        # part on a zone at ANY point, not only when pairs are settled.
        if zone_geoms:
            # round-start snapshot: covers the pair sweep's moves; the
            # zone sweep and the pull-back refresh a moved owner's own
            # ring in place as they go.
            zgs = _zone_geoms(d, pose)
            zmoved, _zskip = _legalize_zones(d, pose, geoms, sides, th, zgs, part_nets)
            moved += zmoved
        spent += 1
        # outline pull-back runs every round escapees exist — a
        # separation move shoves parts out AND the pull can itself
        # unblock the next sweep, so it is not gated on ``moved``.
        if anchors and outline_geom is not None:
            for i in range(n):
                gi = geoms[i]
                if gi is None or d.fixed[i] or outline_geom.covers(gi):
                    continue
                start = np.asarray(pose[i][:2])
                for anchor in anchors:
                    target = anchor - start
                    hit = 0.0
                    for t in np.linspace(0.05, 1.0, 20):
                        test_row = np.asarray(pose[i]).copy()
                        test_row[:2] = start + target * t
                        tg = _ring_geom(d, test_row, i)
                        if (
                            tg is not None
                            and outline_geom.covers(tg)
                            and not _zone_hit(tg, i, zgs, test_row)
                        ):
                            hit = float(t)
                            break
                    if hit > 0.0:
                        pose[i, 0] += target[0] * hit
                        pose[i, 1] += target[1] * hit
                        geoms[i] = _ring_geom(d, pose[i], i)
                        _refresh_owned_zones(d, pose, zgs, i)
                        moved += hit * float(np.hypot(*target))
                        break
        if moved == 0.0:
            break
        total_moved += moved
    # final report — pair leftovers, then unresolvable zone leftovers
    # (a part on a zone reports b = n_parts + zone index)
    remaining: list[tuple[int, int, float]] = []
    for i in range(n):
        gi = geoms[i]
        if gi is None:
            continue
        for j in range(i + 1, n):
            gj = geoms[j]
            if gj is None or (sides[i] != sides[j] and not (th[i] or th[j])):
                continue
            x0, y0, x1, y1 = gi.bounds
            bx0, by0, bx1, by1 = gj.bounds
            if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                continue
            area = float(gi.intersection(gj).area)
            if area > 1e-6:
                remaining.append((i, j, area))
    n_layers = max((max(z.layers) for z in d.zones if z.layers), default=0) + 1
    pin_names_fin: dict[int, str] | None = None
    pads_fin: dict[int, list[tuple[int, Any]]] = {}
    for i, gi in enumerate(geoms):
        if gi is None:
            continue
        x0, y0, x1, y1 = gi.bounds
        for z_i, zg in _zone_geoms(d, pose).items():
            z = d.zones[z_i]
            if z.part == i:
                continue
            if not _zone_blocks_part(z, bool(sides[i]), bool(th[i]), n_layers):
                continue
            bx0, by0, bx1, by1 = zg.bounds
            if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                continue
            if z.kind == "fixed_via":
                if pin_names_fin is None:
                    pin_names_fin = _pin_net_names(d)
                if i not in pads_fin:
                    pads_fin[i] = _pad_polys(d, pose[i], i)
                if _via_zone_exempt(d, pose[i], i, z, zg, pin_names_fin, pads_fin[i]):
                    continue
            elif z.net and z.net in part_nets[i]:
                continue
            area = float(gi.intersection(zg).area)
            if area > 1e-6:
                remaining.append((i, n + z_i, area))
    if outline_geom is not None:
        for i, gi in enumerate(geoms):
            if gi is None:
                continue
            outside = float(gi.difference(outline_geom).area)
            if outside > 1e-6:
                remaining.append((i, -1, outside))
    return LegalizeResult(
        pose=pose,
        resolved=not remaining,
        rounds=spent,
        moved_mm=total_moved,
        remaining=tuple(sorted(remaining)),
    )


__all__ = ["LegalizeResult", "legalize_pose"]

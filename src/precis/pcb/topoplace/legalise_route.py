"""Joint legalisation of the topo embedding (td569542).

Reto's model: the wire search routes completely in math space, then a
legalisation round shoves things slightly — planted via centres, per-
crossing wire offsets along their edges, and unfrozen part translations —
to make the embedding realisable, before emission. One LP per round,
extending :func:`precis.pcb.topoplace.shove.shove_pose`'s idiom: min
Σ|Δ| (L1 through epigraph variables), soft driver rows priced by slack,
hard guards, ≤``_CAP_MM`` per round, re-linearised each round.

Variables:

- via centres (2-D), bounded to the hop edge's triangle pair so the
  homotopy class holds;
- wire offsets (1-D along-edge position) per crossing on a contested
  ``(edge, layer)`` cut, bounded to the edge's free stretch;
- part translations (2-D per rigid unit) — unfrozen, grouped, hard-
  measure endpoints excluded (``proxy.settle_measures`` owns those).

Rows: ordered same-layer spacing plus end margins per contested cut,
via keepouts linearised from the ``_ViaPlanter`` predicates (pads at
``r_site + max(clearance, web)``, zones, outline, drill holes, earlier
wires and vias), the triangle signed-area guard, and the part
separation/zone rows. Rows still violated after application name their
nets for re-route.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix, lil_matrix
from shapely import STRtree
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points

from precis.pcb.clearance import FAB_QUANTUM_MM
from precis.pcb.rules import NetRules
from precis.pcb.topoplace import wires as _wires
from precis.pcb.topoplace.contain import board_polygon
from precis.pcb.topoplace.copper_index import ANY_NET, NO_OWN_NET
from precis.pcb.topoplace.crossings import edge_xy
from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.legalize import (
    _mtv,
    _pad_polys,
    _part_nets,
    _pin_net_names,
    _via_zone_exempt,
    _zone_blocks_part,
)
from precis.pcb.topoplace.mesh import MeshConflictError, TopoMesh, build_mesh
from precis.pcb.topoplace.proxy import (
    _EPS,
    _body_geom,
    _ring_geom,
    one_body_pairs,
    rigid_pairs,
    through_hole_parts,
    zone_world_circle,
    zone_world_ring,
)
from precis.pcb.topoplace.realise import _SHARP_TURN_DEG, _turn_deg
from precis.pcb.topoplace.shove import (
    _CAP_MM,
    _CLR_MM,
    _SLACK_W,
    _TRI_FLOOR_MM2,
    _legal_xy,
    _vert_owner,
)

_DEFAULT_NR = NetRules(track_width_mm=0.15, clearance_mm=0.15)
_ROUTE_SLACK_W = 1e4  # cut/via rows only relax when truly stuck


@dataclass(frozen=True, slots=True)
class LegaliseResult:
    """What one legalisation run changed. ``vias`` maps
    ``(branch, edge_index)`` to the moved centre; ``offsets`` maps
    ``(branch, edge_index)`` to the new crossing parameter on that edge;
    ``pose`` is the absolute pose after part moves (the input pose when
    nothing moved). ``reroute`` names ``net_pins`` indices whose rows
    stayed violated — the caller hands them back to the search as nets
    to re-route last."""

    pose: np.ndarray
    vias: dict[tuple[int, int], tuple[float, float]] = field(default_factory=dict)
    offsets: dict[tuple[int, int], float] = field(default_factory=dict)
    reroute: frozenset[int] = frozenset()
    moved_mm: float = 0.0
    reason: str = ""
    rounds: int = 0


def _convex_hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Monotone-chain hull, ccw, no repeated endpoint."""
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lo: list[tuple[float, float]] = []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    up: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0:
            up.pop()
        up.append(p)
    return lo[:-1] + up[:-1]


def _tri_pair_hull(mesh: TopoMesh, e: int) -> list[tuple[float, float]]:
    """Convex hull of the (up to) two triangles incident on edge ``e`` —
    the via's legal region, as hull vertices."""
    tris = mesh.edge_tris[e] if 0 <= e < len(mesh.edge_tris) else (-1, -1)
    pts: list[tuple[float, float]] = []
    for t in tris:
        t = int(t)
        if 0 <= t < len(mesh.tris):
            for v in mesh.tris[t]:
                xy = mesh.verts[int(v)]
                pts.append((float(xy[0]), float(xy[1])))
    return _convex_hull(pts)


def _hull_rows(
    hull: list[tuple[float, float]],
) -> list[tuple[float, float, float]]:
    """Hull edges as ``(nx, ny, c)`` half-planes ``n·p ≤ c`` — for a ccw
    hull the outward normal of each directed edge is its right normal."""
    rows = []
    m = len(hull)
    for i in range(m):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % m]
        ex, ey = bx - ax, by - ay
        nx, ny = ey, -ex
        norm = math.hypot(nx, ny)
        if norm <= _EPS:
            continue
        nx, ny = nx / norm, ny / norm
        rows.append((nx, ny, nx * ax + ny * ay))
    return rows


def _free_intervals(
    ctx: Any,
    e: int,
    layer: int,
    r_ref: float,
    seglen: float,
    extra: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Free stretches of edge ``e`` on ``layer`` in mm along u→v: pad
    covers at ``r_ref`` merged, minus ``extra`` (via covers, mm)."""
    ints = [
        (a * seglen, b * seglen)
        for a, b, q in ctx.cover_cache.get(r_ref, {}).get(e, ())
        if int(ctx.pad_copper[q]) & (1 << layer)
    ]
    ints += list(extra)
    free: list[tuple[float, float]] = []
    cur = 0.0
    for a, b in sorted(ints):
        if a > cur:
            free.append((cur, a))
        cur = max(cur, b)
    if cur < seglen:
        free.append((cur, seglen))
    return [(a, b) for a, b in free if b - a > _EPS]


def _closest_on_geom(p: tuple[float, float], geom: Any) -> tuple[float, float]:
    """Closest point on ``geom`` to ``p``."""
    q = nearest_points(Point(p), geom)[1]
    return (float(q.x), float(q.y))


def _normal(p: tuple[float, float], q: tuple[float, float]) -> tuple[float, float]:
    dx, dy = p[0] - q[0], p[1] - q[1]
    n = math.hypot(dx, dy)
    if n <= _EPS:
        return (0.0, 0.0)
    return (dx / n, dy / n)


def legalise_route(
    d: TopoDesign,
    mesh: TopoMesh,
    wires: _wires.Wires,
    pose: np.ndarray,
    *,
    n_layers: int,
    net_rules: Mapping[str, NetRules] | None = None,
    rules_t: Sequence[Sequence[float]] | None = None,
    planter: _wires._ViaPlanter | None = None,
    rounds: int = 4,
    part_sink: list[np.ndarray] | None = None,
) -> LegaliseResult:
    """One joint legalisation run over ``wires`` at ``pose``.

    Via/offset moves are returned for the caller to apply through
    :func:`apply_legalise`; part translations come back as the absolute
    ``pose`` (and on ``part_sink`` when given) for a re-route. Rows still
    violated name their ``net_pins`` indices in ``reroute``.
    """
    rules = net_rules or {}
    pose_a = np.asarray(pose, dtype=np.float64).copy()
    n = len(d.parts)

    def _dims(net: int) -> tuple[float, float]:
        if rules_t is not None and 0 <= net < len(rules_t):
            return float(rules_t[net][0]), float(rules_t[net][1])
        name = str(d.net_name[net]) if 0 <= net < len(d.net_name) else str(net)
        nr = rules.get(name, _DEFAULT_NR)
        return float(nr.track_width_mm), float(nr.clearance_mm)

    # part units — shove_pose's grouping, minus hard-measure endpoints
    # (settle_measures owns those; a legalise shove must never break a
    # crystal from its load caps)
    measure_parts: set[int] = set()
    for s in d.measures:
        if s.hard:
            if 0 <= s.ia < n:
                measure_parts.add(s.ia)
            if 0 <= s.ib < n:
                measure_parts.add(s.ib)
    movable = np.flatnonzero(~d.fixed)
    var_of = np.full(n, -1, dtype=np.int32)
    units: list[tuple[int, ...]] = []
    seen: set[int] = set()
    for pi in movable:
        p = int(pi)
        if p in seen or p in measure_parts:
            continue
        gid = int(d.group_of[p]) if len(d.group_of) else -1
        mates = d.group_members[gid] if gid >= 0 else ()
        unit = tuple(m for m in mates if not d.fixed[m] and m not in measure_parts) or (
            p,
        )
        if any(m in measure_parts for m in unit):
            continue
        for m in unit:
            var_of[m] = len(units)
        seen.update(unit)
        units.append(unit)
    m_count = len(units)

    ctx = None
    r_ref = 0.0
    finest = (0.15, 0.15)
    try:
        ctx = _wires._make_ctx(mesh, d, n_layers, pose_a)
        finest = _wires.finest_rule(
            [(_dims(k)[0], _dims(k)[1]) for k in range(len(d.net_pins))]
        )
        r_ref = finest[0] / 2.0 + finest[1] + FAB_QUANTUM_MM
        _wires._pad_eval(ctx, r_ref)
    except Exception:
        ctx = None

    if planter is None and ctx is not None:
        try:
            planter = _wires._ViaPlanter(
                ctx,
                d,
                pose_a,
                net_clearance_by_name={
                    str(d.net_name[k]): _dims(k)[1]
                    for k in range(min(len(d.net_name), len(d.net_pins)))
                },
            )
        except Exception:
            planter = None

    lo = d.outline.min(axis=0) if len(d.outline) >= 3 else pose_a[:, :2].min(axis=0)
    hi = d.outline.max(axis=0) if len(d.outline) >= 3 else pose_a[:, :2].max(axis=0)
    cx_lo, cx_hi = _legal_xy(
        d,
        np.asarray(lo, float) + FAB_QUANTUM_MM,
        np.asarray(hi, float) - FAB_QUANTUM_MM,
    )
    board = board_polygon(d)
    if board is not None:
        board = board.buffer(-FAB_QUANTUM_MM)

    n_zones = sum(1 for z in d.zones if len(z.ring) >= 4)
    part_nets = [_part_nets(d, i) for i in range(n)] if n_zones else []
    pin_names: dict[int, str] | None = None
    moved_mm = 0.0
    reason = ""
    rounds_done = 0
    reroute: set[int] = set()
    # the result is diffed against this snapshot at the end — moves a
    # guard later reverted (sharp-turn drop, via re-certify failure)
    # never reach the caller
    via0 = {
        (bi, int(v.i)): (float(v.x), float(v.y))
        for bi, b in enumerate(wires.branches)
        for v in b.vias
    }
    t0_map = {
        (bi, pos): float(b.t[pos])
        for bi, b in enumerate(wires.branches)
        for pos in range(min(len(b.t), len(b.edges)))
    }
    parts_moved = False
    banned: set[int] = set()  # branches whose moves a guard reverted
    pad_index: tuple[Any, list[Any], list[int]] | None = None
    pad_index_done = False

    for _round in range(rounds):
        rounds_done = _round + 1
        sides = pose_a[:, 3] >= 0.5
        th = through_hole_parts(d)
        geoms = [_ring_geom(d, pose_a[i], i) for i in range(n)]
        zone_geoms = [
            (
                z_i,
                Polygon(
                    np.asarray(zone_world_ring(d, z, pose_a), dtype=np.float64)[:-1]
                ),
            )
            for z_i, z in enumerate(d.zones)
            if len(z.ring) >= 4
        ]
        body_geoms = (
            [_body_geom(d, pose_a[i], i) for i in range(n)]
            if any(z.kind == "mounting_hole" for z in d.zones)
            else geoms
        )
        pads_of_cache: dict[int, Any] = {}

        # ── census, rebuilt each round at the current positions ──
        via_list: list[tuple[int, int, _wires.PlantedVia]] = []
        groups: dict[tuple[int, int], list[tuple[int, int, float]]] = {}
        for bi, b in enumerate(wires.branches):
            for v in b.vias:
                via_list.append((bi, int(v.i), v))
            hop = {int(v.i) for v in b.vias}
            if not len(b.edges):
                continue
            lays = b.layers if len(b.layers) == len(b.edges) + 1 else ()
            for pos, e in enumerate(b.edges):
                if pos in hop:
                    continue
                ly = int(lays[pos]) if lays else 0
                t = float(b.t[pos]) if pos < len(b.t) else 0.5
                # endpoint-tied crossings sit ON a mesh vertex — the path
                # turns there, so they cannot slide along the edge
                # (_spoke_offsets' spread excludes them the same way)
                if not 1e-9 < t < 1.0 - 1e-9:
                    continue
                groups.setdefault((int(e), ly), []).append((bi, pos, t))
        contested = {
            k: sorted(v, key=lambda h: h[2]) for k, v in groups.items() if len(v) >= 2
        }
        off_list = [
            (bi, pos, e, ly)
            for (e, ly), hits in contested.items()
            for bi, pos, _t in hits
        ]
        # foreign wire bodies for via keepouts — current geometry
        wire_bodies: list[tuple[Any, float, int, float]] = []
        for b in wires.branches:
            if len(b.points) < 2:
                continue
            w_k, c_k = _dims(int(b.net))
            wire_bodies.append(
                (
                    LineString([tuple(p) for p in b.points]),
                    w_k / 2.0,
                    int(b.net),
                    c_k,
                )
            )

        # index layout: [part δ 2m][part u 2m][via δ 2V][via u 2V]
        #              [off δ O][off u O][slack_route][slack_part]
        nv = len(via_list)
        no = len(off_list)
        i_vd = 4 * m_count
        i_vu = i_vd + 2 * nv
        i_od = i_vu + 2 * nv
        i_ou = i_od + no
        n_var = i_ou + no

        def vidx(j: int, axis: int, _i: int = i_vd) -> int:
            return _i + 2 * j + axis

        def uvidx(j: int, axis: int, _i: int = i_vu) -> int:
            return _i + 2 * j + axis

        def oidx(k: int, _i: int = i_od) -> int:
            return _i + k

        def uoidx(k: int, _i: int = i_ou) -> int:
            return _i + k

        route_rows: list[tuple[list[tuple[int, float]], float]] = []
        # part rows carry a 2-vector coefficient per part (x and y move
        # together along the separation axis) — a different row shape
        # than the scalar route/tri rows
        part_rows: list[tuple[list[tuple[int, Any]], float]] = []

        # ── via keepout rows ──
        for j, (bi, vi, v) in enumerate(via_list):
            b = wires.branches[bi]
            net = int(b.net)
            _w_k, c_k = _dims(net)
            r_site = float(v.r_site_mm)
            p0 = (float(v.x), float(v.y))
            px, py = p0
            pad_gap = max(c_k, float(planter.mask_web)) if planter is not None else c_k
            cand = np.asarray([Point(px, py)], dtype=object)
            drill = float(v.drill_mm or 0.0)
            # pads — the planter's own index, own-pad mask-web rule
            if planter is not None and planter.pad_ix._tree is not None:
                idxs = planter.pad_ix._tree.query(
                    cand,
                    predicate="dwithin",
                    distance=r_site + pad_gap + planter.pad_ix._reach + FAB_QUANTUM_MM,
                )[0]
                for biq in idxs:
                    biq = int(biq)
                    geom = planter.pad_ix._geoms[biq]
                    rr = float(planter.pad_ix._r[biq])
                    cb = float(planter.pad_ix._c[biq])
                    bn = int(planter.pad_ix._net[biq])
                    own = bn == net and net != NO_OWN_NET
                    web = float(planter.mask_web)
                    req = (
                        r_site
                        + rr
                        + (max(web, cb) if own else max(pad_gap, cb))
                        + FAB_QUANTUM_MM
                    )
                    q = _closest_on_geom(p0, geom)
                    nx, ny = _normal(p0, q)
                    if nx == 0.0 and ny == 0.0:
                        continue
                    row = [(vidx(j, 0), -nx), (vidx(j, 1), -ny)]
                    # a pad owned by a movable part moves with it
                    part = _pad_body_part(planter, biq, d)
                    ou = var_of[part] if part >= 0 else -1
                    if ou >= 0:
                        row.append((ou, nx))
                        row.append((m_count + ou, ny))
                    rhs = nx * (px - q[0]) + ny * (py - q[1]) - req
                    route_rows.append((row, rhs))
            # env bodies — zones, outline; own-net fixed copper exempt
            if planter is not None and planter.env_ix._tree is not None:
                idxs = planter.env_ix._tree.query(
                    cand,
                    predicate="dwithin",
                    distance=r_site + c_k + planter.env_ix._reach + FAB_QUANTUM_MM,
                )[0]
                for biq in idxs:
                    biq = int(biq)
                    geom = planter.env_ix._geoms[biq]
                    rr = float(planter.env_ix._r[biq])
                    cb = float(planter.env_ix._c[biq])
                    bn = int(planter.env_ix._net[biq])
                    if bn != ANY_NET and bn == net:
                        continue
                    req = r_site + rr + max(c_k, cb) + FAB_QUANTUM_MM
                    q = _closest_on_geom(p0, geom)
                    nx, ny = _normal(p0, q)
                    if nx == 0.0 and ny == 0.0:
                        continue
                    row = [(vidx(j, 0), -nx), (vidx(j, 1), -ny)]
                    rhs = nx * (px - q[0]) + ny * (py - q[1]) - req
                    route_rows.append((row, rhs))
            # drill holes — physical barrels, every net foreign
            if (
                planter is not None
                and planter.hole_ix._tree is not None
                and (drill or planter.default_drill_mm) > 0
            ):
                dd_ = drill or planter.default_drill_mm
                idxs = planter.hole_ix._tree.query(
                    cand,
                    predicate="dwithin",
                    distance=dd_ / 2.0 + planter.hole_ix._reach + FAB_QUANTUM_MM,
                )[0]
                for biq in idxs:
                    biq = int(biq)
                    geom = planter.hole_ix._geoms[biq]
                    rr = float(planter.hole_ix._r[biq])
                    cb = float(planter.hole_ix._c[biq])
                    req = dd_ / 2.0 + rr + max(0.0, cb) + FAB_QUANTUM_MM
                    q = _closest_on_geom(p0, geom)
                    nx, ny = _normal(p0, q)
                    if nx == 0.0 and ny == 0.0:
                        continue
                    row = [(vidx(j, 0), -nx), (vidx(j, 1), -ny)]
                    rhs = nx * (px - q[0]) + ny * (py - q[1]) - req
                    route_rows.append((row, rhs))
            # other planted vias — pairwise, both moving
            for j2 in range(j + 1, nv):
                bi2, _vi2, v2 = via_list[j2]
                b2 = wires.branches[bi2]
                if int(b2.net) == net:
                    continue
                qx, qy = float(v2.x), float(v2.y)
                dist = math.hypot(px - qx, py - qy)
                req = (
                    r_site
                    + float(v2.r_site_mm)
                    + max(c_k, _dims(int(b2.net))[1])
                    + FAB_QUANTUM_MM
                )
                if dist > req + _CAP_MM + 1.0 or dist <= _EPS:
                    continue
                nx, ny = (px - qx) / dist, (py - qy) / dist
                # n̂·(p_j − p_j2) ≥ req → -n̂·δ_j + n̂·δ_j2 ≤ n̂·(p0j−p0j2) − req
                route_rows.append(
                    (
                        [
                            (vidx(j, 0), -nx),
                            (vidx(j, 1), -ny),
                            (vidx(j2, 0), nx),
                            (vidx(j2, 1), ny),
                        ],
                        nx * (px - qx) + ny * (py - qy) - req,
                    )
                )
            # foreign wire segments — static at current geometry
            for geom, wr, wn, wc in wire_bodies:
                if wn == net:
                    continue
                gb = geom.bounds
                reach = r_site + wr + max(c_k, wc) + FAB_QUANTUM_MM + _CAP_MM
                if (
                    px < gb[0] - reach
                    or px > gb[2] + reach
                    or py < gb[1] - reach
                    or py > gb[3] + reach
                ):
                    continue
                q = _closest_on_geom(p0, geom)
                dd_ = math.hypot(px - q[0], py - q[1])
                req = r_site + wr + max(c_k, wc) + FAB_QUANTUM_MM
                if dd_ > req + _CAP_MM + 1.0 or dd_ <= _EPS:
                    continue
                nx, ny = _normal(p0, q)
                row = [(vidx(j, 0), -nx), (vidx(j, 1), -ny)]
                rhs = nx * (px - q[0]) + ny * (py - q[1]) - req
                route_rows.append((row, rhs))

        # ── cut rows per contested (edge, layer) ──
        oidx_of = {(bi, pos, e, ly): k for k, (bi, pos, e, ly) in enumerate(off_list)}
        for (e, ly), hits in sorted(contested.items()):
            if ctx is None:
                continue
            seglen = float(ctx.seg_len[e])
            if seglen <= _EPS:
                continue
            eu, ev = ctx.eu[e], ctx.ev[e]
            ex, ey = (ev[0] - eu[0]) / seglen, (ev[1] - eu[1]) / seglen
            # via covers on this edge (mm along u→v)
            vcovers: list[tuple[float, float]] = []
            vcover_var: list[int] = []
            for j, (_bi, _vi, v) in enumerate(via_list):
                vx, vy = float(v.x), float(v.y)
                ddx, ddy = ev[0] - eu[0], ev[1] - eu[1]
                tpar = min(
                    1.0,
                    max(
                        0.0,
                        ((vx - eu[0]) * ddx + (vy - eu[1]) * ddy)
                        / max(seglen * seglen, _EPS),
                    ),
                )
                cx, cy = eu[0] + tpar * ddx, eu[1] + tpar * ddy
                dist = math.hypot(vx - cx, vy - cy)
                reach = float(v.r_site_mm) + 4.0
                if dist < reach:
                    half = math.sqrt(max(reach * reach - dist * dist, 0.0))
                    vcovers.append((tpar * seglen - half, tpar * seglen + half))
                    vcover_var.append(j)
            intervals = _free_intervals(ctx, e, ly, r_ref, seglen, vcovers)
            if not intervals:
                for bi, _pos, _t in hits:
                    reroute.add(int(wires.branches[bi].net))
                continue
            ss = [float(t) * seglen for _b, _p, t in hits]
            med = float(np.median(ss))
            iv = min(
                intervals,
                key=lambda ab: (
                    0.0
                    if ab[0] - 1e-9 <= med <= ab[1] + 1e-9
                    else min(abs(med - ab[0]), abs(med - ab[1]))
                ),
            )
            A0, B0 = iv
            # the covers bounding each end → their owners move the bound
            ints: list[tuple[float, float, int]] = sorted(
                (a * seglen, b * seglen, int(q))
                for a, b, q in ctx.cover_cache.get(r_ref, {}).get(e, ())
                if int(ctx.pad_copper[q]) & (1 << ly)
            )
            left_owner: tuple[str, int] | None = None
            left_b = -math.inf
            right_owner: tuple[str, int] | None = None
            right_a = math.inf
            for ilo, ihi, qp in ints:
                if ihi <= A0 + 1e-6 and ihi > left_b:
                    pin = int(ctx.pad_pin[qp])
                    part = int(d.pin_part[pin]) if 0 <= pin < len(d.pin_part) else -1
                    left_owner = ("part", var_of[part] if part >= 0 else -1)
                    left_b = ihi
                if ilo >= B0 - 1e-6 and ilo < right_a:
                    pin = int(ctx.pad_pin[qp])
                    part = int(d.pin_part[pin]) if 0 <= pin < len(d.pin_part) else -1
                    right_owner = ("part", var_of[part] if part >= 0 else -1)
                    right_a = ilo
            for (ca, cb_), jw in zip(vcovers, vcover_var, strict=True):
                if cb_ <= A0 + 1e-6 and cb_ > left_b:
                    left_owner = ("via", jw)
                    left_b = cb_
                if ca >= B0 - 1e-6 and ca < right_a:
                    right_owner = ("via", jw)
                    right_a = ca
            order_idx = [oidx_of[(bi, pos, e, ly)] for bi, pos, _t in hits]
            wcs = [_dims(int(wires.branches[bi].net)) for bi, _p, _t in hits]
            s0 = [float(t) * seglen for _b, _p, t in hits]
            margins = [w / 2.0 + c - (finest[0] / 2.0 + finest[1]) for w, c in wcs]
            for k in range(len(order_idx) - 1):
                w1, c1 = wcs[k]
                w2, c2 = wcs[k + 1]
                slot = (w1 + w2) / 2.0 + max(c1, c2) + FAB_QUANTUM_MM
                j1, j2 = order_idx[k], order_idx[k + 1]
                # δ_j1 − δ_j2 ≤ s0_{k+1} − s0_k − slot
                route_rows.append(
                    ([(oidx(j1), 1.0), (oidx(j2), -1.0)], s0[k + 1] - s0[k] - slot)
                )
            for side, oi, margin, owner, bound0, s0v in (
                (0, order_idx[0], margins[0], left_owner, A0, s0[0]),
                (1, order_idx[-1], margins[-1], right_owner, B0, s0[-1]),
            ):
                bcoef: list[tuple[int, float]] = []
                if owner is not None:
                    kind, oi2 = owner
                    if kind == "part" and oi2 is not None and oi2 >= 0:
                        bcoef = [(oi2, ex), (m_count + oi2, ey)]
                    elif kind == "via" and oi2 is not None and 0 <= oi2 < nv:
                        bcoef = [(vidx(oi2, 0), ex), (vidx(oi2, 1), ey)]
                if side == 0:
                    # s0 + δ ≥ bound0 + d(bound0) + m  →  -δ + d·δobs ≤ s0-bound0-m
                    row = [(oidx(oi), -1.0)] + [(ix, cf) for ix, cf in bcoef]
                    rhs = s0v - bound0 - margin
                else:
                    # s0 + δ ≤ bound0 + d(bound0) - m  →  δ - d·δobs ≤ bound0-m-s0
                    row = [(oidx(oi), 1.0)] + [(ix, -cf) for ix, cf in bcoef]
                    rhs = bound0 - margin - s0v
                route_rows.append((row, rhs))

        # ── corridor-overflow capacity rows ──
        # A routed CorridorCut's demand tops its capacity: grow the
        # edge's free stretch by the deficit — the pad covers bounding
        # it belong to parts whose translations move the bounds apart
        # (cleanup-66: the overflow is a legalise input, not a refusal;
        # the caller re-checks the rebuilt cut before it may raise).
        # The corridor's own measure (``_pad_eval``/``gap_cap``) reads
        # pad covers only — via covers are NOT its bound, so they stay
        # out of the interval computation here.
        slot_mm = finest[0] + finest[1] + FAB_QUANTUM_MM
        for cut in wires.overflow:
            if cut.refused or ctx is None:
                continue
            e = int(cut.edge)
            if e >= len(ctx.seg_len):
                continue
            seglen = float(ctx.seg_len[e])
            if seglen <= _EPS:
                continue
            eu, ev = ctx.eu[e], ctx.ev[e]
            ddx, ddy = ev[0] - eu[0], ev[1] - eu[1]
            ex, ey = ddx / seglen, ddy / seglen
            # per-layer pad covers (mm along u→v) and free stretches —
            # the binding layer is the one with the least free room,
            # INCLUDING a fully covered layer (capacity 0 there)
            cover_ly: dict[int, list[tuple[float, float, int]]] = {}
            best_ly = -1
            best_fl = math.inf
            best_ivs: list[tuple[float, float]] = []
            for ly in range(n_layers):
                ints_ly = sorted(
                    (a * seglen, b * seglen, int(q))
                    for a, b, q in ctx.cover_cache.get(r_ref, {}).get(e, ())
                    if int(ctx.pad_copper[q]) & (1 << ly)
                )
                cover_ly[ly] = ints_ly
                ivs = _free_intervals(ctx, e, ly, r_ref, seglen, [])
                # the corridor's own measure (``_pad_eval``) is the
                # TOTAL uncovered length per layer — fragmented free
                # space counts the same as one stretch
                fl = sum(b_ - a_ for a_, b_ in ivs)
                if fl < best_fl:
                    best_fl, best_ly, best_ivs = fl, ly, ivs
            if best_ly < 0:
                continue
            needed = max(cut.demand - cut.cap, 0.0) * slot_mm + FAB_QUANTUM_MM

            def _part_of(q: int) -> int:
                pin = int(ctx.pad_pin[q])
                return int(d.pin_part[pin]) if 0 <= pin < len(d.pin_part) else -1

            def _coeffs(
                p_idx: int,
                sgn: float,
                *,
                _ex: float = ex,
                _ey: float = ey,
                _var_of: np.ndarray = var_of,
                _m_count: int = m_count,
            ) -> list[tuple[int, float]]:
                u = _var_of[p_idx] if p_idx >= 0 else -1
                if not 0 <= u < _m_count:
                    return []
                return [(u, sgn * _ex), (_m_count + u, sgn * _ey)]

            cap_row: list[tuple[int, float]] = []
            if best_ivs:
                # grow the widest free stretch: its two bounding covers
                # move apart — right bound +û, left bound −û (rows are
                # Ax <= b, so the gain enters negated). A side bounded
                # by the edge end itself cannot extend.
                iv = max(best_ivs, key=lambda ab: ab[1] - ab[0])
                A0, B0 = iv
                lo_own = ro_own = -1
                lo_b = -math.inf
                ro_a = math.inf
                for ilo, ihi, qp in cover_ly[best_ly]:
                    if ihi <= A0 + 1e-6 and ihi > lo_b:
                        lo_own, lo_b = _part_of(qp), ihi
                    if ilo >= B0 - 1e-6 and ilo < ro_a:
                        ro_own, ro_a = _part_of(qp), ilo
                if A0 > 1e-6:
                    cap_row += _coeffs(lo_own, 1.0)
                if seglen - 1e-6 > B0:
                    cap_row += _coeffs(ro_own, -1.0)
            else:
                # the layer is fully covered — free an edge end by
                # retreating the outermost cover inward. +û motion
                # frees the eu end, −û the ev end; pick the direction
                # carrying the cover's owner AWAY from the cut's other
                # named part.
                ints_b = cover_ly[best_ly]
                if ints_b:
                    refdes = {str(r): i for i, r in enumerate(d.parts)}

                    def _score(
                        p_idx: int,
                        sgn: float,
                        *,
                        _ex: float = ex,
                        _ey: float = ey,
                        _pose_a: np.ndarray = pose_a,
                        _refdes: dict[str, int] = refdes,
                        _owners: tuple[str, ...] = cut.owners,
                    ) -> float:
                        if p_idx < 0:
                            return -math.inf
                        others = [
                            _refdes[o]
                            for o in _owners
                            if o in _refdes and _refdes[o] != p_idx
                        ]
                        if not others:
                            return 0.0
                        q = min(others)
                        return sgn * (
                            _ex * (float(_pose_a[p_idx, 0]) - float(_pose_a[q, 0]))
                            + _ey * (float(_pose_a[p_idx, 1]) - float(_pose_a[q, 1]))
                        )

                    cL = min(ints_b, key=lambda t: t[0])
                    cR = max(ints_b, key=lambda t: t[1])
                    pL, pR = _part_of(cL[2]), _part_of(cR[2])
                    sL = _score(pL, 1.0)  # +û motion frees the eu end
                    sR = _score(pR, -1.0)  # −û motion frees the ev end
                    if sL >= sR:
                        cap_row = _coeffs(pL, -1.0)
                    else:
                        cap_row = _coeffs(pR, 1.0)
            if cap_row:
                route_rows.append((cap_row, -needed))

        # ── part separation / zone rows (shove_pose's drivers) ──
        pair_rigid = rigid_pairs(d)
        pair_gap_free = one_body_pairs(d)
        req = float(d.silk.pair_req_mm) + FAB_QUANTUM_MM
        for i in range(n):
            gi = geoms[i]
            if gi is None:
                continue
            x0, y0, x1, y1 = gi.bounds
            for j in range(i + 1, n):
                gj = geoms[j]
                if gj is None or (sides[i] != sides[j] and not (th[i] or th[j])):
                    continue
                pair = (i, j)
                if pair in pair_rigid or (
                    len(d.group_of) and 0 <= int(d.group_of[i]) == int(d.group_of[j])
                ):
                    continue
                ri = 0.0 if pair in pair_gap_free else req
                bx0, by0, bx1, by1 = gj.bounds
                if x1 + ri < bx0 or bx1 + ri < x0 or y1 + ri < by0 or by1 + ri < y0:
                    continue
                if var_of[i] < 0 and var_of[j] < 0:
                    continue
                dist = float(gi.distance(gj))
                if dist > _EPS:
                    short = ri - dist
                    if short <= _EPS:
                        continue
                    pa, pb = nearest_points(gi, gj)
                    axis = np.asarray([pb.x - pa.x, pb.y - pa.y], dtype=np.float64)
                    axis = axis / float(np.hypot(*axis))
                    part_rows.append(([(i, axis), (j, -axis)], -(short + _CLR_MM)))
                    continue
                if float(gi.intersection(gj).area) <= _EPS:
                    if ri <= _EPS:
                        continue
                    ax_c = np.asarray(gj.centroid.coords[0]) - np.asarray(
                        gi.centroid.coords[0]
                    )
                    al = float(np.hypot(*ax_c))
                    if al <= 1e-9:
                        continue
                    part_rows.append(
                        ([(i, ax_c / al), (j, -ax_c / al)], -(ri + _CLR_MM))
                    )
                    continue
                vec, depth = _mtv(
                    np.asarray(gi.exterior.coords), np.asarray(gj.exterior.coords)
                )
                if depth > 0.0:
                    axis = vec / depth
                    part_rows.append(([(i, axis), (j, -axis)], -(depth + ri + _CLR_MM)))
            if zone_geoms:
                for z_i, zg in zone_geoms:
                    z = d.zones[z_i]
                    if z.part == i or not _zone_blocks_part(
                        z, bool(sides[i]), bool(th[i]), n_layers
                    ):
                        continue
                    barrier = zg.buffer(FAB_QUANTUM_MM)
                    bx0, by0, bx1, by1 = barrier.bounds
                    if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                        continue
                    gi_z = body_geoms[i] if z.kind == "mounting_hole" else gi
                    if gi_z is None or var_of[i] < 0:
                        continue
                    circ = (
                        zone_world_circle(d, z, pose_a)
                        if z.kind == "mounting_hole"
                        else None
                    )
                    if circ is not None:
                        cpt = Point(circ[0], circ[1])
                        dist = float(gi_z.distance(cpt))
                        if dist >= circ[2] + FAB_QUANTUM_MM:
                            continue
                        depth = circ[2] + FAB_QUANTUM_MM - dist
                        if dist > _EPS:
                            near = nearest_points(gi_z, cpt)[0]
                            axis = (
                                np.asarray([near.x - circ[0], near.y - circ[1]]) / dist
                            )
                        else:
                            vec0, d0 = _mtv(
                                np.asarray(zg.exterior.coords),
                                np.asarray(gi_z.exterior.coords),
                            )
                            axis = vec0 / d0 if d0 > 0.0 else np.asarray([1.0, 0.0])
                        part_rows.append(([(i, -axis)], -(depth + _CLR_MM)))
                        continue
                    if float(gi_z.distance(zg)) >= FAB_QUANTUM_MM:
                        continue
                    if z.kind == "fixed_via":
                        if pin_names is None:
                            pin_names = _pin_net_names(d)
                        if i not in pads_of_cache:
                            pads_of_cache[i] = _pad_polys(d, pose_a[i], i)
                        if _via_zone_exempt(
                            d, pose_a[i], i, z, zg, pin_names, pads_of_cache[i]
                        ):
                            continue
                    elif z.kind == "fixed_copper" and z.net and z.net in part_nets[i]:
                        continue
                    vec, depth = _mtv(
                        np.asarray(barrier.exterior.coords),
                        np.asarray(gi_z.exterior.coords),
                    )
                    if depth > 0.0:
                        part_rows.append(([(i, -vec / depth)], -(depth + _CLR_MM)))

        # ── triangle signed-area guard (hard) ──
        tri_owner = _vert_owner(d, mesh)
        cur = mesh.verts
        tri_rows: list[tuple[list[tuple[int, float]], float]] = []
        for va, vb, vc in mesh.tris:
            xa, ya = cur[int(va)]
            xb, yb = cur[int(vb)]
            xc, yc = cur[int(vc)]
            area = 0.5 * (xa * (yb - yc) + xb * (yc - ya) + xc * (ya - yb))
            if area <= _TRI_FLOOR_MM2:
                continue
            floor = min(_TRI_FLOOR_MM2, 0.9 * area)
            grads = {
                int(va): ((yb - yc) / 2.0, (xc - xb) / 2.0),
                int(vb): ((yc - ya) / 2.0, (xa - xc) / 2.0),
                int(vc): ((ya - yb) / 2.0, (xb - xa) / 2.0),
            }
            trow: list[tuple[int, float]] = []
            for gv, (gx, gy) in grads.items():
                o = int(tri_owner[gv])
                if o >= 0 and var_of[o] >= 0:
                    trow.append((var_of[o], -gx))
                    trow.append((m_count + var_of[o], -gy))
            if trow:
                tri_rows.append((trow, area - floor))

        # ── via triangle-pair hull rows (hard) ──
        via_hull: list[list[tuple[float, float, float]]] = []
        for j, (bi, vi, v) in enumerate(via_list):
            b = wires.branches[bi]
            e = int(b.edges[vi]) if vi < len(b.edges) else -1
            hull = _tri_pair_hull(mesh, e) if e >= 0 else []
            via_hull.append(_hull_rows(hull) if hull else [])

        # ── assemble ──
        n_route = len(route_rows)
        n_part = len(part_rows)
        n_tri = len(tri_rows)
        n_hull = sum(len(h) for h in via_hull)
        n_epi_specs = 2 * m_count + 2 * nv + no
        n_row = n_route + n_part + n_tri + n_hull + 2 * n_epi_specs
        n_col = n_var + n_route + n_part
        A = lil_matrix((n_row, n_col))
        bb = np.zeros(n_row)
        rr = 0
        for row, rhs in route_rows:
            for ix, cf in row:
                A[rr, ix] += cf
            A[rr, n_var + rr] = -1.0
            bb[rr] = rhs
            rr += 1
        for prow, prhs in part_rows:
            for part, coeff in prow:
                if var_of[part] >= 0:
                    A[rr, var_of[part]] += coeff[0]
                    A[rr, m_count + var_of[part]] += coeff[1]
            A[rr, n_var + n_route + (rr - n_route)] = -1.0
            bb[rr] = prhs
            rr += 1
        for row, rhs in tri_rows:
            for ix, cf in row:
                A[rr, ix] += cf
            bb[rr] = rhs
            rr += 1
        for j, rows in enumerate(via_hull):
            p0x = float(via_list[j][2].x)
            p0y = float(via_list[j][2].y)
            for nx, ny, c in rows:
                A[rr, vidx(j, 0)] = nx
                A[rr, vidx(j, 1)] = ny
                bb[rr] = c - (nx * p0x + ny * p0y)
                rr += 1
        epi_specs: list[tuple[int, int]] = []
        for m in range(m_count):
            epi_specs.append((m, 2 * m_count + m))
            epi_specs.append((m_count + m, 3 * m_count + m))
        for j in range(nv):
            epi_specs.append((vidx(j, 0), uvidx(j, 0)))
            epi_specs.append((vidx(j, 1), uvidx(j, 1)))
        for k in range(no):
            epi_specs.append((oidx(k), uoidx(k)))
        for vix, uix in epi_specs:
            A[rr, vix] = 1.0
            A[rr, uix] = -1.0
            bb[rr] = 0.0
            rr += 1
            A[rr, vix] = -1.0
            A[rr, uix] = -1.0
            bb[rr] = 0.0
            rr += 1
        assert rr == n_row, (rr, n_row)

        obj = np.zeros(n_col)
        for m in range(2 * m_count):
            obj[2 * m_count + m] = 1.0
        for j in range(2 * nv):
            obj[i_vu + j] = 1.0
        for k in range(no):
            obj[i_ou + k] = 1.0
        for k in range(n_route):
            obj[n_var + k] = _ROUTE_SLACK_W
        for k in range(n_part):
            obj[n_var + n_route + k] = _SLACK_W

        # bounds — order must match the variable layout exactly
        bounds: list[tuple[float, float | None]] = []
        unit_bounds = np.zeros((m_count, 2, 2))
        for m in range(m_count):
            for axis_i in range(2):
                lb = max(
                    (
                        float(cx_lo[p, axis_i]) - float(pose_a[p, axis_i])
                        for p in units[m]
                    ),
                    default=-_CAP_MM,
                )
                ub = min(
                    (
                        float(cx_hi[p, axis_i]) - float(pose_a[p, axis_i])
                        for p in units[m]
                    ),
                    default=_CAP_MM,
                )
                lb = max(lb, -_CAP_MM)
                ub = min(ub, _CAP_MM)
                if lb > ub:
                    lb = ub = 0.0
                unit_bounds[m, axis_i] = (lb, ub)
        for m in range(m_count):
            bounds.append(tuple(unit_bounds[m, 0]))
        for m in range(m_count):
            bounds.append(tuple(unit_bounds[m, 1]))
        for _ in range(2 * m_count):
            bounds.append((0.0, _CAP_MM))
        for j, (bj, _vi, _v) in enumerate(via_list):
            if bj in banned:
                bounds.append((0.0, 0.0))
                bounds.append((0.0, 0.0))
            else:
                bounds.append((-_CAP_MM, _CAP_MM))
                bounds.append((-_CAP_MM, _CAP_MM))
        for _ in range(nv):
            bounds.append((0.0, _CAP_MM))
            bounds.append((0.0, _CAP_MM))
        for k, (bj, _pos, _e, _ly) in enumerate(off_list):
            if bj in banned:
                bounds.append((0.0, 0.0))
            else:
                bounds.append((-_CAP_MM, _CAP_MM))
        for _ in range(no):
            bounds.append((0.0, _CAP_MM))
        for _ in range(n_route + n_part):
            bounds.append((0.0, None))
        assert len(bounds) == n_col, (len(bounds), n_col)
        if n_col == 0:
            break  # nothing movable and no rows — nothing to solve

        res = linprog(obj, A_ub=csr_matrix(A), b_ub=bb, bounds=bounds, method="highs")
        if res.status != 0:
            reason = reason or "infeasible"
            break
        x = res.x
        if float(np.abs(x[:n_var]).sum()) <= 1e-9:
            break

        # ── apply parts ──
        pstep = np.zeros((m_count, 2))
        for m in range(m_count):
            pstep[m] = (x[m], x[m_count + m])
        new_pose = pose_a.copy()
        for m in range(m_count):
            for p in units[m]:
                new_pose[p, 0] += pstep[m, 0]
                new_pose[p, 1] += pstep[m, 1]
        part_ok = True
        try:
            build_mesh(d, new_pose)
        except MeshConflictError:
            scale = 0.5
            part_ok = False
            for _try in range(5):
                cand = pose_a.copy()
                for m in range(m_count):
                    for p in units[m]:
                        cand[p, 0] += pstep[m, 0] * scale
                        cand[p, 1] += pstep[m, 1] * scale
                try:
                    build_mesh(d, cand)
                    new_pose = cand
                    part_ok = True
                    break
                except MeshConflictError:
                    scale *= 0.5
        if part_ok and float(np.abs(pstep).sum()) > 1e-9:
            moved_mm += float(np.abs(pstep).sum())
            pose_a = new_pose
            parts_moved = True

        # ── apply via moves (clamped into the triangle pair) ──
        round_vias: dict[tuple[int, int], tuple[float, float]] = {}
        for j, (bi, vi, v) in enumerate(via_list):
            dx = float(x[vidx(j, 0)])
            dy = float(x[vidx(j, 1)])
            if abs(dx) <= 1e-9 and abs(dy) <= 1e-9:
                continue
            b = wires.branches[bi]
            e = int(b.edges[vi]) if vi < len(b.edges) else -1
            nx_, ny_ = float(v.x) + dx, float(v.y) + dy
            ok_pos = False
            if e >= 0:
                tris = mesh.edge_tris[e] if 0 <= e < len(mesh.edge_tris) else (-1, -1)
                for t in tris:
                    t = int(t)
                    if not (0 <= t < len(mesh.tris)):
                        continue
                    a, bb_, c = (mesh.verts[int(vv)] for vv in mesh.tris[t])
                    ar = (
                        (bb_[0] - a[0]) * (ny_ - a[1]) - (bb_[1] - a[1]) * (nx_ - a[0]),
                        (c[0] - bb_[0]) * (ny_ - bb_[1])
                        - (c[1] - bb_[1]) * (nx_ - bb_[0]),
                        (a[0] - c[0]) * (ny_ - c[1]) - (a[1] - c[1]) * (nx_ - c[0]),
                    )
                    sgn = 1.0 if ar[0] >= 0 else -1.0
                    if all(sgn * vv >= -1e-9 for vv in ar):
                        ok_pos = True
                        break
            if not ok_pos and e >= 0 and ctx is not None:
                eu_, ev_ = ctx.eu[e], ctx.ev[e]
                exx, exy = ev_[0] - eu_[0], ev_[1] - eu_[1]
                dd = exx * exx + exy * exy
                if dd > _EPS:
                    tt = min(
                        1.0,
                        max(
                            0.0,
                            ((nx_ - eu_[0]) * exx + (ny_ - eu_[1]) * exy) / dd,
                        ),
                    )
                    nx_, ny_ = eu_[0] + tt * exx, eu_[1] + tt * exy
            round_vias[(bi, vi)] = (nx_, ny_)
            moved_mm += abs(dx) + abs(dy)

        # ── apply offset moves ──
        round_offsets: dict[tuple[int, int], float] = {}
        for k, (bi, pos, e, ly) in enumerate(off_list):
            dv = float(x[oidx(k)])
            if abs(dv) <= 1e-9:
                continue
            b = wires.branches[bi]
            seglen = float(ctx.seg_len[e]) if ctx is not None else 0.0
            t0 = float(b.t[pos]) if pos < len(b.t) else 0.5
            s_new = t0 * seglen + dv
            t_new = min(1.0, max(0.0, s_new / seglen)) if seglen > _EPS else t0
            round_offsets[(bi, pos)] = t_new
            moved_mm += abs(dv)

        # ── re-certify vias at the input pose's planter ──
        if planter is not None:
            for (bi, vi), (nx_, ny_) in list(round_vias.items()):
                b = wires.branches[bi]
                v = next(vv for vv in b.vias if int(vv.i) == vi)
                net = int(b.net)
                _wk, ck = _dims(net)
                ok = bool(
                    planter.plantable_points(
                        np.asarray([[nx_, ny_]]),
                        float(v.r_site_mm),
                        ck,
                        net,
                        None,
                        drill_mm=float(v.drill_mm or 0.0),
                    )[0]
                )
                if not ok:
                    round_vias.pop((bi, vi), None)
                    reroute.add(net)

        wires_prev = wires
        wires_new = apply_legalise(wires_prev, round_vias, round_offsets, mesh)
        touched = {bi for bi, _vi in round_vias} | {bi for bi, _pos in round_offsets}
        if touched and not parts_moved:
            # re-certify on the real polylines: the LP's linearised rows
            # cannot see the detour legs an offset insert creates, so a
            # move that gained a crossing or pad hit is reverted and its
            # net named for re-route (the branch's vars freeze next round)
            if not pad_index_done:
                pad_index_done = True
                if ctx is not None:
                    try:
                        pin_nets = d.pin_nets()
                        pnets = []
                        for pi_ in range(len(ctx.pad_poly)):
                            pin = int(ctx.pad_pin[pi_])
                            pnets.append(
                                int(pin_nets[pin][1])
                                if 0 <= pin < len(pin_nets)
                                else -1
                            )
                        keep = [
                            i
                            for i, g in enumerate(ctx.pad_poly)
                            if g is not None and not g.is_empty
                        ]
                        pad_index = (
                            STRtree([ctx.pad_poly[i] for i in keep]),
                            [ctx.pad_poly[i] for i in keep],
                            [pnets[i] for i in keep],
                        )
                    except Exception:
                        pad_index = None
            bad = _move_violations(
                wires_prev,
                wires_new,
                touched,
                _dims,
                pad_index,
            )
            if bad:
                for bi in bad:
                    banned.add(bi)
                round_vias = {k: v for k, v in round_vias.items() if k[0] not in bad}
                round_offsets = {
                    k: v for k, v in round_offsets.items() if k[0] not in bad
                }
                wires_new = apply_legalise(wires_prev, round_vias, round_offsets, mesh)
        # ── violated cut rows name their nets — read AFTER the guard's
        # reverts so the positions are the ones actually applied ──
        for (e, ly), hits in contested.items():
            if ctx is None:
                continue
            seglen = float(ctx.seg_len[e])
            if seglen <= _EPS:
                continue
            pos_now = []
            for bi, pos, t in hits:
                tt = round_offsets.get((bi, pos), float(t))
                pos_now.append((tt * seglen, int(wires.branches[bi].net)))
            pos_now.sort()
            for k in range(len(pos_now) - 1):
                net1, net2 = pos_now[k][1], pos_now[k + 1][1]
                w1, c1 = _dims(net1)
                w2, c2 = _dims(net2)
                slot = (w1 + w2) / 2.0 + max(c1, c2) + FAB_QUANTUM_MM
                if pos_now[k + 1][0] - pos_now[k][0] < slot - 1e-6:
                    reroute.add(net1)
                    reroute.add(net2)
        wires = wires_new
        if not round_vias and not round_offsets and not parts_moved:
            break
        if parts_moved:
            # part motion rebuilds the mesh — via/wire geometry is stale;
            # the caller re-routes and legalise runs again on new wires
            break

    if part_sink is not None and parts_moved:
        part_sink.append(pose_a.copy())
    all_vias: dict[tuple[int, int], tuple[float, float]] = {}
    all_offsets: dict[tuple[int, int], float] = {}
    if parts_moved:
        # every via/offset move was linearised against the pre-move part
        # pose — uncertified now. The caller re-routes at ``pose`` and
        # legalise runs again on the fresh wires.
        pass
    else:
        for bi, b in enumerate(wires.branches):
            for v in b.vias:
                key = (bi, int(v.i))
                old = via0.get(key)
                if old is not None and (
                    abs(float(v.x) - old[0]) > 1e-9 or abs(float(v.y) - old[1]) > 1e-9
                ):
                    all_vias[key] = (float(v.x), float(v.y))
            for pos in range(min(len(b.t), len(b.edges))):
                old_t = t0_map.get((bi, pos))
                if old_t is not None and abs(float(b.t[pos]) - old_t) > 1e-9:
                    all_offsets[(bi, pos)] = float(b.t[pos])
    if not reason and reroute:
        reason = "unresolved rows"
    return LegaliseResult(
        pose=pose_a,
        vias=all_vias,
        offsets=all_offsets,
        reroute=frozenset(reroute),
        moved_mm=moved_mm,
        reason=reason,
        rounds=rounds_done,
    )


def _interior_pt(pt: Any, seg: LineString) -> bool:
    """``pt`` strictly inside ``seg`` — not at either endpoint (shared
    vertices are how wires meet legally; only interior contacts count)."""
    cs = list(seg.coords)
    (x1, y1), (x2, y2) = cs[0], cs[-1]
    return (
        math.hypot(pt.x - x1, pt.y - y1) > 1e-6
        and math.hypot(pt.x - x2, pt.y - y2) > 1e-6
    )


def _proper_hit(leg: LineString, other: LineString) -> bool:
    """The two segments cross or overlap — an intersection interior to
    BOTH, or a collinear overlap. Endpoint touches do not count."""
    inter = leg.intersection(other)
    if inter.is_empty:
        return False
    gt = inter.geom_type
    if gt == "LineString":
        return inter.length > 1e-6
    if gt == "MultiLineString":
        return any(g.length > 1e-6 for g in inter.geoms)
    if gt == "Point":
        return _interior_pt(inter, leg) and _interior_pt(inter, other)
    if gt == "MultiPoint":
        return any(
            _interior_pt(pt, leg) and _interior_pt(pt, other) for pt in inter.geoms
        )
    return False


def _seg_index(
    branches: tuple[_wires.WirePath, ...], keep: set[int] | None
) -> tuple[Any, list[Any], list[int]] | None:
    """STRtree over the branches' legs, tagged with net — ``keep``
    restricts to those branch indices (``None`` = all)."""
    geoms: list[Any] = []
    nets: list[int] = []
    for bi, b in enumerate(branches):
        if keep is not None and bi not in keep:
            continue
        if len(b.points) < 2:
            continue
        for p, q in itertools.pairwise(b.points):
            if math.dist(p, q) > 1e-9:
                geoms.append(LineString([tuple(p), tuple(q)]))
                nets.append(int(b.net))
    if not geoms:
        return None
    return STRtree(geoms), geoms, nets


def _move_violations(
    prev: _wires.Wires,
    new: _wires.Wires,
    touched: set[int],
    dims: Any,
    pad_index: tuple[Any, list[Any], list[int]] | None,
) -> dict[int, int]:
    """Per touched branch, how many NEW improper contacts its applied
    geometry has vs ``prev``: proper crossings of foreign wires, overlaps
    of foreign pads, and via barrels against foreign wires. The
    linearised LP rows cannot see the detour legs an offset insert
    creates, so every round's moves are re-certified on the real
    polylines; a branch that gained contacts has its moves reverted and
    its vars frozen next round."""
    # untouched branches' legs are identical in both states — one index
    # serves both scores; touched legs get a per-state index so
    # touched-vs-touched contacts (legs AND via barrels) are seen
    un_idx = _seg_index(
        new.branches, {i for i in range(len(new.branches)) if i not in touched}
    )
    t_prev = _seg_index(prev.branches, touched)
    t_new = _seg_index(new.branches, touched)

    def _hits(
        idx: tuple[Any, list[Any], list[int]] | None,
        geom: Any,
        net: int,
        proper: bool,
    ) -> int:
        if idx is None:
            return 0
        tree, geoms, nets = idx
        sc = 0
        for k in tree.query(geom, predicate="intersects"):
            k = int(k)
            if nets[k] == net:
                continue
            if proper:
                if _proper_hit(geom, geoms[k]):
                    sc += 1
            elif geom.intersection(geoms[k]).length > 1e-6:
                sc += 1
        return sc

    def score(
        branches: tuple[_wires.WirePath, ...],
        bi: int,
        tidx: tuple[Any, list[Any], list[int]] | None,
    ) -> int:
        b = branches[bi]
        net = int(b.net)
        sc = 0
        for p, q in itertools.pairwise(b.points):
            if math.dist(p, q) <= 1e-9:
                continue
            leg = LineString([tuple(p), tuple(q)])
            sc += _hits(un_idx, leg, net, True)
            sc += _hits(tidx, leg, net, True)
            if pad_index is not None:
                ptree, pgeoms, pnets = pad_index
                for k in ptree.query(leg, predicate="intersects"):
                    k = int(k)
                    if pnets[k] == net:
                        continue
                    if leg.intersection(pgeoms[k]).length > 1e-6:
                        sc += 1
        return sc

    def via_score(
        branches: tuple[_wires.WirePath, ...],
        bi: int,
        tidx: tuple[Any, list[Any], list[int]] | None,
    ) -> int:
        b = branches[bi]
        net = int(b.net)
        _w_k, c_k = dims(net)
        sc = 0
        for v in b.vias:
            vx, vy = float(v.x), float(v.y)
            probe = Point(vx, vy).buffer(float(v.r_site_mm) + 2.0)
            for idx in (un_idx, tidx):
                if idx is None:
                    continue
                tree, geoms, nets = idx
                for k in tree.query(probe, predicate="intersects"):
                    k = int(k)
                    if nets[k] == net:
                        continue
                    g = geoms[k]
                    dist = float(g.distance(Point(vx, vy)))
                    _w2, c2 = dims(nets[k])
                    req = float(v.r_site_mm) + _w2 / 2.0 + max(c_k, c2) + FAB_QUANTUM_MM
                    if dist < req:
                        sc += 1
        return sc

    out: dict[int, int] = {}
    for bi in touched:
        if bi >= len(prev.branches) or bi >= len(new.branches):
            continue
        before = score(prev.branches, bi, t_prev) + via_score(prev.branches, bi, t_prev)
        after = score(new.branches, bi, t_new) + via_score(new.branches, bi, t_new)
        if after > before:
            out[bi] = after - before
    return out


def _pad_body_part(planter: Any, pad_ix: int, d: TopoDesign) -> int:
    """The part owning pad-index ``pad_ix`` of the planter's pad index —
    ``-1`` (static bound) when the planter did not record ownership."""
    parts = getattr(planter, "pad_part", None)
    if parts is not None and 0 <= pad_ix < len(parts):
        return int(parts[pad_ix])
    return -1


def apply_legalise(
    wires: _wires.Wires,
    vias: Mapping[tuple[int, int], tuple[float, float]],
    offsets: Mapping[tuple[int, int], float],
    mesh: TopoMesh | None = None,
) -> _wires.Wires:
    """Rewrite ``wires.branches`` with moved via centres and crossing
    offsets — homotopy preserved (vias stay in their triangle pair,
    offsets stay on their edge). ``mesh`` resolves offset edge
    geometry; required when ``offsets`` is non-empty."""
    if not vias and not offsets:
        return wires
    new_branches = []
    for bi, b in enumerate(wires.branches):
        pts: list[tuple[float, float]] = [(float(p[0]), float(p[1])) for p in b.points]
        t_list = list(b.t)
        via_list = [dataclasses.replace(v) for v in b.vias]
        dirty = False
        for (bj, vi), (nx_, ny_) in vias.items():
            if bj != bi:
                continue
            for k, v in enumerate(via_list):
                if int(v.i) != vi:
                    continue
                via_list[k] = dataclasses.replace(v, x=nx_, y=ny_)
                if 0 <= v.k < len(pts):
                    pts[v.k] = (nx_, ny_)
                dirty = True
        # offsets: collect (leg, frac, pos, point) inserts per branch
        inserts: dict[int, list[tuple[float, int, tuple[float, float]]]] = {}
        t_old_of: dict[int, float] = {}
        for (bj, pos), t_new in offsets.items():
            if bj != bi or pos >= len(b.edges):
                continue
            if pos in {int(v.i) for v in via_list}:
                continue
            e = int(b.edges[pos])
            if mesh is None:
                continue
            t_old = float(t_list[pos]) if pos < len(t_list) else 0.5
            q = edge_xy(mesh, e, t_old)
            t_new = float(t_new)
            if abs(t_new - t_old) <= 1e-12:
                continue
            new_pt = edge_xy(mesh, e, t_new)
            best_j, best_d = 0, math.inf
            for j in range(len(pts) - 1):
                dd = _seg_dist(q, pts[j], pts[j + 1])
                if dd < best_d:
                    best_d, best_j = dd, j
            dx, dy = (
                pts[best_j + 1][0] - pts[best_j][0],
                pts[best_j + 1][1] - pts[best_j][1],
            )
            dd = dx * dx + dy * dy
            frac = ((q[0] - pts[best_j][0]) * dx + (q[1] - pts[best_j][1]) * dy) / max(
                dd, _EPS
            )
            inserts.setdefault(best_j, []).append((frac, pos, new_pt))
            t_old_of[pos] = t_old
            t_list[pos] = t_new
            dirty = True
        if inserts:
            # rebuild points with sorted inserts; orig[] maps each new
            # vertex back to its old index (-1 = an inserted crossing,
            # pos_at[] its offset position) so via.k and the sharp-turn
            # guard can be repaired after drops
            new_pts: list[tuple[float, float]] = [pts[0]]
            orig = [0]
            pos_at = [-1]
            for j in range(len(pts) - 1):
                for _f, ppos, pt in sorted(inserts.get(j, [])):
                    new_pts.append(pt)
                    orig.append(-1)
                    pos_at.append(ppos)
                new_pts.append(pts[j + 1])
                orig.append(j + 1)
                pos_at.append(-1)
            pts = new_pts
            # a crossing moved far along a wire that meets its edge at a
            # shallow angle doubles the wire back — drop it and revert
            # its crossing parameter (_spoke_offsets' own guard)
            k = 1
            while k < len(pts) - 1:
                if (
                    pos_at[k] >= 0
                    and _turn_deg(pts[k - 1], pts[k], pts[k + 1]) > _SHARP_TURN_DEG
                ):
                    t_list[pos_at[k]] = t_old_of[pos_at[k]]
                    del pts[k]
                    del orig[k]
                    del pos_at[k]
                    k = max(1, k - 1)
                else:
                    k += 1
            # via vertex indices: inserts on legs j < v.k land before the
            # via's old vertex — orig[] says where each survived
            for k2, v in enumerate(via_list):
                want = int(v.k)
                via_list[k2] = dataclasses.replace(
                    v, k=next((i for i, o in enumerate(orig) if o == want), want)
                )
        if dirty:
            # via crossing params: project moved centres onto their edge
            for k, v in enumerate(via_list):
                vi = int(v.i)
                if (bi, vi) in vias and vi < len(b.edges) and mesh is not None:
                    e = int(b.edges[vi])
                    t_list[vi] = _edge_param(mesh, e, (v.x, v.y))
            new_branches.append(
                dataclasses.replace(
                    b, points=tuple(pts), t=tuple(t_list), vias=tuple(via_list)
                )
            )
        else:
            new_branches.append(b)
    return dataclasses.replace(wires, branches=tuple(new_branches))


def _edge_param(mesh: TopoMesh, e: int, p: tuple[float, float]) -> float:
    eu = mesh.verts[int(mesh.edges[e, 0])]
    ev = mesh.verts[int(mesh.edges[e, 1])]
    ex, ey = ev[0] - eu[0], ev[1] - eu[1]
    dd = ex * ex + ey * ey
    if dd <= _EPS:
        return 0.0
    return min(1.0, max(0.0, ((p[0] - eu[0]) * ex + (p[1] - eu[1]) * ey) / dd))


def _seg_dist(p, a, b) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    dd = dx * dx + dy * dy
    if dd <= _EPS:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = min(1.0, max(0.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / dd))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


__all__ = ["LegaliseResult", "apply_legalise", "legalise_route"]

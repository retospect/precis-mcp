"""Rigid-body shove LP (topo-place spec §4 phase 3, the placement half).

The spec's phase-3 shove is a small LP over per-part translations:
minimise Σ|Δx|+|Δy| subject to every *overflowing* edge's length
constraint, linearised at the current pose, with every mesh triangle's
signed area kept ≥ ε — no inversion, so a wire's edge sequence on the
reference mesh stays valid. This module lands that engine ahead of
``corridor.py``: the corridor driver (demand > capacity per mesh edge
per layer) needs slice-2b's ``wires.py`` edge sequences, which are still
in flight; the driver wired here is the other hard placement constraint
the same LP solves — same-side courtyard overlaps and part-on-zone
intrusions, each linearised as a separation requirement along its
minimum-translation axis (exact under pure translation: a convex ring's
projection shifts by n·δ). :func:`~.legalize.legalize_pose` stays the
exact mop-up behind it — the LP bounds part CENTRES to their legal box,
so ring-level outline escapes and anything the linearisation declines
are still legalize's job (``placer.topo_place`` runs shove → legalize).

Each of ≤ ``rounds`` rounds: rebuild the mesh at the CURRENT pose (the
spec's "linearised at the current pose" — a stacked pair's coincident
ring vertices de-coincide as the slide progresses, so a frozen mesh
would deadlock the separation), detect the residual pairs, solve,
apply. ``δ = 0`` always satisfies the triangle rows (the floor is kept
strictly under the current area), so an infeasible LP means separation
itself cannot be met inside the bounds — reported on ``reason`` rather
than retried. Vertex→part ownership comes from the mesh's own
``edge_owner``/``pin_vertex`` (a crossing-minted vertex rides its
lowest-id owner; outline/zone vertices are immobile), so no mesh
change is needed.

One exemption from the no-inversion guard: triangles whose centroid
lies inside a conflict's INTERSECTION lens. Overlapping courtyards are
illegal geometry — no wire can thread them — and the slivers the ring
crossings mint there *must* collapse for the pair to separate;
guarding them deadlocks the LP on exactly the cases the pass exists to
fix. ``min_tri_area_mm2`` audits the guard: the smallest signed area
any CONSTRAINED round's triangles reach under that round's applied
step.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix, lil_matrix
from shapely.geometry import Point, Polygon

from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.legalize import _mtv, _part_nets, _zone_blocks_part
from precis.pcb.topoplace.mesh import MeshConflictError, TopoMesh, build_mesh
from precis.pcb.topoplace.proxy import (
    _EPS,
    _ring_geom,
    through_hole_parts,
    zone_world_ring,
)

_CLR_MM = 1e-3  # separation margin past the MTV depth (legalize's epsilon)
_TRI_FLOOR_MM2 = 1e-3  # a reference triangle's signed-area floor
_CAP_MM = 2.0  # per-round |δ| trust region — keeps the linearisation honest
_EDGE_MM = 0.5  # centre-box margin; twin of ``placer._EDGE_MM``
_SLACK_W = 100.0  # separation slack penalty (×mm) — ~100× displacement, so
# the LP only leaves overlap when the guards leave it no room


@dataclass(frozen=True, slots=True)
class ShoveResult:
    """``pose`` is the shoved pose; ``resolved`` means the final mesh
    reports no same-side pair, blocking zone or outline residual.
    ``lp_optimum_mm`` is round 1's objective — the reference for the
    spec's "displacement ≤ 1.05 × LP optimum" bound (a multi-round run
    legitimately exceeds it: later rounds see constraints the first
    linearisation could not). ``reason`` is ""/"infeasible"/"mesh"/
    "inverted" (a step the backtracker could not save); ``min_tri_area_mm2``
    the no-inversion audit — the smallest ACTUAL signed area a
    constrained triangle reached under an applied step. ``mesh_errors``
    counts the ``MeshConflictError`` catches the pass swallowed (a
    "mesh" reason means at least one) and ``mesh_error`` keeps the
    first one's text — the silent-failure count the job summary and
    bench report."""

    pose: np.ndarray
    resolved: bool
    rounds: int
    moved_mm: float
    lp_optimum_mm: float
    min_tri_area_mm2: float = math.inf
    reason: str = ""
    remaining: tuple[tuple[int, int, float], ...] = field(default_factory=tuple)
    mesh_errors: int = 0
    mesh_error: str = ""


def _vert_owner(d: TopoDesign, mesh: TopoMesh) -> np.ndarray:
    """(V,) part id per mesh vertex, −1 for immobile ones (board outline,
    zone rings, anything unclaimed). Pad vertices take their pin's part
    first — a wire endpoint must ride its own part — then constrained
    ring edges claim what's left, lowest owner id winning a vertex two
    crossing rings minted."""
    n = len(d.parts)
    owner = np.full(len(mesh.verts), -1, dtype=np.int32)
    for pin in range(len(d.pin_xy)):
        owner[int(mesh.pin_vertex[pin])] = int(d.pin_part[pin])
    for e in np.flatnonzero(mesh.constrained):
        for slot in range(2):
            o = int(mesh.edge_owner[e, slot])
            if 0 <= o < n:
                for v in mesh.edges[e]:
                    if owner[v] < 0:
                        owner[v] = o
    return owner


def _legal_xy(
    d: TopoDesign, lo: np.ndarray, hi: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Per-part legal centre box — the twin of ``placer._legal_xy`` (the
    placer imports this module, so the helper can't live there)."""
    n = len(d.parts)
    r = np.zeros(n)
    for p in range(n):
        pins = d.pin_xy[d.pin_part == p]
        if len(pins):
            r[p] = float(np.hypot(pins[:, 0], pins[:, 1]).max())
    lo_i = lo + (r + _EDGE_MM)[:, None]
    hi_i = hi - (r + _EDGE_MM)[:, None]
    centre = np.broadcast_to((lo + hi) / 2.0, (n, 2))
    ok = hi_i > lo_i
    return np.where(ok, lo_i, centre), np.where(ok, hi_i, centre)


def shove_pose(d: TopoDesign, pose: np.ndarray, *, rounds: int = 10) -> ShoveResult:
    """Spec §4 shove: a global min-|Δ| LP over part translations.

    Drivers: every same-side courtyard pair's MTV separation and every
    blocking zone intrusion — SOFT rows, each relaxed by a slack priced
    ``_SLACK_W``× displacement, so a board with more overlap than the
    guards allow still improves instead of refusing; guards: the
    round's mesh triangles keep signed area ≥ floor (linearised at the
    current pose), part centres stay in their legal box, |δ| ≤
    ``_CAP_MM`` per round — all hard. Runs to no-violations or
    ``rounds``; on LP infeasibility (bounds∩triangles — rare) stops and
    reports (legalize then takes its MTV pass on what remains)."""
    pose = np.asarray(pose, dtype=np.float64).copy()
    n = len(d.parts)
    if not n or not len(d.part_outline):
        return ShoveResult(
            pose=pose, resolved=True, rounds=0, moved_mm=0.0, lp_optimum_mm=0.0
        )
    movable = np.flatnonzero(~d.fixed)
    var_of = np.full(n, -1, dtype=np.int32)
    for m, pi in enumerate(movable):
        var_of[int(pi)] = m
    m_count = len(movable)

    lo = d.outline.min(axis=0) if len(d.outline) >= 3 else pose[:, :2].min(axis=0)
    hi = d.outline.max(axis=0) if len(d.outline) >= 3 else pose[:, :2].max(axis=0)
    cx_lo, cx_hi = _legal_xy(d, np.asarray(lo, float), np.asarray(hi, float))

    n_zones = sum(1 for z in d.zones if len(z.ring) >= 4)
    part_nets = [_part_nets(d, i) for i in range(n)] if n_zones else []
    n_layers = max((max(z.layers) for z in d.zones if z.layers), default=0) + 1

    moved_mm = 0.0
    lp_optimum = 0.0
    min_tri_area = math.inf
    reason = ""
    spent = 0
    mesh_errors = 0
    mesh_error = ""

    for _ in range(rounds):
        sides = pose[:, 3] >= 0.5
        th = through_hole_parts(d)
        geoms = [_ring_geom(d, pose[i], i) for i in range(n)]
        # re-derived each round — a part-owned hole's ring rides its
        # owner's pose, which the last step may have moved
        zone_geoms = [
            (
                z_i,
                Polygon(np.asarray(zone_world_ring(d, z, pose), dtype=np.float64)[:-1]),
            )
            for z_i, z in enumerate(d.zones)
            if len(z.ring) >= 4
        ]
        try:
            ref = build_mesh(d, pose)
        except MeshConflictError as exc:
            mesh_errors += 1
            mesh_error = mesh_error or str(exc)
            reason = reason or "mesh"
            break
        owner = _vert_owner(d, ref)

        # ── constraint rows as [(part, coeff), …] + rhs, the ≤ form.
        # Pair (a,b): n̂ the MTV axis pushing b off a — separation is
        # n̂·(δ_b − δ_a) ≥ s ⇔ n̂·δ_a − n̂·δ_b ≤ −s. Zone vs part: n̂
        # pushes the part off the (immovable) zone — n̂·δ_i ≥ s ⇔
        # −n̂·δ_i ≤ −s. Fixed parts contribute no variable and drop out.
        wants: list[tuple[list[tuple[int, np.ndarray]], float]] = []
        lenses: list[Any] = []  # conflict intersections — guard-exempt tris
        for i in range(n):
            gi = geoms[i]
            if gi is None:
                continue
            x0, y0, x1, y1 = gi.bounds
            for j in range(i + 1, n):
                gj = geoms[j]
                if gj is None or (sides[i] != sides[j] and not (th[i] or th[j])):
                    continue
                bx0, by0, bx1, by1 = gj.bounds
                if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                    continue
                if float(gi.intersection(gj).area) <= _EPS:
                    continue
                if var_of[i] < 0 and var_of[j] < 0:
                    continue  # both fixed — the final mesh reports it
                vec, depth = _mtv(
                    np.asarray(gi.exterior.coords),
                    np.asarray(gj.exterior.coords),
                )
                if depth > 0.0:
                    axis = vec / depth
                    wants.append(([(i, axis), (j, -axis)], -(depth + _CLR_MM)))
                    lenses.append(gi.intersection(gj))
            if zone_geoms:
                for z_i, zg in zone_geoms:
                    z = d.zones[z_i]
                    if z.part == i or (z.net and z.net in part_nets[i]):
                        continue
                    if not _zone_blocks_part(z, bool(sides[i]), bool(th[i]), n_layers):
                        continue
                    bx0, by0, bx1, by1 = zg.bounds
                    if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                        continue
                    if float(gi.intersection(zg).area) <= _EPS:
                        continue
                    if var_of[i] < 0:
                        continue
                    vec, depth = _mtv(
                        np.asarray(zg.exterior.coords),
                        np.asarray(gi.exterior.coords),
                    )
                    if depth > 0.0:
                        wants.append(([(i, -vec / depth)], -(depth + _CLR_MM)))
        if not wants:
            break

        cur = ref.verts  # the mesh was built at THIS pose — δ applies on top
        n_w = len(wants)
        n_col = 4 * m_count + n_w
        A = lil_matrix((n_w + len(ref.tris) + 4 * m_count, n_col))
        b = np.zeros(n_w + len(ref.tris) + 4 * m_count)
        r_ = 0
        for e, (terms, rhs) in enumerate(wants):
            for part, coeff in terms:
                if var_of[part] >= 0:
                    A[r_, var_of[part]] += coeff[0]
                    A[r_, m_count + var_of[part]] += coeff[1]
            A[r_, 4 * m_count + e] = -1.0  # slack_e absorbs the shortfall
            b[r_] = rhs
            r_ += 1
        tri_c = cur[ref.tris].mean(axis=1)  # (T, 2) centroids
        exempt = np.zeros(len(ref.tris), dtype=bool)
        for lens in lenses:
            exempt |= np.asarray(
                [lens.covers(Point(float(x), float(y))) for x, y in tri_c]
            )
        for va, vb, vc in ref.tris[~exempt]:
            xa, ya = cur[va]
            xb, yb = cur[vb]
            xc, yc = cur[vc]
            area = 0.5 * (xa * (yb - yc) + xb * (yc - ya) + xc * (ya - yb))
            floor = min(
                _TRI_FLOOR_MM2, 0.9 * area
            )  # strictly < area: δ=0 stays feasible
            # −Σ_v grad_v·δ_{owner(v)} ≤ area − floor
            grads = {
                int(va): ((yb - yc) / 2.0, (xc - xb) / 2.0),
                int(vb): ((yc - ya) / 2.0, (xa - xc) / 2.0),
                int(vc): ((ya - yb) / 2.0, (xb - xa) / 2.0),
            }
            for v, (gx, gy) in grads.items():
                o = int(owner[v])
                if o >= 0 and var_of[o] >= 0:
                    A[r_, var_of[o]] -= gx
                    A[r_, m_count + var_of[o]] -= gy
            b[r_] = area - floor
            r_ += 1
        for m in range(m_count):
            # u_m ≥ +δ_m  →  δ_m − u_m ≤ 0 ;  u_m ≥ −δ_m → −δ_m − u_m ≤ 0
            k = m  # dx index
            A[r_, k] = 1.0
            A[r_, 2 * m_count + k] = -1.0
            b[r_] = 0.0
            r_ += 1
            A[r_, k] = -1.0
            A[r_, 2 * m_count + k] = -1.0
            b[r_] = 0.0
            r_ += 1
            k = m_count + m  # dy index
            A[r_, k] = 1.0
            A[r_, 2 * m_count + k] = -1.0
            b[r_] = 0.0
            r_ += 1
            A[r_, k] = -1.0
            A[r_, 2 * m_count + k] = -1.0
            b[r_] = 0.0
            r_ += 1
        A_ub = csr_matrix(A)
        obj = np.concatenate(
            [
                np.zeros(2 * m_count),
                np.ones(2 * m_count),
                np.full(n_w, _SLACK_W),
            ]
        )
        bounds: list[tuple[float, float | None]] = []
        for m in range(m_count):
            p = int(movable[m])
            for axis_i in range(2):
                centre = pose[p, axis_i]
                lb = max(float(cx_lo[p, axis_i]) - centre, -_CAP_MM)
                ub = min(float(cx_hi[p, axis_i]) - centre, _CAP_MM)
                if lb > ub:
                    lb = ub = 0.0
                bounds.append((lb, ub))
        bounds = (
            [bounds[2 * m] for m in range(m_count)]
            + [bounds[2 * m + 1] for m in range(m_count)]
            + [(0.0, _CAP_MM)] * (2 * m_count)
            + [(0.0, None)] * n_w
        )

        res = linprog(obj, A_ub=A_ub, b_ub=b, bounds=bounds, method="highs")
        if res.status != 0:
            reason = "infeasible"
            break
        if spent == 0:
            lp_optimum = float(res.fun)
        step = res.x[: 2 * m_count].reshape(2, m_count).T  # (M, 2)
        if float(np.abs(step).sum()) <= 1e-9:
            break
        # Backtracking on the ACTUAL (quadratic) signed areas: the
        # linearised guard can admit a step that inverts a constrained
        # triangle, so verify the exact areas and halve until clean —
        # lens tris stay exempt (they are meant to collapse).
        free = (owner >= 0) & (var_of[np.clip(owner, 0, None)] >= 0)
        keep = ref.tris[~exempt]
        scale = 1.0
        for _try in range(6):
            vstep = np.zeros((len(ref.verts), 2))
            vstep[free] = step[var_of[owner[free]]] * scale
            t = (cur + vstep)[keep]
            actual = 0.5 * (
                t[:, 0, 0] * (t[:, 1, 1] - t[:, 2, 1])
                + t[:, 1, 0] * (t[:, 2, 1] - t[:, 0, 1])
                + t[:, 2, 0] * (t[:, 0, 1] - t[:, 1, 1])
            )
            amin = float(actual.min()) if len(t) else math.inf
            if amin > 1e-9:
                break
            scale *= 0.5
        else:
            reason = reason or "inverted"
            break
        min_tri_area = min(min_tri_area, amin)
        step = step * scale
        for m in range(m_count):
            p = int(movable[m])
            pose[p, 0] += step[m, 0]
            pose[p, 1] += step[m, 1]
        moved_mm += float(np.abs(step).sum())
        spent += 1

    # Final report from a fresh mesh at the shoved pose — pair, zone and
    # outline residuals land on ``remaining`` in legalize's encoding.
    remaining: list[tuple[int, int, float]] = []
    try:
        end = build_mesh(d, pose)
        for cf in end.conflicts:
            if cf.same_side:
                remaining.append((int(cf.a), int(cf.b), float(cf.area_mm2)))
        sides = pose[:, 3] >= 0.5
        th = through_hole_parts(d)
        for part, z_i, area in end.zone_conflicts:
            z = d.zones[z_i]
            if z.part == part or (z.net and part_nets and z.net in part_nets[part]):
                continue
            if _zone_blocks_part(z, bool(sides[part]), bool(th[part]), n_layers):
                remaining.append((int(part), n + int(z_i), float(area)))
        for part, area in end.outline_conflicts:
            remaining.append((int(part), -1, float(area)))
    except MeshConflictError as exc:
        mesh_errors += 1
        mesh_error = mesh_error or str(exc)
        reason = reason or "mesh"
    return ShoveResult(
        pose=pose,
        resolved=not remaining,
        rounds=spent,
        moved_mm=moved_mm,
        lp_optimum_mm=lp_optimum,
        min_tri_area_mm2=min_tri_area,
        reason=reason,
        remaining=tuple(sorted(remaining)),
        mesh_errors=mesh_errors,
        mesh_error=mesh_error,
    )


__all__ = ["ShoveResult", "shove_pose"]

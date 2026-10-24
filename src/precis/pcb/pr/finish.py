"""§Finish for the slice-2 engine: rotation and position sugar, wire
clean-up, emit, exact DRC and the resumable route state.

1. **Rotation sugar** — per rotation-free part try the nearest 90°
   multiple, then the nearest 45° (``field.ROT_TIERS_DEG``); keep a
   snap only if the board stays legal (violation depth no worse) and no
   worse on the priced energy. Same rule for light gridding of part
   positions.
2. **Wire clean-up** — drop points a chord passes within ``tol`` of
   (Ramer–Douglas–Peucker per same-layer run); kept only if the exact
   violation depth does not grow. No angle snap (Reto: "entirely
   arbitrary and I don't care").
3. Polygon-per-net export is not in v1: the copper leaves as tracks at
   their (current-sized, appetite-widened) width; DRC and the fab model
   read tracks the same way.
4. **Exact DRC** with the kernel: :func:`precis.pcb.session.
   routed_drc_findings` (copper) plus :func:`placement_drc_findings`
   (courtyards, outline) on the moved board.
5. **Route state** — :func:`snapshot` / :func:`restore`: poses, per-wire
   polylines and per-piece layers, enough for a later run to resume.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from precis.pcb.ir import PcbIR
from precis.pcb.pr.beliefs import Layout
from precis.pcb.pr.state import PrProblem

#: Rotation tiers tried by the sugar, coarse first (Q7: 90° free, 45° priced).
ROT_SUGAR_DEG: tuple[float, ...] = (90.0, 45.0)
#: Light position grid for the sugar (mm).
POS_GRID_MM = 0.1


def _wrap(th: float) -> float:
    return (th + math.pi) % (2.0 * math.pi) - math.pi


def rotation_sugar(
    relaxer: Any,
    pos: Any,
    assign: Any,
    gpose: Any,
    extra: Any = None,
    *,
    grid_mm: float = POS_GRID_MM,
    tiers_deg: tuple[float, ...] = ROT_SUGAR_DEG,
) -> tuple[Any, dict[str, int]]:
    """Every rotation-free part's θ is WRITTEN as a grid value — the
    placer's D5/Q7 policy (``field.py`` Harden): the nearest 90° multiple
    unless the nearest finer-tier pose (45°) is legal and beats that 90°
    pose on the priced energy by more than ``field.ROT_TILT_MIN_GAIN``.
    The descended continuous θ is never written, whatever it scores, so
    the rotation-grid gate (``tests/test_pcb_esp32c3_rotation_grid.py``)
    reads only grid values. Positions then snap to ``grid_mm`` only when
    that keeps the board legal and no worse. ``pos``/``assign``/``gpose``
    are single-sample tensors ``(1, …)``; returns the new ``gpose`` and
    counts."""
    import torch

    from precis.pcb.field import ROT_TILT_MIN_GAIN

    def score(gp):
        with torch.no_grad():
            E, parts = relaxer.energy(pos, assign, 1.0, gp, extra)
        viol = parts["barrier"] + parts["court"]
        return float(E[0]), float(viol[0])

    counts = {"rot_90": 0, "rot_45": 0, "grid": 0, "tried": 0}
    g = gpose.clone()
    e0, v0 = score(g)
    n = g.shape[1]
    lock_rot = relaxer.glock_rot
    lock_xy = relaxer.glock_xy
    coarse = math.radians(max(tiers_deg))
    fine = math.radians(min(tiers_deg))
    for i in range(n):
        if not bool(lock_rot[i]):
            counts["tried"] += 1
            th = float(g[0, i, 2])
            on_coarse = coarse * round(th / coarse)
            on_fine = fine * round(th / fine)
            trial = g.clone()
            trial[0, i, 2] = on_coarse
            e_c, v_c = score(trial)
            choice, e_n, v_n, tier = trial, e_c, v_c, "rot_90"
            if abs(_wrap(on_fine - on_coarse)) > 1e-9:
                trial_f = g.clone()
                trial_f[0, i, 2] = on_fine
                e_f, v_f = score(trial_f)
                if v_f <= v_c + 1e-9 and e_f + ROT_TILT_MIN_GAIN < e_c:
                    choice, e_n, v_n, tier = trial_f, e_f, v_f, "rot_45"
            if abs(_wrap(float(choice[0, i, 2]) - th)) > 1e-9:
                counts[tier] += 1
            g, e0, v0 = choice, e_n, v_n
        if not bool(lock_xy[i]) and grid_mm > 0.0:
            trial = g.clone()
            trial[0, i, 0] = grid_mm * round(float(g[0, i, 0]) / grid_mm)
            trial[0, i, 1] = grid_mm * round(float(g[0, i, 1]) / grid_mm)
            if not torch.allclose(trial, g):
                e1, v1 = score(trial)
                if v1 <= v0 + 1e-9 and e1 <= e0 + 1e-6:
                    g, e0, v0 = trial, e1, v1
                    counts["grid"] += 1
    return g, counts


def _rdp(points: np.ndarray, tol: float) -> np.ndarray:
    """Ramer–Douglas–Peucker on an open polyline (N, 2)."""
    if len(points) < 3:
        return points
    a, b = points[0], points[-1]
    d = b - a
    den = float(d @ d)
    if den < 1e-18:
        dist = np.linalg.norm(points - a, axis=1)
    else:
        t = ((points - a) @ d) / den
        proj = a + np.clip(t, 0.0, 1.0)[:, None] * d
        dist = np.linalg.norm(points - proj, axis=1)
    k = int(np.argmax(dist[1:-1])) + 1
    if dist[k] <= tol:
        return np.stack((a, b))
    left = _rdp(points[: k + 1], tol)
    right = _rdp(points[k:], tol)
    return np.concatenate((left[:-1], right))


def cleanup_wires(
    polylines: dict[int, np.ndarray],
    layers: dict[int, np.ndarray],
    *,
    tol_mm: float = 0.02,
) -> tuple[dict[int, np.ndarray], dict[int, np.ndarray]]:
    """Simplify every same-layer run of every wire by RDP at ``tol_mm``;
    layer-change vertices (via sites) are always kept. Returns new
    polylines and per-piece layers."""
    out_p: dict[int, np.ndarray] = {}
    out_l: dict[int, np.ndarray] = {}
    for wi, poly in polylines.items():
        lay = layers[wi]
        if len(poly) < 3 or len(lay) != len(poly) - 1:
            out_p[wi], out_l[wi] = poly, lay
            continue
        pts: list[np.ndarray] = []
        lays: list[int] = []
        i = 0
        n = len(lay)
        while i < n:
            layer = int(lay[i])
            j = i
            while j < n and int(lay[j]) == layer:
                j += 1
            run = _rdp(poly[i : j + 1], tol_mm)
            lays.extend([layer] * (len(run) - 1))
            pts.extend(run[1:] if pts else run)
            i = j
        out_p[wi] = np.asarray(pts, dtype=np.float64)
        out_l[wi] = np.asarray(lays, dtype=np.int64)
        if len(out_l[wi]) != len(out_p[wi]) - 1:  # defensive: keep original
            out_p[wi], out_l[wi] = poly, lay
    return out_p, out_l


def snapshot(
    ir: PcbIR,
    problem: PrProblem,
    polylines: dict[int, np.ndarray],
    layers: dict[int, np.ndarray],
) -> dict[str, Any]:
    """The resumable route state: every placed instance's pose and
    every routed wire's polyline + per-piece layers, keyed by IR
    segment id so a rebuilt IR with the same netlist can take it back."""
    poses: dict[str, list[float]] = {}
    for i in range(ir.n_instances):
        x, y = float(ir.inst_x[i]), float(ir.inst_y[i])
        if math.isnan(x) or math.isnan(y):
            continue
        rot = float(ir.inst_rot[i])
        poses[str(ir.instance_refdes[i])] = [x, y, 0.0 if math.isnan(rot) else rot]
    wires = []
    for wi, poly in sorted(polylines.items()):
        if len(poly) < 2:
            continue
        wires.append(
            {
                "seg_id": int(problem.wires[wi].seg_id),
                "points": [[float(p[0]), float(p[1])] for p in poly],
                "layers": [int(l) for l in layers.get(wi, np.zeros(0))],
            }
        )
    return {"version": 1, "poses": poses, "wires": wires}


def restore(
    ir: PcbIR, state: dict[str, Any]
) -> tuple[dict[int, np.ndarray], dict[int, np.ndarray]]:
    """Apply a :func:`snapshot` to ``ir`` (poses, via ``move_instance``)
    and return ``(polylines, layers)`` keyed by IR SEGMENT id (the caller
    maps segment → wire once the problem is built). Unknown refdes or
    segments are skipped — a changed netlist resumes what still matches."""
    idx = {str(ir.instance_refdes[i]): i for i in range(ir.n_instances)}
    for refdes, (x, y, rot) in (state.get("poses") or {}).items():
        i = idx.get(refdes)
        if i is None:
            continue
        ir.move_instance(i, x=float(x), y=float(y), rot=float(rot))
    polys: dict[int, np.ndarray] = {}
    lays: dict[int, np.ndarray] = {}
    for w in state.get("wires") or []:
        s = int(w["seg_id"])
        if s < 0 or s >= ir.n_segments:
            continue
        pts = np.asarray(w["points"], dtype=np.float64).reshape(-1, 2)
        ls = np.asarray(w.get("layers") or [], dtype=np.int64)
        if len(pts) >= 2 and len(ls) == len(pts) - 1:
            polys[s] = pts
            lays[s] = ls
    return polys, lays


def exact_drc(
    ir: PcbIR,
    rres: Any,
    *,
    capability: Any,
    footprints: dict[str, dict[str, Any]] | None,
    outline: list[list[float]] | None,
    net_rules: dict[str, Any] | None = None,
    net_voltages: dict[str, float] | None = None,
    fixed_copper: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """Exact DRC with the kernel on the finished board: the routed copper
    pass plus the placement pass (courtyards, outline) — parts moved, so
    both halves are re-judged."""
    from precis.pcb import session

    out = session.routed_drc_findings(
        ir,
        rres,
        capability=capability,
        footprints=footprints,
        fixed_copper=fixed_copper,
        outline=outline,
        net_rules=net_rules,
        net_voltages=net_voltages,
    )
    out += session.placement_drc_findings(
        ir,
        capability=capability,
        footprints=footprints,
        fixed_copper=fixed_copper,
        outline=outline,
        net_rules=net_rules,
    )
    return out


def layout_wire_state(
    layout: Layout, pos: np.ndarray, assign: np.ndarray
) -> tuple[dict[int, np.ndarray], dict[int, np.ndarray]]:
    """``(polylines, per-piece layers)`` per wire off one sample's point
    array and layer assignment."""
    polys: dict[int, np.ndarray] = {}
    lays: dict[int, np.ndarray] = {}
    for wi in np.unique(layout.wire_of_point):
        pts = pos[layout.wire_of_point == wi]
        pcs = np.flatnonzero(layout.piece_wire == wi)
        polys[int(wi)] = np.asarray(pts, dtype=np.float64)
        lays[int(wi)] = np.asarray(assign[pcs], dtype=np.int64)
    return polys, lays

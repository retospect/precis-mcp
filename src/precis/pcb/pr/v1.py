"""Slice-2 driver: v1 place-and-route on a real board
(``docs/backlog/pcb-place-route-datamodel.md`` §Slices item 2, §Loop
nesting). Free parts, blocks, wire refinement, finish, exact DRC.

The outer loop, per hierarchy level (a rigid-block level first when the
IR has multi-member groups, then the part level) and per refinement
stage (``piece_mm`` 4 → 2 → 1 mm):

    rebuild the problem from the IR's CURRENT poses   (4a: geometry every round)
    4a   seed the wires that have no polyline yet; re-funnel runs whose
         layer changed last round
    4b   layer beliefs (carried across rebuilds by arclength) → K samples
    4c   batched relaxation with part poses as variables
    4d   CEM on Q; best sample → poses written back to the IR,
         polylines + per-piece layers kept for the next round

then §Finish: rotation/grid sugar, wire clean-up, emit, exact DRC, and
the resumable state. The 4-0 starting placement is the caller's: the
bench hands in the job-placed board (``optimize.py``), so pr and vroute
start from the same poses.

Everything is measured, nothing asserted here: :class:`V1Run` carries
the numbers the spec's §Slice 2 results table reports.
"""

from __future__ import annotations

import copy
import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.pcb import realize
from precis.pcb.ir import PcbIR
from precis.pcb.pr import beliefs, finish, uncross
from precis.pcb.pr import seed as prseed
from precis.pcb.pr.beliefs import Layout
from precis.pcb.pr.emit import to_realize_result
from precis.pcb.pr.relax import Relaxer, RelaxParams, RelaxStats
from precis.pcb.pr.state import PrProblem, build_problem, write_poses


@dataclass(frozen=True, slots=True)
class Stage:
    """One refinement stage: span granularity, outer rounds, relax steps."""

    piece_mm: float
    rounds: int
    steps: int
    grow_steps: int


@dataclass(frozen=True, slots=True)
class V1Params:
    """Every provisional parameter the report must name."""

    K: int = 8
    stages: tuple[Stage, ...] = (
        Stage(4.0, 2, 60, 30),
        Stage(2.0, 2, 80, 40),
        Stage(1.0, 2, 80, 40),
    )
    #: Run a rigid-block level first (only when the IR has a group with
    #: two or more movable members), at the coarsest stage's granularity.
    block_level: bool = True
    block_rounds: int = 2
    crowd_mm: float = 0.6
    pair_cost_mm: float = 2.0
    t0: float = 2.0
    t1: float = 0.3
    elite_frac: float = 0.25
    cem_alpha: float = 0.7
    relax: RelaxParams = field(default_factory=RelaxParams)
    finish: bool = True
    cleanup_tol_mm: float = 0.005
    #: Per-wire violation (mm²-equivalent, ≈ (0.03 mm)²) above which a
    #: wire is reported unrouted instead of emitted as illegal copper.
    demote_tau: float = 1e-3
    #: Wall-time limit for the rounds (the job's ``deadline_s``); finish
    #: still runs on the best board so far.
    deadline_s: float | None = None
    seed: int = 0
    #: Nets left unrouted on purpose (a diagnostic: the bench's "-GND"
    #: rows take the star-hub ground net out to show the rest).
    skip_nets: frozenset[str] = frozenset()
    #: Diagnostic: no 4b sampling — every sample keeps the seed's layers,
    #: so the bench can tell a 4b loss from a 4c loss.
    freeze_layers: bool = False


@dataclass(slots=True)
class V1Run:
    ir: PcbIR  # the moved board (a copy of the input)
    problem: PrProblem
    result: Any  # RealizeResult
    polylines: dict[int, np.ndarray]
    layers: dict[int, np.ndarray]
    unrouted: tuple[int, ...]
    reasons: dict[int, str]
    entropy_hist: list[float]
    evals: int
    relax_seconds: float
    wall_s: float
    blocked_frac: float
    n_spans: int
    via_count: int
    wire_mm: float
    rounds_run: int
    levels: list[str]
    sugar: dict[str, int]
    cleanup_kept: bool
    part_travel_mm: float  # mean |Δpos| of movable parts vs the start
    part_turn_deg: float  # mean |Δθ| of movable parts vs the start
    state: dict[str, Any]
    over_capacity: list[str]
    deadline_hit: bool
    #: the movable parts' poses the run started from (x, y, θ_rad) — a
    #: resumed run starts exactly where the saved state ended
    start_pose: np.ndarray
    #: per round: same-layer crossings over the K samples before and
    #: after the layer-move repair
    crossings_hist: list[tuple[int, int]]
    #: wires reported unrouted at finish because they stayed in violation
    demoted: int = 0


def _refine(
    poly: np.ndarray, lay: np.ndarray, piece_mm: float
) -> tuple[np.ndarray, np.ndarray]:
    """Subdivide a wire the way :func:`seed.subdivide` does, carrying each
    piece's layer onto its children."""
    if len(poly) < 2:
        return poly, lay
    pts = [poly[0]]
    lays: list[int] = []
    for i in range(len(poly) - 1):
        a, b = poly[i], poly[i + 1]
        n = max(1, int(np.ceil(float(np.linalg.norm(b - a)) / piece_mm)))
        for k in range(1, n + 1):
            pts.append(a + (b - a) * (k / n))
            lays.append(int(lay[i]) if i < len(lay) else 0)
    return np.asarray(pts), np.asarray(lays, dtype=np.int64)


def _has_blocks(problem: PrProblem) -> bool:
    g = problem.dyn.group
    return bool(len(g)) and len(np.unique(g)) < len(g)


def _assign_from(layout: Layout, layers: dict[int, np.ndarray]) -> np.ndarray:
    """Per-piece hard layers for ``layout`` from the per-wire layer
    arrays; a wire whose piece count does not match (or has none) takes
    its pieces' first allowed layer. ``known`` marks the matched rows."""
    out = np.argmax(layout.span_allowed, axis=1).astype(np.int64)
    for wi in np.unique(layout.piece_wire):
        pcs = np.flatnonzero(layout.piece_wire == wi)
        lay = layers.get(int(wi))
        if lay is not None and len(lay) == len(pcs):
            out[pcs] = lay
    return out


def _known_mask(layout: Layout, layers: dict[int, np.ndarray]) -> np.ndarray:
    known = np.zeros(layout.n_spans, dtype=bool)
    for wi in np.unique(layout.piece_wire):
        pcs = np.flatnonzero(layout.piece_wire == wi)
        lay = layers.get(int(wi))
        if lay is not None and len(lay) == len(pcs):
            known[pcs] = True
    return known


def run(
    ir_in: PcbIR,
    *,
    outline: list[list[float]],
    config: realize.RealizeConfig,
    footprints: dict[str, dict[str, Any]] | None = None,
    params: V1Params | None = None,
    resume: dict[str, Any] | None = None,
) -> V1Run:
    import torch

    prm = params or V1Params()
    torch.manual_seed(prm.seed)
    gen = np.random.default_rng(prm.seed)
    t_start = time.monotonic()
    ir = copy.deepcopy(ir_in)
    seg_polys: dict[int, np.ndarray] = {}
    seg_lays: dict[int, np.ndarray] = {}
    if resume:
        seg_polys, seg_lays = finish.restore(ir, resume)

    def build() -> PrProblem:
        return build_problem(
            ir, outline=outline, config=config, footprints=footprints, free_parts=True
        )

    problem = build()
    start_pose = problem.dyn.pose0.copy()
    wire_of_seg = {w.seg_id: wi for wi, w in enumerate(problem.wires)}
    polylines: dict[int, np.ndarray] = {
        wire_of_seg[s]: p for s, p in seg_polys.items() if s in wire_of_seg
    }
    layers: dict[int, np.ndarray] = {
        wire_of_seg[s]: l for s, l in seg_lays.items() if s in wire_of_seg
    }
    unrouted: set[int] = set()
    reasons: dict[int, str] = {}
    stats = RelaxStats()
    entropy_hist: list[float] = []
    crossings_hist: list[tuple[int, int]] = []
    prev_layout: Layout | None = None
    prev_q: np.ndarray | None = None
    pending_targets: dict[int, np.ndarray] = {}  # wire -> target layers
    prev_piece_layers: dict[int, np.ndarray] = {}
    levels: list[str] = []
    rounds_run = 0
    last: dict[str, Any] = {}
    deadline_hit = False

    schedule: list[tuple[str, Stage]] = []
    if prm.block_level and _has_blocks(problem):
        s0 = prm.stages[0]
        schedule.append(
            ("block", Stage(s0.piece_mm, prm.block_rounds, s0.steps, s0.grow_steps))
        )
    schedule += [("part", s) for s in prm.stages]
    total_rounds = sum(s.rounds for _, s in schedule)

    for level, stage in schedule:
        levels.append(f"{level}@{stage.piece_mm}mm")
        rp = dataclasses.replace(
            prm.relax, steps=stage.steps, grow_steps=stage.grow_steps
        )
        for _rnd in range(stage.rounds):
            if (
                prm.deadline_s is not None
                and time.monotonic() - t_start > prm.deadline_s
            ):
                deadline_hit = True
                break
            problem = build()
            # endpoints follow the moved pins
            for wi, poly in list(polylines.items()):
                w = problem.wires[wi]
                poly = poly.copy()
                poly[0] = w.a
                poly[-1] = w.b
                polylines[wi] = poly
            keep = {
                wi: (poly, int(np.bincount(layers[wi]).argmax()))
                for wi, poly in polylines.items()
                if len(poly) >= 2 and wi in layers and len(layers[wi])
            }
            skip = {
                wi
                for wi, w in enumerate(problem.wires)
                if problem.net_name[w.net_id] in prm.skip_nets
            }
            seeded = prseed.seed(problem, keep=keep, skip=skip)
            unrouted = set(seeded.unrouted)
            reasons = {i: seeded.seeds[i].reason or "" for i in unrouted}
            for wi, s in enumerate(seeded.seeds):
                if wi in unrouted:
                    polylines.pop(wi, None)
                    layers.pop(wi, None)
                    pending_targets.pop(wi, None)
                    continue
                kept = keep.get(wi)
                # a kept wire ripped and re-funnelled comes back as a seed
                if kept is None or not np.array_equal(np.asarray(s.polyline), kept[0]):
                    poly, lay = _refine(
                        np.asarray(s.polyline, dtype=np.float64),
                        np.full(max(1, len(s.polyline) - 1), s.layer, dtype=np.int64),
                        stage.piece_mm,
                    )
                    polylines[wi] = poly
                    layers[wi] = lay
                    pending_targets.pop(wi, None)
            # 4a: re-funnel runs whose layer changed last round
            for wi, tgt in list(pending_targets.items()):
                if wi not in polylines or len(tgt) != len(layers[wi]):
                    continue
                # the targets were recorded on last round's pieces; carry
                # them (and the previous layers) by arclength onto this
                # stage's refinement, so a 4 → 2 → 1 mm boundary never
                # skips the re-funnel
                base_poly = polylines[wi]
                poly, lay = _refine(base_poly, layers[wi], stage.piece_mm)
                tgt_ref = _layers_by_arclength(base_poly, tgt, poly)
                prev = prev_piece_layers.get(wi)
                prev_ref = (
                    _layers_by_arclength(base_poly, prev, poly)
                    if prev is not None and len(prev) == len(tgt)
                    else None
                )
                newp = prseed.refunnel(
                    problem,
                    seeded,
                    wi,
                    tgt_ref,
                    stage.piece_mm,
                    base=poly,
                    prev_layers=prev_ref,
                )
                if len(newp) >= 2:
                    polylines[wi] = np.asarray(newp, dtype=np.float64)
                    layers[wi] = _layers_by_arclength(poly, tgt_ref, polylines[wi])
            pending_targets.clear()
            # refine to this stage's granularity
            for wi in list(polylines):
                polylines[wi], layers[wi] = _refine(
                    polylines[wi], layers[wi], stage.piece_mm
                )
            order = list(range(len(problem.wires)))
            layout = beliefs.build_layout(
                problem,
                [
                    polylines[wi] if wi in polylines else np.zeros((0, 2))
                    for wi in order
                ],
                piece_mm=stage.piece_mm,
            )
            if prev_layout is not None and prev_q is not None:
                q = beliefs.carry_q(prev_layout, prev_q, layout)
            else:
                q = beliefs.init_q(layout, problem.n_layers)
            # blend in the current hard layers (a resumed or re-funnelled
            # wire carries them)
            known = _known_mask(layout, layers)
            if known.any():
                onehot = np.eye(problem.n_layers)[_assign_from(layout, layers)]
                blend = np.where(known[:, None], 0.5 * q + 0.5 * onehot, q)
                q = np.where(layout.span_allowed, blend, 0.0)
                q = q / np.clip(q.sum(1, keepdims=True), 1e-12, None)
            T = prm.t0 + (prm.t1 - prm.t0) * (rounds_run / max(1, total_rounds - 1))
            pairs = beliefs.pair_table(problem, layout, crowd_mm=prm.crowd_mm)
            q = beliefs.update_q(
                problem,
                layout,
                q,
                *pairs,
                temperature=T,
                via_cost_mm=rp.via_cost_mm,
                pair_cost_mm=prm.pair_cost_mm,
            )
            entropy_hist.append(beliefs.belief_entropy(q))
            if prm.freeze_layers:
                # diagnostic: every sample keeps the current hard layers
                # (the seed's), so 4c alone is measured
                assign_np = np.tile(_assign_from(layout, layers)[None, :], (prm.K, 1))
            else:
                assign_np = beliefs.sample_layers(q, prm.K, gen)
            fixed = layout.span_allowed.sum(1) == 1
            assign_np[:, fixed] = np.argmax(layout.span_allowed[fixed], axis=1)[None, :]
            relaxer = Relaxer(problem, layout, rp, blocks=(level == "block"))
            # 4d repair on every sample BEFORE relaxing: same-layer
            # crossings the sampler produced become layer moves (two
            # vias), so the beads they add get relaxed with the rest
            xb_tot = xa_tot = 0
            for kk in range(prm.K):
                assign_np[kk], xb, xa = uncross.uncross_by_layer(
                    layout,
                    layout.points,
                    assign_np[kk],
                    relaxer.pp_i.numpy(),
                    relaxer.pp_j.numpy(),
                    n_layers=problem.n_layers,
                )
                xb_tot += xb
                xa_tot += xa
            crossings_hist.append((xb_tot, xa_tot))
            pos, E, parts, st = relaxer.run(assign_np)
            stats.evals += st.evals
            stats.blocked += st.blocked
            stats.dropped += st.dropped
            stats.steps += st.steps
            stats.seconds += st.seconds
            viol = relaxer.violations(
                pos, torch.tensor(assign_np), parts["gpose"], parts["extra"]
            )
            scores = E.numpy() + rp.stiff_max * viol.numpy()
            q = beliefs.cem_update(
                q,
                assign_np,
                scores,
                elite_frac=prm.elite_frac,
                alpha=prm.cem_alpha,
                allowed=layout.span_allowed,
            )
            k = int(np.argmin(scores))
            best_pos = pos[k].numpy()
            best_assign = assign_np[k]
            new_polys, new_lays = finish.layout_wire_state(
                layout, best_pos, best_assign
            )
            for wi, nl in new_lays.items():
                if wi in unrouted:
                    continue
                old = layers.get(wi)
                if (
                    old is not None
                    and len(old) == len(nl)
                    and not np.array_equal(old, nl)
                ):
                    pending_targets[wi] = nl.copy()
                    prev_piece_layers[wi] = old.copy()
                polylines[wi] = new_polys[wi]
                layers[wi] = nl
            write_poses(ir, problem, parts["pose"][k].numpy())
            prev_layout, prev_q = layout, q
            last = {
                "relaxer": relaxer,
                "layout": layout,
                "pos": best_pos,
                "assign": best_assign,
                "gpose": parts["gpose"][k : k + 1].clone(),
                "extra": parts["extra"][k : k + 1].clone()
                if parts["extra"] is not None
                else None,
                "seeded": seeded,
            }
            rounds_run += 1
        if deadline_hit:
            break

    # ── finish ──
    sugar = {"rot_90": 0, "rot_45": 0, "grid": 0, "tried": 0}
    cleanup_kept = False
    demoted = 0
    order = list(range(len(problem.wires)))
    if last and prm.finish:
        relaxer = last["relaxer"]
        pos_t = torch.tensor(last["pos"][None], dtype=torch.float64)
        asg_t = torch.tensor(last["assign"][None], dtype=torch.int64)
        gp = last["gpose"]
        if problem.dyn.n_parts:
            gp, sugar = finish.rotation_sugar(relaxer, pos_t, asg_t, gp, last["extra"])
            with torch.no_grad():
                _, parts = relaxer.energy(pos_t, asg_t, 1.0, gp, last["extra"])
            write_poses(ir, problem, parts["pose"][0].numpy())
            np_pts = parts["points"][0].numpy()
            new_polys, new_lays = finish.layout_wire_state(
                last["layout"], np_pts, last["assign"]
            )
            for wi in new_polys:
                if wi not in unrouted:
                    polylines[wi], layers[wi] = new_polys[wi], new_lays[wi]
            problem = build()  # the sugar moved parts: fresh pads, anchors

        # wire clean-up, kept only if the exact violation depth does not grow
        def _viol(polys: dict[int, np.ndarray], lays: dict[int, np.ndarray]) -> float:
            lay = beliefs.build_layout(
                problem,
                [polys[wi] if wi in polys else np.zeros((0, 2)) for wi in order],
                piece_mm=1e9,
            )
            rx = Relaxer(problem, lay, prm.relax)
            p = torch.tensor(lay.points[None], dtype=torch.float64)
            a = torch.tensor(_assign_from(lay, lays)[None], dtype=torch.int64)
            return float(rx.violations(p, a, rx.gpose0[None])[0])

        v_before = _viol(polylines, layers)
        cp, cl = finish.cleanup_wires(polylines, layers, tol_mm=prm.cleanup_tol_mm)
        if _viol(cp, cl) <= v_before + 1e-9:
            polylines, layers = cp, cl
            cleanup_kept = True
        # §Results and failures: a wire still in violation after the
        # relaxation (a crossing the layer moves could not clear, an
        # over-capacity corridor) is reported as unrouted with its depth,
        # not emitted as illegal copper. Worst offenders first, in batches,
        # since demoting one wire clears its partners' share too.
        for _pass in range(len(order)):
            dlay = beliefs.build_layout(
                problem,
                [
                    polylines[wi] if wi in polylines else np.zeros((0, 2))
                    for wi in order
                ],
                piece_mm=1e9,
            )
            rx = Relaxer(problem, dlay, prm.relax)
            p = torch.tensor(dlay.points[None], dtype=torch.float64)
            a = torch.tensor(_assign_from(dlay, layers)[None], dtype=torch.int64)
            wv = rx.wire_violations(p, a, rx.gpose0[None])[0].numpy()
            for wi in unrouted:
                wv[wi] = 0.0
            bad = np.flatnonzero(wv > prm.demote_tau)
            if not len(bad):
                break
            batch = bad[np.argsort(-wv[bad])][: max(1, int(np.ceil(0.1 * len(bad))))]
            for wi in batch:
                wi = int(wi)
                unrouted.add(wi)
                reasons[wi] = (
                    f"left in violation after relaxation: {np.sqrt(wv[wi]):.3f} mm "
                    "overlap-equivalent against neighbouring copper on "
                    f"{problem.layers[int(np.bincount(layers[wi]).argmax())] if wi in layers and len(layers[wi]) else '?'} "
                    "(a crossing the layer moves could not clear or an "
                    "over-capacity corridor; move the part or widen the corridor)"
                )
                polylines.pop(wi, None)
                layers.pop(wi, None)
                demoted += 1
    # emit off the final wire state on a layout at its own granularity
    final_layout = beliefs.build_layout(
        problem,
        [polylines[wi] if wi in polylines else np.zeros((0, 2)) for wi in order],
        piece_mm=1e9,
    )
    final_assign = _assign_from(final_layout, layers)
    rres = to_realize_result(
        problem,
        final_layout,
        final_layout.points,
        final_assign,
        unrouted=unrouted,
        reasons=reasons,
    )
    over: list[str] = []
    if last:
        over = prseed.capacity_violations(
            problem,
            last["seeded"],
            [polylines.get(wi, np.zeros((0, 2))) for wi in order],
            [
                int(np.bincount(layers[wi]).argmax())
                if wi in layers and len(layers[wi])
                else -1
                for wi in order
            ],
        )
    if problem.dyn.n_parts and len(start_pose) == problem.dyn.n_parts:
        dpos = problem.dyn.pose0[:, :2] - start_pose[:, :2]
        dth = problem.dyn.pose0[:, 2] - start_pose[:, 2]
        dth = (dth + np.pi) % (2 * np.pi) - np.pi
        travel = float(np.linalg.norm(dpos, axis=1).mean())
        turn = float(np.degrees(np.abs(dth)).mean())
    else:
        travel = turn = 0.0
    wall = time.monotonic() - t_start
    return V1Run(
        ir=ir,
        problem=problem,
        result=rres,
        polylines=polylines,
        layers=layers,
        unrouted=tuple(sorted(unrouted)),
        reasons=reasons,
        entropy_hist=entropy_hist,
        evals=stats.evals,
        relax_seconds=stats.seconds,
        wall_s=wall,
        blocked_frac=(stats.blocked / stats.steps) if stats.steps else 0.0,
        n_spans=final_layout.n_spans,
        via_count=len(rres.vias),
        wire_mm=sum(t.length_mm for t in rres.tracks),
        rounds_run=rounds_run,
        levels=levels,
        sugar=sugar,
        cleanup_kept=cleanup_kept,
        part_travel_mm=travel,
        part_turn_deg=turn,
        state=finish.snapshot(ir, problem, polylines, layers),
        over_capacity=over,
        deadline_hit=deadline_hit,
        start_pose=start_pose,
        crossings_hist=crossings_hist,
        demoted=demoted,
    )


def _layers_by_arclength(
    old_poly: np.ndarray, old_layers: np.ndarray, new_poly: np.ndarray
) -> np.ndarray:
    """Per-piece layers for ``new_poly`` taken from ``old_poly``'s pieces
    by normalised arclength midpoint."""

    def cum(poly: np.ndarray) -> np.ndarray:
        d = np.linalg.norm(np.diff(poly, axis=0), axis=1)
        c = np.concatenate(([0.0], np.cumsum(d)))
        return c / (c[-1] if c[-1] > 0 else 1.0)

    oc = cum(old_poly)
    nc = cum(new_poly)
    mid = (nc[:-1] + nc[1:]) / 2.0
    out = np.zeros(len(new_poly) - 1, dtype=np.int64)
    for p, m in enumerate(mid):
        k = int(np.searchsorted(oc, m, side="right") - 1)
        out[p] = int(old_layers[min(max(k, 0), len(old_layers) - 1)])
    return out

"""Slice-1 driver: 4a seed → outer rounds of (4b beliefs → 4a re-insert
→ 4c relax → CEM) → best-sample emit + measurements. The layer loop's
settle/oscillate behaviour, swept-check block rate and evals/s are the
reportable numbers — this module collects them, asserts nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from precis.pcb.pr import beliefs
from precis.pcb.pr import seed as prseed
from precis.pcb.pr.beliefs import Layout
from precis.pcb.pr.emit import to_realize_result
from precis.pcb.pr.relax import Relaxer, RelaxParams, RelaxStats
from precis.pcb.pr.seed import SeedResult
from precis.pcb.pr.state import PrProblem


@dataclass(frozen=True, slots=True)
class PrParams:
    """Every provisional parameter the report must name."""

    K: int = 16
    rounds: int = 6
    piece_mm: float = 2.0  # via sites every 2 mm
    crowd_mm: float = 0.6  # crowding radius for the P pair table
    pair_cost_mm: float = 2.0  # = 2 vias
    t0: float = 2.0  # belief temperature, linear anneal
    t1: float = 0.3
    elite_frac: float = 0.25
    cem_alpha: float = 0.7
    relax: RelaxParams = field(default_factory=RelaxParams)


@dataclass(slots=True)
class PrRun:
    problem: PrProblem
    seeded: SeedResult
    layout: Layout
    q: np.ndarray
    result: object  # RealizeResult
    unrouted: tuple[int, ...]
    entropy: float
    entropy_hist: list[float]
    evals: int
    relax_seconds: float
    wall_s: float
    blocked_frac: float
    n_segments: int
    over_capacity: list[str]
    via_count: int
    wire_mm: float


def run(problem: PrProblem, params: PrParams | None = None) -> PrRun:
    import torch  # lazy: the host venv has none

    prm = params or PrParams()
    torch.manual_seed(0)
    gen = np.random.default_rng((problem.ctx and 0) or 0)
    t0 = time.monotonic()
    seeded = prseed.seed(problem)
    polylines = [s.polyline for s in seeded.seeds]
    layout = beliefs.build_layout(problem, polylines, piece_mm=prm.piece_mm)
    q = beliefs.init_q(layout, problem.n_layers)
    unrouted = set(seeded.unrouted)
    relaxer = Relaxer(problem, layout, prm.relax)
    stats = RelaxStats()
    entropy_hist: list[float] = []
    best = None
    assign_np = None
    for rnd in range(prm.rounds):
        T = prm.t0 + (prm.t1 - prm.t0) * rnd / max(1, prm.rounds - 1)
        pairs = beliefs.pair_table(problem, layout, crowd_mm=prm.crowd_mm)
        q = beliefs.update_q(
            problem,
            layout,
            q,
            *pairs,
            temperature=T,
            via_cost_mm=prm.relax.via_cost_mm,
            pair_cost_mm=prm.pair_cost_mm,
        )
        entropy_hist.append(beliefs.belief_entropy(q))
        assign_np = beliefs.sample_layers(q, prm.K, gen)
        # pin forced spans (single-layer-allowed rows) to their layer
        fixed = layout.span_allowed.sum(1) == 1
        assign_np[:, fixed] = np.argmax(layout.span_allowed[fixed], axis=1)[None, :]
        # 4a re-insert spans whose consensus layer left the seed's
        consensus = np.argmax(q, axis=1)
        newlines = list(polylines)
        changed = False
        for wi, s in enumerate(seeded.seeds):
            if wi in unrouted or len(s.polyline) < 2:
                continue
            tgt = np.array(
                [consensus[p] for p in np.nonzero(layout.piece_wire == wi)[0]]
            )
            poly = prseed.refunnel(problem, seeded, wi, tgt, prm.piece_mm)
            cur = prseed.subdivide(
                np.asarray(s.polyline, dtype=np.float64), prm.piece_mm
            )
            if not np.array_equal(np.asarray(poly), cur):
                newlines[wi] = poly
                changed = True
        if changed:
            layout = beliefs.build_layout(problem, newlines, piece_mm=prm.piece_mm)
            q = beliefs.init_q(layout, problem.n_layers)
            polylines = newlines
            relaxer = Relaxer(problem, layout, prm.relax)
            assign_np = beliefs.sample_layers(q, prm.K, gen)
            fixed = layout.span_allowed.sum(1) == 1
            assign_np[:, fixed] = np.argmax(layout.span_allowed[fixed], axis=1)[None, :]
        pos, E, parts, st = relaxer.run(assign_np)
        stats.evals += st.evals
        stats.blocked += st.blocked
        stats.dropped += st.dropped
        stats.steps += st.steps
        stats.seconds += st.seconds
        scores = (
            E.numpy()
            + prm.relax.stiff_max
            * relaxer.violations(pos, torch.tensor(assign_np)).numpy()
        )
        q = beliefs.cem_update(
            q,
            assign_np,
            scores,
            elite_frac=prm.elite_frac,
            alpha=prm.cem_alpha,
            allowed=layout.span_allowed,
        )
        k_best = int(np.argmin(scores))
        if best is None or scores[k_best] < best[0]:
            best = (
                float(scores[k_best]),
                pos[k_best].numpy().copy(),
                assign_np[k_best].copy(),
                layout,
            )
    # capacity check on the best sample's final geometry — under the
    # layout that sample was relaxed on (the layout may have been
    # rebuilt in a later round)
    assert best is not None
    best_pos, best_assign, best_layout = best[1], best[2], best[3]
    wires_poly: list[np.ndarray] = []
    layers_of: list[int] = []
    for wi in range(len(problem.wires)):
        pts = best_pos[best_layout.wire_of_point == wi]
        wires_poly.append(pts)
        pcs = np.nonzero(best_layout.piece_wire == wi)[0]
        layers_of.append(
            int(np.bincount(best_assign[pcs]).argmax()) if len(pcs) else -1
        )
    over = prseed.capacity_violations(problem, seeded, wires_poly, layers_of)
    rres = to_realize_result(
        problem,
        best_layout,
        best_pos,
        best_assign,
        unrouted=unrouted,
        reasons={i: seeded.seeds[i].reason or "" for i in unrouted},
    )
    wall = time.monotonic() - t0
    return PrRun(
        problem=problem,
        seeded=seeded,
        layout=layout,
        q=q,
        result=rres,
        unrouted=tuple(sorted(unrouted)),
        entropy=entropy_hist[-1] if entropy_hist else 0.0,
        entropy_hist=entropy_hist,
        evals=stats.evals,
        relax_seconds=stats.seconds,
        wall_s=wall,
        blocked_frac=(stats.blocked / stats.steps) if stats.steps else 0.0,
        n_segments=best_layout.n_spans,
        over_capacity=over,
        via_count=len(rres.vias),
        wire_mm=sum(t.length_mm for t in rres.tracks),
    )

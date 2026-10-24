"""A 4d repair move for the sampler's leftovers: same-layer crossings.

4a seeds every wire crossing-free on ONE layer; 4b then decides layers
per span, and two spans the sampler puts on the same layer may cross in
projection. Relaxation cannot undo a crossing of two polylines (moving a
vertex across the other wire crosses the neighbouring piece instead —
the swept check refuses it, rightly), so a crossing that survives the
sample is a topology defect, not a geometry one. The cheapest discrete
fix is a layer move: push the crossing run of one wire to another
allowed layer where it crosses fewer pieces (two vias), greedily, until
no move helps. The next round's 4a re-funnels the moved runs on their
new layer (``pending_targets`` in :mod:`precis.pcb.pr.v1`).
"""

from __future__ import annotations

import numpy as np

from precis.pcb.pr.beliefs import Layout


def _cross(
    a0: np.ndarray, a1: np.ndarray, b0: np.ndarray, b1: np.ndarray
) -> np.ndarray:
    def c2(ux, uy, vx, vy):
        return ux * vy - uy * vx

    d = a1 - a0
    e = b1 - b0
    o1 = c2(d[:, 0], d[:, 1], b0[:, 0] - a0[:, 0], b0[:, 1] - a0[:, 1])
    o2 = c2(d[:, 0], d[:, 1], b1[:, 0] - a0[:, 0], b1[:, 1] - a0[:, 1])
    o3 = c2(e[:, 0], e[:, 1], a0[:, 0] - b0[:, 0], a0[:, 1] - b0[:, 1])
    o4 = c2(e[:, 0], e[:, 1], a1[:, 0] - b0[:, 0], a1[:, 1] - b0[:, 1])
    return (o1 * o2 < 0.0) & (o3 * o4 < 0.0)


def crossing_pairs(
    layout: Layout, pos: np.ndarray, pair_i: np.ndarray, pair_j: np.ndarray
) -> np.ndarray:
    """Boolean mask over the candidate pairs: the two pieces properly
    cross in projection (layer-blind)."""
    if not len(pair_i):
        return np.zeros(0, dtype=bool)
    a0 = pos[layout.piece_p0[pair_i]]
    a1 = pos[layout.piece_p1[pair_i]]
    b0 = pos[layout.piece_p0[pair_j]]
    b1 = pos[layout.piece_p1[pair_j]]
    return _cross(a0, a1, b0, b1)


def _runs(layout: Layout, assign: np.ndarray, piece: int) -> np.ndarray:
    """Piece ids of the maximal same-layer run containing ``piece``."""
    w = layout.piece_wire[piece]
    pcs = np.flatnonzero(layout.piece_wire == w)
    k = int(np.flatnonzero(pcs == piece)[0])
    layer = assign[piece]
    lo = k
    while lo > 0 and assign[pcs[lo - 1]] == layer:
        lo -= 1
    hi = k
    while hi + 1 < len(pcs) and assign[pcs[hi + 1]] == layer:
        hi += 1
    return pcs[lo : hi + 1]


def uncross_by_layer(
    layout: Layout,
    pos: np.ndarray,
    assign: np.ndarray,
    pair_i: np.ndarray,
    pair_j: np.ndarray,
    *,
    n_layers: int,
    max_moves: int = 200,
) -> tuple[np.ndarray, int, int]:
    """Greedy layer moves on ``assign`` (a copy is returned) until no
    same-layer crossing can be reduced. Returns ``(assign, crossings
    before, crossings after)``."""
    assign = assign.copy()
    geo = crossing_pairs(layout, pos, pair_i, pair_j)
    cand_i, cand_j = pair_i[geo], pair_j[geo]

    def same_layer_count() -> int:
        return int((assign[cand_i] == assign[cand_j]).sum())

    before = same_layer_count()
    if before == 0:
        return assign, 0, 0
    # pieces each piece crosses (layer-blind), for scoring a run's move
    partners: dict[int, list[int]] = {}
    for a, b in zip(cand_i.tolist(), cand_j.tolist(), strict=True):
        partners.setdefault(a, []).append(b)
        partners.setdefault(b, []).append(a)
    moves = 0
    improved = True

    def movable(run: np.ndarray) -> bool:
        """A run may change layer only where its two boundary points can
        host a via (wire ends excepted: the pad is the terminal)."""
        w = layout.piece_wire[run[0]]
        pcs = np.flatnonzero(layout.piece_wire == w)
        start = int(layout.piece_p0[run[0]])
        end = int(layout.piece_p1[run[-1]])
        ok = True
        if run[0] != pcs[0]:
            ok &= bool(layout.via_site_ok[start])
        if run[-1] != pcs[-1]:
            ok &= bool(layout.via_site_ok[end])
        return ok

    while improved and moves < max_moves:
        improved = False
        bad = np.flatnonzero(assign[cand_i] == assign[cand_j])
        for k in bad:
            for piece in (int(cand_i[k]), int(cand_j[k])):
                run = _runs(layout, assign, piece)
                if not movable(run):
                    continue
                cur = int(assign[piece])
                allowed = np.flatnonzero(layout.span_allowed[run].all(0))
                best_l, best_c = cur, _run_cost(run, cur, assign, partners)
                for l in allowed:
                    if l == cur:
                        continue
                    c = _run_cost(run, int(l), assign, partners)
                    if c < best_c:
                        best_l, best_c = int(l), c
                if best_l != cur:
                    assign[run] = best_l
                    moves += 1
                    improved = True
                    break
            if improved:
                break
    return assign, before, same_layer_count()


def _run_cost(
    run: np.ndarray, layer: int, assign: np.ndarray, partners: dict[int, list[int]]
) -> int:
    """Same-layer crossings the run would have on ``layer``."""
    n = 0
    for p in run.tolist():
        for q in partners.get(p, ()):
            if q not in run and assign[q] == layer:
                n += 1
    return n

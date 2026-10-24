"""4b layer beliefs for the slice-1 prototype (spec §Method 4b/4d).

State ``Q[spans, L]``: each wire's polyline is cut into span pieces of
≤ ``piece_mm`` — every interior vertex a candidate via site — and each
span's layer is a categorical belief. Two batched updates alternate at
annealing temperature ``T``: an exact forward–backward pass along each
wire's chain (every placement of vias weighed at once, transition cost
= the via price) and a mean-field step across wires
(``Q ← softmax(−(U + P·Q)/T)``). K hard assignments are drawn for 4c;
the relaxed samples' energies feed the CEM update (v1's learner).

``P[s,t]`` is a static pair table from the CURRENT geometry: two
different-net spans whose pieces cross or sit inside the crowding radius
owe each other, on a shared layer, the cheaper of a detour and two vias
— slice 1 prices that at ``2·via_cost_mm`` (the cap); across layers the
cost is zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from precis.pcb.pr.state import PrProblem


@dataclass(frozen=True, slots=True)
class Layout:
    """Flat SoA over all wires' polylines. ``points`` are shared across
    samples; the relaxer keeps its own (K, N, 2) copy."""

    points: np.ndarray  # (N, 2)
    wire_of_point: np.ndarray  # (N,) int
    free: np.ndarray  # (N,) bool — endpoints of each wire are pinned
    piece_wire: np.ndarray  # (P,) wire idx
    piece_p0: np.ndarray  # (P,) point idx
    piece_p1: np.ndarray  # (P,) point idx
    span_allowed: np.ndarray  # (P, L) bool
    via_site_ok: np.ndarray  # (N,) bool — interior vertex may host a via
    wire_of_piece: np.ndarray  # alias kept for readability = piece_wire
    #: (W,) False for a wire with no polyline (unrouted): its stub pieces
    #: take part in no pair, pull nothing and anchor to no pad.
    wire_active: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))

    @property
    def n_spans(self) -> int:
        return len(self.piece_wire)


def _point_obstacle_clear(problem: PrProblem, p: np.ndarray, keep_mm: float) -> bool:
    """Via-site admissibility: the point sits at least ``keep_mm`` off
    every obstacle's copper (via-in-pad is net-agnostic)."""
    obs = problem.obs
    if len(obs.seg_xy):
        a, b = obs.seg_xy[:, :2], obs.seg_xy[:, 2:]
        d = b - a
        den = (d * d).sum(1).clip(1e-12)
        t = (((p - a) * d).sum(1) / den).clip(0.0, 1.0)
        dist = np.linalg.norm(p - (a + t[:, None] * d), axis=1) - obs.seg_r
        if dist.min() < keep_mm:
            return False
    if len(obs.box_c):
        d = p - obs.box_c
        x = d[:, 0] * obs.box_cs[:, 0] + d[:, 1] * obs.box_cs[:, 1]
        y = -d[:, 0] * obs.box_cs[:, 1] + d[:, 1] * obs.box_cs[:, 0]
        q = np.stack([x, y], axis=1)
        dist = np.linalg.norm(np.maximum(np.abs(q) - obs.box_hw, 0.0), axis=1)
        if dist.min() < keep_mm:
            return False
    return True


def build_layout(
    problem: PrProblem,
    polylines: list[np.ndarray],
    *,
    piece_mm: float = 2.0,
) -> Layout:
    """Cut each wire's polyline into ≤``piece_mm`` span pieces; interior
    vertices are the via-site candidates, cleared of pad keep-out."""
    from precis.pcb.pr.seed import subdivide

    active = np.array([len(p) >= 2 for p in polylines], dtype=bool)
    pts: list[np.ndarray] = []
    wop: list[int] = []
    free: list[bool] = []
    site_ok: list[bool] = []
    pwire: list[int] = []
    allowed: list[int] = []  # lmask per piece
    keep = problem.via_dia_mm / 2.0 + problem.clearance_mm
    for wi, poly in enumerate(polylines):
        poly = subdivide(np.asarray(poly, dtype=np.float64), piece_mm)
        if len(poly) < 2:
            # an unrouted wire is a zero-length stub at its own pad: it
            # keeps the table's shape without acting as a phantom wire
            # (slice 1 relaxed a straight line here, which blocked others)
            poly = np.asarray([problem.wires[wi].a, problem.wires[wi].a])
        w = problem.wires[wi]
        for i, p in enumerate(poly):
            pts.append(p)
            wop.append(wi)
            free.append(0 < i < len(poly) - 1)
            site_ok.append(
                0 < i < len(poly) - 1 and _point_obstacle_clear(problem, p, keep)
            )
        for j in range(len(poly) - 1):
            pwire.append(wi)
            # end spans sit on their pad's layer (the stub), interior
            # spans on the net's routable layers (class lock ∩ signal)
            if j == 0:
                mask = w.a_lmask
            elif j == len(poly) - 2:
                mask = w.b_lmask
            else:
                mask = w.route_lmask
            allowed.append(mask)
    points = np.asarray(pts, dtype=np.float64)
    # Piece j of wire w joins consecutive points of that wire.
    p0: list[int] = []
    p1: list[int] = []
    idx = 0
    for n in np.bincount(wop, minlength=len(problem.wires)):
        for j in range(int(n) - 1):
            p0.append(idx + j)
            p1.append(idx + j + 1)
        idx += int(n)
    lmask = np.asarray(allowed, dtype=np.int64)
    L = problem.n_layers
    span_allowed = (lmask[:, None] & (1 << np.arange(L))).astype(bool)
    return Layout(
        points=points,
        wire_of_point=np.asarray(wop, dtype=np.int64),
        free=np.asarray(free, dtype=bool),
        piece_wire=np.asarray(pwire, dtype=np.int64),
        piece_p0=np.asarray(p0, dtype=np.int64),
        piece_p1=np.asarray(p1, dtype=np.int64),
        span_allowed=span_allowed,
        via_site_ok=np.asarray(site_ok, dtype=bool),
        wire_of_piece=np.asarray(pwire, dtype=np.int64),
        wire_active=active,
    )


def init_q(layout: Layout, n_layers: int) -> np.ndarray:
    q = np.where(layout.span_allowed, 1.0, 0.0)
    return q / q.sum(1, keepdims=True)


def _piece_arclength(layout: Layout) -> tuple[np.ndarray, np.ndarray]:
    """Per piece: normalised arclength ``[s0, s1]`` along its wire."""
    lens = np.linalg.norm(
        layout.points[layout.piece_p1] - layout.points[layout.piece_p0], axis=1
    )
    s0 = np.zeros(layout.n_spans)
    s1 = np.zeros(layout.n_spans)
    for w in np.unique(layout.piece_wire):
        idx = np.flatnonzero(layout.piece_wire == w)
        tot = float(lens[idx].sum()) or 1.0
        cum = np.concatenate(([0.0], np.cumsum(lens[idx]))) / tot
        s0[idx] = cum[:-1]
        s1[idx] = cum[1:]
    return s0, s1


def carry_q(old: Layout, old_q: np.ndarray, new: Layout) -> np.ndarray:
    """Beliefs for a rebuilt layout (wire refinement, moved endpoints):
    each new piece takes the belief of the old piece of the same wire
    that contains its arclength midpoint, masked and renormalised to the
    new piece's allowed layers; a wire new to the layout starts uniform.
    This is what keeps 4b's settled layers across refinement levels
    instead of re-deciding them from scratch (spec §Hierarchy, wire
    refinement as a second axis)."""
    q = init_q(new, new.span_allowed.shape[1])
    if old.n_spans == 0 or new.n_spans == 0:
        return q
    o0, o1 = _piece_arclength(old)
    n0, n1 = _piece_arclength(new)
    mid = (n0 + n1) / 2.0
    old_by_wire: dict[int, np.ndarray] = {}
    for w in np.unique(old.piece_wire):
        old_by_wire[int(w)] = np.flatnonzero(old.piece_wire == w)
    for p in range(new.n_spans):
        idx = old_by_wire.get(int(new.piece_wire[p]))
        if idx is None:
            continue
        inside = idx[(o0[idx] <= mid[p]) & (mid[p] <= o1[idx])]
        src = (
            int(inside[0])
            if len(inside)
            else int(idx[np.argmin(np.abs((o0[idx] + o1[idx]) / 2 - mid[p]))])
        )
        row = np.where(new.span_allowed[p], old_q[src], 0.0)
        tot = row.sum()
        if tot > 0:
            q[p] = row / tot
    return q


def pair_table(
    problem: PrProblem, layout: Layout, *, crowd_mm: float
) -> tuple[np.ndarray, np.ndarray]:
    """Different-wire piece pairs whose current centrelines cross or sit
    inside ``crowd_mm`` — the sparse support of ``P[s,t]``. Piece pairs
    sharing a wire never interact (same net merges)."""
    segs = np.stack(
        [layout.points[layout.piece_p0], layout.points[layout.piece_p1]], axis=1
    )
    pairs: list[tuple[int, int]] = []
    lo = np.minimum(segs[:, 0], segs[:, 1]) - crowd_mm
    hi = np.maximum(segs[:, 0], segs[:, 1]) + crowd_mm
    n = len(segs)
    for i in range(n):
        hit = np.nonzero(
            (lo[i + 1 :, 0] <= hi[i, 0])
            & (hi[i + 1 :, 0] >= lo[i, 0])
            & (lo[i + 1 :, 1] <= hi[i, 1])
            & (hi[i + 1 :, 1] >= lo[i, 1])
        )[0] + (i + 1)
        for j in hit:
            j = int(j)
            if layout.piece_wire[i] != layout.piece_wire[j]:
                a0, a1 = segs[i]
                b0, b1 = segs[j]
                # a proper crossing is a pair whatever the endpoint
                # distances (slice 1 tested only endpoint proximity, so
                # two long crossing spans never entered P and the sampler
                # put them on one layer)
                da, db = a1 - a0, b1 - b0
                o1 = da[0] * (b0[1] - a0[1]) - da[1] * (b0[0] - a0[0])
                o2 = da[0] * (b1[1] - a0[1]) - da[1] * (b1[0] - a0[0])
                o3 = db[0] * (a0[1] - b0[1]) - db[1] * (a0[0] - b0[0])
                o4 = db[0] * (a1[1] - b0[1]) - db[1] * (a1[0] - b0[0])
                if o1 * o2 < 0.0 and o3 * o4 < 0.0:
                    pairs.append((i, j))
                    continue
                d = b1 - b0
                den = float(d @ d) or 1e-12
                close = min(
                    np.linalg.norm(a0 - (b0 + np.clip((a0 - b0) @ d / den, 0, 1) * d)),
                    np.linalg.norm(a1 - (b0 + np.clip((a1 - b0) @ d / den, 0, 1) * d)),
                )
                d2 = a1 - a0
                den2 = float(d2 @ d2) or 1e-12
                close = min(
                    close,
                    *(
                        np.linalg.norm(
                            b - (a0 + np.clip((b - a0) @ d2 / den2, 0, 1) * d2)
                        )
                        for b in (b0, b1)
                    ),
                )
                if close <= crowd_mm:
                    pairs.append((i, j))
    if not pairs:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    a = np.asarray(pairs, dtype=np.int64)
    return a[:, 0], a[:, 1]


def update_q(
    problem: PrProblem,
    layout: Layout,
    q: np.ndarray,
    pair_i: np.ndarray,
    pair_j: np.ndarray,
    *,
    temperature: float,
    via_cost_mm: float,
    pair_cost_mm: float,
) -> np.ndarray:
    """One 4b round: mean-field unary costs, then exact forward–backward
    marginals per wire's span chain."""
    L = problem.n_layers
    T = max(temperature, 1e-3)
    # U[s,l]: span length (layer-independent) plus the pair crowding the
    # CURRENT beliefs put on layer l — the P·Q mean field.
    lens = np.linalg.norm(
        layout.points[layout.piece_p1] - layout.points[layout.piece_p0], axis=1
    )
    unary = np.repeat(lens[:, None], L, axis=1)
    if len(pair_i):
        for l in range(L):
            np.add.at(
                unary[:, l],
                pair_i,
                pair_cost_mm * q[pair_j, l],
            )
            np.add.at(
                unary[:, l],
                pair_j,
                pair_cost_mm * q[pair_i, l],
            )
    out = q.copy()
    # Per-wire forward–backward over its span chain: emissions from the
    # mean field, transitions priced at the via cost.
    order = np.argsort(layout.piece_p0, kind="stable")
    wseq = layout.piece_wire[order]
    allowed = layout.span_allowed
    eye = np.eye(L, dtype=bool)
    bounds: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(order) + 1):
        if i == len(order) or wseq[i] != wseq[start]:
            bounds.append((start, i))
            start = i
    for s0, s1 in bounds:
        idx = order[s0:s1]
        e = -unary[idx] / T
        e = np.where(allowed[idx], e, -np.inf)
        n = len(idx)
        alpha = np.zeros((n, L))
        alpha[0] = e[0]
        for i in range(1, n):
            trans = np.where(eye, 0.0, -via_cost_mm / T)  # (prev, cur)
            alpha[i] = e[i] + np.logaddexp.reduce(alpha[i - 1][:, None] + trans, axis=0)
        beta = np.zeros((n, L))
        for i in range(n - 2, -1, -1):
            trans = np.where(eye, 0.0, -via_cost_mm / T)
            beta[i] = np.logaddexp.reduce(
                e[i + 1][None, :] + trans + beta[i + 1][None, :], axis=1
            )
        post = alpha + beta
        post = np.where(allowed[idx], post, -np.inf)
        post -= post.max(axis=1, keepdims=True)
        p = np.where(allowed[idx], np.exp(post), 0.0)
        out[idx] = p / p.sum(1, keepdims=True)
    return out


def sample_layers(q: np.ndarray, K: int, gen: np.random.Generator) -> np.ndarray:
    """K hard assignments, each span drawn from its Q row."""
    P, L = q.shape
    u = gen.random((K, P))
    c = np.cumsum(q, axis=1)
    return (u[:, :, None] > c[None, :, :]).sum(-1).astype(np.int64)


def cem_update(
    q: np.ndarray,
    assignments: np.ndarray,
    scores: np.ndarray,
    *,
    elite_frac: float = 0.25,
    alpha: float = 0.7,
    floor: float = 0.02,
    allowed: np.ndarray | None = None,
) -> np.ndarray:
    """4d for v1: move Q toward the layer assignments of the best
    samples — no per-choice credit model."""
    K, P = assignments.shape
    L = q.shape[1]
    elite_n = max(1, round(K * elite_frac))
    elite = assignments[np.argsort(scores)[:elite_n]]
    freq = np.stack([(elite == l).mean(0) for l in range(L)], axis=1)
    out = (1 - alpha) * q + alpha * freq
    if allowed is not None:
        out = np.where(allowed, out, 0.0)
    out = np.clip(out, floor, 1.0)
    out = np.where(allowed, out, 0.0) if allowed is not None else out
    return out / out.sum(1, keepdims=True)


def belief_entropy(q: np.ndarray) -> float:
    """Mean per-span entropy (bits) — the settle/oscillate signal."""
    with np.errstate(divide="ignore", invalid="ignore"):
        h = -(q * np.where(q > 0, np.log2(q), 0.0)).sum(1)
    return float(h.mean())

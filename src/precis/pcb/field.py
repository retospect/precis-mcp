"""Unified place-and-route energy field — slice 0 skeleton.

Spec: ``docs/backlog/pcb-unified-place-route.md`` (§State, §Cost, §Derivative
route). One scalar energy ``E(z) = Σ λ_t T_t(z)`` over a packed continuous
state ``z``; each term returns ``(value, grad)`` with a hand-written analytic
gradient (numpy; no autodiff dependency). This slice ships only the plumbing
and two terms:

* **T1** connectivity pull — weighted-average (WA) wirelength per net on
  rotated pad centres, so every pad carries ``∂/∂θ``.
* **T4** courtyard clearance — ``Σ softplus_β(c − sd(poly_i, poly_j))²`` on
  shapely polygon distance (the vector clearance kernel replaces it later).

Nothing here is wired into ``optimize()``; :func:`check_gradient` is the
harness every later term's test reuses (dot-product, Taylor, central FD).

Frames. ``Part.pads`` / ``Part.court`` are in the part's rotating frame:
the world point of local ``p`` is ``R(θ)·p + (x, y)``. Mirroring for
bottom-side parts is the builder's job (it flips the local x before
handing the part over); ``Part.side`` only gates T4, since courtyards on
opposite sides do not interact.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import minimize
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points

_LOG = logging.getLogger(__name__)
_Z = np.ndarray  # packed state, shape (n_vars,)


@dataclass(frozen=True)
class Part:
    """One placed part: pad centres and courtyard in its rotating frame."""

    pads: np.ndarray  # (n_pads, 2) local pad centres
    court: np.ndarray  # (k, 2) local courtyard ring (open or closed)
    side: int = 0  # 0 top, 1 bottom
    fixed_xy: bool = False
    fixed_rot: bool = False
    cls: str | None = None  # JLCPCB spacing class (see SPACING_CLASSES), if known


@dataclass(frozen=True)
class FieldBoard:
    """Parts, nets and vias at their base pose (the fixed-variable values).

    ``nets[k]`` lists ``(part, pad)`` pairs. ``x0/y0/th0`` are the starting
    poses; ``vias0`` is ``(n_vias, 2)`` (carried in the state, unused by
    T1/T4)."""

    parts: tuple[Part, ...]
    nets: tuple[tuple[tuple[int, int], ...], ...]
    x0: np.ndarray
    y0: np.ndarray
    th0: np.ndarray
    vias0: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))


class Layout:
    """Packs the free variables of a :class:`FieldBoard` into one vector.

    Order: free ``x``/``y`` pairs per part, free ``θ`` per part, then the
    via ``(x, y)`` pairs. Fixed variables (``fixed_xy``/``fixed_rot``) keep
    their base values and have no slot, so no gradient."""

    def __init__(self, board: FieldBoard):
        self.board = board
        n = len(board.parts)
        self.free_xy = np.array([not p.fixed_xy for p in board.parts], dtype=bool)
        self.free_rot = np.array([not p.fixed_rot for p in board.parts], dtype=bool)
        self.n_vias = len(board.vias0)
        self._nxy = int(self.free_xy.sum())
        self._nrot = int(self.free_rot.sum())
        self.size = 2 * self._nxy + self._nrot + 2 * self.n_vias
        self.n_parts = n

    def pack(self) -> _Z:
        b = self.board
        return np.concatenate(
            [
                b.x0[self.free_xy],
                b.y0[self.free_xy],
                b.th0[self.free_rot],
                np.asarray(b.vias0, dtype=float).reshape(-1),
            ]
        ).astype(float)

    def unpack(self, z: _Z) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Full ``(x, y, θ, vias)`` arrays with fixed values filled in."""
        b = self.board
        x, y, th = (
            b.x0.astype(float).copy(),
            b.y0.astype(float).copy(),
            b.th0.astype(float).copy(),
        )
        a = self._nxy
        x[self.free_xy] = z[:a]
        y[self.free_xy] = z[a : 2 * a]
        th[self.free_rot] = z[2 * a : 2 * a + self._nrot]
        vias = z[2 * a + self._nrot :].reshape(-1, 2).copy()
        return x, y, th, vias

    def pack_from(self, x: np.ndarray, y: np.ndarray, th: np.ndarray) -> _Z:
        """Full per-part poses → packed state (vias stay at their base)."""
        b = self.board
        return np.concatenate(
            [
                x[self.free_xy],
                y[self.free_xy],
                th[self.free_rot],
                np.asarray(b.vias0, dtype=float).reshape(-1),
            ]
        ).astype(float)

    def scatter(self, gx: np.ndarray, gy: np.ndarray, gth: np.ndarray) -> _Z:
        """Per-part full gradients → packed gradient (vias get zero)."""
        return np.concatenate(
            [
                gx[self.free_xy],
                gy[self.free_xy],
                gth[self.free_rot],
                np.zeros(2 * self.n_vias),
            ]
        )


class Term:
    """Energy term: ``value_grad(z) -> (value, grad)`` over the packed state."""

    def __init__(self, layout: Layout):
        self.layout = layout

    def value_grad(self, z: _Z) -> tuple[float, _Z]:  # pragma: no cover - interface
        raise NotImplementedError


def _world(part: Part, local: np.ndarray, x: float, y: float, th: float) -> np.ndarray:
    c, s = np.cos(th), np.sin(th)
    return np.stack(
        [x + c * local[:, 0] - s * local[:, 1], y + s * local[:, 0] + c * local[:, 1]],
        axis=1,
    )


def _softplus(u: float, beta: float) -> float:
    return float(np.logaddexp(0.0, beta * u)) / beta


def _sigmoid(u: float, beta: float) -> float:
    return 0.5 * (1.0 + float(np.tanh(0.5 * beta * u)))


class ConnectivityPull(Term):
    """T1: per-net WA wirelength on rotated pad centres, x and y.

    ``W(v) = Σ v e^{v/γ}/Σ e^{v/γ} − Σ v e^{−v/γ}/Σ e^{−v/γ}`` — a smooth
    stand-in for the net's half-perimeter span. ``weights`` scales each net
    (supply/ground/pour nets are the caller's to exclude, spec §Cost)."""

    def __init__(
        self, layout: Layout, gamma: float = 0.5, weights: Sequence[float] | None = None
    ):
        super().__init__(layout)
        self.gamma = float(gamma)
        nets = layout.board.nets
        self.weights = (
            np.ones(len(nets)) if weights is None else np.asarray(weights, float)
        )

    def _wa(self, v: np.ndarray) -> tuple[float, np.ndarray]:
        g = self.gamma
        ep = np.exp((v - v.max()) / g)
        en = np.exp(-(v - v.min()) / g)
        sp, sn = ep.sum(), en.sum()
        mp, mn = (v * ep).sum() / sp, (v * en).sum() / sn
        dp = ep / sp * (1.0 + (v - mp) / g)
        dn = en / sn * (1.0 - (v - mn) / g)
        return float(mp - mn), dp - dn

    def value_grad(self, z: _Z) -> tuple[float, _Z]:
        lay = self.layout
        x, y, th, _ = lay.unpack(z)
        parts = lay.board.parts
        gx, gy, gth = np.zeros_like(x), np.zeros_like(y), np.zeros_like(th)
        total = 0.0
        for net, w in zip(lay.board.nets, self.weights, strict=True):
            if len(net) < 2 or w == 0.0:
                continue
            pts = np.array(
                [
                    _world(parts[i], parts[i].pads[k : k + 1], x[i], y[i], th[i])[0]
                    for i, k in net
                ]
            )
            vx, dx = self._wa(pts[:, 0])
            vy, dy = self._wa(pts[:, 1])
            total += w * (vx + vy)
            for (i, _k), px, py, ax, ay in zip(
                net, pts[:, 0], pts[:, 1], dx, dy, strict=True
            ):
                gx[i] += w * ax
                gy[i] += w * ay
                # d(pad)/dθ = (−(Y−y), X−x)
                gth[i] += w * (-ax * (py - y[i]) + ay * (px - x[i]))
        return total, lay.scatter(gx, gy, gth)


class CourtyardClearance(Term):
    """T4: ``Σ_{i<j} softplus_β(c − sd(poly_i, poly_j))²``, same side only.

    ``sd`` is shapely polygon distance when disjoint, and minus the deepest
    vertex penetration when they overlap (the witness pair is a vertex of
    one polygon and the nearest boundary point of the other). Witness points
    are treated as rigidly attached to their bodies — the envelope-theorem
    gradient — so the term is piecewise smooth, with kinks where the witness
    pair switches."""

    def __init__(
        self,
        layout: Layout,
        clearance: float = 0.40,
        beta: float = 8.0,
        pair_min: dict[tuple[int, int], float] | None = None,
    ):
        super().__init__(layout)
        self.c = float(clearance)
        self.beta = float(beta)
        #: per-pair required clearance, keyed ``(i, j)`` with ``i < j``;
        #: pairs not listed use ``clearance``
        self.pair_min = pair_min or {}

    @staticmethod
    def _convex_ring(ring: np.ndarray) -> np.ndarray | None:
        """``ring`` as a CCW open vertex array when it is convex, else None."""
        v = ring[:-1] if len(ring) > 1 and np.allclose(ring[0], ring[-1]) else ring
        if len(v) < 3:
            return None
        e = np.roll(v, -1, axis=0) - v
        cross = (
            e[:, 0] * np.roll(e, -1, axis=0)[:, 1]
            - e[:, 1] * np.roll(e, -1, axis=0)[:, 0]
        )
        if np.all(cross >= -1e-12):
            return v
        if np.all(cross <= 1e-12):
            return v[::-1]
        return None

    def _sat(
        self, va: np.ndarray, vb: np.ndarray, ta: np.ndarray, tb: np.ndarray
    ) -> tuple[float, np.ndarray, np.ndarray] | None:
        """Penetration of two overlapping CONVEX rings by SAT: ``sd = max`` over
        every outward edge normal ``n`` of ``min_q n·(q − p_edge)`` (≤ 0 when
        they overlap). Returns ``(sd, grad_a, grad_b)`` — ``∂sd`` wrt
        ``(x, y, θ)`` of each body, including the rotation of the normal when
        the axis belongs to that body (a plus-sign crossing has no vertex
        inside the other, so the vertex-depth rule gives it nothing)."""
        best: tuple[float, np.ndarray, np.ndarray, np.ndarray, int] | None = None
        for owner, (vo, vq) in enumerate(((va, vb), (vb, va))):
            e = np.roll(vo, -1, axis=0) - vo
            n = np.stack([e[:, 1], -e[:, 0]], axis=1)
            n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-300)
            proj = vq[None, :, :] - vo[:, None, :]  # (edges, q, 2)
            s = np.einsum("kqd,kd->kq", proj, n)
            qi = s.argmin(axis=1)
            sk = s[np.arange(len(n)), qi]
            k = int(sk.argmax())
            if best is None or sk[k] > best[0]:
                best = (float(sk[k]), n[k], vo[k], vq[qi[k]], owner)
        assert best is not None
        sd, n, p, q, owner = best
        t_o, t_q = (ta, tb) if owner == 0 else (tb, ta)

        def perp(r: np.ndarray) -> np.ndarray:
            return np.array([-r[1], r[0]])

        g_q = np.array([n[0], n[1], float(n @ perp(q - t_q))])
        g_o = np.array([-n[0], -n[1], float(perp(n) @ (q - p) - n @ perp(p - t_o))])
        return (sd, g_o, g_q) if owner == 0 else (sd, g_q, g_o)

    def _signed(
        self,
        pa: Polygon,
        pb: Polygon,
        ra: np.ndarray,
        rb: np.ndarray,
        ta: np.ndarray,
        tb: np.ndarray,
    ) -> tuple[float, np.ndarray, np.ndarray]:
        """``(sd, ∂sd/∂pose_a, ∂sd/∂pose_b)``; each gradient is ``(x, y, θ)``."""

        def point_pair(sign: float, a: np.ndarray, b: np.ndarray):
            diff = b - a
            nrm = float(np.linalg.norm(diff))
            if nrm < 1e-12:
                z3 = np.zeros(3)
                return z3, z3
            u = sign * diff / nrm  # ∂sd/∂b; ∂sd/∂a = −u

            def pose_grad(g: np.ndarray, pt: np.ndarray, t: np.ndarray) -> np.ndarray:
                return np.array(
                    [g[0], g[1], -g[0] * (pt[1] - t[1]) + g[1] * (pt[0] - t[0])]
                )

            return pose_grad(-u, a, ta), pose_grad(u, b, tb)

        if not pa.intersects(pb):
            # The distance is attained at a VERTEX of one polygon and the
            # nearest boundary point of the other. Parallel facing edges (every
            # axis-aligned courtyard pair) make shapely's witness an arbitrary
            # point on the overlap, which has the wrong rotation derivative, so
            # witness the vertices; ties share the gradient (a subgradient).
            cands: list[tuple[float, np.ndarray, np.ndarray, bool]] = []
            for src, dst, flip in ((pa, pb, False), (pb, pa, True)):
                for v in list(src.exterior.coords)[:-1]:
                    pt = Point(v)
                    qv = nearest_points(pt, dst)[1]
                    cands.append(
                        (
                            float(pt.distance(dst)),
                            np.array(v),
                            np.array(qv.coords[0]),
                            flip,
                        )
                    )
            dmin = min(c[0] for c in cands)
            tied = [c for c in cands if c[0] <= dmin + 1e-9]
            ga, gb = np.zeros(3), np.zeros(3)
            for _d, vv, qq, flip in tied:
                pa_pt, pb_pt = (qq, vv) if flip else (vv, qq)
                pga, pgb = point_pair(1.0, pa_pt, pb_pt)
                ga += pga / len(tied)
                gb += pgb / len(tied)
            return dmin, ga, gb
        cva, cvb = self._convex_ring(ra), self._convex_ring(rb)
        if cva is not None and cvb is not None:
            res = self._sat(cva, cvb, ta, tb)
            if res is not None:
                return res
        depth = 0.0
        wa: np.ndarray | None = None
        wb: np.ndarray | None = None
        for src, dst, flip in ((pa, pb, False), (pb, pa, True)):
            ring = dst.exterior
            for v in list(src.exterior.coords)[:-1]:
                pt = Point(v)
                if not dst.contains(pt):
                    continue
                d = ring.distance(pt)
                if d > depth:
                    q = ring.interpolate(ring.project(pt))
                    vv, qq = np.array(v), np.array(q.coords[0])
                    depth = d
                    wa, wb = (qq, vv) if flip else (vv, qq)
        if wa is None or wb is None:
            # non-convex, edges cross, no vertex inside: no depth signal
            return 0.0, np.zeros(3), np.zeros(3)
        ga, gb = point_pair(-1.0, wa, wb)
        return -depth, ga, gb

    def value_grad(self, z: _Z) -> tuple[float, _Z]:
        lay = self.layout
        x, y, th, _ = lay.unpack(z)
        parts = lay.board.parts
        n = len(parts)
        rings = [_world(p, p.court, x[i], y[i], th[i]) for i, p in enumerate(parts)]
        polys = [Polygon(r) for r in rings]
        lo = np.array([r.min(axis=0) for r in rings])
        hi = np.array([r.max(axis=0) for r in rings])
        # softplus² is below 1e-12 of its scale past u = −30/β; cull there
        reach = self.c + 30.0 / self.beta
        gx, gy, gth = np.zeros(n), np.zeros(n), np.zeros(n)
        total = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                if parts[i].side != parts[j].side:
                    continue
                gap = np.maximum(lo[i] - hi[j], lo[j] - hi[i]).max()
                if gap > reach:
                    continue
                ti, tj = np.array([x[i], y[i]]), np.array([x[j], y[j]])
                sd, gi, gj = self._signed(
                    polys[i], polys[j], rings[i], rings[j], ti, tj
                )
                u = self.pair_min.get((i, j), self.c) - sd
                sp = _softplus(u, self.beta)
                total += sp * sp
                dE_dsd = -2.0 * sp * _sigmoid(u, self.beta)
                for idx, g in ((i, gi), (j, gj)):
                    gx[idx] += dE_dsd * g[0]
                    gy[idx] += dE_dsd * g[1]
                    gth[idx] += dE_dsd * g[2]
        return total, lay.scatter(gx, gy, gth)


class Energy(Term):
    """``Σ λ_t T_t`` over a list of ``(λ, term)`` pairs."""

    def __init__(self, layout: Layout, terms: Sequence[tuple[float, Term]]):
        super().__init__(layout)
        self.terms = list(terms)

    def value_grad(self, z: _Z) -> tuple[float, _Z]:
        v, g = 0.0, np.zeros(self.layout.size)
        for lam, t in self.terms:
            tv, tg = t.value_grad(z)
            v += lam * tv
            g += lam * tg
        return v, g


@dataclass(frozen=True)
class GradCheck:
    """Result of :func:`check_gradient`."""

    dot_rel_err: float  # |g·d − (f(z+hd)−f(z−hd))/2h| / scale
    taylor_orders: tuple[float, ...]  # log2 remainder ratios as h halves (≈2)
    fd_max_rel_err: float  # worst central-FD vs analytic over sampled vars


def check_gradient(
    f: Callable[[_Z], tuple[float, _Z]],
    z: _Z,
    *,
    rng: np.random.Generator,
    h: float = 1e-4,
    n_fd: int = 8,
) -> GradCheck:
    """Directional dot-product, Taylor-remainder and central-FD checks.

    Call away from kinks. The Taylor remainder ``|f(z+hd) − f − h g·d|``
    shrinks ×4 per halving of ``h`` when the gradient is right (order 2); a
    wrong gradient leaves it order 1."""
    v0, g = f(z)
    d = rng.standard_normal(z.size)
    d /= np.linalg.norm(d) or 1.0
    gd = float(g @ d)
    scale = max(abs(gd), 1e-8)
    cd = (f(z + h * d)[0] - f(z - h * d)[0]) / (2 * h)
    dot_err = abs(gd - cd) / scale
    hs = [4 * h, 2 * h, h]
    rem = [abs(f(z + hh * d)[0] - v0 - hh * gd) for hh in hs]
    orders = tuple(float(np.log2(rem[k] / max(rem[k + 1], 1e-300))) for k in range(2))
    idx = rng.choice(z.size, size=min(n_fd, z.size), replace=False)
    worst = 0.0
    for k in idx:
        e = np.zeros(z.size)
        e[k] = h
        fd = (f(z + e)[0] - f(z - e)[0]) / (2 * h)
        worst = max(worst, abs(fd - g[k]) / max(abs(g[k]), abs(fd), 1e-6))
    return GradCheck(dot_err, orders, worst)


class BoardEdge(Term):
    """T5 (slice 1): ``Σ_i softplus_β(lo − c_i)² + softplus_β(c_i − hi)²`` on
    part CENTRES against the engine's placement rectangle — the same box
    ``_placement_is_legal`` clamps centres to. The exact outline test stays in
    the legality gate; this term only pulls strays inward. Smooth; no ∂/∂θ."""

    def __init__(
        self,
        layout: Layout,
        lo: tuple[float, float],
        hi: tuple[float, float],
        beta: float = 8.0,
    ):
        super().__init__(layout)
        self.lo, self.hi, self.beta = lo, hi, float(beta)

    def value_grad(self, z: _Z) -> tuple[float, _Z]:
        lay = self.layout
        x, y, _th, _ = lay.unpack(z)
        gx, gy = np.zeros_like(x), np.zeros_like(y)
        total = 0.0
        for c, g, lo, hi in (
            (x, gx, self.lo[0], self.hi[0]),
            (y, gy, self.lo[1], self.hi[1]),
        ):
            for u, sgn in ((lo - c, -1.0), (c - hi, 1.0)):
                sp = np.logaddexp(0.0, self.beta * u) / self.beta
                total += float((sp * sp).sum())
                g += 2.0 * sp * 0.5 * (1.0 + np.tanh(0.5 * self.beta * u)) * sgn
        return total, lay.scatter(gx, gy, np.zeros_like(x))


class PadSpring(Term):
    """T6 (slice 1): rubber bands, ``Σ k·softplus_β(|P − Q| − d₀)²`` over
    ``(part i, pad a, part j, pad b, d₀)`` pairs — a decoupling cap's pad and
    the IC pin it serves. One-sided: slack inside ``d₀``."""

    def __init__(
        self,
        layout: Layout,
        springs: Sequence[tuple[int, int, int, int, float]],
        k: float = 1.0,
        beta: float = 8.0,
    ):
        super().__init__(layout)
        self.springs, self.k, self.beta = list(springs), float(k), float(beta)

    def value_grad(self, z: _Z) -> tuple[float, _Z]:
        lay = self.layout
        x, y, th, _ = lay.unpack(z)
        parts = lay.board.parts
        gx, gy, gth = np.zeros_like(x), np.zeros_like(y), np.zeros_like(th)
        total = 0.0
        for i, a, j, b, d0 in self.springs:
            p = _world(parts[i], parts[i].pads[a : a + 1], x[i], y[i], th[i])[0]
            q = _world(parts[j], parts[j].pads[b : b + 1], x[j], y[j], th[j])[0]
            diff = p - q
            d = float(np.linalg.norm(diff))
            if d < 1e-12:
                continue
            sp = _softplus(d - d0, self.beta)
            total += self.k * sp * sp
            g = 2.0 * self.k * sp * _sigmoid(d - d0, self.beta) * diff / d
            for idx, pt, gg in ((i, p, g), (j, q, -g)):
                gx[idx] += gg[0]
                gy[idx] += gg[1]
                gth[idx] += -gg[0] * (pt[1] - y[idx]) + gg[1] * (pt[0] - x[idx])
        return total, lay.scatter(gx, gy, gth)


# ── slice 2: JLCPCB pairwise SMD spacing ─────────────────────────────────

#: Minimum SMD component-to-component spacing, mm, by package class pair.
#: Source: https://jlcpcb.com/help/article/minimum-spacing-for-smd-components
#: (JLCPCB help center, read 2026-10-19; the page lists mm with mil in
#: brackets). The page renders as an upper-triangular matrix whose rows start
#: at the diagonal cell; each row below is ``(class, value)`` read left to
#: right from the diagonal. The table is symmetric. The page gives NO
#: body-to-board-edge rule, so none is encoded here.
SPACING_CLASSES = (
    "0201",
    "0402",
    "0603",
    "0805",
    "1206",
    "QFN",
    "QFP",
    "SOP",
    "SOT",
    "BGA",
)
_SPACING_ROWS: dict[str, tuple[float, ...]] = {
    "0201": (0.15, 0.15, 0.18, 0.18, 0.25, 1.0, 0.5, 0.4, 0.2, 1.0),
    "0402": (0.15, 0.18, 0.18, 0.25, 1.0, 0.5, 0.4, 0.2, 1.0),
    "0603": (0.18, 0.18, 0.25, 1.0, 0.5, 0.4, 0.2, 1.0),
    "0805": (0.18, 0.25, 1.0, 0.5, 0.4, 0.2, 1.0),
    "1206": (0.35, 1.0, 0.5, 0.4, 0.2, 1.0),
    "QFN": (1.0, 1.0, 1.0, 1.0, 1.5),
    "QFP": (1.25, 1.25, 1.0, 1.5),
    "SOP": (0.5, 0.4, 1.0),
    "SOT": (0.4, 1.0),
    "BGA": (2.0,),
}
JLCPCB_SPACING_MM: dict[frozenset[str], float] = {}
for _r, _row in _SPACING_ROWS.items():
    _k = SPACING_CLASSES.index(_r)
    assert len(_row) == len(SPACING_CLASSES) - _k, _r
    for _c, _v in zip(SPACING_CLASSES[_k:], _row, strict=True):
        JLCPCB_SPACING_MM[frozenset((_r, _c))] = _v


def pair_spacing(a: str | None, b: str | None) -> float | None:
    """The JLCPCB minimum spacing between two classes (symmetric), or ``None``
    when either class is unknown."""
    if a is None or b is None:
        return None
    return JLCPCB_SPACING_MM.get(frozenset((a, b)))


def classify_package(
    refdes: str, pads: np.ndarray, pad_w: np.ndarray, pad_h: np.ndarray
) -> str | None:
    """Best-effort :data:`SPACING_CLASSES` member from refdes prefix and pad
    geometry (the IR carries no footprint name). ``None`` when unsure — an
    unclassed part keeps the uniform fallback clearance, never a guess.

    Chips (two pads, R/C/L/D/F prefix) by the pad-to-pad outer length;
    SOT = 3–6 pads in a small box; SOP = two opposite pad rows; QFN/QFP =
    four pad rows, told apart by pad length; BGA = a square grid with
    several interior pads."""
    n = len(pads)
    if n == 0:
        return None
    prefix = re.match(r"[A-Za-z]+", refdes)
    pre = prefix.group(0).upper() if prefix else ""
    xs, ys = pads[:, 0], pads[:, 1]
    ext_x = float(np.ptp(xs) + pad_w.max())
    ext_y = float(np.ptp(ys) + pad_h.max())
    if n == 2 and pre in {"R", "C", "L", "D", "F", "FB", "LED"}:
        length = max(ext_x, ext_y)
        for cls, hi in (("0201", 1.25), ("0402", 2.0), ("0603", 2.7), ("0805", 3.6)):
            if length < hi:
                return cls
        return "1206"
    if n < 3 or pre not in {"U", "IC", "Q", "D", "V"}:
        return None
    if n <= 6 and max(ext_x, ext_y) <= 4.5:
        return "SOT"
    tol = 0.05
    on_x = (np.abs(xs - xs.min()) < tol) | (np.abs(xs - xs.max()) < tol)
    on_y = (np.abs(ys - ys.min()) < tol) | (np.abs(ys - ys.max()) < tol)
    interior = int((~on_x & ~on_y).sum())
    square = float(
        np.median(np.maximum(pad_w, pad_h) / np.maximum(np.minimum(pad_w, pad_h), 1e-6))
    )
    if n >= 16 and interior >= 4 and square < 1.3:
        return "BGA"
    if on_x.sum() + on_y.sum() >= n and interior <= 1 and n >= 8:
        rows_x = int((np.abs(xs - xs.min()) < tol).sum() >= 3) + int(
            (np.abs(xs - xs.max()) < tol).sum() >= 3
        )
        rows_y = int((np.abs(ys - ys.min()) < tol).sum() >= 3) + int(
            (np.abs(ys - ys.max()) < tol).sum() >= 3
        )
        if rows_x == 2 and rows_y == 2:
            return "QFP" if float(np.median(np.maximum(pad_w, pad_h))) >= 1.2 else "QFN"
        if rows_x == 2 or rows_y == 2:
            return "SOP"
    return None


def pair_minimums(
    parts: Sequence[Part], fallback: float, reserve: float = 0.0
) -> dict[tuple[int, int], float]:
    """Per-pair T4 clearance overrides: only the pairs whose JLCPCB table
    value EXCEEDS ``fallback`` (the body-to-body gap every other pair keeps),
    as ``table − reserve`` (``reserve`` is what the courtyards already add
    between two parts). The table can only RAISE a target: the engine's
    legality gate keeps its 0.40 mm per-part reserve (a 0.8 mm hull gap), so
    a table value below the fallback cannot be realised and only makes the
    polish propose placements the gate refuses. Relaxing the reserve to the
    table is an engine change that moves pinned boards, a separate decision.
    Pairs not returned use the term's own clearance unchanged. Keys are
    ``(i, j)`` with ``i < j``."""
    out: dict[tuple[int, int], float] = {}
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            table = pair_spacing(parts[i].cls, parts[j].cls)
            if table is not None and table > fallback:
                out[(i, j)] = table - reserve
    return out


# ── slice 1: placement polish on the engine's board ──────────────────────

#: Net names that keep a tree term in the spec (supply/ground), not a T1 pull.
_RAIL_RE = re.compile(
    r"^(\+?\d+(\.\d+)?V\d*|[ADP]?GND\w*|V(SS|CC|DD|BUS|IN|OUT|BAT|REF)\w*)$", re.I
)
#: T6 rubber-band slack: a served decoupling pad within this of its pin is free.
SUPPORT_D0_MM = 1.5
#: λ_T4 ramp: start loose so wirelength can pull, end firm so overlaps resolve.
LAMBDA_T4_RAMP = (0.25, 1.0, 4.0, 16.0)


@dataclass(frozen=True)
class PolishResult:
    applied: bool
    reason: str  # applied | unplaced | no_free_vars | no_gain | illegal | cost_worse | bad_gradient
    e_before: float
    e_after: float
    moved: tuple[int, ...]

    def summary(self) -> str:
        return (
            f"field_polish {self.reason}: E {self.e_before:.4f} -> "
            f"{self.e_after:.4f}, {len(self.moved)} parts moved"
        )


def board_from_engine(
    engine: Any,
) -> tuple[FieldBoard, list[tuple[int, int, int, int, float]]] | None:
    """The engine's board as a :class:`FieldBoard`; ``None`` if any part is
    unplaced. Frozen (no variable): fixed axes, hard-align operands and rigid
    group members. θ is the engine's clockwise-degree rotation negated into
    radians; bottom-side pads are mirrored in x, courtyards are not (the
    engine does not mirror them either)."""
    from precis.pcb import support_groups as sg

    ir = engine.ir
    n = ir.n_instances
    if not (np.isfinite(ir.inst_x).all() and np.isfinite(ir.inst_y).all()):
        return None
    hard_paired = {
        inst
        for a, b, spec in engine._measure_pairs
        if spec.metric == "align" and spec.hard and spec.weight > 0.0
        for inst in (a, b)
        if inst >= 0
    }
    free_xy, free_rot = set(engine._movable_xy), set(engine._movable_rot)
    pads_of: list[list[int]] = [[] for _ in range(n)]
    for p in range(len(ir.pin_instance)):
        pads_of[int(ir.pin_instance[p])].append(p)
    slot = {p: k for ps in pads_of for k, p in enumerate(ps)}
    parts = []
    for i in range(n):
        bottom = bool(ir.inst_bottom[i])
        pads = np.array(
            [
                [(-1.0 if bottom else 1.0) * float(ir.pin_dx[p]), float(ir.pin_dy[p])]
                for p in pads_of[i]
            ]
        ).reshape(-1, 2)
        frozen = len(engine._rigid_members(i)) > 1 or i in hard_paired
        parts.append(
            Part(
                pads=pads,
                court=np.array(engine._keepout_poly[i], dtype=float).reshape(-1, 2),
                side=int(bottom),
                fixed_xy=frozen or i not in free_xy,
                fixed_rot=frozen or i not in free_rot,
                cls=classify_package(
                    str(ir.instance_refdes[i]),
                    pads,
                    np.array([float(ir.pin_w[p]) for p in pads_of[i]]),
                    np.array([float(ir.pin_h[p]) for p in pads_of[i]]),
                ),
            )
        )
    by_net: dict[int, list[tuple[int, int]]] = {}
    for p in range(len(ir.pin_instance)):
        net = int(ir.pin_net[p])
        if net < 0:
            continue
        by_net.setdefault(net, []).append((int(ir.pin_instance[p]), slot[p]))
    nets = tuple(
        tuple(members)
        for net, members in sorted(by_net.items())
        if len({i for i, _ in members}) >= 2
        and len(members) <= 12
        and int(ir.net_plane_layers[net]) == 0
        and not _RAIL_RE.match(str(ir.net_name[net]))
    )
    springs: list[tuple[int, int, int, int, float]] = []
    for grp in sg.detect_support_groups(ir):
        if grp.kind != "support_cap":
            continue
        for m, served in zip(grp.members, grp.served, strict=True):
            for q in served:
                mine = [p for p in pads_of[m] if ir.pin_net[p] == ir.pin_net[q]]
                if mine:
                    springs.append(
                        (
                            m,
                            slot[mine[0]],
                            int(ir.pin_instance[q]),
                            slot[q],
                            SUPPORT_D0_MM,
                        )
                    )
    th0 = np.array(
        [-math.radians(0.0 if math.isnan(float(r)) else float(r)) for r in ir.inst_rot]
    )
    board = FieldBoard(
        parts=tuple(parts),
        nets=nets,
        x0=np.asarray(ir.inst_x, dtype=float).copy(),
        y0=np.asarray(ir.inst_y, dtype=float).copy(),
        th0=th0,
    )
    return board, springs


def polish_placement(
    engine: Any,
    *,
    max_iter: int = 60,
    lambda_t4: Sequence[float] = LAMBDA_T4_RAMP,
    gamma: float = 0.5,
) -> PolishResult:
    """Descend x, y, θ on T1 + T4 + T5 + T6 with a λ_T4 ramp, snap the moved
    rotations to 90°, and accept ONLY if (a) the snapped pose has lower E at
    the final weights, (b) ``_placement_is_legal`` accepts every move at once,
    and (c) the engine's own ``total()`` does not rise. Otherwise the board is
    left exactly as it was."""
    from precis.pcb.ir import COURTYARD_CLEARANCE_MM
    from precis.pcb.optimize import Move, MoveKind

    ir = engine.ir
    built = board_from_engine(engine)
    if built is None:
        return PolishResult(False, "unplaced", 0.0, 0.0, ())
    board, springs = built
    lay = Layout(board)
    if lay.size == 0:
        return PolishResult(False, "no_free_vars", 0.0, 0.0, ())
    x0, y0, x1, y1 = engine._placement_bounds
    t1 = ConnectivityPull(lay, gamma=gamma)
    # The courtyards are the engine's, already inflated by its 0.40 mm reserve
    # on each side. Unclassed pairs keep slice 1's soft 0.40 mm on top of
    # that (a 1.2 mm body gap); a JLCPCB table value above 1.2 mm raises it.
    t4 = CourtyardClearance(
        lay,
        clearance=COURTYARD_CLEARANCE_MM,
        pair_min=pair_minimums(
            board.parts,
            fallback=3.0 * COURTYARD_CLEARANCE_MM,
            reserve=2.0 * COURTYARD_CLEARANCE_MM,
        ),
    )
    t5 = BoardEdge(lay, (x0, y0), (x1, y1))
    t6 = PadSpring(lay, springs)

    def energy(l4: float) -> Energy:
        return Energy(lay, [(1.0, t1), (l4, t4), (1.0, t5), (1.0, t6)])

    final = energy(lambda_t4[-1])
    z0 = lay.pack()
    e0 = final.value_grad(z0)[0]
    # A wrong analytic gradient silently wrecks a descent; refuse rather than
    # move parts on it. Checked along x, y only: rotation of axis-aligned
    # courtyards sits exactly on a kink (tied witness vertices), where a
    # central difference and the one-sided derivative legitimately differ.
    n_xy = 2 * int(lay.free_xy.sum())

    def xy_only(w: _Z) -> tuple[float, _Z]:
        zz = z0.copy()
        zz[:n_xy] = w
        v, g = final.value_grad(zz)
        return v, g[:n_xy]

    probe = check_gradient(
        xy_only, z0[:n_xy], rng=np.random.default_rng(0), h=1e-5, n_fd=2
    )
    if not probe.dot_rel_err < 5e-2:
        _LOG.warning("field_polish refused: gradient check failed (%s)", probe)
        return PolishResult(False, "bad_gradient", e0, e0, ())
    z = z0.copy()
    for l4 in lambda_t4:
        z = minimize(
            energy(l4).value_grad,
            z,
            jac=True,
            method="L-BFGS-B",
            options={"maxiter": max_iter},
        ).x
    x, y, th, _ = lay.unpack(z)
    rot = -np.degrees(th)
    for k in np.flatnonzero(lay.free_rot):
        if abs(th[k] - board.th0[k]) > 1e-6:
            rot[k] = (round(rot[k] / 90.0) * 90.0) % 360.0
        else:
            rot[k] = -math.degrees(board.th0[k])
        th[k] = -math.radians(rot[k])
    e1 = final.value_grad(lay.pack_from(x, y, th))[0]
    if not e1 < e0:
        return PolishResult(False, "no_gain", e0, e1, ())
    old: list[tuple[float, float, float]] = []
    new: list[tuple[float, float, float]] = []
    moved: list[int] = []
    for i in range(ir.n_instances):
        r_old = 0.0 if math.isnan(float(ir.inst_rot[i])) else float(ir.inst_rot[i])
        r_new = float(rot[i]) if lay.free_rot[i] else r_old
        pose_old = (float(ir.inst_x[i]), float(ir.inst_y[i]), r_old)
        pose_new = (float(x[i]), float(y[i]), r_new)
        if max(abs(a - b) for a, b in zip(pose_old, pose_new, strict=True)) > 1e-6:
            moved.append(i)
            old.append(pose_old)
            new.append(pose_new)
    if not moved:
        return PolishResult(False, "no_gain", e0, e1, ())
    proposals = [(i, p[0], p[1]) for i, p in zip(moved, new, strict=True)]
    rotations = {i: p[2] for i, p in zip(moved, new, strict=True)}
    if not engine._placement_is_legal(proposals, rotations=rotations):
        return PolishResult(False, "illegal", e0, e1, tuple(moved))
    before = engine.total()
    move = Move(MoveKind.TRANSLATE, tuple(moved), tuple(old), tuple(new))
    engine.apply_move(move)
    if engine.total() > before + 1e-9:
        engine.undo_move(move)
        return PolishResult(False, "cost_worse", e0, e1, tuple(moved))
    return PolishResult(True, "applied", e0, e1, tuple(moved))

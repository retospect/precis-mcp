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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points

_Z = np.ndarray  # packed state, shape (n_vars,)


@dataclass(frozen=True)
class Part:
    """One placed part: pad centres and courtyard in its rotating frame."""

    pads: np.ndarray  # (n_pads, 2) local pad centres
    court: np.ndarray  # (k, 2) local courtyard ring (open or closed)
    side: int = 0  # 0 top, 1 bottom
    fixed_xy: bool = False
    fixed_rot: bool = False


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

    def __init__(self, layout: Layout, clearance: float = 0.40, beta: float = 8.0):
        super().__init__(layout)
        self.c = float(clearance)
        self.beta = float(beta)

    def _signed(self, pa: Polygon, pb: Polygon) -> tuple[float, np.ndarray, np.ndarray]:
        """``(sd, witness on A, witness on B)`` in world coordinates."""
        if not pa.intersects(pb):
            qa, qb = nearest_points(pa, pb)
            a, b = np.array(qa.coords[0]), np.array(qb.coords[0])
            return float(np.linalg.norm(a - b)), a, b
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
        if wa is None or wb is None:  # edges cross, no vertex inside: zero depth
            c = np.array(pa.intersection(pb).centroid.coords[0])
            return 0.0, c, c
        return -depth, wa, wb

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
                sd, a, b = self._signed(polys[i], polys[j])
                u = self.c - sd
                sp = _softplus(u, self.beta)
                total += sp * sp
                dE_dsd = -2.0 * sp * _sigmoid(u, self.beta)
                diff = b - a
                nrm = float(np.linalg.norm(diff))
                if nrm < 1e-12:
                    continue
                unit = diff / nrm
                # sd = ±|b−a|: gradient wrt b is sign·unit, wrt a is −sign·unit
                sign = 1.0 if sd >= 0 else -1.0
                gb = dE_dsd * sign * unit
                for idx, pt, g in ((i, a, -gb), (j, b, gb)):
                    gx[idx] += g[0]
                    gy[idx] += g[1]
                    gth[idx] += -g[0] * (pt[1] - y[idx]) + g[1] * (pt[0] - x[idx])
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

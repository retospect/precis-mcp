"""Unified place-and-route energy field — slices 0–2.

Spec: ``docs/backlog/pcb-unified-place-route.md`` (§State, §Cost, §Derivative
route). One scalar energy ``E(z) = Σ λ_t T_t(z)`` over a packed continuous
state ``z``; each term returns ``(value, grad)`` with a hand-written analytic
gradient (numpy; no autodiff dependency). Landed terms:

* **T1** connectivity pull — weighted-average (WA) wirelength per net on
  rotated pad centres, so every pad carries ``∂/∂θ``.
* **T4** courtyard clearance — ``Σ softplus_β(c − sd(poly_i, poly_j))²`` on
  shapely polygon distance (the vector clearance kernel replaces it later),
  with the JLCPCB pairwise SMD spacing table raising per-pair targets, and
  mounting-hole keep-outs entering as fixed obstacles on both sides. Its
  NumPy broad-phase filters same-side bounding-box pairs in the original
  row-major order; vertex Points are built once per part per evaluation
  instead of once per pair. Scalar witnesses and accumulation are
  unchanged, so speeding dense boards does not change placement results.
* **T5** board edge — softplus pull on part centres, inside the same
  per-part box ``OptimizeEngine.bounds_for`` gives the legality gate.
* **T6** rubber bands — decoupling-cap pad to served pin, ``d₀`` slack.
* **T9** raster wells — ``Σ w_p (1 − cos(2πc/p))`` on part centres (own
  pitch included) and vias; ramped coarse → fine in Harden only.
* **T10** rotation wells — the preferred-number well over
  :data:`ROT_TIERS_DEG` (one bilateral-Huber well per angle pitch, tiers
  combined by softmin, ``precis.structsolve.preferred``): multiples of
  90° are free, a 45° pose pays a small cost (Reto Q7 2026-10-22:
  rotation is priced, not forbidden — "routable 45 beats unroutable 90
  any day"). θ is a real variable, snapped to the finest
  well grid (``ROT_SNAP_DEG``) in Harden, and an off-90° snap survives
  only when it beats the same pose's 90° re-snap on the priced energy by
  more than ``ROT_TILT_MIN_GAIN``;
  the write is always a grid value in [0, 360), the descended continuous
  θ never is (gr503694).
* **Density** (gr503556) — ``Σ_b A_b·(ρ_b² − Σ_i φ_ib²)`` over a bin
  grid, ρ deposited through a normalized Gaussian kernel on part
  centres at ``LAMBDA_DENSITY``. Subtracting each part's self-interaction
  makes crowded neighbours push apart without sending a lone part to
  the boundary; the earlier softplus-over-target tail repelled even
  underloaded bins and piled parts against the wall.

``polish_placement`` runs the placement half of this energy as one move on
the engine's board, behind ``OptimizeConfig.field_polish``, gated on
energy gain + the engine's legality predicate — repair over refuse where
the legality gate can still be satisfied. The annealer's
``OptimizeEngine.total()`` is reported as a comparison value, not a gate
(Reto 2026-10-21). :func:`check_gradient` is the harness every term's
test reuses (dot-product, Taylor, central FD).

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import shapely
from scipy.optimize import minimize
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points

from precis.structsolve import preferred

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
    #: the part's own snap raster (``_preferred_snap_grid_mm``): an extra
    #: T9 well pitch and the raster the final snap puts the centre on
    pitch: float | None = None


@dataclass(frozen=True)
class FieldBoard:
    """Parts, nets and vias at their base pose (the fixed-variable values).

    ``nets[k]`` lists ``(part, pad)`` pairs for T1's wirelength pull;
    ``tree_nets`` lists the supply/ground/pour nets excluded from T1 that
    keep a spanning-tree term (``SupplyTree``, D14) at ``tree_weights``.
    ``x0/y0/th0`` are the starting poses; ``vias0`` is ``(n_vias, 2)``
    (carried in the state, unused by T1/T4). ``unpulled_nets`` names the
    nets above :data:`MAX_PULLED_NET_PINS` pins that neither T1 nor the
    tree pulls (reported on the polish result, gr504558)."""

    parts: tuple[Part, ...]
    nets: tuple[tuple[tuple[int, int], ...], ...]
    x0: np.ndarray
    y0: np.ndarray
    th0: np.ndarray
    vias0: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    tree_nets: tuple[tuple[tuple[int, int], ...], ...] = ()
    tree_weights: tuple[float, ...] = ()
    unpulled_nets: tuple[str, ...] = ()


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

    def scatter(
        self,
        gx: np.ndarray,
        gy: np.ndarray,
        gth: np.ndarray,
        gvias: np.ndarray | None = None,
    ) -> _Z:
        """Per-part full gradients → packed gradient (vias flat ``(x, y)``)."""
        return np.concatenate(
            [
                gx[self.free_xy],
                gy[self.free_xy],
                gth[self.free_rot],
                (
                    np.zeros(2 * self.n_vias)
                    if gvias is None
                    else np.asarray(gvias, dtype=float).reshape(-1)
                ),
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
    pair switches.

    ``obstacles`` adds fixed world-frame polygons (the mounting-hole
    keep-outs, gr503640): ``Σ_i Σ_h softplus_β(c − sd(poly_i, poly_h))²``.
    A hole goes through the board, so the same-side exemption does NOT
    apply — every part interacts — except the obstacle's ``exempt``
    instance index, the one part whose own footprint cut it
    (``_hole_owned_by``). Its gradient half is discarded; the obstacle has
    no pose slot. Posed at build time, so a hole that follows a moving
    owner drifts from the term — the legality gate still judges it at the
    owner's proposed pose."""

    def __init__(
        self,
        layout: Layout,
        clearance: float = 0.40,
        beta: float = 8.0,
        pair_min: dict[tuple[int, int], float] | None = None,
        obstacles: Sequence[tuple[np.ndarray, int | None]] = (),
    ):
        super().__init__(layout)
        self.c = float(clearance)
        self.beta = float(beta)
        #: per-pair required clearance, keyed ``(i, j)`` with ``i < j``;
        #: pairs not listed use ``clearance``
        self.pair_min = pair_min or {}
        #: ``(ring, Polygon, lo, hi, exempt)`` per fixed obstacle — the
        #: ring and its bounds cache the cull the part pairs get for free.
        self.obstacles = []
        for ring, exempt in obstacles:
            r = np.asarray(ring, dtype=float)
            self.obstacles.append((r, Polygon(r), r.min(axis=0), r.max(axis=0), exempt))

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
        vertices: tuple[tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]
        | None = None,
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
            # The vertex->polygon distances are one vectorized call per
            # direction — ``nearest_points`` only runs for the tied few;
            # measured on nano this cut ``_signed``'s shapely calls ~30x.
            verts_by_dir: list[
                tuple[np.ndarray, np.ndarray, Polygon, bool, np.ndarray]
            ] = []
            dmin = math.inf
            for direction, (src, dst, flip) in enumerate(
                ((pa, pb, False), (pb, pa, True))
            ):
                verts, points = (
                    vertices[direction]
                    if vertices is not None
                    else (
                        np.asarray(src.exterior.coords)[:-1],
                        None,
                    )
                )
                if points is None:
                    points = shapely.points(verts)
                dists = shapely.distance(points, dst)
                verts_by_dir.append((verts, dists, dst, flip, points))
                dmin = min(dmin, float(dists.min()))
            cands: list[tuple[float, np.ndarray, np.ndarray, bool]] = []
            for verts, dists, dst, flip, points in verts_by_dir:
                for k in np.flatnonzero(dists <= dmin + 1e-9):
                    qv = nearest_points(points[k], dst)[1]
                    cands.append(
                        (
                            float(dists[k]),
                            np.array(verts[k]),
                            np.array(qv.coords[0]),
                            flip,
                        )
                    )
            tied = cands
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
            verts = np.asarray(src.exterior.coords)[:-1]
            inside = shapely.contains_xy(dst, verts[:, 0], verts[:, 1])
            pts = verts[inside]
            if not len(pts):
                continue
            ds = shapely.distance(shapely.points(pts), ring)
            ki = int(ds.argmax())
            if float(ds[ki]) > depth:
                q = ring.interpolate(ring.project(Point(pts[ki])))
                vv, qq = np.array(pts[ki]), np.array(q.coords[0])
                depth = float(ds[ki])
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
        vertices = [np.asarray(p.exterior.coords)[:-1] for p in polys]
        points = [shapely.points(v) for v in vertices]
        lo = np.array([r.min(axis=0) for r in rings])
        hi = np.array([r.max(axis=0) for r in rings])
        # softplus² is below 1e-12 of its scale past u = −30/β; cull there
        reach = self.c + 30.0 / self.beta
        gx, gy, gth = np.zeros(n), np.zeros(n), np.zeros(n)
        total = 0.0
        gaps = np.maximum(lo[:, None] - hi[None], lo[None] - hi[:, None]).max(axis=-1)
        sides = np.array([p.side for p in parts])
        active = np.triu((gaps <= reach) & (sides[:, None] == sides[None, :]), k=1)
        # Row-major nonzero preserves the old i,j accumulation order exactly.
        for i, j in zip(*np.nonzero(active), strict=True):
            ti, tj = np.array([x[i], y[i]]), np.array([x[j], y[j]])
            sd, gi, gj = self._signed(
                polys[i],
                polys[j],
                rings[i],
                rings[j],
                ti,
                tj,
                ((vertices[i], points[i]), (vertices[j], points[j])),
            )
            u = self.pair_min.get((i, j), self.c) - sd
            sp = _softplus(u, self.beta)
            total += sp * sp
            dE_dsd = -2.0 * sp * _sigmoid(u, self.beta)
            for idx, g in ((i, gi), (j, gj)):
                gx[idx] += dE_dsd * g[0]
                gy[idx] += dE_dsd * g[1]
                gth[idx] += dE_dsd * g[2]
        for i in range(n):
            ti = np.array([x[i], y[i]])
            for ring_h, poly_h, lo_h, hi_h, exempt in self.obstacles:
                if exempt == i:
                    continue
                gap = np.maximum(lo[i] - hi_h, lo_h - hi[i]).max()
                if gap > reach:
                    continue
                sd, gi, _g_h = self._signed(
                    polys[i], poly_h, rings[i], ring_h, ti, (lo_h + hi_h) / 2.0
                )
                u = self.c - sd
                sp = _softplus(u, self.beta)
                total += sp * sp
                dE_dsd = -2.0 * sp * _sigmoid(u, self.beta)
                gx[i] += dE_dsd * gi[0]
                gy[i] += dE_dsd * gi[1]
                gth[i] += dE_dsd * gi[2]
        return total, lay.scatter(gx, gy, gth)


class BinDensity(Term):
    """The spreading term (gr503556): smooth bin-density over the
    placement domain — the spec's "router needs more space" force
    (Reto 2026-10-18), built cheap.

    Each part deposits its courtyard area ``a_i`` into the bins through a
    Gaussian kernel on its CENTRE, RENORMALIZED so the whole ``a_i``
    always lands inside the domain (``S_i`` is part ``i``'s in-domain
    kernel mass — half of it leaks out for a part sitting on the edge):

        ρ_b = Σ_i a_i · K_ib / S_i,   K_ib = exp(−|c_b − p_i|² / 2σ²) / (2πσ²)

    Without the ``/S_i`` a part hugging the wall sees half its mass
    evaporate — an un-renormalized kernel literally rewards leaking off
    the domain, and the descent walks parts out of bounds. With it, the
    edge position DOUBLES the local density instead, so the wall stops
    being a downhill direction at all.

    The cost is the field energy ``E = Σ_b A_b · (ρ_b² − Σ_i φ_ib²)``
    with ``φ_ib = a_i·K_ib/S_i`` part ``i``'s deposit — a discrete
    approximation of ∫ρ² with the SELF-interaction removed. Only OTHER
    parts' mass repels a part: a lone part's own blob otherwise pushes it
    toward the domain edge (its in-domain kernel centre of mass sits
    inward), which pinned p2u's free U1 into a far corner against the
    supply tree's pull. The pair-only form's minimum at fixed total area
    is still UNIFORM density: only bins carrying mass push back, and the
    push is proportional to how crowded they already are, so an empty
    half-board beside a jammed strip is costly — the imbalance the
    shelf-packed seed left and the anneal could not fix (SWAP trades
    occupied slots; TRANSLATE is blocked by abutting courtyards). A
    softplus-over-target form was also tried and rejected: its tail
    repels parts from EVERY bin — underloaded ones included — and piles
    them on the boundary instead of spreading them.

    Smooth everywhere (a Gaussian has no kinks), so the FD check applies
    at any point. No ∂/∂θ on purpose: the kernel sees the centre, not
    the orientation — rotation's footprint is T4's job. Fixed parts still
    deposit area (they occupy space); only their gradient vanishes.

    The energy is normalized by ``(Σ a_i)²`` — the raw form scales with
    total part area, so on a sparse board its λ·E dwarfed the supply
    tree and connectivity pulls it is supposed to defer to (p2u: U1's
    tree draw to the J_IN→HUB corridor lost to a ~4·10³ density drop
    into the far corner, gr503640-class residual). The normalized term
    keeps the same force DIRECTION — which bins are crowded — while its
    scale stays ``A²/A_domain``-sized on every board, so
    :data:`LAMBDA_DENSITY` means the same thing everywhere."""

    def __init__(
        self,
        layout: Layout,
        lo: tuple[float, float] | np.ndarray,
        hi: tuple[float, float] | np.ndarray,
        *,
        bins: int = 8,
        sigma_scale: float = 1.0,
    ):
        super().__init__(layout)
        lo_a, hi_a = np.asarray(lo, float), np.asarray(hi, float)
        size = np.maximum(hi_a - lo_a, 1e-6)
        # Bin counts keep cells ~square: n_x/n_y ∝ the side lengths, with
        # `bins` bins on the longer axis.
        ratio = size / size.max()
        n = np.maximum(2, np.round(bins * ratio).astype(int))
        edges = [np.linspace(lo_a[d], hi_a[d], n[d] + 1) for d in range(2)]
        centres = np.stack(
            np.meshgrid(*[(e[:-1] + e[1:]) / 2.0 for e in edges], indexing="ij"),
            axis=-1,
        ).reshape(-1, 2)
        self.centres = centres
        self.bin_area = float(np.prod(size / n))
        #: one bin side of kernel width — a part's mass reaches ~3 bins,
        #: smooth enough for L-BFGS-B, local enough to matter.
        self.sigma = float(sigma_scale * np.sqrt(self.bin_area))
        self.areas = np.array(
            [float(Polygon(p.court).area) for p in layout.board.parts]
        )
        #: ``1/(Σ a_i)²`` — the term's scale factor (class docstring).
        self._inv_a2 = 1.0 / max(float(self.areas.sum()), 1e-9) ** 2
        #: per-bin raw deposit weights K_ib are UNNORMALIZED here — the
        #: ``/S_i`` conservation happens in ``value_grad`` — and the
        #: Σ_b A_b·ρ² weighting keeps the cost's scale independent of the
        #: bin count.

    def value_grad(self, z: _Z) -> tuple[float, _Z]:
        lay = self.layout
        x, y, _th, _ = lay.unpack(z)
        pos = np.stack([x, y], axis=1)  # (n, 2)
        d2 = ((pos[:, None, :] - self.centres[None, :, :]) ** 2).sum(axis=-1)
        k = np.exp(-d2 / (2.0 * self.sigma**2)) / (2.0 * math.pi * self.sigma**2)
        # k is (n, bins); S_i is part i's total in-domain kernel mass and
        # phi_ib = a_i·k_ib/S_i the part's per-bin deposit.
        s = np.maximum(k.sum(axis=1), 1e-9)  # (n,)
        phi = self.areas[:, None] * k / s[:, None]  # (n, bins)
        rho = phi.sum(axis=0)  # (bins,)
        q = (phi * phi).sum(axis=0)  # Σ_i φ_ib²  (bins,)
        total = float(self.bin_area * ((rho * rho) - q).sum() * self._inv_a2)
        # ∂φ_ib/∂p_i = (a_i/(σ²S_i))·k_ib·(c_b − m_i), with
        # m_i = Σ_c k_ic·c_c/S_i the centre of mass of part i's deposit
        # (p_i cancels out of the bracket — a part at its own kernel's
        # centre moves no mass between bins). Then, with
        # E = Σ_b A_b(ρ_b² − Σ_j φ_jb²); only φ_ib depends on p_i, so
        #   ∂E/∂p_i = Σ_b 2A_b(ρ_b − φ_ib) · ∂φ_ib/∂p_i.
        m = (k @ self.centres) / s[:, None]  # (n, 2)
        scale = (self.areas / (self.sigma**2 * s))[:, None]  # (n, 1)
        u = 2.0 * self.bin_area * self._inv_a2 * (rho[None, :] - phi)  # (n, bins)
        t = u * k  # (n, bins)
        g = scale * (t @ self.centres - m * t.sum(axis=1)[:, None])
        return total, lay.scatter(g[:, 0], g[:, 1], np.zeros_like(x))


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
    """T5 (slice 1): ``Σ_i softplus_β(lo_i − c_i)² + softplus_β(c_i − hi_i)²``
    on part CENTRES against a PER-PART box — the same box
    ``OptimizeEngine.bounds_for`` gives the legality gate (the placement
    domain shrunk by the part's own keep-out radius, intersected with its
    soft-pin box). A uniform ``(2,)`` box broadcasts to every part. The
    exact outline test stays in the legality gate; this term only pulls
    strays inward. Smooth; no ∂/∂θ."""

    def __init__(
        self,
        layout: Layout,
        lo: tuple[float, float] | np.ndarray,
        hi: tuple[float, float] | np.ndarray,
        beta: float = 8.0,
    ):
        super().__init__(layout)
        n = len(layout.board.parts)
        self.lo = np.broadcast_to(np.asarray(lo, dtype=float), (n, 2)).copy()
        self.hi = np.broadcast_to(np.asarray(hi, dtype=float), (n, 2)).copy()
        self.beta = float(beta)

    def value_grad(self, z: _Z) -> tuple[float, _Z]:
        lay = self.layout
        x, y, _th, _ = lay.unpack(z)
        gx, gy = np.zeros_like(x), np.zeros_like(y)
        total = 0.0
        for c, g, lo, hi in (
            (x, gx, self.lo[:, 0], self.hi[:, 0]),
            (y, gy, self.lo[:, 1], self.hi[:, 1]),
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


# ── slice 2: wells T9 (raster) and T10 (rotation) ────────────────────────

#: T9's base raster, coarse to fine (spec §Cost row T9). The 2 mm well
#: carries a reduced weight on purpose: it is not nested in 5 mm, so at
#: full weight a 2-mm-only multiple (0.691 + 1.809 = 2.5·w) would read
#: CHEAPER than a 5-mm-only one (2·w from the 10 mm well + 2·w₂); keeping
#: w₂ below 0.25·w restores the coarse-first order. A part's own pitch
#: joins its set at full weight.
GRID_WELL_WEIGHTS: dict[float, float] = {
    10.0: 1.0,
    5.0: 1.0,
    2.0: 0.2,
    1.0: 1.0,
    0.5: 1.0,
}

#: T10's angle tiers in degrees (Reto Q7, 2026-10-22 — "allow rotation in
#: 45° steps any time it helps, with a rotation cost term (penalty for
#: non-90 angles); routable 45 beats unroutable 90 any day"): one
#: bilateral-Huber well per pitch, combined by softmin —
#: :mod:`precis.structsolve.preferred`, the same scorer the
#: preferred-number machinery uses for lengths. Every multiple of 90° is
#: a deepest basin; a 45° pose pays the tier-depth difference
#: (~κ·A·p_fine/2 ≈ 5.6 at λ_wells). The tier list is the single
#: tunable: adding ``15.0`` re-enables a finer-priced tier (the 20-board
#: sweep measured (90,45) at 18 applied / 0 illegal vs 16 / 2 with the
#: 15° tier — the extra rest points let odd 30/60° tilts through without
#: earning a route). ``default_coefficients`` derives the depths.
ROT_TIERS_DEG: tuple[float, ...] = (90.0, 45.0)

#: θ snaps to the nearest multiple of this — T10's finest well tier.
ROT_SNAP_DEG = min(ROT_TIERS_DEG)

#: The rotation cost in coefficient form — κ = 0.5 is the
#: :func:`preferred.default_coefficients` depth rule (each coarser tier
#: deeper by κ·A·p_finer/2), so the 45° pose pays ≈ 11.25·A·λ_wells
#: (~1.4 at the defaults) over the free 90° basin. A = 0.25 sets the
#: sawtooth's steepest pull (A·180/π·λ ≈ 7.2 per rad) near the retired
#: cosine stack's max |∂E/∂θ| (Σ w_k·k ≈ 12.8 before λ) — enough to hold
#: a seed θ against casual drift while a real clearance or wirelength
#: pull can still buy an off-90 pose.
ROT_WELL_COEFFS: preferred.Coefficients = preferred.default_coefficients(
    ROT_TIERS_DEG, A=0.25
)

#: Reto Q7's open question — the early-stage θ drift: without a rotation
#: term the λ_T4 ramp drifted rotatable parts ~−0.5…−0.7 rad into basins
#: the snap could not recover from (no_gain rose 1→7 of 20 boards after
#: the bounds fix). True keeps T10 on from stage 1, so θ sits on a well
#: bottom the whole run — effectively held at its start angle until
#: Harden, but priced rather than pinned, so a real pull can still buy a
#: 45° pose; False restores the original schedule (wells only in
#: Harden). One named switch so either ruling is a one-line change.
ROT_WELL_FROM_STAGE1 = True

#: A snapped θ off the 90° grid is written only when its measured final
#: energy beats the same pose re-snapped to the nearest 90° by more than
#: this margin (gr503694). The tilt's own rotation cost (T10's tier-depth
#: gap) is already inside the comparison, so the constant prices ONLY the
#: board-level gain the tilt must still show on top of that — 10.0 is
#: ~10 mm of wirelength-equivalent energy, the "essentially all
#: 0/90/180/270" reading of D5 (a 0.5 bar let 4-9 of 29 esp32c3 parts
#: stay tilted). ``math.inf`` flips the output policy to 90°-grid-only;
#: 0.0 leaves the rotation cost as the only bar.
ROT_TILT_MIN_GAIN = 10.0


def snap_rotation_deg(rot: float) -> float:
    """θ's writable grid point nearest ``rot`` — a multiple of the finest
    rotation tier (:data:`ROT_SNAP_DEG`), normalised to [0, 360). Shared
    by the polish's snap and ``OptimizeEngine.snap_to_preferred`` so both
    paths write the same grid (Q7)."""
    return round(rot / ROT_SNAP_DEG) * ROT_SNAP_DEG % 360.0


class SupplyTree(Term):
    """D14: a spanning tree per supply/ground/pour net — ``Σ w_n ·
    MST_length`` over the net's rotated pad centres.

    These nets are excluded from T1 (``board_from_engine`` routes them to
    :attr:`FieldBoard.tree_nets` instead): the tree is their only pull, and
    it keeps the net connected while things move — a supply tree's length
    drags one domain's parts together, so 3.3 V parts cluster and 5 V parts
    cluster. Per-net weight from ``tree_weights``; ground is lowest (it
    spans every part and so clusters nothing).

    Piecewise smooth: the MST edge set is discrete and kinks where two
    candidate edges tie; away from ties the gradient is each tree edge's
    unit vector onto both pads' poses (same lever-arm ∂/∂θ as T1)."""

    def __init__(self, layout: Layout):
        super().__init__(layout)
        self.nets = layout.board.tree_nets
        self.weights = np.asarray(layout.board.tree_weights, dtype=float)

    @staticmethod
    def _mst(pts: np.ndarray) -> list[tuple[int, int]]:
        """Prim over ``pts`` (n,2); returns ``(parent, child)`` index pairs."""
        n = len(pts)
        if n < 2:
            return []
        dist = np.linalg.norm(pts - pts[0], axis=1)
        parent = np.zeros(n, dtype=int)
        in_tree = np.zeros(n, dtype=bool)
        in_tree[0] = True
        edges: list[tuple[int, int]] = []
        for _ in range(n - 1):
            d = np.where(in_tree, np.inf, dist)
            j = int(d.argmin())
            edges.append((int(parent[j]), j))
            in_tree[j] = True
            upd = np.linalg.norm(pts - pts[j], axis=1)
            better = upd < dist
            dist[better] = upd[better]
            parent[better] = j
        return edges

    def value_grad(self, z: _Z) -> tuple[float, _Z]:
        lay = self.layout
        x, y, th, _ = lay.unpack(z)
        parts = lay.board.parts
        gx, gy, gth = np.zeros_like(x), np.zeros_like(y), np.zeros_like(th)
        total = 0.0
        for net, w in zip(self.nets, self.weights, strict=True):
            if len(net) < 2 or w == 0.0:
                continue
            pts = np.array(
                [
                    _world(parts[i], parts[i].pads[k : k + 1], x[i], y[i], th[i])[0]
                    for i, k in net
                ]
            )
            for a, b in self._mst(pts):
                diff = pts[a] - pts[b]
                d = float(np.linalg.norm(diff))
                if d < 1e-12:
                    continue
                total += w * d
                g = w * diff / d
                for idx, pt, gg in ((net[a][0], pts[a], g), (net[b][0], pts[b], -g)):
                    gx[idx] += gg[0]
                    gy[idx] += gg[1]
                    gth[idx] += -gg[0] * (pt[1] - y[idx]) + gg[1] * (pt[0] - x[idx])
        return total, lay.scatter(gx, gy, gth)


class GridWell(Term):
    """T9 (slice 2): raster wells, ``Σ w_p (1 − cos(2πc/p))`` on each part
    centre in x and y, and on every via.

    The anchor is the part CENTRE — ``x_i``/``y_i`` — never pad 1, so a
    mirrored or rotated part's pads move in its local frame while its
    centre keeps the same grid (``snap_to_preferred``'s anchor, spec
    Decision 6). ``Part.pitch`` joins the part's pitch set at
    ``own_weight``. ``pitches`` is a ``{pitch_mm: weight}`` map; the
    Harden stage passes the coarsest pitch alone first so the fine wells
    cannot trap a part between coarse multiples, then the full set —
    wells stay off during Spread (spec §Schedule)."""

    def __init__(
        self,
        layout: Layout,
        pitches: Mapping[float, float] | None = None,
        *,
        own_weight: float = 1.0,
        via_pitches: Mapping[float, float] | None = None,
    ):
        super().__init__(layout)
        base = dict(GRID_WELL_WEIGHTS if pitches is None else pitches)
        self.via_pitches = base if via_pitches is None else dict(via_pitches)
        self.per_part = [dict(base) for _ in layout.board.parts]
        for i, part in enumerate(layout.board.parts):
            if part.pitch is not None and math.isfinite(part.pitch) and part.pitch > 0:
                self.per_part[i][float(part.pitch)] = float(own_weight)

    def value_grad(self, z: _Z) -> tuple[float, _Z]:
        lay = self.layout
        x, y, _th, vias = lay.unpack(z)
        gx, gy = np.zeros_like(x), np.zeros_like(y)
        total = 0.0
        for i in range(lay.n_parts):
            for p, w in self.per_part[i].items():
                f = 2.0 * math.pi / p
                total += w * (2.0 - math.cos(f * x[i]) - math.cos(f * y[i]))
                gx[i] += w * f * math.sin(f * x[i])
                gy[i] += w * f * math.sin(f * y[i])
        gvia = np.zeros_like(vias)
        for v in range(len(vias)):
            for p, w in self.via_pitches.items():
                f = 2.0 * math.pi / p
                total += w * (2.0 - math.cos(f * vias[v, 0]) - math.cos(f * vias[v, 1]))
                gvia[v, 0] += w * f * math.sin(f * vias[v, 0])
                gvia[v, 1] += w * f * math.sin(f * vias[v, 1])
        return total, lay.scatter(gx, gy, np.zeros_like(x), gvia)


class RotationWell(Term):
    """T10 (slice 2, Reto Q7 2026-10-22): ``Σ_i P(rot_i)`` — the
    preferred-number penalty of :mod:`precis.structsolve.preferred` over
    the angle tiers in ``coeffs`` (:data:`ROT_WELL_COEFFS` by default).
    One bilateral-Huber well per tier pitch, combined by softmin: the
    deepest basin is the coarsest tier's grid, so multiples of 90° are
    free and a 45° pose pays the 45↔90 depth gap — a rotation COST, not
    a forbidden zone. Smooth (the Huber caps
    smooth both ends of the sawtooth, the softmin smooths tier ties) and
    non-convex. The state θ is radians counter-clockwise; the penalty
    reads the engine's clockwise-degree rotation. No x/y gradient."""

    def __init__(self, layout: Layout, coeffs: preferred.Coefficients | None = None):
        super().__init__(layout)
        self.coeffs = ROT_WELL_COEFFS if coeffs is None else coeffs

    def value_grad(self, z: _Z) -> tuple[float, _Z]:
        lay = self.layout
        x, _y, th, _v = lay.unpack(z)
        res = preferred.evaluate(-np.degrees(th), self.coeffs)
        # d(rot_deg)/dθ = −180/π — chain the degree gradient back to rad.
        gth = -np.asarray(res.grad) * (180.0 / math.pi)
        return float(np.sum(res.value)), lay.scatter(
            np.zeros_like(x), np.zeros_like(x), gth
        )


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
#: Ground names get the lowest D14 tree weight: ground spans every part,
#: so its length term would cluster nothing (spec §Cost, D14).
_GROUND_RE = re.compile(r"^[ADP]?GND\w*$", re.I)
GROUND_TREE_WEIGHT = 0.25
SUPPLY_TREE_WEIGHT = 1.0
#: T6 rubber-band slack: a served decoupling pad within this of its pin is free.
SUPPORT_D0_MM = 1.5
#: λ_T4 ramp: start loose so wirelength can pull, end firm so overlaps resolve.
LAMBDA_T4_RAMP = (0.25, 1.0, 4.0, 16.0)
#: λ_T4 for the bounded repair descent — hard enough to re-open the gaps
#: the ramp's λ=16 cap or a snap closed, short of a second full ramp.
REPAIR_LAMBDA_T4 = 64.0
#: L-BFGS-B convergence tolerance for the polish's stages — scipy's own
#: default (2.2e-9) keeps a stage grinding ~0.001-E-relative gains to
#: ``max_iter``; on a dense board each of those evals is a full O(n²)
#: courtyard pass (~70 ms on nano, ~660 evals/45 s a polish, every one
#: sunk when the pose is refused). 1e-7 stops a stage once an iteration
#: buys less than ~0.0001 of the energy — the tail it skips never moved
#: a pose the snap cascade could see.
POLISH_FTOL = 1e-7
#: λ for the bin-density spread term (gr503556): deliberately small —
#: the term's job is a bias against re-clustering in the descent and the
#: accept gate, not a prime mover. Measured 2026-10-22 on the seeded
#: synthetic boards: λ ≤ ~0.5 keeps every legality gate satisfied (larger
#: values let the spread push peripheral parts past T5's soft wall), and
#: on p2u any weight at all lets U1's corner seed trap its supply-tree
#: pull — the term is a gentle prior, connectivity still wins everywhere
#: it has a real opinion. The term stays on through Harden: the committed
#: pose's spread is a real property, and a snap that re-clusters should
#: fail the energy gate.
LAMBDA_DENSITY = 0.1
#: Bins per LONGER domain side for BinDensity (8×~6 on nano ≈ 12 mm cells,
#: σ one cell — a part's mass reaches its ~3 neighbours).
DENSITY_BINS = 8

#: Accept gate: an applied pose's wirelength terms (T1 + the D14 supply
#: tree) may not exceed their seed value by more than this fraction —
#: spread, courtyard and well terms never pay for a longer ratsnest
#: (measured 2026-10-22: a platform-sensitive descent landed in a basin
#: where density/T4 gains hid a 2× wirelength regression on the sanity
#: board — legal and lower-E, but worse where it counts).
POLISH_WIRELENGTH_WORSEN = 0.02

#: A net with more pins than this gets no pull at all — neither T1 nor the
#: D14 tree (a pull spanning most of the board clusters everything onto
#: its centroid). Deliberate, and reported: ``FieldBoard.unpulled_nets`` /
#: ``PolishResult.unpulled_nets`` name every net it drops (gr504558).
MAX_PULLED_NET_PINS = 12


@dataclass(frozen=True)
class PolishResult:
    applied: bool
    #: applied | unplaced | no_free_vars | empty_bounds | no_gain |
    #: illegal | wirelength_worse | measures_worse | bad_gradient
    reason: str
    e_before: float
    e_after: float
    moved: tuple[int, ...]
    #: Diagnostics: which snap lane applied, or the violated rules and
    #: parts when refused.
    detail: str = ""
    #: Comparison only, never the gate: ``engine.total()`` after minus
    #: before the applied move (``None`` when nothing was applied). The
    #: annealer's risk term prices margins the field path does not
    #: optimize, so it can rise on an accepted pose (Reto 2026-10-21).
    anneal_delta: float | None = None
    #: Nets above :data:`MAX_PULLED_NET_PINS` pins the field did not pull
    #: (``FieldBoard.unpulled_nets``).
    unpulled_nets: tuple[str, ...] = ()

    def summary(self) -> str:
        out = (
            f"field_polish {self.reason}: E {self.e_before:.4f} -> "
            f"{self.e_after:.4f}, {len(self.moved)} parts moved"
        )
        if self.anneal_delta is not None:
            out += f"; anneal {self.anneal_delta:+.2f}"
        if self.unpulled_nets:
            out += (
                f"; unpulled >{MAX_PULLED_NET_PINS}-pin nets: "
                f"{', '.join(self.unpulled_nets)}"
            )
        return f"{out} ({self.detail})" if self.detail else out


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
                pitch=float(engine._preferred_snap_grid_mm(i)),
            )
        )
    by_net: dict[int, list[tuple[int, int]]] = {}
    for p in range(len(ir.pin_instance)):
        net = int(ir.pin_net[p])
        if net < 0:
            continue
        by_net.setdefault(net, []).append((int(ir.pin_instance[p]), slot[p]))
    nets: list[tuple[tuple[int, int], ...]] = []
    tree_nets: list[tuple[tuple[int, int], ...]] = []
    tree_weights: list[float] = []
    unpulled: list[str] = []
    for net, members in sorted(by_net.items()):
        if len({i for i, _ in members}) < 2:
            continue
        name = str(ir.net_name[net])
        if len(members) > MAX_PULLED_NET_PINS:
            unpulled.append(name)
            continue
        if int(ir.net_plane_layers[net]) != 0 or _RAIL_RE.match(name):
            # D14: rails and plane nets keep a spanning-tree pull, never a
            # T1 wirelength one; ground's weight is lowest (it spans every
            # part, so it clusters nothing).
            tree_nets.append(tuple(members))
            tree_weights.append(
                GROUND_TREE_WEIGHT if _GROUND_RE.match(name) else SUPPLY_TREE_WEIGHT
            )
        else:
            nets.append(tuple(members))
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
        nets=tuple(nets),
        x0=np.asarray(ir.inst_x, dtype=float).copy(),
        y0=np.asarray(ir.inst_y, dtype=float).copy(),
        th0=th0,
        tree_nets=tuple(tree_nets),
        tree_weights=tuple(tree_weights),
        unpulled_nets=tuple(unpulled),
    )
    return board, springs


def _wirelength_terms(z: _Z, t1: ConnectivityPull, tree: SupplyTree) -> float:
    return t1.value_grad(z)[0] + tree.value_grad(z)[0]


def placement_wirelength(board: FieldBoard) -> float:
    """The T1 + D14 route-quality score used by the polish accept gate."""
    lay = Layout(board)
    return _wirelength_terms(lay.pack(), ConnectivityPull(lay), SupplyTree(lay))


def _hole_obstacles(engine: Any) -> list[tuple[np.ndarray, int | None]]:
    """Each mounting hole's keep-out polygon in world frame — T4's fixed
    obstacles (gr503640 fix 2) — with the one instance index exempt from
    it (the part whose own footprint cut it, ``_hole_owned_by``), else
    ``None``. :meth:`OptimizeEngine._hole_at` poses a footprint-derived
    hole at its owner's CURRENT pose — the base pose the board packs."""
    from precis.pcb.optimize import _hole_owned_by

    ir = engine.ir
    out = []
    for idx, hole in enumerate(ir.mounting_holes):
        _hx, _hy, poly = engine._hole_at(idx)
        owner = next(
            (i for i in range(ir.n_instances) if _hole_owned_by(ir, hole, i)),
            None,
        )
        out.append((np.asarray(poly, dtype=float), owner))
    return out


def _legality_violations(
    engine: Any,
    proposals: Sequence[tuple[int, float, float]],
    rotations: dict[int, float],
    *,
    limit: int = 8,
) -> list[str]:
    """The rules a proposal set breaks, ``"rule: parts"`` per violation —
    a reason-returning mirror of ``OptimizeEngine._placement_is_legal``
    (which short-circuits on the first). Only the polish's refuse path
    calls it; divergence from the gate is a bug, so keep the checks in the
    gate's order: bounds box, soft-pin travel, courtyard (rest, then
    moved-vs-moved), mounting holes, authored-via gap."""
    from precis.pcb.geom import convex_polygons_overlap
    from precis.pcb.optimize import (
        _FIXED_VIA_CLEARANCE_MM,
        _courtyard_in_outline,
        _hole_owned_by,
    )

    ir = engine.ir
    keepout = engine._keepout_r
    moving = [inst for inst, _, _ in proposals]
    proposed_poly = {
        inst: engine._world_courtyard(inst, x, y, rotations.get(inst))
        for inst, x, y in proposals
    }
    proposed_pose = {
        inst: (x, y, float(rotations.get(inst, ir.inst_rot[inst])))
        for inst, x, y in proposals
    }
    out: list[str] = []
    for i, (inst, x, y) in enumerate(proposals):
        ref = str(ir.instance_refdes[inst])
        x0, y0, x1, y1 = engine.bounds_for(inst)
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            out.append(f"bounds: {ref}")
        if engine._outline_region is not None and not _courtyard_in_outline(
            engine._outline_region, proposed_poly[inst]
        ):
            out.append(f"outline: {ref}")
        t = float(engine._soft_pin_mm[inst])
        if math.isfinite(t):
            ax = float(engine._soft_pin_anchor[inst, 0])
            ay = float(engine._soft_pin_anchor[inst, 1])
            if (
                math.isfinite(ax)
                and math.isfinite(ay)
                and math.hypot(x - ax, y - ay) > t + 1e-9
            ):
                out.append(f"soft_pin: {ref}")
        dx, dy = ir.inst_x - x, ir.inst_y - y
        d2 = dx * dx + dy * dy
        d2[moving] = math.inf
        for other in np.nonzero(d2 < (keepout + keepout[inst]) ** 2)[0]:
            if engine._sides_clear(inst, int(other)):
                continue
            if convex_polygons_overlap(
                proposed_poly[inst], engine._world_courtyard(int(other))
            ):
                out.append(f"courtyard: {ref}<>{ir.instance_refdes[int(other)]}")
        for hole_idx in range(len(engine._hole_polys)):
            if _hole_owned_by(ir, ir.mounting_holes[hole_idx], inst):
                continue
            hx, hy, hole_poly = engine._hole_at(hole_idx, proposed_pose)
            sep_h = keepout[inst] + engine._hole_radius[hole_idx]
            if (x - hx) ** 2 + (y - hy) ** 2 >= sep_h * sep_h:
                continue
            if convex_polygons_overlap(proposed_poly[inst], hole_poly):
                out.append(f"hole: {ref}")
        if engine._fixed_via_gap(inst, x, y, rotations.get(inst)) < (
            _FIXED_VIA_CLEARANCE_MM
        ):
            out.append(f"via_gap: {ref}")
        for other, ox, oy in proposals[i + 1 :]:
            if engine._sides_clear(inst, other):
                continue
            sep_ij = keepout[inst] + keepout[other]
            if (x - ox) ** 2 + (y - oy) ** 2 >= sep_ij * sep_ij:
                continue
            if convex_polygons_overlap(proposed_poly[inst], proposed_poly[other]):
                out.append(f"courtyard: {ref}<>{ir.instance_refdes[other]} (moved)")
        if len(out) >= limit:
            return out
    return out


def resolve_tilts(
    energy_of: Callable[[np.ndarray], float],
    ths: np.ndarray,
    rot: np.ndarray,
    *,
    free_rot: np.ndarray,
    th0: np.ndarray,
    min_gain: float = ROT_TILT_MIN_GAIN,
) -> tuple[np.ndarray, np.ndarray]:
    """90° preferred (spec D5, Q7): a snapped θ off the 90° grid survives
    only when its measured energy beats the same pose re-snapped to the
    nearest 90° by more than ``min_gain``. Greedy, per part, in index
    order. ``energy_of(ths)`` prices the pose with those θ (x, y held);
    ``rot`` is degrees in [0, 360), ``ths`` the matching −radians. A
    part at its seed θ (``th0``) is author or anneal intent, not a tilt,
    and is left alone. The gain is measured at the pose the caller is
    about to write: a tilt that earned it on a different x, y has to
    earn it again (the repair lane re-runs this after its descent)."""
    ths, rot = ths.copy(), rot.copy()
    for k in np.flatnonzero(free_rot):
        if abs(float(ths[k]) - float(th0[k])) <= 1e-6:
            continue
        if not 1e-6 < float(rot[k]) % 90.0 < 90.0 - 1e-6:
            continue
        e_tilt = energy_of(ths)
        keep = ths[k]
        # The tilt competes with the BEST of the four 90° orientations,
        # not only the nearest: the descended θ can sit nearer a
        # pin-flipped 90° (135° rounds to 90° or 180°), and pricing the
        # tilt against the wrong one let it "earn" the pad-spring and
        # wirelength the flip cost (esp32c3 gr503694 follow-up). The
        # nearest wins ties.
        near = round(float(rot[k]) / 90.0) * 90.0 % 360.0
        target, e_grid = near, math.inf
        for step in (0.0, 90.0, 180.0, 270.0):
            cand = (near + step) % 360.0
            ths[k] = -math.radians(cand)
            e_cand = energy_of(ths)
            if e_cand < e_grid:
                target, e_grid = cand, e_cand
        if e_tilt < e_grid - min_gain:
            ths[k] = keep
        else:
            ths[k] = -math.radians(target)
            rot[k] = target
    return ths, rot


def polish_placement(
    engine: Any,
    *,
    max_iter: int = 60,
    repair_iter: int = 60,
    lambda_t4: Sequence[float] = LAMBDA_T4_RAMP,
    gamma: float = 0.5,
    lambda_wells: float = 0.5,
    lambda_density: float = LAMBDA_DENSITY,
) -> PolishResult:
    """Descend x, y, θ on T1 + T4 + T5 + T6 + the D14 tree + bin density
    with a λ_T4 ramp (T10's rotation well joins from stage 1 while
    :data:`ROT_WELL_FROM_STAGE1` holds — Q7's drift question), then a
    Harden stage with the wells on (T9's coarsest raster first, then its
    full pitch set; T10 throughout — spec §Schedule 3, "wells off" in
    Spread), and try the exact snap in a fallback cascade: x, y onto each
    part's own raster + θ onto the nearest well grid point
    (:data:`ROT_SNAP_DEG`), then θ-only, then the old 90° snap, then the
    descended x, y at each part's SEED θ — the continuous descended θ is
    never written (gr503694); every written rotation is normalised to
    [0, 360). On the finest-grid lanes an off-90° θ survives only when
    its measured E, rotation cost included, beats the 90° re-snap by
    ``ROT_TILT_MIN_GAIN`` (Q7: priced, not forbidden). The first pose
    accepted commits — accept
    requires (a) lower E at the final weights, (b) the wirelength terms
    (T1 + supply tree) not worse than the seed beyond
    ``POLISH_WIRELENGTH_WORSEN``, (c) ``_placement_is_legal``
    taking every move at once, and (d) the authored measures' bill
    (``_money_measures``) not rising and no satisfied hard measure
    broken — the field energy does not model measures, so the polish may
    not trade them away (``measures_worse``) — a part the legality gate
    refuses is reverted to its seed and the gates re-run on the board
    actually written — the engine's ``total()`` delta is reported
    as ``anneal_delta``, a comparison value, not the accept gate (Reto
    2026-10-21: the field path's gate is pre-route DRC legality plus the
    field's own energy). Otherwise the board is left exactly as it was.

    When every lane fails the legality gate, one bounded repair descent
    runs before the refuse: θ pinned at the raster+finest-grid snap's wells,
    x/y re-descended at ``REPAIR_LAMBDA_T4`` capped by ``repair_iter`` —
    repair over refuse, the contract the signature's ``repair_iter``
    has always named. The stages' cost is bounded by
    ``POLISH_FTOL`` (early-stop when an iteration buys ~nothing) on top
    of the existing per-stage ``max_iter``, so a dense board's polish
    stops grinding evals it could never spend.

    A stage whose line search dies ABNORMAL is retried with θ pinned:
    at θ = 0 the courtyard witness is a tied-vertex kink whose
    subgradient stalls L-BFGS-B (gr503452), and the retry keeps x, y
    descending while θ stays a real variable everywhere it does move."""
    from precis.pcb.ir import COURTYARD_CLEARANCE_MM
    from precis.pcb.optimize import Move, MoveKind

    ir = engine.ir
    built = board_from_engine(engine)
    if built is None:
        return PolishResult(False, "unplaced", 0.0, 0.0, ())
    board, springs = built
    unpulled = board.unpulled_nets
    lay = Layout(board)
    if lay.size == 0:
        return PolishResult(False, "no_free_vars", 0.0, 0.0, (), unpulled_nets=unpulled)
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
        # gr503640 fix 2: mounting holes as fixed obstacles on BOTH
        # sides, at the same unclassed-pair minimum — the descent pushes
        # a part off a hole instead of discovering it at the gate.
        obstacles=_hole_obstacles(engine),
    )
    # T5's box is the gate's own per-part box (bounds_for shrinks the
    # placement domain by each part's keep-out radius), not the unshrunk
    # _placement_bounds: measured 2026-10-21, the uniform box let parts
    # park up to a keep-out radius outside the gate's domain (7 of 24
    # seeded boards refused on `bounds` alone).
    bound = np.array([engine.bounds_for(i) for i in range(ir.n_instances)])
    t5 = BoardEdge(lay, bound[:, :2], bound[:, 2:])
    t6 = PadSpring(lay, springs)
    t_tree = SupplyTree(lay)
    # The density domain is the union of the per-part legal boxes — the
    # places centres may actually go.
    t_density = BinDensity(
        lay, bound[:, :2].min(axis=0), bound[:, 2:].max(axis=0), bins=DENSITY_BINS
    )
    t9_coarse = GridWell(lay, pitches={10.0: 1.0})
    t9 = GridWell(lay)
    t10 = RotationWell(lay)

    def energy(l4: float, wells: Sequence[Term] = ()) -> Energy:
        return Energy(
            lay,
            [
                (1.0, t1),
                (l4, t4),
                (1.0, t5),
                (1.0, t6),
                (1.0, t_tree),
                (lambda_density, t_density),
            ]
            + [(lambda_wells, w) for w in wells],
        )

    final = energy(lambda_t4[-1], (t9, t10))
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
        return PolishResult(False, "bad_gradient", e0, e0, (), unpulled_nets=unpulled)
    # An authored soft pin whose travel disc misses the domain leaves an
    # empty box (x0 > x1): L-BFGS-B raises on it, crashing the place job
    # (gr504558). Refuse instead, naming the parts.
    empty = [
        str(ir.instance_refdes[k])
        for k in np.flatnonzero(lay.free_xy)
        if bound[k, 0] > bound[k, 2] or bound[k, 1] > bound[k, 3]
    ]
    if empty:
        return PolishResult(
            False,
            "empty_bounds",
            e0,
            e0,
            (),
            f"no legal box: {', '.join(empty)}",
            unpulled_nets=unpulled,
        )
    n_rot = int(lay.free_rot.sum())

    # L-BFGS-B gets the legality gate's own per-part box as bounds
    # (gr503640): an unbounded descent could — and on dense boards did —
    # walk parts outside the domain, and every such proposal died at the
    # legality gate as an all-or-nothing refusal. θ and vias stay free.
    xy_bounds: list[tuple[float | None, float | None]] = [(None, None)] * lay.size
    # z is [x(free), y(free), θ(free_rot), …]: y of slot k sits at
    # n_free + k, not n_xy + k (that index is θ — it clamped θ to the
    # y-box and overran z when fewer parts rotate than translate).
    n_free = n_xy // 2
    for slot, pi in enumerate(np.flatnonzero(lay.free_xy)):
        xy_bounds[slot] = (float(bound[pi, 0]), float(bound[pi, 2]))
        xy_bounds[n_free + slot] = (float(bound[pi, 1]), float(bound[pi, 3]))

    def descend(fg, z: _Z) -> _Z:
        """One L-BFGS-B stage. On an ABNORMAL stop — the θ = 0 courtyard
        witness kink's subgradient stalls the line search after 0–1
        steps, freezing x, y with it (gr503452) — retry once from the
        stall point with θ pinned, so the stage still descends x, y.
        ``ftol`` is the early-stop bound (:data:`POLISH_FTOL`): a stage
        whose iteration buys less than ~0.0001 of the energy has
        converged for placement purposes and stops there."""
        res = minimize(
            fg,
            z,
            jac=True,
            method="L-BFGS-B",
            bounds=xy_bounds,
            options={"maxiter": max_iter, "ftol": POLISH_FTOL},
        )
        if res.status == 2 and n_rot:
            bounds = list(xy_bounds)
            for k in range(n_xy, n_xy + n_rot):
                bounds[k] = (float(res.x[k]), float(res.x[k]))
            res = minimize(
                fg,
                res.x,
                jac=True,
                method="L-BFGS-B",
                bounds=bounds,
                options={"maxiter": max_iter, "ftol": POLISH_FTOL},
            )
        return res.x

    z = z0.copy()
    # ROT_WELL_FROM_STAGE1 (Q7's drift question): with the rotation well
    # on through the ramp, θ starts on a well bottom and stays there —
    # held at its start angle until Harden, but priced rather than pinned
    # (a strong enough real pull can still buy an off-90 pose).
    ramp_wells: tuple[Term, ...] = (t10,) if ROT_WELL_FROM_STAGE1 else ()
    for l4 in lambda_t4:
        z = descend(energy(l4, ramp_wells).value_grad, z)
    # Harden: wells come on after the λ_T4 ramp — T9's coarsest pitch
    # alone first (a fine well's basin could otherwise hold a part between
    # coarse multiples), then the full raster set.
    for wells in ((t9_coarse, t10), (t9, t10)):
        z = descend(energy(lambda_t4[-1], wells).value_grad, z)
    x, y, th, _ = lay.unpack(z)

    def _snap(do_xy: bool, snap_deg: float | None):
        """The exact snap (spec §Schedule 3): part centre onto its own
        raster, θ onto the nearest well — only over the free variables; a
        fixed or unmoved part keeps the pose it had. ``snap_deg=None``
        restores each part's SEED θ (the annealed value the polish started
        from): the continuous descended θ is never written — it is neither
        normalised nor on any grid (gr503694). Every returned rotation is
        normalised to [0, 360)."""
        xs, ys, ths = x.copy(), y.copy(), th.copy()
        if do_xy:
            for k in np.flatnonzero(lay.free_xy):
                p = board.parts[k].pitch
                if p is not None and math.isfinite(p) and p > 0.0:
                    xs[k] = round(float(xs[k]) / p) * p
                    ys[k] = round(float(ys[k]) / p) * p
            # A raster snap can push a centre past its own bounds box
            # (gr503640: "bounds: J_B" on free-bottom) — clamp it back;
            # the legality gate's box is the authority.
            for k in np.flatnonzero(lay.free_xy):
                xs[k] = min(max(float(xs[k]), bound[k, 0]), bound[k, 2])
                ys[k] = min(max(float(ys[k]), bound[k, 1]), bound[k, 3])
        rot = -np.degrees(ths)
        for k in np.flatnonzero(lay.free_rot):
            if snap_deg is None or abs(ths[k] - board.th0[k]) <= 1e-6:
                rot[k] = -math.degrees(board.th0[k])
            else:
                rot[k] = round(rot[k] / snap_deg) * snap_deg
            rot[k] %= 360.0
            ths[k] = -math.radians(rot[k])
        rot %= 360.0
        return xs, ys, ths, rot

    def _resolve_tilts(xs, ys, ths, rot):
        """:func:`resolve_tilts` at this pose's x, y, priced by the final
        energy."""
        ths, rot = resolve_tilts(
            lambda t: float(final.value_grad(lay.pack_from(xs, ys, t))[0]),
            ths,
            rot,
            free_rot=lay.free_rot,
            th0=board.th0,
        )
        return xs, ys, ths, rot

    def _wirelength(xs, ys, ths) -> float:
        """The wirelength terms of one pose — T1's pulls plus the D14
        supply tree (rails are T1-exempt but still wire)."""
        p = lay.pack_from(xs, ys, ths)
        return _wirelength_terms(p, t1, t_tree)

    wl0 = _wirelength(*lay.unpack(z0)[:3])

    def _off90() -> int:
        """Placed instances off the 90° grid — the gripe's per-board count
        (gr503694), read from the pose just written."""
        return sum(
            1
            for i in range(ir.n_instances)
            if 1e-6 < float(ir.inst_rot[i]) % 90.0 < 90.0 - 1e-6
        )

    # Fullest snap first; each weaker pose is tried only if the stronger
    # one buys no E gain or fails the all-at-once legality gate — a
    # raster/45° snap that collides should cost the snap, not the descent.
    reason = "no_gain"
    e1 = e0
    gate_refusals: list[tuple[str, float, tuple[int, ...], str]] = []
    refused: tuple[int, ...] = ()
    detail = ""
    lanes = {
        (True, ROT_SNAP_DEG): f"raster+{ROT_SNAP_DEG:g}deg",
        (False, ROT_SNAP_DEG): f"{ROT_SNAP_DEG:g}deg",
        (False, 90.0): "90deg",
        (False, None): "seed-theta",
    }

    def _apply_honouring_measures(move: Move) -> bool:
        """Apply ``move`` unless it worsens the authored measures. The
        field energy does not model proximity/separation/align measures,
        so the polish must not trade them away: the anneal pays for them
        in ``_money_measures`` and the polish may not raise that bill
        (nor break a satisfied hard measure). Undone on refusal, with
        ``reason = "measures_worse"``."""
        nonlocal reason, refused, detail
        if not engine._measure_pairs:
            engine.apply_move(move)
            return True
        usd0 = engine._money_measures
        hard0 = _hard_measures_met()
        engine.apply_move(move)
        usd1 = engine._money_measures
        if usd1 <= usd0 + 1e-9 and hard0 <= _hard_measures_met():
            return True
        engine.undo_move(move)
        reason = "measures_worse"
        refused = tuple(move.instances)
        detail = f"measures {usd0:.4g} -> {usd1:.4g} USD"
        return False

    def _hard_measures_met() -> frozenset[int]:
        """Indices of the hard, steering measures currently satisfied."""
        return frozenset(
            idx
            for idx, (_a, _b, spec) in enumerate(engine._measure_pairs)
            if spec.hard and spec.weight > 0.0 and engine._measure_usd.get(idx) == 0.0
        )

    def _accept(
        xs: np.ndarray, ys: np.ndarray, ths: np.ndarray, rot: np.ndarray, lane: str
    ) -> PolishResult | None:
        """The accept gates for one candidate pose — E gain at the final
        weights, the never-worse wirelength guard, the all-at-once (then
        partial) legality gate and the measures gate — applying the move
        on success. A part the legality gate refuses is reverted to its
        seed pose and the rest kept (partial accept, gr503640); E and
        wirelength are then re-measured on exactly the board that is
        written, reverted parts at their seed (gr504558).
        ``None`` when a gate refuses; ``reason``/``refused``/``detail``
        carry the cause, ``reason`` reset per call so no earlier lane's
        refusal leaks into this one."""
        nonlocal reason, e1, refused, detail
        reason = "no_gain"
        refused = ()
        e1 = final.value_grad(lay.pack_from(xs, ys, ths))[0]
        if not e1 < e0:
            return None
        # Wirelength never-worse: spread/courtyard/well gains cannot pay
        # for a longer ratsnest (POLISH_WIRELENGTH_WORSEN).
        if _wirelength(xs, ys, ths) > wl0 * (1.0 + POLISH_WIRELENGTH_WORSEN):
            reason = "wirelength_worse"
            return None
        old: dict[int, tuple[float, float, float]] = {}
        new: dict[int, tuple[float, float, float]] = {}
        for i in range(ir.n_instances):
            r_old = 0.0 if math.isnan(float(ir.inst_rot[i])) else float(ir.inst_rot[i])
            r_new = float(rot[i]) % 360.0 if lay.free_rot[i] else r_old
            # θ compared on the circle: normalising an unmoved part's raw
            # rotation into [0, 360) (-90 -> 270) is not a move (gr504558).
            d_rot = abs((r_new - r_old + 180.0) % 360.0 - 180.0)
            d_xy = max(
                abs(float(xs[i]) - float(ir.inst_x[i])),
                abs(float(ys[i]) - float(ir.inst_y[i])),
            )
            if max(d_xy, d_rot) > 1e-6:
                old[i] = (float(ir.inst_x[i]), float(ir.inst_y[i]), r_old)
                new[i] = (float(xs[i]), float(ys[i]), r_new)
        moved = list(new)
        if not moved:
            return None
        in_outline = engine._outline_region is not None

        def legal(keep: Sequence[int]) -> bool:
            return bool(
                engine._placement_is_legal(
                    [(i, new[i][0], new[i][1]) for i in keep],
                    rotations={i: new[i][2] for i in keep},
                    in_outline=in_outline,
                )
            )

        def violations(keep: Sequence[int], limit: int) -> list[str]:
            return _legality_violations(
                engine,
                [(i, new[i][0], new[i][1]) for i in keep],
                {i: new[i][2] for i in keep},
                limit=limit,
            )

        keep = list(moved)
        reverted: list[int] = []
        if not legal(keep):
            # Partial accept (gr503640): a gate failure refuses only the
            # offending parts — revert them and keep the legal rest.
            bad = violations(keep, ir.n_instances)
            bad_refdes = {
                r for v in bad for r in v.split(":", 1)[1].strip().split("<>")
            }
            by_ref = {str(ir.instance_refdes[i]): i for i in keep}
            drop = {by_ref[r] for r in bad_refdes if r in by_ref}
            keep = [i for i in keep if i not in drop]
            if not keep or not drop:
                reason = "illegal"
                refused = tuple(moved)
                detail = "; ".join(bad[:4])
                return None
            reverted += sorted(drop)
            if not legal(keep):
                reason = "illegal"
                refused = tuple(keep)
                detail = "; ".join(violations(keep, 4))
                return None
        written = lane
        if reverted:
            # The gates measure the board actually written: kept parts at
            # their candidate pose, every other part at its seed pose.
            xw, yw, thw = board.x0.copy(), board.y0.copy(), board.th0.copy()
            for i in keep:
                xw[i], yw[i], thw[i] = new[i][0], new[i][1], -math.radians(new[i][2])
            # The kept tilts were priced with the reverted parts at their
            # candidate pose; re-price them on the written board, where a
            # tilt that no longer earns ROT_TILT_MIN_GAIN goes back to the
            # 90° grid (esp32c3 seed 10 kept J1 330°, U2 165°, U3 45° with
            # J2 reverted). A re-snapped part re-meets the legality gate.
            rw = -np.degrees(thw) % 360.0
            _xw, _yw, thw2, rw2 = _resolve_tilts(xw, yw, thw, rw)
            resnapped = [i for i in keep if abs(rw2[i] - rw[i]) > 1e-6]
            if resnapped:
                thw = thw2
                for i in resnapped:
                    new[i] = (new[i][0], new[i][1], float(rw2[i]))
                if not legal(keep):
                    reason = "illegal"
                    refused = tuple(keep)
                    detail = "; ".join(violations(keep, 4))
                    return None
            e1 = final.value_grad(lay.pack_from(xw, yw, thw))[0]
            if not e1 < e0:
                return None
            if _wirelength(xw, yw, thw) > wl0 * (1.0 + POLISH_WIRELENGTH_WORSEN):
                reason = "wirelength_worse"
                return None
            written = (
                f"{lane}; partial: reverted "
                f"{sorted(str(ir.instance_refdes[i]) for i in reverted)}"
            )
        # The accept gate above is legality + field energy only (Reto
        # 2026-10-21): the annealer's total() — its risk term prices
        # margins the field path does not optimize and can jump ~10^3x on
        # a moved part — is measured after the move and reported as
        # anneal_delta, a comparison value, never a reason to undo
        # (gr503458).
        before = engine.total()
        move = Move(
            MoveKind.TRANSLATE,
            tuple(keep),
            tuple(old[i] for i in keep),
            tuple(new[i] for i in keep),
        )
        if not _apply_honouring_measures(move):
            return None
        return PolishResult(
            True,
            "applied",
            e0,
            e1,
            tuple(keep),
            f"{written}; off-90 {_off90()}/{ir.n_instances}",
            anneal_delta=engine.total() - before,
            unpulled_nets=unpulled,
        )

    def _repair() -> PolishResult | None:
        """The bounded repair descent ``repair_iter`` has always named
        (the stage the lane cascade dropped): θ pinned at the raster+finest-grid
        snap's wells, x/y re-descend at :data:`REPAIR_LAMBDA_T4` to
        re-open the gaps the descent or the snap closed, then the same
        accept gates. A pose it cannot make legal is the same refuse the
        cascade reached; `repair_iter` keeps its cost bounded on top of
        :data:`POLISH_EVAL_BUDGET`."""
        xs, ys, ths, rot = _snap(True, ROT_SNAP_DEG)
        xs, ys, ths, rot = _resolve_tilts(xs, ys, ths, rot)
        rep = energy(REPAIR_LAMBDA_T4, (t9, t10))
        free = np.flatnonzero(lay.free_xy)

        def _descend_xy(xs, ys, ths):
            w0 = np.concatenate([xs[free], ys[free]])

            def rep_fg(w: _Z) -> tuple[float, _Z]:
                xx, yy = xs.copy(), ys.copy()
                xx[free] = w[: w.size // 2]
                yy[free] = w[w.size // 2 :]
                v, g = rep.value_grad(lay.pack_from(xx, yy, ths.copy()))
                return v, g[:n_xy]

            w = minimize(
                rep_fg,
                w0,
                jac=True,
                method="L-BFGS-B",
                bounds=xy_bounds[:n_xy],
                options={"maxiter": repair_iter},
            ).x
            xr, yr = xs.copy(), ys.copy()
            xr[free] = w[: w.size // 2]
            yr[free] = w[w.size // 2 :]
            return xr, yr

        xr, yr = _descend_xy(xs, ys, ths)
        # The tilts above were priced at the snapped, still-colliding
        # pose, where turning a part off 90° buys back courtyard overlap
        # the descent then removes anyway. Re-price them at the repaired
        # pose: a tilt that no longer earns ROT_TILT_MIN_GAIN goes back
        # to the 90° grid, and x, y re-descend once around the new θ
        # (esp32c3 seed 4 wrote J1 15°, J2 195°, U1 285°, U2 330° here).
        _xr, _yr, ths2, rot2 = _resolve_tilts(xr, yr, ths, rot)
        if not np.array_equal(rot2, rot):
            ths, rot = ths2, rot2
            xr, yr = _descend_xy(xr, yr, ths)
            _xr, _yr, ths, rot = _resolve_tilts(xr, yr, ths, rot)
        tilted = (xr, yr, ths, rot)
        n_tilt = sum(
            1
            for k in np.flatnonzero(lay.free_rot)
            if abs(float(ths[k]) - float(board.th0[k])) > 1e-6
            and 1e-6 < float(rot[k]) % 90.0 < 90.0 - 1e-6
        )
        if not n_tilt:
            return _accept(*tilted, "repaired")

        # Even re-priced, a tilt is measured against its 90° re-snap at an
        # x, y descended AROUND the tilt — the re-snapped part collides
        # there, so the tilt "earns" the overlap it caused (esp32c3 seeds
        # 8/9/14/15/17/20 wrote 3-4 of 29 off 90°; CI x86 seeds 1-2 too).
        # Price the 90° grid fairly: every tilt onto its best 90°
        # orientation, x, y given the same two descents the tilted board
        # had, and keep the tilts only when the tilted board beats that by
        # ROT_TILT_MIN_GAIN per tilt (D5). The loser is the fallback when
        # the winner fails an accept gate.
        def to_grid(xs, ys, ths, rot):
            return resolve_tilts(
                lambda t: float(final.value_grad(lay.pack_from(xs, ys, t))[0]),
                ths,
                rot,
                free_rot=lay.free_rot,
                th0=board.th0,
                min_gain=math.inf,
            )

        thg, rotg = to_grid(xr, yr, ths, rot)
        xg, yg = _descend_xy(xr, yr, thg)
        thg2, rotg2 = to_grid(xg, yg, ths, rot)
        if not np.array_equal(rotg2, rotg):
            thg, rotg = thg2, rotg2
        xg, yg = _descend_xy(xg, yg, thg)
        e_grid = float(final.value_grad(lay.pack_from(xg, yg, thg))[0])
        e_tilt = float(final.value_grad(lay.pack_from(xr, yr, ths))[0])
        order = [(xg, yg, thg, rotg), tilted]
        if e_tilt < e_grid - ROT_TILT_MIN_GAIN * n_tilt:
            order.reverse()
        for cand in order:
            applied = _accept(*cand, "repaired")
            if applied is not None:
                return applied
        return None

    for do_xy, snap_deg in (
        (True, ROT_SNAP_DEG),
        (False, ROT_SNAP_DEG),
        (False, 90.0),
        (False, None),
    ):
        xs, ys, ths, rot = _snap(do_xy, snap_deg)
        if snap_deg == ROT_SNAP_DEG:
            xs, ys, ths, rot = _resolve_tilts(xs, ys, ths, rot)
        applied = _accept(xs, ys, ths, rot, lanes[(do_xy, snap_deg)])
        if applied is not None:
            return applied
        if reason != "no_gain":
            gate_refusals.append((reason, e1, refused, detail))
    # The repair runs when ANY lane met the legality gate, not only the
    # last one (gr504558: a sticky ``reason`` made it lane-order dependent).
    if any(r[0] == "illegal" for r in gate_refusals):
        applied = _repair()
        if applied is not None:
            return applied
        if reason != "no_gain":
            gate_refusals.append((reason, e1, refused, detail))
    # Report the last gate that refused, else no_gain.
    if gate_refusals:
        reason, e1, refused, detail = gate_refusals[-1]
    if reason == "illegal":
        _LOG.warning("field_polish refused: illegal (%s)", detail)
    return PolishResult(False, reason, e0, e1, refused, detail, unpulled_nets=unpulled)

"""Free-molecular (Knudsen) test-particle Monte Carlo — slice 3 of
docs/backlog/se-flow-kernel.md.

Where the continuum kernel holds only at ``Kn <= 0.01``, the same
dimension-free claim is made by a test-particle propagator with diffuse
wall reflection. Reduced units again: the lumen's axial extent ``L`` maps
to ``x* in [0, 1]`` and every other length by the same scale, so the
reduced particle speed is 1 — ``t* = t v̄/L`` with
``v̄ = sqrt(8 kT / π m)``, a flight of reduced length ``s`` advancing
``t*`` by ``s``. Transmission then depends on shape and the
accommodation coefficient ``alpha`` only — the spec's dimension-free
claim for the nano regime.

Geometry is a :class:`Lumen`: a free volume bounded by a wall and —
unless closed — two open end planes at ``x* = 0`` and ``x* = 1``. Two
wall implementations: :class:`CylinderLumen` (the analytic reference
whose transmission reproduces the Clausing table) and :class:`AtomWall`
(the union of vdW-radius spheres at the generated structure's atom
sites — a hexfold lumen's corrugated wall; ``generated.sites`` in the
spec's words). :func:`propagate` returns the same :class:`Paths` as the
continuum :func:`trace`: ``breakthrough`` is the transmission fraction,
``residence`` the ``t*`` of resolution, ``frames`` the same
``(K, n, 3)`` player blob — so the flow measures and the slice-2b
payload work unchanged.

Particles seed uniformly over the inlet aperture with cosine-weighted
directions (an effusive inlet — the Clausing setup), fly ballistically,
and at each wall hit re-emit diffuse (Knudsen cosine law about the
free-volume normal) with probability ``alpha`` or specularly with
``1 - alpha``. A particle that recrosses the inlet plane is *returned*;
one that crosses the outlet plane is *transmitted*. A particle still
bouncing when ``max_flights`` runs out — or one that leaks sideways
through a sparse atom wall — is trapped (``residence`` NaN), so
``trapped_fraction`` doubles as a leak/validity flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .tracer import Paths

#: Outer-loop cap — a safety budget on wall bounces per run, not the
#: normal stop (which is "every particle resolved"). Lifted via the
#: ``max_flights`` argument when a long tube's tail needs more.
MAX_FLIGHTS = 20000

#: Ray marching below this reduced distance from a surface treats the
#: hit as a self-intersection of the bounce just made.
SURF_EPS = 1e-9

_XHAT = np.array([1.0, 0.0, 0.0])


def _cosine_dirs(rng: np.random.Generator, normals: np.ndarray) -> np.ndarray:
    """Knudsen re-emission directions (P, 3): cosine-weighted about each
    row's unit normal. ``cos θ = sqrt(u)``, ``phi`` uniform — the diffuse
    wall law, and the effusive-inlet direction law when ``normals`` is
    all +x̂."""
    p = normals.shape[0]
    mu = np.sqrt(rng.random(p))
    phi = 2.0 * np.pi * rng.random(p)
    # an orthonormal tangent basis per normal, deterministic in the data
    n = normals
    alt = np.where(np.abs(n[:, :1]) < 0.9, _XHAT, np.array([0.0, 1.0, 0.0]))
    t1 = np.cross(n, alt)
    t1 /= np.linalg.norm(t1, axis=1, keepdims=True)
    t2 = np.cross(n, t1)
    s = np.sqrt(np.maximum(0.0, 1.0 - mu * mu))
    return mu[:, None] * n + s[:, None] * (
        np.cos(phi)[:, None] * t1 + np.sin(phi)[:, None] * t2
    )


class Lumen(Protocol):
    """The propagator's geometry: a free volume bounded by a wall and —
    unless closed — two open end planes at ``x* = 0`` and ``x* = 1``.

    Everything is reduced: ``x*`` runs along the lumen axis (the inlet
    plane at 0, the outlet at 1) and ``(y*, z*)`` are the transverse
    coordinates of the same reduced space."""

    @property
    def has_ports(self) -> bool:  # False for a closed lumen (ends are walls)
        ...

    def hit(self, pos: np.ndarray, dirs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """First wall hit along each ray ``pos + t dirs``:
        ``(t, normals)`` — ``t = inf`` where no hit; ``normals`` (P, 3)
        unit, pointing into the free volume."""
        ...

    def escape(self, pos: np.ndarray, dirs: np.ndarray) -> np.ndarray:
        """``t`` at which each ray leaves the lumen *sideways* (past the
        wall's outer bound); ``inf`` where that cannot happen."""
        ...

    def seed_inlet(self, rng: np.random.Generator, n: int) -> np.ndarray:
        """(n, 3) seed positions, uniform over the inlet aperture in
        free space."""
        ...

    def paths_coords(self, pos: np.ndarray) -> np.ndarray:
        """(P, 3) — ``(x*, y*, psi)`` per position: ``psi`` is the
        transverse fraction across the free cross-section
        (0 = low wall, 1 = high wall), the lumen's analogue of the
        chamber's gap fraction."""
        ...


def _disk_seeds(rng: np.random.Generator, n: int, radius: float) -> np.ndarray:
    """Uniform points in the x*=0 disk of the given reduced radius."""
    u = rng.random(n)
    phi = 2.0 * np.pi * rng.random(n)
    rr = radius * np.sqrt(u)
    return np.column_stack([np.zeros(n), rr * np.cos(phi), rr * np.sin(phi)])


@dataclass(frozen=True)
class CylinderLumen:
    """The analytic straight-tube lumen: a smooth wall at reduced
    ``radius_star`` and open ends at ``x* = 0, 1`` — the reference whose
    transmission is the Clausing table. ``closed=True`` turns both ends
    into diffuse walls (the equilibrium vessel: no ports, nothing
    resolves)."""

    radius_star: float
    closed: bool = False

    @property
    def has_ports(self) -> bool:
        return not self.closed

    def hit(self, pos: np.ndarray, dirs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        p = pos.shape[0]
        r = self.radius_star
        a = dirs[:, 1] ** 2 + dirs[:, 2] ** 2
        b = 2.0 * (pos[:, 1] * dirs[:, 1] + pos[:, 2] * dirs[:, 2])
        c = pos[:, 1] ** 2 + pos[:, 2] ** 2 - r * r
        disc = np.maximum(b * b - 4.0 * a * c, 0.0)
        t = np.where(
            a > 1e-15, (-b + np.sqrt(disc)) / np.where(a > 1e-15, 2.0 * a, 1.0), np.inf
        )
        t = np.where(t > SURF_EPS, t, np.inf)
        normals = np.zeros((p, 3))
        hit = np.isfinite(t)
        if hit.any():
            ph = pos[hit] + t[hit, None] * dirs[hit]
            nrm = -ph / r
            nrm[:, 0] = 0.0
            normals[hit] = nrm
        if self.closed:
            dx = dirs[:, 0]
            # end planes x*=0 (normal +x̂) and x*=1 (normal -x̂), both diffuse
            t0 = np.where(dx < -1e-15, -pos[:, 0] / dx, np.inf)
            t1 = np.where(dx > 1e-15, (1.0 - pos[:, 0]) / dx, np.inf)
            cap = np.minimum(t0, t1)
            better = (cap < t) & (cap > SURF_EPS)
            if better.any():
                t = np.where(better, cap, t)
                normals[better] = np.where(
                    (t0 < t1)[better, None],
                    np.broadcast_to(_XHAT, (int(better.sum()), 3)),
                    np.broadcast_to(-_XHAT, (int(better.sum()), 3)),
                )
        return t, normals

    def escape(self, pos: np.ndarray, dirs: np.ndarray) -> np.ndarray:
        return np.full(pos.shape[0], np.inf)

    def seed_inlet(self, rng: np.random.Generator, n: int) -> np.ndarray:
        return _disk_seeds(rng, n, self.radius_star)

    def paths_coords(self, pos: np.ndarray) -> np.ndarray:
        r = self.radius_star
        return np.column_stack(
            [pos[:, 0], pos[:, 1], np.clip((pos[:, 2] + r) / (2.0 * r), 0.0, 1.0)]
        )


#: particles per chunk in :meth:`AtomWall.hit` — bounds the (C, M)
#: temporaries of the all-sites ray solve.
_HIT_CHUNK = 512


@dataclass(frozen=True)
class AtomWall:
    """A lumen walled by vdW spheres at the generated structure's atom
    sites: the corrugated free surface a gas molecule actually sees
    inside a hexfold tube. ``centres``/``radii`` are reduced — the
    lumen's axial extent is the unit (ports at the two axial extremes
    of the atom cloud, ``x* = 0`` and ``1``). ``r_in`` is the inscribed
    free radius (``min ρ_i - r_i``: every point closer to the axis is
    guaranteed outside every sphere), ``r_out`` the outer bound
    (``max ρ_i + r_i`` — a ray reaching it has leaked), ``r_eff`` the
    Clausing reference radius ``R_axis - r_vdW`` (median ρ minus median
    r), ``extent`` the absolute axial extent in the input's units."""

    centres: np.ndarray  # (M, 3)
    radii: np.ndarray  # (M,)
    r_in: float
    r_out: float
    r_eff: float
    extent: float

    @property
    def has_ports(self) -> bool:
        return True

    @classmethod
    def from_atoms(
        cls,
        positions: np.ndarray,
        radii: np.ndarray,
        *,
        axis: int | None = None,
        min_aspect: float = 1.25,
    ) -> AtomWall:
        """Reduce the site cloud to the lumen frame. ``positions``
        (M, 3) and ``radii`` (M,) share any absolute unit — only their
        ratio enters. ``axis`` is the lumen's axial direction; default
        the longest-extent axis, refused when no axis is at least
        ``min_aspect`` × both transverse extents (a sheet or a blob is
        not a lumen). Also refused: a sealed bore — an atom so close to
        the axis that ``min ρ_i - r_i <= 0`` (a tunnel's end sheet)."""
        pos = np.asarray(positions, dtype=float)
        rad = np.asarray(radii, dtype=float)
        if pos.ndim != 2 or pos.shape[1] != 3 or not pos.shape[0]:
            raise ValueError("an atom wall needs (M, 3) site positions")
        if rad.shape != (pos.shape[0],) or np.any(rad <= 0):
            raise ValueError("an atom wall needs (M,) positive site radii")
        lo, hi = pos.min(axis=0), pos.max(axis=0)
        extents = hi - lo
        if not np.all(np.isfinite(extents)) or not np.all(extents > 0):
            raise ValueError("an atom wall needs a bounded site cloud")
        ax = int(np.argmax(extents)) if axis is None else int(axis)
        lateral = [a for a in range(3) if a != ax]
        aspect = extents[ax] / max(extents[lateral[0]], extents[lateral[1]])
        if aspect < min_aspect:
            raise ValueError(
                f"no clear lumen axis — the site cloud's longest extent is "
                f"only {aspect:.2f}× its widest transverse one "
                f"(a lumen needs ≥ {min_aspect:g})"
            )
        L = float(extents[ax])
        red = np.empty_like(pos)
        red[:, 0] = (pos[:, ax] - lo[ax]) / L
        for k, a in enumerate(lateral):
            red[:, k + 1] = (pos[:, a] - 0.5 * (lo[a] + hi[a])) / L
        rr = rad / L
        rho = np.hypot(red[:, 1], red[:, 2])
        r_in = float(np.min(rho - rr))
        if r_in <= 0.0:
            raise ValueError(
                "the bore is sealed — a site sits at the axis (min ρ - r "
                f"= {r_in:.3g}); a lumen needs open ends"
            )
        return cls(
            centres=red,
            radii=rr,
            r_in=r_in,
            r_out=float(np.max(rho + rr)),
            r_eff=float(np.median(rho) - np.median(rr)),
            extent=L,
        )

    def hit(self, pos: np.ndarray, dirs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """First sphere entry along each ray — the wall. The minimum
        entry point over all sites is the union surface: it cannot lie
        inside another sphere (that sphere would have been entered
        earlier). Vectorised per chunk over sites; O(P × M) per call —
        the lumen-scale cost of a corrugated wall."""
        p = pos.shape[0]
        t = np.full(p, np.inf)
        which = np.full(p, -1, dtype=int)
        c2 = np.einsum("md,md->m", self.centres, self.centres)
        for s in range(0, p, _HIT_CHUNK):
            q, d = pos[s : s + _HIT_CHUNK], dirs[s : s + _HIT_CHUNK]
            proj = (d @ self.centres.T) - np.einsum("cd,cd->c", q, d)[:, None]
            d2 = (
                c2[None, :]
                + np.einsum("cd,cd->c", q, q)[:, None]
                - 2.0 * (q @ self.centres.T)
            ) - proj * proj
            disc = self.radii[None, :] ** 2 - d2
            entry = np.where(disc > 0.0, proj - np.sqrt(np.maximum(disc, 0.0)), np.inf)
            entry = np.where(entry > SURF_EPS, entry, np.inf)
            m = np.argmin(entry, axis=1)
            tm = entry[np.arange(q.shape[0]), m]
            t[s : s + _HIT_CHUNK] = tm
            which[s : s + _HIT_CHUNK] = np.where(np.isfinite(tm), m, -1)
        normals = np.zeros((p, 3))
        ok = which >= 0
        if ok.any():
            i = which[ok]
            ph = pos[ok] + t[ok, None] * dirs[ok]
            normals[ok] = (ph - self.centres[i]) / self.radii[i, None]
        return t, normals

    def escape(self, pos: np.ndarray, dirs: np.ndarray) -> np.ndarray:
        """``t`` where the ray's transverse radius reaches ``r_out``
        going outward — a lateral leak past the wall. ``0`` for a
        position already outside (a leak the bounce missed)."""
        r0 = np.hypot(pos[:, 1], pos[:, 2])
        a = dirs[:, 1] ** 2 + dirs[:, 2] ** 2
        b = 2.0 * (pos[:, 1] * dirs[:, 1] + pos[:, 2] * dirs[:, 2])
        c = pos[:, 1] ** 2 + pos[:, 2] ** 2 - self.r_out**2
        disc = np.maximum(b * b - 4.0 * a * c, 0.0)
        root = (-b + np.sqrt(disc)) / np.where(a > 1e-15, 2.0 * a, 1.0)
        t = np.where((a > 1e-15) & (root > SURF_EPS), root, np.inf)
        return np.where(r0 >= self.r_out, 0.0, t)

    def seed_inlet(self, rng: np.random.Generator, n: int) -> np.ndarray:
        """Uniform over the inscribed free disk — the conservative
        aperture; the grooves between sphere bulges are left to the
        propagation, which is the corrugation being measured."""
        return _disk_seeds(rng, n, self.r_in)

    def paths_coords(self, pos: np.ndarray) -> np.ndarray:
        r = self.r_eff
        return np.column_stack(
            [pos[:, 0], pos[:, 1], np.clip((pos[:, 2] + r) / (2.0 * r), 0.0, 1.0)]
        )


def _interp_frames(
    hist_t: np.ndarray,
    hist_p: np.ndarray,
    record_frames: int,
    resolved: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-particle time histories → a ``(K, n, 3)`` frame blob at
    ``frame_t = linspace(0, t_end, K)`` — the same post-hoc interp the
    continuum tracer runs, but each particle marches on its own clock
    (flight lengths differ), so interpolation is per particle along its
    piecewise-straight path — exact for ballistic flight. Resolved
    particles hold their final position. ``t_end`` is the last
    resolution time when anything resolved (the grid covers the whole
    transit); on a closed run it is the swarm's *smallest* clock, so
    every frame is real data for every particle."""
    final_t = hist_t[-1]
    t_end = float(final_t[resolved].max()) if resolved.any() else float(final_t.min())
    frame_t = np.linspace(0.0, t_end, record_frames)
    n = hist_t.shape[1]
    frames = np.empty((record_frames, n, 3), dtype=np.float32)
    for i in range(n):
        ti = hist_t[:, i]
        for d in range(3):
            frames[:, i, d] = np.interp(frame_t, ti, hist_p[:, i, d])
    return frames, frame_t


def propagate(
    lumen: Lumen,
    n: int,
    alpha: float,
    seed_rng: np.random.Generator | int,
    record_frames: int = 0,
    max_flights: int = MAX_FLIGHTS,
) -> Paths:
    """Run the swarm through the lumen and record transmissions.

    ``alpha`` is required — the tangential accommodation coefficient,
    the one modelling parameter with physics in it (the wall-law
    analogue of the continuum kernel's required ``d_v_star``):
    ``alpha = 1`` is pure diffuse re-emission, ``0`` pure specular.
    ``max_flights`` is the bounce safety budget — the normal stop is
    every particle resolved (transmitted, returned, or trapped), so the
    cap only bites on a leaking or bouncing-forever tail.

    Returns the :class:`~precis_flow.tracer.Paths` contract:
    ``breakthrough`` = transmission, ``broke``/``exited`` = transmitted
    / returned, ``residence`` = ``t*`` of resolution (NaN for trapped),
    ``positions``/``frames`` in ``(x*, y*, psi)``. Same ``seed_rng``
    gives bitwise-equal ``Paths``."""
    if not 0.0 <= alpha <= 1.0:
        raise ValueError(f"alpha must lie in [0, 1], got {alpha!r}")
    if n < 1:
        raise ValueError(f"n must be a positive particle count, got {n!r}")
    rng = (
        seed_rng
        if isinstance(seed_rng, np.random.Generator)
        else np.random.default_rng(seed_rng)
    )

    pos = lumen.seed_inlet(rng, n)
    dirs = _cosine_dirs(rng, np.broadcast_to(_XHAT, (n, 3)).copy())
    t_acc = np.zeros(n)
    broke = np.zeros(n, dtype=bool)
    exited = np.zeros(n, dtype=bool)
    trapped = np.zeros(n, dtype=bool)
    cross = np.full((n, 2), np.nan)
    residence = np.full(n, np.nan)
    active = np.ones(n, dtype=bool)

    hist_t: list[np.ndarray] = []
    hist_p: list[np.ndarray] = []
    if record_frames > 0:
        hist_t.append(t_acc.copy())
        hist_p.append(lumen.paths_coords(pos))

    steps_used = 0
    for step in range(max_flights):
        steps_used = step + 1
        if (broke | exited | trapped).mean() >= 1.0 or not active.any():
            break
        idx = np.where(active)[0]
        p, d = pos[idx], dirs[idx]
        k = idx.size

        t_wall, normals = lumen.hit(p, d)
        t_esc = lumen.escape(p, d)
        if lumen.has_ports:
            dx, px = d[:, 0], p[:, 0]
            t_port = np.where(
                dx > 1e-15, (1.0 - px) / dx, np.where(dx < -1e-15, -px / dx, np.inf)
            )
            t_port = np.where(t_port >= 0.0, t_port, np.inf)
        else:
            t_port = np.full(k, np.inf)
        t = np.minimum(np.minimum(t_wall, t_esc), t_port)
        fin = np.isfinite(t)

        # advance to the event; whatever has no event is trapped
        p_new = p.copy()
        p_new[fin] = p[fin] + t[fin, None] * d[fin]
        t_acc[idx[fin]] += t[fin]
        pos[idx] = p_new
        trapped[idx[~fin]] = True
        active[idx[~fin]] = False

        is_port = np.isfinite(t_port) & (t_port <= t_wall) & (t_port <= t_esc)
        out_i = idx[is_port & (d[:, 0] > 0)]
        in_i = idx[is_port & (d[:, 0] <= 0)]
        pc = lumen.paths_coords(p_new)
        if out_i.size:
            broke[out_i] = True
            residence[out_i] = t_acc[out_i]
            cross[out_i] = pc[is_port & (d[:, 0] > 0), 1:]
            active[out_i] = False
        if in_i.size:
            exited[in_i] = True
            residence[in_i] = t_acc[in_i]
            active[in_i] = False

        is_esc = np.isfinite(t_esc) & (t_esc < t_wall) & (t_esc <= t_port)
        esc_i = idx[is_esc]
        if esc_i.size:
            trapped[esc_i] = True  # leaked sideways: the leak flag
            active[esc_i] = False

        is_wall = np.isfinite(t_wall) & (t_wall < t_port) & (t_wall <= t_esc)
        w = np.where(is_wall)[0]
        if w.size:
            gi = idx[w]
            nrm = normals[w]
            pos[gi] = p_new[w] + nrm * SURF_EPS
            coin = rng.random(w.size)
            diffuse = coin < alpha
            d_new = d[w].copy()
            if diffuse.any():
                d_new[diffuse] = _cosine_dirs(rng, nrm[diffuse])
            if (~diffuse).any():
                sp = ~diffuse
                nd = np.einsum("cd,cd->c", d[w][sp], nrm[sp])
                d_new[sp] = d[w][sp] - 2.0 * nd[:, None] * nrm[sp]
            d_new /= np.linalg.norm(d_new, axis=1, keepdims=True)
            dirs[gi] = d_new

        if record_frames > 0:
            hist_t.append(t_acc.copy())
            hist_p.append(lumen.paths_coords(pos))

    positions = lumen.paths_coords(pos)
    frames = frame_t = None
    if record_frames > 0:
        ht = np.stack(hist_t)
        hp = np.stack(hist_p)
        frames, frame_t = _interp_frames(ht, hp, record_frames, broke | exited)

    return Paths(
        n_particles=n,
        breakthrough=float(broke.mean()) if n else 0.0,
        residence=residence,
        broke=broke,
        exited=exited,
        cross_star=cross,
        positions=positions,
        steps_used=steps_used,
        frames=frames,
        frame_t=frame_t,
    )

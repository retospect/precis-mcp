"""Vectorised Lagrangian particle tracer on a solved :class:`Field`.

Particles are seeded just inside the inlet and advected by the depth-averaged
reduced horizontal velocity ``U*`` plus the continuity-reconstructed reduced
vertical velocity

    w~(x*, y*, z~) = (U* . grad* b~) - div*(U*) (z~ - b~) = (U* . grad* b~) (1 - psi)

with ``b~ = 1 - h*`` the reduced floor height and ``psi = (z~ - b~)/h*`` the
particle's fraction of the local gap. ``w~`` follows the floor into pockets
and vanishes at the flat ceiling (``z~ = 1``); a flat floor gives ``w~ = 0``
exactly.

In this uniform-in-z mean field ``psi`` is conserved along a path, so the
mean field alone transports particles reversibly through pockets with zero
net lift. The missing physics — unresolved 3-D mixing over disturbed floor —
is modelled as stochastic Lagrangian dispersion, active only where the floor
is deepened (``depth* = h* - 1 > 0``):

    D*_z = D_v* . |U*| . depth*          dz~ += sqrt(2 D*_z dt*) . xi

``D_v*`` is the one modelling input with physics in it: transits per vertical
mixing time, ``D_v* = D_v t0 / h0^2``. It is a required argument — the regime
card or the caller states it; the kernel never picks a default.

``z*`` as reported (in ``Paths.positions`` and ``Paths.frames``) is the local
gap fraction ``psi``: 0 = floor, 1 = ceiling. The tracer's vertical coordinate
is the continuity-reconstructed mean-field height above — the Hele-Shaw
parabolic profile is NOT resolved, so a particle's depth-averaged ``psi`` is
what the model physically carries; do not read a no-slip profile into it. That
is ``profile='plug'``, the kernel default. ``profile='parabolic'`` instead
weights the horizontal step by the Hele-Shaw gap profile ``6 psi (1 - psi)``
(1.5x at mid-gap, 0 at floor and lid) so sheared heights spread a tracer pulse
by Taylor dispersion; the vertical model is unchanged (Q4,
docs/backlog/se-flow-kernel.md).

The *first* time ``z~`` crosses the domain's tracking plane is recorded, with
the crossing ``(x*, y*)`` located by sub-step linear interpolation (accuracy
decoupled from the step size). ``t* = t / t0`` counts in transit times.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .domain import Domain
from .flow import Field

# Step-size and termination constants, ported from cfdtest's tracker config.
# ``dt*`` adapts to the field so the absolute velocity scale never enters.
CFL = 0.4  # dt* = CFL * dx* / char_speed
SPEED_PCT = 95.0  # percentile of |U*| used as the characteristic speed
EXIT_FRACTION = 0.95  # stop once this fraction is resolved (broke OR exited)
MAX_STEPS = 4000  # safety cap only; normal stop is EXIT_FRACTION
STUCK_PATIENCE = 50  # consecutive near-stationary steps before "trapped"
STUCK_SPEED_FRAC = 1e-3  # "stationary" = speed below this * char_speed


@dataclass(frozen=True)
class Seed:
    """Where particles are released, in reduced units.

    ``x_star`` is the release ``x*`` just inside the inlet; ``y_lo``/``y_hi``
    bound the release band across the port; ``z_lo``/``z_hi`` bound the seed
    band as a fraction of the *local* gap (0 = floor, 1 = ceiling). Seeding
    the lower half only (``z_hi = 0.5``) makes breakthrough something the
    holes have to earn.
    """

    x_star: float
    y_lo: float
    y_hi: float
    z_lo: float = 0.0
    z_hi: float = 0.5

    @classmethod
    def across_inlet(
        cls,
        domain: Domain,
        x_star: float | None = None,
        z_lo: float = 0.0,
        z_hi: float = 0.5,
    ) -> Seed:
        """Seed across the inlet port's cell-centre ``y*`` extent."""
        rows = domain.ports.inlet.any(axis=1)
        ys = domain.yc_star[rows]
        x = 1.5 * domain.dx_star if x_star is None else x_star
        return cls(
            x_star=x, y_lo=float(ys.min()), y_hi=float(ys.max()), z_lo=z_lo, z_hi=z_hi
        )


@dataclass(frozen=True)
class Paths:
    """Reduced-units trace result.

    ``residence`` is the ``t*`` at which each particle resolved — first plane
    crossing for the broke, domain exit for the rest — NaN for trapped
    particles (neither, typically stuck in a deep low-velocity pocket).
    ``positions`` holds the final ``(x*, y*, psi)`` per particle.
    """

    n_particles: int
    breakthrough: float  # fraction that ever crossed the tracking plane
    residence: np.ndarray  # (N,) t* of resolution; NaN = trapped
    broke: np.ndarray  # (N,) bool
    exited: np.ndarray  # (N,) bool: left the domain / reached the outlet
    cross_star: np.ndarray  # (N, 2) first-crossing (x*, y*); NaN if never
    positions: np.ndarray  # (N, 3) final (x*, y*, psi)
    steps_used: int
    frames: np.ndarray | None  # (record_frames, N, 3) float32 (x*, y*, psi)
    frame_t: np.ndarray | None  # (record_frames,) evenly spaced t*
    absorbed: np.ndarray | None = None  # (N,) wall-reacted (walls= runs)
    absorb_star: np.ndarray | None = None  # (N, 2) absorption (x*, y*)
    capped: bool = False  # stopped on max_steps, not on EXIT_FRACTION

    @property
    def resolved_fraction(self) -> float:
        """Fraction that resolved — broke through, exited, or (``walls=``
        runs) was absorbed — i.e. the statistic that converged."""
        if not self.n_particles:
            return 1.0
        resolved = self.broke | self.exited
        if self.absorbed is not None:
            resolved = resolved | self.absorbed
        return float(resolved.mean())

    @property
    def trapped_fraction(self) -> float:
        """Fraction stuck (neither broke nor exited) — a validity warning flag."""
        return 1.0 - self.resolved_fraction

    def crossing_points(self) -> np.ndarray:
        """(M, 2) array of valid first-crossing ``(x*, y*)`` locations."""
        return self.cross_star[self.broke]


def _bilinear(
    fields: np.ndarray, domain: Domain, x: np.ndarray, y: np.ndarray
) -> np.ndarray:
    """Bilinearly sample stacked ``fields`` (K, ny, nx) at points (x*, y*).

    Returns (K, P). Indices are clamped to the valid range so out-of-domain
    queries return edge values (those particles are retired separately).
    """
    dx = domain.dx_star
    nx, ny = domain.nx, domain.ny
    gx = np.clip(x / dx - 0.5, 0.0, nx - 1.0)
    gy = np.clip(y / dx - 0.5, 0.0, ny - 1.0)
    i0 = np.floor(gx).astype(int)
    j0 = np.floor(gy).astype(int)
    i1 = np.minimum(i0 + 1, nx - 1)
    j1 = np.minimum(j0 + 1, ny - 1)
    tx = gx - i0
    ty = gy - j0
    f00 = fields[:, j0, i0]
    f10 = fields[:, j0, i1]
    f01 = fields[:, j1, i0]
    f11 = fields[:, j1, i1]
    return (
        f00 * (1 - tx) * (1 - ty)
        + f10 * tx * (1 - ty)
        + f01 * (1 - tx) * ty
        + f11 * tx * ty
    )


def _sample(field: Field, x: np.ndarray, y: np.ndarray):
    """Sample flow quantities at (x*, y*). Returns ux*, uy*, h*, b~, bx*, by*."""
    stack = np.stack(
        [
            field.ux_star,
            field.uy_star,
            field.bx_star,
            field.by_star,
            field.domain.h_star,
        ]
    )
    ux, uy, bx, by, h = _bilinear(stack, field.domain, x, y)
    return ux, uy, h, 1.0 - h, bx, by


def _seed_positions(
    field: Field, seed: Seed, n: int, rng: np.random.Generator
) -> np.ndarray:
    """Seed n particles across the seed band. Returns (n, 3) of (x*, y*, z~)."""
    x = np.full(n, seed.x_star)
    y = rng.uniform(seed.y_lo, seed.y_hi, size=n)
    _, _, h, b, _, _ = _sample(field, x, y)
    frac = rng.uniform(seed.z_lo, seed.z_hi, size=n)
    return np.column_stack([x, y, b + frac * h])


def _velocity(field: Field, pos: np.ndarray, profile: str = "plug") -> np.ndarray:
    """3-D reduced mean velocity (n, 3) at particle positions.

    ``profile='plug'`` advects every particle horizontally at the depth-averaged
    reduced speed ``U*`` whatever its height (cfdtest's tracker). ``'parabolic'``
    weights the horizontal step by the Hele-Shaw gap profile ``6 psi (1 - psi)``
    (1.5x at mid-gap, 0 at floor and lid), so particles at different heights
    travel at different speeds — the shear that spreads a tracer pulse (Taylor
    dispersion). The vertical velocity ``w~`` stays the continuity-reconstructed
    *depth-averaged* field either way; the profile touches only the horizontal
    advection (Q4, docs/backlog/se-flow-kernel.md).
    """
    z = pos[:, 2]
    ux, uy, h, b, bx, by = _sample(field, pos[:, 0], pos[:, 1])
    # w~ = (U*.grad* b~)(1 - psi): U*.grad* b~ at the floor, 0 at the ceiling.
    # Reconstructed from the depth-averaged U*, so it is profile-independent.
    u_dot_gradb = ux * bx + uy * by
    psi = np.clip((z - b) / np.where(h > 1e-9, h, 1.0), 0.0, 1.0)
    w = u_dot_gradb * (1.0 - psi)
    if profile == "parabolic":
        shear = 6.0 * psi * (1.0 - psi)
        ux = ux * shear
        uy = uy * shear
    return np.column_stack([ux, uy, w])


def _char_speed(field: Field) -> float:
    """Characteristic speed: the SPEED_PCT percentile of |U*| inside."""
    speed = np.hypot(field.ux_star, field.uy_star)[field.domain.mask]
    return float(np.percentile(speed, SPEED_PCT)) if speed.size else 0.0


def _psi(field: Field, pos: np.ndarray) -> np.ndarray:
    """Local-gap fraction psi in [0, 1] at each (x*, y*, z~)."""
    _, _, h, b, _, _ = _sample(field, pos[:, 0], pos[:, 1])
    return np.clip((pos[:, 2] - b) / np.where(h > 1e-9, h, 1.0), 0.0, 1.0)


def trace(
    field: Field,
    seed: Seed,
    n: int,
    d_v_star: float,
    seed_rng: np.random.Generator | int,
    record_frames: int = 0,
    profile: str = "plug",
    pe: float | None = None,
    epsilon: float | None = None,
    walls: dict[str, float | np.ndarray] | None = None,
    max_steps: int = MAX_STEPS,
) -> Paths:
    """Advect the swarm and record first plane crossings.

    ``d_v_star`` is required: the reduced vertical dispersion over deepened
    floor, ``D_v* = D_v t0 / h0^2`` — transits per vertical mixing time. The
    regime card or the caller states it; there is no hidden default.

    ``profile`` selects the horizontal velocity profile (Q4,
    docs/backlog/se-flow-kernel.md). ``'plug'`` (the kernel default — it keeps
    the cfdtest-calibrated behaviour of the landed slice-1 tracer) moves every
    particle at the depth-averaged speed. ``'parabolic'`` weights the
    horizontal step by the Hele-Shaw gap profile ``6 psi (1 - psi)``, the shear
    that spreads a tracer pulse; the se adapter (slice 2) selects it. Only the
    horizontal advection changes — ``d_v_star`` and the vertical model are
    untouched.

    ``record_frames > 0`` additionally returns ``Paths.frames``, a float32
    ``(record_frames, n, 3)`` array of ``(x*, y*, psi)`` at ``frame_t`` =
    ``linspace(0, t*_end, record_frames)`` (a web player consumes it later;
    resolved particles hold their final position).
    """
    if profile not in ("plug", "parabolic"):
        raise ValueError(f"profile must be 'plug' or 'parabolic', got {profile!r}")
    if pe is not None and profile != "parabolic":
        raise ValueError(
            "trace: pe= needs profile='parabolic' — molecular diffusion on a "
            "plug is not Taylor dispersion (se-multiphysics.md slice 1)"
        )
    if (pe is not None or walls) and epsilon is None:
        raise ValueError("trace: pe=/walls= need epsilon= (h0/L)")
    if epsilon is not None and epsilon <= 0:
        raise ValueError("trace: epsilon must be > 0")
    if pe is not None and pe <= 0:
        raise ValueError("trace: pe must be > 0")
    domain = field.domain
    # wall Damköhler per reactive wall (scalar or (ny,nx) field); inf = a
    # perfectly absorbing wall (P = 1 on every hit).
    wall_da: dict[str, np.ndarray] = {}
    for side, da in (walls or {}).items():
        if side not in ("floor", "lid"):
            raise ValueError(f"trace: walls keys are 'floor'/'lid', got {side!r}")
        wall_da[side] = np.broadcast_to(
            np.asarray(da, dtype=float), domain.shape
        ).astype(float)
    rng = (
        seed_rng
        if isinstance(seed_rng, np.random.Generator)
        else np.random.default_rng(seed_rng)
    )

    pos = _seed_positions(field, seed, n, rng)
    broke = np.zeros(n, dtype=bool)
    exited = np.zeros(n, dtype=bool)
    absorbed = np.zeros(n, dtype=bool) if wall_da else None
    absorb_star = np.full((n, 2), np.nan) if wall_da else None
    cross = np.full((n, 2), np.nan)
    residence = np.full(n, np.nan)
    active = np.ones(n, dtype=bool)

    dx = domain.dx_star
    width = domain.width_star
    z_plane = domain.z_plane_star

    char = _char_speed(field)
    dt = CFL * dx / char if char > 1e-15 else dx
    if pe is not None and epsilon is not None:
        # Vertical-mixing resolution: the spec wants ~100/(Pe·ε) steps per
        # transit so the gap is sampled between velocity changes — a CFL dt
        # decorrelates ψ every step and adds false shear dispersion.
        dt = min(dt, epsilon * pe / 100.0)
    # Absorption cap: keep the per-hit probability ≤ 0.1 (Erban–Chapman
    # fidelity); infinite Da_w needs no cap (P = 1 regardless).
    if wall_da:
        finite = [d for d in wall_da.values() if np.isfinite(d).all()]
        if finite and pe is not None and epsilon is not None:
            da_max = float(max(float(np.max(d)) for d in finite))
            if da_max > 0:
                dt = min(dt, (0.1 / da_max) ** 2 * (epsilon * pe / np.pi))
    stuck_tol = STUCK_SPEED_FRAC * max(char, 1e-15)
    strikes = np.zeros(n, dtype=int)

    record: list[np.ndarray] = []
    if record_frames > 0:
        record.append(
            np.column_stack([pos[:, 0], pos[:, 1], _psi(field, pos)]).astype(np.float32)
        )

    # Species runs need the resolved tail, not a statistic at 95%: a pulse
    # RTD or an absorption fraction truncated at EXIT_FRACTION under-reads
    # the spread by ~20%. They stop only when no particle remains active
    # (or max_steps — reported via Paths.capped).
    species_run = pe is not None or bool(wall_da)
    exit_fraction = 1.0 if species_run else EXIT_FRACTION
    steps_used = 0
    capped = True
    for step in range(max_steps):
        steps_used = step + 1
        # Stop when enough particles are resolved (broke through OR exited).
        resolved = broke | exited
        if absorbed is not None:
            resolved = resolved | absorbed
        if resolved.mean() >= exit_fraction or not active.any():
            capped = False
            break
        idx = np.where(active)[0]
        p = pos[idx]

        # RK2 (midpoint) advection by the mean field.
        v1 = _velocity(field, p, profile)
        pmid = p + 0.5 * dt * v1
        v2 = _velocity(field, pmid, profile)
        pnew = p + dt * v2

        # Stochastic vertical mixing where the floor is deepened:
        # D*_z = D_v* |U*| depth*, dz~ += sqrt(2 D*_z dt*) xi.
        ux2, uy2, h_here, b_here, _, _ = _sample(field, pnew[:, 0], pnew[:, 1])
        depth = np.maximum(h_here - 1.0, 0.0)
        speed = np.hypot(ux2, uy2)
        dz_sigma = np.sqrt(2.0 * d_v_star * speed * depth * dt)
        pnew[:, 2] += dz_sigma * rng.standard_normal(idx.size)

        # Molecular diffusion (pe=): horizontal kick sqrt(2ε/Pe dt*) ξ and
        # vertical in z~ at D_z* = 1/(Pe ε) (the spec's ψ-space
        # D_ψ = 1/(εPe h*²) stretched by the local gap — identical in z~).
        if pe is not None and epsilon is not None:
            sigma_h = np.sqrt(2.0 * epsilon / pe * dt)
            pnew[:, 0] += sigma_h * rng.standard_normal(idx.size)
            pnew[:, 1] += sigma_h * rng.standard_normal(idx.size)
            sigma_z = np.sqrt(2.0 * dt / (pe * epsilon))
            pnew[:, 2] += sigma_z * rng.standard_normal(idx.size)

        z_old = p[:, 2]
        z_new = pnew[:, 2]

        # Detect first upward crossing of the tracking plane (before clamping).
        # Species runs (pe=/walls=) skip it: the plane resolves a particle on
        # its way to the lid, which would truncate the outlet RTD and mask
        # lid absorption — species particles resolve only by exit or reaction.
        if pe is None and not wall_da:
            newly = (~broke[idx]) & (z_old < z_plane) & (z_new >= z_plane)
            if newly.any():
                denom = np.where(z_new - z_old != 0, z_new - z_old, 1.0)
                t = (z_plane - z_old) / denom
                cx = p[:, 0] + t * (pnew[:, 0] - p[:, 0])
                cy = p[:, 1] + t * (pnew[:, 1] - p[:, 1])
                gi = idx[newly]
                broke[gi] = True
                cross[gi, 0] = cx[newly]
                cross[gi, 1] = cy[newly]
                residence[gi] = (step + t[newly]) * dt
                active[gi] = False  # resolved: counted once, no need to track further

        # Wall absorption (walls=): a step ending past a wall is a certain
        # encounter; a step that stays inside can still have touched the
        # wall mid-step — the Brownian-bridge term
        #   P_enc = exp(−d0·d1/(D_tot·dt)),  D_tot = D_tot/D · (ε Pe)⁻¹
        # catches those crossings (endpoint-only detection under-absorbs
        # ~25% in the transport limit — the decay slope comes out 25%
        # shallow). Each encounter absorbs with Erban–Chapman probability
        # P = Da_w sqrt(pi dt* / (ε Pe · D_tot/D)); Da_w = ∞ → P = 1.
        # Survivors reflect back inside below.
        if wall_da:
            if pe is None:
                raise ValueError(
                    "trace: walls= needs pe= — the absorption probability "
                    "is measured in vertical-mixing units"
                )
            assert epsilon is not None
            assert absorbed is not None and absorb_star is not None
            d_tot = 1.0 + epsilon * pe * d_v_star * speed * depth
            roll = rng.random(idx.size)
            for side, d0, d1, crossed in (
                ("floor", z_old - b_here, z_new - b_here, z_new <= b_here),
                ("lid", 1.0 - z_old, 1.0 - z_new, z_new >= 1.0),
            ):
                da_field = wall_da.get(side)
                if da_field is None:
                    continue
                enc = np.exp(
                    -np.clip(d0, 0.0, None)
                    * np.clip(d1, 0.0, None)
                    * epsilon
                    * pe
                    / (d_tot * dt)
                )
                enc = np.where(crossed, 1.0, enc)
                da_here = _bilinear(da_field[None], domain, pnew[:, 0], pnew[:, 1])[0]
                p_abs = da_here * np.sqrt(np.pi * dt / (epsilon * pe * d_tot))
                p_abs = np.where(np.isinf(da_here), 1.0, np.clip(p_abs, 0, 1))
                dying = (roll < enc * p_abs) & active[idx]
                if dying.any():
                    gi = idx[dying]
                    absorbed[gi] = True
                    absorb_star[gi, 0] = pnew[dying, 0]
                    absorb_star[gi, 1] = pnew[dying, 1]
                    residence[gi] = (step + 1) * dt
                    active[gi] = False

        # Reflect z~ into (b~, 1] — Neumann walls for a diffusing particle.
        # Clamping piles walkers onto exactly ψ ∈ {0, 1} where the shear
        # velocity vanishes and inflates every residence/absorption tail.
        span = np.maximum(1.0 - b_here, 1e-12)
        z_rel = pnew[:, 2] - b_here
        z_rel = np.abs(np.mod(z_rel + span, 2.0 * span) - span)
        pnew[:, 2] = b_here + z_rel
        pos[idx] = pnew

        # Retire particles that leave the domain or reach the outlet.
        inside = _bilinear(
            domain.mask.astype(float)[None], domain, pnew[:, 0], pnew[:, 1]
        )[0]
        out_port = (pnew[:, 0] <= 0) | (pnew[:, 0] >= 1.0)
        left = out_port | (pnew[:, 1] <= 0) | (pnew[:, 1] >= width) | (inside < 0.5)
        if species_run:
            # Side walls and mask edges are no-flux for species: reject the
            # step's horizontal move instead of retiring the walker — a
            # species particle lost over a wall corrupts both the outlet
            # RTD and the absorption bookkeeping. Port faces still resolve.
            wall_out = ~out_port & left
            if wall_out.any():
                wr = np.where(wall_out)[0]
                pnew[wr, 0] = p[wr, 0]
                pnew[wr, 1] = p[wr, 1]
                pos[idx] = pnew
                left = out_port
        # Mark as exited only those still active (not just retired by a
        # breakthrough this step).
        gi_left = idx[left]
        still = active[gi_left]
        exited[gi_left[still]] = True
        residence[gi_left[still]] = (step + 1) * dt
        active[gi_left] = False

        # Stuck detector: retire near-stationary particles as trapped.
        slow = speed < stuck_tol
        strikes[idx[slow]] += 1
        strikes[idx[~slow]] = 0
        active[idx[strikes[idx] >= STUCK_PATIENCE]] = False

        if record_frames > 0:
            record.append(
                np.column_stack([pos[:, 0], pos[:, 1], _psi(field, pos)]).astype(
                    np.float32
                )
            )

    positions = np.column_stack([pos[:, 0], pos[:, 1], _psi(field, pos)])
    frames = frame_t = None
    if record_frames > 0:
        rec = np.stack(record)  # (S, n, 3), S = steps_used + 1
        t_axis = np.arange(rec.shape[0]) * dt
        frame_t = np.linspace(0.0, t_axis[-1], record_frames)
        frames = np.empty((record_frames, n, 3), dtype=np.float32)
        for d in range(3):
            for i in range(n):
                frames[:, i, d] = np.interp(frame_t, t_axis, rec[:, i, d])

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
        absorbed=absorbed,
        absorb_star=absorb_star,
        capped=capped,
    )

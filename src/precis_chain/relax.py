"""Mechanical settle of a chain bundle: rigid segment bodies, hinge springs,
loop springs, pins and excluded volume, under FIRE descent.

**What a body is.** One body per *segment*, not per unit — two beads, at the
segment's two ends, held apart by a stiff rigid spring. A 7 kb origami design
is a few hundred segments and therefore a few hundred bodies; the same design
at one bead per base pair would be twelve thousand beads and the settle would
stop being interactive. The consequence is that a segment cannot bend
internally: all bending happens at the hinges between segments, and the
caller chooses the segment length that makes that a fair approximation
(:func:`precis_chain.envelope.capsules_along`'s ``max_turn_rad`` is the same
decision).

**What this is not.** A mechanical settle, not thermodynamics and not
sampling. It has no notion of topology: a loop spring will happily pull a loop
*through* a helix, and nothing here detects that it did. It finds a
low-energy configuration near the one it was handed — a starting geometry that
is already threaded wrongly stays threaded wrongly. Callers must say so
(the ``precis-se-chain-help`` skill states this limit for the nucleic-acid binding).

Energies and the descent, in the numerical idiom of
``precis.structure.georelax.relax_graph`` (spring + repulsion terms summed
into one displacement field) and ``precis.structsolve.formfind`` (arrays in,
arrays out, no store) — read for shape, deliberately not imported; this
package imports nothing from ``precis``. Every spring is ``F = -k (d - rest)``
along its direction, so ``stiffness`` is a spring constant in
force-per-length, and every stiffness is in the caller's own units.

**Ignored: roll and roll torque.** A two-bead body has no roll degree of
freedom. An :class:`Attachment`'s offset (a backbone exit, sitting off the
axis) is carried rigidly by the *axis* rotation, and the torque that an
off-axis force exerts about the axis is dropped. For a helix this is the right
simplification — roll is fixed by the register, not free — but it means a loop
spring cannot rotate a helix to present its exit, only translate and swing it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

import numpy as np

from precis_chain.clash import candidate_pairs, segment_closest_points
from precis_chain.envelope import Capsule
from precis_chain.motif import Motif

_EPS = 1e-12

#: Floor on ``sin(theta)`` in the hinge gradient — same guard, same reason, as
#: ``precis.structure.georelax.angle_theta_gradients``: at a straight or fully
#: folded joint the angle derivative blows up, and the configuration is
#: meaningless there anyway.
_SIN_FLOOR = 1e-3

#: How much stiffer the rigid-body and weld constraints are made than the
#: stiffest soft term, when the caller does not set them. Stiff enough that a
#: body stretches by a few percent under the loads a settle applies, soft
#: enough that FIRE's timestep stays usable.
_CONSTRAINT_FACTOR = 20.0

#: Default excluded-volume spring constant, against 1.0 for a loop spring.
#: Overlap is a hard geometric fact and a loop's slack is not, so EV wins where
#: they disagree — but it is a **penalty, not a projection**: a fully taut
#: unit-stiffness loop pulling two capsules together settles them ~1%
#: interpenetrated (the measured figure at this value), and a stiffer load
#: proportionally more. A caller that needs a hard non-overlap guarantee must
#: check :func:`precis_chain.clash.clashes` on the settled bundle and act on
#: it; the settle will not deliver one on its own.
EV_STIFFNESS = 100.0

#: FIRE parameters, the canonical values from Bitzek et al., PRL 97 (2006).
_FIRE_N_MIN = 5
_FIRE_F_INC = 1.1
_FIRE_F_DEC = 0.5
_FIRE_ALPHA_START = 0.1
_FIRE_F_ALPHA = 0.99


@dataclass(frozen=True)
class Hinge:
    """A joint between two bodies of one chain.

    Welds body ``body_a``'s far end (bead 1) to body ``body_b``'s near end
    (bead 0), and restores the angle between their axes toward ``rest_angle``
    (0 = straight) with spring constant ``stiffness`` (energy per radian
    squared; see :func:`hinge_stiffness`).
    """

    body_a: int
    body_b: int
    stiffness: float
    rest_angle: float = 0.0


@dataclass(frozen=True)
class Attachment:
    """A point rigidly carried by one body.

    ``along`` is the fractional position on the axis (0 = bead 0, 1 = bead 1);
    ``offset`` is a world-frame vector *at the bundle's starting pose*, carried
    by the body's axis rotation thereafter (see the module docstring on roll).
    A zero offset attaches on the axis itself; a backbone exit's offset is
    :func:`precis_chain.fibre.backbone_exit`'s return with no origin.
    """

    body: int
    along: float = 1.0
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class LoopSpring:
    """A flexible loop pulling two attachments together.

    ``rest_length`` is the loop's contour (:func:`precis_chain.loop.contour`),
    and the spring is **one-sided**: it pulls when the attachments are farther
    apart than that and does nothing when they are closer. A chain cannot
    push, so a slack loop exerts no force — which is also why a settle stops
    at the contour rather than collapsing the two exits onto each other.
    """

    a: Attachment
    b: Attachment
    rest_length: float
    stiffness: float = 1.0


@dataclass(frozen=True)
class Pin:
    """A positional constraint on one body end.

    ``stiffness = None`` (the default) is a **hard** pin: the bead is moved to
    ``target`` before the first step and never moves again. A finite stiffness
    is a spring to ``target`` instead, which is what a caller wants when the
    pin is a preference (a lattice site the design would like to sit on)
    rather than a fact.
    """

    body: int
    end: int
    target: tuple[float, float, float]
    stiffness: float | None = None


@dataclass
class BundleResult:
    """The settled bundle and an honest account of how it got there."""

    #: ``(B, 2, 3)`` settled body end positions.
    bodies: np.ndarray
    #: True when the max per-bead force fell below ``tol`` before ``iters``.
    converged: bool
    #: Steps actually taken.
    n_steps: int
    #: Max per-bead force magnitude at the last step.
    max_force: float
    #: Per-step max force, for a caller reporting a convergence envelope.
    curve: list[float] = field(default_factory=list)


def hinge_stiffness(motif: Motif, segment_length: float) -> float:
    """The worm-like-chain bending constant for a hinge between two segments of
    length ``segment_length``: ``persistence_length / (2 * segment_length)``.

    From ``E = (Lp/2) integral kappa^2 ds`` in ``kT``: a kink of ``theta``
    spread over one segment has ``kappa = theta / L``, giving
    ``E = Lp theta^2 / (2 L)``. Units are ``kT`` per radian squared when the
    motif's persistence length and the segment length share a unit — which
    also means a *shorter* segmentation gives a stiffer hinge, correctly: the
    same total bend is being forced through less material.
    """
    if segment_length <= 0.0:
        raise ValueError(f"segment_length must be positive, got {segment_length}")
    return motif.persistence_length / (2.0 * segment_length)


def _skew(w: np.ndarray) -> np.ndarray:
    """``(B, 3, 3)`` skew-symmetric matrices from ``(B, 3)`` vectors."""
    k = np.zeros((w.shape[0], 3, 3))
    k[:, 0, 1] = -w[:, 2]
    k[:, 0, 2] = w[:, 1]
    k[:, 1, 0] = w[:, 2]
    k[:, 1, 2] = -w[:, 0]
    k[:, 2, 0] = -w[:, 1]
    k[:, 2, 1] = w[:, 0]
    return k


def _axis_rotations(u0: np.ndarray, u1: np.ndarray) -> np.ndarray:
    """``(B, 3, 3)`` minimal rotations taking each unit vector ``u0[i]`` to
    ``u1[i]`` (Rodrigues). A body flipped exactly end-for-end has no minimal
    rotation and gets the identity — it cannot arise from a settle that starts
    from a sane pose, and a silent NaN would be worse."""
    w = np.cross(u0, u1)
    cos = np.einsum("ij,ij->i", u0, u1)
    k = _skew(w)
    denom = 1.0 + cos
    safe = denom > 1e-8
    scale = np.where(safe, 1.0 / np.where(safe, denom, 1.0), 0.0)
    return np.eye(3)[None, :, :] + k + scale[:, None, None] * (k @ k)


def carry_rotation(u0: np.ndarray, u1: np.ndarray) -> np.ndarray:
    """``(3, 3)``: the rotation this module carries an :class:`Attachment`'s
    offset by when its body's axis turns from ``u0`` to ``u1``.

    Public because a caller that wants to know where an attachment *ended up*
    has to apply exactly the carry the settle applied internally, and
    reimplementing Rodrigues alongside :func:`_axis_rotations` is a silent
    drift waiting to happen — this delegates to it, so the two cannot
    disagree. Both arguments must be unit vectors.
    """
    return _axis_rotations(
        np.asarray(u0, dtype=float).reshape(1, 3),
        np.asarray(u1, dtype=float).reshape(1, 3),
    )[0]


def _unit_rows(v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(unit rows, norms)`` for ``(N, 3)``; zero rows come back as ``+x``."""
    norms = np.linalg.norm(v, axis=1)
    out = np.empty_like(v)
    bad = norms < _EPS
    out[~bad] = v[~bad] / norms[~bad, None]
    out[bad] = np.array([1.0, 0.0, 0.0])
    return out, norms


def relax_bundle(
    bodies: np.ndarray,
    hinges: Sequence[Hinge],
    loops: Sequence[LoopSpring],
    pins: Sequence[Pin],
    radii: np.ndarray,
    iters: int = 500,
    *,
    rigid_stiffness: float | None = None,
    weld_stiffness: float | None = None,
    ev_stiffness: float = EV_STIFFNESS,
    ev_tol: float = 0.0,
    skip_pairs: Iterable[tuple[int, int]] | None = None,
    tol: float = 1e-6,
    max_step: float | None = None,
) -> BundleResult:
    """Settle a bundle of rigid segment bodies. Pure: nothing is mutated.

    Parameters
    ----------
    bodies:
        ``(B, 2, 3)`` starting end positions, ``bodies[i, 0]`` and
        ``bodies[i, 1]``. Each body's starting length becomes its rigid rest
        length, so the caller's layout defines the segmentation.
    hinges, loops, pins:
        Sequences of :class:`Hinge`, :class:`LoopSpring`, :class:`Pin`.
    radii:
        ``(B,)`` capsule radius per body, for excluded volume.
    iters:
        Maximum FIRE steps.
    rigid_stiffness, weld_stiffness:
        Constraint spring constants. Default: :data:`_CONSTRAINT_FACTOR` times
        the stiffest soft term, so they dominate whatever the caller supplied
        without the caller having to reason about the ratio.
    ev_stiffness, ev_tol:
        Excluded-volume spring constant and the clearance it enforces —
        ``ev_tol`` is the gap two capsules must keep beyond touching (the
        design's ``min_gap``).
    skip_pairs:
        Body pairs exempt from excluded volume, on top of the hinge-welded
        pairs (which are always exempt — they are *supposed* to touch).
    tol:
        Convergence threshold on the maximum per-bead force.
    max_step:
        Per-bead displacement cap per step. Default 5% of the median body
        length — the guard that makes a stiff constraint set safe to integrate
        at all.

    Returns
    -------
    BundleResult
        Settled positions plus the convergence record. ``converged=False`` is
        a real answer, not an error: the caller reports it rather than
        pretending the geometry is settled.

    Notes
    -----
    **Every stiffness is in the caller's units, but ``tol`` and ``max_step``
    are not — pick the length unit to suit them.** The defaults suit
    coordinates of order 1: at metre-scale nucleic-acid coordinates (bodies a
    few e-9 long) the forces are ~1e-9, below ``tol``, so this returns
    ``converged=True`` at step 0 having moved nothing, and ``max_step`` ≈
    3e-10 could not cross a 4 nm gap in 500 steps regardless. That is a
    silent no-op, not an error — the first caller
    (:mod:`precis_se.chain.relax`) builds its bundle in **nanometres** and
    converts back for exactly this reason. A caller working at another scale
    must either do the same or pass ``tol``/``max_step`` scaled to its own
    coordinates.
    """
    pos0 = np.asarray(bodies, dtype=float)
    if pos0.ndim != 3 or pos0.shape[1:] != (2, 3):
        raise ValueError(f"bodies must be (B, 2, 3), got {pos0.shape}")
    n_bodies = pos0.shape[0]
    rad = np.asarray(radii, dtype=float).reshape(-1)
    if rad.shape != (n_bodies,):
        raise ValueError(f"radii must be ({n_bodies},), got {rad.shape}")
    if n_bodies == 0:
        return BundleResult(pos0.copy(), True, 0, 0.0, [])
    if iters < 0:
        raise ValueError(f"iters must be >= 0, got {iters}")
    for h in hinges:
        for idx in (h.body_a, h.body_b):
            if not 0 <= idx < n_bodies:
                raise ValueError(f"hinge body index {idx} outside [0, {n_bodies})")
    for spring in loops:
        for att in (spring.a, spring.b):
            if not 0 <= att.body < n_bodies:
                raise ValueError(
                    f"loop attachment body {att.body} outside [0, {n_bodies})"
                )
    for pin in pins:
        if not 0 <= pin.body < n_bodies:
            raise ValueError(f"pin body {pin.body} outside [0, {n_bodies})")
        if pin.end not in (0, 1):
            raise ValueError(f"pin end must be 0 or 1, got {pin.end}")

    x = pos0.reshape(-1, 3).copy()
    axis0, len0 = _unit_rows(pos0[:, 1] - pos0[:, 0])
    bead_a = 2 * np.arange(n_bodies)
    bead_b = bead_a + 1

    soft_max = max(
        [1.0, ev_stiffness]
        + [abs(h.stiffness) for h in hinges]
        + [abs(s.stiffness) for s in loops]
        + [abs(p.stiffness) for p in pins if p.stiffness is not None]
    )
    k_rigid = (
        _CONSTRAINT_FACTOR * soft_max if rigid_stiffness is None else rigid_stiffness
    )
    k_weld = _CONSTRAINT_FACTOR * soft_max if weld_stiffness is None else weld_stiffness
    scale = float(np.median(len0)) if n_bodies else 1.0
    if scale <= _EPS:
        scale = 1.0
    cap = 0.05 * scale if max_step is None else max_step
    dt_max = 0.5 / np.sqrt(max(k_rigid, k_weld, soft_max))
    dt = 0.1 * dt_max

    frozen = np.zeros(len(x), dtype=bool)
    soft_pin_bead: list[int] = []
    soft_pin_target: list[tuple[float, float, float]] = []
    soft_pin_k: list[float] = []
    for pin in pins:
        bead = 2 * pin.body + pin.end
        if pin.stiffness is None:
            x[bead] = np.asarray(pin.target, dtype=float)
            frozen[bead] = True
        else:
            soft_pin_bead.append(bead)
            soft_pin_target.append(pin.target)
            soft_pin_k.append(pin.stiffness)
    pin_idx = np.array(soft_pin_bead, dtype=int)
    pin_tgt = np.array(soft_pin_target, dtype=float).reshape(-1, 3)
    pin_k = np.array(soft_pin_k, dtype=float)

    hinge_a = np.array([h.body_a for h in hinges], dtype=int)
    hinge_b = np.array([h.body_b for h in hinges], dtype=int)
    hinge_k = np.array([h.stiffness for h in hinges], dtype=float)
    hinge_rest = np.array([h.rest_angle for h in hinges], dtype=float)

    loop_body_a = np.array([s.a.body for s in loops], dtype=int)
    loop_body_b = np.array([s.b.body for s in loops], dtype=int)
    loop_along_a = np.array([s.a.along for s in loops], dtype=float)
    loop_along_b = np.array([s.b.along for s in loops], dtype=float)
    loop_off_a = np.array([s.a.offset for s in loops], dtype=float).reshape(-1, 3)
    loop_off_b = np.array([s.b.offset for s in loops], dtype=float).reshape(-1, 3)
    loop_rest = np.array([s.rest_length for s in loops], dtype=float)
    loop_k = np.array([s.stiffness for s in loops], dtype=float)

    exempt: set[tuple[int, int]] = set()
    for h in hinges:
        lo, hi = sorted((h.body_a, h.body_b))
        exempt.add((lo, hi))
    for i, j in skip_pairs or ():
        lo, hi = sorted((int(i), int(j)))
        exempt.add((lo, hi))

    def attachment_points(
        body: np.ndarray, along: np.ndarray, offset: np.ndarray, rot: np.ndarray
    ) -> np.ndarray:
        base = x[2 * body] + along[:, None] * (x[2 * body + 1] - x[2 * body])
        return base + np.einsum("nij,nj->ni", rot[body], offset)

    def forces() -> np.ndarray:
        f = np.zeros_like(x)
        axis = x[bead_b] - x[bead_a]
        axis_u, axis_len = _unit_rows(axis)

        # Rigid body: the two beads held at the starting separation.
        stretch = k_rigid * (axis_len - len0)
        np.add.at(f, bead_a, stretch[:, None] * axis_u)
        np.add.at(f, bead_b, -stretch[:, None] * axis_u)

        if len(hinges):
            # Weld: body_a's far bead onto body_b's near bead.
            wa = 2 * hinge_a + 1
            wb = 2 * hinge_b
            delta = x[wb] - x[wa]
            dist = np.linalg.norm(delta, axis=1)
            direction, _ = _unit_rows(delta)
            pull = k_weld * dist
            np.add.at(f, wa, pull[:, None] * direction)
            np.add.at(f, wb, -pull[:, None] * direction)

            # Bend: the angle between the two body axes.
            u = axis_u[hinge_a]
            v = axis_u[hinge_b]
            lu = np.maximum(axis_len[hinge_a], _EPS)
            lv = np.maximum(axis_len[hinge_b], _EPS)
            cos = np.clip(np.einsum("ij,ij->i", u, v), -1.0, 1.0)
            theta = np.arccos(cos)
            sin = np.maximum(np.sin(theta), _SIN_FLOOR)
            dtheta_du = -(v - cos[:, None] * u) / (sin * lu)[:, None]
            dtheta_dv = -(u - cos[:, None] * v) / (sin * lv)[:, None]
            de = (hinge_k * (theta - hinge_rest))[:, None]
            np.add.at(f, 2 * hinge_a + 1, -de * dtheta_du)
            np.add.at(f, 2 * hinge_a, de * dtheta_du)
            np.add.at(f, 2 * hinge_b + 1, -de * dtheta_dv)
            np.add.at(f, 2 * hinge_b, de * dtheta_dv)

        if len(loops):
            rot = _axis_rotations(axis0, axis_u)
            pa = attachment_points(loop_body_a, loop_along_a, loop_off_a, rot)
            pb = attachment_points(loop_body_b, loop_along_b, loop_off_b, rot)
            delta = pb - pa
            dist = np.linalg.norm(delta, axis=1)
            direction, _ = _unit_rows(delta)
            # One-sided: a chain pulls, never pushes.
            pull = np.where(dist > loop_rest, loop_k * (dist - loop_rest), 0.0)
            force = pull[:, None] * direction
            np.add.at(f, 2 * loop_body_a, (1.0 - loop_along_a)[:, None] * force)
            np.add.at(f, 2 * loop_body_a + 1, loop_along_a[:, None] * force)
            np.add.at(f, 2 * loop_body_b, -(1.0 - loop_along_b)[:, None] * force)
            np.add.at(f, 2 * loop_body_b + 1, -loop_along_b[:, None] * force)

        if pin_idx.size:
            np.add.at(f, pin_idx, pin_k[:, None] * (pin_tgt - x[pin_idx]))

        if ev_stiffness > 0.0 and n_bodies > 1:
            caps = [
                Capsule(x[2 * i], x[2 * i + 1], float(rad[i])) for i in range(n_bodies)
            ]
            pairs = candidate_pairs(caps, tol=max(ev_tol, 0.0))
            if pairs.size:
                keep = np.array(
                    [(int(i), int(j)) not in exempt for i, j in pairs], dtype=bool
                )
                pairs = pairs[keep]
            if pairs.size:
                bi, bj = pairs[:, 0], pairs[:, 1]
                c1, c2 = segment_closest_points(
                    x[2 * bi], x[2 * bi + 1], x[2 * bj], x[2 * bj + 1]
                )
                sep = c2 - c1
                dist = np.linalg.norm(sep, axis=1)
                want = rad[bi] + rad[bj] + ev_tol
                overlap = want - dist
                active = overlap > 0.0
                if np.any(active):
                    bi, bj = bi[active], bj[active]
                    direction, _ = _unit_rows(sep[active])
                    push = ev_stiffness * overlap[active]
                    push_vec = push[:, None] * direction
                    ti = np.clip(
                        np.einsum(
                            "ij,ij->i",
                            c1[active] - x[2 * bi],
                            axis_u[bi] / np.maximum(axis_len[bi], _EPS)[:, None],
                        ),
                        0.0,
                        1.0,
                    )
                    tj = np.clip(
                        np.einsum(
                            "ij,ij->i",
                            c2[active] - x[2 * bj],
                            axis_u[bj] / np.maximum(axis_len[bj], _EPS)[:, None],
                        ),
                        0.0,
                        1.0,
                    )
                    np.add.at(f, 2 * bi, -(1.0 - ti)[:, None] * push_vec)
                    np.add.at(f, 2 * bi + 1, -ti[:, None] * push_vec)
                    np.add.at(f, 2 * bj, (1.0 - tj)[:, None] * push_vec)
                    np.add.at(f, 2 * bj + 1, tj[:, None] * push_vec)

        f[frozen] = 0.0
        return f

    v = np.zeros_like(x)
    alpha = _FIRE_ALPHA_START
    n_pos = 0
    curve: list[float] = []
    converged = False
    max_force = 0.0
    step_count = 0
    for step_count in range(1, iters + 1):
        f = forces()
        max_force = float(np.max(np.linalg.norm(f, axis=1)))
        curve.append(max_force)
        if max_force < tol:
            converged = True
            break
        power = float(np.sum(f * v))
        if power > 0.0:
            n_pos += 1
            if n_pos > _FIRE_N_MIN:
                dt = min(dt * _FIRE_F_INC, dt_max)
                alpha *= _FIRE_F_ALPHA
        else:
            n_pos = 0
            dt *= _FIRE_F_DEC
            v[:] = 0.0
            alpha = _FIRE_ALPHA_START
        v += dt * f
        f_norm = float(np.linalg.norm(f))
        if f_norm > _EPS:
            v = (1.0 - alpha) * v + alpha * float(np.linalg.norm(v)) * f / f_norm
        dx = dt * v
        step_len = np.linalg.norm(dx, axis=1)
        over = step_len > cap
        if np.any(over):
            dx[over] *= (cap / step_len[over])[:, None]
            v[over] = 0.0
        dx[frozen] = 0.0
        x += dx

    return BundleResult(
        bodies=x.reshape(n_bodies, 2, 3),
        converged=converged,
        n_steps=step_count,
        max_force=max_force,
        curve=curve,
    )

"""Rotation-minimizing frames along a path, and the twist bookkeeping on top.

**Frame convention, used everywhere in this package.** A frame is a ``(3, 3)``
proper rotation matrix whose **columns** are, in order, the unit tangent
``t``, the unit normal ``n`` and the unit binormal ``b = t x n``. A frame
field is ``(N, 3, 3)``, one frame per path sample, so ``frames[i][:, 0]`` is
the tangent at sample ``i`` and ``frames[i] @ local`` maps a local vector
(local ``+x`` = along the chain, local ``+y`` = the normal, local ``+z`` = the
binormal) into world coordinates.

Why a rotation-minimizing frame rather than Frenet: the Frenet normal flips
through an inflection and spins wildly where curvature is small, so a
straight-ish chain segment gets a meaningless roll. The RMF has zero angular
velocity about the tangent by construction — all of a chain's roll is then
*authored* (:func:`apply_twist`), never an artefact of the curve's shape.

:func:`rmf_double_reflection` implements the double-reflection method of
Wang, Juettler, Zheng & Liu, "Computation of rotation minimizing frames",
ACM TOG 27(1), 2008 — second-order accurate, two reflections per step, no
integration and no normalisation drift.
"""

from __future__ import annotations

import numpy as np

_EPS = 1e-12


def _orthonormalise(reference: np.ndarray, tangent: np.ndarray) -> np.ndarray:
    """``reference`` projected perpendicular to ``tangent`` and normalised."""
    t = np.asarray(tangent, dtype=float)
    r = np.asarray(reference, dtype=float)
    perp = r - float(np.dot(r, t)) * t
    norm = float(np.linalg.norm(perp))
    if norm < 1e-9:
        raise ValueError(
            "the frame reference vector r0 is parallel to the first tangent — "
            "there is no normal direction to start from; pass any vector not "
            "along the chain"
        )
    return perp / norm


def rmf_double_reflection(
    points: np.ndarray, tangents: np.ndarray, r0: np.ndarray
) -> np.ndarray:
    """Propagate a rotation-minimizing frame along a sampled curve.

    ``points`` ``(N, 3)``, ``tangents`` ``(N, 3)`` unit (a
    :class:`~precis_chain.path.Path`'s two arrays), ``r0`` ``(3,)`` any vector
    not parallel to ``tangents[0]`` — it is projected perpendicular and
    normalised to become ``frames[0][:, 1]``, so a caller who wants an exact
    starting normal should pass one already perpendicular.

    Returns ``(N, 3, 3)``, columns ``(t, n, b)``.

    Each step reflects the carried normal in the bisecting plane of the chord
    and then in the bisecting plane of the tangents; the composition is the
    (unique) rotation taking ``t[i]`` to ``t[i+1]`` with no extra roll. Two
    consecutive coincident samples, or an exact tangent reversal, degenerate
    the corresponding reflection — those steps carry the frame through
    unchanged rather than producing NaN, which keeps a duplicated waypoint
    from destroying the whole frame field.
    """
    pts = np.asarray(points, dtype=float)
    tan = np.asarray(tangents, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError(f"points must be (N, 3), got {pts.shape}")
    if tan.shape != pts.shape:
        raise ValueError(f"tangents must match points {pts.shape}, got {tan.shape}")
    n = pts.shape[0]
    if n == 0:
        return np.zeros((0, 3, 3))

    frames = np.zeros((n, 3, 3))
    t_i = tan[0] / max(float(np.linalg.norm(tan[0])), _EPS)
    r = _orthonormalise(r0, t_i)
    frames[0, :, 0] = t_i
    frames[0, :, 1] = r
    frames[0, :, 2] = np.cross(t_i, r)

    for i in range(n - 1):
        v1 = pts[i + 1] - pts[i]
        c1 = float(np.dot(v1, v1))
        if c1 > _EPS:
            r_l = r - (2.0 / c1) * float(np.dot(v1, r)) * v1
            t_l = t_i - (2.0 / c1) * float(np.dot(v1, t_i)) * v1
        else:  # coincident samples — nothing to reflect in
            r_l, t_l = r, t_i
        t_next = tan[i + 1] / max(float(np.linalg.norm(tan[i + 1])), _EPS)
        v2 = t_next - t_l
        c2 = float(np.dot(v2, v2))
        if c2 > _EPS:
            r = r_l - (2.0 / c2) * float(np.dot(v2, r_l)) * v2
        else:
            r = r_l
        t_i = t_next
        r = _orthonormalise(r, t_i)
        frames[i + 1, :, 0] = t_i
        frames[i + 1, :, 1] = r
        frames[i + 1, :, 2] = np.cross(t_i, r)
    return frames


def apply_twist(
    frames: np.ndarray, twist_per_length: float, s: np.ndarray
) -> np.ndarray:
    """Roll each frame about its own tangent by ``twist_per_length * s[i]``.

    ``frames`` ``(N, 3, 3)``, ``s`` ``(N,)`` arc length (or any monotone
    parameter in whose unit ``twist_per_length`` is expressed — radians per
    that unit). Positive is the right-hand rule about the tangent: the normal
    rotates toward the binormal.

    Returns a new ``(N, 3, 3)``; the input is not mutated. Tangents are
    untouched, so the result is still a valid frame field for the same path.
    """
    f = np.asarray(frames, dtype=float)
    if f.ndim != 3 or f.shape[1:] != (3, 3):
        raise ValueError(f"frames must be (N, 3, 3), got {f.shape}")
    arc = np.asarray(s, dtype=float)
    if arc.shape != (f.shape[0],):
        raise ValueError(f"s must be ({f.shape[0]},), got {arc.shape}")
    angle = twist_per_length * arc
    cos_a = np.cos(angle)[:, None]
    sin_a = np.sin(angle)[:, None]
    t = f[:, :, 0]
    n = f[:, :, 1]
    b = f[:, :, 2]
    n_new = cos_a * n + sin_a * b
    b_new = -sin_a * n + cos_a * b
    return np.stack([t, n_new, b_new], axis=2)


def twist_between(a: np.ndarray, b: np.ndarray) -> float:
    """The signed roll from frame ``a`` to frame ``b``, radians, wrapped to
    ``[-pi, pi)``.

    Measured about ``a``'s tangent by the right-hand rule: ``b``'s normal is
    expressed in ``a``'s ``(n, b)`` basis and the angle taken with
    :func:`numpy.arctan2`, so ``+pi/2`` means ``b``'s normal points along
    ``a``'s binormal. Meaningful when the two tangents are close (consecutive
    samples, or the two ends of a closed loop); for widely different tangents
    the answer is still well defined but is a projection, not a rotation
    angle.

    The wrap is to the half-open ``[-pi, pi)``: exactly ``pi`` comes back as
    ``-pi``, so the range has no duplicate endpoint and a caller summing
    per-step twists gets a consistent sign.
    """
    fa = np.asarray(a, dtype=float)
    fb = np.asarray(b, dtype=float)
    if fa.shape != (3, 3) or fb.shape != (3, 3):
        raise ValueError(
            f"twist_between takes two (3, 3) frames, got {fa.shape} and {fb.shape}"
        )
    n_b = fb[:, 1]
    angle = float(
        np.arctan2(float(np.dot(n_b, fa[:, 2])), float(np.dot(n_b, fa[:, 1])))
    )
    return float((angle + np.pi) % (2.0 * np.pi) - np.pi)


def accumulated_twist(frames: np.ndarray) -> float:
    """Total unwrapped roll along a frame field — the sum of
    :func:`twist_between` over consecutive frames.

    The per-step sum is what makes a multi-turn total (``21 * 34.286 deg =
    720 deg``) recoverable at all: a single start-to-end
    :func:`twist_between` can only ever report the residue mod ``2 pi``.
    Sampling must be fine enough that no single step turns by more than
    ``pi``, or the sum silently loses turns.
    """
    f = np.asarray(frames, dtype=float)
    if f.ndim != 3 or f.shape[1:] != (3, 3):
        raise ValueError(f"frames must be (N, 3, 3), got {f.shape}")
    return float(sum(twist_between(f[i], f[i + 1]) for i in range(f.shape[0] - 1)))

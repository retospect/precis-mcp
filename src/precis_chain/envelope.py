"""The swept tube around a chain path, as capsules — and the rigid pose each
capsule hands to a CAD envelope.

A chain segment's excluded volume is a capsule (a cylinder with hemispherical
caps): the set of points within ``r`` of the segment ``a -> b``. Capsules are
the kernel's one collision primitive because segment-segment distance is
closed-form and cheap (:mod:`precis_chain.clash`), which is what lets a
thousand-segment design be clash-checked at all.

:func:`capsules_along` converts a path into a capsule chain. The **split
rule** is the documented choice for a numeric the spec left open: the path is
divided into ``n`` pieces of *equal arc length*, with

``n = max(1, ceil(length / max_seg_len), ceil(total_turning / max_turn_rad))``

rather than by a locally greedy walk. Uniform splitting is what makes
``max_seg_len = L/4`` on a straight path give exactly 4 capsules regardless of
how the path happened to be sampled; a greedy walk gives 4 or 5 depending on
whether the samples line up with the cut. The cost is a few extra capsules on
a path that is mostly straight with one tight bend — cheap next to the
confusion of a sample-dependent capsule count.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from precis_chain.path import Path, sample_at

_EPS = 1e-12


@dataclass(frozen=True)
class Capsule:
    """A segment ``a -> b`` thickened by radius ``r``.

    ``a``/``b`` are ``(3,)`` arrays in the caller's length unit; ``r`` the
    same. A zero-length capsule is a sphere and is allowed — it is what a
    single-unit segment degenerates to.
    """

    a: np.ndarray
    b: np.ndarray
    r: float

    def __post_init__(self) -> None:
        a = np.asarray(self.a, dtype=float).reshape(3)
        b = np.asarray(self.b, dtype=float).reshape(3)
        if not math.isfinite(float(self.r)) or float(self.r) < 0.0:
            raise ValueError(f"Capsule.r must be finite and >= 0, got {self.r!r}")
        object.__setattr__(self, "a", a)
        object.__setattr__(self, "b", b)
        object.__setattr__(self, "r", float(self.r))

    @property
    def axis(self) -> np.ndarray:
        """``b - a`` — not normalised; zero for a spherical capsule."""
        return self.b - self.a

    @property
    def length(self) -> float:
        """``|b - a|`` — the cylindrical part's length, caps excluded."""
        return float(np.linalg.norm(self.axis))


def total_turning(path: Path) -> float:
    """Total unsigned turning of ``path``'s tangents, radians — the sum of the
    angle between consecutive unit tangents. Zero for a straight path; ``2
    pi`` for one full planar loop."""
    tan = path.tangents
    if tan.shape[0] < 2:
        return 0.0
    dots = np.clip(np.einsum("ij,ij->i", tan[:-1], tan[1:]), -1.0, 1.0)
    return float(np.sum(np.arccos(dots)))


def _ceil_eps(value: float) -> int:
    """``ceil`` with a relative floating-point guard, so ``L / (L/4)`` is 4 and
    not 5."""
    return math.ceil(value - 1e-9)


def capsules_along(
    path: Path,
    radius: float,
    max_seg_len: float,
    max_turn_rad: float | None = None,
) -> list[Capsule]:
    """Split ``path`` into a chain of capsules of radius ``radius``.

    ``max_seg_len`` bounds each capsule's chord-to-chord arc length;
    ``max_turn_rad`` (``None`` = unbounded) bounds how far the tangent may
    turn within one capsule, which is what keeps the chord from cutting the
    corner: a capsule spanning a turn of ``theta`` on an arc of bend radius
    ``R`` has sagitta ``R (1 - cos(theta/2))``, so ``max_turn_rad = 5 deg``
    holds every sample within ``R (1 - cos 2.5 deg)`` of its chord.

    Returns capsules in path order; consecutive capsules share an endpoint
    exactly, so the union covers the whole sampled path (up to that sagitta).
    """
    if radius < 0.0:
        raise ValueError(f"radius must be >= 0, got {radius}")
    if max_seg_len <= 0.0:
        raise ValueError(f"max_seg_len must be > 0, got {max_seg_len}")
    if max_turn_rad is not None and max_turn_rad <= 0.0:
        raise ValueError(f"max_turn_rad must be > 0 radians, got {max_turn_rad}")
    if len(path) < 2:
        raise ValueError("capsules_along needs a path with >= 2 samples")
    total = path.length
    if total <= _EPS:
        raise ValueError("capsules_along needs a path of non-zero arc length")

    n = max(1, _ceil_eps(total / max_seg_len))
    if max_turn_rad is not None:
        n = max(n, _ceil_eps(total_turning(path) / max_turn_rad))
    cuts, _tangents = sample_at(path, np.linspace(0.0, total, n + 1))
    return [Capsule(cuts[i], cuts[i + 1], radius) for i in range(n)]


def _frame_from_axis(axis: np.ndarray) -> np.ndarray:
    """A deterministic ``(3, 3)`` proper rotation whose third column is
    ``axis`` normalised. The other two columns come from the world axis least
    aligned with ``axis``, so the choice is stable and reproducible but
    otherwise arbitrary — a capsule has no preferred roll."""
    norm = float(np.linalg.norm(axis))
    if norm < 1e-12:
        return np.eye(3)
    z = axis / norm
    helper = np.array([1.0, 0.0, 0.0]) if abs(z[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    x = helper - float(np.dot(helper, z)) * z
    x /= float(np.linalg.norm(x))
    y = np.cross(z, x)
    return np.stack([x, y, z], axis=1)


def _euler_xyz_from_matrix(rot: np.ndarray) -> tuple[float, float, float]:
    """``(rx, ry, rz)`` radians such that ``Rz(rz) @ Ry(ry) @ Rx(rx) == rot``.

    Same convention (and the same gimbal-lock branch) as the CAD DSL's
    ``rot:rx,ry,rz`` pose field, so a caller can hand these straight to an
    envelope without re-deriving anything. Reimplemented rather than imported:
    this package imports nothing from ``precis`` (see the package docstring).
    """
    cos_pitch = float(np.hypot(float(rot[2, 1]), float(rot[2, 2])))
    if cos_pitch > 1e-9:
        rx = float(np.arctan2(rot[2, 1], rot[2, 2]))
        rz = float(np.arctan2(rot[1, 0], rot[0, 0]))
    else:  # pragma: no cover - gimbal lock: axis along +-x, unreachable here
        rz = 0.0
        rx = float(np.arctan2(-rot[1, 2], rot[1, 1]))
    ry = float(np.arcsin(-float(np.clip(rot[2, 0], -1.0, 1.0))))
    return (rx, ry, rz)


def capsule_pose(c: Capsule) -> tuple[np.ndarray, tuple[float, float, float], float]:
    """``(origin, euler_xyz, length)`` placing a ``+z``-aligned cylinder onto
    the capsule.

    ``origin`` is the capsule's ``a`` end, **not** its midpoint: the CAD
    ``cyl`` primitive runs from ``z = 0`` to ``z = h`` in its own local frame
    (``precis.cad.primitives.CircularFrustum``), so ``a`` is where its base
    belongs. ``euler_xyz`` is ``(rx, ry, rz)`` radians in the ``Rz @ Ry @ Rx``
    convention, rotating local ``+z`` onto ``b - a``. ``length`` is
    ``|b - a|`` — the cylinder's ``h``; the capsule's hemispherical caps are
    *not* included, so an envelope built from this pose is the inscribed
    cylinder, and a caller wanting the full swept volume adds ``r`` at each
    end itself.

    A zero-length capsule yields the identity rotation and length 0; the
    caller decides whether that becomes a sphere or an error.
    """
    rot = _frame_from_axis(c.axis)
    return (c.a.copy(), _euler_xyz_from_matrix(rot), c.length)

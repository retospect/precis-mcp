"""Float64 vec3 + rigid transform (rotate + translate, no scale/shear).

The CAD analytic IR: rigid transforms only. This makes membership *and* distance
exact everywhere under transform — a probe inverse-transforms into a
primitive's local frame and every test costs the same as axis-aligned.

Vectors are ``numpy`` arrays of shape ``(3,)``, dtype ``float64``.
A :class:`Transform` is a rotation matrix ``R`` (3×3) plus a translation
``t`` (3,), mapping ``world = R @ local + t``.

Euler convention: ``rot=(rx, ry, rz)`` in **degrees**, applied as
``R = Rz @ Ry @ Rx`` (rotate about local x, then y, then z). Documented
here because it is the one place the convention is fixed; the DSL and the
handler both lower poses through :func:`rotation`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

Vec3 = NDArray[np.float64]

#: Global linear epsilon (mm). Governs touch / coincidence / zero-clearance
#: tests. The load-bearing tunable — *not* the unit.
LINEAR_EPS: float = 1e-6

#: Global angular epsilon (radians) for parallel / coincident-plane tests.
ANGULAR_EPS: float = 1e-9


def vec3(x: float, y: float, z: float) -> Vec3:
    """Build a float64 vec3."""
    return np.array([x, y, z], dtype=np.float64)


def as_vec3(v: object) -> Vec3:
    """Coerce a 3-sequence to a float64 vec3 of shape (3,)."""
    arr = np.asarray(v, dtype=np.float64).reshape(3)
    return arr


def as_float3(
    value: Iterable[Any] | None, default: tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> tuple[float, float, float]:
    """Coerce a dynamic (DB-row) value to a checked ``(x, y, z)`` float triple.

    Used for :class:`~precis.cad.scene.NodeSpec`'s ``loc``/``rot`` fields,
    which — unlike :data:`Vec3` — are plain fixed-length tuples. ``tuple(x
    for ...)`` would give mypy a ``tuple[float, ...]`` that never satisfies
    the fixed-length field type; explicit unpacking gives a real
    ``tuple[float, float, float]`` and raises if the row has the wrong
    number of axes instead of silently truncating/padding.
    """
    if not value:
        return default
    x, y, z = (float(v) for v in value)
    return (x, y, z)


def deg2rad(deg: float) -> float:
    """Degrees → radians."""
    return float(deg) * np.pi / 180.0


def _rot_x(rad: float) -> NDArray[np.float64]:
    c, s = np.cos(rad), np.sin(rad)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)


def _rot_y(rad: float) -> NDArray[np.float64]:
    c, s = np.cos(rad), np.sin(rad)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)


def _rot_z(rad: float) -> NDArray[np.float64]:
    c, s = np.cos(rad), np.sin(rad)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


@dataclass(frozen=True)
class Transform:
    """A rigid transform: rotation ``R`` then translation ``t``.

    ``world = R @ local + t``. Orthonormal ``R`` (``det = +1``) by
    construction; never carries scale or shear, so the inverse is the
    cheap ``R.T``-based map and distances are preserved exactly.
    """

    R: NDArray[np.float64]
    t: Vec3

    def apply(self, p: Vec3) -> Vec3:
        """Map a point local → world."""
        return self.R @ as_vec3(p) + self.t

    def apply_dir(self, d: Vec3) -> Vec3:
        """Map a direction local → world (rotation only, no translation)."""
        return self.R @ as_vec3(d)

    def inverse(self) -> Transform:
        """The inverse rigid transform (world → local)."""
        Rt = self.R.T
        return Transform(R=Rt, t=-(Rt @ self.t))

    def compose(self, other: Transform) -> Transform:
        """``self ∘ other`` — apply ``other`` first, then ``self``."""
        return Transform(R=self.R @ other.R, t=self.R @ other.t + self.t)

    def to_world_point(self, p_local: Vec3) -> Vec3:
        return self.apply(p_local)

    def to_local_point(self, p_world: Vec3) -> Vec3:
        return self.R.T @ (as_vec3(p_world) - self.t)

    def to_local_dir(self, d_world: Vec3) -> Vec3:
        return self.R.T @ as_vec3(d_world)


def identity() -> Transform:
    """The identity transform."""
    return Transform(R=np.eye(3, dtype=np.float64), t=vec3(0.0, 0.0, 0.0))


def translation(x: float, y: float, z: float) -> Transform:
    """A pure translation."""
    return Transform(R=np.eye(3, dtype=np.float64), t=vec3(x, y, z))


def rotation(rx_deg: float, ry_deg: float, rz_deg: float) -> Transform:
    """A pure rotation from Euler angles in degrees (``Rz @ Ry @ Rx``)."""
    R = _rot_z(deg2rad(rz_deg)) @ _rot_y(deg2rad(ry_deg)) @ _rot_x(deg2rad(rx_deg))
    return Transform(R=R, t=vec3(0.0, 0.0, 0.0))


def pose(location: Vec3, rot_deg: Vec3) -> Transform:
    """A placement: rotate (Euler deg) then translate to ``location``."""
    loc = as_vec3(location)
    r = as_vec3(rot_deg)
    rot = rotation(float(r[0]), float(r[1]), float(r[2]))
    return Transform(R=rot.R, t=loc)


def euler_deg_from_matrix(R: NDArray[np.float64]) -> tuple[float, float, float]:
    """Inverse of :func:`rotation`: recover ``(rx, ry, rz)`` degrees from a
    proper rotation matrix (``R = Rz @ Ry @ Rx``).

    Needed wherever a rigid orientation is *built* directly from a
    world-space basis (e.g. a CAD export substitution constructed from an
    arbitrary plane normal) and must be handed back through the DSL's
    ``rot:rx,ry,rz`` pose fields rather than a raw matrix. Degenerate at
    ``|R[2,0]| ≈ 1`` (gimbal lock, ``ry = ±90°``, where ``rx``/``rz`` are
    not independently observable) — that branch fixes ``rz = 0`` and folds
    the coupling into ``rx``, which still reproduces ``R`` exactly, just
    not uniquely.
    """
    r20 = float(np.clip(R[2, 0], -1.0, 1.0))
    cp = float(np.hypot(float(R[2, 1]), float(R[2, 2])))
    if cp > 1e-9:
        rx = float(np.arctan2(R[2, 1], R[2, 2]))
        rz = float(np.arctan2(R[1, 0], R[0, 0]))
    else:
        rz = 0.0
        rx = float(np.arctan2(-R[1, 2], R[1, 1]))
    ry = float(np.arcsin(-r20))
    return (
        float(np.degrees(rx)),
        float(np.degrees(ry)),
        float(np.degrees(rz)),
    )


def normalize(v: Vec3) -> Vec3:
    """Unit vector; raises on a zero-length input."""
    arr = as_vec3(v)
    n = float(np.linalg.norm(arr))
    if n <= LINEAR_EPS:
        raise ValueError("cannot normalize a zero-length vector")
    return arr / n

"""precis_surface.level_set — closed-form TPMS nodal approximations
(docs/backlog/precis-surface-kernel.md "Slice 1 -- the dual route").

Schwarz P, Schwarz D and the gyroid, each as the classic *nodal
approximation*: a low-order trigonometric polynomial whose zero set sits
close to, but is not equal to, the true zero-mean-curvature (minimal)
surface of the same name and symmetry. The true minimal surfaces have no
elementary closed form (Schwarz's and Schoen's constructions are
elliptic-integral parametrisations, not the level set of a simple
function); the nodal forms below are the standard proxy used from
Schoen's 1970 memo through every TPMS-lattice CAD tool, and are what
:mod:`precis_surface.periodic_mesh` meshes.

**Why this distinction is load-bearing.** Only the true minimal surface
has mean curvature ``H = 0`` everywhere, which forces ``K <= 0`` via
``k1 = -k2``. A nodal surface's Gaussian curvature is *not* guaranteed
non-positive everywhere -- it merely tracks the true minimal surface
closely near the zero set of the exact solution. So any ``K <= 0`` check
run against a nodal mesh (docs/backlog/precis-surface-kernel.md's
acceptance section, via :mod:`precis_surface.curvature`) is an INFO-level
report, never an assertion, here or in the integration test that
exercises curvature. This module's own combinatorial guarantees
(:mod:`precis_surface.periodic_mesh`'s welded-quotient Euler
characteristic and edge-manifold closure) are topological and hold
exactly regardless of how close the nodal form sits to the true minimal
surface.

Every function takes an ``(N, 3)`` float64 point array and the cell edge
``a`` (the physical length of one periodic repeat) and returns an
``(N,)`` float64 array of the implicit function's value -- zero on the
surface, periodic with period ``a`` along each axis by construction (the
argument scaling is ``2*pi/a``, i.e. ``X = 2*pi*x/a`` and so on for
``Y``/``Z``). Pure functions, no store access, unit-agnostic
(:mod:`precis.structsolve` house rules): ``a`` and ``pts`` must already
share one length unit; the functions never know which one it is.

:mod:`precis.structsolve.simp` (around its ``lattice_fill``, module-scale
~line 1015) also samples a gyroid level set, for a different purpose:
macro-scale voxel infill of an arbitrary domain, tied to that module's
own wall-thickness linearisation. Its sign/scaling convention
(``g = sin X cos Y + sin Y cos Z + sin Z cos X``, ``X = 2*pi*x/cell``)
matches :func:`gyroid` here -- read it for confirmation, never import it:
that module is voxel-infill machinery with its own contract, this one is
the mesh-realisation kernel's periodic unit cell.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def _scaled(
    pts: NDArray[np.float64], a: float
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    pts = np.asarray(pts, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError(f"pts must be (N, 3), got {pts.shape}")
    if not (a > 0.0 and np.isfinite(a)):
        raise ValueError(f"cell edge a must be a positive length, got {a}")
    k = 2.0 * np.pi / a
    return pts[:, 0] * k, pts[:, 1] * k, pts[:, 2] * k


def schwarz_p(pts: NDArray[np.float64], a: float) -> NDArray[np.float64]:
    """Schwarz P nodal approximation: ``cos X + cos Y + cos Z``, ``X =
    2*pi*x/a`` etc. Zero set is the simple-cubic P surface's proxy --
    the two labyrinths are ``< 0`` and ``> 0``."""
    x, y, z = _scaled(pts, a)
    return np.cos(x) + np.cos(y) + np.cos(z)


def schwarz_d(pts: NDArray[np.float64], a: float) -> NDArray[np.float64]:
    """Schwarz D (Diamond) nodal approximation:
    ``sin X sin Y sin Z + sin X cos Y cos Z + cos X sin Y cos Z + cos X
    cos Y sin Z``, ``X = 2*pi*x/a`` etc."""
    x, y, z = _scaled(pts, a)
    sx, sy, sz = np.sin(x), np.sin(y), np.sin(z)
    cx, cy, cz = np.cos(x), np.cos(y), np.cos(z)
    return sx * sy * sz + sx * cy * cz + cx * sy * cz + cx * cy * sz


def gyroid(pts: NDArray[np.float64], a: float) -> NDArray[np.float64]:
    """Gyroid nodal approximation: ``sin X cos Y + sin Y cos Z + sin Z
    cos X``, ``X = 2*pi*x/a`` etc. -- same sign/scaling convention as
    :func:`precis.structsolve.simp.lattice_fill`'s gyroid sample."""
    x, y, z = _scaled(pts, a)
    return np.sin(x) * np.cos(y) + np.sin(y) * np.cos(z) + np.sin(z) * np.cos(x)


def schwarz_p_grad(pts: NDArray[np.float64], a: float) -> NDArray[np.float64]:
    """Analytic gradient of :func:`schwarz_p` w.r.t. world ``(x, y, z)``:
    ``(-k*sin X, -k*sin Y, -k*sin Z)``, ``k = 2*pi/a`` (chain rule on the
    ``X = k*x`` scaling)."""
    x, y, z = _scaled(pts, a)
    k = 2.0 * np.pi / a
    return -k * np.stack([np.sin(x), np.sin(y), np.sin(z)], axis=1)


def schwarz_d_grad(pts: NDArray[np.float64], a: float) -> NDArray[np.float64]:
    """Analytic gradient of :func:`schwarz_d` w.r.t. world ``(x, y, z)``,
    each component the by-hand partial derivative of the four-term sum
    w.r.t. ``X``/``Y``/``Z`` respectively, scaled by ``k = 2*pi/a``."""
    x, y, z = _scaled(pts, a)
    k = 2.0 * np.pi / a
    sx, sy, sz = np.sin(x), np.sin(y), np.sin(z)
    cx, cy, cz = np.cos(x), np.cos(y), np.cos(z)
    dfdX = cx * sy * sz + cx * cy * cz - sx * sy * cz - sx * cy * sz
    dfdY = sx * cy * sz - sx * sy * cz + cx * cy * cz - cx * sy * sz
    dfdZ = sx * sy * cz - sx * cy * sz - cx * sy * sz + cx * cy * cz
    return k * np.stack([dfdX, dfdY, dfdZ], axis=1)


def gyroid_grad(pts: NDArray[np.float64], a: float) -> NDArray[np.float64]:
    """Analytic gradient of :func:`gyroid` w.r.t. world ``(x, y, z)``,
    each component the by-hand partial derivative of the three-term sum
    w.r.t. ``X``/``Y``/``Z`` respectively, scaled by ``k = 2*pi/a``."""
    x, y, z = _scaled(pts, a)
    k = 2.0 * np.pi / a
    sx, sy, sz = np.sin(x), np.sin(y), np.sin(z)
    cx, cy, cz = np.cos(x), np.cos(y), np.cos(z)
    dfdX = cx * cy - sx * sz
    dfdY = -sx * sy + cy * cz
    dfdZ = -sy * sz + cz * cx
    return k * np.stack([dfdX, dfdY, dfdZ], axis=1)

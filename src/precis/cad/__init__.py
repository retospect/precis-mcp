"""Analytic-IR CAD kernel.

A small, self-contained analytic geometry kernel: rigid-transform-only
primitives (frustum / sphere / torus / half-space-chamfer) that answer
membership, ray-intersection, distance, and face queries in closed form,
plus a boolean DAG fold that keeps subtraction *visible* without ever
computing the merged solid. Edge rounding is a leaf wrapper
(:class:`~precis.cad.primitives.Rounded`: the shape built shrunk, its
exact signed distance offset back out — never a mesh operation) and a
union may ``blend`` with a smooth-min (:mod:`precis.cad.fold`). A
**sampled field** (:class:`~precis.cad.primitives.Field`: a float32 SDF
grid + pitch + origin, trilinear inside its box, the DSL's
``field:<sha256>``) is one more leaf that answers ``distance(p)`` — how
an optimiser's voxel result enters a design — with its grid-side
operations in :mod:`precis.cad.fieldops` (exact Euclidean ``redistance``,
``open``/``close``/``offset``, the SIMP ``from_density`` bridge, and the
payload codec the store keeps a grid in). A design carrying any of the
three exports through the sampled-field backend
(:mod:`precis.cad.fieldmesh`, narrow-band marching cubes over the folded
SDF) while every sharp design still takes the analytic tessellate +
manifold3d route unchanged.

This package deliberately imports **nothing** from the rest of precis
(no DB, no handler, no store) so it stays unit-testable in isolation and
swappable behind the same node-list. The kernel is unit-agnostic
``float64`` throughout — callers pick the length unit; every internal
tolerance is scale-relative (:data:`~precis.cad.vec.LINEAR_REL_EPS`), not
tuned for one magnitude.

That boundary is why sub-assembly instancing (``use <slug> as <name>``)
takes an *injected* ``resolve`` callable rather than reaching for the
store: :func:`~precis.cad.scene.expand_instances` inlines the referenced
design into a flat spec, and the one production resolver lives outside
this package in :mod:`precis.cad_resolve`. Everything downstream — probe,
relate, export, tessellate — therefore still sees a plain flat spec and
never learns that instancing exists. A ``field:`` leaf is resolved the
same way — an injected :data:`~precis.cad.dsl.FieldLoader` riding on
:attr:`~precis.cad.scene.SceneSpec.field_loader` (``Store.cad_load``
binds the store's; the grid is never inlined in the DSL).
"""

from __future__ import annotations

from precis.cad.vec import (
    ANGULAR_EPS,
    LINEAR_REL_EPS,
    Transform,
    deg2rad,
    identity,
    rotation,
    translation,
)

__all__ = [
    "ANGULAR_EPS",
    "LINEAR_REL_EPS",
    "Transform",
    "deg2rad",
    "identity",
    "rotation",
    "translation",
]

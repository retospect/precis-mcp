"""Analytic-IR CAD kernel.

A small, self-contained analytic geometry kernel: rigid-transform-only
primitives (frustum / sphere / torus / half-space-chamfer) that answer
membership, ray-intersection, distance, and face queries in closed form,
plus a boolean DAG fold that keeps subtraction *visible* without ever
computing the merged solid. Edge rounding is a leaf wrapper
(:class:`~precis.cad.primitives.Rounded`: the shape built shrunk, its
exact signed distance offset back out — never a mesh operation) and a
union may ``blend`` with a smooth-min (:mod:`precis.cad.fold`); a design
carrying either exports through the sampled-field backend
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
never learns that instancing exists.
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

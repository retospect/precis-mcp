"""Analytic-IR CAD kernel.

A small, self-contained analytic geometry kernel: rigid-transform-only
primitives (frustum / sphere / torus / half-space-chamfer) that answer
membership, ray-intersection, distance, and face queries in closed form,
plus a boolean DAG fold that keeps subtraction *visible* without ever
computing the merged solid.

This package deliberately imports **nothing** from the rest of precis
(no DB, no handler, no store) so it stays unit-testable in isolation and
swappable behind the same node-list. Units are
millimetres, ``float64`` throughout.

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
    LINEAR_EPS,
    Transform,
    deg2rad,
    identity,
    rotation,
    translation,
)

__all__ = [
    "ANGULAR_EPS",
    "LINEAR_EPS",
    "Transform",
    "deg2rad",
    "identity",
    "rotation",
    "translation",
]

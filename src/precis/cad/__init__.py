"""Analytic-IR CAD kernel (ADR 0041).

A small, self-contained analytic geometry kernel: rigid-transform-only
primitives (frustum / sphere / torus / half-space-chamfer) that answer
membership, ray-intersection, distance, and face queries in closed form,
plus a boolean DAG fold that keeps subtraction *visible* without ever
computing the merged solid.

This package deliberately imports **nothing** from the rest of precis
(no DB, no handler, no store) so it stays unit-testable in isolation and
swappable behind the same node-list (ADR 0041 §9). Units are
millimetres, ``float64`` throughout.
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

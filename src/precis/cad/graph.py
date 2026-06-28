"""The design DAG — a flat instance set + a CSG expression (ADR 0041 §3).

A :class:`Design` is the in-memory evaluation layer: it registers placed
primitive *instances* and composes them with the three boolean ops into a
named-component expression set. Patterns expand to a labelled union of
instances; the ``instance`` helper re-places a sub-expression under an
extra transform (the shared-sub-DAG mechanism).

The kernel ``Design`` is pure (no DB). The handler builds one of these
from a ref's chunk set, then reuses the probe / relate layers on top.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable

from precis.cad.fold import (
    Class,
    Diff,
    Expr,
    Instance,
    Inter,
    Leaf,
    Span,
    Union,
    classify,
    material_intervals,
    ray_spans,
)
from precis.cad.interval import Intervals
from precis.cad.primitives import Placed, Primitive
from precis.cad.vec import Transform, Vec3, identity


class Design:
    """A flat instance set + named-component CSG expressions."""

    def __init__(self) -> None:
        self.instances: dict[str, Instance] = {}
        self.components: dict[str, Expr] = {}
        self._ids = itertools.count(1)

    # -- construction ----------------------------------------------------
    def _new_iid(self) -> str:
        return f"i{next(self._ids)}"

    def prim(
        self,
        label: str,
        primitive: Primitive,
        xform: Transform | None = None,
    ) -> Leaf:
        """Register a placed primitive and return its leaf expression."""
        iid = self._new_iid()
        placed = Placed(prim=primitive, xform=xform or identity())
        self.instances[iid] = Instance(iid=iid, placed=placed, label=label)
        return Leaf(iid)

    @staticmethod
    def merge(*parts: Expr) -> Union:
        return Union(parts=tuple(parts))

    @staticmethod
    def subtract(base: Expr, *cutters: Expr) -> Diff:
        return Diff(base=base, cutters=tuple(cutters))

    @staticmethod
    def intersect(*parts: Expr) -> Inter:
        return Inter(parts=tuple(parts))

    def pattern(
        self,
        label: str,
        primitive: Primitive,
        transforms: Iterable[Transform],
    ) -> Union:
        """Expand a pattern to a union of labelled instances (``label#i``)."""
        leaves: list[Expr] = []
        for i, xf in enumerate(transforms, start=1):
            iid = self._new_iid()
            self.instances[iid] = Instance(
                iid=iid,
                placed=Placed(prim=primitive, xform=xf),
                label=f"{label}#{i}",
            )
            leaves.append(Leaf(iid))
        return Union(parts=tuple(leaves))

    def instance(self, template: Expr, xform: Transform, *, suffix: str = "") -> Expr:
        """Re-place a sub-expression under an extra world transform.

        Realises the shared sub-DAG / instance mechanism: every leaf in
        ``template`` is re-registered with its placement composed under
        ``xform`` (``xform ∘ placed.xform``), keeping the boolean shape.
        """

        def rebuild(e: Expr) -> Expr:
            if isinstance(e, Leaf):
                src = self.instances[e.iid]
                iid = self._new_iid()
                self.instances[iid] = Instance(
                    iid=iid,
                    placed=Placed(
                        prim=src.placed.prim,
                        xform=xform.compose(src.placed.xform),
                    ),
                    label=src.label + suffix,
                )
                return Leaf(iid)
            if isinstance(e, Union):
                return Union(parts=tuple(rebuild(p) for p in e.parts))
            if isinstance(e, Inter):
                return Inter(parts=tuple(rebuild(p) for p in e.parts))
            if isinstance(e, Diff):
                return Diff(
                    base=rebuild(e.base),
                    cutters=tuple(rebuild(c) for c in e.cutters),
                )
            raise TypeError(f"unknown expr node: {e!r}")

        return rebuild(template)

    def add_component(self, name: str, expr: Expr) -> None:
        """Register a top-level component (one physical part)."""
        self.components[name] = expr

    # -- evaluation ------------------------------------------------------
    def whole(self) -> Expr:
        """The union of every component — for whole-part probes."""
        parts = tuple(self.components.values())
        if not parts:
            raise ValueError("design has no components")
        if len(parts) == 1:
            return parts[0]
        return Union(parts=parts)

    def classify_point(self, p: Vec3, *, component: str | None = None) -> Class:
        expr = self.components[component] if component else self.whole()
        return classify(expr, p, self.instances)

    def ray(self, o: Vec3, d: Vec3, *, component: str | None = None) -> list[Span]:
        expr = self.components[component] if component else self.whole()
        return ray_spans(expr, o, d, self.instances)

    def ray_material(
        self, o: Vec3, d: Vec3, *, component: str | None = None
    ) -> Intervals:
        expr = self.components[component] if component else self.whole()
        return material_intervals(expr, o, d, self.instances)

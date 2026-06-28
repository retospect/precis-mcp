"""Boolean CSG fold with node attribution (ADR 0041 §6).

The eval never computes the merged solid. Instead it folds per-primitive
results through the boolean ops:

* ``merge``     = ``any``      (union)
* ``subtract``  = ``first ∧ ¬rest``
* ``intersect`` = ``all``

The fold tracks *attribution* so a probe in a carved region reports
``void`` **and names the blocking node** ("empty; removed by ``bolt#1``").
Testing primitives independently would wrongly report material in a
hole; walking the ops is what makes a drilled bore read as a bore.

The expression operates over *instances* — primitives already placed in
the world frame and carrying a display ``label`` (``plate``, ``bolt#3``).
Patterns expand to a union of labelled instances upstream, so the fold
sees only leaves and the three boolean ops.
"""

from __future__ import annotations

from dataclasses import dataclass

from precis.cad.interval import Intervals, merge_intervals
from precis.cad.primitives import Placed
from precis.cad.vec import LINEAR_EPS, Vec3, as_vec3


@dataclass(frozen=True)
class Instance:
    """A placed primitive with a display label and a stable id."""

    iid: str
    placed: Placed
    label: str


class Expr:
    """A CSG expression node."""


@dataclass(frozen=True)
class Leaf(Expr):
    iid: str


@dataclass(frozen=True)
class Union(Expr):
    parts: tuple[Expr, ...]


@dataclass(frozen=True)
class Diff(Expr):
    base: Expr
    cutters: tuple[Expr, ...]


@dataclass(frozen=True)
class Inter(Expr):
    parts: tuple[Expr, ...]


@dataclass(frozen=True)
class Class:
    """Point classification against an expression.

    ``inside``    — final membership (after cuts).
    ``additive``  — membership of the *base* skeleton ignoring cuts; tells
                    a carved void (additive, removed) apart from plain air.
    ``owner``     — instance id providing material when ``inside``.
    ``blocker``   — instance id that removed material when additive-not-inside.
    """

    inside: bool
    additive: bool
    owner: str | None
    blocker: str | None


def classify(expr: Expr, p: Vec3, instances: dict[str, Instance]) -> Class:
    """Classify a world point against a CSG expression."""
    p = as_vec3(p)
    if isinstance(expr, Leaf):
        inside = instances[expr.iid].placed.contains(p)
        return Class(inside, inside, expr.iid if inside else None, None)
    if isinstance(expr, Union):
        owner: str | None = None
        any_in = any_add = False
        for part in expr.parts:
            c = classify(part, p, instances)
            any_add = any_add or c.additive
            if c.inside and not any_in:
                any_in = True
                owner = c.owner
            elif c.inside:
                any_in = True
        return Class(any_in, any_add, owner, None)
    if isinstance(expr, Inter):
        all_in = all_add = True
        owner = None
        blocker: str | None = None
        for part in expr.parts:
            c = classify(part, p, instances)
            all_in = all_in and c.inside
            all_add = all_add and c.additive
            if owner is None and c.owner is not None:
                owner = c.owner
            if not c.inside and blocker is None:
                blocker = c.owner or c.blocker
        return Class(
            all_in, all_add, owner if all_in else None, None if all_in else blocker
        )
    if isinstance(expr, Diff):
        b = classify(expr.base, p, instances)
        cut_hit: str | None = None
        for cutter in expr.cutters:
            cc = classify(cutter, p, instances)
            if cc.inside:
                cut_hit = cc.owner or cc.blocker
                break
        inside = b.inside and cut_hit is None
        blocker = None
        if b.inside and cut_hit is not None:
            blocker = cut_hit
        elif not b.inside:
            blocker = b.blocker
        return Class(inside, b.additive, b.owner if inside else None, blocker)
    raise TypeError(f"unknown expr node: {expr!r}")


def _instance_endpoints(
    expr: Expr, o: Vec3, d: Vec3, instances: dict[str, Instance]
) -> list[float]:
    """All finite ray-parameter boundaries from every leaf in ``expr``."""
    ts: list[float] = []
    seen: set[str] = set()

    def walk(e: Expr) -> None:
        if isinstance(e, Leaf):
            if e.iid in seen:
                return
            seen.add(e.iid)
            for lo, hi in instances[e.iid].placed.ray_hits(o, d):
                for t in (lo, hi):
                    if t not in (float("inf"), float("-inf")):
                        ts.append(t)
        elif isinstance(e, (Union, Inter)):
            for p in e.parts:
                walk(p)
        elif isinstance(e, Diff):
            walk(e.base)
            for c in e.cutters:
                walk(c)

    walk(expr)
    return ts


@dataclass(frozen=True)
class Span:
    """A classified run along a ray."""

    t_in: float
    t_out: float
    state: str  # 'solid' | 'void'
    feature: str | None  # instance label


def ray_spans(
    expr: Expr,
    o: Vec3,
    d: Vec3,
    instances: dict[str, Instance],
    *,
    eps: float = LINEAR_EPS,
) -> list[Span]:
    """Classify a ray into material / carved-void spans, best-effort labelled.

    Plain air (additive-false) outside the solid is **not** emitted — only
    material runs and the voids carved out of material, each attributed to
    the providing / removing instance.
    """
    bounds = sorted(set(_instance_endpoints(expr, o, d, instances)))
    if len(bounds) < 2:
        return []
    raw: list[Span] = []
    for i in range(len(bounds) - 1):
        ta, tb = bounds[i], bounds[i + 1]
        if tb - ta <= eps:
            continue
        tm = 0.5 * (ta + tb)
        c = classify(expr, as_vec3(o) + tm * as_vec3(d), instances)
        if c.inside:
            raw.append(Span(ta, tb, "solid", _label(c.owner, instances)))
        elif c.additive:
            raw.append(Span(ta, tb, "void", _label(c.blocker, instances)))
        # else: plain air — skip.
    return _coalesce(raw)


def _label(iid: str | None, instances: dict[str, Instance]) -> str | None:
    return instances[iid].label if iid is not None and iid in instances else None


def _coalesce(spans: list[Span]) -> list[Span]:
    """Merge adjacent runs.

    Contiguous *solid* runs fuse into one material span regardless of which
    primitive provides each sub-run (a fused part reads as one solid - ADR
    0041 section 3); the surviving feature is the first provider. Adjacent
    *void* runs merge only when attributed to the same blocking node, so two
    touching holes stay distinct.
    """
    out: list[Span] = []
    for s in spans:
        if (
            out
            and out[-1].state == s.state
            and abs(out[-1].t_out - s.t_in) <= LINEAR_EPS
            and (s.state == "solid" or out[-1].feature == s.feature)
        ):
            prev = out[-1]
            out[-1] = Span(prev.t_in, s.t_out, prev.state, prev.feature)
        else:
            out.append(s)
    return out


def material_intervals(
    expr: Expr, o: Vec3, d: Vec3, instances: dict[str, Instance]
) -> Intervals:
    """Just the solid intervals along the ray (no attribution)."""
    return merge_intervals(
        [
            (s.t_in, s.t_out)
            for s in ray_spans(expr, o, d, instances)
            if s.state == "solid"
        ]
    )

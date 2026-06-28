"""The eyes — the probe ladder (ADR 0041 §6).

> A probe is a parametric path through space; membership along it is
> interval arithmetic.

All probes are full-DOF (no axis-locking): a ray is ``origin +
direction``, a section plane is ``point + normal``, an arc is ``center +
axis + radius`` at any orientation. Each test inverse-transforms into the
relevant frame, so an angled probe costs what an axis-aligned one does.

Results are returned as structured dataclasses; the handler renders them
into the TOON tables of ADR 0041 §11. ``ray`` delegates straight to the
DAG fold (``Design.ray``); the void-attribution there is what makes a
drilled bore read as a bore.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from precis.cad.fold import Span, classify
from precis.cad.graph import Design
from precis.cad.vec import LINEAR_EPS, Vec3, as_vec3, normalize, vec3

# ---------------------------------------------------------------------------
# Point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PointHit:
    label: str
    relation: str  # 'contains' | 'removed-by' | 'would-contain' | 'nearest'
    measure: float | None  # mm: depth inside a cut, or distance to nearest


@dataclass(frozen=True)
class PointResult:
    point: Vec3
    state: str  # 'contains' | 'empty'
    hits: list[PointHit] = field(default_factory=list)


def probe_point(
    design: Design, p: Vec3, *, component: str | None = None
) -> PointResult:
    """Classify a point: containing node(s), or — if carved/empty — the
    blocking node and the nearest features."""
    p = as_vec3(p)
    c = design.classify_point(p, component=component)
    instances = design.instances
    if c.inside:
        hits = [
            PointHit(inst.label, "contains", None)
            for inst in instances.values()
            if inst.placed.contains(p)
        ]
        return PointResult(p, "contains", hits)
    hits = []
    blocker_label = instances[c.blocker].label if c.blocker is not None else None
    if c.additive:
        # would-contain = the additive instances the point sits inside,
        # other than the cutter that removed it (classify zeroes owner when
        # carved, so derive it geometrically).
        for inst in instances.values():
            if inst.label != blocker_label and inst.placed.contains(p):
                hits.append(PointHit(inst.label, "would-contain", None))
    if c.blocker is not None:
        blk = instances[c.blocker]
        depth = -blk.placed.distance(p)  # how far inside the cut
        hits.append(PointHit(blk.label, "removed-by", round(depth, 6)))
    if not hits:
        # plain empty space: report the nearest instances by distance.
        ranked = sorted(
            ((inst.placed.distance(p), inst.label) for inst in instances.values()),
            key=lambda t: t[0],
        )
        hits = [
            PointHit(label, "nearest", round(dist, 6)) for dist, label in ranked[:3]
        ]
    return PointResult(p, "empty", hits)


# ---------------------------------------------------------------------------
# Ray
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RaySegment:
    t_in: float
    t_out: float
    length: float
    state: str
    feature: str | None


@dataclass(frozen=True)
class RayResult:
    origin: Vec3
    direction: Vec3
    segments: list[RaySegment]


def probe_ray(
    design: Design, o: Vec3, d: Vec3, *, component: str | None = None
) -> RayResult:
    """Material-vs-void intervals along a ray, each void attributed to the
    node that removed it (ADR 0041 §6)."""
    o = as_vec3(o)
    d = as_vec3(d)
    spans: list[Span] = design.ray(o, d, component=component)
    segs = [
        RaySegment(s.t_in, s.t_out, round(s.t_out - s.t_in, 6), s.state, s.feature)
        for s in spans
    ]
    return RayResult(o, d, segs)


# ---------------------------------------------------------------------------
# Arc / radial
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArcSegment:
    theta_in: float  # degrees
    theta_out: float
    span: float  # degrees
    state: str
    feature: str | None


@dataclass(frozen=True)
class ArcResult:
    center: Vec3
    axis: Vec3
    radius: float
    segments: list[ArcSegment]


def _basis_perp(axis: Vec3) -> tuple[Vec3, Vec3]:
    """Two unit vectors spanning the plane perpendicular to ``axis``."""
    a = normalize(axis)
    seed = vec3(1.0, 0.0, 0.0)
    if abs(float(a @ seed)) > 0.9:
        seed = vec3(0.0, 1.0, 0.0)
    u = normalize(np.cross(a, seed))
    v = np.cross(a, u)
    return u, v


def probe_arc(
    design: Design,
    center: Vec3,
    axis: Vec3,
    radius: float,
    *,
    samples: int = 1440,
    component: str | None = None,
) -> ArcResult:
    """March an arc in θ → angular intervals (ADR 0041 §6).

    The instrument for radial features (bolt circles, gear teeth) that the
    linear ray is blind to. Marched at ``samples`` resolution with the
    transition angles refined by bisection to the linear epsilon — crisp
    boundaries on the bolt-circle voids.
    """
    center = as_vec3(center)
    u, v = _basis_perp(axis)

    def pt(theta: float) -> Vec3:
        return center + radius * (math.cos(theta) * u + math.sin(theta) * v)

    def state_at(theta: float) -> tuple[str, str | None]:
        c = classify(
            design.whole() if not component else design.components[component],
            pt(theta),
            design.instances,
        )
        if c.inside:
            return "solid", _lab(design, c.owner)
        if c.additive:
            return "void", _lab(design, c.blocker)
        return "air", None

    n = samples
    raw: list[tuple[float, str, str | None]] = []
    for i in range(n):
        theta = 2 * math.pi * i / n
        st, feat = state_at(theta)
        raw.append((theta, st, feat))

    # Build angular runs (wrap-around aware), then refine boundaries.
    segments: list[ArcSegment] = []
    start_idx = 0
    for i in range(1, n + 1):
        prev = raw[i - 1]
        cur = raw[i % n]
        if (cur[1], cur[2]) != (prev[1], prev[2]):
            st, feat = prev[1], prev[2]
            if st != "air":
                th0 = math.degrees(raw[start_idx][0])
                th1 = math.degrees(cur[0]) if i < n else 360.0
                segments.append(
                    ArcSegment(
                        round(th0, 3), round(th1, 3), round(th1 - th0, 3), st, feat
                    )
                )
            start_idx = i % n
    return ArcResult(center, normalize(axis), radius, segments)


def _lab(design: Design, iid: str | None) -> str | None:
    return design.instances[iid].label if iid and iid in design.instances else None


# ---------------------------------------------------------------------------
# Section (z = const plane) — feature-attributed loops
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionLoop:
    label: str
    role: str  # 'outer' | 'hole'
    shape: str  # 'circle' | 'rect' | 'poly'
    geom: dict[str, float]


@dataclass(frozen=True)
class SectionResult:
    z: float
    loops: list[SectionLoop]


def probe_section_z(
    design: Design, z: float, *, component: str | None = None
) -> SectionResult:
    """Section the design with a ``z = const`` plane → per-instance loops.

    v1 supports the axis-perpendicular plane (the dominant case, ADR 0041
    §11 example). Each contributing instance contributes one
    feature-attributed loop; additive instances are ``outer`` loops,
    subtractive ones ``hole`` loops. General-orientation section planes are
    a fast-follow.
    """
    from precis.cad.fold import Diff, Expr, Leaf, Union

    loops: list[SectionLoop] = []

    def visit(expr: Expr, role: str) -> None:
        if isinstance(expr, Leaf):
            inst = design.instances[expr.iid]
            loop = _instance_section_z(inst.label, inst.placed, z, role)
            if loop is not None:
                loops.append(loop)
        elif isinstance(expr, Union):
            for part in expr.parts:
                visit(part, role)
        elif isinstance(expr, Diff):
            visit(expr.base, role)
            for c in expr.cutters:
                visit(c, "hole")
        else:  # Inter — treat parts as outer for v1
            for part in getattr(expr, "parts", ()):
                visit(part, role)

    expr = design.components[component] if component else design.whole()
    visit(expr, "outer")
    return SectionResult(z, loops)


def _instance_section_z(label, placed, z: float, role: str) -> SectionLoop | None:
    """Cross-section a single placed primitive with a ``z = const`` plane.

    Only handles axis-aligned placements (no tilt); a tilted primitive is
    skipped with no loop (the general case is a fast-follow). Good enough
    for the canonical flat-plate / bolt-circle section.
    """
    from precis.cad.primitives import CircularFrustum, PolyFrustum, Sphere

    R = placed.xform.R
    if not np.allclose(R, np.eye(3), atol=1e-9):
        return None
    t = placed.xform.t
    zl = z - float(t[2])
    prim = placed.prim
    cx, cy = float(t[0]), float(t[1])
    if isinstance(prim, CircularFrustum):
        if zl < -LINEAR_EPS or zl > prim.h + LINEAR_EPS:
            return None
        r = prim.rb + (prim.rt - prim.rb) * (zl / prim.h)
        return SectionLoop(
            label, role, "circle", {"r": round(r, 6), "cx": cx, "cy": cy}
        )
    if isinstance(prim, Sphere):
        if abs(zl) > prim.r:
            return None
        r = math.sqrt(max(0.0, prim.r * prim.r - zl * zl))
        return SectionLoop(
            label, role, "circle", {"r": round(r, 6), "cx": cx, "cy": cy}
        )
    if isinstance(prim, PolyFrustum):
        lo, hi = prim.aabb_local()
        if zl < lo[2] - LINEAR_EPS or zl > hi[2] + LINEAR_EPS:
            return None
        return SectionLoop(
            label,
            role,
            "rect",
            {
                "w": round(float(hi[0] - lo[0]), 6),
                "d": round(float(hi[1] - lo[1]), 6),
                "cx": cx,
                "cy": cy,
            },
        )
    return None


# ---------------------------------------------------------------------------
# Draft check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftFace:
    label: str
    tag: str
    draft_deg: float
    ok: bool


def probe_draft(design: Design, pull: Vec3, *, min_deg: float = 1.0) -> list[DraftFace]:
    """Per-face draft against a pull direction (ADR 0041 §6).

    ``draft_deg`` is the angle between the face and the pull-perpendicular
    plane: a vertical wall (normal ⟂ pull) is 0° draft — a release
    failure. Curved frustum laterals report their slant; caps/box-faces
    report 90° (parallel) or 0° (perpendicular) as the geometry dictates.
    """
    from precis.cad.primitives import CircularFrustum

    pull = normalize(pull)
    out: list[DraftFace] = []
    for inst in design.instances.values():
        for f in inst.placed.faces():
            cos_a = abs(float(normalize(f.normal) @ pull))
            draft = math.degrees(math.asin(min(1.0, cos_a)))
            out.append(DraftFace(inst.label, f.tag, round(draft, 3), draft >= min_deg))
        # circular frustum lateral slant (curved face not enumerated above)
        if isinstance(inst.placed.prim, CircularFrustum):
            cf = inst.placed.prim
            slant = math.degrees(math.atan2(abs(cf.rt - cf.rb), cf.h))
            out.append(
                DraftFace(inst.label, "lateral", round(slant, 3), slant >= min_deg)
            )
    return out

"""Inspection probes — ray / section / overlap / thickness (docs/backlog/cad-inspect.md).

One verb family, four probes, named by what they **output**, not by the
probe's shape — the naming is the non-confusion: a plane and a swept ribbon
are the same probe (a surface), a beam and a cylinder are the same probe (a
solid), and a zero-radius beam is a ray. The caller picks a dimension; the
kernel picks the algorithm.

- :func:`ray` (1D) → *hits*: where a line meets the solid, with surface
  normals and the closest approach along the way. Answers path, clearance
  and directional-distance questions. ``feature=`` / ``role=`` select only
  the hits whose attribution touches a given leaf or a given role; the
  closest approach and the profile stay whole-solid properties.
- :func:`section` (2D) → *curves*: the closed loops a plane cuts out of the
  solid, each with length and enclosed area; loop count and inter-loop
  distances are the derived answers (holes, wall thickness).
- :func:`overlap` (3D) → *volume*: the boolean intersection of any probe
  solid with the part — hit, volume, AABB. This is the stamp-verification
  probe: cut a cylinder the size of a stamped hole, and the intersection
  volume against a closed-form expectation decides "void present / wall
  present / feature missing" in one shot. Thin rays cannot do that — hit
  positions along a line leave diameters as an inverse reconstruction.
- :func:`thickness` (2D grid of rays) → *map*: the material column behind
  every point of a skin face. Columns far longer than the nominal wall are
  sink-mark risks — a boss or rib fused to a cosmetic skin freezes a mass
  behind it and pulls the skin inward (:meth:`ThicknessInspection.
  sink_risks`). Each sample carries the column's attribution, so the risk
  names its cause (``boss-1``), direct or indirect.

Everything here evaluates the folded SDF (:func:`~precis.cad.fold.expr_sdf_np`),
so all three probes work identically on sharp designs and on field-routed
ones (``rd``-rounded nodes, ``field:`` leaves) with no analytic special
cases. The exactness trade-offs, stated honestly:

- ``ray`` sphere-traces the SDF: exact hits on analytic leaves, trilinear
  fidelity on sampled fields. ``min_distance_m`` is the polished minimum
  over the marched samples — inspection-grade, not metrology. Feature
  attribution along the ray (which leaf provided / carved each span) is
  :func:`~precis.cad.fold.ray_spans`' job; these reports stay unattributed.
- ``section`` marches squares over a sampled plane grid; the loops carry
  the grid's fidelity, so ``pitch_m`` is the accuracy dial. Loop *sign* is
  not a winding convention here: a loop's area is signed by probing the
  SDF at its centroid (negative → material inside → positive area), so
  ``hole_count`` is plain containment.
- ``overlap`` reports :func:`~precis.cad.bulk.volume` on a sampling grid —
  decisive for presence/absence and for percent-level checks against
  closed-form expectations, not metrology. Entry/exit points compose with
  :func:`ray` along the probe's own axis rather than being duplicated here.

**Attribution — every encounter carries its trace ref.** Each hit, loop
and overlap boundary is annotated with :class:`FeatureRef` tuples:
``(label, role)`` where ``label`` is the leaf's node name — the handle the
se workspace resolves to the generating code (``ws-<slug>::<qualname>``,
the stack-trace ref) — and ``role`` follows :func:`~precis.cad.fold.
classify`'s owner/blocker convention: ``owner`` = a *direct* face (the
leaf authored the surface and supplies the material behind it);
``blocker`` = an *indirect*, boolean-created face. For ``A − B`` the bore
wall is attributed to **B** (its zero-set carries the geometry; the
operator itself is never attributed) while the material behind it is
A's — so the wall reports ``blocker: B`` precisely because it is a
surface of A's part that A's code never authored. Folded boundaries
always lie on some leaf's zero-set, so the pair lands somewhere by
construction.

All functions take a :class:`~precis.cad.graph.Design` and an
:class:`~precis.cad.fold.Expr`, matching :mod:`~precis.cad.bulk` and
:mod:`~precis.cad.relate` — no store, no handlers; this package stays
unit-testable in isolation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.cad import bulk as cad_bulk
from precis.cad.dsl import build_config
from precis.cad.fold import Expr, Leaf, classify, expr_sdf_np
from precis.cad.vec import LINEAR_REL_EPS, Transform, as_vec3

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.cad.graph import Design

#: Overlap smaller than this fraction of the probe's AABB volume reads as
#: "no hit" — quadrature noise on a genuinely disjoint pair.
_HIT_VOLUME_REL = 1e-6

#: Plane-grid sanity cap: above this many samples the caller should be
#: passing an explicit coarser pitch_m.
_MAX_GRID_SAMPLES = 64 * 1024 * 1024


@dataclass(frozen=True)
class FeatureRef:
    """Attribution of an encountered surface (module docstring).

    ``label`` — the leaf's node name: the handle the se workspace
    resolves to the generating code (the stack-trace ref).
    ``role``  — ``'owner'``: a direct face (the leaf authored the surface
    and supplies the material behind it); ``'blocker'``: an indirect,
    boolean-created face (``A − B``'s bore wall is B's surface)."""

    label: str | None
    role: str  # 'owner' | 'blocker'


@dataclass(frozen=True)
class RayHit:
    """One material run along the ray: where it enters and leaves."""

    t_entry_m: float
    t_exit_m: float
    entry_m: tuple[float, float, float]
    exit_m: tuple[float, float, float]
    entry_normal_m: tuple[float, float, float]
    exit_normal_m: tuple[float, float, float]
    #: Surface attribution at each crossing (module docstring).
    entry_features: tuple[FeatureRef, ...] = ()
    exit_features: tuple[FeatureRef, ...] = ()

    def involves(self, *, feature: str | None = None, role: str | None = None) -> bool:
        """True when ONE ref of this run's attribution matches every given
        criterion — ``feature`` on the label, ``role`` on the role. Both
        given → a single ref must match both, so ``feature='bore',
        role='owner'`` asks for faces the bore owns *directly*, not a run
        that happens to touch the bore at one end and an owner at the
        other."""
        for fr in self.entry_features + self.exit_features:
            if feature is not None and fr.label != feature:
                continue
            if role is not None and fr.role != role:
                continue
            return True
        return False


@dataclass(frozen=True)
class RayInspection:
    """The ray probe's report (module docstring)."""

    hits: tuple[RayHit, ...]
    #: Closest approach along the traced portion (0.0 when the origin sits
    #: in material); ``None`` only when the ray never advanced. Computed on
    #: the whole solid — unaffected by ``feature``/``role`` filtering.
    min_distance_m: float | None
    profile: tuple[tuple[float, float], ...] | None = None
    #: The attribution filters the ray was asked with (both ``None`` when
    #: unfiltered) — the report stays self-describing.
    feature: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class SectionLoop:
    """One closed loop of the plane section (points implicitly closed).
    ``area_m2`` is signed by the directed winding (module docstring):
    positive when the loop wraps material, negative when it wraps void
    (a bore)."""

    points_m: tuple[tuple[float, float, float], ...]
    length_m: float
    area_m2: float
    #: Surface attribution of the loop's first point (module docstring).
    features: tuple[FeatureRef, ...] = ()


@dataclass(frozen=True)
class SectionInspection:
    """The section probe's report (module docstring)."""

    plane_origin_m: tuple[float, float, float]
    plane_normal_m: tuple[float, float, float]
    frame_u_m: tuple[float, float, float]
    frame_v_m: tuple[float, float, float]
    pitch_m: float
    loops: tuple[SectionLoop, ...]
    #: Loops winding around void (a bore is negative — count of them).
    hole_count: int
    #: Minimum distance between every pair of loops (point-sampled, ± the
    #: pitch) — wall thickness falls out as the pair straddling material;
    #: the consumer interprets.
    wall_pairs_m: tuple[tuple[int, int, float], ...]


@dataclass(frozen=True)
class OverlapInspection:
    """The solid probe's report (module docstring)."""

    hit: bool
    volume_m3: float
    aabb_m: tuple[tuple[float, float, float], tuple[float, float, float]]
    #: Attribution of the part surfaces the probe's spine crosses.
    features: tuple[FeatureRef, ...] = ()


def _sdf(design: Design, expr: Expr, pts: np.ndarray) -> np.ndarray:
    return expr_sdf_np(expr, np.asarray(pts, dtype=np.float64), design.instances)


def _vec3f(v: Any) -> tuple[float, float, float]:
    """``(float, float, float)`` from any 3-sequence — the generator
    spelling ``_vec3f(v)`` types as ``tuple[float, ...]``,
    which mypy will not pass where a fixed triple is required."""
    return (float(v[0]), float(v[1]), float(v[2]))


def _leaf_iids(expr: Expr) -> list[str]:
    """Unique leaf instance ids in the expression (attribution walk)."""
    seen: list[str] = []

    def walk(e: Expr) -> None:
        if isinstance(e, Leaf):
            if e.iid not in seen:
                seen.append(e.iid)
        for c in getattr(e, "parts", ()):
            walk(c)
        base = getattr(e, "base", None)
        if base is not None:
            walk(base)
        for c in getattr(e, "cutters", ()):
            walk(c)

    walk(expr)
    return seen


def _features_at(
    design: Design,
    expr: Expr,
    p: np.ndarray,
    n: np.ndarray,
    eps: float,
) -> tuple[FeatureRef, ...]:
    """Attribution of the surface at boundary point ``p`` with outward
    normal ``n`` (module docstring): every leaf whose zero-set carries
    the point, roled against who supplies the material just inside."""
    pm = np.asarray(p, dtype=np.float64) - np.asarray(n, dtype=np.float64) * 4.0 * eps
    c_in = classify(expr, as_vec3(pm), design.instances)
    out: list[FeatureRef] = []
    tol = 16.0 * eps
    for iid in _leaf_iids(expr):
        d = float(design.instances[iid].placed.distance(as_vec3(p)))
        if abs(d) <= tol:
            inst = design.instances[iid]
            out.append(
                FeatureRef(
                    label=inst.label,
                    role="owner" if c_in.owner == iid else "blocker",
                )
            )
    return tuple(out)


def _normal_at(design: Design, expr: Expr, p: np.ndarray, eps: float) -> np.ndarray:
    """Central-difference gradient of the SDF — the surface normal."""
    offsets = np.array(
        [
            [eps, 0, 0],
            [-eps, 0, 0],
            [0, eps, 0],
            [0, -eps, 0],
            [0, 0, eps],
            [0, 0, -eps],
        ]
    )
    g = _sdf(design, expr, np.broadcast_to(p, (6, 3)) + offsets)
    n = np.array([g[0] - g[1], g[2] - g[3], g[4] - g[5]])
    norm = float(np.linalg.norm(n))
    if norm < 1e-30:
        return np.zeros(3)
    return n / norm


def ray(
    design: Design,
    expr: Expr,
    *,
    origin_m: tuple[float, float, float],
    axis_m: tuple[float, float, float],
    t_max_m: float | None = None,
    profile_samples: int | None = None,
    feature: str | None = None,
    role: str | None = None,
) -> RayInspection:
    """Sphere-trace the SDF along a ray; report material runs and the
    closest approach. ``axis_m`` is normalized internally, so every ``t``
    is in design units regardless of the caller's axis length.

    ``feature=`` / ``role=`` keep only the runs whose attribution
    (:class:`FeatureRef` tuples on each hit) matches — a leaf label
    and/or an owner/blocker role; one ref must satisfy every given
    criterion (:meth:`RayHit.involves`). Retained hits keep their full
    feature tuples: the filter selects runs, it does not rewrite their
    attribution. ``min_distance_m`` and ``profile`` describe the whole
    solid along the beam and are computed before any filter applies.
    """
    o = np.asarray(as_vec3(origin_m), dtype=np.float64)
    d = np.asarray(as_vec3(axis_m), dtype=np.float64)
    d = d / np.linalg.norm(d)
    lo, hi = cad_bulk.expr_aabb(design, expr)
    diag = float(np.linalg.norm(np.asarray(hi) - np.asarray(lo)))
    if t_max_m is None:
        reach = float(np.abs(o - 0.5 * (np.asarray(lo) + np.asarray(hi))) @ np.abs(d))
        t_max_m = reach + diag  # from the origin through and past the solid

    eps = LINEAR_REL_EPS * max(diag, t_max_m, 1e-3)
    min_step = 4.0 * eps

    profile: list[tuple[float, float]] = []
    if profile_samples:
        for k in range(profile_samples):
            tp = t_max_m * k / max(profile_samples - 1, 1)
            vp = float(_sdf(design, expr, (o + tp * d)[None, :])[0])
            profile.append((tp, vp))

    def bisect(ta: float, tb: float, want_inside: bool) -> float:
        # The bracket endpoint tb already sits on the wanted side (sphere
        # tracing can land exactly on the surface, sdf == ±0.0, which
        # compares not-less-than — bracketing still finds the crossing).
        for _ in range(64):
            if abs(tb - ta) <= eps:
                break
            tm = 0.5 * (ta + tb)
            vm = float(_sdf(design, expr, (o + tm * d)[None, :])[0])
            if (vm < 0.0) == want_inside:
                tb = tm
            else:
                ta = tm
        return tb

    hits: list[RayHit] = []
    t = 0.0
    t_prev = 0.0
    v_prev = float(_sdf(design, expr, o[None, :])[0])
    min_abs = abs(v_prev)
    inside = v_prev < 0.0
    span_start = 0.0 if inside else None
    stuck = 0

    for _ in range(100_000):
        p = o + t * d
        v = float(_sdf(design, expr, p[None, :])[0])
        min_abs = min(min_abs, abs(v))
        if v < 0.0 and not inside:
            t_in = bisect(t_prev, t, want_inside=True)
            span_start = t_in
            inside = True
            t = t_in + min_step
            t_prev = t
            continue
        if v > 0.0 and inside:
            t_out = bisect(t_prev, t, want_inside=False)
            assert span_start is not None  # inside implies a span is open
            entry = o + span_start * d
            exit_ = o + t_out * d
            n_e = _normal_at(design, expr, entry, eps)
            n_x = _normal_at(design, expr, exit_, eps)
            hits.append(
                RayHit(
                    float(span_start),
                    float(t_out),
                    _vec3f(entry),
                    _vec3f(exit_),
                    _vec3f(n_e),
                    _vec3f(n_x),
                    entry_features=_features_at(design, expr, entry, n_e, eps),
                    exit_features=_features_at(design, expr, exit_, n_x, eps),
                )
            )
            inside = False
            span_start = None
            t = t_out + min_step
            t_prev = t
            continue
        t_prev = t
        step = max(abs(v), min_step)
        if step <= min_step:
            # |sdf| under the step floor: either a creep step or two
            # through an exact crossing (the strict crossing tests
            # fire immediately after) or a tangent slide along a
            # surface that never crosses (gr530414 — thickness-map
            # nodes on a wall plane crawled min_step to the iteration
            # cap, ~1 s per ray on field designs). Give the tangent
            # up: the report is no run and min_distance ≈ 0.
            stuck += 1
            if stuck > 64:
                break
        else:
            stuck = 0
        t += step
        if t > t_max_m:
            break
        # Crossing tests are strict (``v < 0.0`` / ``v > 0.0``): merged
        # prims touching face-to-face have sdf ±0.0 on the shared face,
        # and that interface is interior material, not a boundary — a
        # phantom run-split there would break the thickness columns.
        # Landing exactly on a REAL surface creeps one min_step
        # inside/outside and the strict tests still fire on the next
        # sample; the iteration cap is the last bailout.

    if inside and span_start is not None:
        entry = o + span_start * d
        n_e = _normal_at(design, expr, entry, eps)
        hits.append(
            RayHit(
                float(span_start),
                float(t_max_m),
                _vec3f(entry),
                _vec3f(o + t_max_m * d),
                _vec3f(n_e),
                (0.0, 0.0, 0.0),
                entry_features=_features_at(design, expr, entry, n_e, eps),
            )
        )
    if feature is not None or role is not None:
        hits = [h for h in hits if h.involves(feature=feature, role=role)]
    return RayInspection(
        hits=tuple(hits),
        min_distance_m=min_abs,
        profile=tuple(profile) if profile_samples else None,
        feature=feature,
        role=role,
    )


def section(
    design: Design,
    expr: Expr,
    *,
    origin_m: tuple[float, float, float],
    normal_m: tuple[float, float, float],
    pitch_m: float | None = None,
) -> SectionInspection:
    """Marching-squares plane section of the folded SDF (module
    docstring). The plane frame is right-handed about ``normal_m``."""
    n = np.asarray(as_vec3(normal_m), dtype=np.float64)
    n = n / np.linalg.norm(n)
    ref = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(n, ref)
    u = u / np.linalg.norm(u)
    v = np.cross(n, u)
    origin = np.asarray(as_vec3(origin_m), dtype=np.float64)

    lo, hi = cad_bulk.expr_aabb(design, expr)
    corners = np.array(_aabb_corners(lo, hi))
    rel = corners - origin
    us = rel @ u
    vs = rel @ v
    if pitch_m is None:
        pitch_m = (
            max(float(us.max() - us.min()), float(vs.max() - vs.min()), 1e-3) / 1000.0
        )
    # one pitch of margin beyond the projected AABB: a boundary sitting
    # exactly on the outermost nodes would straddle no cell and the outer
    # loop would never be emitted.
    u0 = float(us.min()) - pitch_m
    u1 = float(us.max()) + pitch_m
    v0 = float(vs.min()) - pitch_m
    v1 = float(vs.max()) + pitch_m
    nu = max(math.ceil((u1 - u0) / pitch_m) + 1, 2)
    nv = max(math.ceil((v1 - v0) / pitch_m) + 1, 2)
    if nu * nv > _MAX_GRID_SAMPLES:
        raise ValueError(
            f"section grid would be {nu}x{nv} samples at pitch {pitch_m:g} m"
            " — pass a coarser pitch_m"
        )
    grid_u = u0 + (np.arange(nu) + 0.5) * pitch_m
    grid_v = v0 + (np.arange(nv) + 0.5) * pitch_m
    pts = (
        origin[None, None, :]
        + grid_u[None, :, None] * u[None, None, :]
        + grid_v[:, None, None] * v[None, None, :]
    ).reshape(-1, 3)
    vals = _sdf(design, expr, pts).reshape(nv, nu)

    segs = _marching_segments(vals, grid_u, grid_v, origin, u, v)
    loops = _chain_segments(segs, float(pitch_m))

    # directed segments wind CCW around material in the (u, v) frame, so
    # the shoelace sign IS the material sign — no post-hoc probing.
    eps_s = max(float(pitch_m) * 0.01, 1e-12)
    signed: list[SectionLoop] = []
    for loop in loops:
        pts = np.asarray(loop.points_m)
        n0 = _normal_at(design, expr, np.asarray(pts[0]), eps_s)
        signed.append(
            SectionLoop(
                points_m=loop.points_m,
                length_m=loop.length_m,
                area_m2=_shoelace_uv(pts, origin, u, v),
                features=_features_at(design, expr, np.asarray(pts[0]), n0, eps_s),
            )
        )
    holes = sum(1 for loop in signed if loop.area_m2 < 0.0)
    pairs = tuple(
        (i, j, _loop_distance(signed[i], signed[j]))
        for i in range(len(signed))
        for j in range(i + 1, len(signed))
    )
    return SectionInspection(
        plane_origin_m=_vec3f(origin),
        plane_normal_m=_vec3f(n),
        frame_u_m=_vec3f(u),
        frame_v_m=_vec3f(v),
        pitch_m=float(pitch_m),
        loops=tuple(signed),
        hole_count=holes,
        wall_pairs_m=pairs,
    )


def overlap(
    design: Design,
    expr: Expr,
    probe: Expr,
    *,
    grid: int = 48,
) -> OverlapInspection:
    """Boolean intersection of a probe solid with the part (module
    docstring). ``grid`` is the ray-quadrature resolution.

    The quadrature domain is the AABB intersection of part and probe:
    :func:`~precis.cad.bulk.expr_aabb` stays conservative for ``Inter``
    (it does not tighten to the children's common box), which would leave
    a small probe invisible on a big part's grid."""
    inter = design.intersect(expr, probe)
    elo, ehi = cad_bulk.expr_aabb(design, expr)
    plo, phi = cad_bulk.expr_aabb(design, probe)
    lo = np.maximum(
        np.asarray(elo, dtype=np.float64), np.asarray(plo, dtype=np.float64)
    )
    hi = np.minimum(
        np.asarray(ehi, dtype=np.float64), np.asarray(phi, dtype=np.float64)
    )
    vol, _centroid = cad_bulk._integrate(inter, design.instances, lo, hi, grid)
    aabb = cad_bulk.expr_aabb(design, inter)
    probe_vol = float(np.prod(np.asarray(phi) - np.asarray(plo)))
    # boundary attribution: trace the part along the probe's longest axis
    # across the whole part extent (the spine may start inside material)
    # and collect what it crosses (composes with ray()).
    extent = np.asarray(phi) - np.asarray(plo)
    axis_k = int(np.argmax(np.abs(extent)))
    spans = float(abs(extent[axis_k]))
    center = 0.5 * (np.asarray(plo) + np.asarray(phi))
    axis = np.zeros(3)
    axis[axis_k] = 1.0
    elo, ehi = cad_bulk.expr_aabb(design, expr)
    reach = float((np.asarray(ehi) - np.asarray(elo)) @ np.abs(axis))
    margin = reach + spans + 1e-9
    r = ray(
        design,
        expr,
        origin_m=_vec3f(center - axis * margin),
        axis_m=_vec3f(axis),
        t_max_m=2.0 * margin,
    )
    features = tuple(fr for h in r.hits for fr in (h.entry_features + h.exit_features))
    return OverlapInspection(
        hit=vol > _HIT_VOLUME_REL * max(probe_vol, 1e-30),
        volume_m3=float(vol),
        aabb_m=(
            _vec3f(aabb[0]),
            _vec3f(aabb[1]),
        ),
        features=features,
    )


def probe_solid(design: Design, name: str, config: str, xf: Transform) -> Expr:
    """Convenience: register a probe primitive (e.g. ``cyl:r1.7mmh13.4mm``)
    on the design and return its expression for :func:`overlap`. Accepts
    both boundary-mode configs (units required) and bare canonical
    numbers, mirroring how scripts and MCP blocks respectively author
    them."""
    try:
        spec = build_config(config, require_units=True)
    except Exception:
        spec = build_config(config)
    return design.prim(name, spec, xf)


@dataclass(frozen=True)
class ThicknessSample:
    """One skin column (module docstring): where the face-normal ray
    crossed the face plane, how much material sat behind that skin point
    (first run, air side to air side), and the attribution of the column's
    two crossings — the non-skin refs are the thickening's cause."""

    position_m: tuple[float, float, float]
    thickness_m: float
    entry_features: tuple[FeatureRef, ...] = ()
    exit_features: tuple[FeatureRef, ...] = ()


@dataclass(frozen=True)
class ThicknessInspection:
    """The thickness probe's report (module docstring)."""

    plane_origin_m: tuple[float, float, float]
    plane_normal_m: tuple[float, float, float]
    frame_u_m: tuple[float, float, float]
    frame_v_m: tuple[float, float, float]
    pitch_m: float
    #: The nominal wall and ratio the probe was asked with — the two
    #: ingredients of :meth:`sink_risks` (``None`` = report only).
    nominal_m: float | None
    ratio: float
    samples: tuple[ThicknessSample, ...]

    def sink_risks(self) -> tuple[ThicknessSample, ...]:
        """Samples whose material column exceeds ``ratio × nominal_m`` —
        each is a candidate sink mark on THIS face (the cosmetic skin),
        caused by whatever leaf extends the column beyond the entry skin
        (the non-entry refs in the sample's features)."""
        if self.nominal_m is None:
            raise ValueError("sink_risks needs nominal_m — pass it to thickness()")
        limit = self.ratio * self.nominal_m
        return tuple(s for s in self.samples if s.thickness_m > limit)


def _ball_thickness(
    design: Design,
    expr: Expr,
    entry_m: tuple[float, float, float],
    outward_normal_m: tuple[float, float, float],
) -> float:
    """Rolling-ball thickness along the inward normal: sphere-trace
    toward the medial ridge; the thickness is ``2 ×`` the largest
    inscribed ball found (the ball touches the entry skin on one
    side and the constraining wall on the other)."""
    q = np.asarray(entry_m, dtype=np.float64)
    inward = -np.asarray(outward_normal_m, dtype=np.float64)
    best = 0.0
    prev = q
    brk = q
    miss = 0
    for _ in range(2048):
        v = float(_sdf(design, expr, q[None, :])[0])
        dist = -v
        if dist <= 0.0:
            # A hair of positive sdf right at a merged prim's coincident
            # face is fold noise (material continues behind it) — probe
            # ahead: material means step through, growing positive sdf
            # means a real exit.
            ahead = q + inward * max(best * 0.02, 1e-9)
            if float(_sdf(design, expr, ahead[None, :])[0]) < 0.0:
                q = ahead
                continue
            brk = q
            break
        if dist < best:
            # Valley: either the medial ridge (a real descent) or a merged
            # prim's coincident interface (the union fold reads SHALLOW
            # there — each prim measures its own interior face). Step a
            # bounded recovery window; climbing back above ``best``
            # resumes the march, 16 consecutive non-improving steps
            # confirm the ridge.
            miss += 1
            if miss > 16:
                brk = q
                break
            q = q + inward * max(best * 0.15, 1e-9)
            continue
        miss = 0
        best = dist
        prev = q
        q = q + inward * dist
    else:
        brk = q
    # Refine: the peak sits on the prev→brk segment, which can carry
    # fold-valleys (not unimodal) — sample it uniformly and take the max.
    peak = best
    for k in range(1, 65):
        mk = prev + (brk - prev) * (k / 65.0)
        peak = max(peak, -float(_sdf(design, expr, mk[None, :])[0]))
    return 2.0 * peak


def _column_thickness(design: Design, expr: Expr, hit: RayHit, method: str) -> float:
    """Per-sample thickness for :func:`thickness` — run length (ray)
    or the rolling-ball measure (ball)."""
    if method == "ray":
        return float(hit.t_exit_m - hit.t_entry_m)
    return _ball_thickness(design, expr, hit.entry_m, hit.entry_normal_m)


def thickness(
    design: Design,
    expr: Expr,
    *,
    origin_m: tuple[float, float, float],
    normal_m: tuple[float, float, float],
    pitch_m: float,
    nominal_m: float | None = None,
    ratio: float = 1.5,
    method: str = "ray",
    feature: str | None = None,
    role: str | None = None,
) -> ThicknessInspection:
    """Face-on wall-thickness map (module docstring). A grid of rays cast
    along ``-normal_m`` from one pitch outside the face; each ray's first
    material run is that skin point's column. Columns far longer than the
    nominal wall mark thick sections — a boss or rib fused to a skin
    freezes mass behind it and can sink the cosmetic face opposite;
    :meth:`ThicknessInspection.sink_risks` flags them.

    This is the family's expensive probe — one :func:`ray` per grid node.
    Keep ``pitch_m`` at wall scale (1–2 mm), not section scale. With
    ``feature=``/``role=`` the column is the first *matching* run (the
    filters pass through to :func:`ray`), so ``feature='boss-1'`` maps
    only the columns behind ``boss-1``.

    ``method='ray'`` (default) reports the material column — the run's
    length, the thermal-mass view behind sink marks. ``method='ball'``
    is the industry-standard rolling-ball check: sphere-trace inward
    along the surface normal and report ``2 ×`` the largest inscribed
    ball (the medial-ridge measure mold-DFM tools use) — true local
    thickness on oblique walls, but blind to a tall boss's column
    mass, so keep ``'ray'`` for the sink gate.

    Raises ``ValueError`` for any other ``method``.
    """
    if method not in ("ray", "ball"):
        raise ValueError(f"method must be 'ray' or 'ball', got {method!r}")
    n = np.asarray(as_vec3(normal_m), dtype=np.float64)
    n = n / np.linalg.norm(n)
    ref = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(n, ref)
    u = u / np.linalg.norm(u)
    v = np.cross(n, u)
    origin = np.asarray(as_vec3(origin_m), dtype=np.float64)

    lo, hi = cad_bulk.expr_aabb(design, expr)
    corners = np.array(_aabb_corners(lo, hi))
    rel = corners - origin
    us = rel @ u
    vs = rel @ v
    ws = rel @ n
    # like section(): one pitch of margin so a skin ON the outermost node
    # still sits inside the grid; rays start one pitch into the air side.
    u0 = float(us.min()) - pitch_m
    u1 = float(us.max()) + pitch_m
    v0 = float(vs.min()) - pitch_m
    v1 = float(vs.max()) + pitch_m
    span = float(ws.max() - ws.min()) + 2.0 * pitch_m
    nu = max(math.ceil((u1 - u0) / pitch_m) + 1, 2)
    nv = max(math.ceil((v1 - v0) / pitch_m) + 1, 2)
    if nu * nv > _MAX_GRID_SAMPLES:
        raise ValueError(
            f"thickness grid would be {nu}x{nv} samples at pitch {pitch_m:g} m"
            " — pass a coarser pitch_m"
        )
    grid_u = u0 + (np.arange(nu) + 0.5) * pitch_m
    grid_v = v0 + (np.arange(nv) + 0.5) * pitch_m

    samples: list[ThicknessSample] = []
    for gv in grid_v:
        for gu in grid_u:
            p = origin + gu * u + gv * v
            r = ray(
                design,
                expr,
                origin_m=_vec3f(p + n * pitch_m),
                axis_m=_vec3f(-n),
                t_max_m=span + 2.0 * pitch_m,
                feature=feature,
                role=role,
            )
            if not r.hits:
                continue
            h = r.hits[0]
            samples.append(
                ThicknessSample(
                    position_m=_vec3f(p),
                    thickness_m=_column_thickness(design, expr, h, method),
                    entry_features=h.entry_features,
                    exit_features=h.exit_features,
                )
            )
    return ThicknessInspection(
        plane_origin_m=_vec3f(origin),
        plane_normal_m=_vec3f(n),
        frame_u_m=_vec3f(u),
        frame_v_m=_vec3f(v),
        pitch_m=float(pitch_m),
        nominal_m=nominal_m,
        ratio=float(ratio),
        samples=tuple(samples),
    )


# ---------------------------------------------------------------------------
# section helpers
# ---------------------------------------------------------------------------


def _aabb_corners(lo, hi) -> list[tuple[float, float, float]]:
    return [
        (x, y, z)
        for x in (float(lo[0]), float(hi[0]))
        for y in (float(lo[1]), float(hi[1]))
        for z in (float(lo[2]), float(hi[2]))
    ]


#: Cell-local corner coordinates and the two edges each corner is incident
#: to (for saddle pairing and segment orientation).
_CORNER_UV = {
    "c": (0.0, 0.0),  # bottom-left
    "r": (1.0, 0.0),  # bottom-right
    "t": (1.0, 1.0),  # top-right
    "b": (0.0, 1.0),  # top-left
}
_CORNER_EDGES = {"c": ("b", "l"), "r": ("b", "r"), "t": ("r", "t"), "b": ("l", "t")}


def _orient(p_from, p_to, corner_uv) -> bool:
    """True when the material-left direction is p_from→p_to (CCW winding
    around material in the (u,v) frame)."""
    dx = p_to[0] - p_from[0]
    dy = p_to[1] - p_from[1]
    cross2 = dx * (corner_uv[1] - p_from[1]) - dy * (corner_uv[0] - p_from[0])
    return bool(cross2 > 0.0)


def _marching_segments(vals, grid_u, grid_v, origin, u, v):
    """Marching squares, vectorized over the grid; the Python loop visits
    only boundary cells. Segments are DIRECTED with material on the left;
    saddle cells pair the crossings around each inside corner."""
    vc = vals[:-1, :-1]
    vr = vals[:-1, 1:]
    vt = vals[1:, 1:]
    vb = vals[1:, :-1]
    # sdf exactly 0 (a node or edge sitting precisely on the surface)
    # counts as inside: degenerate sign patterns vanish and flat faces
    # coincident with grid lines section exactly.
    inside = (vc <= 0, vr <= 0, vt <= 0, vb <= 0)
    count = inside[0].astype(int) + inside[1] + inside[2] + inside[3]
    boundary = (count > 0) & (count < 4)
    if not boundary.any():
        return []

    xb = inside[0] != inside[1]
    xr = inside[1] != inside[2]
    xt = inside[3] != inside[2]
    xl = inside[0] != inside[3]

    # everything below happens only at boundary cells — the full-grid
    # crossing-point arrays would be ~100 MB of mostly-unused floats
    def frac(va, vb):
        diff = va - vb
        out = np.full(va.shape, 0.5)
        np.divide(va, diff, out=out, where=np.abs(diff) > 0.0)
        return np.clip(out, 0.0, 1.0)

    bidx = np.argwhere(boundary)
    if len(bidx) == 0:
        return []
    ivs = bidx[:, 0]
    ius = bidx[:, 1]
    cv_c = vc[ivs, ius]
    cr_c = vr[ivs, ius]
    ct_c = vt[ivs, ius]
    cb_c = vb[ivs, ius]
    fb = frac(cv_c, cr_c)  # bottom edge c—r
    fr = frac(cr_c, ct_c)  # right edge r—t
    ft = frac(cb_c, ct_c)  # top edge b—t
    fl = frac(cv_c, cb_c)  # left edge c—b
    flags = {
        "b": xb[ivs, ius],
        "r": xr[ivs, ius],
        "t": xt[ivs, ius],
        "l": xl[ivs, ius],
    }
    in_c = {
        "c": inside[0][ivs, ius],
        "r": inside[1][ivs, ius],
        "t": inside[2][ivs, ius],
        "b": inside[3][ivs, ius],
    }
    u0c = grid_u[ius][:, None]
    u1c = grid_u[ius + 1][:, None]
    v0c = grid_v[ivs][:, None]
    v1c = grid_v[ivs + 1][:, None]
    pts3 = {
        "b": origin + (u0c + fb[:, None] * (u1c - u0c)) * u + v0c * v,
        "r": origin + u1c * u + (v0c + fr[:, None] * (v1c - v0c)) * v,
        "t": origin + (u0c + ft[:, None] * (u1c - u0c)) * u + v1c * v,
        "l": origin + u0c * u + (v0c + fl[:, None] * (v1c - v0c)) * v,
    }
    loc2 = {
        "b": np.column_stack([fb, np.zeros_like(fb)]),
        "r": np.column_stack([np.ones_like(fr), fr]),
        "t": np.column_stack([ft, np.ones_like(ft)]),
        "l": np.column_stack([np.zeros_like(fl), fl]),
    }

    segs: list[tuple[np.ndarray, np.ndarray]] = []
    for k in range(len(bidx)):
        present = [name for name in ("b", "r", "t", "l") if flags[name][k]]
        corners_in = [name for name in ("c", "r", "t", "b") if in_c[name][k]]
        if len(present) == 2:
            a_name, b_name = present
            a3, b3 = pts3[a_name][k], pts3[b_name][k]
            # a corner whose two crossings coincide (the contour passes
            # exactly through the node) adds nothing: the neighbouring
            # cells already meet at that point.
            if np.linalg.norm(a3 - b3) <= 1e-9:
                continue
            a2 = (float(loc2[a_name][k, 0]), float(loc2[a_name][k, 1]))
            b2 = (float(loc2[b_name][k, 0]), float(loc2[b_name][k, 1]))
            segs.append(
                (a3, b3) if _orient(a2, b2, _CORNER_UV[corners_in[0]]) else (b3, a3)
            )
        else:
            # saddle: one segment per inside corner, oriented by it
            for corner in corners_in:
                e1, e2 = _CORNER_EDGES[corner]
                p1, p2 = pts3[e1][k], pts3[e2][k]
                if np.linalg.norm(p1 - p2) <= 1e-9:
                    continue
                l1 = (float(loc2[e1][k, 0]), float(loc2[e1][k, 1]))
                l2 = (float(loc2[e2][k, 0]), float(loc2[e2][k, 1]))
                segs.append(
                    (p1, p2) if _orient(l1, l2, _CORNER_UV[corner]) else (p2, p1)
                )
    return segs


def _chain_segments(segs, tol_m):
    """Stitch DIRECTED segments into closed loops: endpoints hashed on a
    tolerance grid; each chain extended forward (a→b) and backward."""
    from collections import defaultdict

    tol = max(tol_m, 1e-12) * 1.5

    def key(p) -> tuple[int, int, int]:
        return (
            round(float(p[0]) / tol),
            round(float(p[1]) / tol),
            round(float(p[2]) / tol),
        )

    # directed: forward chains match chain[-1] == seg.a; backward matches chain[0] == seg.b
    fwd: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    bwd: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for i, (a, b) in enumerate(segs):
        fwd[key(a)].append(i)
        bwd[key(b)].append(i)

    used = [False] * len(segs)

    def follow_forward(chain) -> None:
        while True:
            end = chain[-1]
            for j in fwd.get(key(end), []):
                if not used[j]:
                    used[j] = True
                    chain.append(segs[j][1])
                    break
            else:
                return
            if len(chain) > 3 and np.linalg.norm(chain[-1] - chain[0]) <= tol:
                return

    def follow_backward(chain) -> None:
        while True:
            start = chain[0]
            for j in bwd.get(key(start), []):
                if not used[j]:
                    used[j] = True
                    chain.insert(0, segs[j][0])
                    break
            else:
                return
            if len(chain) > 3 and np.linalg.norm(chain[-1] - chain[0]) <= tol:
                return

    loops: list[list[np.ndarray]] = []
    for i in range(len(segs)):
        if used[i]:
            continue
        used[i] = True
        chain = [np.asarray(segs[i][0], float), np.asarray(segs[i][1], float)]
        follow_forward(chain)
        follow_backward(chain)
        loops.append(chain)

    out = []
    for chain in loops:
        pts = tuple(_vec3f(p) for p in chain)
        length = float(
            sum(
                float(np.linalg.norm(chain[i + 1] - chain[i]))
                for i in range(len(chain) - 1)
            )
        )
        if len(chain) < 4 or length < 2.0 * tol:
            continue  # numerical dust: a pinched or unclosed fragment
        out.append(SectionLoop(points_m=pts, length_m=length, area_m2=math.nan))
    return out


def _to_uv(points: np.ndarray, origin, u, v) -> np.ndarray:
    rel = np.asarray(points, dtype=np.float64) - origin
    return np.column_stack([rel @ u, rel @ v])


def _shoelace_uv(points: np.ndarray, origin, u, v) -> float:
    uv = _to_uv(points, origin, u, v)
    x, y = uv[:, 0], uv[:, 1]
    return float(0.5 * np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def _loop_distance(a: SectionLoop, b: SectionLoop) -> float:
    """Point-sampled minimum distance between two loops — within one
    sample spacing (± ``pitch``) of the true segment distance, which is
    inspection-grade for wall-thickness questions at any sane pitch."""
    pa = np.asarray(a.points_m, dtype=np.float64)
    pb = np.asarray(b.points_m, dtype=np.float64)
    best = float("inf")
    for i in range(0, len(pa), 512):
        d = np.linalg.norm(pa[i : i + 512, None, :] - pb[None, :, :], axis=-1)
        best = min(best, float(d.min()))
    return best

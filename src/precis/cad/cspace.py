"""One-dimensional collision windows, with scale-relative contact resolution.

The gap oracle is injected: the walk never imports handlers or a store.
Disjoint material AABBs give a conservative separation without a narrow
phase call. Persistent contact uses a bounded sample probe, not a
certificate. Otherwise a resolution-sized floor prevents asymptotic
stalling; features below that stated resolution are not certified.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from precis.cad.bulk import (
    _expr_bound_covers_material,
    _expr_leaf_iids,
    expr_aabb,
    expr_aabb_disjoint,
)
from precis.cad.graph import Design
from precis.cad.primitives import Field
from precis.cad.scene import SceneSpec, couples_of, joints_of, mates_of, ports_of
from precis.cad.vec import Transform, Vec3, aabb_corners

Box = tuple[Vec3, Vec3]

CONTACT_SAMPLES = 64
AABB_PROBE_LIMIT = 20000


def has_sampled_field(design: Design, component: str) -> bool:
    """Field values need their own gradient-aware gap-speed bound.

    ``exact=False`` permits arbitrary slopes, and even an exact sampled
    distance's trilinear interpolant need not be 1-Lipschitz. The analytic
    sweep must refuse these pairs rather than step by a false distance.
    Include cutters: their field contributes to the folded gap too.
    """
    return any(
        isinstance(design.instances[iid].placed.prim, Field)
        for iid in _expr_leaf_iids(design.components[component])
    )


class SweepLimit(ValueError):
    """The budget ran out before the whole range could be classified."""


class AABBProbeLimit(SweepLimit):
    """The fixed cheap-probe cap ran out; increasing the exact cap cannot help."""


@dataclass(frozen=True)
class SweepResult:
    windows: tuple[tuple[float, float], ...]
    contact_throughout: bool
    evaluations: int
    exact_evaluations: int
    tolerance: float
    sampled_contact: bool = False


def collision_windows(
    build: Callable[[float], Design],
    pair: tuple[str, str],
    limits: tuple[float, float],
    *,
    speed: float,
    resolution: float,
    max_evaluations: int,
    gap: Callable[[Design, str, str], float],
) -> SweepResult:
    """Walk a pair's signed gap; bisect sign changes to ``resolution/speed``.

    ``speed`` bounds change of the *oracle's signed gap* per unit q, not
    just one body's velocity (sum both bodies; the min-max SDF oracle
    needs a further factor two). Negative runs only become interference
    windows if at least one witness penetrates deeper than resolution.
    Only exact gap calls, including edge refinement, spend the pair's
    ``max_evaluations`` budget. Cheap AABB-only probes have a separate
    fixed cap. Either exhaustion raises, never returns partial windows.
    An initially contacting pair gets 64 evenly spaced probes. If all
    remain in band, return sampled contact without certifying the gaps
    between probes; otherwise resume advancement with cached readings.
    """
    lo, hi = limits
    if (
        not all(math.isfinite(x) for x in (lo, hi, speed, resolution))
        or lo > hi
        or speed < 0
        or resolution <= 0
        or max_evaluations < 1
    ):
        raise ValueError(
            "sweep requires finite ordered limits, speed >= 0 and positive resolution/budget"
        )
    tolerance = resolution / speed if speed else hi - lo
    evaluations = exact = 0
    contact = True
    readings: dict[float, float] = {}

    def measure(q: float) -> float:
        nonlocal evaluations, exact, contact
        if q in readings:
            return readings[q]
        design = build(q)
        a, b = pair
        ea, eb = design.components[a], design.components[b]
        if expr_aabb_disjoint(design, ea, eb):
            if evaluations - exact >= AABB_PROBE_LIMIT:
                raise AABBProbeLimit(
                    f"{a} × {b} exhausted {AABB_PROBE_LIMIT} cheap AABB-only probes"
                )
            al, ah = expr_aabb(design, ea)
            bl, bh = expr_aabb(design, eb)
            value = float(np.linalg.norm(np.maximum(0.0, np.maximum(al - bh, bl - ah))))
            # A lower bound inside the contact band cannot establish
            # actual contact. Only a narrow-phase measurement can.
            contact = False
        else:
            if exact >= max_evaluations:
                raise SweepLimit(
                    f"{a} × {b} exhausted {max_evaluations} exact gap calls"
                )
            exact += 1
            value = gap(design, a, b)
            contact = contact and abs(value) <= resolution
        evaluations += 1
        if not math.isfinite(value):
            raise ValueError(f"non-finite gap for {a} × {b}")
        readings[q] = value
        return value

    def edge(left: float, right: float, left_gap: float) -> float:
        while right - left > tolerance:
            mid = (left + right) / 2.0
            if mid in (left, right):
                break
            if (measure(mid) < 0) == (left_gap < 0):
                left = mid
            else:
                right = mid
        return (left + right) / 2.0

    q, value = lo, measure(lo)
    if speed == 0 or lo == hi:
        windows = ((lo, hi),) if value < -resolution else ()
        return SweepResult(windows, contact, evaluations, exact, tolerance)
    if contact:
        for sample in np.linspace(lo, hi, CONTACT_SAMPLES)[1:]:
            measure(float(sample))
            if not contact:
                break
        if contact:
            return SweepResult((), True, evaluations, exact, tolerance, True)
    start = lo if value < 0 else None
    deep = value < -resolution
    windows_list: list[tuple[float, float]] = []
    while q < hi:
        step = max(abs(value), resolution) / speed
        following = min(hi, q + step)
        if following <= q:
            raise ValueError("sweep resolution is below floating-point joint precision")
        after = measure(following)
        if (after < 0) != (value < 0):
            boundary = edge(q, following, value)
            if after < 0:
                start, deep = boundary, after < -resolution
            else:
                if deep and start is not None:
                    windows_list.append((start, boundary))
                start, deep = None, False
        else:
            deep = deep or after < -resolution
        q, value = following, after
    if start is not None and deep:
        windows_list.append((start, hi))
    return SweepResult(tuple(windows_list), contact, evaluations, exact, tolerance)


@dataclass(frozen=True)
class MotionBound:
    """Rigid-group membership, point speed and enclosing travel box."""

    group: tuple[str, ...]
    speed: float
    envelope: Box


def _radius(box: Box, frame: Transform) -> float:
    points = np.asarray(aabb_corners(*box)) - frame.t
    axis = frame.R[:, 2]
    radial = points - np.outer(points @ axis, axis)
    return float(np.max(np.linalg.norm(radial, axis=1)))


def _travel_box(
    box: Box, frame: Transform, kind: str, delta: float, pitch: float
) -> Box:
    """Enclose every corner's orbit (including angular extrema), not samples."""
    axis = frame.R[:, 2]
    if kind == "prismatic":
        shift = axis * delta
        return box[0] + np.minimum(shift, 0), box[1] + np.maximum(shift, 0)
    low, high = sorted((0.0, delta))
    minima, maxima = [], []
    for point in aabb_corners(*box):
        relative = np.asarray(point) - frame.t
        centre = frame.t + axis * float(relative @ axis)
        radial = relative - axis * float(relative @ axis)
        tangent = np.cross(axis, radial)
        lower, upper = [], []
        for k in range(3):
            amp = math.hypot(radial[k], tangent[k])
            if high - low >= 2 * math.pi:
                lower.append(centre[k] - amp)
                upper.append(centre[k] + amp)
                continue
            values = [
                centre[k] + radial[k] * math.cos(q) + tangent[k] * math.sin(q)
                for q in (low, high)
            ]
            phase = math.atan2(tangent[k], radial[k])
            for root in (phase, phase + math.pi):
                q = root + 2 * math.pi * math.ceil((low - root) / (2 * math.pi))
                if q <= high:
                    values.append(
                        centre[k] + radial[k] * math.cos(q) + tangent[k] * math.sin(q)
                    )
            lower.append(min(values))
            upper.append(max(values))
        minima.append(lower)
        maxima.append(upper)
    shift = axis * delta * pitch / (2 * math.pi) if kind == "screw" else np.zeros(3)
    return (
        np.min(minima, axis=0) + np.minimum(shift, 0),
        np.max(maxima, axis=0) + np.maximum(shift, 0),
    )


def motion_bounds(
    spec: SceneSpec,
    design: Design,
    frames: dict[str, Transform],
    driver: str,
    travel: float,
) -> dict[str, MotionBound]:
    """Bound all descendants of a driver and its gear/belt chain.

    Frames and geometry are at the range's first state. A descendant's
    entire relative travel is enclosed before measuring the ancestor's
    radius, so a driven joint on a moving anchor cannot invalidate the
    bound. Fixed mates and other joints held neutral inherit their
    parent's motion; coupled groups remain distinct collision candidates.
    """
    joints = {j.component: (j.kind, j.pitch) for j in joints_of(spec)}
    parents: dict[str, str | None] = {}
    ports = {p.name: p for p in ports_of(spec)}
    for mate in mates_of(spec):
        parents[mate.instance] = (
            mate.anchor_instance or ports[mate.anchor_port].component or None
        )
        if mate.kind != "fixed":
            joints[mate.instance] = (mate.kind, mate.pitch)
    ratios = {driver: 1.0}
    couples = couples_of(spec)
    for _ in couples:
        for couple in couples:
            if couple.drive in ratios:
                ratios[couple.driven] = ratios[couple.drive] * couple.ratio
    owners = set(joints) | set(parents)
    result: dict[str, MotionBound] = {}
    for comp, expr in design.components.items():
        owner = next(
            (
                name
                for name in sorted(owners, key=len, reverse=True)
                if comp == name or comp.startswith(name + ".")
            ),
            None,
        )
        ancestry = []
        while owner is not None:
            if owner in ratios and ratios[owner] != 0:
                ancestry.append(owner)
            owner = parents.get(owner)
        try:
            box = expr_aabb(design, expr)
            if not _expr_bound_covers_material(design, expr):
                raise ValueError("AABB does not bound all material")
        except ValueError as exc:
            raise ValueError(
                f"component {comp!r} has no finite motion bound: {exc}"
            ) from exc
        speed = 0.0
        # ancestry is leaf first: accumulate the descendant's orbit before
        # bounding a parent's lever arm and enclosing its orbit in turn.
        for name in ancestry:
            kind, pitch = joints[name]
            ratio, frame = ratios[name], frames[name]
            speed += abs(ratio) * (
                1.0
                if kind == "prismatic"
                else math.hypot(
                    _radius(box, frame),
                    pitch / (2 * math.pi) if kind == "screw" else 0.0,
                )
            )
            box = _travel_box(box, frame, kind, travel * ratio, pitch)
        result[comp] = MotionBound(tuple(reversed(ancestry)), speed, box)
    return result

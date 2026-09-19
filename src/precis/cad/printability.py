"""Cad-level print orientation search + process DRC findings.

Mesh-in, findings-out, **se-free** (``docs/backlog/se-print-implementer.md``
Engine 2 — this module absorbs ``cad-printability-probe.md``): given an
already-tessellated world-space triangle mesh (verts/tris as
:func:`precis.cad.tessellate.node_meshes`/export's mesh backends hand
them, in the mesh's own units — metres, when the mesh came from the cad
kernel), search over candidate build-down directions and score each one
against a caller-assembled ``rules`` dict (per-material process
thresholds, e.g. ``se_capabilities.json`` figures resolved through
``capabilities.resolve()``) and ``policy`` dict (the family-level
orientation weights, e.g. ``capabilities.orientation_policy()``). Neither
dict is looked up here — only read — so both se's implementer and the cad
kind's ``view='printability'`` (:mod:`precis.handlers.cad`) share this one
function with no cross-kind import.

Units: every length in ``mesh``/``rules``/``loads`` must already be in the
**same units** the mesh itself is expressed in (metres, for anything that
came out of the cad kernel) — this module does no unit conversion of its
own. A caller holding a capability row's native millimetre figures
converts once with :func:`rules_mm_to_m` rather than scattering ``/
1000`` calls at every call site.

Honesty rule: a ``rules`` field a term needs is never defaulted — a
missing field means that term is *skipped* (listed in
:attr:`Score.skipped`, contributing nothing to the total) rather than
scored against a made-up number. ``policy`` is the opposite case: it is
the one thing this module DOES require in full (the weights are a
judgment call the caller must make explicitly), so a missing weight or
``sweep_deg`` raises :class:`ValueError` naming what's absent.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad.tessellate import Mesh
from precis.cad.vec import Vec3, as_vec3, normalize

# ---------------------------------------------------------------------------
# small vector helpers (kept local — printability.py stays self-contained,
# no reach into tessellate.py's private NodeSpec-shaped helpers)
# ---------------------------------------------------------------------------


def _tangent_basis(w: Vec3) -> tuple[Vec3, Vec3]:
    """Unit ``(u, v)`` such that ``(u, v, w)`` is right-handed orthonormal."""
    ref = (
        np.array([0.0, 0.0, 1.0])
        if abs(float(w[2])) < 0.9
        else np.array([1.0, 0.0, 0.0])
    )
    u = np.cross(ref, w)
    u = u / np.linalg.norm(u)
    v = np.cross(w, u)
    return u, v


def _rotation_matrix(axis: Vec3, theta: float) -> NDArray[np.float64]:
    """Rodrigues' rotation matrix for ``theta`` radians about unit ``axis``."""
    x, y, z = axis / np.linalg.norm(axis)
    c, s = math.cos(theta), math.sin(theta)
    cc = 1.0 - c
    return np.array(
        [
            [x * x * cc + c, x * y * cc - z * s, x * z * cc + y * s],
            [y * x * cc + z * s, y * y * cc + c, y * z * cc - x * s],
            [z * x * cc - y * s, z * y * cc + x * s, z * z * cc + c],
        ]
    )


def _rotate_about(v: Vec3, axis: Vec3, theta: float) -> Vec3:
    """``v`` rotated by ``theta`` radians about unit ``axis`` (Rodrigues')."""
    a = axis / np.linalg.norm(axis)
    return (
        v * math.cos(theta)
        + np.cross(a, v) * math.sin(theta)
        + a * float(np.dot(a, v)) * (1.0 - math.cos(theta))
    )


def _down_rotation(down: Vec3) -> NDArray[np.float64]:
    """The rotation matrix mapping unit ``down`` onto ``-z``."""
    d = normalize(down)
    target = np.array([0.0, 0.0, -1.0])
    c = float(np.dot(d, target))
    if c > 1.0 - 1e-9:  # already -z
        return np.eye(3)
    if c < -1.0 + 1e-9:  # exactly +z — any perpendicular axis, 180 degrees
        u, _ = _tangent_basis(d)
        return _rotation_matrix(u, math.pi)
    axis = np.cross(d, target)
    axis = axis / np.linalg.norm(axis)
    theta = math.acos(max(-1.0, min(1.0, c)))
    return _rotation_matrix(axis, theta)


def rotate_to_frame(verts: NDArray[np.float64], down: Vec3) -> NDArray[np.float64]:
    """Rotate ``verts`` so ``down`` maps to ``-z``, then translate so the
    lowest point sits at ``z = 0`` — the build-plate convention se's export
    reuses (rotated so build-down is ``-z``, bed at ``z = 0``)."""
    r = _down_rotation(as_vec3(down))
    rotated = verts @ r.T
    rotated = rotated.copy()
    rotated[:, 2] -= rotated[:, 2].min()
    return rotated


def rotate_all_to_frame(
    meshes: Sequence[NDArray[np.float64]], down: Vec3
) -> list[NDArray[np.float64]]:
    """:func:`rotate_to_frame` for several bodies that print TOGETHER: one
    rotation, and one shared translation so the lowest point of the
    **union** sits at ``z = 0`` — each body keeps its place relative to
    the others (a print-in-place group, se's print groups). Rotating each
    body on its own would drop every one of them onto the bed."""
    r = _down_rotation(as_vec3(down))
    rotated = [np.asarray(v, dtype=np.float64) @ r.T for v in meshes]
    floor = min((float(v[:, 2].min()) for v in rotated if len(v)), default=0.0)
    out = []
    for v in rotated:
        v = v.copy()
        v[:, 2] -= floor
        out.append(v)
    return out


def _face_normals_areas(
    verts: NDArray[np.float64], tris: NDArray[np.int64]
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    a = verts[tris[:, 0]]
    b = verts[tris[:, 1]]
    c = verts[tris[:, 2]]
    cross = np.cross(b - a, c - a)
    areas = 0.5 * np.linalg.norm(cross, axis=1)
    normals = np.divide(
        cross,
        (2.0 * areas)[:, None],
        out=np.zeros_like(cross),
        where=(areas > 0)[:, None],
    )
    return normals, areas


def _bed_contact_ratio(
    areas: NDArray[np.float64],
    bed_mask: NDArray[np.bool_],
    lo: NDArray[np.float64],
    hi: NDArray[np.float64],
) -> float:
    """Bed-contact area over the v1 footprint proxy (the xy-AABB of the
    *whole* rotated mesh — cheap, exact for an axis-aligned orientation,
    but a tilted orientation's true footprint can exceed its own AABB
    (imagine a diagonal slice), which would otherwise read as a
    physically-impossible >100% contact fraction; clamped to 1.0 since
    contact can never exceed its own footprint by definition."""
    footprint = float((hi[0] - lo[0]) * (hi[1] - lo[1]))
    if footprint <= 0:
        return 0.0
    contact_area = float(areas[bed_mask].sum())
    return min(1.0, contact_area / footprint)


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------

#: The 6 axis-aligned "down" directions, in the deterministic order every
#: sweep/dedup pass below iterates them.
_BASE_AXES: tuple[tuple[float, float, float], ...] = (
    (1.0, 0.0, 0.0),
    (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, -1.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.0, 0.0, -1.0),
)

#: Unit-vector dedup tolerance (candidates() dedups on this).
_DEDUP_TOL = 1e-6


def candidates(sweep_deg: float) -> list[Vec3]:
    """The 6 axis-aligned "down" directions, plus (when ``sweep_deg`` is
    truthy) rotations of each about its two perpendicular axes at every
    non-zero multiple of ``sweep_deg`` up to 360°, deduplicated (unit
    vectors, 1e-6 tolerance). Deterministic order: the 6 base axes first
    (in the order above), then per axis, its two perpendicular axes (u
    then v) in ascending angle — so a re-read agrees candidate-for-
    candidate, not just set-for-set.

    ``sweep_deg <= 0`` (falsy) returns just the 6 axis-aligned directions —
    the sweep is a rules-dict knob (`` policy['sweep_deg']``), not a fixed
    behaviour of this function.
    """
    out: list[Vec3] = []

    def add(v: Vec3) -> None:
        for existing in out:
            if float(np.linalg.norm(existing - v)) < _DEDUP_TOL:
                return
        out.append(v)

    axes = [as_vec3(a) for a in _BASE_AXES]
    for axis in axes:
        add(axis)
    if sweep_deg and sweep_deg > 0:
        steps = round(360.0 / sweep_deg)
        for axis in axes:
            u, v = _tangent_basis(axis)
            for rot_axis in (u, v):
                for k in range(1, steps):
                    theta = math.radians(sweep_deg * k)
                    add(normalize(_rotate_about(axis, rot_axis, theta)))
    return out


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

#: The five score terms `policy['weights']` must all carry — orient()/
#: score() refuse (naming what's missing) rather than defaulting any of
#: them, since these are a judgment call, not a measured figure.
_WEIGHT_TERMS = ("overhang", "bed_contact", "height", "bridges", "load_vs_layer")


def _require_weights(policy: dict[str, Any]) -> dict[str, float]:
    weights = policy.get("weights")
    if weights is None:
        raise ValueError("policy is missing 'weights'")
    missing = [k for k in _WEIGHT_TERMS if k not in weights]
    if missing:
        raise ValueError(f"policy['weights'] is missing: {', '.join(missing)}")
    return {k: float(weights[k]) for k in _WEIGHT_TERMS}


@dataclass(frozen=True)
class _BridgePatch:
    span: float  # metres — the patch's shortest in-plane AABB side
    triangle_indices: tuple[int, ...]


def _adjacency(tris: NDArray[np.int64]) -> list[list[int]]:
    """Triangle index -> its edge-adjacent triangle indices (shares 2
    vertices, i.e. an edge — an interior edge of a watertight mesh is
    shared by exactly 2 triangles)."""
    edge_map: dict[tuple[int, int], list[int]] = {}
    for i, tri in enumerate(tris):
        a, b, c = (int(tri[0]), int(tri[1]), int(tri[2]))
        for e in ((a, b), (b, c), (c, a)):
            edge_map.setdefault((min(e), max(e)), []).append(i)
    adj: list[list[int]] = [[] for _ in range(len(tris))]
    for idxs in edge_map.values():
        if len(idxs) == 2:
            i, j = idxs
            adj[i].append(j)
            adj[j].append(i)
    return adj


def _bridge_patches(
    rverts: NDArray[np.float64],
    tris: NDArray[np.int64],
    normals: NDArray[np.float64],
    mask: NDArray[np.bool_],
) -> list[_BridgePatch]:
    """Group the ``mask``-selected (down-facing, off-bed) triangles into
    coplanar, edge-connected patches — v1 groups by plane only (each
    patch's plane is fixed at its seed triangle, never re-fit as the patch
    grows), so a curved ceiling's normal keeps drifting off the seed plane
    and never joins one big patch — it reads as (small, isolated)
    overhang triangles instead, the documented conservative reading."""
    idxs = np.nonzero(mask)[0]
    if idxs.size == 0:
        return []
    diag = float(np.linalg.norm(rverts.max(axis=0) - rverts.min(axis=0))) or 1.0
    angle_cos_tol = math.cos(math.radians(1.0))
    offset_tol = 1e-6 * diag
    adj = _adjacency(tris)
    candidate_set = set(int(i) for i in idxs)
    visited: set[int] = set()
    anchor = rverts[tris[:, 0]]  # one point per triangle, on its own plane
    patches: list[_BridgePatch] = []
    for seed in idxs.tolist():
        seed = int(seed)
        if seed in visited:
            continue
        seed_n = normals[seed]
        seed_offset = float(np.dot(seed_n, anchor[seed]))
        group = [seed]
        visited.add(seed)
        frontier = [seed]
        while frontier:
            t = frontier.pop()
            for nb in adj[t]:
                if nb in visited or nb not in candidate_set:
                    continue
                if float(np.dot(normals[nb], seed_n)) < angle_cos_tol:
                    continue
                if abs(float(np.dot(seed_n, anchor[nb])) - seed_offset) > offset_tol:
                    continue
                visited.add(nb)
                group.append(nb)
                frontier.append(nb)
        u, v = _tangent_basis(seed_n)
        verts_idx = np.unique(tris[group].reshape(-1))
        pts = rverts[verts_idx]
        us, vs = pts @ u, pts @ v
        span = float(min(float(us.max() - us.min()), float(vs.max() - vs.min())))
        patches.append(_BridgePatch(span=span, triangle_indices=tuple(group)))
    return patches


@dataclass(frozen=True)
class Score:
    """One candidate's raw process terms plus the weighted ``total`` used
    for ranking (lower = better — a cost, not a score-to-maximize: every
    term but ``bed_contact_ratio`` is a penalty, ``bed_contact`` is
    subtracted since more contact is better). A term whose ``rules`` field
    is missing is ``None`` here (never a fabricated number) and named in
    :attr:`skipped`; ``height``/``load_vs_layer`` always have a value
    (height comes from the mesh alone; load_vs_layer is 0 with no loads)."""

    total: float
    overhang_area: float | None
    bed_contact_ratio: float | None
    height: float
    bridge_count: int | None
    load_vs_layer: float
    skipped: tuple[str, ...]

    def terms(self) -> dict[str, float]:
        """The available (non-skipped) raw terms as a plain dict, for a
        candidate table or :class:`BuildCandidate`."""
        out: dict[str, float] = {
            "height": self.height,
            "load_vs_layer": self.load_vs_layer,
        }
        if self.overhang_area is not None:
            out["overhang_area"] = self.overhang_area
        if self.bed_contact_ratio is not None:
            out["bed_contact_ratio"] = self.bed_contact_ratio
        if self.bridge_count is not None:
            out["bridge_count"] = float(self.bridge_count)
        return out


def score(
    mesh: Mesh,
    down: Vec3,
    rules: dict[str, Any],
    policy: dict[str, Any],
    loads: Sequence[Vec3] = (),
) -> Score:
    """Score one candidate ``down`` direction against ``rules``/``policy``.

    All terms are measured on ``mesh`` rotated so ``down`` is ``-z`` (bed =
    the rotated min-z plane). Normalisation before weighting: overhang is
    a fraction of the mesh's own total surface area, height a fraction of
    the mesh's bbox diagonal, bed_contact and bridges use their own
    natural units (a ratio, a count) directly — see the module docstring
    for the "never a default" honesty rule on missing ``rules`` fields.
    """
    weights = _require_weights(policy)
    verts, tris = mesh
    if len(tris) == 0:
        raise ValueError("mesh has no triangles to score")
    rverts = rotate_to_frame(verts, down)
    normals, areas = _face_normals_areas(rverts, tris)
    total_area = float(areas.sum())
    lo, hi = rverts.min(axis=0), rverts.max(axis=0)
    height = float(hi[2] - lo[2])
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    down_facing = normals[:, 2] < -1e-9

    skipped: list[str] = []
    weighted = 0.0

    layer_height = rules.get("layer_height")
    if layer_height is not None:
        tri_min_z = rverts[tris][:, :, 2].min(axis=1)
        bed_mask = down_facing & (tri_min_z <= lo[2] + layer_height + 1e-12)
    else:
        # No bed-contact threshold to test against — every down-facing
        # triangle is conservatively treated as NOT bed contact (never
        # borrowed from a sibling field); overhang below then reads as
        # slightly pessimistic near the bed rather than silently exact.
        bed_mask = np.zeros(len(tris), dtype=bool)

    overhang_area: float | None = None
    max_overhang = rules.get("max_overhang")
    if max_overhang is not None:
        angle = np.degrees(np.arcsin(np.clip(np.abs(normals[:, 2]), -1.0, 1.0)))
        mask = down_facing & (angle > max_overhang) & (~bed_mask)
        overhang_area = float(areas[mask].sum())
        weighted += weights["overhang"] * (
            overhang_area / total_area if total_area > 0 else 0.0
        )
    else:
        skipped.append("overhang")

    bed_contact_ratio: float | None = None
    if layer_height is not None:
        bed_contact_ratio = _bed_contact_ratio(areas, bed_mask, lo, hi)
        weighted -= weights["bed_contact"] * bed_contact_ratio
    else:
        skipped.append("bed_contact")

    weighted += weights["height"] * (height / diag)

    bridge_count: int | None = None
    max_bridge = rules.get("max_bridge")
    if max_bridge is not None and layer_height is not None:
        mask = down_facing & (~bed_mask)
        patches = _bridge_patches(rverts, tris, normals, mask)
        bridge_count = sum(1 for p in patches if p.span > max_bridge)
        weighted += weights["bridges"] * bridge_count
    else:
        # bridges needs BOTH a threshold (max_bridge) and a way to tell a
        # bridge patch apart from bed contact (layer_height) — either
        # missing means "can't tell", not "assume zero bridges".
        skipped.append("bridges")

    load_vs_layer = 0.0
    if loads:
        r = _down_rotation(as_vec3(down))
        for ld in loads:
            ldv = as_vec3(ld)
            n = float(np.linalg.norm(ldv))
            if n <= 0:
                continue
            ldr = r @ ldv
            load_vs_layer += abs(float(ldr[2])) / n
    weighted += weights["load_vs_layer"] * load_vs_layer

    return Score(
        total=weighted,
        overhang_area=overhang_area,
        bed_contact_ratio=bed_contact_ratio,
        height=height,
        bridge_count=bridge_count,
        load_vs_layer=load_vs_layer,
        skipped=tuple(skipped),
    )


@dataclass(frozen=True)
class BuildCandidate:
    """One orientation candidate — carries the direction, the ranking
    score (a cost: lower is better), the per-term raw dict, and which
    terms were skipped for lack of a rules field."""

    down: Vec3
    score: float
    terms: dict[str, float]
    skipped: tuple[str, ...]
    index: int


def orient(
    mesh: Mesh,
    rules: dict[str, Any],
    policy: dict[str, Any],
    loads: Sequence[Vec3] = (),
) -> list[BuildCandidate]:
    """Search every candidate build-down direction, score each, and return
    them **best-first** (lowest cost). Ties break by lowest ``height``,
    then lowest candidate index — deterministic, so a re-read agrees with
    a previously stored build frame.

    ``policy`` must carry ``sweep_deg`` (:func:`candidates`'s knob) and a
    complete ``weights`` dict (:func:`score`'s requirement) — refuses by
    name otherwise, same as ``score()``, so the very first candidate fails
    fast instead of after 100+ silent scores.
    """
    if "sweep_deg" not in policy:
        raise ValueError("policy is missing 'sweep_deg'")
    _require_weights(policy)
    cands = candidates(float(policy["sweep_deg"]))
    scored: list[BuildCandidate] = []
    for i, d in enumerate(cands):
        s = score(mesh, d, rules, policy, loads)
        scored.append(
            BuildCandidate(
                down=d, score=s.total, terms=s.terms(), skipped=s.skipped, index=i
            )
        )
    scored.sort(key=lambda c: (c.score, c.terms.get("height", 0.0), c.index))
    return scored


# ---------------------------------------------------------------------------
# process findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrintFinding:
    """One process-DRC finding — se maps this onto its own
    ``precis_se.validate.ValidationIssue`` (which carries the same
    ``measured``/``expected``/``suggested_fix`` fields, added for this
    engine); this module never imports se."""

    rule: str
    measured: str
    expected: str
    severity: str
    suggested_fix: str
    detail: str


def process_findings(
    mesh: Mesh,
    down: Vec3,
    rules: dict[str, Any],
    *,
    best_other: str | None = None,
) -> list[PrintFinding]:
    """Process-DRC findings for the mesh in the given build ``down`` frame.

    Only the rules whose fields are present are checked (a missing field
    means that rule is silently not run — never a default threshold): so
    with an empty ``rules`` this returns ``[]``, never invented findings.
    ``best_other`` is an already-formatted description (e.g. a better
    candidate's ``down``) folded into the ``overhang`` finding's
    ``suggested_fix`` when the caller has one; otherwise a generic fix is
    named.
    """
    verts, tris = mesh
    if len(tris) == 0:
        return []
    rverts = rotate_to_frame(verts, down)
    normals, areas = _face_normals_areas(rverts, tris)
    lo, hi = rverts.min(axis=0), rverts.max(axis=0)
    down_facing = normals[:, 2] < -1e-9

    layer_height = rules.get("layer_height")
    if layer_height is not None:
        tri_min_z = rverts[tris][:, :, 2].min(axis=1)
        bed_mask = down_facing & (tri_min_z <= lo[2] + layer_height + 1e-12)
    else:
        bed_mask = np.zeros(len(tris), dtype=bool)

    findings: list[PrintFinding] = []

    max_overhang = rules.get("max_overhang")
    if max_overhang is not None:
        angle = np.degrees(np.arcsin(np.clip(np.abs(normals[:, 2]), -1.0, 1.0)))
        mask = down_facing & (angle > max_overhang) & (~bed_mask)
        if np.any(mask):
            worst = float(angle[mask].max())
            area = float(areas[mask].sum())
            fix = (
                f"reorient to {best_other}"
                if best_other
                else "reorient / chamfer under the face / expect supports"
            )
            findings.append(
                PrintFinding(
                    rule="overhang",
                    measured=f"{worst:.1f}° worst face angle, {area:.6g} m² violating area",
                    expected=f"<= {max_overhang:g}°",
                    severity="warn",
                    suggested_fix=fix,
                    detail=(
                        "down-facing triangles whose angle from vertical "
                        "exceeds max_overhang, excluding bed contact"
                    ),
                )
            )

    max_bridge = rules.get("max_bridge")
    if max_bridge is not None:
        mask = down_facing & (~bed_mask)
        patches = _bridge_patches(rverts, tris, normals, mask)
        violating = [p for p in patches if p.span > max_bridge]
        if violating:
            longest = max(p.span for p in violating)
            findings.append(
                PrintFinding(
                    rule="bridge",
                    measured=f"{longest:.6g} m longest unsupported span",
                    expected=f"<= {max_bridge:g} m",
                    severity="warn",
                    suggested_fix="split the span",
                    detail=(
                        f"{len(violating)} coplanar down-facing patch(es) above "
                        "the bed exceed max_bridge (v1: plane-grouped — a "
                        "curved ceiling reads as overhang instead)"
                    ),
                )
            )

    min_bed_contact = rules.get("min_bed_contact")
    if min_bed_contact is not None and layer_height is not None:
        ratio = _bed_contact_ratio(areas, bed_mask, lo, hi)
        if ratio < min_bed_contact:
            findings.append(
                PrintFinding(
                    rule="bed_contact",
                    measured=f"{ratio:.3f}",
                    expected=f">= {min_bed_contact:g}",
                    severity="warn",
                    suggested_fix="reorient / brim",
                    detail="bed-contact area ÷ xy-AABB footprint (v1 footprint proxy)",
                )
            )

    max_build = rules.get("max_build")
    if max_build is not None:
        extents = (hi - lo).tolist()
        limits = (
            [float(x) for x in max_build]
            if isinstance(max_build, (list, tuple))
            else [float(max_build)] * 3
        )
        if any(e > lim for e, lim in zip(extents, limits, strict=True)):
            findings.append(
                PrintFinding(
                    rule="build_volume",
                    measured=(
                        f"{extents[0]:.6g} x {extents[1]:.6g} x {extents[2]:.6g} m"
                    ),
                    expected=f"<= {limits} m",
                    severity="error",
                    suggested_fix="split the block / reorient diagonally",
                    detail="part extent in the build frame vs the printer's max_build",
                )
            )

    return findings


# ---------------------------------------------------------------------------
# unit conversion helper
# ---------------------------------------------------------------------------

#: rules fields expressed as a length in a capability row's native mm
#: figures — the only ones :func:`rules_mm_to_m` rescales.
_LENGTH_RULE_FIELDS = frozenset(
    {"layer_height", "max_bridge", "max_build", "min_hole", "min_feature", "min_wall"}
)


def rules_mm_to_m(rules: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``rules`` with every length field rescaled mm -> m — for a
    caller holding a capability row's native millimetre figures
    (``se_capabilities.json``) to convert once, here, rather than
    scattering ``/ 1000`` at every call site. Non-length fields
    (``max_overhang`` in degrees, ``min_bed_contact``/``strength_z_ratio``
    ratios) and ``None`` values (an unpublished figure) pass through
    unchanged."""
    out = dict(rules)
    for key in _LENGTH_RULE_FIELDS:
        if key in out and out[key] is not None:
            v = out[key]
            out[key] = (
                [float(x) / 1000.0 for x in v]
                if isinstance(v, (list, tuple))
                else float(v) / 1000.0
            )
    return out

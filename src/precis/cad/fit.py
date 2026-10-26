"""Mesh fit check — the engine for ``docs/backlog/se-fit-check-and-probe.md``.

Checks use real cad geometry (ruling A): each component is tessellated once
(:mod:`precis.cad.tessellate` at a scale-relative chord, Decision 2), its
boolean DAG folded in manifold3d by the same recipe
:mod:`precis.cad.export` uses, and each instance placed by lazy
``Manifold.transform``. Pairs are broad-phased on the placed bounding
boxes, measured with ``Manifold.min_gap`` plus a boolean intersection,
and classified against the connect's mechanism rule (Decision 5). A
result within ``3 × chord`` of a verdict boundary — or an overlap thinner
than ``2 × chord`` — is re-tessellated at ``chord/10`` and recomputed
(Decision 3's referee: mesh refinement, never ``relate.clearance``,
Decision 11).

This module is **pure**: no DB, no ``precis_se`` import — the gap-rule
table, the thread-size table and the labels (block names, made-by lines)
all come in from the caller (Decision 10). ``precis_geom.fit`` /
``precis_se.fit`` are the thin adapters. Field leaves (``rd``, ``blend``,
``field:``/``relief:``) have no analytic mesh: the component is reported
as "not fit-checked" and never fails a run (Decision 12).

Units: everything internal is SI metres — the report prints mm rounded to
1 µm so two runs give byte-identical text.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.cad.dsl import FIELD_ALIAS, RELIEF_ALIAS, ROUND_KEY, format_spec, parse
from precis.cad.scene import NodeSpec, SceneSpec
from precis.cad.tessellate import design_aabb, node_meshes
from precis.cad.vec import Transform

#: Default per-component tessellation chord (Decision 2): the finer of
#: 0.01 mm and 1e-4 × the component's AABB diagonal.
CHORD_MAX_M = 1e-5
CHORD_DIAG_FRACTION = 1e-4

#: Tolerance floor (Decision 5): ``tol = max(0.05 mm, 3×chord, process)`` —
#: the per-process term is a server-side value; offline the floor and the
#: mesh band are what apply.
TOL_FLOOR_M = 5e-5

#: ``min_gap`` search distance: 2 mm on unconnected pairs, 10 mm on
#: connected ones (the pair loop's `search`).
SEARCH_M = 2e-3
SEARCH_CONNECTED_M = 1e-2

#: Contact band (Decision 3): results within ``BAND × chord`` of a verdict
#: boundary, or overlaps thinner than ``BAND_THIN × chord``, go to the
#: chord/10 referee.
BAND = 3.0
BAND_THIN = 2.0
REFINE_DIV = 10.0

#: Report rounding — all printed lengths at 1 µm (mm with 3 decimals).
_ROUND_M = 1e-6

#: Pair-cache key rounding for the relative transform (Decision 4: 1 µm).
_KEY_ROUND = 1e-6


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One gap rule from the Decision-5 table. ``kind`` drives
    classification: ``press`` wants contact-or-overlap, ``contact`` wants
    ``|gap| ≈ 0``, ``slide`` wants a positive gap, ``screw`` is ``contact``
    with a thread-engagement overlap allowance, ``skip`` is "not
    fit-checked", ``clearance`` wants ``gap ≥ expected`` (per-pair
    ``min_clearance``), ``unconnected`` flags any touch/overlap."""

    kind: str
    expected: float = 0.0
    label: str = ""

    @property
    def expected_text(self) -> str:
        if self.kind == "press":
            return "gap ≤ 0 (press)"
        if self.kind == "contact":
            return "gap ≈ 0"
        if self.kind == "slide":
            return "gap > 0"
        if self.kind == "screw":
            return "gap ≈ 0 (thread overlap ok)"
        if self.kind == "clearance":
            return f"gap ≥ {_mm(self.expected)} mm"
        if self.kind == "unconnected":
            return "no connect — expected clear"
        return "not fit-checked"


def default_rules() -> dict[str, Rule]:
    """The Decision-5 table, keyed by mechanism with the class fallbacks.

    Lookup order at classification time is ``joint.mechanism`` then
    ``joint.class``; a connect with neither gets ``rigid`` (a bare connect
    declares contact intent)."""
    return {
        # mechanisms
        "press": Rule("press", 0.0, "press"),
        "bearing": Rule("contact", 0.0, "bearing"),
        "key": Rule("contact", 0.0, "key"),
        "clamp": Rule("contact", 0.0, "clamp"),
        "screw": Rule("screw", 0.0, "screw"),
        "snap": Rule("contact", 0.0, "snap"),
        "integral": Rule("skip", 0.0, "integral"),
        "bond": Rule("skip", 0.0, "bond"),
        "cable": Rule("skip", 0.0, "cable"),
        "tensioner": Rule("skip", 0.0, "tensioner"),
        "magnet": Rule("skip", 0.0, "magnet"),
        # class fallbacks (no mechanism declared)
        "prismatic": Rule("slide", 0.0, "prismatic"),
        "cylindrical": Rule("slide", 0.0, "cylindrical"),
        "revolute": Rule("slide", 0.0, "revolute"),
        "rigid": Rule("contact", 0.0, "rigid"),
    }


#: Unconnected pairs and connects whose key misses the rule table get this.
UNCONNECTED_RULE = Rule("unconnected", 0.0, "unconnected")


# ---------------------------------------------------------------------------
# tessellation
# ---------------------------------------------------------------------------


def _to_manifold(verts: np.ndarray, tris: np.ndarray) -> Any:
    import manifold3d as m3d

    return m3d.Manifold(
        m3d.Mesh(
            vert_properties=np.asarray(verts, dtype=np.float64).astype(np.float32),
            tri_verts=np.asarray(tris).astype(np.uint32),
        )
    )


def mesh_to_manifold(mesh: tuple[np.ndarray, np.ndarray]) -> Any:
    """(verts, tris) → ``Manifold`` — the adapter's cache-rebuild path."""
    return _to_manifold(mesh[0], mesh[1])


def unmeshable_reason(spec: SceneSpec, component: str) -> str | None:
    """Why ``component`` has no analytic mesh, or ``None`` when it folds —
    field leaves (``field:``/``relief:``), ``rd`` rounding and ``blend``
    unions are "not fit-checked" in v1 (Decision 12)."""
    for node in spec.nodes:
        if node.component != component:
            continue
        shape = parse(node.config)
        if node.blend > 0.0:
            return f"blend on node {node.name!r}"
        if shape.alias in (FIELD_ALIAS, RELIEF_ALIAS):
            return f"{shape.alias}: leaf on node {node.name!r}"
        if ROUND_KEY in shape.params:
            return f"rd on node {node.name!r}"
    return None


def component_chord(spec: SceneSpec, component: str) -> float:
    """Decision 2: ``min(0.01 mm, 1e-4 × component AABB diagonal)``.

    The AABB comes from the folded *positive* extent: union of the
    component's node bboxes as placed (cutters can be oversized, so a
    node-local bound is the honest scale measure — computing it from the
    already-tessellated default mesh would need the chord first)."""
    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    for node in spec.nodes:
        if node.component != component or node.op == "cut":
            continue
        nlo, nhi = _node_aabb(node)
        lo = np.minimum(lo, nlo)
        hi = np.maximum(hi, nhi)
    if not np.isfinite(lo).all():
        return CHORD_MAX_M
    return min(CHORD_MAX_M, CHORD_DIAG_FRACTION * float(np.linalg.norm(hi - lo)))


def _node_aabb(node: NodeSpec) -> tuple[np.ndarray, np.ndarray]:
    """Analytic bound on a node's placed extent (patterns included) —
    what :func:`component_chord` sizes itself by without paying for a
    mesh pass."""
    from precis.cad.scene import _node_xform, _pattern_transforms

    shape = parse(node.config)
    p = shape.params
    a = shape.alias
    # local frame: prisms are centred in x/y with the base at z=0
    # (tessellate.py's convention); sphere/torus are centred.
    if a == "box":
        lo, hi = (-p["w"] / 2, -p["d"] / 2, 0.0), (p["w"] / 2, p["d"] / 2, p["h"])
    elif a in ("cyl", "cone", "tcone"):
        rr = max(p.get("r", 0.0), p.get("rb", 0.0), p.get("rt", 0.0))
        lo, hi = (-rr, -rr, 0.0), (rr, rr, p["h"])
    elif a == "sphere":
        lo, hi = (-p["r"], -p["r"], -p["r"]), (p["r"], p["r"], p["r"])
    elif a == "torus":
        rr = p["R"] + p["r"]
        lo, hi = (-rr, -rr, -p["r"]), (rr, rr, p["r"])
    elif a in ("hex", "ngon"):
        lo, hi = (-p["r"], -p["r"], 0.0), (p["r"], p["r"], p["h"])
    else:  # chamfer/frustum/pyramid/…: a coarse safe bound is enough for a chord
        m = max(
            (abs(float(v)) for v in p.values() if isinstance(v, (int, float))),
            default=1.0,
        )
        lo, hi = (-m, -m, -m), (m, m, 2.0 * m)
    corners = np.array(
        [
            [x, y, z]
            for x in (lo[0], hi[0])
            for y in (lo[1], hi[1])
            for z in (lo[2], hi[2])
        ]
    )
    xfs = (
        _pattern_transforms(node)
        if node.pattern is not None
        else [_node_xform(node.loc, node.rot)]
    )
    plo = np.full(3, np.inf)
    phi = np.full(3, -np.inf)
    for xf in xfs:
        pts = corners @ xf.R.T + xf.t
        plo = np.minimum(plo, pts.min(axis=0))
        phi = np.maximum(phi, pts.max(axis=0))
    return plo, phi


def tessellate_component(spec: SceneSpec, component: str, chord: float) -> Any:
    """Fold one component of ``spec`` into a ``manifold3d`` solid at
    ``chord`` (metres), in the spec's local frame.

    Mirrors :func:`precis.cad.export._component_solids`: each node's
    pattern-expanded meshes become one solid, then ``add``/``cut``/
    ``intersect`` fold in node order. ``chamfer`` nodes clamp to the
    design AABB the same way export does. Returns ``None`` when the
    component has no nodes or folds to nothing, and raises
    :class:`FitError` only for a mesh the kernel itself rejects."""
    nodes = [n for n in spec.nodes if n.component == component]
    if not nodes:
        return None
    aabb = (
        design_aabb(spec)
        if any(parse(n.config).alias == "chamfer" for n in spec.nodes)
        else None
    )
    cur: Any | None = None
    for node in nodes:
        parts = [_to_manifold(v, t) for v, t in node_meshes(node, aabb, chord=chord)]
        man = parts[0]
        for p in parts[1:]:
            man = man + p
        if cur is None:
            cur = man
        elif node.op == "add":
            cur = cur + man
        elif node.op == "cut":
            cur = cur - man
        elif node.op == "intersect":
            cur = cur ^ man
    if cur is None or cur.is_empty():
        return None
    return cur


def geometry_hash(spec: SceneSpec, component: str, chord: float) -> str:
    """The tessellation cache key (Decision 4): sha256 of the component's
    canonical node text — ``format_spec`` per node plus its placement
    fields — plus the chord."""
    import hashlib
    import json

    rows: list[str] = []
    for n in spec.nodes:
        if n.component != component:
            continue
        rows.append(
            "|".join(
                [
                    n.name,
                    n.op,
                    format_spec(parse(n.config)),
                    repr(tuple(n.loc)),
                    repr(tuple(n.rot)),
                    json.dumps(n.pattern, sort_keys=True),
                    repr(n.blend),
                ]
            )
        )
    text = "\n".join(rows) + f"|chord={chord!r}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------


def _as_manifold_dict(solids: Any) -> dict[str, Any]:
    if isinstance(solids, Mapping):
        return {str(k): v for k, v in solids.items()}
    return {"part": solids}


@dataclass
class Placed:
    """One placed part for :func:`fit`. ``solids`` maps component name →
    world-space folded ``Manifold``; ``combined`` unions them for pair
    measurements. ``refine`` re-tessellates at a finer chord for the
    Decision-3 referee (``None`` → the band verdict is reported
    unrefined). ``material`` maps component → material slug (the DSL's
    ``material`` lines)."""

    name: str
    solids: dict[str, Any]
    xf: Transform
    chord: float
    base: str | None = None
    parent: str | None = None
    made_by: str | None = None
    refine: Callable[[float], Mapping[str, Any]] | None = None
    material: Mapping[str, str] = field(default_factory=dict)
    note: str | None = None  # set ⇒ not fit-checked, with the reason
    geo_hash: str | None = None
    _combined: Any = field(default=None, repr=False)
    _aabb: tuple[np.ndarray, np.ndarray] | None = field(default=None, repr=False)

    @property
    def block(self) -> str:
        return self.base or self.name

    @property
    def checked(self) -> bool:
        return self.note is None

    @property
    def combined(self) -> Any:
        if self._combined is None:
            cur = next(iter(self.solids.values()))
            for s in list(self.solids.values())[1:]:
                cur = cur + s
            self._combined = cur
        return self._combined

    @property
    def aabb(self) -> tuple[np.ndarray, np.ndarray]:
        if self._aabb is None:
            lo = np.full(3, np.inf)
            hi = np.full(3, -np.inf)
            for s in self.solids.values():
                b = s.bounding_box()
                lo = np.minimum(lo, np.asarray(b[:3], dtype=np.float64))
                hi = np.maximum(hi, np.asarray(b[3:], dtype=np.float64))
            self._aabb = (lo, hi)
        return self._aabb


def _xf_to_m34(xf: Transform) -> list[list[float]]:
    """``precis.cad.vec.Transform`` → manifold3d's 3×4 ``[R | t]``."""
    return [
        [float(xf.R[i, 0]), float(xf.R[i, 1]), float(xf.R[i, 2]), float(xf.t[i])]
        for i in range(3)
    ]


def _rot_axis(axis: np.ndarray, rad: float) -> Transform:
    a = axis / np.linalg.norm(axis)
    ux = np.array([[0.0, -a[2], a[1]], [a[2], 0.0, -a[0]], [-a[1], a[0], 0.0]])
    R = np.eye(3) + math.sin(rad) * ux + (1.0 - math.cos(rad)) * (ux @ ux)
    return Transform(R=R, t=np.zeros(3))


def array_member_transforms(spec: Mapping[str, Any]) -> list[Transform]:
    """Per-member local transforms of a canonical array spec
    (``{'kind': 'linear'|'polar', 'count', …}`` — the shape
    ``precis_geom.assembly.array_spec`` normalises, and the manifest
    ``linear``/``polar`` keys carry). The step is in the instance's own
    frame; the instance pose applies outside. Polar mirrors the cad
    ``polar:nNrR`` convention: member k rotated ``2πk/count`` about
    ``axis`` then offset ``radius`` along a fixed perpendicular (axis=+z
    → +x). Shared by the ``precis_geom.fit`` and ``precis_se.fit``
    adapters — each adds its own ``name#k`` labels."""
    count = int(spec["count"])
    if spec.get("kind") == "linear":
        step = np.asarray(spec["axis"], dtype=float) * float(spec["pitch"])
        return [Transform(R=np.eye(3), t=k * step) for k in range(count)]
    axis = np.asarray(spec["axis"], dtype=float)
    radius = float(spec["radius"])
    ref = (
        np.array([1.0, 0.0, 0.0])
        if abs(float(axis[0])) < 0.9
        else np.array([0.0, 1.0, 0.0])
    )
    u = ref - axis * float(ref @ axis)
    u = u / np.linalg.norm(u)
    return [
        _rot_axis(axis, 2.0 * math.pi * k / count).compose(
            Transform(R=np.eye(3), t=u * radius)
        )
        for k in range(count)
    ]


def place(
    solids: Any,
    xf: Transform,
    *,
    name: str,
    chord: float,
    base: str | None = None,
    parent: str | None = None,
    made_by: str | None = None,
    refine: Callable[[float], Mapping[str, Any]] | None = None,
    material: Mapping[str, str] | None = None,
    note: str | None = None,
    geo_hash: str | None = None,
) -> Placed:
    """Place a template's folded solids at ``xf`` — lazy
    ``Manifold.transform`` per component, no re-meshing."""
    if note is not None:
        return Placed(
            name=name,
            solids={},
            xf=xf,
            chord=chord,
            base=base,
            parent=parent,
            made_by=made_by,
            material=dict(material or {}),
            note=note,
            geo_hash=geo_hash,
        )
    m34 = _xf_to_m34(xf)
    placed = {comp: s.transform(m34) for comp, s in _as_manifold_dict(solids).items()}
    return Placed(
        name=name,
        solids=placed,
        xf=xf,
        chord=chord,
        base=base,
        parent=parent,
        made_by=made_by,
        refine=refine,
        material=dict(material or {}),
        geo_hash=geo_hash,
    )


# ---------------------------------------------------------------------------
# single-pair gap (the Decision-3 referee as a query)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComponentGap:
    """One pair's signed gap, measured on the folded manifold3d solids —
    the same measurement the fit engine trusts (gr514448: the CSG SDF
    only *bounds* distance inside carved bodies and its seed grid misses
    thin features, so ``relate.clearance`` reads wrong there — axle/fork
    on the unicycle reported 2.5 mm against a true 13.5 mm).

    ``gap`` is signed: positive surface distance when disjoint,
    ``−thickness`` (``2V/A``) when the boolean intersection is
    non-empty. ``resolution`` is ``BAND ×`` the chord the answer was
    computed at — a ``|gap|`` inside it is "touching within mesh
    resolution", the band renderers must not print as a bare "clear".
    ``refined`` marks a measurement the chord/10 referee recomputed."""

    gap: float
    interfering: bool
    resolution: float
    thickness: float
    refined: bool


def component_gap(
    spec: SceneSpec,
    a: str,
    b: str,
    *,
    solids: MutableMapping[tuple[str, float], Any] | None = None,
) -> ComponentGap | None:
    """Signed minimum gap between components ``a`` and ``b`` of ``spec``.

    Each component folds through :func:`tessellate_component` at its own
    :func:`component_chord`; the pair measures with
    ``Manifold.min_gap`` plus a boolean intersection, and a result within
    ``BAND × chord`` of the touch boundary — or an overlap thinner than
    ``BAND_THIN × chord`` — re-tessellates both sides at ``chord/10`` and
    recomputes (Decision 3's referee, applied to a bare query the same
    way the fit engine applies it inside a band verdict).

    ``solids`` is an optional ``(component, chord) → Manifold`` memo so a
    caller sweeping many pairs tessellates each component once. The
    ``min_gap`` search bound is the pair's union-AABB diagonal — the
    largest distance two enclosed bodies can have — so the returned gap
    is the true figure, never a clamped one.

    Returns ``None`` when either component cannot be meshed
    (:func:`unmeshable_reason` — ``field:``/``relief:``/``rd``/``blend``)
    or folds to nothing; the caller falls back to the analytic path.
    """
    if unmeshable_reason(spec, a) is not None or unmeshable_reason(spec, b) is not None:
        return None

    def solid(comp: str, chord: float) -> Any:
        if solids is None:
            return tessellate_component(spec, comp, chord)
        key = (comp, chord)
        if key not in solids:
            solids[key] = tessellate_component(spec, comp, chord)
        return solids[key]

    chord_a, chord_b = component_chord(spec, a), component_chord(spec, b)
    sa, sb = solid(a, chord_a), solid(b, chord_b)
    if sa is None or sb is None:
        return None
    chord = max(chord_a, chord_b)

    box_a, box_b = sa.bounding_box(), sb.bounding_box()
    lo = np.minimum(np.asarray(box_a[:3]), np.asarray(box_b[:3]))
    hi = np.maximum(np.asarray(box_a[3:]), np.asarray(box_b[3:]))
    search = float(np.linalg.norm(hi - lo)) or SEARCH_M

    def measure(x: Any, y: Any) -> tuple[float, float]:
        gap, vol, area = _measure(x, y, search)
        thickness = 2.0 * vol / area if area > 0.0 else 0.0
        return (-thickness if vol > 0.0 else gap), thickness

    sg, thickness = measure(sa, sb)
    refined = False
    if abs(sg) <= BAND * chord or 0.0 < thickness <= BAND_THIN * chord:
        finer = chord / REFINE_DIV
        ra, rb = solid(a, finer), solid(b, finer)
        if ra is not None and rb is not None:
            sg, thickness = measure(ra, rb)
            chord = finer
            refined = True
    return ComponentGap(
        gap=sg,
        interfering=thickness > 0.0,
        resolution=BAND * chord,
        thickness=thickness,
        refined=refined,
    )


# ---------------------------------------------------------------------------
# connects / measurements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Connect:
    """One connect as the engine sees it: two block names (port names are
    display only), the ``joint`` dict (``class``/``mechanism``/``params``),
    and the resolved ``thread_size`` for a ``screw`` (``None`` = no
    annotation — the engine never infers one, Decision 6)."""

    a: str
    b: str
    joint: Mapping[str, Any] | None = None
    a_port: str | None = None
    b_port: str | None = None
    thread_size: str | None = None
    made_by: str | None = None


def _rule_of(conn: Connect, rules: Mapping[str, Rule]) -> Rule | None:
    """Mechanism first, then class (Decision 5); a connect naming neither
    still declares contact intent → ``rigid``. An unknown key falls
    through to the next."""
    joint = conn.joint or {}
    for key in (joint.get("mechanism"), joint.get("class")):
        if key is None:
            continue
        r = rules.get(str(key))
        if r is not None:
            return r
    return rules["rigid"]


def _measure(a: Any, b: Any, search: float) -> tuple[float, float, float]:
    """``(gap_m, overlap_m3, area_m2)`` — gap from ``min_gap`` (clamped to
    ``search``), overlap from the boolean intersection."""
    gap = float(a.min_gap(b, search))
    inter = a ^ b
    vol = float(inter.volume())
    area = float(inter.surface_area()) if vol > 0.0 else 0.0
    return gap, vol, area


def _pair_key(a: Placed, b: Placed, rule: Rule) -> str:
    """Decision 4's pair cache key: (hash a, hash b, relative transform
    rounded to 1 µm, rule kind). Rotation entries are dimensionless —
    rounded to the same 1e-6 grid."""
    import hashlib

    ha, hb = sorted([a.geo_hash or a.name, b.geo_hash or b.name])
    rel = a.xf.inverse().compose(b.xf)
    vals = tuple(round(float(v), 6) for v in (*rel.R.flat, *rel.t))
    return hashlib.sha256(
        f"{ha}|{hb}|{vals}|{rule.kind}:{rule.expected!r}".encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass
class Row:
    severity: str  # error | ok | info
    subject: str  # "a / b" for a pair, a part name for a note
    measure: str  # "gap +5.000 mm" / "overlap 234.0 mm³ (thickness 2.060 mm)"
    expected: str = ""
    rule: str = ""
    made_by_a: str = "-"
    made_by_b: str = "-"


def _mm(v_m: float) -> float:
    return round(v_m * 1e3, 3)


def _fmt_gap(g_m: float) -> str:
    return f"gap +{_mm(g_m):.3f} mm" if g_m >= 0 else f"gap {_mm(g_m):.3f} mm"


def _fmt_measure(g_m: float, vol_m3: float, area_m2: float) -> str:
    if vol_m3 > 0.0:
        thickness = 2.0 * vol_m3 / area_m2 if area_m2 > 0.0 else 0.0
        return (
            f"overlap {round(vol_m3 * 1e9, 3):.3f} mm³ "
            f"(thickness {_mm(thickness):.3f} mm)"
        )
    return _fmt_gap(g_m)


@dataclass
class FitReport:
    """The check's result. ``rows`` carry the report's lines; ``findings``
    is the machine-readable error set keyed by (rule, sorted pair) —
    ``to_json()`` is what ``fit.json`` stores and the previous-build diff
    compares (Decision 8). ``text()`` renders deterministically: pairs and
    blocks sorted, values rounded to 1 µm."""

    rows: list[Row] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    n_parts: int = 0
    n_pairs: int = 0
    n_connected: int = 0
    chord_m: float = CHORD_MAX_M
    mass_rows: list[str] = field(default_factory=list)
    #: Total mass in kg when every solid part resolved a density; ``None``
    #: when the section refused or there was nothing to weigh. The
    #: previous-build diff's mass delta reads this (Decision 8).
    mass_kg: float | None = None

    @property
    def errors(self) -> list[Row]:
        return [r for r in self.rows if r.severity == "error"]

    @property
    def has_errors(self) -> bool:
        return bool(self.findings)

    def text(self) -> str:
        head = [
            f"# fit check — {self.n_parts} parts, {self.n_connected} connects, "
            f"{self.n_pairs} pairs measured, chord {_mm(self.chord_m):.3f} mm",
            "# severity | pair | gap / overlap | expected | rule | made-by a | made-by b",
        ]
        order = {"error": 0, "ok": 1, "info": 2}
        rows = sorted(self.rows, key=lambda r: (order.get(r.severity, 3), r.subject))
        body = [
            f"{r.severity} | {r.subject} | {r.measure} | {r.expected} | "
            f"{r.rule} | {r.made_by_a} | {r.made_by_b}"
            for r in rows
        ]
        mass = self.mass_rows
        return "\n".join([*head, *body, *(["# mass", *mass] if mass else [])]) + "\n"

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "parts": self.n_parts,
            "pairs_measured": self.n_pairs,
            "connects": self.n_connected,
            "chord_m": self.chord_m,
            "findings": list(self.findings),
            "report": self.text(),
            "mass_kg": self.mass_kg,
        }


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------


class FitError(ValueError):
    """A fit-check refusal that names what to fix."""


def _classify(
    sg: float,
    vol: float,
    thickness: float,
    rule: Rule,
    tol: float,
    band: float,
) -> str | None:
    """``sg`` is the signed gap: ``min_gap`` when disjoint, ``-thickness``
    when overlapping. Returns the verdict — ``'error'``, ``'band'``
    (within mesh resolution → the chord/10 referee recomputes) or
    ``None`` (clean)."""
    thin = (BAND_THIN / BAND) * band  # 2×chord when band = 3×chord
    if rule.kind == "press":
        if vol > 0.0:
            return "band" if thickness <= thin else None
        if sg > tol:
            return "band" if abs(sg - tol) <= band else "error"
        return None
    if rule.kind in ("contact", "screw"):
        # screw adds its thread-engagement allowance to the overlap limit.
        lim = tol + (rule.expected if rule.kind == "screw" else 0.0)
        if vol > 0.0:
            if thickness > lim + band:
                return "error"
            if thickness > lim or thickness <= thin:
                return "band"
            return None
        if sg > tol:
            return "band" if abs(sg - tol) <= band else "error"
        return "band" if sg >= tol - band else None
    if rule.kind == "slide":
        if vol > 0.0:
            return "band" if thickness <= thin else "error"
        if sg <= 0.0 or sg <= band:
            return "band"
        return None
    if rule.kind == "clearance":
        if vol > 0.0:
            return "band" if thickness <= thin else "error"
        if sg < rule.expected:
            return "band" if sg >= rule.expected - band else "error"
        return "band" if sg <= rule.expected + band else None
    if rule.kind == "unconnected":
        if vol > 0.0:
            return "band" if thickness <= thin else "error"
        if sg < tol:
            return "error"
        return "band" if sg <= tol + band else None
    return None


def fit(
    placed: list[Placed],
    connects: list[Connect],
    rules: Mapping[str, Rule],
    *,
    sizes: Mapping[str, tuple[float, float]] | None = None,
    materials: Mapping[str, float] | None = None,
    min_clearance: Mapping[tuple[str, str], float] | None = None,
    pair_cache: MutableMapping[str, dict[str, float]] | None = None,
) -> FitReport:
    """Run the mesh fit check over ``placed`` parts and ``connects``.

    ``rules`` is the gap-rule table (``default_rules()`` for the spec's
    own). ``sizes`` maps a thread size → ``(major_m, minor_m)`` for the
    ``screw`` overlap allowance; a screw connect without a resolvable size
    gets no allowance (never inferred, Decision 6). ``materials`` maps
    material slug → kg/m³ for the mass section (a missing table or slug
    produces a refusal line; the rest of the report still runs, Decision 5
    item 5). ``pair_cache`` is the Decision-4 memo: keyed by
    (hash a, hash b, relative transform @1 µm, rule) → raw measurements;
    supply ``{}`` or the persisted ``.fitcache`` map.
    """
    rep = FitReport()
    parts = [p for p in placed if p.checked]
    notes = [p for p in placed if not p.checked]
    rep.n_parts = len(parts) + len(notes)
    rep.chord_m = max((p.chord for p in parts), default=CHORD_MAX_M)

    by_block: dict[str, list[Placed]] = {}
    for p in parts:
        by_block.setdefault(p.block, []).append(p)

    # -- connects → pair rules -------------------------------------------
    pair_conns: dict[tuple[str, str], list[Connect]] = {}
    skipped_conns: list[tuple[Connect, str]] = []
    for c in connects:
        pa, pb = by_block.get(c.a, []), by_block.get(c.b, [])
        if c.a == c.b:
            skipped_conns.append((c, "connect joins a block to itself"))
            continue
        if not pa or not pb:
            skipped_conns.append((c, "endpoint not a fit-checked part"))
            continue
        for x in pa:
            for y in pb:
                if x.name != y.name:
                    pair = (x.name, y.name) if x.name < y.name else (y.name, x.name)
                    pair_conns.setdefault(pair, []).append(c)
    rep.n_connected = len(pair_conns)

    for c, why in skipped_conns:
        rep.rows.append(
            Row(
                "info",
                f"{c.a} / {c.b}",
                f"not checked — {why}",
                rule="connect",
                made_by_a=c.made_by or "-",
            )
        )
    for p in notes:
        rep.rows.append(
            Row(
                "info",
                p.name,
                f"not fit-checked — {p.note}",
                made_by_a=p.made_by or "-",
            )
        )

    # -- broad phase ------------------------------------------------------
    plist = sorted(parts, key=lambda p: p.name)
    pairs: list[tuple[Placed, Placed]] = []
    for i, a in enumerate(plist):
        for b in plist[i + 1 :]:
            key = (a.name, b.name)
            connected = key in pair_conns
            if not connected and (a.parent == b.block or b.parent == a.block):
                continue
            inflate = max(SEARCH_CONNECTED_M if connected else SEARCH_M, TOL_FLOOR_M)
            lo_a, hi_a = a.aabb
            lo_b, hi_b = b.aabb
            if connected or bool(
                np.all(lo_a <= hi_b + inflate) and np.all(lo_b <= hi_a + inflate)
            ):
                pairs.append((a, b))

    # -- measure + classify ----------------------------------------------
    for a, b in pairs:
        key = (a.name, b.name)
        conns = pair_conns.get(key, [])
        rules_here: list[tuple[Connect, Rule]] = []
        for c in conns:
            r = _rule_of(c, rules)
            if r is not None and r.kind == "skip":
                rep.rows.append(
                    Row(
                        "info",
                        f"{a.name} / {b.name}",
                        f"not fit-checked ({r.label})",
                        rule=r.label,
                        made_by_a=c.made_by or a.made_by or "-",
                        made_by_b=b.made_by or "-",
                    )
                )
                continue
            if r is None:
                r = rules["rigid"]
            if r.kind == "screw" and c.thread_size and sizes and c.thread_size in sizes:
                d, d_minor = sizes[c.thread_size]
                r = Rule("screw", (d - d_minor) / 2.0, r.label)
            rules_here.append((c, r))
        if conns and not rules_here:
            continue  # every connect on the pair was 'skip' — not measured
        rep.n_pairs += 1
        rule = UNCONNECTED_RULE
        for c, r in rules_here:
            if _strictness(r) > _strictness(rule):
                rule = r
        # a per-pair min_clearance (manifest fit_rules) overrides the table
        if min_clearance:
            mc = min_clearance.get(key, min_clearance.get((key[1], key[0])))
            if mc is not None:
                rule = Rule("clearance", float(mc), "min_clearance")
        chord = max(a.chord, b.chord)
        band = BAND * chord
        tol = max(TOL_FLOOR_M, band)
        search = max(SEARCH_M, tol, rule.expected)
        if conns:
            search = max(search, SEARCH_CONNECTED_M)

        ck = _pair_key(a, b, rule)
        cached = pair_cache.get(ck) if pair_cache is not None else None
        if cached is not None:
            gap, vol, area = cached["gap"], cached["vol"], cached["area"]
        else:
            gap, vol, area = _measure(a.combined, b.combined, search)
            if pair_cache is not None:
                pair_cache[ck] = {"gap": gap, "vol": vol, "area": area}
        thickness = 2.0 * vol / area if area > 0.0 else 0.0
        sg = -thickness if vol > 0.0 else gap

        verdict = _classify(sg, vol, thickness, rule, tol, band)
        refined = False
        if verdict == "band" and a.refine is not None and b.refine is not None:
            sa = _combine(a.refine(chord / REFINE_DIV))
            sb = _combine(b.refine(chord / REFINE_DIV))
            gap, vol, area = _measure(sa, sb, search)
            thickness = 2.0 * vol / area if area > 0.0 else 0.0
            sg = -thickness if vol > 0.0 else gap
            refined = True
            verdict = _settle(sg, vol, thickness, rule, tol)

        measure = _fmt_measure(gap, vol, area) + (" [refined]" if refined else "")
        rule_cell = rule.label
        if len(conns) > 1:
            ends = "; ".join(
                f"{c.a}.{c.a_port or '?'}↔{c.b}.{c.b_port or '?'}" for c in conns
            )
            rule_cell = f"{rule.label} [{ends}]"
        if verdict == "error":
            rep.rows.append(
                Row(
                    "error",
                    f"{a.name} / {b.name}",
                    measure,
                    expected=rule.expected_text,
                    rule=rule_cell,
                    made_by_a=a.made_by or "-",
                    made_by_b=b.made_by or "-",
                )
            )
            rep.findings.append(
                {
                    "rule": rule.label,
                    "pair": [a.name, b.name],
                    "gap_m": round(gap, 9),
                    "overlap_m3": round(vol, 12),
                    "thickness_m": round(thickness, 9),
                }
            )
        elif vol > 0.0 or conns or gap < search:
            rep.rows.append(
                Row(
                    "ok",
                    f"{a.name} / {b.name}",
                    measure,
                    expected=rule.expected_text,
                    rule=rule_cell,
                    made_by_a=a.made_by or "-",
                    made_by_b=b.made_by or "-",
                )
            )

    # -- mass (item 5): per block, total, CoG, world bbox ------------------
    rep.mass_rows, rep.mass_kg = _mass_section(parts, notes, materials)

    rep.findings.sort(key=lambda f: (f["rule"], f["pair"]))
    return rep


def _strictness(r: Rule) -> int:
    """Strictest-rule-wins order for multi-connect pairs (press fires on
    any positive gap, contact on any deviation, slide only on overlap)."""
    return {"press": 4, "screw": 3, "contact": 2, "slide": 1, "unconnected": 0}.get(
        r.kind, 0
    )


def _settle(
    sg: float, vol: float, thickness: float, rule: Rule, tol: float
) -> str | None:
    """The referee's final call — the rule applied raw to the refined
    numbers (a still-in-band result at chord/10 is resolved, not deferred)."""
    if rule.kind == "press":
        return "error" if vol <= 0.0 and sg > tol else None
    if rule.kind in ("contact", "screw"):
        lim = tol + (rule.expected if rule.kind == "screw" else 0.0)
        if vol > 0.0:
            return "error" if thickness > lim else None
        return "error" if sg > tol else None
    if rule.kind == "slide":
        return "error" if vol > 0.0 or sg <= 0.0 else None
    if rule.kind == "clearance":
        return "error" if vol > 0.0 or sg < rule.expected else None
    if rule.kind == "unconnected":
        return "error" if vol > 0.0 or sg < tol else None
    return None


def _combine(solids: Mapping[str, Any]) -> Any:
    cur = next(iter(solids.values()))
    for s in list(solids.values())[1:]:
        cur = cur + s
    return cur


def _centroid(solid: Any) -> tuple[np.ndarray, float] | None:
    """``(centroid, volume_m3)`` of a placed solid via the divergence
    theorem on its mesh (deterministic; needed for CoG — manifold3d has
    volume but no centroid)."""
    mesh = solid.to_mesh()
    verts = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    tris = np.asarray(mesh.tri_verts, dtype=np.int64)
    if tris.size == 0:
        return None
    a = verts[tris[:, 0]]
    b = verts[tris[:, 1]]
    c = verts[tris[:, 2]]
    vol6 = np.einsum("ij,ij->i", a, np.cross(b, c))
    total = float(vol6.sum())
    if abs(total) < 1e-30:
        return None
    centroid = ((a + b + c) / 4.0 * vol6[:, None]).sum(axis=0) / total
    return centroid, total / 6.0


def _mass_section(
    parts: list[Placed], notes: list[Placed], materials: Mapping[str, float] | None
) -> tuple[list[str], float | None]:
    """Per-block mass + CoG + world bbox lines. ``materials`` maps slug →
    kg/m³; without it (or with a missing slug) the section carries one
    refusal naming every offending block.component — the other checks
    still ran (slice-1 engine item 5)."""
    lines: list[str] = []
    missing: list[str] = []
    total = 0.0
    cog_sum = np.zeros(3)
    vol_sum = 0.0
    any_mass = False
    for p in sorted(parts, key=lambda p: p.name):
        comp_mass = 0.0
        comp_cog = np.zeros(3)
        comp_vol = 0.0
        for comp, solid in sorted(p.solids.items()):
            slug = p.material.get(comp)
            r = _centroid(solid)
            if slug is None or materials is None or slug not in materials:
                missing.append(f"{p.name}:{comp}" + (f" ({slug})" if slug else ""))
                continue
            if r is None:
                continue
            centroid, vol = r
            rho = float(materials[slug])
            comp_mass += vol * rho
            comp_cog += centroid * vol * rho
            comp_vol += vol
        if comp_mass > 0.0:
            any_mass = True
            c = comp_cog / comp_mass
            lo, hi = p.aabb
            lines.append(
                f"{p.name} | {comp_mass:.4f} kg | CoG [{_mm(c[0]):.3f}, "
                f"{_mm(c[1]):.3f}, {_mm(c[2]):.3f}] mm | bbox "
                f"[{_mm(lo[0]):.3f},{_mm(lo[1]):.3f},{_mm(lo[2]):.3f}] .. "
                f"[{_mm(hi[0]):.3f},{_mm(hi[1]):.3f},{_mm(hi[2]):.3f}] mm"
            )
            total += comp_mass
            cog_sum += comp_cog
            vol_sum += comp_vol
    for p in notes:
        for comp in p.material:
            missing.append(f"{p.name}:{comp} ({p.material[comp]}) — not fit-checked")
    if missing:
        why = "no materials= density table" if materials is None else "no density"
        lines.append(
            f"mass: refused ({why}) for {', '.join(sorted(set(missing)))}"
            if materials is None
            else f"mass: no density for {', '.join(sorted(set(missing)))} "
            f"— pass materials={{'slug': kg/m³}}"
        )
    if not any_mass and not missing:
        lines.append("mass: no solid parts")
    elif any_mass:
        c = cog_sum / total if total > 0 else np.zeros(3)
        lines.append(
            f"total | {total:.4f} kg | CoG [{_mm(c[0]):.3f}, {_mm(c[1]):.3f}, "
            f"{_mm(c[2]):.3f}] mm"
        )
    return lines, (total if any_mass else None)


__all__ = [
    "ComponentGap",
    "Connect",
    "FitError",
    "FitReport",
    "Placed",
    "Row",
    "Rule",
    "array_member_transforms",
    "component_chord",
    "component_gap",
    "default_rules",
    "fit",
    "geometry_hash",
    "mesh_to_manifold",
    "place",
    "tessellate_component",
    "unmeshable_reason",
]

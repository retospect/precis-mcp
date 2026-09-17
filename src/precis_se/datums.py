"""se datums + the measurement-from-geometry evaluator
(docs/backlog/se-datum-measure-eval.md; open item 10 of
multiscale-optimisation-method.md §4 — the attachment point for
:mod:`precis.structsolve.preferred`'s round-value wells).

A measure is declared against a **datum** — a named feature of the
block (a face, an axis, a port, the pose frame) that rides with the
envelope but is never itself an optimiser DOF (the no-free-DOF guard:
a datum that could slide would let a term "satisfy" a measurement by
moving the reference instead of the geometry). ``datum`` is stored on
``se_measures`` as a **column**, not a JSON key — it is addressable
(the ``datums`` view answers "which measures hang off datum A", and a
stale resolution is a DRC-class finding), and JSON neither indexes nor
constrains.

**Predicate declares, name pins.** The stored selector may be a
predicate (``face:largest``); every evaluation records the *resolved*
``instance.tag`` in :attr:`MeasureValue.datum_resolved`. When the
resolution differs from the caller-passed previous one (a predicate
now picks another face; a named face is gone after a topology move)
the evaluator emits a ``datum moved`` note — the caller re-baselines,
never integrates a gradient across the jump. Selectors are strict on
**shape** at write (:func:`parse_selector` raises
:exc:`~precis_se.measures.MeasureError` on an unknown prefix or a
malformed body) and lenient on **existence** — ``face:body.side9``
parses fine and fails only at resolve, returning ``value=None`` plus a
naming note.

v1 vocabulary, exactly: ``frame`` · ``port:<name>`` ·
``face:<instance>.<tag>`` · ``axis:<instance>`` · ``face:largest`` ·
``face:normal=<±x|±y|±z>`` · ``face:perp=assembly``. Tags are the cad
kernel's own (``bottom``, ``top``, ``side<N>``, ``cut``). Compound
predicates live in :func:`rank_datums`, not the grammar — ranking
picks the default, the grammar only names an override.

All geometry comes from the block's posed cad **envelope**
(:func:`precis_se.ops.effective_envelope` → :mod:`precis.cad.dsl` → a
:class:`~precis.cad.primitives.Placed` primitive): metres, rigid pose
only — a ``set_pose`` moves datum and feature together, so
datum-relative numbers are pose-invariant by construction. Face plane
points come from an exact ray exit (convex primitives); face *areas*
are exact for :class:`~precis.cad.primitives.PolyFrustum` envelopes
(its face polygons) and a planform estimate off the AABB otherwise —
a ranking heuristic, never a measurement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.cad import dsl as cad_dsl
from precis.cad.primitives import CircularFrustum, Placed, PolyFrustum
from precis.cad.vec import as_vec3
from precis.cad.vec import pose as cad_pose
from precis_se.measures import MeasureError, MeasureSpec, declared_band
from precis_se.ops import SeBlock, SeTree, effective_envelope, effective_ports

#: Default assembly/insertion direction when a caller supplies none —
#: world +z, the standing se convention for a single shared insertion
#: direction.
_DEFAULT_ASSEMBLY_DIR = np.array([0.0, 0.0, 1.0])


@dataclass(frozen=True)
class Selector:
    """A parsed ``datum``/``feature`` selector. ``kind`` is one of
    ``frame | port | face | axis``; a ``face`` with ``instance=None``
    is a predicate (``pred`` ∈ ``largest | normal | perp_assembly``)."""

    kind: str
    instance: str | None = None
    tag: str | None = None
    name: str | None = None  # port name
    pred: str | None = None
    arg: str | None = None  # e.g. '+x' for pred='normal'


@dataclass(frozen=True)
class ResolvedDatum:
    """A selector resolved against the live tree. ``error`` set means
    unresolvable — the lenient-existence half of the posture: legal at
    write, a finding at read. ``point``/``normal`` are world-frame;
    ``members`` names the constituent features (the frame's 3-2-1
    faces, the rotational primitive's axis + base)."""

    selector: str
    kind: str
    resolved: str | None = None
    point: Any = None
    normal: Any = None
    members: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class Ranked:
    """One datum candidate in :func:`rank_datums`' ordering."""

    datum: str  # the selector text that names it
    score: float
    reason: str


@dataclass(frozen=True)
class MeasureValue:
    """The geometric number for one measure: ``value`` in the measure's
    own unit (``None`` when unresolvable), the resolved datum identity,
    and honesty notes (``mismatch`` — band-first: outside a declared
    ``[min,max]``, else beyond ``relation.tol`` of the declared value,
    else not exactly equal; a datum that moved since the caller's last
    evaluation; an unresolvable selector). ``source='derived'`` — never confused with a declared
    ``origin`` number."""

    value: float | None
    unit: str
    datum_resolved: str | None
    source: str = "derived"
    notes: tuple[str, ...] = ()


def parse_selector(text: Any) -> Selector:
    """Parse one selector string — strict on shape, silent on existence
    (module docstring). Raises :class:`MeasureError` on anything outside
    the v1 vocabulary."""
    if not isinstance(text, str) or not text.strip():
        raise MeasureError(
            f"datum selector must be a non-empty string, got {text!r} — "
            "vocabulary: frame | port:<name> | face:<instance>.<tag> | "
            "axis:<instance> | face:largest | face:normal=<±x|±y|±z> | "
            "face:perp=assembly"
        )
    s = text.strip()
    if s == "frame":
        return Selector(kind="frame")
    head, sep, body = s.partition(":")
    if not sep or not body:
        raise MeasureError(
            f"unknown datum selector {s!r} — vocabulary: frame | "
            "port:<name> | face:<instance>.<tag> | axis:<instance> | "
            "face:largest | face:normal=<±x|±y|±z> | face:perp=assembly"
        )
    if head == "port":
        if "." in body or not body.strip():
            raise MeasureError(f"port selector needs a plain name, got {s!r}")
        return Selector(kind="port", name=body.strip())
    if head == "axis":
        if "." in body or not body.strip():
            raise MeasureError(f"axis selector is 'axis:<instance>', got {s!r}")
        return Selector(kind="axis", instance=body.strip())
    if head == "face":
        if body == "largest":
            return Selector(kind="face", pred="largest")
        if body.startswith("normal="):
            d = body[len("normal=") :]
            if d not in ("+x", "-x", "+y", "-y", "+z", "-z"):
                raise MeasureError(f"face:normal= takes ±x|±y|±z, got {s!r}")
            return Selector(kind="face", pred="normal", arg=d)
        if body == "perp=assembly":
            return Selector(kind="face", pred="perp_assembly")
        inst, dot, tag = body.rpartition(".")
        if not dot or not inst.strip() or not tag.strip():
            raise MeasureError(
                f"face selector is 'face:<instance>.<tag>' or a predicate "
                f"(largest | normal=±x|±y|±z | perp=assembly), got {s!r}"
            )
        return Selector(kind="face", instance=inst.strip(), tag=tag.strip())
    raise MeasureError(
        f"unknown datum selector prefix {head!r} in {s!r} — vocabulary: "
        "frame | port:<name> | face:<instance>.<tag> | axis:<instance> | "
        "face:largest | face:normal=<±x|±y|±z> | face:perp=assembly"
    )


def _placed(
    tree: SeTree, name: str, env_override: dict[str, str] | None = None
) -> Placed | None:
    """The block's envelope as a world-posed primitive, or ``None`` when
    it has none / no longer parses (a stored-envelope problem is the
    caller's note, not a crash). ``env_override`` swaps one block's
    envelope text — the finite-difference path."""
    node = tree.blocks.get(name)
    if node is None:
        return None
    env = effective_envelope(tree, node)
    if env_override and name in env_override:
        env = env_override[name]
    if env is None:
        return None
    try:
        prim = cad_dsl.build(cad_dsl.parse(env))
    except (cad_dsl.DslError, ValueError):
        return None
    xform = cad_pose(as_vec3(node.pose), as_vec3(node.rot))
    return Placed(prim=prim, xform=xform)


def _hull_area_2d(pts: np.ndarray) -> float:
    """Convex-hull area of projected points (monotone chain) — the
    planform fallback for face area when the primitive does not expose
    its face polygons."""

    def cross(o: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
        return float((a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]))

    uniq = sorted({(float(p[0]), float(p[1])) for p in pts})
    if len(uniq) < 3:
        return 0.0
    P = [np.array(u) for u in uniq]
    lower: list[np.ndarray] = []
    for p in P:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[np.ndarray] = []
    for p in reversed(P):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    area = 0.0
    for i in range(len(hull)):
        a, b = hull[i], hull[(i + 1) % len(hull)]
        area += float(a[0] * b[1] - b[0] * a[1])
    return abs(area) / 2.0


def _face_geometry(placed: Placed) -> dict[str, tuple[Any, Any, float]]:
    """``tag → (world normal, world plane point, area)`` for every
    planar face. The plane point is the exact ray exit along the face
    normal from the AABB centre (convex primitives). Area is exact for
    ``PolyFrustum`` (its face polygons, aligned 1:1 with ``faces``) and
    the AABB planform estimate otherwise."""
    prim = placed.prim
    lo, hi = prim.aabb_local()
    centre = (as_vec3(lo) + as_vec3(hi)) / 2.0
    faces = prim.faces_local()
    polys = getattr(prim, "_face_polys", None)  # PolyFrustum, when present
    out: dict[str, tuple[Any, Any, float]] = {}
    corners_local = np.array(
        [
            [lo[0], lo[1], lo[2]],
            [hi[0], lo[1], lo[2]],
            [lo[0], hi[1], lo[2]],
            [hi[0], hi[1], lo[2]],
            [lo[0], lo[1], hi[2]],
            [hi[0], lo[1], hi[2]],
            [lo[0], hi[1], hi[2]],
            [hi[0], hi[1], hi[2]],
        ]
    )
    corners_world = (placed.xform.R @ corners_local.T).T + placed.xform.t
    for i, f in enumerate(faces):
        n_l = as_vec3(f.normal)
        hits = prim.ray_hits_local(centre, n_l)
        if not hits:
            continue
        t_exit = max(b for _a, b in hits)
        p_local = centre + t_exit * n_l
        p_world = placed.xform.R @ p_local + placed.xform.t
        n_world = placed.xform.apply_dir(n_l)
        area = 0.0
        if isinstance(prim, PolyFrustum) and polys is not None and i < len(polys):
            ring = polys[i][1]
            avec = np.zeros(3)
            for j in range(len(ring)):
                avec += np.cross(ring[j], ring[(j + 1) % len(ring)])
            area = float(np.linalg.norm(avec)) / 2.0
        if area == 0.0:
            # planform estimate: project the world AABB onto the face
            # plane — exact for axis-aligned prismatic faces, a mild
            # overestimate for slanted ones (a ranking heuristic only).
            n = n_world / float(np.linalg.norm(n_world))
            u = np.cross(n, np.array([0.0, 0.0, 1.0]))
            if float(np.linalg.norm(u)) < 1e-9:
                u = np.cross(n, np.array([1.0, 0.0, 0.0]))
            u = u / float(np.linalg.norm(u))
            v = np.cross(n, u)
            pts = np.stack([corners_world @ u, corners_world @ v], axis=1)
            area = _hull_area_2d(pts)
        out[f.tag] = (n_world, p_world, area)
    return out


def _frame_members(
    prim: Any, geom: dict[str, tuple[Any, Any, float]], instance: str
) -> tuple[str, ...]:
    """The frame default's named constituents: a prismatic envelope's
    3-2-1 — the face nearest the frame origin on each axis, preferring
    the negative-facing one (the corner the part sits in); a rotational
    primitive's axis + base face."""
    if isinstance(prim, CircularFrustum):
        rot = [f"axis:{instance}"]
        if "bottom" in geom:
            rot.append(f"{instance}.bottom")
        return tuple(rot)
    # local-frame pick: nearest plane to origin per axis, − side first
    members: list[str] = []
    local_geom: dict[str, tuple[Any, float]] = {}
    prim_lo, prim_hi = prim.aabb_local()
    centre = (as_vec3(prim_lo) + as_vec3(prim_hi)) / 2.0
    for f in prim.faces_local():
        n = as_vec3(f.normal)
        hits = prim.ray_hits_local(centre, n)
        if not hits:
            continue
        p = centre + max(b for _a, b in hits) * n
        local_geom[f.tag] = (n, float(n @ p))  # normal, plane offset
    for axis in (
        np.array([0.0, 0.0, 1.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([1.0, 0.0, 0.0]),
    ):
        best: tuple[float, float, str] | None = None
        for tag, (n, off) in local_geom.items():
            dot = float(n @ axis)
            if abs(dot) < 0.9:
                continue
            # nearest plane to the origin; prefer the negative-facing
            # face (dot < 0) on a tie — the 3-2-1 corner convention.
            key = (abs(off), 0.0 if dot < 0 else 1.0, tag)
            if best is None or key < best:
                best = key
        if best is not None:
            members.append(f"{instance}.{best[2]}")
    return tuple(members)


def resolve(
    tree: SeTree,
    block: str | SeBlock,
    selector: str | Selector | None,
    *,
    assembly_dir: Any = None,
    env_override: dict[str, str] | None = None,
) -> ResolvedDatum:
    """Resolve a selector against the live tree. ``None``/``'frame'``
    is the NULL-datum default — the block's pose frame. Never raises on
    existence; a miss comes back as ``error``."""
    node = tree.blocks[block] if isinstance(block, str) else block
    sel = parse_selector(selector) if isinstance(selector, str) else selector
    if sel is None:
        sel = Selector(kind="frame")
    text = selector if isinstance(selector, str) else _selector_text(sel)

    def err(msg: str) -> ResolvedDatum:
        return ResolvedDatum(selector=text, kind=sel.kind, error=msg)

    if sel.kind == "frame":
        placed = _placed(tree, node.name, env_override)
        if placed is None:
            return err(f"block {node.name!r} has no parseable envelope")
        geom = _face_geometry(placed)
        members = _frame_members(placed.prim, geom, node.name)
        return ResolvedDatum(
            selector=text,
            kind="frame",
            resolved="frame",
            point=np.asarray(placed.xform.t, dtype=float),
            normal=None,
            members=members,
        )
    if sel.kind == "port":
        port = effective_ports(tree, node).get(sel.name or "")
        if port is None:
            return err(f"no port {sel.name!r} on block {node.name!r}")
        if not port.direction:
            return err(f"port {sel.name!r} on {node.name!r} has no direction")
        placed = _placed(tree, node.name, env_override)
        origin = (
            np.asarray(placed.xform.t, dtype=float)
            if placed is not None
            else as_vec3(node.pose)
        )
        n = as_vec3(port.direction)
        n = n / float(np.linalg.norm(n))
        if placed is not None:
            n = placed.xform.apply_dir(n)
        return ResolvedDatum(
            selector=text,
            kind="port",
            resolved=f"port:{sel.name}",
            point=origin,
            normal=n,
        )
    if sel.kind == "axis":
        target = sel.instance or node.name
        placed = _placed(tree, target, env_override)
        if placed is None:
            return err(f"block {target!r} has no parseable envelope")
        if not isinstance(placed.prim, CircularFrustum):
            return err(
                f"block {target!r} has no axis — its envelope is not a "
                "rotational primitive"
            )
        return ResolvedDatum(
            selector=text,
            kind="axis",
            resolved=f"axis:{target}",
            point=np.asarray(placed.xform.t, dtype=float),
            normal=placed.xform.apply_dir(np.array([0.0, 0.0, 1.0])),
        )
    # face
    target = sel.instance or node.name
    placed = _placed(tree, target, env_override)
    if placed is None:
        return err(f"block {target!r} has no parseable envelope")
    geom = _face_geometry(placed)
    if sel.pred is None:
        got = geom.get(sel.tag or "")
        if got is None:
            return err(
                f"no face tagged {sel.tag!r} on block {target!r} "
                f"(has: {', '.join(sorted(geom)) or 'none'})"
            )
        n, p, _a = got
        return ResolvedDatum(
            selector=text,
            kind="face",
            resolved=f"{target}.{sel.tag}",
            point=p,
            normal=n,
        )
    if not geom:
        return err(f"block {target!r} has no planar faces")
    if sel.pred == "largest":
        tag = max(geom, key=lambda t: (geom[t][2], t))
    elif sel.pred == "normal":
        want = {
            "+x": [1, 0, 0],
            "-x": [-1, 0, 0],
            "+y": [0, 1, 0],
            "-y": [0, -1, 0],
            "+z": [0, 0, 1],
            "-z": [0, 0, -1],
        }[sel.arg or "+z"]
        d = np.asarray(want, dtype=float)
        tag = max(geom, key=lambda t: (float(geom[t][0] @ d), t))
        if float(geom[tag][0] @ d) < 0.9:
            return err(f"no face on {target!r} with normal ≈ {sel.arg} (best: {tag})")
    else:  # perp_assembly — the face the part sits on: plane ⊥ insert dir
        d = np.asarray(
            _DEFAULT_ASSEMBLY_DIR if assembly_dir is None else assembly_dir,
            dtype=float,
        )
        d = d / float(np.linalg.norm(d))
        tag = max(geom, key=lambda t: (abs(float(geom[t][0] @ d)), t))
    n, p, _a = geom[tag]
    return ResolvedDatum(
        selector=text, kind="face", resolved=f"{target}.{tag}", point=p, normal=n
    )


def _selector_text(sel: Selector) -> str:
    if sel.kind == "frame":
        return "frame"
    if sel.kind == "port":
        return f"port:{sel.name}"
    if sel.kind == "axis":
        return f"axis:{sel.instance}"
    if sel.pred == "largest":
        return "face:largest"
    if sel.pred == "normal":
        return f"face:normal={sel.arg}"
    if sel.pred == "perp_assembly":
        return "face:perp=assembly"
    return f"face:{sel.instance}.{sel.tag}"


def rank_datums(
    tree: SeTree, block: str | SeBlock, *, assembly_dir: Any = None
) -> list[Ranked]:
    """The deterministic datum ranking (method doc §4 heuristics):
    port faces first — a contract-fixed face costs no optimiser DOF —
    then planar faces scored ``area × flatness × perpendicularity to
    the assembly direction × measurable`` (measurable is a ``True``
    placeholder — the caliper swept-volume check is explicitly out of
    scope), ties broken by tag. ``frame`` closes the list: it is the
    NULL-datum default, not a ranked surface."""
    node = tree.blocks[block] if isinstance(block, str) else block
    out: list[Ranked] = []
    for pname in sorted(effective_ports(tree, node)):
        out.append(
            Ranked(
                datum=f"port:{pname}",
                score=math.inf,
                reason="port face — contract-fixed, costs no optimiser "
                "DOF (a free datum)",
            )
        )
    placed = _placed(tree, node.name)
    if placed is not None:
        d = (
            None
            if assembly_dir is None
            else as_vec3(assembly_dir) / float(np.linalg.norm(as_vec3(assembly_dir)))
        )
        geom = _face_geometry(placed)
        scored: list[tuple[float, str]] = []
        for tag, (n, _p, area) in geom.items():
            perp = 1.0 if d is None else abs(float(n @ d))
            scored.append((-(area * 1.0 * perp * 1.0), tag))
        for neg_score, tag in sorted(scored):
            area = geom[tag][2]
            why = f"flat face, area {area:.4g} m²"
            if d is not None:
                why += f", |n·assembly| = {abs(float(geom[tag][0] @ d)):.3g}"
            why += ", measurable=assumed (caliper sweep TODO)"
            out.append(
                Ranked(datum=f"face:{node.name}.{tag}", score=-neg_score, reason=why)
            )
    out.append(
        Ranked(
            datum="frame",
            score=-math.inf,
            reason="pose frame — the default when datum is NULL (3-2-1 "
            "on a prismatic envelope, axis + base face on a rotational "
            "primitive)",
        )
    )
    return out


def _feature_of(spec: MeasureSpec) -> str | None:
    rel = spec.relation or {}
    f = rel.get("feature")
    return f if isinstance(f, str) and f.strip() else None


def evaluate_measure(
    tree: SeTree,
    spec: MeasureSpec,
    *,
    prev_resolved: str | None = None,
    env_override: dict[str, str] | None = None,
) -> MeasureValue:
    """The geometric number for one measure: distance between the
    measured feature (``relation.feature``, same selector grammar) and
    the datum feature, along the datum normal/axis — for a ``frame``
    datum, the feature's position along its own normal from the frame
    origin. Always nonneg (a distance). Stateless; the caller passes
    ``prev_resolved`` and re-baselines on a ``datum moved`` note."""
    notes: list[str] = []
    block = tree.blocks.get(spec.block)
    if block is None:
        return MeasureValue(
            None, spec.unit, None, notes=(f"block {spec.block!r} not found",)
        )
    if spec.unit != "m":
        # A feature measurement is a length off the cad envelope; a
        # count/ratio/deg measure has no geometric number to compare.
        return MeasureValue(
            None,
            spec.unit,
            None,
            notes=(
                f"measure unit is {spec.unit!r}; a feature measurement is a "
                "length in m — nothing to derive",
            ),
        )
    sel_text = spec.datum or "frame"
    try:
        datum = resolve(tree, block, sel_text, env_override=env_override)
    except MeasureError as exc:
        return MeasureValue(
            None,
            spec.unit,
            None,
            notes=(f"unresolvable datum selector {sel_text!r}: {exc}",),
        )
    if datum.error is not None:
        return MeasureValue(
            None, spec.unit, None, notes=(f"datum {sel_text!r}: {datum.error}",)
        )
    resolved = datum.resolved
    if prev_resolved is not None and prev_resolved != resolved:
        notes.append(f"datum moved: {prev_resolved} → {resolved}")
    feat_text = _feature_of(spec)
    if feat_text is None:
        return MeasureValue(
            None,
            spec.unit,
            resolved,
            notes=tuple(
                notes + ["no 'feature' selector in relation — nothing to measure"]
            ),
        )
    try:
        feat = resolve(tree, block, feat_text, env_override=env_override)
    except MeasureError as exc:
        return MeasureValue(
            None,
            spec.unit,
            resolved,
            notes=tuple(
                notes + [f"unresolvable feature selector {feat_text!r}: {exc}"]
            ),
        )
    if feat.error is not None:
        return MeasureValue(
            None,
            spec.unit,
            resolved,
            notes=tuple(notes + [f"feature {feat_text!r}: {feat.error}"]),
        )
    if datum.kind == "frame":
        direction = feat.normal
        if direction is None:
            return MeasureValue(
                None,
                spec.unit,
                resolved,
                notes=tuple(
                    notes
                    + ["frame datum + feature with no normal — nothing to project onto"]
                ),
            )
    else:
        direction = datum.normal
    assert direction is not None
    value = abs(float((feat.point - datum.point) @ direction))
    # Mismatch precedence: a declared band ``[min,max]`` is the
    # acceptance criterion when present (the derived number must fall
    # inside it); else ``relation.tol`` around the declared value; else
    # exact agreement with the declared value.
    band = declared_band(spec)
    if band is not None:
        lo, hi = band
        if not (lo <= value <= hi):
            declared = (
                f"declared {spec.value:g} {spec.unit}, "
                if spec.value is not None
                else ""
            )
            notes.append(
                f"mismatch: {declared}derived {value:g} {spec.unit} outside "
                f"band [{lo:g}, {hi:g}]"
            )
    elif spec.value is not None:
        tol = float((spec.relation or {}).get("tol", 0.0))
        if abs(value - spec.value) > tol:
            notes.append(
                f"mismatch: declared {spec.value:g} {spec.unit} vs "
                f"derived {value:g} {spec.unit}"
                + (f" (tol {tol:g})" if tol > 0 else "")
            )
    return MeasureValue(value, spec.unit, resolved, notes=tuple(notes))


def d_measure(
    tree: SeTree, spec: MeasureSpec, params: list[str] | None = None
) -> dict[str, float]:
    """``dm/dparam`` by central finite difference over the block's
    envelope params (v1; analytic comes with the torch port). Step =
    ``1e-6 × |param|``, floor ``1e-9``. Params that don't exist, or
    whose perturbation leaves the measure unresolvable, report
    ``nan`` — honest, never a crash."""
    block = tree.blocks.get(spec.block)
    if block is None:
        return {}
    env = effective_envelope(tree, block)
    if env is None:
        return {}
    try:
        parsed = cad_dsl.parse(env)
    except (cad_dsl.DslError, ValueError):
        return {}
    names = params if params is not None else list(parsed.params)
    out: dict[str, float] = {}
    for pname in names:
        base = parsed.params.get(pname)
        if base is None:
            out[pname] = math.nan
            continue
        h = max(1e-6 * abs(base), 1e-9)
        vals: list[float] = []
        for sign in (1.0, -1.0):
            mod = dict(parsed.params)
            mod[pname] = base + sign * h
            try:
                env_mod = cad_dsl.format_spec(cad_dsl.ShapeSpec(parsed.alias, mod))
            except (cad_dsl.DslError, ValueError):
                env_mod = None
            v = (
                evaluate_measure(tree, spec, env_override={block.name: env_mod}).value
                if env_mod is not None
                else None
            )
            vals.append(math.nan if v is None else v)
        out[pname] = (vals[0] - vals[1]) / (2 * h)
    return out

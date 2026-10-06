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

Vocabulary (:data:`VOCABULARY`), exactly: ``frame`` · ``port:<name>`` ·
``face:<instance>.<tag>`` · ``axis:<instance>`` · ``face:largest`` ·
``face:normal=<±x|±y|±z>`` · ``face:perp=assembly`` — plus the region
selectors (docs/backlog/se-region-property-layer.md slice A):

- ``patch:<instance>.<tag>@<u>,<v>+<w>x<h>`` — a ``w``×``h`` rectangle on
  face ``tag``, centred ``(u, v)`` from the face centre (metres). ``u``
  runs along the face's first in-plane axis — block-local ``+x``
  projected onto the face plane, or block-local ``+y`` when the face
  normal is within :data:`U_FALLBACK_DEG` of ±x (a face that near ⊥ x
  has no stable ``+x`` projection) — and ``v = n × u``; so a ``set_pose``
  carries the patch with the block. Resolves to the patch centre with the
  face normal. The check is against the face's bounding extent: a centre
  off the face is an error; a rectangle reaching past it is flagged
  ``patch_exceeds_face`` (usually a units slip — ``8`` for ``8e-10``);
  a face that is not a rectangle (area off its bounding rectangle's by
  more than 1 %, or a curved/non-polygon face) is flagged
  ``bounds_approximate``.
- ``ring:<instance>.<tag>`` — the boundary loop of face ``tag`` (a rim,
  an edge loop). Resolves to the loop's face-plane centre with the face
  normal as the loop axis.
- ``sites:<block>/<seam>/s<i>..s<j>`` — hexfold's seam-site addressing,
  sites ``i`` to ``j`` inclusive.
- ``atoms:<block>[<indices>]`` — atom ordinals in the block's bound
  structure, ``0,3,5-9`` (ranges inclusive).

``sites:``/``atoms:`` parse strictly but never resolve here: their
coordinates live in the bound ``structure`` design, which this pure
module does not load — :func:`resolve` checks the block exists and is
bound, then returns the lenient unresolvable note. Their indices only mean
something against ONE structure version, so a measure on one carries a
**pin** (``MeasureSpec.datum_pin``, ``"<structure-slug>@v<n>"``, stamped at
write by :func:`stamp_region_pins`); :func:`stale_pin_note` says when the
block is bound to something else now. Tags are the cad
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

**Ranking and evaluation.** :func:`rank_datums` is deterministic:
largest flat face, port faces free, process-setup candidates, accessible.
:func:`evaluate_measure` reads the number from geometry (ray exits for plain
extents, the param for envelope dimensions, ``feature`` relations for
sub-envelope anchors) and stamps ``source: derived``. Its ``mismatch`` note
is band-first: a declared ``[min,max]`` flags the derived value falling
outside it, else ``relation.tol`` around the declared value, else exact.
:func:`d_measure` central-differences over the envelope params. A measure's
taxon ref id is its identity and its slug a name —
``MeasureSpec.measurand_live`` is refreshed by id on load while the
``measurand`` snapshot keys the registries; the store-free DRC adds
``datum_unresolved`` and ``patch_exceeds_face``, and the handler adds
``region_pin_stale`` when a stale pin replaces the "not loaded" note.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, NamedTuple

import numpy as np

from precis.cad import dsl as cad_dsl
from precis.cad.primitives import CircularFrustum, Placed, PolyFrustum
from precis.cad.vec import aabb_corners, as_vec3
from precis.cad.vec import pose as cad_pose
from precis.utils.units import format_quantity
from precis_se.measures import (
    MeasureError,
    MeasureSpec,
    declared_band,
    is_geometric,
    measurand_name,
)
from precis_se.ops import SeBlock, SeTree, effective_envelope, effective_ports

#: Default assembly/insertion direction when a caller supplies none —
#: world +z, the standing se convention for a single shared insertion
#: direction.
_DEFAULT_ASSEMBLY_DIR = np.array([0.0, 0.0, 1.0])

#: The whole selector grammar, one line — every shape error names it.
VOCABULARY = (
    "frame | port:<name> | face:<instance>.<tag> | axis:<instance> | "
    "face:largest | face:normal=<±x|±y|±z> | face:perp=assembly | "
    "patch:<instance>.<tag>@<u>,<v>+<w>x<h> | ring:<instance>.<tag> | "
    "sites:<block>/<seam>/s<i>..s<j> | atoms:<block>[<indices>]"
)

#: A face whose normal is within this many degrees of ±x takes block-local
#: ``+y`` (not ``+x``) as its patch ``u`` axis: ``+x`` projected onto such a
#: face is a near-zero vector whose direction is numerical noise.
U_FALLBACK_DEG = 5.0
_U_FALLBACK_COS = math.cos(math.radians(U_FALLBACK_DEG))
#: A face counts as a rectangle when its polygon area is within this
#: fraction of its bounding rectangle's.
_RECT_AREA_RTOL = 0.01

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_PATCH_RE = re.compile(rf"^(?P<u>{_NUM}),(?P<v>{_NUM})\+(?P<w>{_NUM})x(?P<h>{_NUM})$")
_SITES_RE = re.compile(r"^s(?P<lo>\d+)\.\.s(?P<hi>\d+)$")
_ATOMS_RE = re.compile(r"^(?P<block>[^\[\]]+)\[(?P<idx>[^\[\]]*)\]$")
_INDEX_ITEM_RE = re.compile(r"^(?P<lo>\d+)(?:-(?P<hi>\d+))?$")
#: Ceiling on the atoms one ``atoms:`` selector expands to.
_MAX_ATOMS = 100_000


@dataclass(frozen=True)
class Selector:
    """A parsed ``datum``/``feature`` selector. ``kind`` is one of
    ``frame | port | face | axis | patch | ring | sites | atoms``; a
    ``face`` with ``instance=None`` is a predicate (``pred`` ∈ ``largest |
    normal | perp_assembly``). ``patch`` carries its centre ``(u, v)`` and
    extent ``(w, h)`` in metres; ``sites``/``atoms`` name their block in
    ``instance``, ``sites`` its ``seam`` and inclusive ``lo..hi``,
    ``atoms`` its sorted, de-duplicated ``indices``."""

    kind: str
    instance: str | None = None
    tag: str | None = None
    name: str | None = None  # port name
    pred: str | None = None
    arg: str | None = None  # e.g. '+x' for pred='normal'
    u: float | None = None
    v: float | None = None
    w: float | None = None
    h: float | None = None
    seam: str | None = None
    lo: int | None = None
    hi: int | None = None
    indices: tuple[int, ...] = ()


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
    #: Honesty notes on a RESOLVED region (a patch past its face, an
    #: approximate bound) and the matching machine ``flags``
    #: (``patch_exceeds_face`` | ``bounds_approximate``).
    notes: tuple[str, ...] = ()
    flags: frozenset[str] = frozenset()
    #: ``error`` is the lenient "expected until a computer exists" kind
    #: (a ``sites:``/``atoms:`` selector on a bound block, an unbound one,
    #: or a stale pin) — declared intent, not a broken reference, so
    #: ``datum_unresolved`` stays quiet about it.
    expected: bool = False


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
    :data:`VOCABULARY`."""
    if not isinstance(text, str) or not text.strip():
        raise MeasureError(
            f"datum selector must be a non-empty string, got {text!r} — "
            f"vocabulary: {VOCABULARY}"
        )
    s = text.strip()
    if s == "frame":
        return Selector(kind="frame")
    head, sep, body = s.partition(":")
    if not sep or not body:
        raise MeasureError(f"unknown datum selector {s!r} — vocabulary: {VOCABULARY}")
    if head == "patch":
        return _parse_patch(s, body)
    if head == "ring":
        inst, dot, tag = body.rpartition(".")
        if not dot or not inst.strip() or not tag.strip() or "@" in body:
            raise MeasureError(
                f"ring selector is 'ring:<instance>.<tag>' (the boundary loop "
                f"of that face), got {s!r} — vocabulary: {VOCABULARY}"
            )
        return Selector(kind="ring", instance=inst.strip(), tag=tag.strip())
    if head == "sites":
        return _parse_sites(s, body)
    if head == "atoms":
        return _parse_atoms(s, body)
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
        f"unknown datum selector prefix {head!r} in {s!r} — vocabulary: {VOCABULARY}"
    )


def _parse_patch(s: str, body: str) -> Selector:
    """``patch:<instance>.<tag>@<u>,<v>+<w>x<h>`` — finite numbers, a
    positive extent."""
    shape = (
        f"patch selector is 'patch:<instance>.<tag>@<u>,<v>+<w>x<h>' (centre "
        f"u,v from the face centre, extent w×h, metres), got {s!r} — "
        f"vocabulary: {VOCABULARY}"
    )
    face, at, rect = body.partition("@")
    inst, dot, tag = face.rpartition(".")
    if not at or not dot or not inst.strip() or not tag.strip():
        raise MeasureError(shape)
    m = _PATCH_RE.match(rect.strip())
    if m is None:
        raise MeasureError(shape)
    u, v, w, h = (float(m[k]) for k in ("u", "v", "w", "h"))
    if not all(math.isfinite(x) for x in (u, v, w, h)):
        raise MeasureError(f"patch numbers must be finite, got {s!r}")
    if w <= 0.0 or h <= 0.0:
        raise MeasureError(f"patch extent w×h must be positive, got {s!r}")
    return Selector(
        kind="patch", instance=inst.strip(), tag=tag.strip(), u=u, v=v, w=w, h=h
    )


def _parse_sites(s: str, body: str) -> Selector:
    """``sites:<block>/<seam>/s<i>..s<j>`` — ``i ≤ j``."""
    parts = body.split("/")
    shape = (
        f"sites selector is 'sites:<block>/<seam>/s<i>..s<j>' (hexfold seam "
        f"sites i..j inclusive), got {s!r} — vocabulary: {VOCABULARY}"
    )
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise MeasureError(shape)
    block, seam, span = (p.strip() for p in parts)
    m = _SITES_RE.match(span)
    if m is None:
        raise MeasureError(shape)
    lo, hi = int(m["lo"]), int(m["hi"])
    if lo > hi:
        raise MeasureError(f"sites range s{lo}..s{hi} runs backwards in {s!r}")
    return Selector(kind="sites", instance=block, seam=seam, lo=lo, hi=hi)


def _parse_atoms(s: str, body: str) -> Selector:
    """``atoms:<block>[<indices>]`` — comma-separated ordinals and
    inclusive ``a-b`` ranges, at least one."""
    shape = (
        f"atoms selector is 'atoms:<block>[<indices>]' (atom ordinals, e.g. "
        f"[0,3,5-9]), got {s!r} — vocabulary: {VOCABULARY}"
    )
    m = _ATOMS_RE.match(body)
    if m is None or not m["block"].strip():
        raise MeasureError(shape)
    items = [i.strip() for i in m["idx"].split(",")]
    if not items or any(not i for i in items):
        raise MeasureError(shape)
    out: set[int] = set()
    for item in items:
        im = _INDEX_ITEM_RE.match(item)
        if im is None:
            raise MeasureError(shape)
        lo = int(im["lo"])
        hi = int(im["hi"]) if im["hi"] is not None else lo
        if lo > hi:
            raise MeasureError(f"atoms range {item!r} runs backwards in {s!r}")
        if hi - lo >= _MAX_ATOMS or len(out) + (hi - lo + 1) > _MAX_ATOMS:
            raise MeasureError(
                f"atoms selector names more than {_MAX_ATOMS} atoms in {s!r} — "
                "address a region that large with a patch or a sites span"
            )
        out.update(range(lo, hi + 1))
    return Selector(
        kind="atoms", instance=m["block"].strip(), indices=tuple(sorted(out))
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
    corners_local = np.array(aabb_corners(lo, hi))
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
    pin: str | None = None,
) -> ResolvedDatum:
    """Resolve a selector against the live tree. ``None``/``'frame'``
    is the NULL-datum default — the block's pose frame. Never raises on
    existence; a miss comes back as ``error``. ``pin`` is the measure's
    ``datum_pin``: for a ``sites:``/``atoms:`` selector whose block is
    bound to a different structure version now, the stale note replaces
    the usual "not loaded" one."""
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
    if sel.kind in ("sites", "atoms"):
        note = _atomic_region_note(tree, sel, pin)
        return ResolvedDatum(
            selector=text,
            kind=sel.kind,
            error=note,
            expected=_atomic_note_is_expected(tree, sel),
        )
    if sel.kind in ("patch", "ring"):
        target = sel.instance or node.name
        placed = _placed(tree, target, env_override)
        if placed is None:
            return err(f"block {target!r} has no parseable envelope")
        frame = _face_frame(placed, sel.tag or "")
        if frame is None:
            tags = ", ".join(sorted(_face_geometry(placed))) or "none"
            return err(f"no face tagged {sel.tag!r} on block {target!r} (has: {tags})")
        p_l, n_l, u_l, v_l, (u_lo, u_hi), (v_lo, v_hi), is_rect = frame
        if sel.kind == "ring":
            return ResolvedDatum(
                selector=text,
                kind="ring",
                resolved=f"ring:{target}.{sel.tag}",
                point=placed.xform.R @ p_l + placed.xform.t,
                normal=placed.xform.apply_dir(n_l),
            )
        u = float(sel.u or 0.0)
        v = float(sel.v or 0.0)
        slack = 1e-9 * max(u_hi - u_lo, v_hi - v_lo, 1e-30)
        if not (
            u_lo - slack <= u <= u_hi + slack and v_lo - slack <= v <= v_hi + slack
        ):
            return err(
                f"patch centre ({u:g}, {v:g}) lies off face {target}.{sel.tag}, "
                f"which spans u∈[{u_lo:g}, {u_hi:g}], v∈[{v_lo:g}, {v_hi:g}] m "
                "(the face's bounding extent)"
            )
        centre_l = p_l + u * u_l + v * v_l
        notes: list[str] = []
        flags: set[str] = set()
        half_w, half_h = float(sel.w or 0.0) / 2.0, float(sel.h or 0.0) / 2.0
        if (
            u - half_w < u_lo - slack
            or u + half_w > u_hi + slack
            or v - half_h < v_lo - slack
            or v + half_h > v_hi + slack
        ):
            notes.append(
                f"patch {_fmt_pair(sel.w, sel.h)} extends past face "
                f"{target}.{sel.tag} ({_fmt_pair(u_hi - u_lo, v_hi - v_lo)}) "
                "— units?"
            )
            flags.add("patch_exceeds_face")
        if not is_rect:
            notes.append(
                f"bounds approximate: face {target}.{sel.tag} is not a "
                "rectangle, so the check uses its bounding extent"
            )
            flags.add("bounds_approximate")
        return ResolvedDatum(
            selector=text,
            kind="patch",
            resolved=_selector_text(replace(sel, instance=target)),
            point=placed.xform.R @ centre_l + placed.xform.t,
            normal=placed.xform.apply_dir(n_l),
            notes=tuple(notes),
            flags=frozenset(flags),
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


def _u_axis(n: Any) -> Any:
    """The unit in-plane ``u`` axis of a face with local unit normal ``n``:
    block-local ``+x`` projected onto the face, or ``+y`` projected when
    ``n`` is within :data:`U_FALLBACK_DEG` of ±x (``|n·x̂| ≥ cos`` of it)."""
    if abs(float(n[0])) >= _U_FALLBACK_COS:
        u = np.array([0.0, 1.0, 0.0]) - float(n[1]) * n
    else:
        u = np.array([1.0, 0.0, 0.0]) - float(n[0]) * n
    return u / float(np.linalg.norm(u))


def _polygon_is_rectangle(ring: Any, u: Any, v: Any) -> bool:
    """Is the face polygon ``ring`` (local vertices) a rectangle in the
    ``u``/``v`` frame — its area within :data:`_RECT_AREA_RTOL` of its
    bounding rectangle's?"""
    pts = np.asarray(ring, dtype=float)
    us, vs = pts @ u, pts @ v
    bbox = float(us.max() - us.min()) * float(vs.max() - vs.min())
    if bbox <= 0.0:
        return False
    avec = np.zeros(3)
    for j in range(len(pts)):
        avec += np.cross(pts[j], pts[(j + 1) % len(pts)])
    area = float(np.linalg.norm(avec)) / 2.0
    return abs(area - bbox) <= _RECT_AREA_RTOL * bbox


def _face_frame(placed: Placed, tag: str) -> FaceFrame | None:
    """Face ``tag``'s block-LOCAL frame: plane point (the ray exit along
    the normal from the AABB centre — :func:`_face_geometry`'s point),
    unit normal, the in-plane ``u``/``v`` axes (:func:`_u_axis`, module
    docstring's convention), the face's bounding extent along each,
    measured from the plane point over the local AABB corners, and whether
    the face is known to be a rectangle (only a ``PolyFrustum`` face
    polygon can say so; a disc or any non-polygon face is not). ``None``
    when no planar face carries the tag."""
    prim = placed.prim
    lo, hi = prim.aabb_local()
    centre = (as_vec3(lo) + as_vec3(hi)) / 2.0
    polys = getattr(prim, "_face_polys", None)
    for i, f in enumerate(prim.faces_local()):
        if f.tag != tag:
            continue
        n = as_vec3(f.normal)
        n = n / float(np.linalg.norm(n))
        hits = prim.ray_hits_local(centre, n)
        if not hits:
            return None
        p = centre + max(b for _a, b in hits) * n
        u = _u_axis(n)
        v = np.cross(n, u)
        corners = np.array(aabb_corners(lo, hi)) - p
        us, vs = corners @ u, corners @ v
        is_rect = (
            isinstance(prim, PolyFrustum)
            and polys is not None
            and i < len(polys)
            and _polygon_is_rectangle(polys[i][1], u, v)
        )
        return FaceFrame(
            p,
            n,
            u,
            v,
            (float(us.min()), float(us.max())),
            (float(vs.min()), float(vs.max())),
            bool(is_rect),
        )
    return None


class FaceFrame(NamedTuple):
    """:func:`_face_frame`'s result: plane point, normal, ``u``, ``v``, the
    bounding extent ``(lo, hi)`` along ``u`` and ``v``, ``is_rectangle``."""

    point: Any
    normal: Any
    u: Any
    v: Any
    u_range: tuple[float, float]
    v_range: tuple[float, float]
    is_rectangle: bool


def _fmt_pair(a: Any, b: Any) -> str:
    """``8 × 4 m`` / ``0.8 × 0.4 nm`` — two metre lengths through the neat
    formatter, the unit shown once when both share it."""
    fa = format_quantity(float(a or 0.0), "length")
    fb = format_quantity(float(b or 0.0), "length")
    na, _, ua = fa.rpartition(" ")
    nb, _, ub = fb.rpartition(" ")
    if na and nb and ua == ub:
        return f"{na} × {nb} {ua}"
    return f"{fa} × {fb}"


def _index_text(indices: tuple[int, ...]) -> str:
    """Sorted ordinals back to ``0,3,5-9`` — runs of three or more
    collapse to a range."""
    out: list[str] = []
    i = 0
    while i < len(indices):
        j = i
        while j + 1 < len(indices) and indices[j + 1] == indices[j] + 1:
            j += 1
        if j - i >= 2:
            out.append(f"{indices[i]}-{indices[j]}")
        else:
            out.extend(str(x) for x in indices[i : j + 1])
        i = j + 1
    return ",".join(out)


def same_region(a: str | None, b: str | None) -> bool:
    """Do two selector strings name the same region? Compared parsed,
    so ``patch:b.top@0,0+1e-3x1e-3`` and ``…@0.0,0.0+0.001x0.001`` agree;
    an unparseable string compares as text."""
    if a is None or b is None:
        return a == b
    try:
        return parse_selector(a) == parse_selector(b)
    except MeasureError:
        return a.strip() == b.strip()


def atomic_owner(tree: SeTree, sel: Selector) -> SeBlock | None:
    """The block whose binding a ``sites:``/``atoms:`` selector indexes
    into: the named block, or its template when it is an instance. ``None``
    when the named block does not exist."""
    node = tree.blocks.get(sel.instance or "")
    if node is None:
        return None
    owner = tree.blocks.get(node.template) if node.template else node
    return owner or node


def _atomic_note_is_expected(tree: SeTree, sel: Selector) -> bool:
    """Is :func:`_atomic_region_note`'s text the lenient kind (anything but
    a missing block — an unbound or loaded-elsewhere region is declared
    intent, not a broken reference)?"""
    return atomic_owner(tree, sel) is not None


_PIN_RE = re.compile(r"^(?P<slug>[^@\s]+)@v(?P<ver>\d+)$")


def pin_text(slug: str, version: int) -> str:
    """``"<structure-slug>@v<version>"`` — :attr:`MeasureSpec.datum_pin`."""
    return f"{slug}@v{version}"


def parse_pin(text: str) -> tuple[str, int] | None:
    """A ``datum_pin`` back to ``(slug, version)``; ``None`` if malformed."""
    m = _PIN_RE.match(text.strip())
    return None if m is None else (m["slug"], int(m["ver"]))


def stale_pin_note(tree: SeTree, spec: MeasureSpec) -> str | None:
    """The note a measure's pinned ``sites:``/``atoms:`` datum earns when
    its block is bound to a different structure (or a later version of it)
    than the indices were declared against; ``None`` when the pin is
    current, absent, not applicable, or cannot be checked (a bare tree
    with no :attr:`SeTree.structure_versions`, or a structure gone from the
    store — nothing to compare against, never a guess)."""
    if spec.datum_pin is None or spec.datum is None:
        return None
    try:
        sel = parse_selector(spec.datum)
    except MeasureError:
        return None
    return _stale_for(tree, sel, spec.datum_pin)


def _stale_for(tree: SeTree, sel: Selector, pin: str) -> str | None:
    pinned = parse_pin(pin)
    if pinned is None or sel.kind not in ("atoms", "sites"):
        return None
    owner = atomic_owner(tree, sel)
    if owner is None:
        return None  # a missing block is datum_unresolved's finding
    pin_slug, pin_ver = pinned
    cur = owner.bound if owner.bound_kind == "structure" and owner.bound else None
    versions = tree.structure_versions
    cur_ver = versions(cur) if (versions is not None and cur is not None) else None
    if cur is None:
        now = "the block is now bound to no structure"
    elif cur != pin_slug:
        label = pin_text(cur, cur_ver) if cur_ver is not None else cur
        now = f"the block is now bound to {label}"
    elif cur_ver is None or cur_ver == pin_ver:
        return None
    else:
        now = f"the block is now bound to {pin_text(cur, cur_ver)}"
    return (
        f"{sel.kind}: pinned to {pin_text(pin_slug, pin_ver)}, {now} — "
        "indices may name different atoms; re-declare the region"
    )


def snapshot_measure_datums(tree: SeTree) -> dict[int, tuple[MeasureSpec, str | None]]:
    """``id(measure) → (measure, datum)`` before a batch of ops — the
    baseline :func:`stamp_region_pins` compares against. Holds the measure
    objects so ids cannot be reused by a measure minted mid-batch."""
    return {id(m): (m, m.datum) for m in tree.measures}


def stamp_region_pins(
    tree: SeTree,
    before: dict[int, tuple[MeasureSpec, str | None]],
    version_of: Callable[[str], int | None],
) -> None:
    """Stamp ``datum_pin`` on every ``atoms:``/``sites:`` measure that was
    written (minted, or its datum changed) since ``before`` and has no pin
    yet, from the block's bound structure NOW. A block that binds no
    structure (or one ``version_of`` cannot find) gets no pin — the
    lenient note covers it. A pin the caller passed explicitly is kept
    (an ops-export replay), and a measure nobody touched is never
    re-pinned, so an old stale pin cannot be silently refreshed."""
    for m in tree.measures:
        if m.datum_pin is not None or not m.datum:
            continue
        try:
            sel = parse_selector(m.datum)
        except MeasureError:
            continue
        if sel.kind not in ("atoms", "sites"):
            continue
        prior = before.get(id(m))
        if prior is not None and same_region(prior[1], m.datum):
            continue
        owner = atomic_owner(tree, sel)
        if owner is None or owner.bound_kind != "structure" or not owner.bound:
            continue
        version = version_of(owner.bound)
        if version is not None:
            m.datum_pin = pin_text(owner.bound, version)


def _atomic_region_note(tree: SeTree, sel: Selector, pin: str | None = None) -> str:
    """Why a ``sites:``/``atoms:`` selector does not resolve here — the
    lenient-existence note (module docstring): the block is missing, it
    binds no structure, its pin is stale (that note replaces the usual
    one), or (the normal case) its atom coordinates live in the bound
    structure design this pure resolver does not load."""
    name = sel.instance or ""
    node = tree.blocks.get(name)
    if node is None:
        return f"no block {name!r} — {sel.kind}: names the block that owns the atoms"
    if pin is not None:
        stale = _stale_for(tree, sel, pin)
        if stale is not None:
            return stale
    owner = tree.blocks.get(node.template) if node.template else node
    owner = owner or node
    if owner.bound_kind != "structure" or not owner.bound:
        return (
            f"block {name!r} binds no structure design — {sel.kind}: "
            "addresses atoms of a bound structure (bind_structure or "
            "generate first)"
        )
    return (
        f"{sel.kind}: on {name!r} addresses atoms of structure "
        f"{owner.bound!r}, whose coordinates this resolver does not load — "
        "the region is declared; its geometry resolves when a property "
        "computer reads the structure"
    )


def _selector_text(sel: Selector) -> str:
    if sel.kind == "frame":
        return "frame"
    if sel.kind == "port":
        return f"port:{sel.name}"
    if sel.kind == "axis":
        return f"axis:{sel.instance}"
    if sel.kind == "patch":
        u, v, w, h = (repr(float(x or 0.0)) for x in (sel.u, sel.v, sel.w, sel.h))
        return f"patch:{sel.instance}.{sel.tag}@{u},{v}+{w}x{h}"
    if sel.kind == "ring":
        return f"ring:{sel.instance}.{sel.tag}"
    if sel.kind == "sites":
        return f"sites:{sel.instance}/{sel.seam}/s{sel.lo}..s{sel.hi}"
    if sel.kind == "atoms":
        return f"atoms:{sel.instance}[{_index_text(sel.indices)}]"
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


_REGION_KINDS = frozenset({"patch", "ring", "sites", "atoms"})


def region_datum_notes(
    tree: SeTree, block: SeBlock, spec: MeasureSpec
) -> tuple[str, ...]:
    """What a measure with NO derived number still has to say about a
    region datum (``patch:``/``ring:``/``sites:``/``atoms:``): the
    resolver's error (the stale-pin note takes the place of the usual
    "not loaded" one) or its honesty notes (patch past its face, bounds
    approximate). Other datum kinds say nothing — a count/ratio measure
    on a face never evaluated them."""
    if not spec.datum:
        return ()
    try:
        sel = parse_selector(spec.datum)
    except MeasureError as exc:
        return (f"unresolvable datum selector {spec.datum!r}: {exc}",)
    if sel.kind not in _REGION_KINDS:
        return ()
    try:
        datum = resolve(tree, block, sel, pin=spec.datum_pin)
    except MeasureError as exc:
        return (f"unresolvable datum selector {spec.datum!r}: {exc}",)
    if datum.error is not None:
        return (f"datum {spec.datum!r}: {datum.error}",)
    return datum.notes


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
                *region_datum_notes(tree, block, spec),
                f"measure unit is {spec.unit!r}; a feature measurement is a "
                "length in m — nothing to derive",
            ),
        )
    if not is_geometric(spec):
        # A metre-valued measurand that is not a feature distance (an
        # absorption wavelength): the region is its datum, not a ruler.
        return MeasureValue(
            None,
            spec.unit,
            None,
            notes=(
                *region_datum_notes(tree, block, spec),
                f"measurand {measurand_name(spec)!r} is not a feature distance — "
                "nothing to derive from geometry",
            ),
        )
    sel_text = spec.datum or "frame"
    try:
        datum = resolve(
            tree, block, sel_text, env_override=env_override, pin=spec.datum_pin
        )
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
    notes.extend(datum.notes)
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

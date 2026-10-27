"""se observation rays — multiscale §5.4 ``cast_ray`` + §5.5 bookmarks
(structure-workspaces slice 2r.1, docs/backlog/structure-workspaces.md
§Observation rays).

A ray is a **block child** beside ports, measures and pockets (the
:mod:`precis_se.pockets` pattern): a stored *definition*, never an event
list — ``{name, origin, direction, length, kind, fields, step, reach,
frame, anchor, made_by}`` plus the last five observation **digests**
(:func:`digest`: a hash of the event list and a few key numbers). Events
are recomputed on every read; :func:`observe` is pure over the tree and
its resolvers, so the same call answers ``view='ray'``, ``observe_ray``,
the ``~ray:<name>`` eye and a diff against an older digest.

Units: ``origin``/``length`` are metres in the ray's ``frame`` — the
owning block's local frame by default (the ray rides ``set_pose``,
``tile``, ``instance``), ``'world'``, or ``'port:<name>'`` (origin at
the port, ``+z`` its measured axis). ``step`` (sampling) and ``reach``
(how far off the line an atom still counts as passed) are atomic and in
Å.

Dispatch is by block binding along ONE world-frame ray: a
structure-bound block runs the atomic evaluator
(:func:`precis.structure.probe.ray_hits`) in its own frame, any other
block with an envelope the macro kernel
(:func:`precis.cad.probe.ray_hits`); the hits concatenate in ``s``
order. Fields with no data are absent from the header, never a per-hit
gap: ``charge`` waits for slice 4's stored charges, ``relax`` for 4a's
per-atom levels (``args['levels']`` on a service frame — the
:mod:`precis_se.eyeview` scoping contract).

Persisted to ``se_rays`` in lockstep with the owning block row, like
``se_pockets``. Template-owned: an instance's rays are its template's,
evaluated at the instance's pose.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import numpy as np

from precis.structure.probe import RAY_FIELDS

#: Every ``kind`` a ray may carry — §Macro leg's four plus the bare probe.
RAY_KINDS = ("probe", "bolt_axis", "tool_access", "load_path", "manufacturability")
#: Atomic defaults (Å) — spec §Observation rays.
DEFAULT_STEP_A = 0.5
DEFAULT_REACH_A = 4.0
#: Digests kept per ray (newest last).
MAX_DIGESTS = 5
#: Rays per bundle, at most — one observation of a bundle ray costs this
#: many single-ray observations (the per-read and per-import bound).
MAX_BUNDLE = 64
#: One read's hit cap; the overflow line names ``args={'from': …}``.
MAX_HITS = 40
#: Below this a moved hit is unchanged (Å) — float noise across a save.
_MOVE_EPS_A = 1e-3


# The Å<->m crossing lives in atomic.validate (the seam allowlist); that
# module imports ``precis_se.ops``, which imports this one, so bind lazily.
def _m_to_A(x: Any) -> Any:
    from precis_se.atomic.validate import m_to_A

    return m_to_A(x)


def _A_to_m(x: Any) -> Any:
    from precis_se.atomic.validate import A_to_m

    return A_to_m(x)


@dataclass
class RaySpec:
    """One ray's stored definition (module docstring) plus its digests.
    ``fields`` ``None`` = every field; ``frame`` ``None`` = the owning
    block; ``made_by`` ``{'frame': <trace frame id>}`` for a workspace
    ray, ``{'op': 'add_ray', 'call': <ledger seq>}`` for an op one."""

    name: str
    origin: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    direction: list[float] = field(default_factory=lambda: [0.0, 0.0, 1.0])
    length: float = 1e-9
    kind: str = "probe"
    fields: list[str] | None = None
    step: float = DEFAULT_STEP_A
    reach: float = DEFAULT_REACH_A
    frame: str | None = None
    anchor: list[str] | None = None
    made_by: dict[str, Any] | None = None
    #: ``{'radius': m, 'count': n}`` — a ray bundle (``pga.ray.bundle``):
    #: ``n`` parallel rays on a circle, one probe, the union of hits.
    bundle: dict[str, Any] | None = None
    digests: list[dict[str, Any]] = field(default_factory=list)

    def definition(self) -> dict[str, Any]:
        """The ``se_rays.definition`` JSONB — everything but digests;
        ``bundle`` only when set (2r.1 rows and checkpoints carry none)."""
        d = {
            "origin": list(self.origin),
            "direction": list(self.direction),
            "length": self.length,
            "kind": self.kind,
            "fields": list(self.fields) if self.fields is not None else None,
            "step": self.step,
            "reach": self.reach,
            "frame": self.frame,
            "anchor": list(self.anchor) if self.anchor is not None else None,
            "made_by": dict(self.made_by) if self.made_by is not None else None,
        }
        if self.bundle is not None:
            d["bundle"] = dict(self.bundle)
        return d

    @classmethod
    def from_row(cls, name: str, definition: dict[str, Any], digests: Any) -> RaySpec:
        d = dict(definition or {})
        return cls(
            name=name,
            origin=[float(x) for x in d.get("origin") or [0.0, 0.0, 0.0]],
            direction=[float(x) for x in d.get("direction") or [0.0, 0.0, 1.0]],
            length=float(d.get("length") or 1e-9),
            kind=str(d.get("kind") or "probe"),
            fields=list(d["fields"]) if d.get("fields") is not None else None,
            step=float(d.get("step") or DEFAULT_STEP_A),
            reach=float(d.get("reach") or DEFAULT_REACH_A),
            frame=d.get("frame"),
            anchor=list(d["anchor"]) if d.get("anchor") is not None else None,
            made_by=dict(d["made_by"]) if d.get("made_by") is not None else None,
            bundle=dict(d["bundle"]) if d.get("bundle") is not None else None,
            digests=[dict(x) for x in (digests or [])],
        )


#: Header order of every field a ray can report — the atomic ones, then
#: the macro event list.
_FIELD_ORDER = (*RAY_FIELDS, "solid")


class RayError(ValueError):
    """A ray definition that cannot be built — the op layer re-raises it
    as its own ``OpError``."""


# ── op-side vetting (the op handlers live in precis_se.ops) ──────────────


def _length_m(raw: Any, what: str) -> float:
    from precis.utils.units import parse_quantity

    if isinstance(raw, bool):
        raise RayError(f"{what} must be a length (metres or '12 nm'), got {raw!r}")
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(parse_quantity(str(raw), "length", arg_name=what))
    except Exception as exc:
        raise RayError(f"{what}: {exc}") from exc


def _vec_m(raw: Any, what: str) -> list[float]:
    from precis.utils.units import parse_vector_quantity

    if isinstance(raw, (list, tuple)) and all(
        isinstance(x, (int, float)) and not isinstance(x, bool) for x in raw
    ):
        if len(raw) != 3:
            raise RayError(f"{what} must be a 3-vector, got {raw!r}")
        return [float(x) for x in raw]
    try:
        return [float(x) for x in parse_vector_quantity(raw, "length", arg_name=what)]
    except Exception as exc:
        raise RayError(f"{what}: {exc}") from exc


def _unit_dir(raw: Any, what: str) -> list[float]:
    try:
        v = np.asarray([float(x) for x in raw], dtype=float)
    except (TypeError, ValueError) as exc:
        raise RayError(f"{what} must be a unitless 3-vector, got {raw!r}") from exc
    n = float(np.linalg.norm(v)) if v.shape == (3,) else 0.0
    if v.shape != (3,) or n < 1e-12:
        raise RayError(f"{what} must be a nonzero 3-vector, got {raw!r}")
    return [float(x) for x in v / n]


def vet_fields(raw: Any) -> list[str] | None:
    from precis.structure.probe import RAY_FIELDS

    if raw is None:
        return None
    vals = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    out = [str(v).strip() for v in vals if str(v).strip()]
    bad = sorted(set(out) - set(RAY_FIELDS))
    if bad:
        raise RayError(
            f"unknown ray field(s) {', '.join(bad)} — fields are "
            f"{', '.join(RAY_FIELDS)}"
        )
    return out or None


def apply_fields(spec: RaySpec, op: dict[str, Any], *, partial: bool) -> None:
    """Write the definition keys present in ``op`` onto ``spec``
    (``partial`` = ``set_ray``'s presence-based update)."""
    if "origin" in op or not partial:
        spec.origin = _vec_m(op.get("origin") or [0.0, 0.0, 0.0], "origin")
    if "direction" in op or not partial:
        spec.direction = _unit_dir(op.get("direction") or [0, 0, 1], "direction")
    if "length" in op or not partial:
        if op.get("length") is None:
            raise RayError("a ray needs 'length' (metres or '12 nm' — rays are capped)")
        spec.length = _length_m(op["length"], "length")
        if not spec.length > 0:
            raise RayError(f"length must be > 0, got {op['length']!r}")
    if "kind" in op:
        kind = str(op["kind"] or "probe").strip()
        if kind not in RAY_KINDS:
            raise RayError(
                f"ray kind must be one of {', '.join(RAY_KINDS)}, got {kind!r}"
            )
        spec.kind = kind
    if "fields" in op:
        spec.fields = vet_fields(op["fields"])
    for key in ("step", "reach"):
        if key in op and op[key] is not None:
            val = op[key]
            try:
                num = float(val)
            except (TypeError, ValueError) as exc:
                raise RayError(f"{key} is Å, a bare number > 0, got {val!r}") from exc
            if not num > 0:
                raise RayError(f"{key} must be > 0 Å, got {val!r}")
            setattr(spec, key, num)
    if "frame" in op:
        fr = op["frame"]
        fr = str(fr).strip() if fr is not None else None
        if fr not in (None, "", "world") and not (fr or "").startswith("port:"):
            raise RayError(
                f"ray frame must be omitted (the block), 'world' or "
                f"'port:<name>', got {op['frame']!r}"
            )
        spec.frame = fr or None
    if "anchor" in op:
        a = op["anchor"]
        spec.anchor = [str(x) for x in a] if isinstance(a, (list, tuple)) else None
    if "bundle" in op:
        b = op["bundle"]
        if b is None:
            spec.bundle = None
        else:
            if not isinstance(b, dict) or "radius" not in b or "count" not in b:
                raise RayError(
                    "bundle is {'radius': <length>, 'count': <int ≥ 3>} — n rays "
                    "on a circle of that radius around the axis"
                )
            radius = _length_m(b["radius"], "bundle radius")
            try:
                count = int(b["count"])
            except (TypeError, ValueError) as exc:
                raise RayError(
                    f"bundle count must be an int, got {b['count']!r}"
                ) from exc
            if not radius > 0 or not 3 <= count <= MAX_BUNDLE:
                raise RayError(
                    f"bundle needs radius > 0 and 3 ≤ count ≤ {MAX_BUNDLE}, got {b!r}"
                )
            spec.bundle = {"radius": radius, "count": count}


# ── the world ray ─────────────────────────────────────────────────────────


def _placement(node: Any) -> Any:
    from precis.cad.vec import as_vec3, pose

    return pose(as_vec3(node.pose), as_vec3(node.rot))


def _port_frame(tree: Any, node: Any, port_name: str) -> tuple[np.ndarray, np.ndarray]:
    """``(origin m, 3×3 basis)`` of a port in its block's local frame —
    pose (or the bound atom), ``+z`` the port's axis (``direction``, its
    rotation's z, or bound→axis atom)."""
    from precis.cad.probe import _basis_perp
    from precis.cad.vec import as_vec3, pose
    from precis_se.ops import effective_ports

    port = effective_ports(tree, node).get(port_name)
    if port is None:
        raise RayError(
            f"ray frame 'port:{port_name}': no port {port_name!r} on block "
            f"{node.name!r} (ports: {', '.join(sorted(effective_ports(tree, node)))})"
        )
    origin = np.zeros(3)
    axis: np.ndarray | None = None
    if port.pose is not None:
        origin = np.asarray(port.pose, dtype=float)
        if port.rot is not None:
            axis = np.asarray(
                pose(as_vec3([0, 0, 0]), as_vec3(port.rot)).apply_dir(
                    as_vec3([0.0, 0.0, 1.0])
                ),
                dtype=float,
            )
    if port.direction is not None:
        axis = np.asarray(port.direction, dtype=float)
    if axis is None or float(np.linalg.norm(axis)) < 1e-12:
        raise RayError(
            f"ray frame 'port:{port_name}': the port has no measured axis "
            "(direction or rot) — set one, or use the block frame"
        )
    z = axis / np.linalg.norm(axis)
    u, v = _basis_perp(as_vec3(z))
    return origin, np.column_stack([np.asarray(u), np.asarray(v), z])


def world_ray(tree: Any, block: str, spec: RaySpec) -> tuple[np.ndarray, np.ndarray]:
    """``(origin m, unit direction)`` in world coordinates."""
    from precis.cad.vec import as_vec3

    node = tree.blocks.get(block)
    if node is None:
        raise RayError(f"block {block!r} is not in the design")
    o = np.asarray(spec.origin, dtype=float)
    d = np.asarray(spec.direction, dtype=float)
    if spec.frame == "world":
        return o, d / np.linalg.norm(d)
    if spec.frame and spec.frame.startswith("port:"):
        p0, basis = _port_frame(tree, node, spec.frame[len("port:") :])
        o = p0 + basis @ o
        d = basis @ d
    pl = _placement(node)
    ow = np.asarray(pl.apply(as_vec3(o)), dtype=float)
    dw = np.asarray(pl.apply_dir(as_vec3(d)), dtype=float)
    return ow, dw / np.linalg.norm(dw)


# ── observation ───────────────────────────────────────────────────────────


@dataclass
class SeHit:
    """One event on the world ray. ``s`` is metres from the origin;
    ``atomic`` picks the display unit (Å vs mm); ``key`` is the diff
    identity ``(field, block, subject, event)``; ``nums`` the movable
    numbers a diff reports (``d`` Å, ``q`` e, ``V`` volts)."""

    s: float
    block: str
    field: str
    event: str
    subject: str
    text: str
    atomic: bool
    # ``dataclasses.field`` by module: the ``field`` attribute above shadows
    # the bare name inside this class body.
    nums: dict[str, float] = dataclasses.field(default_factory=dict)
    atom: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.field, self.block, self.subject, self.event)


@dataclass
class Observation:
    name: str
    block: str
    spec: RaySpec
    origin: np.ndarray
    direction: np.ndarray
    hits: list[SeHit]
    fields: list[str]
    end: float | None
    structures: dict[str, int | None]
    pi_keys: dict[str, str]
    blocks_crossed: list[str]
    pi_crossed: list[str]
    pockets_crossed: list[str]
    macro_events: list[Any] = dataclasses.field(default_factory=list)
    kind_lines: list[str] = dataclasses.field(default_factory=list)


def _frame_texts(doc: dict[str, Any] | None) -> tuple[Any, Any]:
    """``(trace, key → hit text)`` for one block's run trace."""
    from precis_se import traceview as tv

    if not doc:
        return None, {}
    tr = tv.Trace(doc)
    texts: dict[str, str] = {}
    for f in tr.frames:
        user = tv.nearest_user(tr, f)
        qual = str(f.get("qual") or "?").split(".")[-1]
        where = (
            f" · {user.get('file')}:{user.get('line')} {str(user.get('qual')).split('.')[-1]}"
            if user is not None and user is not f
            else ""
        )
        texts[str(f["key"])] = f"{qual}#{f['id']} (frame:{f['key']}{where})"
    return tr, texts


def _region_tests(tree: Any, name: str, node: Any, scene: Any) -> list[tuple[str, Any]]:
    """``(subject, inside(points Å, block frame))`` for the block's
    envelope and its shaped pockets (the CSG distance field,
    :mod:`precis.cad.relate`); ``'hull'`` is the bound atoms' convex
    hull; ``mol:``/``flow:`` shapes are not envelopes and are skipped."""
    from precis.cad.relate import component_sdf_np
    from precis_se.ops import effective_envelope
    from precis_se.pockets import HULL
    from precis_se.simp_bridge import _envelope_design

    out: list[tuple[str, Any]] = []

    def sdf_test(dsl: str) -> Any:
        try:
            design, expr = _envelope_design(dsl)
        except Exception:
            return None

        def inside(pts_a: np.ndarray) -> np.ndarray:
            return np.asarray(component_sdf_np(design, expr, _A_to_m(pts_a))) <= 0.0

        return inside

    env = effective_envelope(tree, node)
    if env:
        t = sdf_test(env)
        if t is not None:
            out.append((f"block {name}", t))
    owner = tree.blocks.get(node.template) if node.template else node
    for pname, pocket in sorted(((owner or node).pockets or {}).items()):
        shape = pocket.shape
        if not shape or shape.startswith(("mol:", "flow:")):
            continue
        if shape == HULL:
            try:
                from scipy.spatial import Delaunay

                carts = np.asarray(
                    [scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()]
                )
                tri = Delaunay(carts)
            except Exception:
                continue

            def hull_inside(pts: np.ndarray, tri: Any = tri) -> np.ndarray:
                return np.asarray(tri.find_simplex(pts) >= 0)

            out.append((f"pocket {name}.{pname}", hull_inside))
            continue
        t = sdf_test(shape)
        if t is not None:
            out.append((f"pocket {name}.{pname}", t))
    return out


def observe(
    tree: Any,
    block: str,
    spec: RaySpec,
    *,
    slug: str = "",
    scenes: dict[str, Any] | None = None,
) -> Observation:
    """Recompute ``spec``'s events on the design (module docstring).
    ``scenes`` overrides bound scenes by structure slug — a diff passes
    the scenes at a digest's versions; the live ones come from
    :attr:`SeTree.structures`. A ``bundle`` ray observes each of its
    parallel rays and reports the union (first ``s`` per event)."""
    from precis.cad.vec import as_vec3
    from precis.structure import cache as st_cache
    from precis.structure import probe as st_probe
    from precis.structure import rayview
    from precis_se import eyeview, rays_macro
    from precis_se.atomic.clearance import VDW_RADII_A

    o_w, d_w = world_ray(tree, block, spec)
    length = float(spec.length)
    if spec.bundle:
        return _observe_bundle(tree, block, spec, o_w, d_w, slug=slug, scenes=scenes)
    resolver = getattr(tree, "structures", None)
    traces = getattr(tree, "traces", None)
    pi_res = getattr(tree, "structure_pi", None)
    hits: list[SeHit] = []
    fields: set[str] = set()
    ends: list[float] = []
    versions: dict[str, int | None] = {}
    pi_keys: dict[str, str] = {}
    crossed: list[str] = []
    pi_crossed: list[str] = []
    pockets_crossed: list[str] = []
    # the atomic leg only where a block binds a structure — a macro-only
    # design (and every ray of its bundles) skips it outright
    atomic_blocks = [
        n
        for n, nd in sorted(tree.blocks.items())
        if (tree.blocks.get(nd.template or "") or nd).bound_kind == "structure"
    ]
    for name in atomic_blocks:
        node = tree.blocks[name]
        owner = tree.blocks.get(node.template) if node.template else node
        owner = owner or node
        live = None
        override = None
        if owner.bound_kind == "structure" and owner.bound:
            live = resolver(owner.bound) if resolver is not None else None
            if scenes is not None:
                override = scenes.get(owner.bound)
        if live is None and override is None:
            continue  # a macro block — rays_macro below
        # An override swaps the geometry only; the run trace and meta stay
        # the live ref's (the trace is the design's, not the version's).
        scene: Any = override if override is not None else getattr(live, "scene", None)
        meta = dict(getattr(live, "meta", None) or {})
        bound_slug = str(owner.bound)
        versions[bound_slug] = None if override is not None else meta.get("version")
        inv = _placement(node).inverse()
        o_l = _m_to_A(np.asarray(inv.apply(as_vec3(o_w)), dtype=float))
        d_l = np.asarray(inv.apply_dir(as_vec3(d_w)), dtype=float)
        pi_data: dict[str, Any] | None
        if override is not None:
            pi_data = st_probe.pi_perceive(scene)
        else:
            pi_data = pi_res(owner.bound) if pi_res is not None else None
        if pi_data is not None:
            pi_keys[owner.bound] = st_cache.pi_key(scene)
        gen = meta.get("generated") or {}
        doc = traces(gen.get("trace")) if traces is not None else None
        tr, frame_texts = _frame_texts(doc)
        labels = list(scene.atoms)
        ord_of = {la: i for i, la in enumerate(labels)}

        def frame_of(la: str, tr: Any = tr, scene: Any = scene) -> str | None:
            if tr is None:
                return None
            fr = tr.maker_frame(scene.atoms[la].maker)
            return str(fr["key"]) if fr is not None else None

        bt = SimpleNamespace(doc=doc)  # the slice of _BlockTrace _relax_of reads

        def relax_of(
            la: str, name: str = name, bt: Any = bt, ord_of: dict[str, int] = ord_of
        ) -> str | None:
            text, level, frozen = eyeview._relax_of(name, bt, ord_of[la])
            if level is None:  # per-atom levels are 4a's — nothing to report
                return None
            return " ".join(x for x in (level, text, frozen) if x)

        trace = st_probe.ray_hits(
            scene,
            o_l,
            d_l,
            length=_m_to_A(length),
            fields=spec.fields,
            step=spec.step,
            reach=spec.reach,
            pi_data=pi_data,
            charges=None,  # slice 4 stores per-run charges; until then absent
            frame_of=frame_of,
            relax_of=relax_of,
            regions=_region_tests(tree, name, node, scene),
            vdw_radii=VDW_RADII_A,
        )
        fields.update(trace.fields)
        if trace.end is not None:
            ends.append(_A_to_m(trace.end))
        for h in trace.hits:
            nums: dict[str, float] = {}
            if h.field == "pass":
                nums["d"] = float(h.data["d"])
                if "q" in h.data:
                    nums["q"] = float(h.data["q"])
            if h.field == "charge":
                nums["V"] = float(h.data["V"])
            if h.field == "region" and h.event == "enter":
                where = crossed if h.subject.startswith("block ") else pockets_crossed
                if h.subject.split(" ", 1)[1] not in where:
                    where.append(h.subject.split(" ", 1)[1])
            if h.field == "pass" and name not in crossed:
                crossed.append(name)
            if h.field == "pi" and h.event == "enter":
                sel = f"pi:{name}#{h.data['system']}"
                if sel not in pi_crossed:
                    pi_crossed.append(sel)
            hits.append(
                SeHit(
                    s=_A_to_m(h.s),
                    block=name,
                    field=h.field,
                    event=h.event,
                    subject=h.subject,
                    text=rayview.hit_text(
                        h, prefix=f"{name}#", block=name, frame_texts=frame_texts
                    ),
                    atomic=True,
                    nums=nums,
                    atom=h.subject if h.field == "pass" else None,
                )
            )
    unit = _macro_unit(length)
    events: list[Any] = []
    for g in rays_macro.block_geoms(tree):
        events.extend(rays_macro.block_events(g, o_w, d_w, length, unit))
    events.extend(rays_macro.gap_events(events, unit))
    if spec.kind != "tool_access":  # a tool's reach cares about material only
        events.extend(rays_macro.port_events(tree, o_w, d_w, length, unit))
    for e in events:
        fields.add("solid")
        if e.event == "enter" and e.block not in crossed:
            crossed.append(e.block)
        hits.append(
            SeHit(
                e.s, e.block, "solid", e.event, e.subject, e.text, False, dict(e.nums)
            )
        )
    hits.sort(key=lambda h: (h.s, h.block, h.field))
    order = _FIELD_ORDER
    return Observation(
        name=spec.name,
        block=block,
        spec=spec,
        origin=o_w,
        direction=d_w,
        hits=hits,
        fields=[f for f in order if f in fields],
        end=max(ends) if ends else None,
        structures=versions,
        pi_keys=pi_keys,
        blocks_crossed=crossed,
        pi_crossed=pi_crossed,
        pockets_crossed=pockets_crossed,
        macro_events=sorted(events, key=lambda e: e.s),
        kind_lines=rays_macro.kind_lines(
            tree, spec, sorted(events, key=lambda e: e.s), unit
        ),
    )


def _observe_bundle(
    tree: Any,
    block: str,
    spec: RaySpec,
    o_w: np.ndarray,
    d_w: np.ndarray,
    *,
    slug: str,
    scenes: dict[str, Any] | None,
) -> Observation:
    """A bundle is one probe: each parallel ray observed in world
    coordinates, events merged by identity at their first ``s``."""
    from precis_se import rays_macro

    b = spec.bundle or {}
    parts = []
    count = min(int(b["count"]), MAX_BUNDLE)
    for off in rays_macro.bundle_offsets(d_w, float(b["radius"]), count):
        one = dataclasses.replace(
            spec,
            origin=[float(x) for x in o_w + off],
            direction=[float(x) for x in d_w],
            frame="world",
            bundle=None,
            digests=[],
        )
        parts.append(observe(tree, block, one, slug=slug, scenes=scenes))
    first: dict[tuple[str, str, str, str], SeHit] = {}
    events: dict[tuple[str, str], Any] = {}
    for obs in parts:
        for h in obs.hits:
            if h.key not in first or h.s < first[h.key].s:
                first[h.key] = h
        for e in obs.macro_events:
            k = (e.subject, e.event)
            if k not in events or e.s < events[k].s:
                events[k] = e
    merged = sorted(first.values(), key=lambda h: (h.s, h.block, h.field))
    evs = sorted(events.values(), key=lambda e: e.s)
    unit = _macro_unit(float(spec.length))
    return Observation(
        name=spec.name,
        block=block,
        spec=spec,
        origin=o_w,
        direction=d_w,
        hits=merged,
        fields=sorted({f for o in parts for f in o.fields}, key=[*_FIELD_ORDER].index),
        end=max((o.end for o in parts if o.end is not None), default=None),
        structures=parts[0].structures if parts else {},
        pi_keys=parts[0].pi_keys if parts else {},
        blocks_crossed=sorted({b_ for o in parts for b_ in o.blocks_crossed}),
        pi_crossed=sorted({p for o in parts for p in o.pi_crossed}),
        pockets_crossed=sorted({p for o in parts for p in o.pockets_crossed}),
        macro_events=evs,
        kind_lines=rays_macro.kind_lines(tree, spec, evs, unit),
    )


# ── digest + diff ─────────────────────────────────────────────────────────


def _canonical(obs: Observation) -> list[list[Any]]:
    return [
        [
            *h.key,
            round(_m_to_A(h.s), 3),
            # atomic numbers are Å/e/V already; macro ones are metres — µm
            *(round(h.nums[k] * (1.0 if h.atomic else 1e6), 3) for k in sorted(h.nums)),
        ]
        for h in obs.hits
    ]


def event_hash(obs: Observation) -> str:
    blob = json.dumps(_canonical(obs), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def digest(
    obs: Observation, *, service: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The stored observation digest — never the event list: the hash,
    the structure versions and ``pi_key``s it saw, the charge run (none
    until slice 4) and a few key numbers."""
    first = next((h for h in obs.hits), None)
    d: dict[str, Any] = {
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hash": event_hash(obs),
        "structures": dict(obs.structures),
        "pi_key": dict(obs.pi_keys),
        "charges_run": None,
        "key": {
            "hits": len(obs.hits),
            "first": f"{first.block} {first.text}" if first is not None else None,
            "end_A": round(_m_to_A(obs.end), 3) if obs.end is not None else None,
        },
    }
    if service is not None:
        d["service"] = dict(service)
    return d


def push_digest(spec: RaySpec, dg: dict[str, Any]) -> None:
    spec.digests = [*spec.digests, dg][-MAX_DIGESTS:]


def pick_digest(
    spec: RaySpec, against: Any, *, current_hash: str | None = None
) -> dict[str, Any]:
    """``'last'`` — the last observation that is not this one: the newest
    digest, or the one before it when the newest already records the
    current events (an import or relax re-observes as it writes, so its
    own digest is "now", and the question is what it was before) — or an
    index into the stored digests (``-1`` = newest)."""
    if not spec.digests:
        raise RayError(
            f"ray {spec.name!r} has no stored observation — observe_ray first"
        )
    if against in (None, "", "last"):
        newest = spec.digests[-1]
        if (
            current_hash is not None
            and newest.get("hash") == current_hash
            and len(spec.digests) > 1
        ):
            return spec.digests[-2]
        return newest
    if isinstance(against, str) and against.strip().lower().startswith("v"):
        raise RayError(
            "against='v<n>' (a design revision) resolves through "
            "view='ray' — the revision checkpoint read needs the store; "
            "use 'last' or a digest index here"
        )
    try:
        return spec.digests[int(against)]
    except (TypeError, ValueError, IndexError) as exc:
        raise RayError(
            f"against={against!r}: 'last' or an index into the "
            f"{len(spec.digests)} stored digest(s)"
        ) from exc


def diff_lines(old: Observation, new: Observation) -> list[str]:
    """Hits matched by ``(field, block, subject, event)``: added,
    removed, moved (``Δs``, ``Δd``, ``Δq``, ``ΔV``); the unchanged fold to
    one count line."""
    L = new.spec.length
    before = {h.key: h for h in old.hits}
    after = {h.key: h for h in new.hits}
    moved: list[str] = []
    unchanged = 0
    for key, h in after.items():
        prev = before.get(key)
        if prev is None:
            continue
        mf, mu = (_m_to_A(1.0), "Å") if h.atomic else _macro_unit(L)
        ds = (h.s - prev.s) * mf
        parts = []
        if abs(ds) > (_MOVE_EPS_A if h.atomic else 1e-6):
            parts.append(f"Δs {ds:+.3f} {mu}")
        for k, unit in (("d", "Å"), ("q", "e"), ("V", "V")):
            if h.atomic and k in h.nums and k in prev.nums:
                dv = h.nums[k] - prev.nums[k]
                if abs(dv) > _MOVE_EPS_A:
                    parts.append(f"Δ{k} {dv:+.3f} {unit}")
        for k in ("len", "off", "dia"):
            if not h.atomic and k in h.nums and k in prev.nums:
                dv = (h.nums[k] - prev.nums[k]) * mf
                if abs(dv) > 1e-6:
                    parts.append(f"Δ{k} {dv:+.3f} {mu}")
        if parts:
            moved.append(
                f"  moved   {_fmt_s(h.s, h.atomic, L)}  {h.text}  " + "  ".join(parts)
            )
        else:
            unchanged += 1
    added = [
        f"  added   {_fmt_s(h.s, h.atomic, L)}  {h.text}"
        for k, h in after.items()
        if k not in before
    ]
    removed = [
        f"  removed {_fmt_s(h.s, h.atomic, L)}  {h.text}"
        for k, h in before.items()
        if k not in after
    ]
    head = (
        f"{len(moved)} moved · {len(added)} added · {len(removed)} removed · "
        f"{unchanged} unchanged"
    )
    return [head, *moved, *added, *removed]


# ── render ────────────────────────────────────────────────────────────────


#: Macro display units by ray length (metres): a nm-scale envelope
#: reads in nm, a bracket in mm — atomic hits are always Å.
_MACRO_UNITS = ((1e-3, 1e3, "mm"), (1e-6, 1e6, "µm"), (0.0, 1e9, "nm"))


def _macro_unit(length_m: float) -> tuple[float, str]:
    return next((f, u) for lo, f, u in _MACRO_UNITS if length_m >= lo)


def _fmt_s(s_m: float, atomic: bool, scale: float = 1e-3) -> str:
    """``s`` in Å (atomic) or the ray's macro unit (``scale`` = the ray
    length the unit is picked from)."""
    if atomic:
        return f"{_m_to_A(s_m):6.1f} Å"
    f, u = _macro_unit(scale)
    return f"{s_m * f:6.1f} {u}"


def _fmt_dir(d: np.ndarray) -> str:
    for i, ax in enumerate("xyz"):
        if abs(abs(float(d[i])) - 1.0) < 1e-9:
            return ("+" if d[i] > 0 else "−") + ax
    return "[" + " ".join(f"{float(x):.3g}" for x in d) + "]"


def header(obs: Observation, *, n_hits: int | None = None) -> str:
    from precis.utils.units import format_quantity, format_vector

    spec = obs.spec
    where = f"block {obs.block}"
    if spec.frame:
        where += f" · {spec.frame}"
    kind = f" · kind {spec.kind}" if spec.kind != "probe" else ""
    if spec.bundle:
        f, u = _macro_unit(spec.length)
        kind += (
            f" · bundle {int(spec.bundle['count'])}×r "
            f"{float(spec.bundle['radius']) * f:g} {u}"
        )
    fields = ",".join(obs.fields) if obs.fields else "—"
    n = len(obs.hits) if n_hits is None else n_hits
    return (
        f"ray {obs.name} · {where} · {format_vector(list(spec.origin), 'length')} "
        f"{_fmt_dir(np.asarray(spec.direction))} · "
        f"{format_quantity(spec.length, 'length')}{kind} · fields {fields} · {n} hits"
    )


def field_counts(obs: Observation) -> str:
    counts: dict[str, int] = {}
    for h in obs.hits:
        counts[h.field] = counts.get(h.field, 0) + 1
    return " · ".join(f"{f} {counts[f]}" for f in obs.fields if f in counts)


def hit_lines(obs: Observation, *, start: float | None = None) -> list[str]:
    """The hit list from ``start`` (metres), capped at :data:`MAX_HITS`
    with the overflow naming the next ``from``."""
    rows = [h for h in obs.hits if start is None or h.s >= start - 1e-15]
    lines = [
        f"  {_fmt_s(h.s, h.atomic, obs.spec.length)}  {h.text}" for h in rows[:MAX_HITS]
    ]
    if len(rows) > MAX_HITS:
        nxt = rows[MAX_HITS]
        lines.append(
            f"  … +{len(rows) - MAX_HITS} more — args={{'name': {obs.name!r}, "
            f"'from': '{_fmt_s(nxt.s, nxt.atomic, obs.spec.length).strip()}'}}"
        )
    if obs.end is not None and (start is None or obs.end >= start):
        lines.append(f"  end {_fmt_s(obs.end, True).strip()} (last atom within reach)")
    lines.extend(obs.kind_lines)
    return lines


def render(obs: Observation, *, start: float | None = None) -> str:
    return "\n".join([header(obs), *hit_lines(obs, start=start)])


def find_ray(tree: Any, name: str) -> tuple[str, RaySpec] | None:
    """``(block, spec)`` for a design-wide ray name (names are unique per
    design — the ``<slug>~ray:<name>`` handle carries no block)."""
    for bname, node in tree.blocks.items():
        if node.template is None and name in (node.rays or {}):
            return bname, node.rays[name]
    return None


def all_rays(tree: Any) -> list[tuple[str, RaySpec]]:
    return [
        (bname, spec)
        for bname, node in sorted(tree.blocks.items())
        if node.template is None
        for _n, spec in sorted((node.rays or {}).items())
    ]


def render_rays(tree: Any, slug: str = "") -> str:
    """``view='rays'``: one line per ray — name, block, length, hit
    count, ``Δ`` since the last stored observation."""
    from precis.utils.units import format_quantity

    rows = all_rays(tree)
    if not rows:
        return (
            "no rays on this design — edit(kind='se', id=…, ops=[{'op': "
            "'add_ray', 'block': …, 'name': …, 'origin': …, 'direction': …, "
            "'length': …}])"
        )
    out = [f"{len(rows)} ray(s)"]
    for bname, spec in rows:
        try:
            obs = observe(tree, bname, spec, slug=slug)
            n = str(len(obs.hits))
            if not spec.digests:
                delta = "never observed"
            elif spec.digests[-1].get("hash") == event_hash(obs):
                delta = "Δ none"
            else:
                delta = "Δ changed since last observe"
        except RayError as exc:
            n, delta = "—", f"unavailable ({exc})"
        out.append(
            f"  {spec.name} · block {bname} · "
            f"{format_quantity(spec.length, 'length')} · {n} hits · {delta} · "
            f"{slug}~ray:{spec.name}"
        )
    return "\n".join(out)


def atom_ray_lines(
    tree: Any, slug: str, block: str, label: str, *, cap: int = 4
) -> list[str]:
    """The atom eye's ``Rays:`` lines — every ray with a pass hit on
    ``block#label``: ``axis · s=3.0 Å · d 1.40 · <slug>~ray:axis``."""
    out: list[str] = []
    for bname, spec in all_rays(tree):
        try:
            obs = observe(tree, bname, spec, slug=slug)
        except RayError:
            continue
        for h in obs.hits:
            if h.field == "pass" and h.block == block and h.atom == label:
                out.append(
                    f"  {spec.name} · s={_fmt_s(h.s, True).strip()} · "
                    f"d {h.nums['d']:.2f} · {slug}~ray:{spec.name}"
                )
                break
    if not out:
        return []
    extra = len(out) - cap
    lines = ["Rays:", *out[:cap]]
    if extra > 0:
        lines.append(f"  … +{extra} more — view='rays'")
    return lines


def made_by_text(store: Any, tree: Any, block: str, spec: RaySpec) -> str | None:
    """``made-by:`` for a ray — a workspace ray's maker frame through the
    block's run trace (the block eye's line), an op ray's ``op add_ray
    (call N)``."""
    mb = spec.made_by or {}
    if mb.get("op"):
        call = mb.get("call")
        return f"made-by: op {mb['op']} (call {call if call is not None else '—'})"
    fid = mb.get("frame")
    if fid is None:
        return None
    from precis_se import handler as _h

    node = tree.blocks.get(block)
    if node is None:
        return None
    return _h._made_by_line(store, tree, node, frame_id=int(fid))


def is_finite_length(spec: RaySpec) -> bool:
    return math.isfinite(spec.length) and spec.length > 0

"""``get(kind='se', view='inspect')`` — the one inspect family on a block
(molecular-workspace-code slice 6, docs/backlog/molecular-workspace-code.md
§Inspect).

``args={'block', 'probe': 'ray'|'section'|'overlap'|'thickness', ...}``
probes one block in its own frame, computed on demand — nothing is stored
(stored rays stay ``view='ray'``). A structure-bound block runs the atomic
leg (:mod:`precis.structure.inspectview`: the van der Waals union, Å, the
bound run's trace naming the frames); any other block with a solid runs
:mod:`precis.cad.inspect` on its bound cad design or envelope (metres,
rendered in mm). Lengths are metres (bare numbers) or unit strings, like
``view='ray'``; directions (``axis``/``normal``) are bare vectors;
``target`` narrows an atomic block to a ``frame:<key>`` / ``pi:<id>`` /
``find:<pred>`` / label selection.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np

from precis.blocktree.types import no_block_msg
from precis.cad import inspect as ci
from precis.cad.vec import Transform
from precis.errors import BadInput, NotFound
from precis.structure import inspectview as iv
from precis.utils.units import parse_quantity
from precis_se import rays_macro
from precis_se import traceview as tv
from precis_se.atomic import validate as se_atomic_validate
from precis_se.identity import AmbiguousLabel, resolve_block


def _metres(value: Any) -> float:
    """A user length — bare metres or a unit string — in metres."""
    if isinstance(value, str):
        return float(parse_quantity(value, "length", arg_name="inspect length"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadInput(
            f"lengths are metres (a number) or a unit string, got {value!r}",
            next="e.g. '2 nm', 2e-9 (metres)",
        )
    return float(value)


def render_inspect(tree: Any, slug: str, args: dict[str, Any]) -> str:
    """The ``view='inspect'`` body for design ``slug``'s block (module
    docstring)."""
    base = f"get(kind='se', id={slug!r}, view='inspect'"
    probe = iv.require_probe(args, next_base=base)
    name = str(args.get("block") or "").strip()
    if not name:
        raise BadInput(
            "view='inspect' needs args={'block': <block>}",
            next=f"{base}, args={{'block': '<block>', 'probe': {probe!r}}}) — "
            f"get(kind='se', id={slug!r}, view='tree') lists the blocks",
        )
    try:
        node = resolve_block(tree, name)
    except AmbiguousLabel as exc:
        raise BadInput(
            str(exc), next="name the block by uid ('#<uid>') instead"
        ) from exc
    if node is None:
        raise NotFound(
            no_block_msg(tree, name),
            next=f"get(kind='se', id={slug!r}) lists the blocks",
        )
    owner = tree.blocks.get(node.template or "") or node
    if owner.bound_kind == "structure" and owner.bound:
        return _atomic(tree, base, node, owner, args)
    return _macro(tree, slug, base, node, args)


def _atomic(tree: Any, base: str, node: Any, owner: Any, args: dict[str, Any]) -> str:
    resolver = getattr(tree, "structures", None)
    live = resolver(owner.bound) if resolver is not None else None
    scene = getattr(live, "scene", None)
    if scene is None:
        raise NotFound(
            f"block {node.name!r} binds structure {owner.bound!r}, which has no atoms",
            next=f"get(kind='structure', id={owner.bound!r}) to check the design",
        )
    spec = iv.parse_spec(
        args,
        length=lambda v: se_atomic_validate.m_to_A(_metres(v)),
        next_base=base,
        extra_keys=frozenset({"block"}),
    )
    meta = dict(getattr(live, "meta", None) or {})
    traces = getattr(tree, "traces", None)
    doc = traces((meta.get("generated") or {}).get("trace")) if traces else None
    tr = tv.Trace(doc) if doc else None

    def frame_key(maker: int | None) -> str | None:
        fr = tr.maker_frame(maker) if tr is not None else None
        return str(fr["key"]) if fr is not None else None

    def frame_atoms(key: str) -> set[int] | None:
        if tr is None or key not in tr.by_key:
            return None
        return tr.subtree_ids(key)

    pi_res = getattr(tree, "structure_pi", None)
    needs_pi = spec.probe == "ray" or (spec.target or "").startswith("pi:")
    try:
        res, n = iv.run(
            spec,
            scene,
            frame_key=frame_key if tr is not None else None,
            frame_atoms=frame_atoms if tr is not None else None,
            pi_data=pi_res(owner.bound) if needs_pi and pi_res is not None else None,
        )
    except ValueError as exc:
        raise BadInput(str(exc), next=f"{base}, args={{'probe': ...}})") from exc
    head = f"block {node.name} · {n}/{len(scene.atoms)} atoms · vdW union (Å)"
    return iv.render(spec.probe, res, head=head)


def _unit_vec(v: np.ndarray | None, default: tuple[float, float, float]) -> np.ndarray:
    d = np.asarray(default if v is None else v, dtype=float)
    return d / np.linalg.norm(d)


def _beam_frame(origin: np.ndarray, axis: np.ndarray) -> Transform:
    """Cylinder frame: base disc at ``origin``, +z along ``axis``."""
    seed = (
        np.array([1.0, 0.0, 0.0]) if abs(axis[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    )
    u = np.cross(axis, seed)
    u = u / np.linalg.norm(u)
    return Transform(
        R=np.column_stack([u, np.cross(axis, u), axis]), t=np.asarray(origin, float)
    )


def _macro(tree: Any, slug: str, base: str, node: Any, args: dict[str, Any]) -> str:
    geom = next((g for g in rays_macro.block_geoms(tree) if g.name == node.name), None)
    if geom is None:
        raise BadInput(
            f"block {node.name!r} has no solid to probe (no structure, bound "
            "cad design or envelope)",
            next=f"get(kind='se', id={slug!r}, view='block', args={{'block': "
            f"{node.name!r}}})",
        )
    spec = iv.parse_spec(
        args, length=_metres, next_base=base, extra_keys=frozenset({"block"})
    )
    if spec.target or spec.other or spec.radii:
        raise BadInput(
            "target / with / radii select atoms — this block is a macro solid",
            next="drop them; probe the whole block, or bind a structure for atoms",
        )
    design = geom.design
    expr = design.whole()
    fld = ci.cad_field(design, expr)
    lo, hi = (np.asarray(v, dtype=float) for v in fld.aabb)
    centre = 0.5 * (lo + hi)
    diag = float(np.linalg.norm(hi - lo))
    z = (0.0, 0.0, 1.0)
    head = f"block {node.name} · {geom.source} (mm)"
    probe = spec.probe
    res: Any
    if probe == "ray":
        d = _unit_vec(spec.axis, z)
        o = spec.origin if spec.origin is not None else centre - d * (0.5 * diag + 1e-6)
        res = ci.ray_field(
            fld,
            origin_m=ci._vec3f(o),
            axis_m=ci._vec3f(d),
            t_max_m=spec.length,
            profile_samples=spec.profile_samples,
            max_steps=spec.max_steps,
        )
        if spec.radius:
            beam = ci.cylinder_field(o, d, spec.radius, spec.length or 2.0 * diag)
            res = replace(res, radius_m=spec.radius, swept=ci.overlap_fields(fld, beam))
    elif probe == "section":
        res = ci.section_field(
            fld,
            origin_m=ci._vec3f(spec.origin if spec.origin is not None else centre),
            normal_m=ci._vec3f(_unit_vec(spec.normal, z)),
            pitch_m=spec.pitch,
        )
    elif probe == "thickness":
        n = _unit_vec(spec.normal, z)
        top = centre + n * float(np.abs((hi - lo) @ n)) * 0.5
        res = ci.thickness_field(
            fld,
            origin_m=ci._vec3f(spec.origin if spec.origin is not None else top),
            normal_m=ci._vec3f(n),
            pitch_m=spec.pitch if spec.pitch is not None else diag / 40.0,
            nominal_m=spec.nominal,
            ratio=spec.ratio,
            method=spec.method,
        )
    else:
        if spec.radius is None:
            raise BadInput(
                "probe='overlap' on a macro block needs a probe cylinder: radius= "
                "(+ origin/axis/length)",
                next=f"{base}, args={{'block': {node.name!r}, 'probe': 'overlap', "
                "'radius': '2 mm', 'origin': [...], 'axis': [0, 0, 1], "
                "'length': '10 mm'}})",
            )
        d = _unit_vec(spec.axis, z)
        length = spec.length or diag
        o = spec.origin if spec.origin is not None else centre - d * 0.5 * length
        cut = ci.probe_solid(
            design,
            "inspect-beam",
            f"cyl:r{spec.radius * 1e3:.9g}mmh{length * 1e3:.9g}mm",
            _beam_frame(o, d),
        )
        res = ci.overlap(design, expr, cut, grid=spec.grid)
    return iv.render(probe, res, head=head, scale=1e3, unit="mm")

"""``view='suggest_checks'`` — checks derived from the design's own
declarations (gr521590).

``validate``/``drc`` run fixed rules; observation rays and region
measurands are hand-authored, so a verify loop starts blank. This
read-only view proposes the checks the design already implies:

- each threaded port (a ``thread`` annotation that resolves to a size)
  ⇒ a ``bolt_axis`` ray down the port axis — the same shape the bracket
  fixture authors by hand: ``frame='port:<name>'``, starting a lead above
  the port and running the thread depth plus margin;
- each shaped pocket (``add_pocket`` with a cad-DSL ``shape``) ⇒ a
  ``region``-field probe through the shape's longest axis, and each cut
  feature (a ``Diff`` cutter leaf in a block's bound cad design) ⇒ a
  probe down the cutter's axis whose ``void`` event proves the void;
- each measure DRC's ``measurand_unchecked`` flags ⇒ a ``set_measure``
  swap to a *checked* measurand of the same se unit (the swap is refused
  at write when the units would contradict), or the honest "nothing
  checks this unit" when the registry covers none.

Every suggestion renders the full call that runs it —
``edit(kind='se', id=…, ops=[…])`` to write, ``get(kind='se', id=…,
view='ray', args=…)`` to probe without writing — paste, never invented
syntax. A declaration already covered (a ``bolt_axis`` ray on the port;
a ray on the block under the suggestion's name, or with the same origin
and direction within 1e-9) drops out, so applying a suggestion makes the
next read go quiet. Nothing is written and no event is computed; the
suggestions are geometry-free except where a shape/cutter extent places
the probe. Output is capped at :data:`TOP_N` per kind with the rest
counted, never dropped silently.
"""

from __future__ import annotations

import re
from difflib import get_close_matches
from typing import Any

#: Suggestions shown per kind; the remainder folds into a "+N more" line.
TOP_N = 6

#: A pocket's embedded shape string truncates past this width.
_SHAPE_CAP = 80

#: ``add_ray`` reserves these in ray names (the ``<slug>~ray:<name>``
#: handle) — generated names fold them to '-'.
_RAY_NAME_BAD = re.compile(r"[.~:]")


def _f(x: float) -> float:
    """Trim a float for the snippet — ``0.036``, not
    ``0.036000000000000004`` (6 significant digits is past anything a
    hand-authored ray carries)."""
    return float(f"{x:.6g}")


def _v3(v: Any) -> list[float]:
    return [_f(float(c)) for c in v]


def _op_line(slug: str, op: dict[str, Any]) -> str:
    return f"edit(kind='se', id={slug!r}, ops=[{op!r}])"


def _ray_get_line(slug: str, args: dict[str, Any]) -> str:
    return f"get(kind='se', id={slug!r}, view='ray', args={args!r})"


class _Proposals:
    """One kind's collected suggestions plus the declarations it had to
    skip (a threaded port with no axis, an unshaped pocket — counted and
    named, never silently absent)."""

    def __init__(self) -> None:
        self.items: list[
            tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]
        ] = []
        self.skipped: list[str] = []

    def add(
        self,
        subject: str,
        why: str,
        *,
        op: dict[str, Any] | None = None,
        ray_args: dict[str, Any] | None = None,
    ) -> None:
        self.items.append((subject, why, op, ray_args))


def _fresh_name(base: str, taken: set[str]) -> str:
    name = base
    k = 2
    while name in taken:
        name = f"{base}-{k}"
        k += 1
    taken.add(name)
    return name


def _ray_name(prefix: str, label: str) -> str:
    """The generated ray's natural name — a label may carry a namespace
    ``.`` (a spliced ``use`` instance) or ``~`` (a spliced payload), both
    reserved by ``add_ray``, so they fold to '-'."""
    return _RAY_NAME_BAD.sub("-", f"{prefix}-{label}")


def _cap(s: str) -> str:
    return s if len(s) <= _SHAPE_CAP else s[:_SHAPE_CAP] + "…"


def _probed(node: Any, base: str, args: dict[str, Any]) -> bool:
    """``True`` when the block already carries this probe: a ray of the
    suggestion's name, or one whose origin and direction match within
    1e-9 — either way the check is already there and the view goes
    quiet."""
    rays = node.rays or {}
    if base in rays:
        return True
    o, d = args["origin"], args["direction"]
    for spec in rays.values():
        if len(spec.origin) != len(o) or len(spec.direction) != len(d):
            continue
        if all(
            abs(float(a) - float(b)) <= 1e-9 for a, b in zip(o, spec.origin)
        ) and all(abs(float(a) - float(b)) <= 1e-9 for a, b in zip(d, spec.direction)):
            return True
    return False


def _bolt_suggestions(tree: Any, taken: set[str]) -> _Proposals:
    """One ``bolt_axis`` ray per threaded port, in the port's own frame —
    ``+z`` the port axis (into the material by the add_port convention),
    so the ray starts ``lead`` above it and crosses the stack on the way
    in. ``lead`` covers the unknown clamp height: the deeper of the
    thread's own depth or two diameters — the same headroom the fixture
    rays carry."""
    from precis_se import rays as se_rays
    from precis_se.ops import effective_ports
    from precis_se.rays_macro import thread_diameter_m

    out = _Proposals()
    for bname in sorted(tree.blocks):
        node = tree.blocks[bname]
        if node.template is not None:
            continue  # rays (and ports) live on the template
        covered = {
            spec.frame.split(":", 1)[1]
            for spec in (node.rays or {}).values()
            if spec.kind == "bolt_axis"
            and spec.frame
            and spec.frame.startswith("port:")
        }
        for pname, port in sorted(effective_ports(tree, node).items()):
            ann = dict(port.annotations or {})
            thread = ann.get("thread")
            if not thread:
                continue
            dia = thread_diameter_m(thread)
            if dia is None and ann.get("diameter") is not None:
                dia = float(ann["diameter"])
            if not dia or dia <= 0:
                out.skipped.append(f"{bname}.{pname} (thread {thread!r} has no size)")
                continue
            if pname in covered:
                continue
            try:
                se_rays._port_frame(tree, node, pname)
            except Exception:
                out.skipped.append(f"{bname}.{pname} (no port axis)")
                continue
            depth = float(ann.get("depth") or 2.0 * dia)
            lead = max(depth, 2.0 * dia)
            length = lead + depth + 4.0 * dia
            name = _fresh_name(_ray_name("bolt", pname), taken)
            ray = {
                "block": bname,
                "frame": f"port:{pname}",
                "origin": _v3([0.0, 0.0, -lead]),
                "direction": [0, 0, 1],
                "length": _f(length),
                "kind": "bolt_axis",
            }
            op = {"op": "add_ray", "name": name, "anchor": [f"{bname}.{pname}"], **ray}
            why = f"{thread} · depth {_f(depth * 1e3):g} mm"
            out.add(f"{bname}.{pname}", why, op=op, ray_args=ray)
    return out


def _aabb(instances: Any) -> Any:
    """Union aabb ``(lo, hi)`` over a design's placed instances; ``None``
    when empty or non-finite."""
    import numpy as np

    los, his = [], []
    for inst in instances:
        lo, hi = inst.placed.aabb()
        lo, hi = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)
        if not (np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))):
            return None
        los.append(lo)
        his.append(hi)
    if not los:
        return None
    return np.min(np.stack(los), axis=0), np.max(np.stack(his), axis=0)


def _region_probe_args(
    bname: str, lo: Any, hi: Any, axis: Any, *, fields: list[str] | None
) -> dict[str, Any] | None:
    """Block-frame ``view='ray'`` args through ``(lo, hi)`` along unit
    ``axis``: origin a quarter-span before the near face, length a span
    and a half — the margin keeps enter/leave events visible."""
    import numpy as np

    axis = np.asarray(axis, dtype=float)
    ext_dir = float(np.abs(np.asarray(hi - lo, dtype=float) @ axis))
    if ext_dir < 1e-12:
        return None
    centre = (np.asarray(lo, dtype=float) + np.asarray(hi, dtype=float)) / 2.0
    origin = centre - 0.75 * ext_dir * axis
    args: dict[str, Any] = {
        "block": bname,
        "origin": _v3(origin),
        "direction": _v3(axis),
        "length": _f(1.5 * ext_dir),
    }
    if fields is not None:
        args["fields"] = list(fields)
    return args


def _pocket_suggestions(tree: Any, taken: set[str]) -> _Proposals:
    """A ``region``-field ray through each shaped pocket's aabb, along its
    longest axis — on a structure-bound block the ``region`` field names
    the pocket entered/left; on a macro block the solid events say what
    fills it. Unshaped pockets (suggestive by contract) and shapes the
    ray layer itself can't bound (``hull``, ``mol:``, ``flow:`` — the
    same set :func:`precis_se.rays._region_tests` skips) are counted."""
    from precis_se.pockets import HULL
    from precis_se.simp_bridge import _envelope_design

    out = _Proposals()
    for bname in sorted(tree.blocks):
        node = tree.blocks[bname]
        if node.template is not None:
            continue
        for pname, pocket in sorted((node.pockets or {}).items()):
            shape = pocket.shape
            if not shape or shape == HULL or shape.startswith(("mol:", "flow:")):
                out.skipped.append(
                    f"{bname}.{pname} ({'unshaped' if not shape else shape.split(':', 1)[0]})"
                )
                continue
            try:
                design, _expr = _envelope_design(shape)
            except Exception:
                out.skipped.append(f"{bname}.{pname} (shape {shape!r} doesn't parse)")
                continue
            box = _aabb(design.instances.values())
            if box is None:
                out.skipped.append(f"{bname}.{pname} (unbounded shape)")
                continue
            lo, hi = box
            ext = hi - lo
            axis = [0.0, 0.0, 0.0]
            axis[int(ext.argmax())] = 1.0
            args = _region_probe_args(bname, lo, hi, axis, fields=["region"])
            if args is None:
                out.skipped.append(f"{bname}.{pname} (zero extent)")
                continue
            base = _ray_name("region", pname)
            if _probed(node, base, args):
                continue
            name = _fresh_name(base, taken)
            op = {"op": "add_ray", "name": name, **args}
            out.add(f"pocket {bname}.{pname}", _cap(shape), op=op, ray_args=args)
    return out


def _cutter_iids(expr: Any) -> Any:
    """Every leaf iid used as a cutter anywhere in a component expression
    — a nested ``Diff`` inside a cutter counts too."""
    from precis.cad.fold import Diff, Inter, Leaf, Union

    def leaves(e: Any) -> Any:
        if isinstance(e, Leaf):
            yield e.iid
        elif isinstance(e, Diff):
            yield from leaves(e.base)
            for c in e.cutters:
                yield from leaves(c)
        elif isinstance(e, (Union, Inter)):
            for p in e.parts:
                yield from leaves(p)

    def walk(e: Any) -> Any:
        if isinstance(e, Diff):
            for c in e.cutters:
                yield from leaves(c)
                yield from walk(c)
            yield from walk(e.base)
        elif isinstance(e, (Union, Inter)):
            for p in e.parts:
                yield from walk(p)

    yield from walk(expr)


def _cutter_suggestions(tree: Any, taken: set[str]) -> _Proposals:
    """A probe down each cut feature of a cad-bound block: the cutter's
    own ``+z`` (a bore's axis) through its aabb — the ray's ``void
    <block>.<feature>`` event then proves the void exists where the
    feature declared it, with the measured ``⌀``/length/axis offset."""
    import numpy as np

    from precis.cad.vec import as_vec3

    out = _Proposals()
    cad_res = getattr(tree, "cad_designs", None)
    if cad_res is None:
        return out
    for bname in sorted(tree.blocks):
        node = tree.blocks[bname]
        if node.template is not None:
            continue
        if node.bound_kind != "cad" or not node.bound:
            continue
        got = cad_res(node.bound)
        if not got:
            continue
        design, _meta = got
        iids: set[str] = set()
        for expr in design.components.values():
            iids.update(_cutter_iids(expr))
        for iid in sorted(iids):
            inst = design.instances.get(iid)
            if inst is None:
                continue
            axis = np.asarray(
                inst.placed.xform.apply_dir(as_vec3([0.0, 0.0, 1.0])), dtype=float
            )
            n = float(np.linalg.norm(axis))
            if n < 1e-12:
                out.skipped.append(f"{bname}.{inst.label} (no cutter axis)")
                continue
            axis = axis / n
            box = _aabb([inst])
            if box is None:
                out.skipped.append(f"{bname}.{inst.label} (unbounded cutter)")
                continue
            lo, hi = box
            args = _region_probe_args(bname, lo, hi, axis, fields=None)
            if args is None:
                out.skipped.append(f"{bname}.{inst.label} (zero extent)")
                continue
            base = _ray_name("cut", inst.label)
            if _probed(node, base, args):
                continue
            name = _fresh_name(base, taken)
            op = {"op": "add_ray", "name": name, **args}
            out.add(
                f"cut {bname}.{inst.label}",
                "void present where the feature declared it",
                op=op,
                ray_args=args,
            )
    return out


def _measurand_suggestions(tree: Any) -> _Proposals:
    """A ``set_measure`` to a checked measurand of the same se unit for
    every measure :func:`precis_se.properties.is_checked` would let DRC
    flag ``measurand_unchecked`` — legacy units map to the seeded legacy
    nodes (``length`` is computed by the datum evaluator), the computer
    registry's slugs resolve through the tree's own measurand resolver.
    A flagged measure whose unit nothing checks gets the roster instead
    of a bad swap."""
    from precis_se.measures import LEGACY_MEASURANDS, measurand_name
    from precis_se.properties import COMPUTERS, is_checked

    out = _Proposals()
    resolver = getattr(tree, "measurands", None)
    for m in tree.measures:
        if is_checked(m.measurand):
            continue
        flagged = measurand_name(m) or m.measurand or "?"
        unit = m.unit
        cands = []
        if resolver is not None:
            for slug in sorted(COMPUTERS):
                try:
                    msr = resolver(slug)
                except Exception:
                    continue
                if msr.unit == unit:
                    cands.append(slug)
            if cands:
                cands = get_close_matches(flagged, cands, n=len(cands), cutoff=0.0)
        legacy = LEGACY_MEASURANDS.get(unit)
        if legacy:
            cands.append(legacy)
        subject = f"{m.block}.{m.name} · measurand {flagged!r}"
        if unit:
            subject += f" [{unit}]"
        if cands:
            why = (
                "measurand_unchecked — the swap changes what is measured, "
                "not just the unit; checked same-unit measurand(s): " + ", ".join(cands)
            )
            op = {
                "op": "set_measure",
                "block": m.block,
                "name": m.name,
                "measurand": cands[0],
            }
            out.add(subject, why, op=op)
        else:
            checked = ", ".join(sorted(COMPUTERS) + sorted(LEGACY_MEASURANDS.values()))
            out.add(
                subject,
                f"measurand_unchecked — no checked measurand in unit "
                f"{unit or '(categorical)'!r} (checked: {checked})",
            )
    return out


def suggest_checks(
    tree: Any,
) -> list[tuple[str, _Proposals]]:
    """``(title, proposals)`` per kind, in check order: fasteners first
    (a bolt that bottoms out breaks the build), then region/void probes,
    then the measurand swaps."""
    from precis_se import rays as se_rays

    taken = {spec.name for _b, spec in se_rays.all_rays(tree)}
    bolts = _bolt_suggestions(tree, taken)
    pockets = _pocket_suggestions(tree, taken)
    cutters = _cutter_suggestions(tree, taken)
    measurands = _measurand_suggestions(tree)
    return [
        ("bolt axes — threaded ports", bolts),
        ("region probes — pockets & cut features", _merge(pockets, cutters)),
        ("measurands — unchecked", measurands),
    ]


def _merge(a: _Proposals, b: _Proposals) -> _Proposals:
    out = _Proposals()
    out.items = a.items + b.items
    out.skipped = a.skipped + b.skipped
    return out


def render_suggest_checks(tree: Any, slug: str = "") -> str:
    sections = suggest_checks(tree)
    total = sum(len(p.items) for _t, p in sections)
    if not total:
        skipped = [s for _t, p in sections for s in p.skipped]
        tail = (
            " (skipped — no probe can be placed: " + ", ".join(skipped) + ")"
            if skipped
            else ""
        )
        return (
            "nothing to suggest — no threaded ports, shaped pockets, cut "
            f"features or unchecked measurands declared{tail}"
        )
    lines = [
        f"{total} suggested check(s) — read-only, nothing written; each "
        f"line is a ready call (top {TOP_N} per kind):"
    ]
    for title, props in sections:
        if not props.items and not props.skipped:
            continue
        skip = (
            f" · skipped: {', '.join(props.skipped[:3])}"
            + (f" +{len(props.skipped) - 3}" if len(props.skipped) > 3 else "")
            if props.skipped
            else ""
        )
        lines.append(f"{title} ({len(props.items)}{skip}):")
        for subject, why, op, ray_args in props.items[:TOP_N]:
            lines.append(f"  {subject} — {why}")
            if op is not None:
                lines.append(f"    {_op_line(slug, op)}")
            if ray_args is not None:
                lines.append(f"    {_ray_get_line(slug, ray_args)}")
        if len(props.items) > TOP_N:
            lines.append(f"  … +{len(props.items) - TOP_N} more")
    return "\n".join(lines)

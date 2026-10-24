"""``precis_geom.fit`` — the offline fit check over a run's ``out/`` dir.

``fit.check(run_dir)`` (and ``python -m precis_geom.fit <run_dir>``) reads
``manifest.json`` + ``blocks/*`` + ``trace.json``, composes world poses
(parents included, ``linear``/``polar`` instance arrays expanded),
tessellates each template once at the Decision-2 chord through
:mod:`precis.cad.fit`, and classifies every connected pair plus every
AABB-near pair.

Tessellations and pair measurements cache under ``out/.fitcache/`` keyed
by the spec's geometry hash (Decision 4), so a rebuild re-runs only the
pairs that moved. The report lands in ``out/fit.json`` (previous run
rotated to ``fit.prev.json`` beside ``trace.prev.json``, Decision 8) and
is also returned as a :class:`precis.cad.fit.FitReport`.

Per-pair rules ride the manifest ``fit_rules`` key: ``{"min_clearance":
[{"pair": ["a", "b"], "value": "2 mm"}]}`` — values are unit strings
through ``precis.utils.units`` (bare numbers are metres).
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

from precis.cad import fit as _engine
from precis.cad.scene import NodeSpec, SceneSpec, parse_source
from precis.cad.vec import Transform, identity
from precis.cad.vec import pose as vec_pose
from precis.utils.units import parse_quantity, parse_vector_quantity
from precis_geom import trace_schema as _ts

_FIT_JSON = "fit.json"
_FIT_PREV = "fit.prev.json"
_CACHE_DIR = ".fitcache"


class FitCheckError(RuntimeError):
    """``strict=True`` on a report with error findings."""


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def _load_spec(root: Path, entry: dict[str, Any]) -> SceneSpec | None:
    """``blocks/<name>.cad`` (DSL source) or ``.cad.json`` → SceneSpec."""
    path = root / entry["file"]
    text = path.read_text(encoding="utf-8")
    if entry["file"].endswith(".cad.json"):
        data = json.loads(text)
        nodes = [NodeSpec.from_meta(str(n["name"]), n) for n in data.get("nodes") or []]
        return SceneSpec(
            nodes=nodes,
            components=[str(c) for c in data.get("components") or ["part"]],
            meta=dict(data.get("meta") or {"units": "mm"}),
        )
    return parse_source(text)


def _sites(trace_doc: dict[str, Any] | None) -> dict[int, str]:
    """trace frame id → ``file:line`` — the report's made-by labels."""
    out: dict[int, str] = {}
    for f in (trace_doc or {}).get("frames") or []:
        if isinstance(f, dict) and "id" in f:
            out[int(f["id"])] = f"{f.get('file', '?')}:{f.get('line', '?')}"
    return out


def _pose_vec(inst: dict[str, Any]) -> tuple[list[float], list[float]]:
    loc = [0.0, 0.0, 0.0]
    rot = [0.0, 0.0, 0.0]
    if inst.get("pose") is not None:
        loc = parse_vector_quantity(
            inst["pose"], "length", unit=inst.get("pose_unit"), arg_name="pose"
        )
    if inst.get("rot") is not None:
        rot = parse_vector_quantity(
            inst["rot"], "angle", unit=inst.get("rot_unit"), arg_name="rot"
        )
    return loc, rot


def _rot_axis(axis: np.ndarray, rad: float) -> Transform:
    a = axis / np.linalg.norm(axis)
    ux = np.array([[0.0, -a[2], a[1]], [a[2], 0.0, -a[0]], [-a[1], a[0], 0.0]])
    R = np.eye(3) + math.sin(rad) * ux + (1.0 - math.cos(rad)) * (ux @ ux)
    return Transform(R=R, t=np.zeros(3))


def _array_members(inst: dict[str, Any]) -> list[tuple[str, Transform]]:
    """Expand an instance's ``linear``/``polar`` array spec into
    ``(name#k, local transform)`` members — one unarrayed instance is a
    single member. The step is in the instance's own frame (``pga.tile``
    packs copies in the Geom's frame; the instance pose applies outside).
    Polar mirrors the cad ``polar:nNrR`` convention: member k rotated
    ``2πk/count`` about ``axis`` then offset ``radius`` along a fixed
    perpendicular (axis=+z → +x)."""
    name = str(inst["name"])
    spec = inst.get("linear") or inst.get("polar")
    if not spec:
        return [(name, identity())]
    count = int(spec["count"])
    if spec.get("kind") == "linear":
        step = np.asarray(spec["axis"], dtype=float) * float(spec["pitch"])
        return [
            (
                f"{name}#{k + 1}",
                Transform(R=np.eye(3), t=k * step),
            )
            for k in range(count)
        ]
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
        (
            f"{name}#{k + 1}",
            _rot_axis(axis, 2.0 * math.pi * k / count).compose(
                Transform(R=np.eye(3), t=u * radius)
            ),
        )
        for k in range(count)
    ]


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------


def _thread_sizes() -> dict[str, tuple[float, float]]:
    """Thread size → ``(major_m, minor_m)`` from ``precis.component_series``
    — the ISO 68-1 internal-thread minor ``D − 1.082532·P``. Used only for
    the screw rule's overlap allowance; the size itself still comes from
    the port's ``annotations.thread`` (never inferred, Decision 6)."""
    from precis import component_series

    out: dict[str, tuple[float, float]] = {}
    for series in component_series.load_series():
        for size in series.sizes:
            specs = getattr(size, "specs", None) or {}
            d = specs.get("outer_diameter") or specs.get("thread_size")
            pitch = specs.get("thread_pitch")
            if isinstance(d, (int, float)) and isinstance(pitch, (int, float)):
                key = str(specs.get("thread_size") or size.key)
                out[key] = (d / 1e3, (d - 1.082532 * pitch) / 1e3)
    return out


def _fit_rules(manifest: dict[str, Any], extra: Any) -> dict[tuple[str, str], float]:
    """``min_clearance`` pairs → metres. The manifest key is ``fit_rules``;
    ``extra`` is the ``Run.check(min_clearance=...)`` argument — explicit
    wins over stored."""
    out: dict[tuple[str, str], float] = {}
    stored = (manifest.get("fit_rules") or {}).get("min_clearance") or []
    for row in stored:
        pair = tuple(sorted(row["pair"]))
        out[pair] = parse_quantity(row["value"], "length", require_unit=False)
    for pair, value in (extra or {}).items():
        out[tuple(sorted(pair))] = parse_quantity(value, "length", require_unit=False)
    return out


def check_run(
    root: Path,
    manifest: dict[str, Any] | None,
    trace_doc: dict[str, Any] | None,
    *,
    min_clearance: dict[tuple[str, str], Any] | None = None,
    materials: dict[str, float] | None = None,
) -> _engine.FitReport:
    """The check over an already-parsed manifest + trace (or the on-disk
    ones when ``None``). Shared by :func:`check` and ``out.Run.check``."""
    root = Path(root)
    if manifest is None:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if trace_doc is None:
        try:
            trace_doc = _ts.loads((root / "trace.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            trace_doc = None
    sites = _sites(trace_doc)

    blocks = {str(b["name"]): b for b in manifest.get("blocks") or []}
    insts = {str(i["name"]): i for i in manifest.get("instances") or []}
    used_as_template = {str(i.get("template")) for i in insts.values()}

    # world transforms, parent-relative compose (ops.compose_world_pose)
    world: dict[str, Transform] = {}

    def world_of(name: str) -> Transform:
        if name in world:
            return world[name]
        inst = insts[name]
        loc, rot = _pose_vec(inst)
        local = vec_pose(np.asarray(loc), np.asarray(rot))
        parent = inst.get("parent")
        xf = world_of(str(parent)).compose(local) if parent in insts else local
        world[name] = xf
        return xf

    # tessellate each template once per component (cache in .fitcache)
    cache_dir = root / _CACHE_DIR
    pair_cache: dict[str, dict[str, float]] = {}
    pairs_file = cache_dir / "pairs.json"
    try:
        pair_cache = json.loads(pairs_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass

    meshes: dict[tuple[str, str, float], Any] = {}  # (block, comp, chord)
    specs: dict[str, SceneSpec | None] = {}

    def solid_for(block: str, comp: str, chord: float) -> Any:
        key = (block, comp, chord)
        if key in meshes:
            return meshes[key]
        spec = specs[block]
        if spec is None:
            raise FitCheckError(f"{block}: no spec (atomic or unreadable)")
        gh = _engine.geometry_hash(spec, comp, chord)
        cache_file = cache_dir / f"{gh}.npz"
        try:
            blob = np.load(cache_file)
            solid = _engine.mesh_to_manifold((blob["v"], blob["t"]))
        except (OSError, ValueError, KeyError):
            solid = _engine.tessellate_component(spec, comp, chord)
            if solid is not None:
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    mesh = solid.to_mesh()
                    np.savez(
                        cache_file,
                        v=np.asarray(mesh.vert_properties)[:, :3],
                        t=np.asarray(mesh.tri_verts),
                    )
                except (OSError, ValueError):
                    pass
        meshes[key] = solid
        return solid

    placed: list[_engine.Placed] = []
    for name, entry in sorted(blocks.items()):
        if entry.get("scale") != "macro" or name in used_as_template:
            continue
        # an uninstanced macro block is a part at the world origin
        spec = _load_spec(root, entry)
        specs[name] = spec
        _place_block(placed, name, name, spec, entry, identity(), sites, solid_for)

    for name in sorted(insts):
        inst = insts[name]
        template = str(inst.get("template") or "")
        entry = blocks.get(template)
        if entry is None or entry.get("scale") != "macro":
            continue
        if template not in specs:
            specs[template] = _load_spec(root, entry)
        spec = specs[template]
        base_xf = world_of(name)
        for member, mxf in _array_members(inst):
            _place_block(
                placed,
                member,
                template,
                spec,
                entry,
                base_xf.compose(mxf),
                sites,
                solid_for,
                base=name,
                parent=inst.get("parent"),
                made_by=sites.get(int(inst.get("frame") or -1)),
            )

    connects: list[_engine.Connect] = []
    port_thread = _port_threads(blocks)
    for c in manifest.get("connects") or []:
        a_raw, b_raw = str(c.get("a", "")), str(c.get("b", ""))
        a_blk, _, a_port = a_raw.partition(".")
        b_blk, _, b_port = b_raw.partition(".")
        thread = port_thread.get((a_blk, a_port)) or port_thread.get((b_blk, b_port))
        connects.append(
            _engine.Connect(
                a=a_blk,
                b=b_blk,
                joint=c.get("joint"),
                a_port=a_port or None,
                b_port=b_port or None,
                thread_size=thread,
                made_by=sites.get(int(c.get("frame") or -1)),
            )
        )

    report = _engine.fit(
        placed,
        connects,
        _engine.default_rules(),
        sizes=_thread_sizes(),
        materials=materials,
        min_clearance=_fit_rules(manifest, min_clearance),
        pair_cache=pair_cache,
    )
    try:
        pairs_file.parent.mkdir(parents=True, exist_ok=True)
        pairs_file.write_text(
            json.dumps(pair_cache, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
    return report


def _port_threads(blocks: dict[str, dict[str, Any]]) -> dict[tuple[str, str], str]:
    """``(block, port)`` → ``annotations.thread`` — the scripted fastener
    size (Decision 6); ``None`` absent — never inferred."""
    out: dict[tuple[str, str], str] = {}
    for name, entry in blocks.items():
        for p in entry.get("ports") or []:
            ann = p.get("annotations") or {}
            if isinstance(ann.get("thread"), str):
                out[(name, str(p["name"]))] = ann["thread"]
    return out


def _place_block(
    placed: list[_engine.Placed],
    name: str,
    template: str,
    spec: SceneSpec | None,
    entry: dict[str, Any],
    xf: Transform,
    sites: dict[int, str],
    solid_for: Any,
    *,
    base: str | None = None,
    parent: str | None = None,
    made_by: str | None = None,
) -> None:
    made_by = made_by or sites.get(int(entry.get("frame") or -1))
    if spec is None:
        return
    materials = dict(spec.meta.get("materials") or {})
    solids: dict[str, Any] = {}
    reasons: list[str] = []
    chords: list[float] = []
    gh_parts: list[str] = []
    for comp in spec.components or []:
        reason = _engine.unmeshable_reason(spec, comp)
        if reason is not None:
            reasons.append(f"{comp}: {reason}")
            continue
        chord = _engine.component_chord(spec, comp)
        chords.append(chord)
        solid = solid_for(template, comp, chord)
        if solid is None:
            reasons.append(f"{comp}: empty fold")
            continue
        solids[comp] = solid
        gh_parts.append(_engine.geometry_hash(spec, comp, chord))
    chord = max(chords) if chords else _engine.CHORD_MAX_M

    if not solids:
        placed.append(
            _engine.place(
                {},
                xf,
                name=name,
                chord=chord,
                base=base,
                parent=parent,
                made_by=made_by,
                material=materials,
                note="; ".join(reasons) or "no meshable component",
            )
        )
        return

    comps = list(solids)
    m34 = [[float(xf.R[i, j]) for j in range(3)] + [float(xf.t[i])] for i in range(3)]

    def refine(finer: float) -> dict[str, Any]:
        out = {}
        for comp in comps:
            s = solid_for(template, comp, finer)
            if s is not None:
                out[comp] = s.transform(m34)
        return out

    placed.append(
        _engine.place(
            solids,
            xf,
            name=name,
            chord=chord,
            base=base,
            parent=parent,
            made_by=made_by,
            refine=refine,
            material=materials,
            geo_hash=hashlib.sha256("|".join(gh_parts).encode()).hexdigest(),
        )
    )


def _write_report(root: Path, report: _engine.FitReport) -> Path:
    """``out/fit.json``; a previous one rotates to ``fit.prev.json``
    (Decision 8 — the ``_prev_trace`` pattern)."""
    root = Path(root)
    cur = root / _FIT_JSON
    if cur.is_file():
        cur.replace(root / _FIT_PREV)
    cur.write_text(json.dumps(report.to_json(), indent=1) + "\n", encoding="utf-8")
    return cur


def check(
    run_dir: str | Path,
    *,
    strict: bool = False,
    min_clearance: dict[tuple[str, str], Any] | None = None,
    materials: dict[str, float] | None = None,
) -> _engine.FitReport:
    """Fit-check a finished run dir; writes ``out/fit.json`` and returns
    the report. ``strict=True`` raises :class:`FitCheckError` on any error
    finding."""
    root = Path(run_dir)
    report = check_run(
        root, None, None, min_clearance=min_clearance, materials=materials
    )
    _write_report(root, report)
    if strict and report.has_errors:
        raise FitCheckError(
            f"fit check failed: {len(report.findings)} finding(s) — see {root}/fit.json"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    strict = "--strict" in args
    args = [a for a in args if a != "--strict"]
    if not args:
        print("usage: python -m precis_geom.fit <run_dir> [--strict]", file=sys.stderr)
        return 2
    report = check(args[0], strict=strict)
    print(report.text(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``precis_geom.out`` — write a run's ``out/`` in the importer's layout.

The dev-loop side of the workspace ``out/`` contract
(:mod:`precis_se.workspace_import`, the structure-workspaces spec
§Import boundary): a scratch script on the host builds with the core
library, declares its blocks/instances/connects here, and the run writes
``manifest.json`` + ``trace.json`` + ``blocks/…`` in exactly the shape
``tests/workspaces/data/two_blocks/out/`` exercises — so the importer
accepts a dev-loop run and a sandboxed ``precis_ws.out`` run alike.

    from precis_geom import out

    with out.run("hcol") as o:                    # scratch/out/hcol/
        scene = build_something()
        o.block("p", scene, desc="carbon strip",
                ports=[{"name": "tail", "atom": "aC4", "roles": ["join"]}])
        o.block("plate", "plate box:w40d20h10")    # macro DSL source
        o.instance("pin", template="plate", parent="p",
                   pose="[0 0 10] nm")
        o.connect("p.tail", "pin.top")
    # exit writes manifest.json + trace.json

:func:`run` activates a :class:`precis_geom.trace.Tracer` (``work_root``
= the scratch dir, so the script's own file mints ``user`` frames) when
none is live; an already-active tracer is left untouched.
``block``/``instance``/``connect`` are ``@traced`` ``kind="core"`` calls,
and a manifest entry's ``frame`` defaults to its own call's frame id —
the manifest↔trace join the sandbox's ``precis_ws.out`` stamps the same
way. ``finish()`` is the explicit form when the context manager doesn't
fit; ``run()`` calls it on exit.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from precis_geom import extxyz as _extxyz
from precis_geom import trace as _trace
from precis_geom import trace_schema as _ts

#: ``<work>/out/<run>/``'s parent — the scratch dir is the host analogue
#: of the sandbox's ``/work``.
_SCRATCH_DIR = Path("scratch")
_OUT_DIRNAME = "out"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


def default_root(name: str) -> Path:
    """``scratch/out/<name>/`` relative to the current directory."""
    return _SCRATCH_DIR / _OUT_DIRNAME / name


def _vet_name(name: str, *, what: str = "block") -> str:
    n = str(name or "").strip()
    if not n or not _NAME_RE.match(n) or n.startswith("."):
        raise ValueError(
            f"out.{what}: bad name {name!r} — it becomes a file under "
            "blocks/, so keep it [A-Za-z0-9_.-] with no leading dot"
        )
    return n


def _geom_parts(obj: Any) -> tuple[Any, list[dict[str, Any]] | None]:
    """Unwrap a :class:`precis_geom.assembly.Geom` → ``(geometry,
    manifest ports)``; anything else passes through with ``None`` ports.

    A pga object flattens to its merged geometry (the ``out`` boundary
    writes bytes, not the object graph); its declared ports become
    manifest port rows verbatim — ``atom`` names a label of the
    flattened scene (kept by flatten while unique), ``direction`` is
    the block-frame direction vector."""
    from precis_geom import assembly as _pga

    if not isinstance(obj, _pga.Geom):
        return obj, None
    flat = obj.flatten()
    ports: list[dict[str, Any]] = []
    flat_labels = set(getattr(flat, "atoms", {}) or {})
    for p in obj.ports.values():
        row: dict[str, Any] = {"name": p.name}
        if p.atom is not None:
            if flat_labels and p.atom not in flat_labels:
                raise ValueError(
                    f"out.block: port {p.name!r} binds atom {p.atom!r}, which "
                    "is not a label of the flattened scene"
                )
            row["atom"] = p.atom
        if p.roles:
            row["roles"] = list(p.roles)
        if p.direction is not None:
            row["direction"] = [float(v) for v in p.direction]
        ports.append(row)
    return flat, ports or None


def _array_fields(spec: dict[str, Any]) -> dict[str, Any]:
    """``Geom.array`` (canonical, ``kind``-keyed) → the op vocabulary the
    importer replays: ``linear``/``polar`` minus ``kind`` — the same
    fields ``ops_export`` writes back out."""
    spec = dict(spec)
    kind = spec.pop("kind", None)
    if kind not in ("linear", "polar"):
        raise ValueError(f"out.instance: bad array spec kind {kind!r}")
    return {kind: spec}


def _symbolic_fields(g: Any) -> dict[str, Any]:
    """A symbolic pga ``Geom`` (``pga.tile``/``pga.instance`` on a
    template *name*) → the manifest ``instances`` fields: ``template``,
    ``linear``/``polar`` from the array spec, and ``pose``/``rot``/
    ``pose_unit`` from ``g.pose``.

    Refuses a non-symbolic Geom (geometry belongs in ``out.block``) and
    an *improper* pose — Euler angles carry only proper rotations, so a
    mirrored placement can never round-trip through pose/rot."""
    import numpy as _np

    from precis.cad import vec as _vec

    if g.template is None:
        raise ValueError(
            "out.instance: a Geom template must be symbolic (a "
            "pga.tile/pga.instance on a block name) — out.block writes "
            "geometry"
        )
    out: dict[str, Any] = {"template": g.template}
    if g.array is not None:
        out.update(_array_fields(g.array))
    if g.pose is not None:
        if float(_np.linalg.det(_np.asarray(g.pose.R))) < 0:
            raise ValueError(
                "out.instance: the placement is a reflection (det −1) — "
                "pose/rot cannot carry an improper transform "
                "(next: mirror the geometry itself and out.block it, or "
                "re-place without the reflection)"
            )
        units = g.meta.get("units")
        if units is None:
            raise ValueError(
                "out.instance: the Geom carries no units — "
                "(next: build it via pga (units are stamped) or pass "
                "pose=/pose_unit= explicitly)"
            )
        out["pose"] = [float(v) for v in g.pose.t]
        out["pose_unit"] = {"A": "Å"}.get(units, units)
        out["rot"] = [float(v) for v in _vec.euler_rad_from_matrix(g.pose.R)]
        out["rot_unit"] = "rad"  # euler_rad_from_matrix emits radians
    return out


def _block_kind(scene_like: Any, name: str, scale: str | None) -> str:
    """The manifest ``scale`` for one block's geometry object.

    Atomic blocks are :class:`precis.structure.Scene` objects
    (``.atoms`` + ``.cell``) — written as ``blocks/<name>.extxyz``.
    Macro blocks are a :class:`precis.cad.scene.SceneSpec` (``.nodes``)
    — ``blocks/<name>.cad.json`` — or DSL source text — written verbatim
    to ``blocks/<name>.cad`` (the importer parses both).
    """
    if scale is not None and scale not in ("atomic", "macro"):
        raise ValueError(f"out.block {name!r}: scale must be 'atomic' or 'macro'")
    if isinstance(scene_like, str):
        kind = "macro"
    elif hasattr(scene_like, "atoms") and hasattr(scene_like, "cell"):
        kind = "atomic"
    elif hasattr(scene_like, "nodes"):
        kind = "macro"
    else:
        raise ValueError(
            f"out.block {name!r}: unrecognised geometry "
            f"{type(scene_like).__name__} — pass a structure Scene, a "
            "cad SceneSpec, or DSL source text"
        )
    if scale is not None and scale != kind:
        raise ValueError(
            f"out.block {name!r}: scale={scale!r} but the geometry looks "
            f"{kind!r} — fix the scale or the object"
        )
    return kind


def _write_atomic(root: Path, name: str, scene: Any) -> str:
    """``blocks/<name>.extxyz`` via the one canonical writer
    (:mod:`precis_geom.extxyz`, duck-typed on the scene protocol — this
    package is a standalone kernel and never imports ``precis``), plus
    the ``.bonds.json`` sidecar when the scene carries bonds (the
    importer's default sidecar path — no ``bonds`` key needed in the
    manifest). Returns the manifest ``file``."""
    rel = f"blocks/{name}.extxyz"
    p = root / rel
    p.write_text(_extxyz.to_extxyz(scene), encoding="utf-8")
    bonds = list(getattr(scene, "bonds", None) or [])
    if bonds:
        (root / f"blocks/{name}.bonds.json").write_text(
            _extxyz.bonds_to_json(bonds), encoding="utf-8"
        )
    return rel


def _write_macro(root: Path, name: str, geom: Any) -> str:
    """``blocks/<name>.cad.json`` for a SceneSpec, or verbatim DSL source
    to ``blocks/<name>.cad`` for a string."""
    if isinstance(geom, str):
        rel = f"blocks/{name}.cad"
        (root / rel).write_text(geom, encoding="utf-8")
        return rel
    rel = f"blocks/{name}.cad.json"
    nodes = []
    for n in geom.nodes:
        node = {"name": n.name, **n.to_meta()}
        nodes.append(node)
    doc = {
        "schema": 1,
        "nodes": nodes,
        "components": list(getattr(geom, "components", None) or ["part"]),
        "meta": dict(getattr(geom, "meta", None) or {"units": "mm"}),
    }
    (root / rel).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return rel


def _ray_entry(
    r_or_name: Any,
    args: tuple[Any, ...],
    kw: dict[str, Any],
    *,
    block: str | None,
    frame: int | None,
) -> dict[str, Any]:
    """A ``pga.Ray`` (or ``pga.ray`` arguments) → the importer's
    ``rays`` manifest row; ``None``-valued keys are omitted."""
    from precis_geom import assembly as _pga

    r = (
        r_or_name
        if isinstance(r_or_name, _pga.Ray)
        else _pga.ray(r_or_name, *args, **kw)
    )
    blk = r.block if r.block is not None else block
    if blk is None:
        raise ValueError(
            f"out.ray: ray {r.name!r} has no block — pass block= (or set it on the ray)"
        )
    fr = r.frame_id if r.frame_id is not None else frame
    if fr is None:
        fr = _trace.leaf_id()
    entry: dict[str, Any] = {
        "name": _vet_name(r.name, what="ray"),
        "block": str(blk),
        "origin": [float(v) for v in r.origin],
        "direction": [float(v) for v in r.direction],
        "length": float(r.length),
        "kind": r.kind,
    }
    if r.fields is not None:
        entry["fields"] = list(r.fields)
    if r.step is not None:
        entry["step"] = float(r.step)
    if r.reach is not None:
        entry["reach"] = float(r.reach)
    if r.frame_in is not None:
        entry["in"] = r.frame_in
    if r.anchor is not None:
        entry["anchor"] = list(r.anchor)
    if fr is not None:
        entry["frame"] = int(fr)
    return entry


class Run:
    """One run's ``out/`` under construction. ``blocks`` files are
    written eagerly (a crashed script leaves inspectable geometry);
    ``finish()`` writes ``manifest.json`` and the live tracer's
    ``trace.json``."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._blocks: list[dict[str, Any]] = []
        self._instances: list[dict[str, Any]] = []
        self._connects: list[dict[str, Any]] = []
        self._rays: list[dict[str, Any]] = []
        self._ray_names: set[str] = set()
        self._names: set[str] = set()
        self._finished = False

    # -- manifest calls ---------------------------------------------------

    @_trace.traced(kind="core", name="out.block")
    def block(
        self,
        name: str,
        geom: Any,
        *,
        scale: str | None = None,
        ports: list[dict[str, Any]] | None = None,
        relax: dict[str, Any] | None = None,
        desc: str | None = None,
        frame: int | None = None,
    ) -> dict[str, Any]:
        """Declare one manifest block and write its geometry file.

        ``geom`` is a structure :class:`~precis.structure.scene.Scene`
        (atomic → ``blocks/<name>.extxyz`` + ``.bonds.json``), a cad
        :class:`~precis.cad.scene.SceneSpec` (macro → ``.cad.json``), or
        DSL source text (macro → ``.cad``). ``ports``/``relax``/``desc``
        pass through to the manifest entry verbatim (the importer's
        vet is the contract). ``frame`` defaults to this call's own
        trace-frame id."""
        n = _vet_name(name)
        if n in self._names:
            raise ValueError(f"out.block: duplicate block name {n!r}")
        geom, gports = _geom_parts(geom)
        if ports is None:
            ports = gports
        kind = _block_kind(geom, n, scale)
        (self.root / "blocks").mkdir(parents=True, exist_ok=True)
        if kind == "atomic":
            file_rel = _write_atomic(self.root, n, geom)
        else:
            file_rel = _write_macro(self.root, n, geom)
        entry: dict[str, Any] = {"name": n, "file": file_rel, "scale": kind}
        if ports:
            entry["ports"] = [dict(p) for p in ports]
        if relax is not None:
            entry["relax"] = dict(relax)
        if desc is not None:
            entry["desc"] = str(desc)
        fr = frame if frame is not None else _trace.leaf_id()
        if fr is not None:
            entry["frame"] = int(fr)
        self._blocks.append(entry)
        self._names.add(n)
        made: dict[str, int] = {"blocks": 1}
        if ports:
            made["ports"] = len(ports)
        _trace.record(made=made)
        return entry

    @_trace.traced(kind="core", name="out.instance")
    def instance(
        self,
        name: str,
        *,
        template: Any,
        parent: str | None = None,
        pose: Any = None,
        rot: Any = None,
        pose_unit: str | None = None,
        rot_unit: str | None = None,
        linear: dict[str, Any] | None = None,
        polar: dict[str, Any] | None = None,
        frame: int | None = None,
    ) -> dict[str, Any]:
        """One manifest ``instances`` entry — a template placement.

        ``template`` may be a symbolic :class:`precis_geom.assembly.Geom`
        (``pga.tile("p", n, pitch=…)``): its template name and array
        spec land as the ``linear``/``polar`` keys the importer replays
        through ``array_block``."""
        from precis_geom import assembly as _pga

        n = _vet_name(name, what="instance")
        if isinstance(template, _pga.Geom):
            fields = _symbolic_fields(template)
            template = fields.pop("template")
            if linear is None:
                linear = fields.get("linear")
            if polar is None:
                polar = fields.get("polar")
            if pose is None and rot is None:
                # the caller's explicit pose/rot win as a pair — mixing
                # caller rot with the Geom's pose is never meaningful
                pose = fields.get("pose")
                rot = fields.get("rot")
                pose_unit = pose_unit or fields.get("pose_unit")
                rot_unit = rot_unit or fields.get("rot_unit")
        entry: dict[str, Any] = {"name": n, "template": str(template)}
        if parent is not None:
            entry["parent"] = str(parent)
        if pose is not None:
            entry["pose"] = pose
        if rot is not None:
            entry["rot"] = rot
        if pose_unit is not None:
            entry["pose_unit"] = str(pose_unit)
        if rot_unit is not None:
            entry["rot_unit"] = str(rot_unit)
        if linear is not None:
            entry["linear"] = dict(linear)
        if polar is not None:
            entry["polar"] = dict(polar)
        fr = frame if frame is not None else _trace.leaf_id()
        if fr is not None:
            entry["frame"] = int(fr)
        self._instances.append(entry)
        _trace.record(made={"instances": 1})
        return entry

    @_trace.traced(kind="core", name="out.connect")
    def connect(
        self,
        a: str,
        b: str,
        *,
        kind: str | None = None,
        joint: Any = None,
        objectives: Any = None,
        frame: int | None = None,
    ) -> dict[str, Any]:
        """One manifest ``connects`` entry — an intent edge
        ``blk.port`` ↔ ``blk.port``."""
        entry: dict[str, Any] = {"a": str(a), "b": str(b)}
        if kind is not None:
            entry["kind"] = str(kind)
        if joint is not None:
            entry["joint"] = joint
        if objectives is not None:
            entry["objectives"] = objectives
        fr = frame if frame is not None else _trace.leaf_id()
        if fr is not None:
            entry["frame"] = int(fr)
        self._connects.append(entry)
        return entry

    @_trace.traced(kind="core", name="out.ray")
    def ray(
        self,
        r_or_name: Any,
        *args: Any,
        block: str | None = None,
        frame: int | None = None,
        **kw: Any,
    ) -> dict[str, Any]:
        """One manifest ``rays`` entry — an observation ray on a block
        declared in this run. Takes a :class:`precis_geom.assembly.Ray`
        or builds one via ``pga.ray(name, *args, **kw)``; ``frame`` is
        the manifest trace-frame id (the Ray's reference frame lives on
        the Ray itself)."""
        entry = _ray_entry(r_or_name, args, kw, block=block, frame=frame)
        if entry["block"] not in self._names:
            raise ValueError(
                f"out.ray: block {entry['block']!r} is not declared in this "
                f"run (declared: {sorted(self._names) or '(none)'})"
            )
        if entry["name"] in self._ray_names:
            raise ValueError(f"out.ray: duplicate ray name {entry['name']!r}")
        self._ray_names.add(entry["name"])
        self._rays.append(entry)
        return entry

    # -- serialisation ------------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"schema": 1, "blocks": list(self._blocks)}
        if self._instances:
            doc["instances"] = list(self._instances)
        if self._connects:
            doc["connects"] = list(self._connects)
        if self._rays:
            doc["rays"] = list(self._rays)
        return doc

    def finish(
        self,
        *,
        notes: str | None = None,
        run_meta: dict[str, Any] | None = None,
    ) -> Path:
        """Write ``manifest.json`` + ``trace.json``; returns the out dir."""
        if self._finished:
            raise ValueError(f"out.run: finish() already ran for {self.root}")
        self._finished = True
        self.root.mkdir(parents=True, exist_ok=True)
        doc = self.manifest()
        if notes is not None:
            doc["notes"] = str(notes)
        (self.root / "manifest.json").write_text(
            json.dumps(doc, indent=1) + "\n", encoding="utf-8"
        )
        tr = _trace.current()
        if tr is not None:
            trace_doc = tr.dump(run_meta or {})
            (self.root / "trace.json").write_text(
                _ts.dumps(trace_doc) + "\n", encoding="utf-8"
            )
        return self.root


def _resolve_target(target: str | os.PathLike[str]) -> Path:
    """A bare run name resolves under ``scratch/out/``; anything holding
    a separator (or an absolute path) is the out dir itself."""
    s = str(target)
    if os.sep in s or (os.altsep and os.altsep in s) or os.path.isabs(s):
        return Path(s)
    return default_root(_vet_name(s, what="run"))


@contextlib.contextmanager
def run(
    target: str | os.PathLike[str],
    *,
    work_root: str | os.PathLike[str] | None = None,
    notes: str | None = None,
    run_meta: dict[str, Any] | None = None,
) -> Iterator[Run]:
    """One run's ``out/``, manifest + trace written on exit.

    ``target`` is a run name (``out.run("hcol")`` →
    ``scratch/out/hcol/``) or an explicit out-dir path. ``work_root`` is
    the tracer's user-code root — default the run dir's
    ``<work>/out/<run>`` parent-of-parent (``scratch/`` for a named run),
    the host analogue of the sandbox's ``/work``. When a tracer is
    already live it is used as-is (``work_root`` is then moot).
    """
    root = _resolve_target(target)
    wr = Path(work_root) if work_root is not None else root.parent.parent
    o = Run(root)
    if _trace.current() is not None:
        yield o
        o.finish(notes=notes, run_meta=run_meta)
        return
    with _trace.run(work_root=wr, **(run_meta or {})):
        yield o
        o.finish(notes=notes, run_meta=run_meta)


__all__ = ["Run", "default_root", "run"]

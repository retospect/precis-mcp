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
        template: str,
        parent: str | None = None,
        pose: Any = None,
        rot: Any = None,
        pose_unit: str | None = None,
        rot_unit: str | None = None,
        frame: int | None = None,
    ) -> dict[str, Any]:
        """One manifest ``instances`` entry — a template placement."""
        n = _vet_name(name, what="instance")
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

    # -- serialisation ------------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"schema": 1, "blocks": list(self._blocks)}
        if self._instances:
            doc["instances"] = list(self._instances)
        if self._connects:
            doc["connects"] = list(self._connects)
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

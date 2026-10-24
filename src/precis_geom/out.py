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

A ``with`` body that raises is written by :meth:`Run.fail` — a
``status="failed"`` manifest + the trace's error-marked frames — and
re-raised untouched; a previous good ``trace.json`` is kept as
``trace.prev.json`` and stays the run diff's baseline (gr514056).
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import sys
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
    if r.bundle is not None:
        entry["bundle"] = {"radius": float(r.bundle[0]), "count": int(r.bundle[1])}
    if fr is not None:
        entry["frame"] = int(fr)
    return entry


class Run:
    """One run's ``out/`` under construction. ``blocks`` files are
    written eagerly (a crashed script leaves inspectable geometry);
    ``finish()`` writes ``manifest.json`` and the live tracer's
    ``trace.json``. ``diff`` is the vs-previous-run frame diff
    ``finish()`` computes when this ``root`` already holds a
    ``trace.json`` (gr513671) — ``None`` on a first run or when no
    tracer was live. ``fail(exc)`` is the failure form of ``finish()`` —
    :func:`run` calls it on an exception inside the ``with``."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._blocks: list[dict[str, Any]] = []
        self._instances: list[dict[str, Any]] = []
        self._connects: list[dict[str, Any]] = []
        self._rays: list[dict[str, Any]] = []
        self._ray_names: set[str] = set()
        self._names: set[str] = set()
        self._finished = False
        self.diff: dict[str, Any] | None = None

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
        """Write ``manifest.json`` + ``trace.json``; returns the out dir.

        When the dir already holds a ``trace.json`` the new dump is
        diffed against it before it is overwritten — frames matched by
        (qual path from the root, ``occ``), NOT ``key`` (keys are
        line-based until the structural-keys spec lands). The result is
        ``self.diff`` (``added``/``removed`` path lists, ``changed``
        frames with was/now ``made`` and ``args``), echoed as a ``vs
        previous: +A -R ~C`` line and stored as manifest ``notes`` when
        ``notes`` is unset. The old trace is then rotated to
        ``trace.prev.json`` (one level, clobbering an older
        ``trace.prev.json``) so the run just diffed against stays
        inspectable after the overwrite (gr514055) — unless
        ``trace.json`` holds a failed run's dump, in which case the
        ``trace.prev.json`` that failure preserved already *is* the
        baseline and stays put (gr514056)."""
        if self._finished:
            raise ValueError(f"out.run: finish() already ran for {self.root}")
        self._finished = True
        self.root.mkdir(parents=True, exist_ok=True)
        doc = self.manifest()
        tr = _trace.current()
        trace_doc = tr.dump(run_meta or {}) if tr is not None else None
        prev, rotate = _prev_trace(self.root)
        if trace_doc is not None and prev is not None:
            self.diff = _trace_diff(prev, trace_doc)
        line = _diff_note(self.diff) if self.diff is not None else None
        if notes is not None:
            doc["notes"] = str(notes)
        elif line is not None:
            doc["notes"] = line
        (self.root / "manifest.json").write_text(
            json.dumps(doc, indent=1) + "\n", encoding="utf-8"
        )
        if line is not None:
            print(f"{self.root}: {line}")
        if trace_doc is not None:
            if rotate is not None:
                rotate.replace(self.root / "trace.prev.json")
            (self.root / "trace.json").write_text(
                _ts.dumps(trace_doc) + "\n", encoding="utf-8"
            )
        return self.root

    def fail(
        self,
        exc: BaseException,
        *,
        notes: str | None = None,
        run_meta: dict[str, Any] | None = None,
    ) -> Path:
        """The failure form of :meth:`finish`, called by :func:`run`
        while the tracer is still live: ``manifest.json`` with
        ``status="failed"`` + the exception, and the dump (its
        error-marked frames) to ``trace.json``.

        A previous good ``trace.json`` is moved aside to
        ``trace.prev.json`` first — the failed dump must not silently
        clobber the baseline the next run diffs against (gr514056)."""
        self._finished = True
        self.root.mkdir(parents=True, exist_ok=True)
        tr = _trace.current()
        if tr is not None and exc not in tr._raised and tr.stack[-1].kind != "root":
            # The raise left user code without crossing a traced call
            # (a bare ``raise`` in the script): mark the innermost live
            # frame so the dump still names a culprit.
            tr.stack[-1].error = type(exc).__name__
            tr._raised.add(exc)
        trace_doc = (
            tr.dump({**(run_meta or {}), "status": "failed"})
            if tr is not None
            else None
        )
        doc = self.manifest()
        doc["status"] = "failed"
        doc["error"] = _error_entry(exc, tr, trace_doc)
        if notes is not None:
            doc["notes"] = str(notes)
        (self.root / "manifest.json").write_text(
            json.dumps(doc, indent=1) + "\n", encoding="utf-8"
        )
        if trace_doc is not None:
            prev = self.root / "trace.json"
            try:
                old = _ts.loads(prev.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old = None  # absent/corrupt — nothing good to preserve
            if old is not None and (old.get("run") or {}).get("status") != "failed":
                prev.replace(self.root / "trace.prev.json")
            (self.root / "trace.json").write_text(
                _ts.dumps(trace_doc) + "\n", encoding="utf-8"
            )
        at = doc["error"].get("at") or {}
        loc = f" at {at['file']}:{at['line']}" if "file" in at else ""
        where = "trace.json" if trace_doc is not None else "manifest.json"
        print(
            f"out.run: failed -> {self.root}/{where} (error={type(exc).__name__}{loc})",
            file=sys.stderr,
        )
        return self.root


#: ``error.message`` cap in a failed manifest — a pointer, not a log.
_ERR_MSG_CAP = 240


def _error_entry(
    exc: BaseException,
    tr: _trace.Tracer | None,
    doc: dict[str, Any] | None,
) -> dict[str, Any]:
    """A failed manifest's ``error``: the exception type, a capped
    message, the user-code anchor (``at``) and the library frames the
    anchor collapsed (``below``)."""
    msg = str(exc)
    entry: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": (msg if len(msg) <= _ERR_MSG_CAP else msg[: _ERR_MSG_CAP - 1] + "…"),
    }
    anchor, below = _error_path(doc)
    at = _failure_at(exc, tr, anchor)
    if at is not None:
        entry["at"] = at
    if below:
        entry["below"] = below
    return entry


def _error_path(
    doc: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The dump's failure chain — ``(nearest kind=user ancestor of the
    error-marked frame, the frames collapsed below it down to and
    including that frame)``: the rendered stack ends in user code, the
    library frames go below (gr513668's rule applied to failures)."""
    frames = [f for f in (doc or {}).get("frames", []) if isinstance(f, dict)]
    err = next((f for f in reversed(frames) if f.get("error")), None)
    if err is None:
        return None, []
    by_key = {f.get("key"): f for f in frames}
    below: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = err
    while cur is not None and cur.get("kind") not in ("user", "root"):
        below.append(cur)
        cur = by_key.get(cur.get("parent"))
    anchor = cur if cur is not None and cur.get("kind") == "user" else None
    path = [
        {"qual": f["qual"], "file": f["file"], "line": f["line"]}
        for f in reversed(below)
    ]
    return anchor, path


def _failure_at(
    exc: BaseException,
    tr: _trace.Tracer | None,
    anchor: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """``{file, line, qual?}`` of the failure's user-code anchor — the
    trace's nearest ``kind=user`` frame (``anchor``) names it, and the
    traceback's innermost frame under ``work_root`` (``Tracer._in_work``,
    the same predicate the user-frame walk mints with) pins the exact
    line; the dump's frame or the raise site are the fallbacks."""
    user = deepest = None
    tb = exc.__traceback__
    while tb is not None:
        deepest = tb
        if tr is not None and tr._in_work(tb.tb_frame):
            user = tb
        tb = tb.tb_next
    if user is not None and tr is not None:
        f = user.tb_frame
        at: dict[str, Any] = {"file": tr._file_of(f), "line": user.tb_lineno}
        if anchor is not None:
            at["qual"] = anchor["qual"]
        elif f.f_code.co_name != "<module>":
            at["qual"] = _trace._user_qual(f)
        return at
    if anchor is not None:
        return {
            "file": anchor["file"],
            "line": anchor["line"],
            "qual": anchor["qual"],
        }
    if deepest is None:
        return None
    f = deepest.tb_frame
    file = tr._file_of(f) if tr is not None else f.f_code.co_filename
    return {"file": file, "line": deepest.tb_lineno}


def _prev_trace(root: Path) -> tuple[dict[str, Any] | None, Path | None]:
    """``(the trace a new run diffs against, the file to rotate to
    ``trace.prev.json`` before the overwrite)`` — ``trace.json`` and its
    doc, normally. When ``trace.json`` holds a failed run's dump
    (``run.status="failed"``) the ``trace.prev.json`` that failure
    preserved is the last-good baseline instead: it already sits where
    the rotation would put it, so nothing rotates (a crash can't
    promote its dump into the good baseline or clobber it). An
    unreadable ``trace.json`` diffs as a first run but still rotates."""
    prev = root / "trace.json"
    try:
        doc = _ts.loads(prev.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, prev if prev.is_file() else None
    if (doc.get("run") or {}).get("status") != "failed":
        return doc, prev
    kept = root / "trace.prev.json"
    try:
        return _ts.loads(kept.read_text(encoding="utf-8")), None
    except (OSError, ValueError):
        return doc, prev  # no good baseline survived — diff vs the failed dump


def _frame_paths(doc: dict[str, Any]) -> dict[tuple[tuple[str, ...], int], dict]:
    """``{(qual path from the root, occ): frame}`` — the match id a
    run-to-run diff uses in place of ``key`` (line-based today)."""
    frames = [f for f in doc.get("frames", []) if isinstance(f, dict)]
    by_key = {f["key"]: f for f in frames if "key" in f}
    out: dict[tuple[tuple[str, ...], int], dict] = {}
    for f in frames:
        if f.get("kind") == "root":
            continue
        chain: list[str] = []
        cur: dict[str, Any] | None = f
        while cur is not None and cur.get("kind") != "root":
            chain.append(str(cur.get("qual", "")))
            cur = by_key.get(cur.get("parent"))
        out.setdefault((tuple(reversed(chain)), int(f.get("occ", 0))), f)
    return out


def _render_path(k: tuple[tuple[str, ...], int]) -> str:
    return ">".join(k[0])


def _trace_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Frames added/removed/changed between two trace documents.

    ``changed`` keeps one entry per matched (path, ``occ``) whose
    ``made`` counts OR ``args`` digest moved — an arg-only edit (the
    typical "change a parameter" re-run) shows up even when the entity
    counts happen to come out equal (gr514055)."""
    old_map, new_map = _frame_paths(old), _frame_paths(new)
    changed = [
        {
            "path": _render_path(k),
            "was": old_map[k].get("made") or {},
            "now": new_map[k].get("made") or {},
            "args_was": old_map[k].get("args") or {},
            "args_now": new_map[k].get("args") or {},
        }
        for k in sorted(new_map.keys() & old_map.keys())
        if (old_map[k].get("made") or {}) != (new_map[k].get("made") or {})
        or (old_map[k].get("args") or {}) != (new_map[k].get("args") or {})
    ]
    return {
        "added": sorted(_render_path(k) for k in new_map.keys() - old_map.keys()),
        "removed": sorted(_render_path(k) for k in old_map.keys() - new_map.keys()),
        "changed": changed,
    }


_ABSENT: Any = object()


def _unique_pairs(
    pairs: Iterator[tuple[Any, Any]],
) -> list[tuple[Any, Any]]:
    """The differing ``(was, now)`` pairs of one field across a group of
    same-path changes, deduplicated in first-seen order — six identical
    ``strip`` occurrences collapse to one ``24 -> 32``."""
    seen: list[tuple[Any, Any]] = []
    for w, n in pairs:
        if w != n and (w, n) not in seen:
            seen.append((w, n))
    return seen


def _arg_note(v: Any) -> str:
    """An args-digest value for the note; a param present on only one
    side renders ``—`` (the digest carries no absence marker)."""
    return "—" if v is _ABSENT else str(v)


def _diff_note(diff: dict[str, Any]) -> str:
    """``vs previous: +A -R ~C (main.build>strip x6: atoms 24 -> 32;
    periods 4 -> 6)``.

    Same-path changed frames (the ``occ`` the match id carries and the
    render drops) group into one ``path xN:`` line so six identical
    ``strip`` occurrences print once with a count instead of six
    identical lines (gr514055)."""
    note = (
        f"vs previous: +{len(diff['added'])} -{len(diff['removed'])} "
        f"~{len(diff['changed'])}"
    )
    parts: list[str] = []
    for path, group in itertools.groupby(diff["changed"], key=lambda c: c["path"]):
        entries = list(group)
        label = f"{path} x{len(entries)}" if len(entries) > 1 else path
        segs: list[str] = []
        for e in sorted({k for c in entries for k in set(c["was"]) | set(c["now"])}):
            pairs = _unique_pairs(
                (c["was"].get(e, 0), c["now"].get(e, 0)) for c in entries
            )
            if pairs:
                segs.append(f"{e} " + ", ".join(f"{w} -> {n}" for w, n in pairs))
        for a in sorted(
            {k for c in entries for k in set(c["args_was"]) | set(c["args_now"])}
        ):
            pairs = _unique_pairs(
                (c["args_was"].get(a, _ABSENT), c["args_now"].get(a, _ABSENT))
                for c in entries
            )
            if pairs:
                segs.append(
                    f"{a} "
                    + ", ".join(f"{_arg_note(w)} -> {_arg_note(n)}" for w, n in pairs)
                )
        if segs:
            parts.append(f"{label}: " + "; ".join(segs))
    return note + (" (" + "; ".join(parts) + ")" if parts else "")


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

    An exception inside the ``with`` still writes the run record via
    :meth:`Run.fail` — ``manifest.json`` carrying ``status="failed"``
    plus the error, and the live dump's error-marked frames — then
    re-raises with its traceback untouched.
    """
    root = _resolve_target(target)
    wr = Path(work_root) if work_root is not None else root.parent.parent
    o = Run(root)
    live = (
        contextlib.nullcontext()
        if _trace.current() is not None
        else _trace.run(work_root=wr, **(run_meta or {}))
    )
    with live:
        try:
            yield o
        except BaseException as exc:
            try:
                o.fail(exc, notes=notes, run_meta=run_meta)
            except Exception as werr:
                print(
                    f"out.run: could not write the failure record: {werr}",
                    file=sys.stderr,
                )
            raise
        o.finish(notes=notes, run_meta=run_meta)


__all__ = ["Run", "default_root", "run"]

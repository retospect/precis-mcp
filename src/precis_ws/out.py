"""``out`` — the run's output lane: manifest + trace.

``out.block(name, obj, …)`` registers one block: the geometry file
(``blocks/<name>.cad.json`` for a :class:`precis.cad.scene.SceneSpec`,
``<name>.cad`` for a DSL string, ``<name>.extxyz`` for an ASE ``Atoms``
or :class:`precis.structure.scene.Scene`) plus its manifest entry
(``ports``/``relax``/``desc``/``scale``/``bonds`` — the shapes
``precis_se.workspace_import`` validates). ``out.instance``/
``out.connect`` append the manifest's ``instances``/``connects``;
``out.ray`` appends an observation ray to ``rays``.
:func:`finish` writes ``out/manifest.json`` and ``out/trace.json`` (the
run's construction trace, ``precis_geom.trace``); it also runs at
interpreter exit, so a script that just builds still leaves both
artifacts.

Every public function is ``@traced`` — the ``block``/``instance``/
``connect`` calls are themselves ``core`` frames in the run's trace, and
each stamps its manifest row with the calling frame's id unless an
explicit ``frame=`` is given.
"""

from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
from typing import Any

from precis_geom import trace as _trace
from precis_geom import trace_schema
from precis_geom.trace import traced

_SCHEMA = 1
_finished = False

_blocks: list[dict[str, Any]] = []
_instances: list[dict[str, Any]] = []
_connects: list[dict[str, Any]] = []
_rays: list[dict[str, Any]] = []
_notes: str | None = None
_payloads: dict[str, tuple[bytes, str]] = {}  # file name → (bytes, encoding)


def _out_dir() -> Path:
    raw = os.environ.get("PRECIS_WS_OUT_DIR")
    if raw:
        return Path(raw)
    return Path(os.environ.get("PRECIS_WS_WORK_DIR") or "/work") / "out"


def _frame_id(frame: int | None) -> int | None:
    return _trace.leaf_id() if frame is None else frame


def _block_file(name: str, obj: Any) -> tuple[str, bytes, str]:
    """``(blocks/<name>.<ext>, payload, scale)`` for the object's geometry form.

    A :class:`~precis.cad.scene.SceneSpec` serialises to the importer's
    ``{schema: 1, nodes, components, meta}`` cad.json; a bare string is the
    cad DSL source itself (``.cad``); ASE ``Atoms`` / structure ``Scene``
    become extxyz (``atomic`` scale).
    """
    from precis.cad.scene import SceneSpec
    from precis_geom import assembly as _pga
    from precis_geom import extxyz as _extxyz

    if isinstance(obj, _pga.Geom):
        obj = obj.flatten()
    if isinstance(obj, SceneSpec):
        doc: dict[str, Any] = {
            "schema": 1,
            "nodes": [{"name": n.name, **n.to_meta()} for n in obj.nodes],
            "components": list(obj.components),
            "meta": dict(obj.meta),
        }
        return (
            f"blocks/{name}.cad.json",
            json.dumps(doc, sort_keys=True).encode("utf-8"),
            "macro",
        )
    if isinstance(obj, str):
        return f"blocks/{name}.cad", obj.encode("utf-8"), "macro"
    if hasattr(obj, "atoms") and hasattr(obj, "cell"):
        return (
            f"blocks/{name}.extxyz",
            _extxyz.to_extxyz(obj).encode("utf-8"),
            "atomic",
        )
    scene_to_extxyz = getattr(obj, "to_extxyz", None)
    if callable(scene_to_extxyz):
        return f"blocks/{name}.extxyz", scene_to_extxyz().encode("utf-8"), "atomic"
    if type(obj).__name__ == "Atoms":  # ase.Atoms without importing ase
        import io

        from ase.io import write as ase_write

        buf = io.StringIO()
        ase_write(buf, obj, format="extxyz")
        return f"blocks/{name}.extxyz", buf.getvalue().encode("utf-8"), "atomic"
    raise TypeError(
        f"out.block: no geometry writer for {type(obj).__name__} "
        "(want precis.cad.scene.SceneSpec, a DSL string, an ASE Atoms or a "
        "precis.structure.scene.Scene)"
    )


@traced
def block(
    name: str,
    obj: Any,
    *,
    scale: str | None = None,
    ports: list[dict[str, Any]] | None = None,
    relax: dict[str, Any] | None = None,
    desc: str | None = None,
    bonds: Any = None,
    frame: int | None = None,
) -> None:
    """Register one output block; the file is written at :func:`finish`.

    ``ports``/``relax`` are manifest rows verbatim (the importer vets
    them); ``bonds`` may be a JSON-able structure written to
    ``blocks/<name>.bonds.json`` or an already-staged relative path.
    ``frame`` defaults to the calling trace frame. A
    :class:`precis_geom.assembly.Geom` flattens to geometry here and
    carries its declared ports into the manifest entry."""
    from precis_geom.out import _geom_parts

    obj, gports = _geom_parts(obj)
    if ports is None:
        ports = gports
    fname, payload, inferred = _block_file(name, obj)
    entry: dict[str, Any] = {
        "name": name,
        "file": fname,
        "scale": scale or inferred,
    }
    if ports:
        entry["ports"] = list(ports)
    if relax is not None:
        entry["relax"] = relax
    if desc is not None:
        entry["desc"] = desc
    fid = _frame_id(frame)
    if fid is not None:
        entry["frame"] = fid
    _payloads[fname] = (payload, "utf-8")
    if bonds is not None:
        if isinstance(bonds, str):
            entry["bonds"] = bonds
        else:
            bname = str(Path(fname).with_suffix(".bonds.json"))
            _payloads[bname] = (
                json.dumps(bonds, sort_keys=True).encode("utf-8"),
                "utf-8",
            )
            entry["bonds"] = bname
    _blocks.append(entry)
    _trace.record(made={"blocks": 1})


@traced
def instance(
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
) -> None:
    """Register one placement of a declared block.

    Blocks and instances share one namespace — an instance named like its
    own template (``instance("wheel", template="wheel")``) surfaces later
    as a generic "duplicate block name". The convention: a block that
    exists only to be instanced is named ``t_<part>`` (``t_spoke``) and
    placements take the plain names (``spoke``, ``spoke2``) — the
    ``precis-workspace-help`` skill's "``t_`` rule" is the long form;
    ``template=True`` on :func:`block` (gr514241) is the planned typed
    marker."""
    from precis_geom import assembly as _pga
    from precis_geom.out import _symbolic_fields

    if isinstance(template, _pga.Geom):
        fields = _symbolic_fields(template)
        template = fields.pop("template")
        linear = linear or fields.get("linear")
        polar = polar or fields.get("polar")
        if pose is None and rot is None:
            pose = fields.get("pose")
            rot = fields.get("rot")
            pose_unit = pose_unit or fields.get("pose_unit")
            rot_unit = rot_unit or fields.get("rot_unit")
    entry: dict[str, Any] = {"name": name, "template": template}
    for k, v in (
        ("parent", parent),
        ("pose", pose),
        ("rot", rot),
        ("pose_unit", pose_unit),
        ("rot_unit", rot_unit),
        ("linear", linear),
        ("polar", polar),
    ):
        if v is not None:
            entry[k] = v
    fid = _frame_id(frame)
    if fid is not None:
        entry["frame"] = fid
    _instances.append(entry)
    _trace.record(made={"instances": 1})


@traced
def connect(
    a: str,
    b: str,
    *,
    kind: str | None = None,
    joint: str | None = None,
    objectives: Any = None,
    frame: int | None = None,
) -> None:
    entry: dict[str, Any] = {"a": a, "b": b}
    for k, v in (("kind", kind), ("joint", joint), ("objectives", objectives)):
        if v is not None:
            entry[k] = v
    fid = _frame_id(frame)
    if fid is not None:
        entry["frame"] = fid
    _connects.append(entry)


@traced
def ray(
    r_or_name: Any,
    *args: Any,
    block: str | None = None,
    frame: int | None = None,
    **kw: Any,
) -> None:
    """Register one observation ray (a :class:`precis_geom.assembly.Ray`
    or ``pga.ray`` arguments) on a declared block."""
    from precis_geom.out import _ray_entry

    entry = _ray_entry(r_or_name, args, kw, block=block, frame=frame)
    if entry["block"] not in {b["name"] for b in _blocks}:
        raise ValueError(f"out.ray: block {entry['block']!r} is not declared")
    if any(r["name"] == entry["name"] for r in _rays):
        raise ValueError(f"out.ray: duplicate ray name {entry['name']!r}")
    _rays.append(entry)


@traced
def note(text: str) -> None:
    global _notes
    _notes = text if _notes is None else f"{_notes}\n{text}"


def finish() -> Path:
    """Write ``manifest.json`` + ``trace.json`` under the out dir. Idempotent."""
    global _finished
    if _finished:
        return _out_dir()
    _finished = True
    out_dir = _out_dir()
    if not out_dir.parent.is_dir() and not out_dir.exists():
        # Outside a workspace run (host import, tests) there is no /work —
        # nothing registered means nothing to write anyway.
        if not _blocks and not _payloads:
            return out_dir
    for fname, (payload, _enc) in _payloads.items():
        dest = out_dir / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema": _SCHEMA,
        "blocks": _blocks,
        "instances": _instances,
        "connects": _connects,
    }
    if _rays:
        manifest["rays"] = _rays
    if _notes:
        manifest["notes"] = _notes
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    doc = _trace.dump()
    if doc is not None:
        (out_dir / "trace.json").write_text(
            trace_schema.dumps(doc) + "\n", encoding="utf-8"
        )
    return out_dir


def _finish_quiet() -> None:
    """atexit: best-effort — a script that never called ``finish`` still
    tries to leave artifacts, but exit-time writes never raise."""
    try:
        finish()
    except Exception:
        pass


atexit.register(_finish_quiet)


def _reset() -> None:
    """Test hook: a second logical run in one process."""
    global _finished, _notes
    _finished = False
    _notes = None
    _blocks.clear()
    _instances.clear()
    _connects.clear()
    _rays.clear()
    _payloads.clear()


__all__ = ["block", "connect", "finish", "instance", "note", "ray"]

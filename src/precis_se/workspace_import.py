"""Workspace import — the trusted side of ``/work/out``
(structure-workspaces slice 0, docs/backlog/structure-workspaces.md
§Import boundary).

A workspace script never writes the DB: it writes ``out/manifest.json``
plus one geometry file per block (and ``out/trace.json``, the run's
construction trace — :mod:`precis_geom.trace_schema`, the module
sw-s2's capture writes against). This module is the
trust boundary: it validates the harvested tree and imports it into the
linked se design inside ONE :func:`precis_se.persist.tree_mutation`, so
the minted designs, the block writes and the trace chunk commit or roll
back together.

Manifest shape (``additionalProperties: false`` — an unknown key is the
run's failure, refused naming the key)::

    {"schema": 1,
     "blocks": [{"name", "file", "scale": "atomic"|"macro",
                 "ports"?: [{"name", "atom"?, "roles"?, "direction"?,
                             "expected_element"?, "expected_hybridization"?,
                             "frame"?}],
                 "relax"?, "desc"?, "frame"?, "bonds"?}],
     "instances"?: [{"name", "template", "parent"?, "pose"?, "rot"?,
                     "pose_unit"?, "rot_unit"?, "frame"?}],
     "connects"?: [{"a": "blk.port", "b": "blk.port", "kind"?, "joint"?,
                    "objectives"?, "frame"?}],
     "rays"?: [{"name", "block", "length", "origin"?, "direction"?,
                "kind"?, "fields"?, "step"?, "reach"?, "in"?, "anchor"?,
                "frame"?}],
     "notes"?,
     "status"?="failed", "error"?}   ← a failed run's record; refused

* ``rays`` (``out.ray``, structure-workspaces 2r): observation rays as
  block children (:mod:`precis_se.rays`) — ``frame`` is the maker's trace
  frame (the ray's ``made-by:``), ``in`` its reference frame. They
  replace this workspace's previous rays; one the run stops writing
  retires.

* atomic ``file``: ``blocks/<name>.extxyz``
  (:func:`precis.structure.importers.extxyz.extxyz_to_scene`); bonds ride
  ``blocks/<name>.bonds.json`` (or the entry's ``bonds`` path).
* macro ``file``: ``blocks/<name>.cad.json`` — a JSON-encoded
  :class:`~precis.cad.scene.SceneSpec` (``nodes``/``components``/``meta``;
  a node's ``frame`` reaches :attr:`precis.cad.fold.Instance.frame` on
  build) — or a plain-text file holding the cad DSL source, which parses
  to the same spec.

Ownership/replace: the design's ``refs.meta['workspaces'][<slug>]``
records what the last import made (``{blocks, instances, connects}``).
What the new manifest omits is retired — block rows via ``save_tree``,
the bound structure/cad ref via ``retire_ref``; ``restore_ref`` brings it
back. A same-named block takes over: its structure ref is rewritten in
place (version bump) and ports/connects re-resolve — a port that vanished
is a ``port.missing`` echo finding, never a silent drop.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from precis.cad.dsl import DslError
from precis.cad.dsl import parse as parse_config
from precis.cad.scene import (
    NodeSpec,
    SceneError,
    SceneSpec,
    coerce_pattern,
    parse_source,
)
from precis.errors import BadInput, NotFound
from precis.store.types import ChunkInsert
from precis.structure.importers.extxyz import bonds_from_json, extxyz_to_scene
from precis.utils.units import UnitRequiredError
from precis_geom import trace_schema
from precis_se import persist, traceview
from precis_se.atomic.generate import (
    PendingGenerate,
    finish_generate,
    generated_record,
)
from precis_se.ops import OpError, SeTree, apply_ops
from precis_se.rays import DEFAULT_REACH_A, DEFAULT_STEP_A

log = logging.getLogger(__name__)

#: Import caps, refused before any write (spec §Import boundary). Sized
#: off the harvest caps — a file bigger than the harvest file cap could
#: never have arrived through the sandbox anyway.
MAX_WS_FILE_BYTES = 2_000_000
MAX_WS_TRACE_BYTES = 8_000_000
MAX_WS_OUT_BYTES = 32_000_000
MAX_WS_BLOCKS = 256
MAX_WS_BLOCK_ATOMS = 100_000

#: ``status``/``error`` are a failed run's record keys (``Run.fail``) —
#: legal top-level shape so the vet reaches the run-failed refusal
#: instead of reporting them as unknown keys (gr514210).
_TOP_KEYS = frozenset(
    {"schema", "blocks", "instances", "connects", "rays", "notes", "status", "error"}
)
#: An ``out.ray`` entry (structure-workspaces 2r): ``frame`` is the
#: maker's trace frame id, as on every manifest entry; ``in`` is the
#: ray's own reference frame (omitted = the block, ``'world'``,
#: ``'port:<name>'``) — the ray definition's ``frame``.
_RAY_KEYS = frozenset(
    {
        "name",
        "block",
        "origin",
        "direction",
        "length",
        "kind",
        "fields",
        "step",
        "reach",
        "in",
        "anchor",
        "bundle",
        "frame",
    }
)
_BLOCK_KEYS = frozenset(
    {"name", "file", "scale", "ports", "relax", "desc", "frame", "bonds"}
)
_PORT_KEYS = frozenset(
    {
        "name",
        "atom",
        "roles",
        "direction",
        "expected_element",
        "expected_hybridization",
        "frame",
        "pose",
        "annotations",
    }
)
_INSTANCE_KEYS = frozenset(
    {
        "name",
        "template",
        "parent",
        "pose",
        "rot",
        "pose_unit",
        "rot_unit",
        "linear",
        "polar",
        "frame",
    }
)
_CONNECT_KEYS = frozenset({"a", "b", "kind", "joint", "objectives", "frame"})
_CAD_NODE_KEYS = frozenset(
    {"name", "op", "config", "component", "loc", "rot", "pattern", "blend", "frame"}
)
_SCALES = ("atomic", "macro")


def _unknown_keys(d: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    for k in d:
        if k not in allowed:
            raise BadInput(
                f"manifest {where}: unknown key {k!r}",
                next=f"drop it — {where} accepts {sorted(allowed)}",
            )


#: Port annotation keys the ray macro leg reads, with their type check —
#: ``diameter``/``depth`` metres (> 0), ``thread`` a metric designation.
_NUMERIC_ANNOTATIONS = ("diameter", "depth")


def _vet_port_annotations(p: dict[str, Any], where: str) -> None:
    """A manifest port's ``annotations``: a dict; ``diameter``/``depth``
    positive numbers (m); ``thread`` text. Refused naming the port."""
    ann = p.get("annotations")
    if ann is None:
        return
    nxt = (
        "annotations={'thread': 'M5x0.8', 'depth': 0.008} or "
        "{'diameter': 0.0055} — metres, bare numbers"
    )
    if not isinstance(ann, dict):
        raise BadInput(
            f"manifest {where} ({p.get('name')!r}): 'annotations' must be an "
            f"object, got {ann!r}",
            next=nxt,
        )
    for key in _NUMERIC_ANNOTATIONS:
        if key not in ann:
            continue
        val = ann[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not val > 0:
            raise BadInput(
                f"manifest {where} ({p.get('name')!r}): annotations.{key} must be a "
                f"positive number of metres, got {val!r}",
                next=nxt,
            )
    if "thread" in ann and not isinstance(ann["thread"], str):
        raise BadInput(
            f"manifest {where} ({p.get('name')!r}): annotations.thread must be "
            f"text like 'M5x0.8', got {ann['thread']!r}",
            next=nxt,
        )


@dataclass(frozen=True)
class BlockEntry:
    """One validated manifest block."""

    name: str
    file: str
    scale: str
    ports: list[dict[str, Any]]
    relax: dict[str, Any] | None
    desc: str | None
    frame: int | None
    bonds: str | None


def _vet_manifest(
    data: Any, site: Callable[[Any], str] | None = None
) -> dict[str, Any]:
    """Validate the manifest; refusals name the offending key.

    ``site`` maps an entry's ``frame`` stamp to ``' at file:line
    (frame N)'`` — ``import_workspace_run``'s trace lookup. Without it
    the same refusals stand, minus the script locations."""

    def _no_loc(frame: Any) -> str:
        return ""

    loc_of = site or _no_loc
    if not isinstance(data, dict):
        raise BadInput("manifest.json must be a JSON object")
    _unknown_keys(data, _TOP_KEYS, "manifest.json")
    if data.get("status") == "failed":
        err = data.get("error") or {}
        detail = (
            f" — {err['type']}: {err.get('message', '')}"
            if isinstance(err, dict) and err.get("type")
            else ""
        )
        raise BadInput(
            f"manifest.json: run failed{detail}",
            next="fix the script and re-run — a failed run's out/ is a "
            "record of where it stopped, not importable state",
        )
    if data.get("schema") != 1:
        raise BadInput(f"manifest.json: schema must be 1, got {data.get('schema')!r}")
    blocks_raw = data.get("blocks")
    if not isinstance(blocks_raw, list):
        raise BadInput("manifest.json: 'blocks' must be a list (empty = clear)")
    if len(blocks_raw) > MAX_WS_BLOCKS:
        raise BadInput(
            f"manifest.json: {len(blocks_raw)} blocks exceeds the "
            f"{MAX_WS_BLOCKS} blocks-per-run cap"
        )
    blocks: list[BlockEntry] = []
    names: set[str] = set()
    for i, raw in enumerate(blocks_raw):
        where = f"blocks[{i}]"
        if not isinstance(raw, dict):
            raise BadInput(f"manifest {where}: must be an object")
        _unknown_keys(raw, _BLOCK_KEYS, where)
        name = str(raw.get("name") or "").strip()
        file = str(raw.get("file") or "").strip()
        if not name or not file:
            raise BadInput(f"manifest {where}: needs 'name' and 'file'")
        if name in names:
            raise BadInput(f"manifest {where}: duplicate block name {name!r}")
        names.add(name)
        scale = str(raw.get("scale") or "").strip().lower()
        if scale not in _SCALES:
            raise BadInput(
                f"manifest {where}: 'scale' must be one of {_SCALES}, "
                f"got {raw.get('scale')!r}",
                options=list(_SCALES),
            )
        ports = raw.get("ports") or []
        if not isinstance(ports, list):
            raise BadInput(f"manifest {where}: 'ports' must be a list")
        for j, p in enumerate(ports):
            pw = f"{where}.ports[{j}]"
            if not isinstance(p, dict):
                raise BadInput(f"manifest {pw}: must be an object")
            _unknown_keys(p, _PORT_KEYS, pw)
            if not str(p.get("name") or "").strip():
                raise BadInput(f"manifest {pw}: needs 'name'")
            _vet_port_annotations(p, pw)
            if scale == "macro" and p.get("atom") is not None:
                raise BadInput(
                    f"manifest {pw}: 'atom' binds a port to an extxyz label — "
                    "only atomic blocks carry atoms"
                )
        relax = raw.get("relax")
        if relax is not None and not isinstance(relax, dict):
            raise BadInput(f"manifest {where}: 'relax' must be an object")
        frame = raw.get("frame")
        bonds_path = raw.get("bonds")
        blocks.append(
            BlockEntry(
                name=name,
                file=file,
                scale=scale,
                ports=[dict(p) for p in ports],
                relax=dict(relax) if relax is not None else None,
                desc=str(raw["desc"]).strip() if raw.get("desc") is not None else None,
                frame=int(frame) if frame is not None else None,
                bonds=str(bonds_path).strip() if bonds_path is not None else None,
            )
        )
    for field_name, allowed in (
        ("instances", _INSTANCE_KEYS),
        ("connects", _CONNECT_KEYS),
    ):
        raw_list = data.get(field_name) or []
        if not isinstance(raw_list, list):
            raise BadInput(f"manifest.json: {field_name!r} must be a list")
        for i, raw in enumerate(raw_list):
            where = f"{field_name}[{i}]"
            if not isinstance(raw, dict):
                raise BadInput(f"manifest {where}: must be an object")
            _unknown_keys(raw, allowed, where)
    # An instance may not share a name with any block — templates and
    # instances live in the ONE block namespace, which is why the
    # workspace convention prefixes template blocks ``t_`` (gr514240:
    # the op layer's generic "duplicate block name" named neither the
    # collision nor the convention).
    block_pos = {b.name: j for j, b in enumerate(blocks)}
    for i, inst in enumerate(data.get("instances") or []):
        iname = str(inst.get("name") or "").strip()
        if not iname or iname not in block_pos:
            continue
        j = block_pos[iname]
        loc_bits = []
        for w, f in (
            (f"blocks[{j}]", blocks[j].frame),
            (f"instances[{i}]", inst.get("frame")),
        ):
            s = loc_of(f)
            if s:
                loc_bits.append(f"{w}{s}")
        loc = f" — script: {', '.join(loc_bits)}" if loc_bits else ""
        raise BadInput(
            f"manifest instances[{i}]: instance name {iname!r} collides "
            f"with blocks[{j}] of the same name — templates and "
            f"instances share one namespace{loc}",
            next=f"rename the block to 't_{iname}' and point the "
            "instance's template at it (the t_ prefix convention: "
            "template 't_wheel', instance 'wheel')",
        )
    rays_raw = data.get("rays") or []
    if not isinstance(rays_raw, list):
        raise BadInput("manifest.json: 'rays' must be a list")
    ray_names: set[str] = set()
    for i, raw in enumerate(rays_raw):
        where = f"rays[{i}]"
        if not isinstance(raw, dict):
            raise BadInput(f"manifest {where}: must be an object")
        _unknown_keys(raw, _RAY_KEYS, where)
        rname = str(raw.get("name") or "").strip()
        rblock = str(raw.get("block") or "").strip()
        if not rname or not rblock or raw.get("length") is None:
            raise BadInput(f"manifest {where}: needs 'name', 'block' and 'length'")
        if rblock not in names:
            raise BadInput(
                f"manifest {where}: block {rblock!r} is not a manifest block",
                next="a ray rides a block this run writes — out.ray(…, block=<name>)",
            )
        if rname in ray_names:
            raise BadInput(f"manifest {where}: duplicate ray name {rname!r}")
        ray_names.add(rname)
    return {
        "blocks": blocks,
        "instances": [dict(x) for x in data.get("instances") or []],
        "connects": [dict(x) for x in data.get("connects") or []],
        "rays": [dict(x) for x in rays_raw],
        "notes": data.get("notes"),
    }


def _read_file(out_dir: Path, rel: str, *, cap: int, what: str) -> str:
    """One file under ``out_dir`` — confined, capped, refused by name."""
    p = (out_dir / rel).resolve()
    root = out_dir.resolve()
    if root not in p.parents and p != root:
        raise BadInput(f"manifest: {what} path escapes out/: {rel!r}")
    if not p.is_file():
        raise BadInput(
            f"manifest: {what} not found: {rel!r}",
            next="the script must write it under out/ before exiting",
        )
    size = p.stat().st_size
    if size > cap:
        raise BadInput(
            f"manifest: {what} {rel!r} is {size} B, over the {cap} B file cap"
        )
    return p.read_text(encoding="utf-8")


def _out_total_bytes(out_dir: Path) -> int:
    total = 0
    for p in out_dir.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            if total > MAX_WS_OUT_BYTES:
                raise BadInput(
                    f"out/ totals over the {MAX_WS_OUT_BYTES} B cap — the "
                    "harvest refused this tree before import"
                )
    return total


def _spec_from_cad_json(text: str, *, what: str) -> SceneSpec:
    """``.cad.json`` — the SceneSpec (what the DSL parses to) as JSON."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadInput(f"{what}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise BadInput(f"{what}: expected {{'schema': 1, 'nodes': [...]}}")
    _unknown_keys(data, frozenset({"schema", "nodes", "components", "meta"}), what)
    nodes_raw = data.get("nodes")
    if not isinstance(nodes_raw, list):
        raise BadInput(f"{what}: 'nodes' must be a list")
    nodes: list[NodeSpec] = []
    for i, raw in enumerate(nodes_raw):
        where = f"{what} nodes[{i}]"
        if not isinstance(raw, dict):
            raise BadInput(f"{where}: must be an object")
        _unknown_keys(raw, _CAD_NODE_KEYS, where)
        name = str(raw.get("name") or "").strip()
        op = str(raw.get("op") or "add").strip()
        config = str(raw.get("config") or "").strip()
        if not name or not config:
            raise BadInput(f"{where}: needs 'name' and 'config'")
        loc = [float(x) for x in raw.get("loc") or [0.0, 0.0, 0.0]]
        rot = [float(x) for x in raw.get("rot") or [0.0, 0.0, 0.0]]
        frame = raw.get("frame")
        nodes.append(
            NodeSpec(
                name=name,
                op=op,
                config=config,
                component=str(raw.get("component") or "part"),
                loc=(loc[0], loc[1], loc[2]),
                rot=(rot[0], rot[1], rot[2]),
                pattern=coerce_pattern(raw.get("pattern")),
                blend=float(raw.get("blend") or 0.0),
                frame=int(frame) if frame is not None else None,
            )
        )
    spec = SceneSpec(
        nodes=nodes,
        components=[str(c) for c in data.get("components") or ["part"]],
        meta=dict(data.get("meta") or {"units": "mm"}),
    )
    return spec


def _block_envelope(scene: Any) -> str:
    """The workspace block's envelope: a sphere comfortably containing the
    atoms, canonical bare metres — the same sizing posture as
    :func:`precis_se.atomic.generate.generated_cell`."""
    import numpy as np

    from precis_se.atomic.generate import ingest_envelope
    from precis_se.atomic.generators._types import fmt_length_A

    carts = [scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()]
    r = max((float(np.linalg.norm(c)) for c in carts), default=1.0) + 5.0
    # Å-suffixed text through the one m-boundary, not a local factor
    # (tests/test_se_atomic_angstrom_seam.py).
    return ingest_envelope(f"sphere:r{fmt_length_A(r)}")


def _generated_ctx(
    *,
    workspace: str,
    run_job: int | None,
    script_sha: str | None,
    lock_sha: str | None,
    image_digest: str | None,
    params: dict[str, Any] | None,
    requested_by: str | None,
    trace_root: str | None,
    frame: int | None,
    relax: dict[str, Any] | None,
) -> dict[str, Any]:
    """The ``meta['generated']`` provenance record — which agent, which
    workspace, which script made the block (spec §Import boundary)."""
    ctx: dict[str, Any] = {
        "workspace": workspace,
        "run_job": run_job,
        "script_sha": script_sha,
        "lock_sha": lock_sha,
        "image_digest": image_digest,
        "params": params or {},
        "requested_by": requested_by,
        "trace": {"run_job": run_job, "root": trace_root},
    }
    if frame is not None:
        ctx["frame"] = frame
    if relax is not None:
        ctx["relax"] = relax
    return ctx


@dataclass
class TraceView:
    """A parsed ``trace.json`` — the shared ``precis_geom.trace_schema``
    wire dicts — plus the importer's own cap accounting (the writer's
    ``run.truncated`` flag OR the reader-side ``MAX_FRAMES`` head-keep,
    whichever trips first). ``schema`` is the doc's own version — the
    stored ``ws_trace`` chunk re-emits it unchanged (a schema-1 doc keeps
    its line-bearing keys and lacks the schema-2 fields)."""

    run: dict[str, Any]
    frames: list[dict[str, Any]]
    truncated: bool = False
    dropped: int = 0
    schema: int = trace_schema.SCHEMA_VERSION

    @property
    def root_key(self) -> str | None:
        """The root frame's key — ``generated.trace.root`` records it."""
        for f in self.frames:
            if f.get("parent") is None:
                return f["key"]
        return None

    def key_set(self) -> list[str]:
        """Every frame key in file order — what goldens compare."""
        return [f["key"] for f in self.frames]


def _load_trace(text: str) -> TraceView:
    """Parse + cap a ``trace.json`` through the shared schema module —
    ``loads`` refuses malformed shape naming the key; the importer maps
    that to ``BadInput`` and applies its own frame cap on top of the
    writer's ``run.truncated`` flag."""
    try:
        doc = trace_schema.loads(text)
    except ValueError as exc:
        raise BadInput(
            f"{exc}",
            next="the run's trace.json must match schema "
            f"{trace_schema.SCHEMA_VERSION} (precis_geom.trace_schema)",
        ) from exc
    frames = list(doc["frames"])
    truncated = bool((doc.get("run") or {}).get("truncated"))
    dropped = 0
    if len(frames) > trace_schema.MAX_FRAMES:
        dropped = len(frames) - trace_schema.MAX_FRAMES
        frames = frames[: trace_schema.MAX_FRAMES]
        truncated = True
    return TraceView(
        run=dict(doc.get("run") or {}),
        frames=frames,
        truncated=truncated,
        dropped=dropped,
        schema=int(doc.get("schema") or trace_schema.SCHEMA_VERSION),
    )


@dataclass
class ImportResult:
    """What one workspace import did — the run echo's data half."""

    design_slug: str
    run_ref: int | None = None
    imported: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    retired: list[dict[str, Any]] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    trace: TraceView | None = None

    def echo(self) -> str:
        lines = [
            f"workspace import into {self.design_slug!r}: "
            f"{len(self.imported)} block(s) written"
        ]
        if self.replaced:
            lines.append("replaced: " + ", ".join(self.replaced))
        for r in self.retired:
            lines.append(
                f"retired: {r['name']} ({r['kind']} {r['slug']}) — "
                f"restore_ref({r['ref_id']}) brings it back"
                if r["ref_id"] is not None
                else f"retired: {r['name']} (block)"
            )
        lines.extend(self.findings)
        return "\n".join(lines)


def _with_site(exc: BadInput, site_text: str) -> None:
    """Append ``' at <file>:<line> (frame <id>)'`` to a refusal's cause in
    place — mutating ``cause``/``args`` keeps the concrete exception type
    (:class:`UnitRequiredError` included), its ``next=`` and its
    ``options=`` untouched (gr514246)."""
    if site_text:
        exc.cause = f"{exc.cause}{site_text}"
        exc.args = (exc.cause,)


def _dsl_source_line(body: str, exc: Exception) -> str | None:
    """Best-effort ``line 3: '<source text>'`` for a cad-DSL parse
    failure — ``arg_name``'s own ``line N:`` when it carries one
    (placement/dim/joint tokens), else the first source token that
    re-raises the same error under a boundary parse (the shape
    ``config`` token, whose ``alias:key`` ``arg_name`` names no line —
    gr514246)."""
    arg = getattr(exc, "arg_name", None) or ""
    lines = body.splitlines()
    m = re.match(r"line (\d+):", arg)
    if m:
        n = int(m[1])
        if 1 <= n <= len(lines):
            text = lines[n - 1].split("#", 1)[0].strip()
            return f"line {n}: {text!r}" if text else f"line {n}"
        return f"line {n}"
    for n, raw in enumerate(lines, 1):
        src = raw.split("#", 1)[0].strip()
        for tok in src.split():
            if ":" not in tok:
                continue
            try:
                parse_config(tok, require_units=True)
            except Exception as exc2:
                if type(exc2) is type(exc) and str(exc2) == str(exc):
                    return f"line {n}: {src!r}"
                continue
    return None


def _macro_dsl_spec(entry: BlockEntry, body: str) -> SceneSpec:
    """Parse a macro block's cad-DSL source — every refusal names the
    block and (best effort) the DSL line (gr514246).
    :class:`UnitRequiredError` is re-raised as the same type — its
    ``next=`` unit hint survives — with the block and line folded into
    the cause; :class:`DslError` and :class:`SceneError` (a bare
    ``ValueError`` today — it would surface as Internal) become
    ``BadInput``."""
    try:
        return parse_source(body)
    except UnitRequiredError as exc:
        dsl = _dsl_source_line(body, exc)
        exc.cause = (
            f"block {entry.name!r} cad DSL"
            + (f" {dsl}" if dsl else "")
            + f": {exc.cause}"
        )
        exc.args = (exc.cause,)
        raise
    except DslError as exc:
        dsl = _dsl_source_line(body, exc)
        raise BadInput(
            f"block {entry.name!r}: cad DSL parse failed"
            + (f" ({dsl})" if dsl else "")
            + f": {exc}"
        ) from exc
    except SceneError as exc:
        raise BadInput(f"block {entry.name!r}: cad DSL parse failed: {exc}") from exc


def _apply(tree: SeTree, ops: list[dict[str, Any]], *, what: str, at: str = "") -> None:
    try:
        apply_ops(tree, ops)
    except OpError as exc:
        raise BadInput(f"workspace import: {what}: {exc}{at}") from exc


def import_workspace_run(
    store: Any,
    *,
    workspace: str,
    design: str,
    out_dir: str | Path,
    run_job: int | None = None,
    script_sha: str | None = None,
    lock_sha: str | None = None,
    image_digest: str | None = None,
    params: dict[str, Any] | None = None,
    requested_by: str | None = None,
    target: str = "design",
) -> ImportResult:
    """Import one workspace run's ``out/`` tree into an se design.

    ``design`` is the linked design's slug; ``target='fork'`` imports into
    a copy ``<slug>--<run_job|workspace>`` linked ``derived-from`` instead.
    Everything validates (manifest, caps, files, ops) before the single
    ``tree_mutation`` transaction writes anything; a bad manifest or a bad
    file refuses naming the key and writes nothing.
    """
    out_path = Path(out_dir)
    if not out_path.is_dir():
        raise BadInput(f"workspace out/ dir not found: {out_dir!r}")
    _out_total_bytes(out_path)
    manifest_text = _read_file(
        out_path, "manifest.json", cap=MAX_WS_FILE_BYTES, what="manifest"
    )
    try:
        manifest_raw = json.loads(manifest_text)
    except json.JSONDecodeError as exc:
        raise BadInput(f"manifest.json is not valid JSON: {exc}") from exc
    findings: list[str] = []
    trace: TraceView | None = None
    trace_path = out_path / "trace.json"
    if trace_path.is_file():
        if trace_path.stat().st_size > MAX_WS_TRACE_BYTES:
            raise BadInput(
                f"trace.json is {trace_path.stat().st_size} B, over the "
                f"{MAX_WS_TRACE_BYTES} B cap"
            )
        trace = _load_trace(trace_path.read_text(encoding="utf-8"))
        if trace.truncated:
            findings.append(
                f"trace.truncated: kept {len(trace.frames)} of "
                f"{len(trace.frames) + trace.dropped} frames"
            )
    else:
        findings.append("trace.missing: out/trace.json absent — importing without it")

    # Every refusal tied to a manifest entry appends the entry's script
    # site — `` at <file>:<line> (frame <id>)`` from the entry's ``frame``
    # stamp resolved against the run's trace (gr514246/gr514247). No
    # trace.json → no suffix; the errors keep their types and text.
    _tv = (
        traceview.Trace({"run": trace.run, "frames": trace.frames})
        if trace is not None
        else None
    )

    def _site(frame: Any) -> str:
        if _tv is None:
            return ""
        try:
            loc = traceview.site(_tv, int(frame))
        except (TypeError, ValueError):
            return ""
        return f" at {loc}" if loc else ""

    # Vet runs after the trace load so instance/block refusals cite the
    # entries' script sites too; everything still refuses before a write.
    manifest = _vet_manifest(manifest_raw, site=_site)

    # Parse every geometry file BEFORE the write transaction — caps and
    # refusals happen here so nothing half-imports.
    parsed: dict[str, tuple[Any, Any]] = {}
    for entry in manifest["blocks"]:
        try:
            body = _read_file(
                out_path,
                entry.file,
                cap=MAX_WS_FILE_BYTES,
                what=f"blocks[{entry.name!r}] file",
            )
            if entry.scale == "atomic":
                scene = extxyz_to_scene(body)
                if len(scene.atoms) > MAX_WS_BLOCK_ATOMS:
                    raise BadInput(
                        f"block {entry.name!r}: {len(scene.atoms)} atoms exceeds "
                        f"the {MAX_WS_BLOCK_ATOMS} atoms-per-block cap"
                    )
                bonds_rel = entry.bonds or str(
                    Path(entry.file).with_suffix(".bonds.json")
                )
                if (out_path / bonds_rel).is_file():
                    scene.bonds = bonds_from_json(
                        _read_file(
                            out_path,
                            bonds_rel,
                            cap=MAX_WS_FILE_BYTES,
                            what=f"{entry.name!r} bonds",
                        )
                    )
                parsed[entry.name] = ("atomic", scene)
            else:
                if entry.file.endswith(".json"):
                    spec = _spec_from_cad_json(body, what=f"{entry.name!r}.cad.json")
                else:
                    spec = _macro_dsl_spec(entry, body)
                parsed[entry.name] = ("macro", spec)
        except BadInput as exc:
            _with_site(exc, _site(entry.frame))
            raise

    ref = store.get_ref(kind="se", id=design)
    if ref is None:
        raise NotFound(f"se design {design!r} not found")
    if target == "fork":
        fork_slug = f"{design}--{run_job if run_job is not None else workspace}"
        with store.tx() as conn:
            fork_ref = store.insert_ref(
                kind="se",
                slug=fork_slug,
                title=f"{ref.title or design} (workspace {workspace} fork)",
                meta={"description": f"workspace fork of {design}"},
                conn=conn,
            )
            persist.save_tree(
                store,
                ref_id=fork_ref.id,
                tree=persist.tree_from_json(
                    persist.tree_to_json(persist.load_tree(store, ref.id)),
                    store=store,
                ),
                card_text=f"workspace fork of {design}",
                conn=conn,
            )
            store.add_link(
                src_ref_id=fork_ref.id,
                dst_ref_id=ref.id,
                relation="derived-from",
                set_by="workspace_import",
                conn=conn,
            )
        ref = fork_ref
        design = fork_slug
    elif target != "design":
        raise BadInput(
            f"workspace import target must be 'design' or 'fork', got {target!r}",
            options=["design", "fork"],
        )

    # The rays as the design stands BEFORE this import — a ray whose
    # events differ afterwards is ``ray.changed`` (slice 2r.2).
    rays_before = persist.observe_all_rays(store, int(ref.id))
    result = ImportResult(design_slug=design, trace=trace, run_ref=run_job)
    result.findings.extend(findings)
    block_entries = {e.name: e for e in manifest["blocks"]}
    inst_entries = {
        str(i["name"]): i
        for i in manifest["instances"]
        if str(i.get("name") or "").strip()
    }

    with persist.tree_mutation(store, ref.id) as conn:
        tree = persist.load_tree(store, ref.id)
        tree.own_slug = design
        tree.foreign = persist.foreign_resolver(store)

        # Which live blocks does THIS workspace own? Via the bound ref's
        # generated.workspace — one lookup per bound slug.
        bound_meta: dict[tuple[str, str], dict[str, Any] | None] = {}
        for node in tree.blocks.values():
            if node.bound_kind in ("structure", "cad") and node.bound:
                key = (node.bound_kind, node.bound)
                if key not in bound_meta:
                    bref = store.get_ref(
                        kind=key[0], id=key[1], include_deleted=True, conn=conn
                    )
                    bound_meta[key] = dict(bref.meta) if bref is not None else None
        owned = {
            n
            for n, node in tree.blocks.items()
            if node.bound_kind in ("structure", "cad")
            and node.bound
            and (bound_meta.get((node.bound_kind, node.bound)) or {})
            .get("generated", {})
            .get("workspace")
            == workspace
        }
        prev = ((ref.meta or {}).get("workspaces") or {}).get(workspace) or {}
        prev_blocks = set(prev.get("blocks") or []) | owned
        prev_instances = set(prev.get("instances") or [])
        prev_connects = {
            tuple(sorted((str(c.get("a")), str(c.get("b")))))
            for c in prev.get("connects") or []
        }

        manifest_blocks = set(block_entries)
        manifest_connects = {
            tuple(sorted((str(c.get("a", "")), str(c.get("b", "")))))
            for c in manifest["connects"]
        }

        # ── retire what the new manifest omits ──
        for name in sorted(prev_blocks - manifest_blocks):
            drop_node = tree.blocks.get(name)
            if drop_node is None:
                continue
            subtree = {name} | {n for n, b in tree.blocks.items() if b.parent == name}
            tree.connects = [
                c
                for c in tree.connects
                if c.a_block not in subtree and c.b_block not in subtree
            ]
            for n in subtree:
                gone = tree.blocks.pop(n)
                if gone.bound and gone.bound_kind in ("structure", "cad"):
                    bref = store.get_ref(kind=gone.bound_kind, id=gone.bound, conn=conn)
                    if bref is not None:
                        store.retire_ref(bref.id, conn=conn)
                        result.retired.append(
                            {
                                "name": n,
                                "kind": gone.bound_kind,
                                "slug": gone.bound,
                                "ref_id": int(bref.id),
                            }
                        )
        # stale instances + connects the manifest dropped
        for name in sorted(prev_instances - set(inst_entries)):
            inst_node = tree.blocks.get(name)
            if inst_node is not None and inst_node.template is not None:
                tree.connects = [
                    c for c in tree.connects if c.a_block != name and c.b_block != name
                ]
                tree.blocks.pop(name)
                result.retired.append(
                    {"name": name, "kind": "block", "slug": name, "ref_id": None}
                )
        drop_pairs = prev_connects - manifest_connects
        if drop_pairs:
            tree.connects = [
                c
                for c in tree.connects
                if tuple(sorted((f"{c.a_block}.{c.a_port}", f"{c.b_block}.{c.b_port}")))
                not in drop_pairs
            ]

        # ── write the manifest's blocks ──
        pendings: list[PendingGenerate] = []
        cad_saves: list[tuple[str, str, SceneSpec, str]] = []
        for entry in manifest["blocks"]:
            kind, payload = parsed[entry.name]
            bsite = _site(entry.frame)
            existing = tree.blocks.get(entry.name)
            if existing is not None and entry.name not in owned:
                raise BadInput(
                    f"workspace import: block name {entry.name!r} is taken by a "
                    f"block this workspace does not own{bsite}",
                    next="rename the block, or remove the other block first",
                )
            gen_ctx = _generated_ctx(
                workspace=workspace,
                run_job=run_job,
                script_sha=script_sha,
                lock_sha=lock_sha,
                image_digest=image_digest,
                params=params,
                requested_by=requested_by,
                trace_root=trace.root_key if trace else None,
                frame=entry.frame,
                relax=entry.relax,
            )
            ops: list[dict[str, Any]] = []
            if existing is None:
                ops.append(
                    {
                        "op": "add_block",
                        "name": entry.name,
                        "desc": entry.desc or f"workspace {workspace} block",
                    }
                )
            else:
                if entry.desc is not None:
                    ops.append(
                        {"op": "set_desc", "block": entry.name, "desc": entry.desc}
                    )
                result.replaced.append(entry.name)
            port_ops = []
            for p in entry.ports:
                po: dict[str, Any] = {
                    "op": "add_port",
                    "block": entry.name,
                    "name": p["name"],
                    "roles": p.get("roles") or [],
                }
                if p.get("direction") is not None:
                    po["direction"] = p["direction"]
                if p.get("expected_element") is not None:
                    po["expected_element"] = p["expected_element"]
                if p.get("expected_hybridization") is not None:
                    po["expected_hybridization"] = p["expected_hybridization"]
                ann = dict(p.get("annotations") or {})
                if p.get("frame") is not None:
                    ann["frame"] = int(p["frame"])
                if ann:
                    po["annotations"] = ann
                if p.get("pose") is not None:
                    po["pose"] = p["pose"]
                if p.get("atom") is not None:
                    po["atom"] = p["atom"]  # consumed below, not an op key
                port_ops.append((p, po))
            if existing is not None:
                for pname in set(existing.ports) - {p["name"] for p in entry.ports}:
                    ops.append(
                        {"op": "remove_port", "block": entry.name, "name": pname}
                    )
            for p, po in port_ops:
                atom = po.pop("atom", None)
                if existing is not None and p["name"] in existing.ports:
                    continue  # port exists; binding refreshed below
                ops.append(po)
            if kind == "atomic":
                ops.append({"op": "set_mode", "block": entry.name, "mode": "atomic"})
            _apply(tree, ops, what=f"block {entry.name!r}", at=bsite)
            node = tree.blocks[entry.name]
            if kind == "atomic":
                scene = payload
                struct_slug = f"{design}-{entry.name}"
                ports_map: dict[str, str] = {}
                for p in entry.ports:
                    atom = p.get("atom")
                    if atom is None:
                        continue
                    if str(atom) not in scene.atoms:
                        raise BadInput(
                            f"manifest blocks[{entry.name!r}]: port "
                            f"{p['name']!r} atom {atom!r} is not in "
                            f"{entry.file}{bsite}"
                        )
                    ports_map[str(p["name"])] = str(atom)
                # Port element gate — the same check prepare_generate runs.
                for p in entry.ports:
                    exp = p.get("expected_element")
                    if exp and p.get("atom") is not None:
                        got = scene.atoms[str(p["atom"])].element
                        if got != exp:
                            raise BadInput(
                                f"manifest blocks[{entry.name!r}]: port "
                                f"{p['name']!r} expects {exp!r}, atom "
                                f"{p['atom']!r} is {got!r}{bsite}"
                            )
                prior = store.get_ref(
                    kind="structure",
                    id=struct_slug,
                    include_deleted=True,
                    conn=conn,
                )
                if prior is not None:
                    prior_ws = ((prior.meta or {}).get("generated") or {}).get(
                        "workspace"
                    )
                    if prior_ws != workspace:
                        raise BadInput(
                            f"workspace import: structure slug {struct_slug!r} "
                            "is held by a design this workspace did not "
                            f"make{bsite}",
                            next="rename the block",
                        )
                    store.restore_ref(prior.id, conn=conn)
                pendings.append(
                    PendingGenerate(
                        block_name=entry.name,
                        struct_slug=struct_slug,
                        title=f"{entry.name} (workspace {workspace})",
                        scene=scene,
                        card_text=(
                            f"{entry.name} (atomistic structure). "
                            f"{len(scene.atoms)} atoms, {len(scene.bonds)} bonds."
                        ),
                        provenance=entry.desc or "",
                        ports_map=ports_map,
                        generated=generated_record("workspace", {}, extra=gen_ctx),
                    )
                )
            else:
                spec = payload
                cad_slug = f"{design}-{entry.name}"
                prior = store.get_ref(
                    kind="cad", id=cad_slug, include_deleted=True, conn=conn
                )
                if prior is not None:
                    prior_ws = ((prior.meta or {}).get("generated") or {}).get(
                        "workspace"
                    )
                    if prior_ws != workspace:
                        raise BadInput(
                            f"workspace import: cad slug {cad_slug!r} is "
                            "held by a design this workspace did not "
                            f"make{bsite}",
                            next="rename the block",
                        )
                    if prior.retired_at is not None:
                        store.restore_ref(prior.id, conn=conn)
                spec.meta["generated"] = generated_record(
                    "workspace", {}, extra=gen_ctx
                )
                cad_saves.append(
                    (
                        cad_slug,
                        f"{entry.name} (workspace {workspace})",
                        spec,
                        f"{entry.name} (cad design, workspace {workspace}).",
                    )
                )
                node.bound_kind = "cad"
                node.bound = cad_slug

        # ── instances + connects ──
        for inst in manifest["instances"]:
            name = str(inst["name"]).strip()
            existing_inst = tree.blocks.get(name)
            if existing_inst is not None and existing_inst.template is None:
                # A same-named ordinary block survived retirement — one
                # this workspace does not own (a manifest-owned sibling
                # is already refused by _vet_manifest). The op layer
                # would only say "duplicate block name" (gr514240).
                raise BadInput(
                    f"workspace import: instance {name!r} shares a name "
                    f"with block {name!r} already in the design — "
                    "templates and instances share one namespace"
                    f"{_site(inst.get('frame'))}",
                    next="rename the instance, or rename the block and "
                    "point the instance's template at it (the t_ prefix "
                    "convention)",
                )
            if existing_inst is not None:
                del tree.blocks[name]  # re-created below (pose/template update)
            # linear/polar make the row an array_block — the script-side
            # ``pga.tile(block, n, pitch=…)`` packed form, replayed through
            # the same op as a hand-authored array so the trees compare
            # byte-for-byte (structure-workspaces 2a).
            op: dict[str, Any] = {
                "op": (
                    "array_block"
                    if inst.get("linear") is not None or inst.get("polar") is not None
                    else "instance_block"
                ),
                "name": name,
                "template": inst["template"],
            }
            for k in (
                "parent",
                "pose",
                "rot",
                "pose_unit",
                "rot_unit",
                "linear",
                "polar",
            ):
                if inst.get(k) is not None:
                    op[k] = inst[k]
            _apply(
                tree,
                [op],
                what=f"instance {name!r}",
                at=_site(inst.get("frame")),
            )
        for c in manifest["connects"]:
            pair = tuple(sorted((str(c["a"]), str(c["b"]))))
            have = {
                tuple(sorted((f"{x.a_block}.{x.a_port}", f"{x.b_block}.{x.b_port}")))
                for x in tree.connects
            }
            if pair in have:
                continue
            op = {"op": "connect", "a": c["a"], "b": c["b"]}
            for k in ("kind", "joint", "objectives"):
                if c.get(k) is not None:
                    op[k] = c[k]
            _apply(
                tree,
                [op],
                what=f"connect {c['a']}—{c['b']}",
                at=_site(c.get("frame")),
            )

        # ── rays (slice 2r): the manifest's rays replace this workspace's
        # previous ones IN PLACE — a ray the run still writes keeps its
        # digest ring (the history ``against`` and ``ray.changed`` read);
        # a ray this run no longer writes retires ──
        prev_rays = {str(r) for r in prev.get("rays") or []}
        manifest_rays = {f"{r['block']}.{r['name']}" for r in manifest["rays"]}
        for ray_key in sorted(prev_rays - manifest_rays):
            rblock, _sep, rname = ray_key.partition(".")
            rnode = tree.blocks.get(rblock)
            if rnode is not None and rname in rnode.rays:
                _apply(
                    tree,
                    [{"op": "remove_ray", "block": rblock, "name": rname}],
                    what=f"ray {ray_key!r}",
                )
        for r in manifest["rays"]:
            rnode = tree.blocks[r["block"]]
            # every definition key, absent ones reset — a set_ray that
            # leaves out a key would keep the previous run's value
            fields_op: dict[str, Any] = {
                "block": r["block"],
                "name": r["name"],
                "length": r["length"],
                "origin": r.get("origin") or [0.0, 0.0, 0.0],
                "direction": r.get("direction") or [0.0, 0.0, 1.0],
                "kind": r.get("kind") or "probe",
                "fields": r.get("fields"),
                "step": r.get("step") if r.get("step") is not None else DEFAULT_STEP_A,
                "reach": r.get("reach")
                if r.get("reach") is not None
                else DEFAULT_REACH_A,
                "anchor": r.get("anchor"),
                "bundle": r.get("bundle"),
                "frame": r.get("in"),
            }
            if r["name"] in rnode.rays:
                _apply(
                    tree,
                    [{"op": "set_ray", **fields_op}],
                    what=f"ray {r['name']!r}",
                    at=_site(r.get("frame")),
                )
            else:
                _apply(
                    tree,
                    [{"op": "add_ray", **fields_op}],
                    what=f"ray {r['name']!r}",
                    at=_site(r.get("frame")),
                )
            ray_spec = rnode.rays[r["name"]]
            ray_spec.made_by = (
                {"frame": int(r["frame"])} if r.get("frame") is not None else None
            )

        # port.missing: connects whose endpoint port vanished on replace
        for cx in tree.connects:
            for blk, port in ((cx.a_block, cx.a_port), (cx.b_block, cx.b_port)):
                cx_node = tree.blocks.get(blk)
                if (
                    cx_node is not None
                    and cx_node.template is None
                    and port not in cx_node.ports
                ):
                    result.findings.append(
                        f"port.missing: {blk}.{port} — connect endpoint has no "
                        "matching port in this manifest"
                    )

        # The minted designs join this transaction (finish_generate/cad_save
        # conn=), then the tree — one commit for the whole import.
        for pending in pendings:
            finish_generate(store, tree, pending, conn=conn)
            result.imported.append(pending.block_name)
        for slug, title, spec, card in cad_saves:
            store.cad_save(slug=slug, title=title, spec=spec, card_text=card, conn=conn)
            result.imported.append(slug.rsplit("-", 1)[-1])

        persist.save_tree(
            store,
            ref_id=ref.id,
            tree=tree,
            card_text=f"{design}: workspace {workspace} import "
            f"({len(result.imported)} blocks)",
            conn=conn,
        )
        # Ownership record for the next import's drop set.
        conn.execute(
            "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
            (
                Jsonb(
                    {
                        "workspaces": {
                            workspace: {
                                "blocks": sorted(manifest_blocks),
                                "instances": sorted(inst_entries),
                                "connects": [
                                    {"a": c["a"], "b": c["b"]}
                                    for c in manifest["connects"]
                                ],
                                "rays": sorted(manifest_rays),
                                "run_job": run_job,
                                "script_sha": script_sha,
                            }
                        }
                    }
                ),
                ref.id,
            ),
        )
        # trace.json + manifest stored once on the run job (design ref when
        # the import ran without a job — slice 0's containerless path).
        home = run_job if run_job is not None else ref.id
        row = conn.execute(
            "SELECT COALESCE(MAX(ord), -1) + 1 FROM chunks WHERE ref_id = %s",
            (home,),
        ).fetchone()
        assert row is not None
        ord_next = int(row[0])
        chunks = [
            ChunkInsert(
                ord=ord_next,
                text=manifest_text,
                meta={"chunk_kind": "ws_manifest", "workspace": workspace},
            )
        ]
        if trace is not None:
            chunks.append(
                ChunkInsert(
                    ord=ord_next + 1,
                    text=trace_schema.dumps(
                        {
                            "schema": trace.schema,
                            "run": trace.run,
                            "frames": trace.frames,
                        }
                    ),
                    meta={
                        "chunk_kind": "ws_trace",
                        "workspace": workspace,
                        "truncated": trace.truncated,
                    },
                )
            )
        store.chunks.insert_chunks(home, chunks, conn=conn)

    persist.sync_realized_by(store, ref.id, tree)
    # Every import re-observes; the comparison needs the committed
    # geometry, so it runs after the import's transaction.
    result.findings.extend(
        persist.reobserve_after_rebuild(
            store,
            int(ref.id),
            rays_before,
            service={"import": run_job, "workspace": workspace},
        )
    )
    return result

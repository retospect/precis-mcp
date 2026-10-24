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
     "notes"?}

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from precis.cad.dsl import DslError
from precis.cad.scene import NodeSpec, SceneSpec, coerce_pattern, parse_source
from precis.errors import BadInput, NotFound
from precis.store.types import ChunkInsert
from precis.structure.importers.extxyz import bonds_from_json, extxyz_to_scene
from precis_geom import trace_schema
from precis_se import persist
from precis_se.atomic.generate import (
    PendingGenerate,
    finish_generate,
    generated_record,
)
from precis_se.ops import OpError, SeTree, apply_ops

log = logging.getLogger(__name__)

#: Import caps, refused before any write (spec §Import boundary). Sized
#: off the harvest caps — a file bigger than the harvest file cap could
#: never have arrived through the sandbox anyway.
MAX_WS_FILE_BYTES = 2_000_000
MAX_WS_TRACE_BYTES = 8_000_000
MAX_WS_OUT_BYTES = 32_000_000
MAX_WS_BLOCKS = 256
MAX_WS_BLOCK_ATOMS = 100_000

_TOP_KEYS = frozenset({"schema", "blocks", "instances", "connects", "notes"})
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


def _vet_manifest(data: Any) -> dict[str, Any]:
    """Validate the manifest; refusals name the offending key."""
    if not isinstance(data, dict):
        raise BadInput("manifest.json must be a JSON object")
    _unknown_keys(data, _TOP_KEYS, "manifest.json")
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
    return {
        "blocks": blocks,
        "instances": [dict(x) for x in data.get("instances") or []],
        "connects": [dict(x) for x in data.get("connects") or []],
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
    whichever trips first)."""

    run: dict[str, Any]
    frames: list[dict[str, Any]]
    truncated: bool = False
    dropped: int = 0

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


def _apply(tree: SeTree, ops: list[dict[str, Any]], *, what: str) -> None:
    try:
        apply_ops(tree, ops)
    except OpError as exc:
        raise BadInput(f"workspace import: {what}: {exc}") from exc


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
        manifest = _vet_manifest(json.loads(manifest_text))
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

    # Parse every geometry file BEFORE the write transaction — caps and
    # refusals happen here so nothing half-imports.
    parsed: dict[str, tuple[Any, Any]] = {}
    for entry in manifest["blocks"]:
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
            bonds_rel = entry.bonds or str(Path(entry.file).with_suffix(".bonds.json"))
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
                try:
                    spec = parse_source(body)
                except DslError as exc:
                    raise BadInput(
                        f"block {entry.name!r}: cad DSL parse failed: {exc}"
                    ) from exc
            parsed[entry.name] = ("macro", spec)

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
            existing = tree.blocks.get(entry.name)
            if existing is not None and entry.name not in owned:
                raise BadInput(
                    f"workspace import: block name {entry.name!r} is taken by a "
                    "block this workspace does not own",
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
                if p.get("frame") is not None:
                    po["annotations"] = {"frame": int(p["frame"])}
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
            _apply(tree, ops, what=f"block {entry.name!r}")
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
                            f"{entry.file}"
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
                                f"{p['atom']!r} is {got!r}"
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
                            "is held by a design this workspace did not make",
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
                            "held by a design this workspace did not make",
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
            if name in tree.blocks and tree.blocks[name].template is not None:
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
            _apply(tree, [op], what=f"instance {name!r}")
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
            _apply(tree, [op], what=f"connect {c['a']}—{c['b']}")

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
                            "schema": trace_schema.SCHEMA_VERSION,
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
    return result

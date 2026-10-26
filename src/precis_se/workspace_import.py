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
                 "relax"?, "desc"?, "frame"?, "bonds"?, "bonds_sha"?,
                 "mode"?, "sha"?, "notes"?,
                 "measures"?: [{"name", "value"?|"min"?/"max"?, "unit"?,
                                "strength"?, "reason"?, "relation"?,
                                "datum"?, "origin"?}],
                 "loads"?, "template"?, "findings"?, "census"?}],
     "instances"?: [{"name", "template", "parent"?, "pose"?, "rot"?,
                     "pose_unit"?, "rot_unit"?, "frame"?}],
     "connects"?: [{"a": "blk.port", "b": "blk.port", "kind"?, "joint"?,
                    "objectives"?, "frame"?}],
     "rays"?: [{"name", "block", "length", "origin"?, "direction"?,
                "kind"?, "fields"?, "step"?, "reach"?, "in"?, "anchor"?,
                "frame"?}],
     "scenario"?, "notes"?,
     "status"?="failed", "error"?}   ← a failed run's record; refused

* ``rays`` (``out.ray``, structure-workspaces 2r): observation rays as
  block children (:mod:`precis_se.rays`) — ``frame`` is the maker's trace
  frame (the ray's ``made-by:``), ``in`` its reference frame. They
  replace this workspace's previous rays; one the run stops writing
  retires.
* ``mode``/``measures``/``loads`` on a block (gr514245): replayed as the
  ``set_mode``/``add_measure``/``set_load`` ops — the block's
  manufacturing mode, its named measures and its load declarations. Like
  ports/rays they are replace semantics: a facet the new manifest drops
  is cleared on the block it was declared on.
* ``notes`` on a block (gr514245): a list of ``{"name", "kind", "text",
  "re"?, "about"?, "origin"?}`` — ``add_note`` entries on the design's
  interrogation ledger (``about`` defaults to the declaring block,
  ``origin`` to ``generated``). The ledger is append-only, so notes are
  additive, not replace: an identical note already on the ledger is
  skipped, a same-named different one refuses.
* ``scenario`` (gr514245): the design's governing production scenario id
  (:func:`precis.design.scenarios.set_design_scenario`), existence-vetted
  before any write.
* ``template`` on a block (gr514241): ``true`` marks a library part —
  instanced by ``out.instance`` entries, never itself placed in the
  assembly (excluded from the tree view's part walk, the fill fraction,
  the envelope checks and the clearance surface; listed under
  "templates"). The declared set persists in this workspace's record
  under ``refs.meta`` and :func:`precis_se.persist.load_tree` stamps it
  back onto the block as ``template_only`` — instance-named
  template-ness needs no flag at all (:func:`precis_se.ops.
  template_names` is the union).
* ``sha``/``bonds_sha`` on a block (gr514248/gr517643): the writer's
  sha256 of the geometry file's / the bonds sidecar's bytes. Declared
  means verified: the file's bytes must hash to it or the whole import
  refuses naming the path and both digests — the inline
  ``{relpath: text}`` door retypes every byte, and this is the only
  check that catches a corrupted upload. ``sha`` is also stamped back
  onto the bound ref as ``meta.generated.sha`` so the next import can
  answer changed/unchanged without re-diffing geometry (gr514249).
  ``bonds``/``bonds_sha`` are atomic-only keys.
* ``census`` on a block (gr514059): a hexfold Net's structural census
  ``out.block`` stamps — junction/seam/port atom sets and the
  degree/valence summary in the block's ``a<El><n>`` label space.
  Run-record only: vetted to be an object, never imported.

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

import hashlib
import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
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
from precis.design import scenarios as design_scenarios
from precis.errors import BadInput, NotFound
from precis.store.types import ChunkInsert
from precis.structure.importers.extxyz import bonds_from_json, extxyz_to_scene
from precis.utils.md_parse import _normalize_segment
from precis.utils.units import UnitRequiredError, parse_quantity
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
MAX_WS_FILES = 512

#: A workspace slug doubles as a directory name under the python-kind
#: stage dir (:mod:`precis.handlers._python_ws`) — this regex is the
#: traversal gate (gr517172): lowercase letters/digits/'-', starting
#: alphanumeric, ≤64 chars.
_WS_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _vet_workspace_slug(workspace: str) -> None:
    """Refuse a workspace slug that is not a safe identifier token — it
    lands in ``stage_dir/<slug>`` paths, ``ws-<slug>--`` file slugs and
    ``<design>--<workspace>`` fork slugs, so ``../../app`` must never
    reach them."""
    if not _WS_SLUG_RE.fullmatch(workspace):
        raise BadInput(
            f"workspace slug {workspace!r} must match ^[a-z0-9][a-z0-9-]{{0,63}}$",
            next="lowercase letters/digits/'-', starting alphanumeric — "
            "e.g. 'hexfold-prism'",
        )


#: ``status``/``error`` are a failed run's record keys (``Run.fail``) —
#: legal top-level shape so the vet reaches the run-failed refusal
#: instead of reporting them as unknown keys (gr514210).
_TOP_KEYS = frozenset(
    {
        "schema",
        "blocks",
        "instances",
        "connects",
        "rays",
        "notes",
        "scenario",
        "status",
        "error",
        "fit",
        "fit_rules",
    }
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
    {
        "name",
        "file",
        "scale",
        "ports",
        "relax",
        "desc",
        "frame",
        "bonds",
        "mode",
        "measures",
        "loads",
        "notes",
        "template",
        "findings",
        "census",
        "sha",
        "bonds_sha",
    }
)
#: A ``notes`` entry's keys — the ``add_note`` op vocabulary (gr514245).
_NOTE_KEYS = frozenset({"name", "kind", "text", "re", "about", "origin"})
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
#: A ``measures`` entry's keys — the ``add_measure`` op vocabulary. Vetted
#: here because the op picks known fields and never sees a stray (the
#: swallowed-facet rule — an op that can't tell typo from key must be
#: told the whole accepted set by its caller).
_MEASURE_KEYS = frozenset(
    {
        "name",
        "value",
        "min",
        "max",
        "unit",
        "origin",
        "relation",
        "strength",
        "reason",
        "provenance",
        "datum",
        "datum_pin",
        "measurand",
    }
)
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
    mode: str | None
    measures: list[dict[str, Any]]
    loads: dict[str, Any] | None
    notes: list[dict[str, Any]]
    template: bool
    findings: list[str]
    sha: str | None
    bonds_sha: str | None


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
    note_names: set[str] = set()
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
        mode = raw.get("mode")
        if mode is not None and not str(mode).strip():
            raise BadInput(
                f"manifest {where}: 'mode' must be a non-empty mode key "
                f"('fdm/asa', 'purchase', 'stock-cut/steel-1045', …)"
            )
        measures = raw.get("measures")
        if measures is not None:
            if not isinstance(measures, list):
                raise BadInput(f"manifest {where}: 'measures' must be a list")
            for j, mm in enumerate(measures):
                mw = f"{where}.measures[{j}]"
                if not isinstance(mm, dict):
                    raise BadInput(f"manifest {mw}: must be an object")
                _unknown_keys(mm, _MEASURE_KEYS, mw)
                if not str(mm.get("name") or "").strip():
                    raise BadInput(f"manifest {mw}: needs 'name'")
        loads = raw.get("loads")
        if loads is not None and not isinstance(loads, dict):
            raise BadInput(
                f"manifest {where}: 'loads' must be an object — the "
                "set_load vocabulary (force, torque, fixed, duty, cycles, …)"
            )
        notes = raw.get("notes")
        if notes is not None:
            if not isinstance(notes, list):
                raise BadInput(f"manifest {where}: 'notes' must be a list")
            for j, nn in enumerate(notes):
                nw = f"{where}.notes[{j}]"
                if not isinstance(nn, dict):
                    raise BadInput(f"manifest {nw}: must be an object")
                _unknown_keys(nn, _NOTE_KEYS, nw)
                nname = str(nn.get("name") or "").strip()
                if (
                    not nname
                    or not str(nn.get("kind") or "").strip()
                    or not str(nn.get("text") or "").strip()
                ):
                    raise BadInput(
                        f"manifest {nw}: needs 'name', 'kind' and 'text' — "
                        "the add_note vocabulary"
                    )
                if nname in note_names:
                    raise BadInput(
                        f"manifest {nw}: duplicate note name {nname!r} — "
                        "note names are unique per design"
                    )
                note_names.add(nname)
        template = raw.get("template")
        if template is not None and not isinstance(template, bool):
            raise BadInput(
                f"manifest {where}: 'template' must be true or false — "
                "true marks a library part instanced by out.instance, "
                "never itself placed in the assembly (gr514241)"
            )
        findings = raw.get("findings")
        if findings is not None and (
            not isinstance(findings, list)
            or any(not isinstance(f, str) for f in findings)
        ):
            raise BadInput(
                f"manifest {where}: 'findings' must be a list of strings — "
                "the Geom findings out.block stamps "
                "(chirality.flipped &c., gr514205)"
            )
        census = raw.get("census")
        if census is not None and not isinstance(census, dict):
            raise BadInput(
                f"manifest {where}: 'census' must be an object — the "
                "junction/seam/valence summary out.block stamps for a "
                "hexfold Net (gr514059); run-record only, the importer "
                "does not read it"
            )
        for sha_key in ("sha", "bonds_sha"):
            sv = raw.get(sha_key)
            if sv is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", str(sv)):
                raise BadInput(
                    f"manifest {where}: {sha_key!r} must be a sha256 hex "
                    f"digest (64 hex chars), got {sv!r}"
                )
        if scale != "atomic" and (
            raw.get("bonds") is not None or raw.get("bonds_sha") is not None
        ):
            raise BadInput(
                f"manifest {where}: 'bonds'/'bonds_sha' belong to atomic "
                "blocks only — a macro block carries no bond list"
            )
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
                mode=str(mode).strip() if mode is not None else None,
                measures=[dict(m) for m in measures or []],
                loads=dict(loads) if loads is not None else None,
                notes=[dict(n) for n in notes or []],
                template=bool(template),
                findings=[str(f) for f in findings or []],
                sha=str(raw["sha"]).strip().lower() if raw.get("sha") else None,
                bonds_sha=(
                    str(raw["bonds_sha"]).strip().lower()
                    if raw.get("bonds_sha")
                    else None
                ),
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
    fit = data.get("fit")
    if fit is not None and not isinstance(fit, dict):
        raise BadInput(
            "manifest.json: 'fit' must be an object when present — the "
            "run's own fit report (recomputed on import, never trusted)",
            next="omit 'fit', or pass the run's out/fit.json object "
            "({'rows': […], …}); the server recomputes it either way",
        )
    fit_rules = data.get("fit_rules")
    if fit_rules is not None:
        if not isinstance(fit_rules, dict):
            raise BadInput(
                "manifest.json: 'fit_rules' must be an object",
                next="e.g. {'min_clearance': [{'pair': ['ia', 'ib'], "
                "'value': '2 mm'}]}",
            )
        mc = fit_rules.get("min_clearance")
        if mc is not None:
            if not isinstance(mc, list):
                raise BadInput(
                    "manifest.json: 'fit_rules.min_clearance' must be a list",
                    next="e.g. [{'pair': ['ia', 'ib'], 'value': '2 mm'}]",
                )
            for j, row in enumerate(mc):
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("pair"), list)
                    or len(row["pair"]) != 2
                    or row.get("value") is None
                ):
                    raise BadInput(
                        f"manifest.json: fit_rules.min_clearance[{j}] must be "
                        "{'pair': ['a', 'b'], 'value': '<length>'}",
                        next="e.g. {'pair': ['ia', 'ib'], 'value': '2 mm'}",
                    )
                try:
                    clr = parse_quantity(row["value"], "length", require_unit=False)
                except Exception as exc:
                    raise BadInput(
                        f"manifest.json: fit_rules.min_clearance[{j}].value "
                        f"{row['value']!r} is not a valid length ({exc})",
                        next="a length string like '2 mm', or a bare number "
                        "in metres; e.g. {'pair': ['ia', 'ib'], 'value': '2 mm'}",
                    ) from exc
                if not math.isfinite(clr) or clr < 0.0:
                    raise BadInput(
                        f"manifest.json: fit_rules.min_clearance[{j}].value "
                        f"{row['value']!r} must be a finite length ≥ 0",
                        next="e.g. {'pair': ['ia', 'ib'], 'value': '2 mm'}",
                    )
    scenario = data.get("scenario")
    if scenario is not None and not str(scenario).strip():
        raise BadInput(
            "manifest.json: 'scenario' must be a non-empty scenario id",
            next="a design's production scenario id — "
            "design_scenarios lists them (view='validate' names the "
            "governing one)",
        )
    return {
        "blocks": blocks,
        "instances": [dict(x) for x in data.get("instances") or []],
        "connects": [dict(x) for x in data.get("connects") or []],
        "rays": [dict(x) for x in rays_raw],
        "notes": data.get("notes"),
        "scenario": str(scenario).strip() if scenario is not None else None,
        "fit": fit,
        "fit_rules": fit_rules,
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


def _macro_envelope(name: str, spec: SceneSpec) -> str:
    """A macro block's render envelope: a sphere about the block origin
    containing the design's analytic AABB
    (:func:`precis.cad.tessellate.design_aabb` — kernel bounds, no mesh),
    canonical bare metres via the one m-boundary
    (:func:`precis_se.atomic.generate.ingest_envelope`). A ``box`` cannot
    hold an AABB that does not straddle the origin (centred x/y, base at
    z=0, no offset) and with se-fit-check-and-probe ruling A no check
    reads the envelope, so a sphere is enough. A spec that will not build
    or bound is a named refusal — a macro block silently going
    envelope-less is what broke clearance/drc/validate (gr514242)."""
    import numpy as np

    from precis.cad.tessellate import design_aabb
    from precis_se.atomic.generate import ingest_envelope

    try:
        lo, hi = design_aabb(spec)
    except Exception as exc:
        raise BadInput(
            f"block {name!r}: cad geometry will not build: {exc}",
            next="the render envelope comes from the design's AABB — fix "
            "the block's cad, or give it at least one finite solid node",
        ) from exc
    r = float(np.linalg.norm(np.maximum(np.abs(lo), np.abs(hi))))
    if not np.isfinite(r) or r <= 0.0:
        raise BadInput(
            f"block {name!r}: cad geometry has no finite AABB ({list(lo)}..{list(hi)})",
            next="the render envelope comes from the design's AABB — give "
            "the block at least one finite solid node",
        )
    try:
        return ingest_envelope(f"sphere:r{r}m")
    except OpError as exc:
        raise BadInput(f"block {name!r}: {exc}") from exc


def _generated_ctx(
    *,
    workspace: str,
    run_job: int | None,
    script_sha: str | None,
    lock_sha: str | None,
    sha: str | None,
    image_digest: str | None,
    params: dict[str, Any] | None,
    requested_by: str | None,
    trace_root: str | None,
    frame: int | None,
    relax: dict[str, Any] | None,
) -> dict[str, Any]:
    """The ``meta['generated']`` provenance record — which agent, which
    workspace, which script made the block (spec §Import boundary).
    ``sha`` is the manifest's own sha256 of the block file — the next
    import's changed/unchanged signal (gr514248/gr514249)."""
    ctx: dict[str, Any] = {
        "workspace": workspace,
        "run_job": run_job,
        "script_sha": script_sha,
        "lock_sha": lock_sha,
        "sha": sha,
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
    """What one workspace import did — the run echo's data half.

    ``changed``/``unchanged``/``sha_unknown`` classify the ``replaced``
    blocks by their bound ref's prior ``meta.generated.sha`` against the
    manifest's ``sha`` (and the port-name set — a facet the file sha
    cannot see); ``moved`` names the instances whose local pose/rot
    shifted numerically (gr514249). ``verified`` names the files whose
    bytes checked out against a declared ``sha``/``bonds_sha``
    (gr517643)."""

    design_slug: str
    run_ref: int | None = None
    imported: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    retired: list[dict[str, Any]] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    sha_unknown: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    trace: TraceView | None = None

    def echo(self) -> str:
        lines = [
            f"workspace import into {self.design_slug!r}: "
            f"{len(self.imported)} block(s) written"
        ]
        if self.verified:
            lines.append("sha256 verified: " + ", ".join(self.verified))
        if self.replaced:
            lines.append("replaced: " + ", ".join(self.replaced))
        if self.changed:
            lines.append("block sources changed: " + ", ".join(self.changed))
        if self.unchanged:
            lines.append("block sources unchanged: " + ", ".join(self.unchanged))
        if self.sha_unknown:
            lines.append(
                "block sources unknown (no prior sha): " + ", ".join(self.sha_unknown)
            )
        if self.moved:
            lines.append("poses moved: " + ", ".join(self.moved))
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
    (placement/dim/joint tokens, and since gr514497 the shape
    ``config`` token's unit errors too), else the first source token
    that re-raises the same error under a boundary parse (a
    ``DslError`` carries no ``arg_name`` — gr514246)."""
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
    the cause; :class:`DslError` and :class:`SceneError` (a ``BadInput``
    since gr514498) are re-wrapped only to fold the block name into the
    cause."""
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


#: Re-import pose compare tolerance (gr514249) — ``local_pose``/
#: ``local_rot`` are already SI metres/radians, the same bound the
#: writer-side manifest diff uses (:data:`precis_geom.out._POSE_REL_TOL`).
_POSE_REL_TOL = 1e-9
_POSE_ABS_TOL = 1e-12


def _vec_close(a: list[float], b: list[float]) -> bool:
    return len(a) == len(b) and all(
        math.isclose(x, y, rel_tol=_POSE_REL_TOL, abs_tol=_POSE_ABS_TOL)
        for x, y in zip(a, b)
    )


def _apply(tree: SeTree, ops: list[dict[str, Any]], *, what: str, at: str = "") -> None:
    try:
        apply_ops(tree, ops)
    except OpError as exc:
        raise BadInput(f"workspace import: {what}: {exc}{at}") from exc


def _foreign_blocks(
    store: Any, ref: Any, tree: SeTree, workspace: str, *, conn: Any = None
) -> list[str]:
    """Block names of ``tree`` this workspace did NOT make.

    A bound node is owned when the bound structure/cad ref's
    ``meta.generated.workspace`` stamp matches — the stamp is
    authoritative over the name: a hand-made block squatting on an
    imported block's name does not inherit the record's ownership. An
    unbound node (an instance) is owned when the design's
    ``meta.workspaces[workspace]`` record lists the name — it carries no
    binding to stamp-check. ``conn`` re-reads the design row under the
    caller's lock so the record is the committed-latest, not a stale
    pre-lock snapshot."""
    live = (
        store.get_ref(kind="se", id=int(ref.id), conn=conn) if conn is not None else ref
    )
    record = (((live or ref).meta or {}).get("workspaces") or {}).get(workspace) or {}
    owned_names = set(record.get("blocks") or []) | set(record.get("instances") or [])
    foreign = []
    for name, node in tree.blocks.items():
        if node.bound_kind in ("structure", "cad") and node.bound:
            bref = store.get_ref(
                kind=node.bound_kind,
                id=node.bound,
                include_deleted=True,
                conn=conn,
            )
            if (
                bref is not None
                and ((bref.meta or {}).get("generated") or {}).get("workspace")
                == workspace
            ):
                continue
        elif name in owned_names:
            continue
        foreign.append(name)
    return sorted(foreign)


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
    _vet_workspace_slug(workspace)
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

    # gr514245: a declared scenario is vetted here (before any write) —
    # the same unknown-id refusal ``put``'s scenario key gives.
    scenario_id = manifest["scenario"]
    if scenario_id is not None and (
        design_scenarios.get_scenario(store, scenario_id) is None
    ):
        known = (
            ", ".join(s.scenario_id for s in design_scenarios.list_scenarios(store))
            or "(none)"
        )
        raise BadInput(
            f"manifest.json: unknown scenario {scenario_id!r} — known: {known}",
            next="declare the scenario first, or drop the key",
        )

    # Parse every geometry file BEFORE the write transaction — caps and
    # refusals happen here so nothing half-imports. A declared ``sha`` /
    # ``bonds_sha`` is verified against the file's bytes here too
    # (gr517643): the inline ``{relpath: text}`` door retypes every byte,
    # so the manifest's own hash is the only thing that can catch a
    # corrupted upload — a mismatch refuses the whole import. ``verified``
    # collects the files that checked out; the run echo reports them.
    parsed: dict[str, tuple[Any, Any]] = {}
    verified: list[str] = []

    def _check_sha(rel: str, declared: str, what: str) -> None:
        # Hash the file's RAW bytes — ``read_text``'s universal-newline
        # read would make a CRLF upload false-refuse (gr517643).
        got = hashlib.sha256((out_path / rel).read_bytes()).hexdigest()
        if got != declared:
            raise BadInput(
                f"block {what}: {rel!r} sha256 mismatch — manifest declares "
                f"{declared}, the uploaded file hashes {got}",
                next="re-upload the out/ tree verbatim — the bytes were "
                "rewritten between the run and the import",
            )
        verified.append(rel)

    for entry in manifest["blocks"]:
        try:
            body = _read_file(
                out_path,
                entry.file,
                cap=MAX_WS_FILE_BYTES,
                what=f"blocks[{entry.name!r}] file",
            )
            if entry.sha is not None:
                _check_sha(entry.file, entry.sha, f"{entry.name!r}")
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
                sidecar = (out_path / bonds_rel).is_file()
                if entry.bonds is not None and not sidecar:
                    raise BadInput(
                        f"block {entry.name!r}: declared bonds file "
                        f"{bonds_rel!r} is not in the out/ tree",
                        next="upload the .bonds.json sidecar too, or drop "
                        "the 'bonds' key",
                    )
                if entry.bonds_sha is not None and not sidecar:
                    # A declared digest with nothing on disk must refuse —
                    # passing it silently is a verify-by-omission hole
                    # (gr517643).
                    raise BadInput(
                        f"block {entry.name!r}: declared 'bonds_sha' but no "
                        f"sidecar {bonds_rel!r} in the out/ tree — nothing "
                        "to verify against",
                        next="upload the .bonds.json sidecar too, or drop 'bonds_sha'",
                    )
                if sidecar:
                    bonds_body = _read_file(
                        out_path,
                        bonds_rel,
                        cap=MAX_WS_FILE_BYTES,
                        what=f"{entry.name!r} bonds",
                    )
                    if entry.bonds_sha is not None:
                        _check_sha(bonds_rel, entry.bonds_sha, f"{entry.name!r}")
                    scene.bonds = bonds_from_json(bonds_body)
                    if not scene.bonds:
                        raise BadInput(
                            f"block {entry.name!r}: bonds file {bonds_rel!r} "
                            "parsed to an empty bond list — the writer only "
                            "writes the sidecar when bonds exist",
                            next="fix the file or drop the sidecar (and any "
                            "'bonds'/'bonds_sha' keys)",
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
    result.verified.extend(verified)
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

        # The foreign-block gate (the gripe's safety rule): an import on
        # 'design' replaces the workspace's whole record, so a design
        # holding blocks this workspace did not make is refused outright
        # — it could never replace or retire them anyway, and a silent
        # co-exist hides exactly the hexfold-dogfood-r4 mix-up the gate
        # exists for. It runs here — under the tree advisory lock, inside
        # the write transaction — so the check serializes with every
        # other writer of this design and guards every caller of this
        # function, not only the ``import_workspace`` op door. 'fork' is
        # the sanctioned escape: the copy may carry anything.
        if target == "design":
            foreign = _foreign_blocks(store, ref, tree, workspace, conn=conn)
            if foreign:
                raise BadInput(
                    f"workspace import: design {design!r} has blocks "
                    f"workspace {workspace!r} did not make: "
                    f"{', '.join(foreign)}",
                    next=[
                        "put(kind='se', id='<fresh-slug>', ...) a fresh design",
                        "'target': 'fork' imports into a derived copy instead",
                    ],
                )

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
            # Measures ride the block that owns them — the same vacancy
            # rule _op_remove_block applies (this retire pops blocks
            # directly, so the cascade is spelled out here).
            tree.measures = [m for m in tree.measures if m.block not in subtree]
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
        # Manifest-declared notes collect here and apply once, after
        # every block — a ``re`` may name a note a later block declares.
        note_decls: list[tuple[BlockEntry, dict[str, Any]]] = []
        for entry in manifest["blocks"]:
            kind, payload = parsed[entry.name]
            bsite = _site(entry.frame)
            # gr514205 — the run's Geom findings ride the block row;
            # echo them so an import, not just manifest.json, shows a
            # mirrored hand.
            result.findings.extend(f"block {entry.name!r}: {f}" for f in entry.findings)
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
                sha=entry.sha,
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
                # gr514249 — a same-named block is a replace either way;
                # what the echo owes the reader is whether anything
                # actually moved: the prior bound ref's
                # ``meta.generated.sha`` vs this manifest's ``sha`` (the
                # bound_meta snapshot predates this loop's writes), plus
                # the port-name set the file sha cannot see.
                prior_meta: dict[str, Any] | None = None
                if existing.bound_kind and existing.bound:
                    prior_meta = bound_meta.get((existing.bound_kind, existing.bound))
                prior_sha = ((prior_meta or {}).get("generated") or {}).get("sha")
                ports_same = set(existing.ports) == {
                    str(p["name"]) for p in entry.ports
                }
                if prior_sha is not None and entry.sha is not None:
                    (
                        result.unchanged
                        if prior_sha == entry.sha and ports_same
                        else result.changed
                    ).append(entry.name)
                elif ports_same:
                    result.sha_unknown.append(entry.name)
                else:
                    result.changed.append(entry.name)
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
            else:
                # gr514242: a macro block's envelope is derived from the
                # bound cad geometry's AABB — without it clearance/drc/
                # validate could not answer a fit or size question.
                try:
                    envelope = _macro_envelope(entry.name, payload)
                except BadInput as exc:
                    _with_site(exc, bsite)
                    raise
                ops.append(
                    {
                        "op": "set_envelope",
                        "block": entry.name,
                        "envelope": envelope,
                        "origin": "generated",
                    }
                )
            # gr514245 — the manifest's declared facets replay as ops
            # after the scale defaults, so an explicit ``mode`` wins over
            # the atomic one and ``loads`` gets clear+set (replace, not
            # merge) semantics on a re-import.
            if entry.mode is not None:
                ops.append({"op": "set_mode", "block": entry.name, "mode": entry.mode})
            if entry.loads is not None:
                ops.append({"op": "set_load", "block": entry.name, "clear": True})
                ops.append({"op": "set_load", "block": entry.name, **entry.loads})
            for m in entry.measures:
                mname = str(m["name"])
                existing_measure = any(
                    x.block == entry.name and x.name == mname for x in tree.measures
                )
                if existing_measure:
                    ops.append(
                        {
                            "op": "remove_measure",
                            "block": entry.name,
                            "name": mname,
                        }
                    )
                ops.append(
                    {"op": "add_measure", "block": entry.name, **m, "name": mname}
                )
            note_decls.extend((entry, n) for n in entry.notes)
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

        # ── declared facets the manifest dropped (gr514245): the
        # manifest is the workspace's whole state, so a mode/loads/
        # measure this run no longer declares is cleared on the block it
        # was declared on — when that block still exists ──
        new_loads = {e.name for e in manifest["blocks"] if e.loads is not None}
        new_modes = {e.name for e in manifest["blocks"] if e.mode is not None}
        new_measures = {
            f"{e.name}.{m['name']}" for e in manifest["blocks"] for m in e.measures
        }
        drop_ops: list[dict[str, Any]] = []
        for blk in sorted(set(prev.get("loads") or []) - new_loads):
            if blk in tree.blocks:
                drop_ops.append({"op": "set_load", "block": blk, "clear": True})
        # Atomic blocks keep mode='atomic' — the import's own default,
        # not a manifest declaration (the block loop re-stamps it every
        # run, so clearing here would only strip the default back off).
        atomic_names = {e.name for e in manifest["blocks"] if e.scale == "atomic"}
        for blk in sorted(set(prev.get("modes") or []) - new_modes - atomic_names):
            prev_node = tree.blocks.get(blk)
            if (
                prev_node is not None
                and prev_node.template is None
                and prev_node.mode is not None
            ):
                drop_ops.append({"op": "set_mode", "block": blk, "mode": None})
        for key in sorted(set(prev.get("measures") or []) - new_measures):
            blk, _sep, mname = key.partition(".")
            if blk in tree.blocks and any(
                m.block == blk and m.name == mname for m in tree.measures
            ):
                drop_ops.append({"op": "remove_measure", "block": blk, "name": mname})
        if drop_ops:
            _apply(tree, drop_ops, what="dropped manifest facets")

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
            was_pose = (
                (list(existing_inst.local_pose), list(existing_inst.local_rot))
                if existing_inst is not None
                else None
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
            # gr514249 — a re-placed instance reports its pose/rot move
            # numerically (local_* are SI), where a byte-compare of the
            # manifest strings would flag float noise as a move.
            if was_pose is not None:
                new_inst = tree.blocks[name]
                if not (
                    _vec_close(was_pose[0], new_inst.local_pose)
                    and _vec_close(was_pose[1], new_inst.local_rot)
                ):
                    result.moved.append(name)
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

        # ── declared notes (gr514245): the design's ledger is
        # append-only, so notes replay additive — an identical one
        # already on the ledger is skipped (a re-import replays its
        # manifest verbatim), a same-named different one refuses (the
        # run cannot rewrite the record — the ledger's own rule) ──
        for entry, n in note_decls:
            nname = str(n["name"]).strip()
            raw_about = n.get("about")
            about = (
                [str(raw_about)]
                if isinstance(raw_about, str)
                else ([str(a) for a in raw_about] if raw_about is not None else [])
            ) or [entry.name]
            op_n: dict[str, Any] = {
                "op": "add_note",
                "name": nname,
                "kind": str(n["kind"]).strip(),
                "text": n["text"],
                "about": about,
                "origin": str(n.get("origin") or "generated").strip(),
            }
            if n.get("re") is not None:
                op_n["re"] = n["re"]
            ex = next((x for x in tree.notes if x.name == nname), None)
            if ex is not None:
                if (
                    ex.kind == op_n["kind"]
                    and ex.body == str(n["text"]).strip()
                    and ex.re
                    == (str(n["re"]).strip() if n.get("re") is not None else None)
                    and sorted(ex.about) == sorted(about)
                    and ex.origin == op_n["origin"]
                ):
                    continue
                raise BadInput(
                    f"manifest blocks[{entry.name!r}] notes: {nname!r} is "
                    f"already on the design's ledger with different "
                    f"text{_site(entry.frame)}",
                    next="notes are append-only — add a NEW note to amend, "
                    "or rename this one",
                )
            _apply(tree, [op_n], what=f"note {nname!r}", at=_site(entry.frame))

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

        # gr514245: a declared scenario governs the design (vet ran
        # pre-write, above); absent → the existing link stands.
        if scenario_id is not None:
            design_scenarios.set_design_scenario(
                store,
                int(ref.id),
                scenario_id,
                set_by="workspace_import",
                conn=conn,
            )
        persist.save_tree(
            store,
            ref_id=ref.id,
            tree=tree,
            card_text=f"{design}: workspace {workspace} import "
            f"({len(result.imported)} blocks)",
            conn=conn,
        )
        # se-fit-check-and-probe Decisions 8/9: the manifest's own ``fit``
        # payload is untrusted input — the importer recomputes on the
        # just-applied tree and stores the server's report at
        # ``workspaces[<ws>].fit`` (JSON, no migration). Specs come from
        # ``parsed``: the cad_saves above are still uncommitted in this
        # transaction, so ``store.cad_load`` could not see them. A failed
        # recompute is a finding, never a skipped silent store.
        fit_record: dict[str, Any] | None = None
        try:
            from precis_se import fit as se_fit

            specs_by_slug = {
                f"{design}-{e.name}": parsed[e.name][1]
                for e in manifest["blocks"]
                if parsed[e.name][0] == "macro"
            }

            def _fit_spec_of(owner: Any) -> tuple[Any, dict[str, Any] | None]:
                spec = specs_by_slug.get(owner.bound or "")
                if spec is None:
                    return None, None
                return spec, dict(spec.meta or {})

            fit_sites = se_fit.entry_sites(
                manifest, trace.frames if trace is not None else None
            )
            fit_run = se_fit.compute(
                store,
                tree,
                spec_of=_fit_spec_of,
                site_of=lambda f: (
                    traceview.site(_tv, int(f))
                    if (_tv is not None and f is not None)
                    else None
                ),
                sites=fit_sites,
                min_clearance=se_fit.min_clearance_of(manifest["fit_rules"]),
            )
            prev_fit = prev.get("fit")
            fit_record = se_fit.stored_record(
                fit_run.report.to_json(),
                workspace=workspace,
                run_job=run_job,
                fit_rules=manifest["fit_rules"],
                prev=prev_fit if isinstance(prev_fit, dict) else None,
                sites=fit_sites,
            )
        except Exception as exc:
            result.findings.append(f"fit: recompute failed: {exc}")

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
                                "measures": sorted(new_measures),
                                "loads": sorted(new_loads),
                                "modes": sorted(new_modes),
                                "templates": sorted(
                                    e.name for e in manifest["blocks"] if e.template
                                ),
                                "run_job": run_job,
                                "script_sha": script_sha,
                                **({"fit": fit_record} if fit_record else {}),
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


# ── The ``import_workspace`` se op ─────────────────────────────────────
#
# ``edit(kind='se', id=<design>, ops=[{'op': 'import_workspace', ...}])`` —
# the agent-callable door into :func:`import_workspace_run` (the
# precis-workspace-help "one-command import"). The run's ``out/`` tree and
# the workspace's source files ride inline as ``{relpath: text}`` dicts:
# nothing on prod needs a host path. ``SeHandler._edit_impl`` intercepts
# it before ``tree_mutation`` (sole op, like the design-history ops);
# ``_put_impl`` refuses it — put builds a fresh tree and the op needs an
# existing design ref.

#: ``additionalProperties: false``, same posture as the manifest vet —
#: a typo'd key is the caller's failure, named.
_IMPORT_OP_KEYS = frozenset(
    {
        "op",
        "workspace",
        "run",
        "files",
        "dry_run",
        "target",
        "script_sha",
        "lock_sha",
        "params",
        "requested_by",
        "run_job",
    }
)

#: The workspace folder predicates — the rows
#: :mod:`precis.handlers._python_ws` reads for the ``ws-<slug>`` python
#: roots (``_FOLDER_SQL``/``_FILES_SQL``). precis cannot import precis_se,
#: so the reader keeps its own copy; keep these aligned with it.
_WS_FOLDER_SQL = """
SELECT r.ref_id, r.meta FROM refs r
WHERE r.kind = 'folder' AND r.retired_at IS NULL
  AND (r.meta->'workspace'->>'slug' = %s
       OR (SELECT ri.id_value FROM ref_identifiers ri
            WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key'
            LIMIT 1) = %s)
"""
_WS_FILES_SQL = """
SELECT ref_id,
       COALESCE(meta->>'workspace_path', meta->>'harvest_orig_path')
  FROM refs
 WHERE parent_id = %s AND kind = 'plaintext' AND retired_at IS NULL
"""


class _DryRunDone(Exception):
    """Sentinel unwinding the dry-run ``store.atomic()`` scope — the
    result is already captured; raising this IS the rollback."""


def _vet_relpath_map(raw: Any, *, what: str, total_cap: int) -> dict[str, str]:
    """A ``{relpath: text}`` tree — the run's ``out/`` or the workspace
    file set — vetted before any write: clean relative posix paths only
    (no absolute, no ``..``/``.``/empty segments, no redundant
    separators), text values, the harvest's file-count, per-file and
    total byte caps, and no path that is a proper ancestor of another
    (``manifest.json`` + ``manifest.json/x`` would explode the staging
    write as NotADirectoryError — refuse it as BadInput)."""
    if not isinstance(raw, dict):
        raise BadInput(
            f"import_workspace: {what} must be an object mapping relpath → file text",
            next="'run': {'manifest.json': '{...}', 'trace.json': '{...}', "
            "'blocks/tube.extxyz': '...'}",
        )
    if len(raw) > MAX_WS_FILES:
        raise BadInput(
            f"import_workspace: {what} holds {len(raw)} files, over the "
            f"{MAX_WS_FILES} file-count cap"
        )
    out: dict[str, str] = {}
    total = 0
    for rel, text in raw.items():
        rel_s = str(rel)
        p = PurePosixPath(rel_s)
        if (
            p.is_absolute()
            or any(s in ("", ".", "..") for s in rel_s.split("/"))
            or rel_s != p.as_posix()
        ):
            raise BadInput(
                f"import_workspace: {what} path must be a clean relative "
                f"posix path, got {rel_s!r}",
                next="keys like 'manifest.json' or 'blocks/tube.extxyz' — "
                "no leading '/', no '..', no '.', no '//'",
            )
        if not isinstance(text, str):
            raise BadInput(
                f"import_workspace: {what}[{rel_s!r}] must be text, "
                f"got {type(text).__name__}"
            )
        size = len(text.encode("utf-8"))
        if size > MAX_WS_FILE_BYTES:
            raise BadInput(
                f"import_workspace: {what} {rel_s!r} is {size} B, over the "
                f"{MAX_WS_FILE_BYTES} B file cap"
            )
        total += size
        if total > total_cap:
            raise BadInput(
                f"import_workspace: {what} totals over the {total_cap} B cap"
            )
        out[p.as_posix()] = text
    for rel in out:
        for parent in PurePosixPath(rel).parents:
            parent_s = parent.as_posix()
            if parent_s == ".":
                break
            if parent_s in out:
                raise BadInput(
                    f"import_workspace: {what} path {rel!r} nests under "
                    f"{parent_s!r}, which is also a file — a key can't be "
                    "both file and directory",
                    next="drop one of the two keys",
                )
    return out


def _file_slug(rel: str) -> str:
    """``main.py`` → ``main-py``; ``lib/util.py`` → ``lib--util-py`` — the
    file's plaintext-slug tail. The extension stays encoded (unlike
    ``file_slug_from_path``, which strips it): ``main`` and ``main.py``
    must not collide."""
    return "--".join(
        seg for p in PurePosixPath(rel).parts if (seg := _normalize_segment(p))
    )


def _sync_workspace_files(
    store: Any,
    conn: Any,
    *,
    workspace: str,
    design_slug: str,
    design_ref_id: int,
    files: dict[str, str],
    requested_by: str | None,
) -> str:
    """Find-or-mint the workspace's ``folder`` ref (the ``_python_ws``
    predicate), sync its ``plaintext`` children to ``files`` — a file the
    dict drops retires; bodies rewrite by chunks DELETE+INSERT, never
    UPDATE — and keep the folder's ``part-of`` edge to the design.
    Returns the echo line."""
    folder_id: int | None = None
    folder_meta: dict[str, Any] = {}
    rows = conn.execute(_WS_FOLDER_SQL, (workspace, f"ws-{workspace}")).fetchall()
    for ref_id, meta in rows:
        ws = (meta or {}).get("workspace")
        if isinstance(ws, dict) and ws.get("slug", workspace) == workspace:
            folder_id, folder_meta = int(ref_id), dict(ws)
            break
    if folder_id is None:
        folder = store.insert_ref(
            kind="folder",
            slug=None,
            title=f"workspace {workspace}",
            meta={
                "workspace": {
                    "slug": workspace,
                    "design": design_slug,
                    "created_by": requested_by or "import_workspace",
                }
            },
            conn=conn,
        )
        folder_id = int(folder.id)
    else:
        other = folder_meta.get("design")
        if other is not None and str(other).split(":")[-1] != design_slug:
            raise BadInput(
                f"import_workspace: workspace {workspace!r}'s folder already "
                f"belongs to design {other!r} — two designs never share a "
                "folder",
                next=f"pick another 'workspace' slug, or import into "
                f"{str(other).split(':')[-1]!r}",
            )
    store.add_link(
        src_ref_id=folder_id,
        dst_ref_id=design_ref_id,
        relation="part-of",
        set_by="workspace_import",
        conn=conn,
    )
    existing: dict[str, int] = {}
    for ref_id, rel in conn.execute(_WS_FILES_SQL, (folder_id,)).fetchall():
        if rel:
            existing[str(rel)] = int(ref_id)
    created: list[str] = []
    updated: list[str] = []
    retired: list[str] = []
    unchanged = 0
    for rel in sorted(files):
        child_id = existing.get(rel)
        state = "live"
        if child_id is None:
            state = "new"
            slug = f"ws-{workspace}--{_file_slug(rel)}"
            prior = store.get_ref(
                kind="plaintext", id=slug, include_deleted=True, conn=conn
            )
            if prior is not None:
                # A child this folder already owns that an earlier import
                # retired — restore it and rewrite its chunks rather than
                # refuse on the slug, so files {a,b} → {a} → {a,b}
                # converges back. Only when its recorded path IS this
                # file's: ``_file_slug`` folds case and punctuation, so
                # 'Main.py' collides with 'main.py' — a same-slug ref
                # carrying a different ``workspace_path`` is not this
                # file and stays a refusal.
                if (
                    prior.retired_at is None
                    or int(prior.parent_id or -1) != folder_id
                    or (prior.meta or {}).get("workspace_path") != rel
                ):
                    raise BadInput(
                        f"import_workspace: file slug {slug!r} is taken outside "
                        "this workspace",
                        next="rename the file in 'files' (slugs fold case "
                        "and punctuation — 'Main.py' collides with "
                        "'main.py'), or pick another 'workspace' slug",
                    )
                store.restore_ref(int(prior.id), conn=conn)
                child_id = int(prior.id)
                state = "restored"
            else:
                child = store.insert_ref(
                    kind="plaintext",
                    slug=slug,
                    title=rel,
                    parent_id=folder_id,
                    meta={"workspace_path": rel, "harvest_orig_path": rel},
                    conn=conn,
                )
                child_id = int(child.id)
        prev = (
            "".join(
                c.text for c in store.chunks.list_chunks_for_ref(child_id, conn=conn)
            )
            if state == "live"
            else None
        )
        if prev != files[rel]:
            store.chunks.insert_chunks(
                child_id,
                [ChunkInsert(ord=0, text=files[rel])],
                replace=True,
                conn=conn,
            )
        if state != "live":
            created.append(rel)
        elif prev != files[rel]:
            updated.append(rel)
        else:
            unchanged += 1
    for rel, ref_id in sorted(existing.items()):
        if rel not in files:
            store.retire_ref(ref_id, conn=conn)
            retired.append(rel)
    bits = []
    if created:
        bits.append("+" + ", +".join(created))
    if updated:
        bits.append("~" + ", ~".join(updated))
    if retired:
        bits.append("-" + ", -".join(retired))
    if unchanged:
        bits.append(f"{unchanged} unchanged")
    return "files: " + (", ".join(bits) if bits else "folder in sync")


def import_workspace_op(store: Any, *, design: str, op: dict[str, Any]) -> str:
    """Run the ``import_workspace`` se op; return the response body.

    Keys: ``workspace`` (req — the workspace slug), ``run`` (req —
    ``{relpath: text}``, the run's whole ``out/`` tree verbatim:
    ``manifest.json``, ``trace.json``, ``blocks/*``), ``files`` (opt —
    ``{relpath: text}`` synced into the workspace folder, linked
    ``part-of`` the design; absent = the folder untouched, ``{}`` =
    cleared), ``dry_run``, ``target`` ('design'|'fork'), and the
    provenance keys ``script_sha``/``lock_sha``/``params``/
    ``requested_by``/``run_job`` (``None`` stores the trace on the
    design ref — the containerless path)."""
    _unknown_keys(op, _IMPORT_OP_KEYS, "import_workspace op")
    workspace = str(op.get("workspace") or "").strip()
    if not workspace:
        raise BadInput(
            "import_workspace needs 'workspace' (the workspace slug)",
            next="edit(kind='se', id=..., ops=[{'op':'import_workspace',"
            "'workspace':'hexfold','run':{'manifest.json':'{...}', ...}}])",
        )
    _vet_workspace_slug(workspace)
    run = _vet_relpath_map(op.get("run"), what="'run'", total_cap=MAX_WS_OUT_BYTES)
    if "manifest.json" not in run:
        raise BadInput(
            "import_workspace: 'run' holds no manifest.json",
            next="the out/ tree verbatim — out.run writes manifest.json "
            "beside trace.json and blocks/",
        )
    files_raw = op.get("files")
    files = (
        None
        if files_raw is None
        else _vet_relpath_map(files_raw, what="'files'", total_cap=MAX_WS_OUT_BYTES)
    )
    # A workspace file must be a script that parses — gr517643: a
    # retyped/corrupted main.py used to sync happily and still report
    # "N block(s) written". SyntaxError carries lineno/offset, so the
    # refusal names the file:line.
    for rel, text in (files or {}).items():
        if not rel.endswith(".py"):
            continue
        try:
            compile(text, rel, "exec")
        except (SyntaxError, ValueError, TypeError) as exc:
            # ValueError/TypeError too — null bytes, a lone \r, a nested
            # literal: a corrupt upload must refuse as BadInput, not
            # escape as an unhandled exception (gr517643).
            where = (
                f"{rel}:{exc.lineno}"
                if isinstance(exc, SyntaxError) and exc.lineno
                else rel
            )
            detail = exc.msg if isinstance(exc, SyntaxError) else str(exc)
            raise BadInput(
                f"import_workspace: 'files' {where} does not compile — {detail}",
                next="fix the script and re-upload — the folder syncs verbatim source",
            ) from exc
    dry_run = bool(op.get("dry_run"))
    target = str(op.get("target") or "design").strip()
    if target not in ("design", "fork"):
        raise BadInput(
            f"import_workspace 'target' must be 'design' or 'fork', "
            f"got {op.get('target')!r}",
            options=["design", "fork"],
        )
    run_job = op.get("run_job")
    if run_job is not None:
        try:
            run_job = int(run_job)
        except (TypeError, ValueError) as exc:
            raise BadInput(
                f"import_workspace 'run_job' must be a job ref id, "
                f"got {op.get('run_job')!r}"
            ) from exc
        if store.get_ref(kind="job", id=run_job) is None:
            raise BadInput(
                f"import_workspace: run_job {run_job} is not a job ref",
                next="omit 'run_job' — the trace then stores on the design ref itself",
            )
    params = op.get("params")
    if params is not None and not isinstance(params, dict):
        raise BadInput("import_workspace 'params' must be an object")
    script_sha = str(op["script_sha"]).strip() if op.get("script_sha") else None
    lock_sha = str(op["lock_sha"]).strip() if op.get("lock_sha") else None
    requested_by = str(op["requested_by"]).strip() if op.get("requested_by") else None
    ref = store.get_ref(kind="se", id=design)
    if ref is None:
        raise NotFound(
            f"se design {design!r} not found",
            next=f"put(kind='se', id={design!r}, title=...) first — "
            "import_workspace imports into an existing design",
        )
    # The foreign-block gate runs inside ``import_workspace_run``, under
    # the tree advisory lock its ``tree_mutation`` takes inside this
    # ``atomic`` — the check and the writes share one serialized span.
    result: ImportResult | None = None
    files_echo: str | None = None
    try:
        with store.atomic() as conn:
            if files is not None:
                files_echo = _sync_workspace_files(
                    store,
                    conn,
                    workspace=workspace,
                    design_slug=design,
                    design_ref_id=int(ref.id),
                    files=files,
                    requested_by=requested_by,
                )
            with TemporaryDirectory() as td:
                out_dir = Path(td) / "out"
                for rel, text in run.items():
                    dest = out_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(text, encoding="utf-8")
                result = import_workspace_run(
                    store,
                    workspace=workspace,
                    design=design,
                    out_dir=out_dir,
                    run_job=run_job,
                    script_sha=script_sha,
                    lock_sha=lock_sha,
                    params=params,
                    requested_by=requested_by,
                    target=target,
                )
            if dry_run:
                raise _DryRunDone
    except _DryRunDone:
        pass
    assert result is not None
    head = f"# se design '{result.design_slug}' — workspace {workspace!r} import" + (
        " — DRY RUN, nothing written" if dry_run else ""
    )
    parts = [head]
    if files_echo is not None:
        parts.append(files_echo)
    parts.append(result.echo())
    parts.append(
        f"Next: get(kind='se', id={result.design_slug!r}, view='tree')"
        + (
            f" · the code: get(kind='python', id='ws-{workspace}::<qualname>')"
            if files is not None
            else ""
        )
    )
    return "\n\n".join(parts)

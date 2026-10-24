"""The construction-trace wire format — ``out/trace.json`` schema 1.

A workspace run's **construction trace** is a tree of **frames**: one frame
per call that placed or changed geometry (structure-workspaces spec,
§Construction traces). ``precis_geom.trace`` *writes* it inside the sandbox;
``precis_se.workspace_import`` *reads* it on the trusted side. Both import
this module, so the field names, the key recipe and the collapse encoding
live exactly once — stdlib only, since the same file ships inside the
``precis-geom`` wheel and runs under ``--network none``.

Top level::

    {"schema": 1,
     "run": {"workspace": <slug|null>, "job": <int|null>,
             "script_sha": <str|null>, "lock_sha": <str|null>,
             "started_utc": <iso|null>, "finished_utc": <iso|null>,
             "collapsed": <int>, "truncated": <bool>},
     "frames": [<frame>, ...]}

Frame fields (all present unless noted)::

    id       small int interned per run; entity ``maker`` stamps cite it.
    key      sha8(parent.key, qual, line, occ) — the stable citation.
             Depends on nothing run-local, so an unchanged script yields
             identical keys on every run.
    parent   parent frame's key; ``None`` on the root frame.
    qual     the call's dotted name, shortest stable form: user code is
             ``<file stem>.<co_qualname>`` (``main.bundle``); core is the
             module-relative path with its top package dropped
             (``sheetnet.junction``, ``cad.graph.Design.prim``,
             ``out.block``).
    file     the *call site* file — ``/work``-relative for user code, the
             caller's module name (``cad.graph``) for library internals.
    line     the call-site line that invoked this call — for the
             outermost user frame (its caller is the driver outside
             ``/work``) the function's own ``def`` line instead, so the
             root of the rendered stack always cites the script.
    occ      occurrence counter of this ``(qual, line)`` under the same
             parent, 0-based: the third ``junction`` at one call site is
             ``#2``.
    kind     ``root`` | ``user`` | ``core`` | ``module`` | ``service`` |
             ``op``.
    args     bounded digest: scalars and short strings verbatim, floats
             ``%.6g``, objects ``<Class sha8>``.
    made     ``{entity_type: int}`` — what this frame alone placed.
    touched  ``{entity_type: int | [[lo, hi], ...]}`` — what it changed.
    error    optional — the exception type name (``ValueError``) when
             the call raised; only the innermost traced frame the
             exception left carries it. Absent on success.

Collapsed sibling form: past ``COLLAPSE_SIBLINGS`` (64) same-site
*childless* siblings, occurrences 64..N fold into one frame carrying
``occ`` = 64, ``occ_end`` = N, ``collapsed`` = count, ``id``/``id_end``
spanning the folded run, and ``made``/``touched`` = their sums.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA_VERSION = 1

#: Frame ``kind`` values. ``root`` is the synthetic run node every
#: top-level frame parents to; the rest come from the spec's table.
FRAME_KINDS = frozenset({"root", "user", "core", "module", "service", "op"})

#: Past this many same-site childless siblings, occurrences fold into one
#: frame with an ``occ`` range (spec §Construction traces, Risks).
COLLAPSE_SIBLINGS = 64

#: Hard cap on emitted frames; past it the writer sets ``run.truncated``
#: (the importer still imports and emits ``trace.truncated``).
MAX_FRAMES = 20_000

#: Entity types ``made``/``touched`` count.
ENTITY_TYPES = frozenset({"atoms", "bonds", "instances", "faces", "blocks", "ports"})


def sha8(text: str) -> str:
    """8 bytes (16 hex chars) of sha256 — collision-free well past the
    ``MAX_FRAMES`` scale while staying short enough to cite."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def frame_key(parent_key: str, qual: str, line: int, occ: int) -> str:
    """``sha8(parent.key, qual, line, occ)`` — the spec's stable key."""
    return sha8(f"{parent_key}\x00{qual}\x00{line}\x00{occ}")


def dumps(trace: dict[str, Any]) -> str:
    """Canonical serialization: sorted keys, no incidental whitespace, so
    identical trees hash identically."""
    return json.dumps(trace, sort_keys=True, separators=(",", ":"))


def loads(text: str) -> dict[str, Any]:
    """Parse + shape-check a ``trace.json``. Raises ``ValueError`` naming
    the offending key — the importer maps that to ``next=``."""
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("trace.json: top level must be an object")
    if doc.get("schema") != SCHEMA_VERSION:
        raise ValueError(
            f"trace.json: schema {doc.get('schema')!r} != {SCHEMA_VERSION}"
        )
    frames = doc.get("frames")
    if not isinstance(frames, list):
        raise ValueError("trace.json: 'frames' must be a list")
    seen: set[int] = set()
    keys: set[str] = set()
    for i, fr in enumerate(frames):
        if not isinstance(fr, dict):
            raise ValueError(f"trace.json: frames[{i}] is not an object")
        for req in ("id", "key", "qual", "file", "line", "occ", "kind"):
            if req not in fr:
                raise ValueError(f"trace.json: frames[{i}] lacks '{req}'")
        if fr["kind"] not in FRAME_KINDS:
            raise ValueError(
                f"trace.json: frames[{i}].kind {fr['kind']!r} not in "
                f"{sorted(FRAME_KINDS)}"
            )
        if fr["id"] in seen and not fr.get("collapsed"):
            raise ValueError(f"trace.json: frames[{i}].id {fr['id']} reused")
        seen.add(fr["id"])
        keys.add(fr["key"])
    for i, fr in enumerate(frames):
        parent = fr.get("parent")
        if parent is not None and parent not in keys:
            raise ValueError(f"trace.json: frames[{i}] parent {parent!r} has no frame")
    return doc


__all__ = [
    "COLLAPSE_SIBLINGS",
    "ENTITY_TYPES",
    "FRAME_KINDS",
    "MAX_FRAMES",
    "SCHEMA_VERSION",
    "dumps",
    "frame_key",
    "loads",
    "sha8",
]

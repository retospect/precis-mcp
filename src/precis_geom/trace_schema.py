"""The construction-trace wire format — ``out/trace.json`` schema 3
(reads schema 1, 2 and 3).

A workspace run's **construction trace** is a tree of **frames**: one frame
per call that placed or changed geometry (structure-workspaces spec,
§Construction traces). ``precis_geom.trace`` *writes* it inside the sandbox;
``precis_se.workspace_import`` *reads* it on the trusted side. Both import
this module, so the field names, the key recipe and the collapse encoding
live exactly once — stdlib only, since the same file ships inside the
``precis-geom`` wheel and runs under ``--network none``.

Top level::

    {"schema": 3,
     "run": {"workspace": <slug|null>, "job": <int|null>,
             "script_sha": <str|null>, "lock_sha": <str|null>,
             "started_utc": <iso|null>, "finished_utc": <iso|null>,
             "collapsed": <int>, "truncated": <bool>,
             "key_collisions": <int>},
     "frames": [<frame>, ...]}

Frame fields (all present unless noted)::

    id       small int interned per run; entity ``maker`` stamps cite it.
    key      sha8(parent.key, qual, occ) — the stable citation
             (schema 2; schema 1 hashed the call-site ``line`` too).
             Depends on nothing run-local or positional, so an unchanged
             script — and a script edited above the call — yields
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
    line_in_def  schema 2 — ``line`` minus the enclosing def's
             ``co_firstlineno``; 0 on the root and on calls whose site
             sits outside ``/work`` (library internals, the driver).
    src      schema 2 — the stripped call-site source line, capped at 120
             chars; ``null`` when the call site is outside ``/work`` or
             the source is unreadable. Re-anchoring, not identity.
    occ      occurrence counter of this ``qual`` under the same parent,
             0-based across all call sites: the third ``junction`` a
             parent makes is ``#2`` wherever it was called from.
    kind     ``root`` | ``user`` | ``core`` | ``module`` | ``service`` |
             ``op``.
    args     bounded digest: scalars and short strings verbatim, floats
             ``%.6g``, objects ``<Class n sha8>``. Capture visits at most
             256 values, depth 16 and 4096 payload bytes, and hashes at most
             4096 canonical bytes. Already materialised content hashes can
             replace traversal. ``capped`` before the closing ``>`` marks
             a partial sample (not whole-content equality); large strided
             arrays fall back to shape. No unbounded custom repr is called.
    dur_ms   schema 3 — inclusive elapsed milliseconds, using a monotonic
             clock. Sampled user frames cover only the observed interval;
             live frames report elapsed time at dump, completed ones freeze.
    ret      schema 3 — optional bounded return digest, using the same
             recipe as args so consumers can join to producers. Absent for
             failed calls, synthetic roots and sampled user/context frames;
             a successful None return is explicitly null.
    made     ``{entity_type: int}`` — what this frame alone placed.
    touched  ``{entity_type: int | [[lo, hi], ...]}`` — what it changed.
    error    optional — the exception type name (``ValueError``) when
             the call raised; only the innermost traced frame the
             exception left carries it. Absent on success.

Collapsed sibling form: past ``COLLAPSE_SIBLINGS`` (64) same-``qual``
*childless* siblings under one parent, occurrences 64..N fold into one
frame carrying ``occ`` = 64, ``occ_end`` = N, ``collapsed`` = count,
``id``/``id_end`` spanning the folded run, and ``made``/``touched`` =
their sums. The folded frame's key is ``frame_key(parent, qual, 64)``,
unchanged as the group grows.
Schema 3 also carries ``args_last`` and ``args_digest`` (hash of all folded
argument dictionaries in order), summed ``dur_ms``, and ``returns`` with
successful return count, first/last digest, and a hash of the ordered
return-presence/digest pairs. These summaries stay bounded as the fold grows.

Schema 1 vs 2: schema-1 docs keep their line-bearing keys and lack
``line_in_def``/``src``/``run.key_collisions``. Duplicate ``key`` values
are legal only when the writer counted them in ``run.key_collisions``
(the ``_sync`` re-mint edge); ``loads`` rejects a doc whose duplicates
outnumber the count.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA_VERSION = 3

#: Schema versions ``loads`` accepts. Schema 1 predates the structural
#: key (``line`` was hashed in) and the ``line_in_def``/``src`` fields.
SCHEMA_VERSIONS = frozenset({1, 2, SCHEMA_VERSION})

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
ENTITY_TYPES = frozenset(
    {"atoms", "bonds", "instances", "faces", "blocks", "ports", "parts", "nets"}
)


def sha8(text: str) -> str:
    """8 bytes (16 hex chars) of sha256 — collision-free well past the
    ``MAX_FRAMES`` scale while staying short enough to cite."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def frame_key(parent_key: str, qual: str, occ: int) -> str:
    """``sha8(parent.key, qual, occ)`` — the spec's stable key (schema 2:
    structure only, no position — an edit above the call keeps the key)."""
    return sha8(f"{parent_key}\x00{qual}\x00{occ}")


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
    schema = doc.get("schema")
    if schema not in SCHEMA_VERSIONS:
        raise ValueError(
            f"trace.json: schema {schema!r} not in {sorted(SCHEMA_VERSIONS)}"
        )
    frames = doc.get("frames")
    if not isinstance(frames, list):
        raise ValueError("trace.json: 'frames' must be a list")
    required = ("id", "key", "qual", "file", "line", "occ", "kind") + (
        ("line_in_def", "src") if schema >= 2 else ()
    )
    seen: set[int] = set()
    keys: set[str] = set()
    dups = 0
    for i, fr in enumerate(frames):
        if not isinstance(fr, dict):
            raise ValueError(f"trace.json: frames[{i}] is not an object")
        for req in required:
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
        if fr["key"] in keys:
            dups += 1
        keys.add(fr["key"])
    declared = int((doc.get("run") or {}).get("key_collisions") or 0)
    if dups > declared:
        raise ValueError(
            f"trace.json: {dups} duplicate frame key(s) but "
            f"run.key_collisions is {declared} — never merge silently"
        )
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
    "SCHEMA_VERSIONS",
    "dumps",
    "frame_key",
    "loads",
    "sha8",
]

"""``precis migrate-refs`` — rewrite legacy ref forms to universal handles.

One-off (re-runnable) data migration that unifies the *old* inline
reference grammars onto the single computed-handle form:

* a bare ``kind:ident`` mention (``memory:6184`` / ``finding:abc123``)
  → ``[me6184]`` / ``[fi42]``;
* an authoring link ``[[memory:6184]]`` → ``[me6184]``;
* a display link ``[label](memory:6184)`` → ``[label](me6184)``;
* a legacy draft cross-ref ``[¶<base58>]`` → ``[dc<chunk_id>]`` (and the
  same target inside a ``[label](¶<base58>)`` display link).

Deliberately **left alone**: ``paper:`` mentions and ``[§slug~n]``
citations (these stay the bibliography-keyed citation form), bare paper
cite_keys, conv handles, already-migrated ``[xx<id>]`` handles, and any
mention whose ``ident`` does not resolve to a live ref (so over-firing
prose like ``time:30`` is never touched — every rewrite is
resolution-gated).

Scope: **drafts** (live draft chunks, rewritten via ``edit_text`` so the
embedding/link cascade re-derives) and **thoughts** (``memory`` bodies —
the ``memory_body`` chunk, rewritten via the memory ``edit`` verb so the
body chunk re-embeds and auto-mention links re-sync). Papers, jobs, convs
and other body chunks are out of scope and reported as skipped.

Dry-run by default: prints the planned rewrites and writes nothing. Pass
``--apply`` to commit. Re-running ``--apply`` is safe and idempotent
(an already-migrated body no longer matches).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis.cli._common import resolve_dsn
from precis.utils import handle_registry, mentions

# Kinds whose bare ``kind:ident`` mention we rewrite. The full linkify
# allowlist minus ``paper`` (its mentions are citations, left as-is) and
# the low-signal kinds the grammar already suppresses.
_REWRITE_KINDS: frozenset[str] = (
    mentions.LINKIFY_KINDS - mentions.LOW_SIGNAL_KINDS - {"paper"}
)

# One pass over the three bracket shapes (authoring / display / bare) plus
# the bare ``kind:ident`` mention — same precedence the LaTeX exporter
# uses, so a ``kind:ref`` *inside* a bracket is consumed by the bracket
# branch (not double-rewritten). Group names are inherited from the
# sub-patterns: auth | (disp,tgt) | bare | (kind,id,chunk).
_COMBINED = re.compile(
    mentions.AUTHORING_PATTERN.pattern
    + "|"
    + mentions.DISPLAY_LINK_PATTERN.pattern
    + "|"
    + mentions.BARE_BRACKET_REF_PATTERN.pattern
    + "|"
    + mentions.REF_PATTERN.pattern
)

# Cheap SQL pre-filter: a row is a candidate iff it carries a rewriteable
# ``kind:`` prefix or a legacy pilcrow. Keeps the scan off the whole
# corpus; ``rewrite`` makes the real decision per row.
_PREFILTER = "(\\m(" + "|".join(sorted(_REWRITE_KINDS)) + "):)|¶"

# Resolvers: ``(kind, ident) -> handle | None`` and ``base58 -> dc-handle
# | None``.
RecordResolver = Callable[[str, str], "str | None"]
ChunkResolver = Callable[[str], "str | None"]


# ── pure rewrite core (no DB; resolvers injected) ─────────────────────


def _ref_handle(
    kind: str, ident: str, chunk: str | None, resolve_record: RecordResolver
) -> str | None:
    """Handle for a ``kind:ident(~chunk)?`` mention, or ``None`` to leave
    it untouched (paper citation, off-allowlist prose, chunk-addressed
    mention that can't ride a bare handle, or an ident that doesn't
    resolve)."""
    if kind not in _REWRITE_KINDS:
        return None
    if chunk:  # a ~chunk suffix has no bare-handle form — leave it
        return None
    return resolve_record(kind, ident.lstrip("#"))


def _address_to_handle(
    addr: str, resolve_record: RecordResolver, resolve_chunk: ChunkResolver
) -> str | None:
    """Handle for a link *target* — either a ``¶<base58>`` draft xref or a
    ``kind:ident`` mention. ``None`` for anything else (URL, ``§`` cite,
    already-migrated handle)."""
    if addr.startswith("¶"):
        return resolve_chunk(addr[1:])
    m = mentions.REF_PATTERN.fullmatch(addr)
    if m is None:
        return None
    return _ref_handle(m.group("kind"), m.group("id"), m.group("chunk"), resolve_record)


def _rewrite_match(
    m: re.Match[str],
    resolve_record: RecordResolver,
    resolve_chunk: ChunkResolver,
) -> str:
    whole = m.group(0)
    # [[address]] authoring link → bare [handle]
    if m.group("auth") is not None:
        h = _address_to_handle(m.group("auth").strip(), resolve_record, resolve_chunk)
        return f"[{h}]" if h else whole
    # [label](target) display link → [label](handle)
    if m.group("tgt") is not None:
        h = _address_to_handle(m.group("tgt").strip(), resolve_record, resolve_chunk)
        return f"[{m.group('disp')}]({h})" if h else whole
    # [bare] bracketed ref — only the legacy ¶ form migrates
    if m.group("bare") is not None:
        bare = m.group("bare")
        if bare.startswith("¶"):
            h = resolve_chunk(bare[1:])
            return f"[{h}]" if h else whole
        return whole  # §cite or an already-migrated [xx<id>] handle
    # bare kind:ident mention → [handle]
    if m.group("kind") is not None:
        h = _ref_handle(
            m.group("kind"), m.group("id"), m.group("chunk"), resolve_record
        )
        return f"[{h}]" if h else whole
    return whole


def rewrite(
    text: str,
    *,
    resolve_record: RecordResolver,
    resolve_chunk: ChunkResolver,
) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite every legacy ref form in ``text``. Returns
    ``(new_text, changes)`` where ``changes`` is the list of
    ``(old_span, new_span)`` substitutions made (empty ⇒ no change)."""
    changes: list[tuple[str, str]] = []

    def _sub(m: re.Match[str]) -> str:
        new = _rewrite_match(m, resolve_record, resolve_chunk)
        if new != m.group(0):
            changes.append((m.group(0), new))
        return new

    return _COMBINED.sub(_sub, text), changes


# ── store-bound resolvers ─────────────────────────────────────────────


def _make_resolvers(store: Any) -> tuple[RecordResolver, ChunkResolver]:
    def resolve_record(_kind: str, ident: str) -> str | None:
        ref = mentions.resolve_handle_ref(store, ident)
        if ref is None or getattr(ref, "deleted_at", None) is not None:
            return None
        # The actual kind wins (a ``memory:6184`` whose row is really a
        # finding formats as ``fi6184``) — matching read-time resolution.
        return handle_registry.try_format(ref.kind, ref.id)

    def resolve_chunk(base58: str) -> str | None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT chunk_id FROM chunks WHERE handle = %s", (base58,)
            ).fetchone()
        if row is None:
            return None
        return handle_registry.format_handle("draft", int(row[0]), chunk=True)

    return resolve_record, resolve_chunk


# ── scan + apply ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Change:
    """One target whose text changes. ``ident`` is the draft chunk handle
    or the memory ``ref_id``; ``changes`` is the clean ``(old → new)`` span
    list for the dry-run report."""

    ident: Any
    old: str
    new: str
    changes: list[tuple[str, str]]


def _scan_drafts(store: Any) -> list[Change]:
    """A :class:`Change` for every live draft chunk whose text changes."""
    resolve_record, resolve_chunk = _make_resolvers(store)
    with store.pool.connection() as conn:
        rows = conn.execute(
            """SELECT c.handle, c.text
                 FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
                WHERE r.kind = 'draft' AND r.deleted_at IS NULL
                  AND c.retired_at IS NULL AND c.pos IS NOT NULL
                  AND c.text ~ %s""",
            (_PREFILTER,),
        ).fetchall()
    out: list[Change] = []
    for handle, text in rows:
        new, changes = rewrite(
            text, resolve_record=resolve_record, resolve_chunk=resolve_chunk
        )
        if changes:
            out.append(Change(handle, text, new, changes))
    return out


def _scan_memories(store: Any) -> list[Change]:
    """A :class:`Change` for every live memory whose body changes.

    A memory's prose lives in its ``memory_body`` chunk (migration 0050),
    not ``refs.title`` (now a short header) — so scan the chunk. The apply
    side (``handler.edit(id, text=...)``) rewrites that same chunk.
    """
    resolve_record, resolve_chunk = _make_resolvers(store)
    with store.pool.connection() as conn:
        rows = conn.execute(
            """SELECT c.ref_id, c.text
                 FROM chunks c JOIN refs r ON r.ref_id = c.ref_id
                WHERE r.kind = 'memory' AND r.deleted_at IS NULL
                  AND c.chunk_kind = 'memory_body'
                  AND c.text ~ %s""",
            (_PREFILTER,),
        ).fetchall()
    out: list[Change] = []
    for ref_id, text in rows:
        if not text:
            continue
        new, changes = rewrite(
            text, resolve_record=resolve_record, resolve_chunk=resolve_chunk
        )
        if changes:
            out.append(Change(int(ref_id), text, new, changes))
    return out


def _print_samples(label: str, items: list[Change], n: int = 8) -> None:
    total_spans = sum(len(c.changes) for c in items)
    print(
        f"\n{label}: {len(items)} to rewrite ({total_spans} references)",
        file=sys.stderr,
    )
    for c in items[:n]:
        shown = ", ".join(f"{o} → {x}" for o, x in c.changes[:4])
        more = "" if len(c.changes) <= 4 else f" (+{len(c.changes) - 4})"
        print(f"  {c.ident}: {shown}{more}", file=sys.stderr)
    if len(items) > n:
        print(f"  … and {len(items) - n} more", file=sys.stderr)


def add_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``migrate-refs`` subparser on ``sub``."""
    p = sub.add_parser(
        "migrate-refs",
        help="Rewrite legacy kind:ref / ¶ references to universal handles.",
        description=(
            "Unify legacy inline references (kind:ident mentions, "
            "[[authoring]] links, [¶base58] draft xrefs) onto the single "
            "[handle] form across draft chunks and memory bodies. Leaves "
            "paper: mentions and [§…] citations. Dry-run by default; pass "
            "--apply to write."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes. Without this flag the command is a dry-run.",
    )
    p.add_argument(
        "--scope",
        choices=("all", "drafts", "thoughts"),
        default="all",
        help="Which corpora to migrate (default: all).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Rewrite at most N items per scope (default: all).",
    )
    p.add_argument("--database-url", default=None, help="Override PRECIS_DATABASE_URL.")
    return p


def run(args: argparse.Namespace) -> None:
    """Execute ``precis migrate-refs``."""
    from precis.config import load_config
    from precis.runtime import build_runtime

    cfg = load_config()
    dsn = resolve_dsn(args.database_url, cfg=cfg)
    cfg = cfg.model_copy(update={"database_url": dsn})
    runtime = build_runtime(cfg)
    store = runtime.store
    if store is None:
        print(
            "migrate-refs: no database configured - set PRECIS_DATABASE_URL",
            file=sys.stderr,
        )
        sys.exit(2)

    dry_run = not args.apply
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(
        f"migrate-refs [{mode}]: scope={args.scope} limit={args.limit}", file=sys.stderr
    )

    counts: Counter[str] = Counter()

    if args.scope in ("all", "drafts"):
        drafts = _scan_drafts(store)
        if args.limit is not None:
            drafts = drafts[: args.limit]
        _print_samples("draft chunks", drafts)
        if not dry_run:
            for c in drafts:
                store.edit_text(c.ident, c.new, source={"tool": "migrate-refs"})
        counts["draft_chunks"] = len(drafts)

    if args.scope in ("all", "thoughts"):
        memories = _scan_memories(store)
        if args.limit is not None:
            memories = memories[: args.limit]
        _print_samples("memory bodies", memories)
        if not dry_run:
            handler = runtime.hub.handler_for("memory")
            for c in memories:
                handler.edit(id=c.ident, mode="replace", text=c.new)
        counts["memory_bodies"] = len(memories)

    print(
        f"\nmigrate-refs [{mode}] done: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
        file=sys.stderr,
    )
    if dry_run and counts.total():
        print("Re-run with --apply to commit.", file=sys.stderr)


__all__ = ["add_parser", "rewrite", "run"]

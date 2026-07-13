"""Ref-mention grammar + resolution — single source for two surfaces.

A memory (or any note-like body) routinely names other refs inline:
prefixed ``kind:id~chunk`` handles (``memory:6184``, ``paper:acheson26~12``),
bare paper cite_keys (``futrell25``), and bare Discord conv handles
(``discord/<server>/<channel>/<thread>``). Two features consume the
same grammar:

1. **Read-time (web).** ``precis_web.linkify`` turns the handles into
   hover-preview anchors, and the refs detail page renders a
   References panel + inline ``[N]`` footnotes.
2. **Write-time (this module's reason to exist).** When a memory is
   inserted/edited, we resolve the same handles and materialise real
   ``links`` rows (``related-to``) so the memory becomes a node in the
   graph — discoverable from the *target's* side, not just visually at
   read time.

The regexes and kind allowlist live here so both surfaces share one
grammar; ``precis_web.linkify`` re-exports them under their old private
names for back-compat. This module is deliberately dependency-light
(stdlib + a duck-typed ``store``) so the core handlers can import it
without dragging in the web stack.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Grammar — the canonical home for these patterns (moved out of
# precis_web.linkify so the write path can reach them without a web dep).
# ---------------------------------------------------------------------------

#: Prefixed ``kind:ref(~chunk)?``. ``id`` is numeric (``#?[0-9]+``),
#: a bare slug, or a path slug (``discord/a/b/c``). Trailing ``~N`` /
#: ``~N..M`` / ``~pN`` is the chunk address.
REF_PATTERN = re.compile(
    r"\b"
    r"(?P<kind>[a-z][a-z0-9-]*)"
    r":"
    r"(?P<id>"
    r"#?[0-9]+"
    r"|"
    r"[A-Za-z][A-Za-z0-9_-]*(?:/[A-Za-z0-9_-]+)+"
    r"|"
    r"[A-Za-z][A-Za-z0-9_-]*"
    r")"
    r"(?P<chunk>~(?:p[0-9]+|[0-9]+(?:\.\.[0-9]+)?))?"
    r"(?!\w)"
)

#: Bare conv handle the Discord bridge emits — all three numeric path
#: segments required so it doesn't grab ``discord/general``.
BARE_CONV_PATTERN = re.compile(
    r"\bdiscord/[0-9]+/[0-9]+/[0-9]+(?:~(?:p[0-9]+|[0-9]+(?:\.\.[0-9]+)?))?"
    r"(?!\w)"
)

#: Bare paper cite_key: ``<surname><2-digit year><optional letter>``.
#: Either a chunk suffix disambiguates a ≥2-letter key, or (no suffix)
#: ≥3 surname letters keep ``ai99`` / ``ml22`` off prose.
BARE_PAPER_PATTERN = re.compile(
    r"(?<![\w-])"
    r"(?:"
    r"[a-z]{2,}[0-9]{2}[a-z]?~(?:p[0-9]+|[0-9]+(?:\.\.[0-9]+)?)"
    r"|"
    r"[a-z]{3,}[0-9]{2}[a-z]?"
    r")"
    r"(?!\w)"
)

#: Kinds we resolve. The regex over-fires on ``noun:value`` prose
#: (``user:asa``, ``tag:open``); emission/resolution is gated on this
#: allowlist. Keep aligned with ``_REFS_BROWSABLE_KINDS`` in
#: routes/refs.py.
LINKIFY_KINDS: frozenset[str] = frozenset(
    {
        "memory",
        "conv",
        "gripe",
        "pres",
        "oracle",
        "paper",
        "patent",
        "todo",
        "job",
        "finding",
        "citation",
        "draft",
        "anki",
        "perplexity-research",
        "perplexity-reasoning",
        "web",
        "youtube",
        "websearch",
        "cron",
        "message",
        "math",
        "calc",
        "skill",
        "provenance",
        "random",
    }
)

#: Real kinds we still suppress because they read as noise in prose
#: (``tag:open`` is ambiguous with a tag namespace). Today both are
#: absent from ``LINKIFY_KINDS`` too; this stays as the explicit
#: opt-out lever.
LOW_SIGNAL_KINDS: frozenset[str] = frozenset({"tag", "link"})


# ---------------------------------------------------------------------------
# Draft inline-reference grammar (ADR 0033 §8) — the bracket / sigil forms
# layered on top of the bare ``kind:ref`` mentions above. They live here,
# the grammar SSOT, so both consumers share one definition: the parser
# (``precis.utils.draft_markup``) and the highlighter
# (``precis_web.linkify``). The *superset* a draft chunk may carry is
# these bracket forms ∪ the bare ``kind:ref`` mentions.
#
#   [[<kind:id>]]      authoring link (provenance; renders to nothing)
#   [text](<target>)   display link — target is ¶handle / §paper~n /
#                      kind:id / URL; the reader sees ``text``
#   [¶<handle>]        bare cross-ref to a chunk in this draft
#   [§<paper>~<n>]     bare citation to an external corpus chunk
# ---------------------------------------------------------------------------

#: ``[[address]]`` — authoring link, provenance only.
AUTHORING_PATTERN = re.compile(r"\[\[(?P<auth>[^\[\]]+)\]\]")
#: ``[display](target)`` — markdown display link.
DISPLAY_LINK_PATTERN = re.compile(r"\[(?P<disp>[^\[\]]*)\]\((?P<tgt>[^()]+)\)")
#: A bare bracketed reference (no display text): ``[me6184]`` — the
#: universal form (a handle is a ref to something), or the legacy sigil
#: forms ``[¶h]`` / ``[§p~n]``. The handle alternative is ``<2-char
#: code><digits>``; resolution gates it against the registry, so a
#: non-handle like ``[ab12]`` simply doesn't resolve and stays literal.
BARE_BRACKET_REF_PATTERN = re.compile(r"\[(?P<bare>[¶§][^\[\]]+|[a-z]{2}\d+)\]")

#: Bracketed patent *public number* — country code + serial (+ optional
#: kind code), e.g. ``[US9927397B1]``, ``[EP1234567A1]``, or the app form
#: ``[US20210012345A1]``. Distinct from the ADR-0036 handle form
#: ``[pt34596]`` (2-char code + trailing digits): a public number carries
#: trailing letters, so it never matches the handle alternation above and
#: was silently dropped by the autolinker (gripe #48807). Case-insensitive
#: because memories cite it lower-cased. **Emission is DB-gated** — see
#: :func:`resolve_patent_pubnum`: a match becomes a link only when it
#: resolves to a real ``ref_identifiers.id_kind='pub_id'`` patent, so
#: prose like ``[US Patent 123]`` or an invented ``[US0000]`` stays literal.
PATENT_PUBNUM_PATTERN = re.compile(r"\[(?P<pub>[A-Za-z]{2}\d{3,}[A-Za-z]?\d{0,2})\]")

#: ``§<slug>~<n>`` citation sugar — equivalent to ``paper:<slug>~<n>``.
DRAFT_CITE_PATTERN = re.compile(
    r"§(?P<slug>[A-Za-z][A-Za-z0-9_-]*)"
    r"(?P<chunk>~(?:p[0-9]+|[0-9]+(?:\.\.[0-9]+)?))?"
)

#: The three bracket shapes as one alternation (authoring first so it
#: wins over the display form on ``[[…]]``). ``draft_markup`` parses
#: against this; ``linkify`` folds it into its combined highlight pass.
DRAFT_MARKUP_PATTERN = re.compile(
    AUTHORING_PATTERN.pattern
    + "|"
    + DISPLAY_LINK_PATTERN.pattern
    + "|"
    + BARE_BRACKET_REF_PATTERN.pattern
)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

#: ``(kind, ident, chunk)`` — ``ident`` is the raw id/slug as written
#: (``#`` stripped by callers when needed); ``chunk`` is the regex group
#: including its leading ``~`` (or ``None``).
Handle = tuple[str, str, "str | None"]


def extract_handles(body: str) -> list[Handle]:
    """Every ref handle in ``body``, in appearance order, deduplicated.

    Mirrors the web References-panel walk exactly: prefixed handles
    gated on :data:`LINKIFY_KINDS`, bare conv handles → ``conv``, bare
    paper cite_keys → ``paper``. Dedup key is ``(kind, ident, chunk)``.
    """
    if not body:
        return []
    seen: set[Handle] = set()
    out: list[Handle] = []

    def _push(kind: str, ident: str, chunk: str | None) -> None:
        key = (kind, ident.lstrip("#"), chunk)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    for m in REF_PATTERN.finditer(body):
        kind = m.group("kind")
        if kind not in LINKIFY_KINDS or kind in LOW_SIGNAL_KINDS:
            continue
        _push(kind, m.group("id"), m.group("chunk"))
    for m in BARE_CONV_PATTERN.finditer(body):
        slug, _, suffix = m.group(0).partition("~")
        _push("conv", slug, ("~" + suffix) if suffix else None)
    for m in BARE_PAPER_PATTERN.finditer(body):
        slug, _, suffix = m.group(0).partition("~")
        _push("paper", slug, ("~" + suffix) if suffix else None)
    return out


def chunk_to_pos(chunk: str | None) -> int | None:
    """A ``~N`` chunk address → ``N`` (a single chunk ``pos``).

    Ranges (``~1..5``) and PDF-page jumps (``~p2``) are NOT single
    chunk endpoints, so they collapse to ``None`` (ref-level link).
    """
    if not chunk:
        return None
    body = chunk[1:] if chunk.startswith("~") else chunk
    return int(body) if body.isdigit() else None


# ---------------------------------------------------------------------------
# Resolution — single-sourced lookup used by the web _expand_handle and
# the write-time autolinker.
# ---------------------------------------------------------------------------


def resolve_handle_ref(store: Any, ident: str, *, include_deleted: bool = True) -> Any:
    """Resolve a handle's ``ident`` to a ``Ref`` (or ``None``).

    Numeric idents fetch by id; slugs go through ``ref_identifiers``
    matching ``id_kind IN ('cite_key', 'pub_id')`` — the latter is how a
    ``finding`` is addressed (its 6-char base32 ``pub_id``), so a
    ``finding:<pub_id>`` mention resolves. The same two-step the web
    preview route uses. Kind is intentionally not re-checked:
    ``memory:6134`` resolves ref 6134 whatever its kind, matching the
    read-time behaviour.
    """
    ident = ident.lstrip("#")
    try:
        numeric: int | None = int(ident)
    except ValueError:
        numeric = None
    if numeric is not None:
        return store.fetch_refs_by_ids([numeric], include_deleted=include_deleted).get(
            numeric
        )
    # Slug → cite_key / pub_id row → ref_id (cite_key wins on collision).
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id FROM ref_identifiers "
                "WHERE id_kind IN ('cite_key', 'pub_id') AND id_value = %s "
                "ORDER BY (id_kind = 'cite_key') DESC LIMIT 1",
                (ident,),
            ).fetchone()
    except Exception:
        log.debug("mentions: cite_key lookup failed for %r", ident, exc_info=True)
        return None
    if row is None:
        return None
    rid = int(row[0])
    return store.fetch_refs_by_ids([rid], include_deleted=include_deleted).get(rid)


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """A resolved, live link endpoint for the write-time autolinker."""

    dst_ref_id: int
    dst_pos: int | None


def resolve_handle_target(store: Any, token: str) -> LinkTarget | None:
    """A bare ADR 0036 universal handle (``me5`` a memory, ``dc41`` a draft
    chunk, ``pc10`` a paper chunk, …) → a live ``LinkTarget`` via the one
    decoder ``store.resolve_handle``. ``None`` if ``token`` is not a
    well-formed / resolvable handle, so the caller falls through to the
    legacy ``kind:id`` / ``¶`` / ``§`` paths. The single rule the LLM
    relies on: *a handle is a ref to something*."""
    from precis.utils import handle_registry

    if not handle_registry.is_well_formed(handle_registry.normalize(token)):
        return None
    try:
        r = store.resolve_handle(token)
    except Exception:
        log.debug("mentions: resolve_handle failed for %r", token, exc_info=True)
        return None
    if r is None:
        return None
    pos = r.chunk_ord if getattr(r, "chunk_id", None) is not None else None
    return LinkTarget(int(r.ref_id), pos)


def resolve_patent_pubnum(store: Any, token: str) -> LinkTarget | None:
    """A patent public number (``US9927397B1``) → its patent ``LinkTarget``.

    Looks the token up in ``ref_identifiers`` under ``id_kind='pub_id'``
    (case-insensitively — memories cite it lower-cased), and emits a link
    only when the row resolves to a **live patent** ref. The kind guard
    keeps a same-shaped ``pub_id`` on another kind from being mistaken for
    a patent, and the DB hit is the over-fire gate: an unknown bracketed
    ``[US0000]`` finds no row and stays literal (gripe #48807). ``None``
    on no row / soft-deleted / non-patent, so the caller drops it.
    """
    val = token.strip()
    if not val:
        return None
    try:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id FROM ref_identifiers "
                "WHERE id_kind = 'pub_id' AND upper(id_value) = upper(%s) LIMIT 1",
                (val,),
            ).fetchone()
    except Exception:
        log.debug("mentions: pub_id lookup failed for %r", val, exc_info=True)
        return None
    if row is None:
        return None
    rid = int(row[0])
    ref = store.fetch_refs_by_ids([rid]).get(rid)
    if ref is None or getattr(ref, "deleted_at", None) is not None:
        return None
    if getattr(ref, "kind", None) != "patent":
        return None
    return LinkTarget(rid, None)


def _patent_pubnum_tokens(body: str) -> list[str]:
    """Every bracketed patent-public-number token in ``body`` (inner text,
    without the brackets). Resolution is DB-gated downstream, so this only
    has to be shape-permissive."""
    return [m.group("pub") for m in PATENT_PUBNUM_PATTERN.finditer(body)]


def _universal_handle_tokens(body: str) -> list[str]:
    """Every bracketed handle token in ``body`` — the bare ``[me5]`` form,
    a ``[label](me5)`` target, or an ``[[me5]]`` authoring address.
    (Legacy ``¶``/``§`` and ``kind:id`` tokens fall out in
    :func:`resolve_handle_target`, which only resolves well-formed
    handles.)"""
    out: list[str] = []
    for m in DRAFT_MARKUP_PATTERN.finditer(body):
        if m.group("auth") is not None:
            out.append(m.group("auth").strip())
        elif m.group("tgt") is not None:
            out.append(m.group("tgt").strip())
        elif m.group("bare") is not None:
            out.append(m.group("bare"))
    return out


def resolve_link_targets(
    store: Any, body: str, *, exclude_ref_id: int | None = None
) -> list[LinkTarget]:
    """Resolve every handle in ``body`` to a live ``LinkTarget``.

    Covers both the legacy ``kind:ident`` mentions and the universal
    ``[handle]`` form (bare / display / authoring) so a note's edges
    survive the migration onto handles. Skips handles that don't resolve,
    point at a soft-deleted ref, or point back at ``exclude_ref_id`` (the
    note we're linking *from* — no self-loops). Deduplicated by
    ``(dst_ref_id, dst_pos)`` so two mentions of the same chunk produce
    one link.
    """
    targets: dict[tuple[int, int | None], LinkTarget] = {}
    for _kind, ident, chunk in extract_handles(body):
        ref = resolve_handle_ref(store, ident)
        if ref is None or getattr(ref, "deleted_at", None) is not None:
            continue
        if exclude_ref_id is not None and ref.id == exclude_ref_id:
            continue
        pos = chunk_to_pos(chunk)
        targets.setdefault((ref.id, pos), LinkTarget(ref.id, pos))
    for token in _universal_handle_tokens(body):
        tgt = resolve_handle_target(store, token)
        if tgt is None:
            continue
        if exclude_ref_id is not None and tgt.dst_ref_id == exclude_ref_id:
            continue
        targets.setdefault((tgt.dst_ref_id, tgt.dst_pos), tgt)
    for token in _patent_pubnum_tokens(body):
        tgt = resolve_patent_pubnum(store, token)
        if tgt is None:
            continue
        if exclude_ref_id is not None and tgt.dst_ref_id == exclude_ref_id:
            continue
        targets.setdefault((tgt.dst_ref_id, tgt.dst_pos), tgt)
    return list(targets.values())

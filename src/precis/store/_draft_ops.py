"""Store ops for the `draft` kind: create/add/read core (edit/move/retire alongside).

Drafts use chunk columns ingest never touches — `handle`, `pos`
(fractional sibling order), `parent_chunk_id`, `content_sha`,
`retired_at` — hence their own composed sub-store, :class:`DraftStore`
(`store.drafts`), holding a :class:`~precis.store.core.StoreCore` ref
rather than mixing into ``Store``. Every structural write logs a
`chunk_events` row.

``Store`` carries no flat delegation for drafts — reached only as
``store.drafts.*`` (the migration this module itself once needed is
done; see ``docs/backlog/codereview-store-decomposition.md``). One level
down, the same carve is mid-flight: the review ledger (``record_review``,
``reviewable_chunks``, ...) is carved into
:class:`~precis.store._draft_review_ops.DraftReviewStore`
(``store.drafts.review``), and *this* module still exposes every review
method flatly (``store.drafts.record_review(...)``) via a transitional
delegation block below, deleted per call site as callers migrate to
``store.drafts.review.*``.
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput, Gone, NotFound
from precis.store._draft_review_ops import (
    ChunkReviewEntry,
    DraftReviewRow,
    DraftReviewStore,
    ReviewableChunk,
)
from precis.store.core import StoreCore
from precis.utils import handle_registry
from precis.utils.fractional import key_between, n_keys_between
from precis.utils.handles import new_handle

if TYPE_CHECKING:
    from precis.store.store import Store

#: Re-exported so review-fanout code (the lens × chunk-kind mapping,
#: ``quest/review_fanout.py``) imports it off this module rather than
#: reaching into ``utils.wordcount`` directly — but ``wordcount.py``
#: stays the one place the set is *defined* (it must stay
#: store-independent), so this is the single source of truth, not a
#: second drifting copy. The actual consumer moved to
#: :mod:`precis.store._draft_review_ops` (``review_rollup_for_draft``)
#: with the rest of the review surface; the explicit ``as`` re-export
#: below keeps this an intentional public name, not dead code.
from precis.utils.wordcount import PROSE_CHUNK_KINDS as PROSE_CHUNK_KINDS

_HANDLE_RETRIES = 6

#: An ``ask-user`` / ``halt`` tag value of the form ``see-chunk-N`` is a
#: redirect handle: the real (long / whitespaced) prose lives in a
#: ``tag_overflow`` chunk at ``ord = N`` on the ref
#: (handlers._tag_redirect.redirect_long_tag_values).
_SEE_CHUNK_RE = re.compile(r"^see-chunk-(\d+)$")

#: Above this many characters of concatenated prose, skip the inline
#: Schwartz-Hearst abbreviation scan in ``defined_abbrevs`` (regex over the
#: whole draft — multi-second on a 1M+ char draft). Explicit ``term``
#: chunks still populate the glossary; only the auto-detected inline pairs
#: are dropped for very large drafts.
_ABBREV_INLINE_SCAN_CAP = 300_000

#: Request lifecycle ordering for :meth:`DraftStore.anchored_todos` — active
#: first, then done/abandoned (which now *persist* so you can click in and
#: debug the LLM run, rather than vanishing on completion).
_REQUEST_ORDER = {"open": 0, "scheduled": 1, "doing": 2, "paused": 3}


def content_sha(text: str) -> str:
    """Hash of the resolved-for-search text (markers are stripped later;
    for now the raw source). Drives per-consumer re-derivation."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: Intra-draft chunk cross-refs as they appear *in prose* — the bare
#: bracket handle ``[dc41]``, the display-link target ``](dc41)`` (the
#: ``[cap]`` half is left intact), and the legacy ``[¶<base58>]`` anchor.
#: Only these forms are document-internal (not graph edges), so
#: :meth:`fork_draft`'s link remap never touches them — see
#: :func:`_remap_intra_draft_xrefs`.
_DRAFT_XREF_REMAP_RE = re.compile(
    r"\[dc(?P<bare>\d+)\]"
    r"|\]\(dc(?P<tgt>\d+)"
    r"|\[¶(?P<legacy>[^\[\]]+)\]"
)


def _remap_intra_draft_xrefs(
    text: str,
    id_map: dict[int, int],
    handle_to_new_id: dict[str, int],
) -> str:
    """Rewrite in-prose intra-draft cross-refs (``[dc<id>]``, ``[cap](dc<id>)``,
    legacy ``[¶<base58>]``) onto copied chunks, for :meth:`fork_draft`. These
    are document-internal (chunk text, not ``links``), so the fork's link
    remap can't reach them — copied verbatim they'd point at the source
    draft. ``id_map`` (old chunk_id → new) remaps ``dc<id>``; legacy ``¶``
    anchors remap via ``handle_to_new_id``, normalised to ``[dc<id>]``. A
    cross-draft ``[dc<id>]`` (chunk_id absent from both maps) is left alone."""

    def _sub(m: re.Match[str]) -> str:
        bare = m.group("bare")
        if bare is not None:
            new = id_map.get(int(bare))
            return f"[dc{new}]" if new is not None else m.group(0)
        tgt = m.group("tgt")
        if tgt is not None:
            new = id_map.get(int(tgt))
            return f"](dc{new}" if new is not None else m.group(0)
        new = handle_to_new_id.get(m.group("legacy"))
        return f"[dc{new}]" if new is not None else m.group(0)

    return _DRAFT_XREF_REMAP_RE.sub(_sub, text)


@dataclass(frozen=True, slots=True)
class DraftChunk:
    chunk_id: int
    ref_id: int
    handle: str  # legacy base-58 anchor (internal key, retiring)
    chunk_kind: str
    text: str
    pos: str
    parent_chunk_id: int | None
    depth: int
    meta: dict[str, Any] = field(default_factory=dict)
    #: Owning ref kind — ``'draft'`` (default) or ``'plan'``.
    #: Drives the ``.dc`` handle code so plan chunks render ``pe<id>`` while
    #: drafts stay ``dc<id>``. The chunk *columns* are identical; only the
    #: handle namespace differs.
    kind: str = "draft"
    #: ``retired_at IS NOT NULL`` — the chunk is no longer part of the draft
    #: (excluded from reading order, search, and export) but stays directly
    #: addressable by handle (:meth:`get_draft_chunk` is live-or-retired,
    #: gripe 49153). Only populated on the direct-address path; bulk readers
    #: filter retired rows in SQL and leave the default False (gr192827:
    #: an undisclosed retired chunk reads as a search bug).
    retired: bool = False

    @property
    def dc(self) -> str:
        """Universal handle for this chunk (``dc42`` for a draft,
        ``pe42`` for a plan). The agent-facing address; supersedes the legacy
        ``¶<base58>``."""
        return handle_registry.format_handle(self.kind, self.chunk_id, chunk=True)


@dataclass(frozen=True, slots=True)
class TocEntry:
    """A TOC heading, enriched with its gist (llm summary) and keywords
    when present. ``depth`` is relative to the TOC root; the §-number is
    computed by the renderer from the depth sequence."""

    handle: str  # legacy base-58 anchor (internal)
    depth: int
    title: str
    keywords: list[str]
    gist: str | None
    chunk_id: int = 0
    #: Owning ref kind — ``'draft'`` (default) or ``'plan'``;
    #: drives the ``.dc`` handle code (see :class:`DraftChunk`).
    kind: str = "draft"

    @property
    def dc(self) -> str:
        """Universal handle for this heading (``dc42`` draft /
        ``pe42`` plan)."""
        return handle_registry.format_handle(self.kind, self.chunk_id, chunk=True)


@dataclass(frozen=True, slots=True)
class DraftWorkItem:
    """An open todo working on this draft (walked draft→project→subtree),
    with child-job status and whether a failure bubble *blocks* it.
    Surfaces stuck enrichment work in the draft outline instead of
    burying it in the task tree."""

    todo_id: int
    title: str
    blocked: bool  # carries an OPEN:child-failed:* bubble
    jobs: tuple[tuple[int, str], ...]  # (job_ref_id, status) for child jobs
    # Raw ``ask-user[:question]`` tag values on this todo — work that is
    # waiting on a human answer. Surfaced inline in the draft so the
    # operator answers in place instead of hunting the Asks/alerts tab.
    asks: tuple[str, ...] = ()


def _split_blocks(text: str) -> list[str]:
    """Split a multi-paragraph `put` at blank-line boundaries; trim.
    (Block elements like fenced code aren't special-cased yet.)"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = [p.strip() for p in text.split("\n\n")]
    blocks = [p for p in parts if p]
    return blocks or [text.strip()]


class _AbbrevMixin:
    """Abbreviation detection + ignore-list ops, mixed into
    :class:`DraftStore` (split out for legibility). ``self`` is a
    ``DraftStore`` at runtime; the stubs below are the mixin
    forward-declaration so mypy type-checks this class standalone."""

    pool: Any
    tx: Any
    add_chunks: Any  # provided by DraftStore
    reading_order: Any  # provided by DraftStore
    move_chunk: Any  # provided by DraftStore
    retire_chunk: Any  # provided by DraftStore
    _children: Any  # provided by DraftStore

    def ensure_glossary_heading(self, ref_id: int) -> str:
        """Back-compat alias for the glossary registry's home heading. Superseded by :meth:`ensure_registry_heading`; kept for the
        draft-importer and any legacy caller."""
        return self.ensure_registry_heading(ref_id, "glossary")

    def ensure_registry_heading(self, ref_id: int, role: str) -> str:
        """``dc<chunk_id>`` handle of the draft's home heading for ``role``
        (``glossary`` / ``parts`` / ``components``) — where ``term`` leaves
        of that registry file.

        Found **by ``meta.registry == role``**, a stable tag surviving a
        heading rename (a text-only match would mint a second duplicate
        heading on rename/import). Failing that, **adopts** a legacy
        text-matched heading (stamps ``meta.registry``) rather than mint a
        duplicate; mints + stamps fresh only as last resort. A
        one-per-role reconcile then folds any stragglers."""
        from precis.draft import registry as _reg

        with self.pool.connection() as conn:
            # 1. The role-tagged home (earliest by pos if several — the
            #    reconcile below collapses the rest).
            row = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'heading' AND retired_at IS NULL "
                "AND pos IS NOT NULL AND meta->>'registry' = %s "
                "ORDER BY pos LIMIT 1",
                (ref_id, role),
            ).fetchone()
            if row is None:
                # 2. Adopt a legacy text-matched heading — stamp the role on it.
                aliases = list(_reg.LEGACY_HEADING_ALIASES.get(role, frozenset()))
                if aliases:
                    row = conn.execute(
                        "SELECT chunk_id FROM chunks WHERE ref_id = %s "
                        "AND chunk_kind = 'heading' AND retired_at IS NULL "
                        "AND pos IS NOT NULL AND lower(btrim(text)) = ANY(%s) "
                        "ORDER BY pos LIMIT 1",
                        (ref_id, aliases),
                    ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE chunks SET meta = COALESCE(meta, '{}'::jsonb) "
                        "|| jsonb_build_object('registry', %s::text) "
                        "WHERE chunk_id = %s",
                        (role, int(row[0])),
                    )
        if row is not None:
            self._reconcile_registry_headings(ref_id, role)
            return handle_registry.format_handle("draft", int(row[0]), chunk=True)
        # 3. Mint + stamp a fresh home heading at the end of the draft.
        created = self.add_chunks(
            ref_id=ref_id,
            chunk_kind="heading",
            text=_reg.heading_title(role),
            at={"last": True},
            meta={"registry": role},
        )
        return str(created[0].dc)  # dc handle, matching the lookup/adopt paths

    def _reconcile_registry_headings(self, ref_id: int, role: str) -> None:
        """Invariant: at most one registry heading per role per draft. If
        several carry ``meta.registry == role``, keep the earliest-pos one
        canonical, reparent every other's children under it, retire the
        emptied duplicates."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, handle FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'heading' AND retired_at IS NULL "
                "AND pos IS NOT NULL AND meta->>'registry' = %s "
                "ORDER BY pos",
                (ref_id, role),
            ).fetchall()
        if len(rows) < 2:
            return  # the common path — one home, nothing to fold
        canonical_id = int(rows[0][0])
        canonical = handle_registry.format_handle("draft", canonical_id, chunk=True)
        # ``move_chunk``/``retire_chunk`` key on the legacy ``chunks.handle``
        # (via ``_row``), so the *acting* handle must be legacy; the ``into``
        # target resolves through ``get_draft_chunk`` which accepts ``dc…``.
        for _dup_id, dup_handle in rows[1:]:
            with self.pool.connection() as conn:
                kids = self._children(conn, ref_id, int(_dup_id))
            # Move each child under the canonical home (its subtree follows).
            for kid in kids:
                self.move_chunk(
                    kid.handle,
                    {"into": canonical, "last": True},
                    source={"reconcile": f"registry:{role}"},
                )
            # Retire the now-empty duplicate heading (legacy handle).
            self.retire_chunk(str(dup_handle), source={"reconcile": f"registry:{role}"})

    def parts_callout_map(self, ref_id: int, role: str = "parts") -> dict[str, int]:
        """``{normalized dc-handle: numeral}`` for an ``assign="render"``
        registry. Numerals are **display labels derived from reading-order
        position**, not stored — inserting/reordering a leaf renumbers the
        whole series. Empty for a non-render registry."""
        from precis.draft import registry as _reg

        policy = _reg.policy_for(role)
        if policy.assign != "render":
            return {}
        ordered = [
            c.dc
            for c in self.reading_order(ref_id)
            if c.chunk_kind == "term" and (c.meta or {}).get("registry") == role
        ]
        norm = {
            handle_registry.normalize(h): n
            for h, n in _reg.render_callouts(ordered, policy).items()
        }
        return norm

    def undefined_abbrevs(self, ref_id: int, text: str) -> list[str]:
        """Acronym-shaped tokens in ``text`` not yet defined for this draft —
        no ``term`` chunk ``short``, no inline ``Long Form (ABBR)``
        anywhere in the prose, not on ``meta.abbrev_ignore``. The
        write-hint's complaint set; opus then defines or marks
        not-an-abbrev."""
        from precis.utils.abbreviations import find as _sh_find
        from precis.utils.abbreviations import find_acronyms as _find_acronyms

        cand = _find_acronyms(text)
        if not cand:
            return []
        known: set[str] = set()
        with self.pool.connection() as conn:
            for (short,) in conn.execute(
                "SELECT meta->>'short' FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'term' AND retired_at IS NULL",
                (ref_id,),
            ).fetchall():
                if short:
                    known.add(short)
            mrow = conn.execute(
                "SELECT meta->'abbrev_ignore' FROM refs WHERE ref_id = %s",
                (ref_id,),
            ).fetchone()
            if mrow and mrow[0]:
                known |= {str(t) for t in mrow[0]}
            prow = conn.execute(
                "SELECT string_agg(text, ' ') FROM chunks WHERE ref_id = %s "
                "AND ord >= 0 AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
        if prow and prow[0]:
            known |= set(_sh_find(prow[0]).keys())
        return sorted(cand - known)

    def defined_abbrevs(self, ref_id: int) -> dict[str, str]:
        """``{short: long}`` for every abbreviation **defined** in this
        draft — explicit ``term`` chunks (``meta.short`` → text) plus inline
        ``Long Form (ABBR)`` first-uses (Schwartz-Hearst). Explicit terms
        win on a clash. Drives the reader's hover-definition highlight."""
        from precis.utils.abbreviations import find as _sh_find

        out: dict[str, str] = {}
        with self.pool.connection() as conn:
            prow = conn.execute(
                "SELECT string_agg(text, ' ') FROM chunks WHERE ref_id = %s "
                "AND ord >= 0 AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
            # Inline pairs first; explicit term chunks overwrite them. The
            # Schwartz-Hearst scan is regex over the *whole* concatenated
            # prose — multi-second on a huge draft (1M+ chars). Above the
            # cap, skip it: the abbreviation highlight is a reader nicety,
            # and explicit ``term`` chunks (below) still give the glossary.
            if prow and prow[0] and len(prow[0]) <= _ABBREV_INLINE_SCAN_CAP:
                out.update(_sh_find(prow[0]))
            for short, long in conn.execute(
                "SELECT meta->>'short', text FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'term' AND retired_at IS NULL",
                (ref_id,),
            ).fetchall():
                if short and (long or "").strip():
                    out[str(short)] = str(long).strip()
        return out

    def defined_terms(self, ref_id: int) -> dict[str, Any]:
        """Rich per-**surface** hover records for every registry ``term``
        leaf — generalizes :meth:`defined_abbrevs`. ``{surface: TermEntry}``:
        a leaf is reachable under each string surface (``meta.short``, every
        ``meta.surface_forms``, ``meta.mpn``, ``meta.abbrev``), all mapping
        to the same record ``{definition, registry?, callout?, mpn?,
        manufacturer?, url?, ordering?, abbrev?}``. Inline ``Long Form
        (ABBR)`` first-uses contribute bare ``{definition}``; explicit
        ``term`` leaves win on a clash. Drives the reader's ``.pa-pop`` hover."""
        from precis.utils.abbreviations import find as _sh_find

        out: dict[str, dict[str, Any]] = {}
        with self.pool.connection() as conn:
            prow = conn.execute(
                "SELECT string_agg(text, ' ') FROM chunks WHERE ref_id = %s "
                "AND ord >= 0 AND retired_at IS NULL",
                (ref_id,),
            ).fetchone()
            # Inline pairs first; explicit term leaves overwrite them (same
            # precedence + huge-draft cap as ``defined_abbrevs``).
            if prow and prow[0] and len(prow[0]) <= _ABBREV_INLINE_SCAN_CAP:
                for short, long in _sh_find(prow[0]).items():
                    if short and (long or "").strip():
                        out[str(short)] = {"definition": str(long).strip()}
            for meta, text in conn.execute(
                "SELECT meta, text FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'term' AND retired_at IS NULL",
                (ref_id,),
            ).fetchall():
                definition = (text or "").strip()
                if not definition:
                    continue
                m = meta or {}
                entry: dict[str, Any] = {"definition": definition}
                for key in (
                    "registry",
                    "callout",
                    "mpn",
                    "manufacturer",
                    "url",
                    "ordering",
                    "abbrev",
                ):
                    val = m.get(key)
                    if val is not None and val != "":
                        entry[key] = val
                # Every string surface of the leaf routes to the same record.
                surfaces: list[str] = []
                short_form = m.get("short")
                if short_form:
                    surfaces.append(str(short_form))
                for sf in m.get("surface_forms") or []:
                    if sf:
                        surfaces.append(str(sf))
                mpn = m.get("mpn")
                if mpn:
                    surfaces.append(str(mpn))
                # A dedicated acronym surface (gripe 56690), parallel to
                # ``short`` — lets a term's primary label be the long form
                # while its abbreviation still hover-resolves in prose.
                abbrev = m.get("abbrev")
                if abbrev:
                    surfaces.append(str(abbrev))
                for surface in surfaces:
                    out[surface] = entry
        return out

    def registry_callouts(self, ref_id: int, role: str) -> list[int]:
        """The assigned ``meta.callout`` values for a registry's live ``term``
        leaves — the input to the next ``assign="insert"`` callout."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT (meta->>'callout')::int FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'term' AND retired_at IS NULL "
                "AND meta->>'registry' = %s AND meta ? 'callout'",
                (ref_id, role),
            ).fetchall()
        return [int(r[0]) for r in rows if r[0] is not None]

    def add_abbrev_ignore(self, ref_id: int, tokens: list[str]) -> None:
        """Add ``tokens`` to ``refs.meta.abbrev_ignore`` (deduped) — the
        LLM's "not an abbreviation" silence valve."""
        clean = [str(t).strip() for t in (tokens or []) if str(t).strip()]
        if not clean:
            return
        with self.tx() as conn:
            row = conn.execute(
                "SELECT meta->'abbrev_ignore' FROM refs WHERE ref_id = %s",
                (ref_id,),
            ).fetchone()
            existing = list(row[0]) if row and row[0] else []
            merged = sorted({*existing, *clean})
            conn.execute(
                "UPDATE refs SET meta = jsonb_set(meta, '{abbrev_ignore}', "
                "%s::jsonb, true) WHERE ref_id = %s",
                (Jsonb(merged), ref_id),
            )


class DraftStore(_AbbrevMixin):
    """Composed sub-store for draft chunk ops — ``store.drafts``. Holds
    the shared :class:`~precis.store.core.StoreCore` (pool/tx lifecycle)
    rather than its own pool, plus a back-reference to the host
    :class:`Store` for ops crossing into refs/links (``insert_ref``,
    ``add_link``, ``resolve_handle``)."""

    def __init__(self, core: StoreCore, *, host: Store) -> None:
        self._core = core
        self._host = host

    @property
    def pool(self) -> ConnectionPool:
        return self._core.pool

    def tx(self) -> AbstractContextManager[psycopg.Connection]:
        return self._core.tx()

    @cached_property
    def review(self) -> DraftReviewStore:
        """The chunk-review ledger domain — same carve pattern as
        :class:`DraftStore` off ``Store`` (module docstring). Cached: every
        access returns the same instance."""
        return DraftReviewStore(self._core, host=self)

    def _lock_sections(
        self, conn: psycopg.Connection, ref_id: int, *parents: int | None
    ) -> None:
        """Serialize structural draft ops per (ref, section) — a section is
        identified by its parent heading chunk (``None`` = top level).
        Acquires a ``pg_advisory_xact_lock`` per distinct section touched,
        in sorted key order (deadlock-free against e.g. a racing A→B/B→A
        move); held to transaction end. Call only from inside an open
        ``self.tx()``.

        Deliberately section-scoped, not per-ref: unrelated sections of the
        same draft still parallelize (gr176088). Key is a single bigint via
        ``hashtextextended('draft-section:<ref_id>:<parent_or_0>', 0)`` —
        avoids the ``(int4, int4)`` overload's overflow hazard on chunk ids.

        Accepted gap (TOCTOU): the lock is taken *after* the caller
        resolves which section(s) an op touches, so a concurrent move
        landing in between can still skew an adjacency pos-key —
        fractional keys tolerate that; the guarantee is narrower: two
        *structural* writers targeting the same section serialize against
        each other. ``retire_chunk``'s ``cascade``/``promote`` locks only
        the retired chunk + its immediate parent, not deeper descendant
        sections the cascade also touches.
        """
        for parent_key in sorted({p or 0 for p in parents}):
            conn.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('draft-section:' || %s || ':' || %s, 0))",
                (ref_id, parent_key),
            )

    # -- low-level inserts ---------------------------------------------------

    def _insert_draft_chunk(
        self,
        conn: psycopg.Connection,
        *,
        ref_id: int,
        chunk_kind: str,
        text: str,
        parent_chunk_id: int | None,
        pos: str,
        meta: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        kind: str = "draft",
    ) -> DraftChunk:
        """Insert one draft chunk: mint a unique handle (savepoint-retry),
        assign insertion-serial `ord`, set pos/parent/content_sha/meta, log
        a `created` event. ``meta`` e.g. a ``term``'s ``{short, long,
        surface_forms}`` or a ``figure``'s provenance."""
        sha = content_sha(text)
        meta = dict(meta or {})
        last_exc: Exception | None = None
        for _ in range(_HANDLE_RETRIES):
            handle = new_handle()
            try:
                with conn.transaction():  # savepoint
                    row = conn.execute(
                        """
                        INSERT INTO chunks
                            (ref_id, set_by, ord, chunk_kind, text,
                             handle, pos, parent_chunk_id, content_sha, meta)
                        VALUES (%s, 'agent',
                            (SELECT COALESCE(MAX(ord), -1) + 1
                               FROM chunks WHERE ref_id = %s),
                            %s, %s, %s, %s, %s, %s, %s)
                        RETURNING chunk_id
                        """,
                        (
                            ref_id,
                            ref_id,
                            chunk_kind,
                            text,
                            handle,
                            pos,
                            parent_chunk_id,
                            sha,
                            Jsonb(meta),
                        ),
                    ).fetchone()
                break
            except psycopg.errors.UniqueViolation as exc:
                last_exc = exc
                continue
        else:  # pragma: no cover - astronomically unlikely
            raise RuntimeError(
                f"could not mint a unique handle in {_HANDLE_RETRIES} tries"
            ) from last_exc

        assert row is not None
        chunk_id = int(row[0])
        conn.execute(
            """
            INSERT INTO chunk_events
                (chunk_id, event_kind, content_sha, source)
            VALUES (%s, 'created', %s, %s)
            """,
            (chunk_id, sha, Jsonb(source or {})),
        )
        return DraftChunk(
            chunk_id=chunk_id,
            ref_id=ref_id,
            handle=handle,
            chunk_kind=chunk_kind,
            text=text,
            pos=pos,
            parent_chunk_id=parent_chunk_id,
            depth=0,
            meta=meta,
            kind=kind,
        )

    # -- lookups -------------------------------------------------------------

    def draft_subtree_chunk_ids(self, handle: str) -> list[int]:
        """Chunk ids of the subtree rooted at ``handle`` — the chunk
        itself plus all live descendants. Empty when the handle is
        unknown. Used to scope draft search to one section."""
        chunk = self.get_draft_chunk(handle)
        if chunk is None:
            return []
        return [chunk.chunk_id, *self.descendant_chunk_ids(chunk.chunk_id)]

    def descendant_chunk_ids(self, chunk_id: int) -> list[int]:
        """Live descendant chunk ids of ``chunk_id`` (excluding itself) —
        the walk :meth:`draft_subtree_chunk_ids` uses internally, exposed
        standalone for a caller that already holds the chunk (skips a
        redundant :meth:`get_draft_chunk` round trip; e.g. the ``exclude=``
        cite-closure resolver, ``precis.handlers._exclude_closure``)."""
        with self.pool.connection() as conn:
            return self._descendant_ids(conn, chunk_id)

    def draft_term_shorts(self, ref_id: int) -> set[str]:
        """The ``short`` of every live glossary ``term`` chunk in the
        draft — used to tell an inline-only abbreviation from one already
        promoted to the glossary."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT meta->>'short' FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'term' AND retired_at IS NULL",
                (ref_id,),
            ).fetchall()
        return {r[0] for r in rows if r[0]}

    def draft_terms(self, ref_id: int) -> dict[str, tuple[str, str]]:
        """``handle → (short, long)`` for live glossary ``term`` chunks —
        the ``short`` lives in ``meta`` (not exposed on ``DraftChunk``),
        so exporters fetch it here to render "SHORT — long"."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT handle, meta->>'short', text FROM chunks "
                "WHERE ref_id = %s AND chunk_kind = 'term' AND retired_at IS NULL",
                (ref_id,),
            ).fetchall()
        return {str(r[0]): (str(r[1] or ""), str(r[2] or "")) for r in rows}

    def draft_chunk_meta(self, handle: str) -> dict[str, Any]:
        """The raw ``chunks.meta`` JSON for a draft chunk (``{}`` if none).
        Not on :class:`DraftChunk` — read it when re-deriving a table's
        markdown from its canonical ``meta.table``."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM chunks WHERE handle = %s", (_bare(handle),)
            ).fetchone()
        return dict(row[0]) if row and row[0] else {}

    def soft_delete_draft(self, ref_id: int) -> int:
        """Soft-delete a whole draft **atomically**: mark the ref
        ``deleted_at`` and retire all live chunks, one transaction.
        Recoverable (clear ``deleted_at``+``retired_at`` to restore).
        Returns chunks retired. Raises if the ref isn't a live draft."""
        with self.tx() as conn:
            rc = conn.execute(
                "UPDATE refs SET deleted_at = now() "
                "WHERE ref_id = %s AND kind = 'draft' AND deleted_at IS NULL",
                (ref_id,),
            ).rowcount
            if rc == 0:
                raise BadInput(f"no live draft ref id={ref_id}")
            chunks = conn.execute(
                "UPDATE chunks SET retired_at = now() "
                "WHERE ref_id = %s AND retired_at IS NULL",
                (ref_id,),
            ).rowcount
        return int(chunks)

    def universal_chunk(self, handle: str) -> dict[str, Any] | None:
        """Resolve ANY universal *chunk* handle (``pc123`` paper chunk,
        ``lc..`` plaintext, ``mc..`` markdown, …) to owning ref + position +
        text — cross-kind generalisation of ``get_draft_chunk`` for the
        reader's hover-preview/click-through. ``{kind, ref_id, ord,
        chunk_kind, text}`` or ``None`` (not a chunk handle, or gone — a
        dangling ``pc999`` degrades to a 'missing' popover)."""
        parsed = handle_registry.parse(handle.strip())
        if parsed is None or not parsed[1]:  # not a chunk handle
            return None
        kind, _is_chunk, chunk_id = parsed
        if kind == "cad":
            # cad node handles (ca<id>) live in cad_nodes, not chunks —
            # read via get(view=…), not this hover.
            return None
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id, ord, chunk_kind, text FROM chunks WHERE chunk_id = %s",
                (chunk_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "kind": kind,
            "ref_id": int(row[0]),
            "ord": row[1],
            "chunk_kind": row[2],
            "text": row[3] or "",
        }

    def universal_chunks(self, handles: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Bulk twin of :meth:`universal_chunk` — resolves many chunk
        handles in one query instead of one per handle (the Claims-rail
        renderer was paying one query per distinct grounding chunk across
        every hub). The numeric id a handle decodes to IS the ``chunk_id``
        regardless of kind code, so one ``ANY(%s)`` query resolves them
        all; a malformed handle or ``cad`` (outside ``chunks`` — see
        :meth:`universal_chunk`) is simply absent from the result."""
        by_chunk_id: dict[int, list[str]] = {}
        for h in handles:
            parsed = handle_registry.parse(h.strip())
            if parsed is None or not parsed[1] or parsed[0] == "cad":
                continue
            by_chunk_id.setdefault(parsed[2], []).append(h)
        if not by_chunk_id:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, ref_id, ord, chunk_kind, text FROM chunks "
                "WHERE chunk_id = ANY(%s)",
                (list(by_chunk_id),),
            ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for chunk_id, ref_id, ord_, chunk_kind, text in rows:
            for h in by_chunk_id.get(int(chunk_id), []):
                parsed = handle_registry.parse(h.strip())
                assert parsed is not None  # filtered above
                out[h] = {
                    "kind": parsed[0],
                    "ref_id": int(ref_id),
                    "ord": ord_,
                    "chunk_kind": chunk_kind,
                    "text": text or "",
                }
        return out

    def chunk_text_at(self, ref_id: int, ord: int) -> str | None:
        """The text of one chunk addressed by ``(ref_id, ord)`` — the
        classic ``paper:slug~n`` chunk address (``n`` = ``chunks.ord``). Used
        by the reMarkable footnote export to quote the referenced paper
        chunk. ``None`` when no such chunk exists."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord = %s",
                (ref_id, ord),
            ).fetchone()
        return (row[0] or "") if row is not None else None

    def get_draft_chunk(self, handle: str, *, kind: str = "draft") -> DraftChunk | None:
        """A single live-or-retired draft/plan chunk by its address.

        Accepts the universal handle (``dc<chunk_id>`` draft / ``pe<chunk_id>``
        plan — looked up by ``chunk_id``) or the legacy ``¶<base58>`` / bare
        base-58 anchor (looked up by ``chunks.handle``). ``kind`` sets the
        returned chunk's handle namespace (``pe<id>`` for a plan) and, for a
        universal-handle address, the code prefix that must match."""
        parsed = handle_registry.parse(handle.strip())
        key: str | int
        if parsed is not None and parsed[0] == kind and parsed[1]:
            where, key = "chunk_id = %s", parsed[2]
        else:
            where, key = "handle = %s", _bare(handle)
        with self.pool.connection() as conn:
            row = conn.execute(
                f"""SELECT chunk_id, handle, chunk_kind, text, pos,
                          parent_chunk_id, ref_id, meta, retired_at
                     FROM chunks WHERE {where}""",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return DraftChunk(
            chunk_id=row[0],
            ref_id=row[6],
            handle=row[1],
            chunk_kind=row[2],
            text=row[3],
            pos=row[4],
            parent_chunk_id=row[5],
            depth=0,
            meta=dict(row[7] or {}),
            kind=kind,
            retired=row[8] is not None,
        )

    def draft_relative_chunk_ids(
        self, handle: str, *, kind: str = "draft"
    ) -> list[int] | None:
        """Resolve a relative draft/plan handle to target chunk id(s).

        ``dc<id>^N`` walks ``N`` ancestors (``parent_chunk_id``);
        ``dc<id>+N``/``-N`` steps ``N`` siblings (ordered by ``pos`` under
        the same parent); ``dc<id>-lo..hi`` is the signed sibling span
        (reading-context window). Returns target ids (one for a
        step/ancestor, the contiguous run for a span), **empty list** when
        out of range/past the root, or ``None`` when ``handle`` isn't a
        relative handle of ``kind`` (caller then tries the absolute path).
        ``kind='plan'`` resolves ``pe<id>`` handles."""
        parsed = handle_registry.parse_relative(handle)
        if parsed is None:
            return None
        parsed_kind, _is_chunk, chunk_id, op = parsed
        if parsed_kind != kind:
            return None
        base = self.get_draft_chunk(
            handle_registry.format_handle(kind, chunk_id, chunk=True), kind=kind
        )
        if base is None:
            return []
        op_kind, *rest = op
        with self.pool.connection() as conn:
            if op_kind == "ancestor":
                (n,) = rest
                cur = base.chunk_id
                for _ in range(n):
                    row = conn.execute(
                        "SELECT parent_chunk_id FROM chunks WHERE chunk_id = %s",
                        (cur,),
                    ).fetchone()
                    if row is None or row[0] is None:
                        return []  # climbed past the document root
                    cur = int(row[0])
                return [cur]
            siblings = self._children(conn, base.ref_id, base.parent_chunk_id)
        idx = next(
            (i for i, c in enumerate(siblings) if c.chunk_id == base.chunk_id), None
        )
        if idx is None:
            return []
        if op_kind == "step":
            (n,) = rest
            target = idx + n
            return [siblings[target].chunk_id] if 0 <= target < len(siblings) else []
        # span: signed offsets around the anchor, clamped to the sibling run.
        lo_off, hi_off = rest
        lo = max(0, idx + lo_off)
        hi = min(len(siblings) - 1, idx + hi_off)
        if lo > hi:
            return []
        return [siblings[i].chunk_id for i in range(lo, hi + 1)]

    def _children(
        self,
        conn: psycopg.Connection,
        ref_id: int,
        parent_chunk_id: int | None,
        *,
        kind: str = "draft",
    ) -> list[DraftChunk]:
        """Live children of a parent (NULL = roots), ordered by pos."""
        rows = conn.execute(
            """SELECT chunk_id, handle, chunk_kind, text, pos, parent_chunk_id
                 FROM chunks
                WHERE ref_id = %s
                  AND parent_chunk_id IS NOT DISTINCT FROM %s
                  AND retired_at IS NULL AND pos IS NOT NULL
                ORDER BY pos COLLATE "C" ASC""",
            (ref_id, parent_chunk_id),
        ).fetchall()
        return [
            DraftChunk(
                chunk_id=r[0],
                ref_id=ref_id,
                handle=r[1],
                chunk_kind=r[2],
                text=r[3],
                pos=r[4],
                parent_chunk_id=r[5],
                depth=0,
                kind=kind,
            )
            for r in rows
        ]

    def reading_order(self, ref_id: int, *, kind: str = "draft") -> list[DraftChunk]:
        """All live chunks of a draft in DFS reading order (roots by pos,
        recurse into children by pos), with depth.

        Built in Python from one **flat, indexed** fetch, not a recursive
        SQL CTE — the CTE's worktable join couldn't index-seek, re-scanning
        every chunk of the ref per recursion level (≈O(N·depth), ~5.5s on a
        9,700-chunk draft). A single ``chunks_ref_id_idx`` scan + this DFS
        is milliseconds.

        Ordering matches the old ``sort_path COLLATE "C"``: siblings sort by
        ``pos`` (base-62 fractional keys, byte order), DFS pre-order puts a
        parent before its subtree and a subtree before the next sibling.
        Chunks reachable only through a retired/absent parent are excluded,
        as the CTE also only walked live chunks from a NULL-parent root."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, handle, chunk_kind, text, pos, "
                "       parent_chunk_id, meta "
                "  FROM chunks "
                " WHERE ref_id = %s AND retired_at IS NULL AND pos IS NOT NULL",
                (ref_id,),
            ).fetchall()
        # children keyed by parent_chunk_id (None = root). A child whose
        # parent isn't a live chunk lands in a bucket no walk ever visits,
        # so it (and its subtree) drop out — matching the old CTE.
        children: dict[Any, list[Any]] = {}
        for r in rows:
            children.setdefault(r[5], []).append(r)
        for lst in children.values():
            lst.sort(key=lambda r: r[4])  # by pos, byte order
        out: list[DraftChunk] = []
        # iterative DFS pre-order; push siblings reversed so they pop ascending.
        stack: list[tuple[Any, int]] = [
            (r, 0) for r in reversed(children.get(None, []))
        ]
        while stack:
            r, depth = stack.pop()
            out.append(
                DraftChunk(
                    chunk_id=r[0],
                    ref_id=ref_id,
                    handle=r[1],
                    chunk_kind=r[2],
                    text=r[3],
                    pos=r[4],
                    parent_chunk_id=r[5],
                    depth=depth,
                    meta=dict(r[6] or {}),
                    kind=kind,
                )
            )
            kids = children.get(r[0])
            if kids:
                stack.extend((k, depth + 1) for k in reversed(kids))
        return out

    def chunk_ord_map(self, ref_id: int) -> dict[int, int]:
        """``chunk_id → ord`` for every live body chunk of ``ref_id``.

        The link layer addresses a chunk endpoint by ``ord``
        (``add_link(src_pos=…)`` translates ord→chunk_id), but
        :class:`DraftChunk` exposes only the fractional ``pos`` string. The
        draft autolinker needs each source chunk's ``ord`` to write a
        chunk-level ``cites`` edge, so this returns the mapping in one
        indexed scan. Card variants (``ord < 0``) are excluded — only real
        body chunks are link endpoints.
        """
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, ord FROM chunks "
                "WHERE ref_id = %s AND retired_at IS NULL AND ord >= 0",
                (ref_id,),
            ).fetchall()
        return {int(r[0]): int(r[1]) for r in rows}

    def chunk_connections(
        self, ref_id: int, handles: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Per-chunk graph connections — every ref linked *to or from* a
        chunk, grouped by handle (where ``derived-from`` provenance and
        dream-memories referencing a paragraph surface in the reader). Each
        entry: ``{relation, direction, kind, ident, title}`` (``ident`` =
        slug or numeric id). Deduped per (handle, other-ref, relation)."""
        if not handles:
            return {}
        sql = """
            SELECT c.handle, l.relation,
                   CASE WHEN l.src_chunk_id = c.chunk_id THEN 'out' ELSE 'in' END AS dir,
                   o.ref_id, o.kind,
                   (SELECT ri.id_value FROM ref_identifiers ri
                     WHERE ri.ref_id = o.ref_id AND ri.id_kind = 'cite_key'
                     LIMIT 1) AS slug,
                   o.title
              FROM chunks c
              JOIN links l
                ON l.src_chunk_id = c.chunk_id OR l.dst_chunk_id = c.chunk_id
              JOIN refs o
                ON o.ref_id = CASE WHEN l.src_chunk_id = c.chunk_id
                                   THEN l.dst_ref_id ELSE l.src_ref_id END
             WHERE c.ref_id = %s AND c.handle = ANY(%s)
               AND c.retired_at IS NULL AND o.deleted_at IS NULL
             ORDER BY c.handle, l.created_at
        """
        out: dict[str, list[dict[str, Any]]] = {}
        seen: set[tuple[str, int, str]] = set()
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (ref_id, handles)).fetchall()
        for handle, relation, direction, oid, kind, slug, title in rows:
            key = (handle, int(oid), relation)
            if key in seen:
                continue
            seen.add(key)
            out.setdefault(handle, []).append(
                {
                    "relation": relation,
                    "direction": direction,
                    "kind": kind,
                    "ident": slug or str(oid),
                    "title": (title or "").split("\n", 1)[0][:80],
                }
            )
        return out

    def ref_connections(self, ref_id: int) -> list[dict[str, Any]]:
        """Whole-ref graph connections — the other end of every ``links``
        row anchored at the REF (both chunk endpoints NULL), same
        ``{relation, direction, kind, ident, title}`` shape as
        :meth:`chunk_connections` (one chip renderer feeds off both): a
        draft's project (``draft-of``), what it ``cites``, and inbound
        ``raises-concern-about`` findings filed against it. Deduped per
        (other-ref, relation, direction); newest last, as stored."""
        sql = """
            SELECT l.relation,
                   CASE WHEN l.src_ref_id = %(rid)s THEN 'out' ELSE 'in' END AS dir,
                   o.ref_id, o.kind,
                   (SELECT ri.id_value FROM ref_identifiers ri
                     WHERE ri.ref_id = o.ref_id AND ri.id_kind = 'cite_key'
                     LIMIT 1) AS slug,
                   o.title
              FROM links l
              JOIN refs o
                ON o.ref_id = CASE WHEN l.src_ref_id = %(rid)s
                                   THEN l.dst_ref_id ELSE l.src_ref_id END
             WHERE (l.src_ref_id = %(rid)s OR l.dst_ref_id = %(rid)s)
               AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL
               AND o.deleted_at IS NULL AND o.ref_id <> %(rid)s
             ORDER BY l.created_at
        """
        out: list[dict[str, Any]] = []
        seen: set[tuple[int, str, str]] = set()
        with self.pool.connection() as conn:
            rows = conn.execute(sql, {"rid": ref_id}).fetchall()
        for relation, direction, oid, kind, slug, title in rows:
            key = (int(oid), relation, direction)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "relation": relation,
                    "direction": direction,
                    "kind": kind,
                    "ident": slug or str(oid),
                    "title": (title or "").split("\n", 1)[0][:80],
                }
            )
        return out

    def anchored_todos(self, handles: list[str]) -> dict[str, list[dict[str, Any]]]:
        """ALL change-request todos anchored at each chunk (``meta.anchor =
        '¶<handle>'`` or the newer bare ``dc<id>``), grouped by (bare)
        handle — including **done/won't-do**, so a finished request stays
        clickable (its ``plan_tick`` job's LLM transcript is the debugging
        surface). Active requests sort first. ``started``+``done``+``failed``
        drive the close-X: shown on not-yet-started/done/failed, suppressed
        only while actively running.

        An anchored todo is **not a `links` row** (gripe 178766) — invisible
        to the fisheye ring and the graph-connection surface unless read
        back through ``meta.anchor``. Shared by
        :func:`precis_web.routes.drafts._requests_by_handle` (thin wrapper)
        and :func:`precis_web.draft_links.chunk_links`'s ``flags`` — the
        one data path both draft readers assemble from."""
        if not handles:
            return {}
        # Match both the new bare ``dc<id>`` anchors and any legacy ``¶<handle>``
        # ones still stored (transition); the group key below normalises to bare.
        anchors = list(handles) + [f"¶{h}" for h in handles]
        sql = (
            "SELECT r.ref_id, r.title, r.meta->>'anchor' AS anchor, "
            "  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1) AS status, "
            "  EXISTS (SELECT 1 FROM refs j WHERE j.parent_id = r.ref_id "
            "          AND j.kind = 'job') AS started, "
            "  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN' "
            "      AND t.value LIKE 'ask-user:%%' LIMIT 1) AS asking, "
            "  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN' "
            "      AND t.value LIKE 'child-failed:%%' LIMIT 1) AS failed_tag, "
            # AUDIT:<category> — a content-QA audit stamps this on the anchored
            # change-request todo so the reader badges the chunk by category.
            "  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "    WHERE rt.ref_id = r.ref_id AND t.namespace = 'AUDIT' LIMIT 1) AS audit "
            "FROM refs r "
            "WHERE r.kind = 'todo' AND r.deleted_at IS NULL "
            "  AND r.meta->>'anchor' = ANY(%s)"
        )
        out: dict[str, list[dict[str, Any]]] = {}
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (anchors,)).fetchall()
        for ref_id, title, anchor, status, started, asking, failed_tag, audit in rows:
            status = status or "open"
            handle = (anchor or "").lstrip("¶")
            # ``OPEN:ask-user:<value>`` → the human question. The value is
            # either the literal question or a ``see-chunk-N`` redirect handle
            # for prose that overflowed the 80-char tag cap into a chunk;
            # resolve_ask_question reads the chunk back so we show the real
            # question, not the opaque "see chunk 0" slug. ``ask_tag`` keeps
            # the raw tag so the inline answer form can strip it on submit.
            ask_tag = asking or ""
            ask = (
                self.resolve_ask_question(ref_id, ask_tag.split("ask-user:", 1)[-1])
                if ask_tag
                else ""
            )
            # A failed child job parks the parent behind a ``child-failed:<id>``
            # bubble — surface *why* (its job_summary), not just "failed".
            fail_reason = ""
            fail_job_id: int | None = None
            if failed_tag:
                job_id = failed_tag.split("child-failed:", 1)[-1].strip()
                if job_id.isdigit():
                    fail_job_id = int(job_id)
                    fail_reason = self.job_fail_reason(fail_job_id) or ""
            out.setdefault(handle, []).append(
                {
                    "ref_id": ref_id,
                    "title": (title or "").split("\n", 1)[0][:60],
                    # Full first line of the original request, for the promoted
                    # "needs you" panel (the chip's title is truncated).
                    "request": (title or "").split("\n", 1)[0][:400],
                    "status": status,
                    "done": status in ("done", "won't-do"),
                    # "started" = a plan_tick (or other) job minted; the
                    # X-to-cancel only shows before that.
                    "started": bool(started),
                    # attention: waiting on the user, or a failed child job.
                    "asking": ask,
                    "ask_tag": ask_tag,
                    "failed": bool(failed_tag),
                    "fail_reason": fail_reason,
                    # The failed child job id — the ▶ restart button posts to it.
                    "fail_job_id": fail_job_id,
                    # AUDIT:<category> if this request came from a content-QA
                    # audit (missing-citation / empty-stub / …); '' otherwise.
                    "audit": audit or "",
                }
            )
        for reqs in out.values():
            reqs.sort(key=lambda r: _REQUEST_ORDER.get(r["status"], 9))
        return out

    # ---- element→chunk bindings ------------------------------
    #
    # A diagram (figure / mermaid) element is bound to the chunk it depicts
    # via a chunk-level ``depicts`` link from the diagram's SOURCE chunk to
    # the target chunk/ref. The element's stable source ``id=`` is the join
    # key; it lives in ``links.meta.elements`` (a set), NOT a column — the
    # links UNIQUE key is (src, dst, relation), so ONE row per edge carries
    # every element that anchors it. Kind-agnostic: any chunk that owns a
    # stable-id'd source can bind.

    @staticmethod
    def _binding_handle(kind: str, ref_id: int, chunk_id: int | None) -> str:
        """Canonical universal handle for a bind target — a chunk handle
        (``dc42``/``pc10``) when chunk-level, else the record handle
        (``me5``). Falls back to ``kind:id`` for a kind with no handle code."""
        try:
            if chunk_id is not None:
                return handle_registry.format_handle(kind, int(chunk_id), chunk=True)
            return handle_registry.format_handle(kind, int(ref_id))
        except Exception:
            return f"{kind}:{chunk_id if chunk_id is not None else ref_id}"

    def _ref_of_chunk(self, conn: psycopg.Connection, chunk_id: int) -> int:
        row = conn.execute(
            "SELECT ref_id FROM chunks WHERE chunk_id = %s AND retired_at IS NULL",
            (chunk_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no live source chunk {chunk_id} to bind from")
        return int(row[0])

    def bind_element(
        self,
        *,
        node_chunk_id: int,
        element: str,
        target: str,
        relation: str = "depicts",
        set_by: str = "agent",
        conn: psycopg.Connection | None = None,
    ) -> None:
        """Bind diagram element ``element`` (a stable source id) to
        ``target`` (a universal handle — ``dc…`` draft chunk, ``pc…`` paper
        chunk, ``me…`` memory, …). Idempotent: merged into the edge's
        ``meta.elements`` set, one link row per (source, target, relation)."""
        element = element.strip()
        if not element:
            raise BadInput("an element id is required to bind")

        def _do(c: psycopg.Connection) -> None:
            src_ref = self._ref_of_chunk(c, node_chunk_id)
            rh = self._host.resolve_handle(target, conn=c)
            if rh is None:
                raise BadInput(
                    f"cannot resolve bind target {target!r}",
                    next="pass a live universal handle (dc…/pc…/me…/pa…)",
                )
            dst_ref, dst_chunk = rh.ref_id, rh.chunk_id
            if src_ref == dst_ref and node_chunk_id == (dst_chunk or -1):
                raise BadInput("an element cannot depict its own source chunk")
            row = c.execute(
                "SELECT link_id, meta FROM links "
                "WHERE src_ref_id = %s AND src_chunk_id = %s "
                "  AND dst_ref_id = %s AND dst_chunk_id IS NOT DISTINCT FROM %s "
                "  AND relation = %s",
                (src_ref, node_chunk_id, dst_ref, dst_chunk, relation),
            ).fetchone()
            if row is not None:
                link_id, meta = int(row[0]), (row[1] or {})
                elems = list(dict.fromkeys([*(meta.get("elements") or []), element]))
                c.execute(
                    "UPDATE links SET meta = jsonb_set(meta, '{elements}', %s) "
                    "WHERE link_id = %s",
                    (Jsonb(elems), link_id),
                )
            else:
                c.execute(
                    "INSERT INTO links "
                    "  (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, "
                    "   relation, set_by, meta) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        src_ref,
                        node_chunk_id,
                        dst_ref,
                        dst_chunk,
                        relation,
                        set_by,
                        Jsonb({"elements": [element]}),
                    ),
                )

        if conn is not None:
            _do(conn)
        else:
            with self.pool.connection() as c:
                _do(c)

    def unbind_element(
        self,
        *,
        node_chunk_id: int,
        element: str,
        target: str | None = None,
        relation: str = "depicts",
        conn: psycopg.Connection | None = None,
    ) -> int:
        """Remove ``element`` from the node's bindings. With ``target`` given,
        only that edge; otherwise every ``relation`` edge of the node. A row
        whose element set empties is deleted. Returns the number of edges
        touched."""
        element = element.strip()

        def _do(c: psycopg.Connection) -> int:
            src_ref = self._ref_of_chunk(c, node_chunk_id)
            filt = "src_ref_id = %s AND src_chunk_id = %s AND relation = %s"
            params: list[Any] = [src_ref, node_chunk_id, relation]
            if target is not None:
                rh = self._host.resolve_handle(target, conn=c)
                if rh is None:
                    raise BadInput(f"cannot resolve unbind target {target!r}")
                filt += " AND dst_ref_id = %s AND dst_chunk_id IS NOT DISTINCT FROM %s"
                params += [rh.ref_id, rh.chunk_id]
            rows = c.execute(
                f"SELECT link_id, meta FROM links WHERE {filt}", params
            ).fetchall()
            touched = 0
            for link_id, meta in rows:
                elems = list((meta or {}).get("elements") or [])
                if element not in elems:
                    continue
                touched += 1
                elems = [e for e in elems if e != element]
                if elems:
                    c.execute(
                        "UPDATE links SET meta = jsonb_set(meta, '{elements}', %s) "
                        "WHERE link_id = %s",
                        (Jsonb(elems), int(link_id)),
                    )
                else:
                    c.execute("DELETE FROM links WHERE link_id = %s", (int(link_id),))
            return touched

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def element_bindings(self, node_chunk_id: int) -> list[dict[str, Any]]:
        """Every element→target binding anchored on diagram source chunk
        ``node_chunk_id`` — one entry per (element, target), exploding
        ``meta.elements``. Each: ``{element, relation, kind, ident, handle,
        chunk_id, title}``. Drives the prepared-context assembler (slice 2)."""
        sql = """
            SELECT l.meta, l.relation, o.ref_id, o.kind, l.dst_chunk_id,
                   (SELECT ri.id_value FROM ref_identifiers ri
                     WHERE ri.ref_id = o.ref_id AND ri.id_kind = 'cite_key'
                     LIMIT 1) AS slug,
                   o.title
              FROM links l
              JOIN refs o ON o.ref_id = l.dst_ref_id
             WHERE l.src_chunk_id = %s AND l.relation = 'depicts'
               AND o.deleted_at IS NULL
             ORDER BY l.created_at
        """
        out: list[dict[str, Any]] = []
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (node_chunk_id,)).fetchall()
        for meta, relation, oid, kind, dst_chunk, slug, title in rows:
            handle = self._binding_handle(
                str(kind), int(oid), int(dst_chunk) if dst_chunk is not None else None
            )
            for element in (meta or {}).get("elements") or []:
                out.append(
                    {
                        "element": element,
                        "relation": relation,
                        "kind": kind,
                        "ident": slug or str(oid),
                        "handle": handle,
                        "chunk_id": int(dst_chunk) if dst_chunk is not None else None,
                        "title": (title or "").split("\n", 1)[0][:80],
                    }
                )
        return out

    def set_element_bindings(
        self,
        *,
        node_chunk_id: int,
        desired: list[dict[str, Any]],
        set_by: str = "agent",
    ) -> dict[str, int]:
        """Reconcile a diagram source chunk's full binding set to
        ``desired`` (``{element, target, relation?}`` list — the turn
        loop's ``links`` array): adds missing, removes absent; unresolvable
        targets are skipped, never failing the turn. Returns ``{'added':
        n, 'removed': m}``. Empty ``desired`` clears all bindings; omit the
        call entirely to leave bindings untouched."""
        have: set[tuple[str, str, str]] = {
            (b["element"], b["handle"], b["relation"])
            for b in self.element_bindings(node_chunk_id)
        }
        want: set[tuple[str, str, str]] = set()
        added = removed = 0
        with self.pool.connection() as conn:
            for spec in desired:
                element = str(spec.get("element", "")).strip()
                target = str(spec.get("target", "")).strip()
                relation = str(spec.get("relation") or "depicts").strip()
                if not element or not target:
                    continue
                rh = self._host.resolve_handle(target, conn=conn)
                if rh is None:
                    continue  # skip unresolvable — don't fail the turn
                canon = self._binding_handle(rh.kind, rh.ref_id, rh.chunk_id)
                want.add((element, canon, relation))
            for element, canon, relation in want - have:
                self.bind_element(
                    node_chunk_id=node_chunk_id,
                    element=element,
                    target=canon,
                    relation=relation,
                    set_by=set_by,
                    conn=conn,
                )
                added += 1
            for element, canon, relation in have - want:
                self.unbind_element(
                    node_chunk_id=node_chunk_id,
                    element=element,
                    target=canon,
                    relation=relation,
                    conn=conn,
                )
                removed += 1
        return {"added": added, "removed": removed}

    def live_paper_cites(self, handles: set[str], slugs: set[str]) -> set[str]:
        """Citation tokens resolving to a **live paper we hold** — the draft
        reader's local-vs-external colouring signal. ``handles`` are
        universal handles (``pc10`` chunk, ``pa42624`` record); ``slugs``
        are ``§slug``/``paper:slug`` cite_keys. Returns the subset (handle
        or slug) pointing at a live ``kind='paper'`` ref; anything not
        returned is an **external reference**. One connection, batched by
        target table."""
        chunk_pks: dict[int, str] = {}
        record_pks: dict[int, str] = {}
        for h in handles:
            parsed = handle_registry.parse(h)
            if parsed is None or parsed[0] != "paper":
                continue
            _kind, is_chunk, pk = parsed
            (chunk_pks if is_chunk else record_pks)[pk] = handle_registry.normalize(h)
        slug_list = list(slugs)
        live: set[str] = set()
        if not (slug_list or chunk_pks or record_pks):
            return live
        with self.pool.connection() as conn:
            if slug_list:
                rows = conn.execute(
                    "SELECT ri.id_value FROM ref_identifiers ri "
                    "JOIN refs r ON r.ref_id = ri.ref_id "
                    "WHERE ri.id_kind = 'cite_key' AND r.kind = 'paper' "
                    "  AND r.deleted_at IS NULL AND ri.id_value = ANY(%s)",
                    (slug_list,),
                ).fetchall()
                live |= {str(v) for (v,) in rows}
            if chunk_pks:
                rows = conn.execute(
                    "SELECT c.chunk_id FROM chunks c "
                    "JOIN refs r ON r.ref_id = c.ref_id "
                    "WHERE r.kind = 'paper' AND r.deleted_at IS NULL "
                    "  AND c.retired_at IS NULL AND c.chunk_id = ANY(%s)",
                    (list(chunk_pks),),
                ).fetchall()
                live |= {chunk_pks[int(pk)] for (pk,) in rows}
            if record_pks:
                rows = conn.execute(
                    "SELECT r.ref_id FROM refs r "
                    "WHERE r.kind = 'paper' AND r.deleted_at IS NULL "
                    "  AND r.ref_id = ANY(%s)",
                    (list(record_pks),),
                ).fetchall()
                live |= {record_pks[int(rid)] for (rid,) in rows}
        return live

    def chunk_edit_stats(
        self, ref_id: int, handles: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Per-chunk edit churn from ``chunk_events`` — ``{handle:
        {edits, last_at}}`` where ``edits`` counts ``edited`` events (the
        "changed Nx" chip) and ``last_at`` is the most recent event time.
        A chunk with only its ``created`` event has ``edits=0``."""
        if not handles:
            return {}
        sql = """
            SELECT c.handle,
                   count(*) FILTER (WHERE ce.event_kind = 'edited') AS edits,
                   max(ce.ts) AS last_at
              FROM chunks c
              JOIN chunk_events ce ON ce.chunk_id = c.chunk_id
             WHERE c.ref_id = %s AND c.handle = ANY(%s)
             GROUP BY c.handle
        """
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (ref_id, handles)).fetchall()
        return {
            h: {"edits": int(edits), "last_at": last_at} for h, edits, last_at in rows
        }

    def block_views(
        self, ref_id: int, handles: list[str] | None = None
    ) -> dict[str, dict[str, str]]:
        """Per-block ``{handle: {summary, keywords}}`` for a draft.
        ``summary`` = the ``llm-v1`` two-part summary (``chunk_summaries``);
        ``keywords`` = comma-joined KeyBERT terms (``chunks.keywords``,
        first 12); either is ``''`` before the ``llm_summarize``/
        ``chunk_keywords`` workers reach the chunk — callers fall back
        (summary → keywords → truncated text). Shared by the web reader's
        view slider and the outline render. ``handles`` scopes the result
        (the on-demand row path must not re-scan the whole draft per row);
        ``None`` = whole draft."""
        where = "c.ref_id = %s AND c.retired_at IS NULL AND c.pos IS NOT NULL AND c.ord >= 0"
        params: tuple[Any, ...] = (ref_id,)
        if handles is not None:
            if not handles:
                return {}
            where += " AND c.handle = ANY(%s)"
            params = (ref_id, handles)
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT c.handle, c.keywords,
                       (SELECT s.text FROM chunk_summaries s
                          WHERE s.chunk_id = c.chunk_id
                            AND s.summarizer = 'llm-v1' LIMIT 1) AS summary
                  FROM chunks c
                 WHERE {where}
                """,
                params,
            ).fetchall()
        return {
            handle: {
                "keywords": ", ".join((kws or [])[:12]),
                "summary": (summary or "").strip(),
            }
            for handle, kws, summary in rows
        }

    def draft_toc(
        self, ref_id: int, *, root_handle: str | None = None
    ) -> list[TocEntry]:
        """The heading-only DFS skeleton (the TOC) for a draft, or for the
        subtree under ``root_handle`` (TOC at any hierarchy level). Each
        heading carries its gist (``llm-v1`` summary) and keywords when a
        worker has produced them; fresh drafts just show titles."""
        root_id: int | None = None
        if root_handle is not None:
            head = self.get_draft_chunk(root_handle)
            if head is None:
                raise ValueError(f"toc: unknown heading {root_handle!r}")
            root_id = head.chunk_id
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                -- Headings form their own tree (only headings have
                -- children), so walk chunk_kind='heading' by parent.
                WITH RECURSIVE h AS (
                    SELECT chunk_id, handle, text, keywords, pos,
                           pos AS sort_path, 0 AS depth
                      FROM chunks
                     WHERE ref_id = %s AND chunk_kind = 'heading'
                       AND retired_at IS NULL AND pos IS NOT NULL
                       AND parent_chunk_id IS NOT DISTINCT FROM %s
                    UNION ALL
                    SELECT c.chunk_id, c.handle, c.text, c.keywords, c.pos,
                           h.sort_path || '/' || c.pos, h.depth + 1
                      FROM chunks c JOIN h ON c.parent_chunk_id = h.chunk_id
                     WHERE c.chunk_kind = 'heading' AND c.retired_at IS NULL
                       AND c.pos IS NOT NULL
                )
                SELECT h.handle, h.depth, h.text, h.keywords,
                       (SELECT s.text FROM chunk_summaries s
                         WHERE s.chunk_id = h.chunk_id
                           AND s.summarizer = 'llm-v1' LIMIT 1) AS gist,
                       h.chunk_id
                  FROM h ORDER BY h.sort_path COLLATE "C" ASC
                """,
                (ref_id, root_id),
            ).fetchall()
        return [
            TocEntry(
                handle=r[0],
                depth=r[1],
                title=r[2],
                keywords=list(r[3] or []),
                gist=r[4],
                chunk_id=int(r[5]),
            )
            for r in rows
        ]

    # -- position resolution -------------------------------------------------

    @staticmethod
    def _ghost_bracket(
        sibs: list[DraftChunk], tgt: DraftChunk, *, before: bool
    ) -> tuple[str | None, str | None]:
        """(lo, hi) for inserting relative to a *retired* anchor (gripe
        49153). ``get_draft_chunk`` returns live-or-retired, but
        ``_children`` yields only live siblings, so a retired anchor is
        absent from ``sibs`` and a plain index lookup would raise. A
        retired chunk keeps its ``pos``, so it still names a valid slot:
        bracket the live siblings by position — ``before`` → (live-pred,
        ghost.pos]; else [ghost.pos, live-succ). Keeps a stale handle
        working instead of a 500 (reachable only by a caller holding an
        old handle — search never hands one out). Comparison matches
        ``_children``'s ``ORDER BY pos COLLATE "C"`` (byte order).
        """
        if before:
            lo = max((s.pos for s in sibs if s.pos < tgt.pos), default=None)
            return lo, tgt.pos
        hi = min((s.pos for s in sibs if s.pos > tgt.pos), default=None)
        return tgt.pos, hi

    def _resolve_at(
        self,
        conn: psycopg.Connection,
        ref_id: int,
        at: dict[str, Any] | None,
    ) -> tuple[int | None, str | None, str | None]:
        """Resolve an `at` intent → (parent_chunk_id, lo_pos, hi_pos).
        New chunks get fractional keys strictly between lo and hi."""
        at = at or {}
        anchor = at.get("before") or at.get("after")
        if anchor is not None:
            tgt = self.get_draft_chunk(_bare(anchor))
            if tgt is None:
                raise NotFound(f"at: unknown chunk handle {anchor!r}")
            sibs = self._children(conn, ref_id, tgt.parent_chunk_id)
            idx = next(
                (i for i, s in enumerate(sibs) if s.chunk_id == tgt.chunk_id), None
            )
            if idx is None:  # anchor retired → recover into its ghost slot
                lo, hi = self._ghost_bracket(sibs, tgt, before="before" in at)
                return tgt.parent_chunk_id, lo, hi
            if "before" in at:
                lo = sibs[idx - 1].pos if idx > 0 else None
                hi = tgt.pos
            else:
                lo = tgt.pos
                hi = sibs[idx + 1].pos if idx + 1 < len(sibs) else None
            return tgt.parent_chunk_id, lo, hi

        into = at.get("into")
        if into is not None:
            parent = self.get_draft_chunk(_bare(into))
            if parent is None:
                raise NotFound(f"at: unknown parent handle {into!r}")
            kids = self._children(conn, ref_id, parent.chunk_id)
            if at.get("first"):
                return parent.chunk_id, None, (kids[0].pos if kids else None)
            return parent.chunk_id, (kids[-1].pos if kids else None), None

        roots = self._children(conn, ref_id, None)
        if at.get("first"):
            return None, None, (roots[0].pos if roots else None)
        return None, (roots[-1].pos if roots else None), None

    # -- create / add --------------------------------------------------------

    def draft_attached_work(
        self, draft_ref_id: int, *, limit: int = 20
    ) -> list[DraftWorkItem]:
        """Open todos working on this draft, blocked-first, capped. Walks
        ``draft → (draft-of) → project root → todo subtree`` for open
        todos that are *blocked* (``OPEN:child-failed:*`` bubble) or have
        a non-succeeded child job (running/queued/failed) — so a failed
        enrichment job registers on the draft instead of silently parking
        out of rotation. Clean, fully-done work is omitted."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                WITH RECURSIVE proj AS (
                    SELECT dst_ref_id AS pid FROM links
                     WHERE src_ref_id = %(draft)s AND relation = 'draft-of'
                     LIMIT 1
                ),
                subtree AS (
                    SELECT r.ref_id FROM refs r JOIN proj ON r.ref_id = proj.pid
                    UNION ALL
                    SELECT c.ref_id FROM refs c
                      JOIN subtree s ON c.parent_id = s.ref_id
                     WHERE c.kind = 'todo' AND c.deleted_at IS NULL
                ),
                open_todos AS (
                    SELECT r.ref_id, r.title
                      FROM refs r
                      JOIN subtree s ON s.ref_id = r.ref_id
                      JOIN ref_tags rt ON rt.ref_id = r.ref_id
                      JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE r.kind = 'todo' AND r.deleted_at IS NULL
                       AND t.namespace = 'STATUS' AND t.value = 'open'
                ),
                bubbles AS (
                    SELECT rt.ref_id, count(*) AS n
                      FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE t.namespace = 'OPEN' AND t.value LIKE 'child-failed:%%'
                     GROUP BY rt.ref_id
                ),
                jobs AS (
                    SELECT j.parent_id AS todo_id, j.ref_id AS job_id,
                           COALESCE(t.value, 'queued') AS status
                      FROM refs j
                      LEFT JOIN ref_tags rt ON rt.ref_id = j.ref_id
                      LEFT JOIN tags t
                        ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
                     WHERE j.kind = 'job' AND j.deleted_at IS NULL
                ),
                asks AS (
                    -- ``ask-user[:question]`` tags: work waiting on a human.
                    SELECT rt.ref_id,
                           array_agg(t.value ORDER BY t.value) AS ask_tags
                      FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE t.namespace = 'OPEN'
                       AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')
                     GROUP BY rt.ref_id
                )
                SELECT o.ref_id, o.title,
                       (b.n IS NOT NULL) AS blocked,
                       COALESCE(
                           jsonb_agg(
                               jsonb_build_array(jb.job_id, jb.status)
                               ORDER BY jb.job_id
                           ) FILTER (WHERE jb.job_id IS NOT NULL),
                           '[]'::jsonb
                       ) AS jobs,
                       a.ask_tags
                  FROM open_todos o
                  LEFT JOIN bubbles b ON b.ref_id = o.ref_id
                  LEFT JOIN jobs jb ON jb.todo_id = o.ref_id
                  LEFT JOIN asks a ON a.ref_id = o.ref_id
                 GROUP BY o.ref_id, o.title, b.n, a.ask_tags
                HAVING b.n IS NOT NULL
                    OR a.ask_tags IS NOT NULL
                    OR bool_or(jb.status IN ('running', 'queued', 'failed'))
                 ORDER BY (b.n IS NOT NULL OR a.ask_tags IS NOT NULL) DESC,
                          o.ref_id
                 LIMIT %(limit)s
                """,
                {"draft": draft_ref_id, "limit": int(limit)},
            ).fetchall()
        items: list[DraftWorkItem] = []
        for ref_id, title, blocked, jobs, ask_tags in rows:
            first = (title or "").strip().splitlines()[0] if title else ""
            items.append(
                DraftWorkItem(
                    todo_id=int(ref_id),
                    title=first,
                    blocked=bool(blocked),
                    jobs=tuple((int(j[0]), str(j[1])) for j in (jobs or [])),
                    asks=tuple(str(t) for t in (ask_tags or [])),
                )
            )
        return items

    def resolve_ask_question(self, ref_id: int, tag_value: str) -> str:
        """Turn an ``ask-user`` tag *value* into the human question text.
        ``tag_value`` is the part after ``ask-user:`` — the literal inline
        question, the empty string (bare "any human will do" marker), or a
        ``see-chunk-N`` redirect for a question that overflowed the
        80-char tag cap into a ``tag_overflow`` chunk (read back and its
        ``ask-user:``/``halt:`` prefix peeled). Returns "" for the bare
        marker or an unresolvable handle."""
        value = (tag_value or "").strip()
        m = _SEE_CHUNK_RE.match(value)
        if m is None:
            return value
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord = %s",
                (ref_id, int(m.group(1))),
            ).fetchone()
        if not row or not row[0]:
            return ""
        text = str(row[0]).strip()
        # The writer stores ``"<ns>: <question>"`` — peel the namespace off.
        for ns in ("ask-user", "halt"):
            if text.startswith(f"{ns}:"):
                return text[len(ns) + 1 :].strip()
        return text

    def job_fail_reason(self, job_ref_id: int, *, limit: int = 240) -> str | None:
        """First line of a failed job's reason — *why* it died — so the
        draft view can say so instead of a bare "failed". Prefers the
        ``job_summary`` chunk (captured stdout, whitespace-collapsed); most
        dispatchers only write that on the SUCCESS tail, so a failed job
        usually has none and falls back to the first line of the latest
        ``job_event`` chunk (the ``record_failure`` diagnostic). Capped at
        ``limit`` chars; ``None`` when neither chunk exists.

        Mirrors, without importing (would be circular: ``precis.handlers``
        imports ``precis.store`` at module scope), the query shape of
        ``handlers._todo_views._latest_job_event_reasons``.

        Both kinds resolve in ONE round-trip (``job_summary`` outranking
        ``job_event``) rather than query-then-fallback — this runs per
        failed job inside ``precis_web.routes.drafts._work_items``'s loop,
        a page-render hot path, and a failed job almost never has a
        summary, so a second query would double the *common* case."""
        with self.pool.connection() as conn:
            row = conn.execute(
                # The CASE keeps each kind's ORIGINAL tie-break when a job
                # has several of one: FIRST job_summary (ord ASC), LATEST
                # job_event (ord DESC, via the negation). Collapsing both to
                # one direction would be a silent behaviour change.
                "SELECT chunk_kind, text FROM chunks "
                "WHERE ref_id = %s AND chunk_kind IN ('job_summary', 'job_event') "
                "ORDER BY (chunk_kind = 'job_summary') DESC, "
                "         CASE WHEN chunk_kind = 'job_summary' "
                "              THEN ord ELSE -ord END ASC "
                "LIMIT 1",
                (int(job_ref_id),),
            ).fetchone()
        if not row or not row[1]:
            return None
        kind, raw = str(row[0]), str(row[1])
        if kind == "job_summary":
            # Captured stdout: whitespace-collapsed whole, as before.
            text = " ".join(raw.split())
        else:
            # ``job_event`` is a message line followed by a ``--- tail ---``
            # block of raw subprocess output — the UI wants the message.
            text = raw.split("\n", 1)[0].strip()
        return text[:limit].rstrip() + ("…" if len(text) > limit else "")

    def create_draft(
        self,
        *,
        name: str,
        title: str,
        project_ref_id: int,
        meta: dict[str, Any] | None = None,
        kind: str = "draft",
        relation: str = "draft-of",
    ) -> tuple[Any, DraftChunk]:
        """Create a draft (or ``kind='plan'``) ref bound 1:1 to its
        project, born with a title `heading` chunk so it is never empty.
        ``relation`` is the project-binding link (``draft-of``/``plan-of``),
        each 1:1 per project — so a project can own both without
        collision. Returns ``(ref, title_chunk)``."""
        with self.tx() as conn:
            dup = conn.execute(
                "SELECT 1 FROM links WHERE dst_ref_id = %s AND relation = %s",
                (project_ref_id, relation),
            ).fetchone()
            if dup is not None:
                raise ValueError(f"project ref {project_ref_id} already has a {kind}")
            ref = self._host.insert_ref(
                kind=kind,
                slug=name,
                title=title,
                meta=dict(meta or {}),
                conn=conn,
            )
            title_chunk = self._insert_draft_chunk(
                conn,
                ref_id=ref.id,
                chunk_kind="heading",
                text=title,
                parent_chunk_id=None,
                pos=key_between(None, None),
                source={"reason": "draft-title"},
                kind=kind,
            )
            self._host.add_link(
                src_ref_id=ref.id,
                dst_ref_id=project_ref_id,
                relation=relation,
                conn=conn,
            )
        return ref, title_chunk

    def draft_title_chunk_id(
        self, conn: psycopg.Connection, ref_id: int
    ) -> tuple[int, str] | None:
        """The ``(chunk_id, text)`` of a draft's title heading — the live
        root heading first in reading order, the chunk ``create_draft``
        laid down. ``None`` for a draft that has none (imported, first
        block not a root heading) — not an error; the caller renames the
        ref alone."""
        row = conn.execute(
            """SELECT chunk_id, text FROM chunks
                WHERE ref_id = %s AND chunk_kind = 'heading'
                  AND parent_chunk_id IS NULL
                  AND pos IS NOT NULL AND retired_at IS NULL
                ORDER BY pos ASC LIMIT 1""",
            (ref_id,),
        ).fetchone()
        return (int(row[0]), row[1] or "") if row is not None else None

    def set_draft_title(
        self, ref_id: int, title: str, *, source: dict[str, Any] | None = None
    ) -> tuple[str, bool]:
        """Rename a draft: ``refs.title`` **and** its title heading chunk,
        in one transaction. Returns ``(old_title, heading_synced)``.

        The two are separately writable (the heading is an ordinary
        editable chunk; ``refs.title`` had no other write path), so a
        draft could show one title in its reader and another in every
        search result/list/link chip. Renaming always writes both,
        converging even from an already-diverged state — a heading whose
        text no longer matches ``refs.title`` is overwritten, not
        preserved.

        The heading is edited in place (``chunk_id``/``handle`` survive,
        per :meth:`edit_text`), so inbound anchors stay live and only
        derived data re-derives off the new ``content_sha``."""
        clean = (title or "").strip()
        if not clean:
            raise BadInput(
                "a draft title can't be blank",
                next="edit(kind='draft', id='<slug>', title='…')",
            )
        with self.tx() as conn:
            row = conn.execute(
                "SELECT title FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
                (ref_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"no draft ref {ref_id}")
            old = row[0] or ""
            # A rename to the name it already has touches neither ``refs`` nor
            # the event log — ``updated_at`` feeds "last touched" (the reader
            # header renders it), so a no-op save must not make the document
            # look freshly edited. The heading sync below still runs: converging
            # an already-drifted heading is exactly the case where the ref title
            # is unchanged.
            if clean != old:
                conn.execute(
                    "UPDATE refs SET title = %s, updated_at = now() WHERE ref_id = %s",
                    (clean, ref_id),
                )
                conn.execute(
                    """INSERT INTO ref_events (ref_id, source, event, payload)
                       VALUES (%s, %s, 'title_changed', %s::jsonb)""",
                    (
                        ref_id,
                        (source or {}).get("actor") or "draft-edit",
                        Jsonb({"old_title": old, "new_title": clean}),
                    ),
                )
            head = self.draft_title_chunk_id(conn, ref_id)
            if head is None:
                return old, False
            chunk_id, prev = head
            if prev == clean:
                return old, True
            sha = content_sha(clean)
            conn.execute(
                "UPDATE chunks SET text = %s, content_sha = %s WHERE chunk_id = %s",
                (clean, sha, chunk_id),
            )
            conn.execute(
                """INSERT INTO chunk_events
                       (chunk_id, event_kind, content_sha, prev_text, source)
                   VALUES (%s, 'edited', %s, %s, %s)""",
                (chunk_id, sha, prev, Jsonb(source or {"reason": "draft-title"})),
            )
        return old, True

    def _insert_forked_chunk(
        self,
        conn: psycopg.Connection,
        *,
        ref_id: int,
        src: dict[str, Any],
    ) -> int:
        """Insert one copy of a source draft chunk for :meth:`fork_draft`.

        Preserves ``ord``/``pos``/``chunk_kind``/``text``/``section_path``/
        ``content_sha``/``meta``/``keywords``/``retired_at`` verbatim (a
        retired source copies retired; a live one's ``content_sha`` still
        matches its text for the embed/summarize workers), but mints a
        FRESH handle (never the source's — globally unique), same
        savepoint-retry as :meth:`_insert_draft_chunk`. ``parent_chunk_id``
        is left NULL — insertion order doesn't guarantee parent-before-child
        — so :meth:`fork_draft` fixes hierarchy in a second pass once every
        chunk has a new id. Returns the new ``chunk_id``."""
        last_exc: Exception | None = None
        row = None
        for _ in range(_HANDLE_RETRIES):
            handle = new_handle()
            try:
                with conn.transaction():  # savepoint
                    row = conn.execute(
                        """
                        INSERT INTO chunks
                            (ref_id, set_by, ord, chunk_kind, text, handle,
                             pos, parent_chunk_id, content_sha, meta,
                             section_path, keywords, retired_at)
                        VALUES (%s, 'agent', %s, %s, %s, %s, %s, NULL, %s,
                                %s, %s, %s, %s)
                        RETURNING chunk_id
                        """,
                        (
                            ref_id,
                            src["ord"],
                            src["chunk_kind"],
                            src["text"],
                            handle,
                            src["pos"],
                            src["content_sha"],
                            Jsonb(src["meta"]),
                            src["section_path"],
                            src["keywords"],
                            src["retired_at"],
                        ),
                    ).fetchone()
                break
            except psycopg.errors.UniqueViolation as exc:
                last_exc = exc
                continue
        else:  # pragma: no cover - astronomically unlikely
            raise RuntimeError(
                f"could not mint a unique handle in {_HANDLE_RETRIES} tries"
            ) from last_exc

        assert row is not None
        new_chunk_id = int(row[0])
        conn.execute(
            """
            INSERT INTO chunk_events
                (chunk_id, event_kind, content_sha, source)
            VALUES (%s, 'created', %s, %s)
            """,
            (
                new_chunk_id,
                src["content_sha"],
                Jsonb({"reason": "fork", "src_chunk_id": src["chunk_id"]}),
            ),
        )
        return new_chunk_id

    def fork_draft(
        self,
        src_ref_id: int,
        project_id: int,
        *,
        new_slug: str,
        title: str | None = None,
    ) -> Any:
        """Deep-copy an entire draft into a NEW draft bound to
        ``project_id``, leaving ``src_ref_id`` untouched. One transaction:

        1. the ``refs`` row (title/meta/authors/year) under ``new_slug``;
        2. every chunk (live *and* retired), hierarchy intact — fresh
           handle per copy, everything else (``ord``/``pos``/``chunk_kind``/
           ``text``/``content_sha``/``meta``/``keywords``/``section_path``/
           ``retired_at``) verbatim;
        3. ``chunk_blobs``/``chunk_tags`` side tables keyed on the copies.
           ``chunk_embeddings``/``chunk_summaries`` skipped (worker
           re-derives from ``content_sha``); ``chunk_review`` skipped so
           the copy starts fully unreviewed (pipeline rung 3);
        3b. in-prose intra-draft cross-refs rewritten onto the copies (see
           :func:`_remap_intra_draft_xrefs`) — document-internal, so step
           4's link remap can't reach them and copied verbatim they'd
           dangle into the source; ``content_sha``/``created`` event
           recomputed per rewritten chunk;
        4. every link touching ``src_ref_id`` either direction (INSERT-only
           — source edges untouched, unlike
           :meth:`~precis.store._links_ops.LinksMixin.migrate_links`, which
           deletes them), endpoints remapped onto the new ref/chunks; a
           would-be self-loop after remap is dropped rather than raising
           the ``links_check`` CHECK;
        5. a ``copy-of`` provenance edge new→source (mirrors ``draft-of``/
           ``has-draft`` — ``migrations/0032_draft_relations.sql``);
        6. the ``draft-of`` bind to ``project_id`` (1:1, same guard as
           :meth:`create_draft`).

        Returns the new :class:`~precis.store.types.Ref`. Raises
        ``NotFound`` if ``src_ref_id`` isn't a live draft, ``ValueError``
        if ``project_id`` already owns a draft."""
        with self.tx() as conn:
            dup = conn.execute(
                "SELECT 1 FROM links WHERE dst_ref_id = %s AND relation = 'draft-of'",
                (project_id,),
            ).fetchone()
            if dup is not None:
                raise ValueError(f"project ref {project_id} already has a draft")

            src_row = conn.execute(
                "SELECT title, meta, authors, year FROM refs "
                "WHERE ref_id = %s AND kind = 'draft' AND deleted_at IS NULL",
                (src_ref_id,),
            ).fetchone()
            if src_row is None:
                raise NotFound(f"no live draft ref id={src_ref_id}")
            src_title, src_meta, src_authors, src_year = src_row

            new_ref = self._host.insert_ref(
                kind="draft",
                slug=new_slug,
                title=title or src_title,
                meta=dict(src_meta or {}),
                authors=list(src_authors) if src_authors else None,
                year=src_year,
                conn=conn,
            )

            # -- 2/3. chunks + side tables ------------------------------
            chunk_rows = conn.execute(
                """
                SELECT chunk_id, ord, chunk_kind, text, pos, parent_chunk_id,
                       content_sha, meta, section_path, keywords, retired_at,
                       handle
                  FROM chunks WHERE ref_id = %s ORDER BY chunk_id
                """,
                (src_ref_id,),
            ).fetchall()

            id_map: dict[int, int] = {}
            old_parents: dict[int, int | None] = {}
            # legacy ``¶<base58>`` anchor → new chunk_id, for the xref remap
            handle_to_new_id: dict[str, int] = {}
            for r in chunk_rows:
                src_chunk = {
                    "chunk_id": r[0],
                    "ord": r[1],
                    "chunk_kind": r[2],
                    "text": r[3],
                    "pos": r[4],
                    "content_sha": r[6],
                    "meta": dict(r[7] or {}),
                    "section_path": list(r[8] or []),
                    "keywords": list(r[9]) if r[9] else None,
                    "retired_at": r[10],
                }
                new_chunk_id = self._insert_forked_chunk(
                    conn, ref_id=new_ref.id, src=src_chunk
                )
                id_map[r[0]] = new_chunk_id
                old_parents[r[0]] = r[5]
                handle_to_new_id[r[11]] = new_chunk_id

            # second pass: fix up parent_chunk_id now every chunk has a
            # new id (a parent can be inserted after its child above).
            for old_id, new_id in id_map.items():
                old_parent = old_parents[old_id]
                if old_parent is not None:
                    conn.execute(
                        "UPDATE chunks SET parent_chunk_id = %s WHERE chunk_id = %s",
                        (id_map[old_parent], new_id),
                    )

            # third pass: rewrite in-prose intra-draft cross-refs so they
            # point at the COPIED chunks, not the source's. These live in the
            # chunk text (document-internal — TOC / \ref), never the links
            # table, so the link remap below can't reach them; copied verbatim
            # they'd dangle back into the source draft. dc<id> == dc+chunk_id,
            # so id_map is the remap. Text change ⇒ recompute content_sha (a
            # pure fn of text) and keep the just-created chunk_events row in
            # sync, so the embed/summarize workers key off the right hash. Safe
            # to UPDATE in place here (unlike a committed body chunk): the copy
            # has no chunk_embeddings/chunk_summaries yet — they're re-derived.
            for r in chunk_rows:
                src_text = r[3]
                new_text = _remap_intra_draft_xrefs(src_text, id_map, handle_to_new_id)
                if new_text == src_text:
                    continue
                new_id = id_map[r[0]]
                new_sha = content_sha(new_text)
                conn.execute(
                    "UPDATE chunks SET text = %s, content_sha = %s WHERE chunk_id = %s",
                    (new_text, new_sha, new_id),
                )
                conn.execute(
                    "UPDATE chunk_events SET content_sha = %s "
                    "WHERE chunk_id = %s AND event_kind = 'created'",
                    (new_sha, new_id),
                )

            if id_map:
                old_ids = list(id_map)
                blob_rows = conn.execute(
                    """
                    SELECT chunk_id, bytes, mime, sha256, size_bytes, width, height
                      FROM chunk_blobs WHERE chunk_id = ANY(%s)
                    """,
                    (old_ids,),
                ).fetchall()
                for b_chunk_id, b_bytes, b_mime, b_sha, b_size, b_w, b_h in blob_rows:
                    conn.execute(
                        """
                        INSERT INTO chunk_blobs
                            (chunk_id, bytes, mime, sha256, size_bytes, width, height)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            id_map[b_chunk_id],
                            b_bytes,
                            b_mime,
                            b_sha,
                            b_size,
                            b_w,
                            b_h,
                        ),
                    )
                tag_rows = conn.execute(
                    "SELECT chunk_id, tag_id, set_by FROM chunk_tags "
                    "WHERE chunk_id = ANY(%s)",
                    (old_ids,),
                ).fetchall()
                for t_chunk_id, t_tag_id, t_set_by in tag_rows:
                    conn.execute(
                        "INSERT INTO chunk_tags (chunk_id, tag_id, set_by) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (id_map[t_chunk_id], t_tag_id, t_set_by),
                    )
                # chunk_embeddings / chunk_summaries: skipped, re-derived by
                # the worker from content_sha. chunk_review: skipped, the
                # copy starts fully unreviewed.

            # -- 4. links touching src_ref_id, either direction ---------
            link_rows = conn.execute(
                """
                SELECT src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id,
                       relation, set_by, meta
                  FROM links WHERE src_ref_id = %s OR dst_ref_id = %s
                """,
                (src_ref_id, src_ref_id),
            ).fetchall()
            for (
                l_src_ref,
                l_src_chunk,
                l_dst_ref,
                l_dst_chunk,
                relation,
                set_by,
                meta,
            ) in link_rows:
                new_src_ref = new_ref.id if l_src_ref == src_ref_id else l_src_ref
                new_dst_ref = new_ref.id if l_dst_ref == src_ref_id else l_dst_ref
                new_src_chunk = (
                    id_map[l_src_chunk]
                    if l_src_chunk is not None and l_src_ref == src_ref_id
                    else l_src_chunk
                )
                new_dst_chunk = (
                    id_map[l_dst_chunk]
                    if l_dst_chunk is not None and l_dst_ref == src_ref_id
                    else l_dst_chunk
                )
                if new_src_ref == new_dst_ref and new_src_chunk == new_dst_chunk:
                    # would-be self-loop after remap (the links_check CHECK
                    # forbids it) — drop rather than raise.
                    continue
                conn.execute(
                    """
                    INSERT INTO links
                        (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id,
                         relation, set_by, meta)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT
                        (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation)
                        DO NOTHING
                    """,
                    (
                        new_src_ref,
                        new_src_chunk,
                        new_dst_ref,
                        new_dst_chunk,
                        relation,
                        set_by,
                        Jsonb(meta or {}),
                    ),
                )

            # -- 5. copy-of provenance (has-copy mirrors at read time) --
            self._host.add_link(
                src_ref_id=new_ref.id,
                dst_ref_id=src_ref_id,
                relation="copy-of",
                conn=conn,
            )

            # -- 6. bind the new draft to its project (1:1, draft-of) ---
            self._host.add_link(
                src_ref_id=new_ref.id,
                dst_ref_id=project_id,
                relation="draft-of",
                conn=conn,
            )
        return new_ref

    def add_chunks(
        self,
        *,
        ref_id: int,
        chunk_kind: str,
        text: str,
        at: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        split: bool = True,
        kind: str = "draft",
    ) -> list[DraftChunk]:
        """Add one or more chunks (a multi-paragraph `text` splits at blank
        lines), returned in order. ``meta`` (e.g. a ``term``'s ``{short,
        long}``) is stamped on each. ``kind`` sets the handle namespace
        (``'plan'`` → ``pe<id>``). ``split=False`` inserts ``text``
        verbatim as one chunk — for a derived projection that must not
        fragment (a ``table``'s markdown render)."""
        blocks = _split_blocks(text) if split else [text]
        with self.tx() as conn:
            parent, lo, hi = self._resolve_at(conn, ref_id, at)
            self._lock_sections(conn, ref_id, parent)
            keys = n_keys_between(lo, hi, len(blocks))
            return [
                self._insert_draft_chunk(
                    conn,
                    ref_id=ref_id,
                    chunk_kind=chunk_kind,
                    text=block,
                    parent_chunk_id=parent,
                    pos=key,
                    source={"reason": "add"},
                    meta=meta,
                    kind=kind,
                )
                for block, key in zip(blocks, keys, strict=True)
            ]

    def add_figure(
        self,
        *,
        ref_id: int,
        caption: str,
        origin: str,
        image: bytes,
        mime: str,
        at: dict[str, Any] | None = None,
        figure_meta: dict[str, Any] | None = None,
    ) -> DraftChunk:
        """Add a single ``figure`` chunk: caption is the face (``text`` —
        embedded, searchable), image bytes go to ``chunk_blobs``,
        ``meta.figure`` carries ``origin`` plus provenance (e.g. a
        third-party ``permission`` paper-trail). Unlike :meth:`add_chunks`
        the caption is **not** split at blank lines. Both writes share one
        transaction — a figure never lands without its bytes."""
        sha = hashlib.sha256(image).hexdigest()
        width, height = _image_dims(image)
        fig = {"origin": origin, **(figure_meta or {})}
        with self.tx() as conn:
            parent, lo, hi = self._resolve_at(conn, ref_id, at)
            self._lock_sections(conn, ref_id, parent)
            chunk = self._insert_draft_chunk(
                conn,
                ref_id=ref_id,
                chunk_kind="figure",
                text=caption,
                parent_chunk_id=parent,
                pos=key_between(lo, hi),
                meta={"figure": fig},
                source={"reason": "add-figure", "origin": origin},
            )
            conn.execute(
                """INSERT INTO chunk_blobs
                       (chunk_id, bytes, mime, sha256, size_bytes, width, height)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (chunk.chunk_id, image, mime, sha, len(image), width, height),
            )
        return chunk

    def get_chunk_blob(self, handle: str) -> tuple[bytes, str] | None:
        """Raw ``(bytes, mime)`` for a chunk's blob (a figure image), or
        ``None`` if the chunk has none. The only path that de-TOASTs the
        bytes — used by the web blob route and (later) export."""
        with self.pool.connection() as conn:
            row = conn.execute(
                """SELECT b.bytes, b.mime FROM chunk_blobs b
                     JOIN chunks c ON c.chunk_id = b.chunk_id
                    WHERE c.handle = %s""",
                (_bare(handle),),
            ).fetchone()
        if row is None:
            return None
        return bytes(row[0]), row[1]

    def chunk_blob_version(self, chunk_id: int) -> str | None:
        """Cheap existence check *and* cache-busting discriminator for a
        chunk's blob — ``SELECT sha256`` only, never de-TOASTs ``bytes``.
        ``None`` when there's no blob row (falsy, usable as the old boolean
        ``has_chunk_blob`` was); otherwise the sha256, which the
        figure-source resolver appends (truncated) to the reader's
        ``<img>`` URL so a "refresh"-swapped blob busts the browser's
        5-minute ``Cache-Control``. Called per figure at render time, so
        must not pull megabytes."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT sha256 FROM chunk_blobs WHERE chunk_id = %s",
                (chunk_id,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def upsert_chunk_blob(
        self,
        chunk_id: int,
        image: bytes,
        mime: str,
        *,
        conn: psycopg.Connection | None = None,
    ) -> None:
        """Insert or **replace** a chunk's blob (`chunk_blobs` row). Unlike
        :meth:`add_figure` (insert-only, at creation), this is the render
        path: a computed figure's image is *regenerable*, so re-rendering
        overwrites bytes in place keyed on ``chunk_id``. Re-derives
        ``sha256``/``size``/dims from the bytes."""
        sha = hashlib.sha256(image).hexdigest()
        width, height = _image_dims(image)

        def _do(c: psycopg.Connection) -> None:
            c.execute(
                """INSERT INTO chunk_blobs
                       (chunk_id, bytes, mime, sha256, size_bytes, width, height)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (chunk_id) DO UPDATE SET
                       bytes = EXCLUDED.bytes, mime = EXCLUDED.mime,
                       sha256 = EXCLUDED.sha256, size_bytes = EXCLUDED.size_bytes,
                       width = EXCLUDED.width, height = EXCLUDED.height""",
                (chunk_id, image, mime, sha, len(image), width, height),
            )

        if conn is not None:
            _do(conn)
        else:
            with self.tx() as c:
                _do(c)

    def figure_render_bundle(self, figure_chunk_id: int) -> dict[str, Any] | None:
        """Everything the render pass needs for a computed `figure`: its
        render recipe (`meta.render`) plus, in plotted order, the
        `meta.table` payload + `content_sha` of each `plots` data chunk.
        ``None`` when the chunk carries no `meta.render` recipe (a plain
        uploaded *image*, not a *graph*). ``input_shas`` (render src + each
        data sha) feed the content-addressed invalidation key."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT chunk_kind, meta FROM chunks WHERE chunk_id = %s",
                (figure_chunk_id,),
            ).fetchone()
            if row is None or row[0] != "figure":
                return None
            meta = dict(row[1] or {})
            render = meta.get("render")
            if not isinstance(render, dict) or not render.get("src"):
                return None  # an uploaded image, not a computed graph
            # plotted data chunks, in their reading order
            data_rows = conn.execute(
                """SELECT c.chunk_id, c.meta, c.content_sha
                     FROM links l JOIN chunks c ON c.chunk_id = l.dst_chunk_id
                    WHERE l.src_chunk_id = %s AND l.relation = 'plots'
                      AND c.retired_at IS NULL
                    ORDER BY c.ord""",
                (figure_chunk_id,),
            ).fetchall()
        tables = [dict(r[1] or {}).get("table") for r in data_rows]
        return {
            "render": render,
            "tables": [t for t in tables if t is not None],
            "input_shas": [str(render.get("src"))] + [str(r[2]) for r in data_rows],
        }

    def stamp_render_key(self, figure_chunk_id: int, cached_key: str) -> None:
        """Record a freshly-rendered figure's invalidation key at
        ``meta.render.cached_key`` — a later mark-stale pass
        compares it to the recomputed `hash(src, plotted data shas)`."""
        with self.tx() as conn:
            conn.execute(
                "UPDATE chunks SET meta = "
                "jsonb_set(meta, '{render,cached_key}', to_jsonb(%s::text), true) "
                "WHERE chunk_id = %s",
                (cached_key, figure_chunk_id),
            )

    def stamp_figure_data_package(
        self,
        chunk_id: int,
        snapshot: dict[str, Any],
        *,
        conn: psycopg.Connection | None = None,
    ) -> None:
        """Swap a figure chunk's ``meta.figure.data_package`` snapshot in
        place — the "refresh" path re-renders off the source ref
        (:func:`precis.quest.figures.quest_pareto_figure`/
        ``pathway_profile_figure``) and stamps fresh numbers here so the
        export data-package appendix keeps matching the plotted pixels.
        Caption/origin/permission untouched. Optional ``conn`` (like
        :meth:`upsert_chunk_blob`) lets the route swap blob + snapshot in
        one transaction. Logs an ``edited`` chunk_event."""

        def _do(c: psycopg.Connection) -> None:
            row = c.execute(
                "SELECT retired_at FROM chunks WHERE chunk_id = %s", (chunk_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"no chunk {chunk_id}")
            if row[0] is not None:
                raise Gone(
                    f"chunk {chunk_id} is retired — refresh the current live "
                    "chunk instead"
                )
            c.execute(
                "UPDATE chunks SET meta = jsonb_set(COALESCE(meta, '{}'::jsonb), "
                "'{figure,data_package}', %s::jsonb, true) WHERE chunk_id = %s",
                (Jsonb(snapshot), chunk_id),
            )
            c.execute(
                "INSERT INTO chunk_events (chunk_id, event_kind, source) "
                "VALUES (%s, 'edited', %s)",
                (chunk_id, Jsonb({"reason": "figure-data-package-refresh"})),
            )

        if conn is not None:
            _do(conn)
        else:
            with self.tx() as c:
                _do(c)

    def set_render_recipe(
        self,
        chunk_id: int,
        recipe: dict[str, Any],
        *,
        conn: psycopg.Connection | None = None,
    ) -> None:
        """Stamp a figure chunk's `meta.render` recipe (the graph code). Set
        at creation, rewritten on edit; a rewrite clears any prior
        `cached_key` so the figure is stale until re-rendered. Logs an
        `edited` chunk_event (`reason: render-recipe`)."""

        def _do(c: psycopg.Connection) -> None:
            c.execute(
                "UPDATE chunks SET meta = jsonb_set(meta, '{render}', %s::jsonb, true) "
                "WHERE chunk_id = %s",
                (Jsonb(recipe), chunk_id),
            )
            c.execute(
                "INSERT INTO chunk_events (chunk_id, event_kind, source) "
                "VALUES (%s, 'edited', %s)",
                (chunk_id, Jsonb({"reason": "render-recipe"})),
            )

        if conn is not None:
            _do(conn)
        else:
            with self.tx() as c:
                _do(c)

    def link_figure_plots(self, figure_chunk_id: int, data_chunk_ids: list[int]) -> int:
        """Create the figure→data `plots` edges for a computed figure, by
        chunk_id. Resolves each chunk's `(ref_id, ord)` and routes through
        :meth:`add_link` (dedup + validation). Returns the count. All
        chunks must already exist."""
        with self.tx() as conn:
            rows = conn.execute(
                "SELECT chunk_id, ref_id, ord FROM chunks WHERE chunk_id = ANY(%s)",
                ([figure_chunk_id, *data_chunk_ids],),
            ).fetchall()
            info = {int(r[0]): (int(r[1]), int(r[2])) for r in rows}
            fig_ref, fig_ord = info[figure_chunk_id]
            n = 0
            for dcid in data_chunk_ids:
                d_ref, d_ord = info[dcid]
                self._host.add_link(
                    src_ref_id=fig_ref,
                    src_pos=fig_ord,
                    dst_ref_id=d_ref,
                    dst_pos=d_ord,
                    relation="plots",
                    conn=conn,
                )
                n += 1
            return n

    def link_figure_canvas(self, figure_chunk_id: int, canvas_ref_id: int) -> None:
        """Wire a draft figure chunk → a ``kind='figure'`` canvas via a
        ``has-figure`` link (the ``canvas`` medium). Chunk→ref edge:
        the source is the figure *chunk*, the target the whole figure *ref*.
        Idempotent (``add_link`` dedups on the endpoint tuple)."""
        with self.tx() as conn:
            row = conn.execute(
                "SELECT ref_id, ord FROM chunks WHERE chunk_id = %s",
                (figure_chunk_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"no chunk {figure_chunk_id}")
            fig_ref, fig_ord = int(row[0]), int(row[1])
            self._host.add_link(
                src_ref_id=fig_ref,
                src_pos=fig_ord,
                dst_ref_id=canvas_ref_id,  # ref-level dst (dst_pos=None)
                relation="has-figure",
                conn=conn,
            )

    def figure_canvas_ref(self, figure_chunk_id: int) -> int | None:
        """The live ``kind='figure'`` canvas ref linked from this figure chunk
        via ``has-figure``, or ``None``. Joins ``refs`` so a
        soft-deleted canvas reads as absent (the figure falls back to blob /
        placeholder)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                """SELECT l.dst_ref_id
                     FROM links l
                     JOIN refs r ON r.ref_id = l.dst_ref_id
                    WHERE l.src_chunk_id = %s
                      AND l.relation = 'has-figure'
                      AND r.kind = 'figure'
                      AND r.deleted_at IS NULL
                    LIMIT 1""",
                (figure_chunk_id,),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def figure_owning_draft(self, canvas_ref_id: int) -> tuple[int, int] | None:
        """The ``(draft_ref_id, anchor_chunk_id)`` owning this figure canvas
        via the reverse ``has-figure`` edge, or ``None``. Inverse of
        :meth:`figure_canvas_ref`: given a ``kind='figure'`` canvas, find
        the live draft **chunk** whose caption drew it — used by the
        diagram-propose loop (``precis.diagram.doc_context``) for Layer-1
        context. Joins ``refs`` so a soft-deleted draft reads as absent."""
        with self.pool.connection() as conn:
            row = conn.execute(
                """SELECT l.src_ref_id, l.src_chunk_id
                     FROM links l
                     JOIN refs r ON r.ref_id = l.src_ref_id
                    WHERE l.dst_ref_id = %s
                      AND l.relation = 'has-figure'
                      AND r.kind = 'draft'
                      AND r.deleted_at IS NULL
                      AND l.src_chunk_id IS NOT NULL
                    LIMIT 1""",
                (canvas_ref_id,),
            ).fetchone()
        return (int(row[0]), int(row[1])) if row is not None else None

    def set_figure_provenance(
        self,
        handle: str,
        *,
        permission: dict[str, Any] | None = None,
        origin: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> DraftChunk | None:
        """Update a figure chunk's provenance meta in place:
        replace ``meta.figure.permission`` and/or ``meta.figure.origin``,
        leaving the caption and image bytes untouched (no re-embed). Logs
        an ``edited`` event so the change shows in the chunk's history."""
        with self.tx() as conn:
            row = conn.execute(
                "SELECT chunk_id, chunk_kind, meta, retired_at "
                "FROM chunks WHERE handle = %s",
                (_bare(handle),),
            ).fetchone()
            if row is None:
                raise NotFound(f"no draft chunk ¶{_bare(handle)}")
            chunk_id, chunk_kind, meta, retired = row
            if chunk_kind != "figure":
                raise BadInput(f"¶{_bare(handle)} is a {chunk_kind}, not a figure")
            if retired is not None:
                raise Gone(
                    f"¶{_bare(handle)} is retired — it was removed or replaced; "
                    "edit the current live chunk instead"
                )
            meta = dict(meta or {})
            fig = dict(meta.get("figure") or {})
            if origin is not None:
                fig["origin"] = origin
            if permission is not None:
                fig["permission"] = permission
            meta["figure"] = fig
            conn.execute(
                "UPDATE chunks SET meta = %s WHERE chunk_id = %s",
                (Jsonb(meta), chunk_id),
            )
            conn.execute(
                "INSERT INTO chunk_events (chunk_id, event_kind, source) "
                "VALUES (%s, 'edited', %s)",
                (chunk_id, Jsonb({**(source or {}), "reason": "figure-provenance"})),
            )
        return self.get_draft_chunk(handle)

    def set_chunk_style(self, handle: str, style: str | None) -> DraftChunk | None:
        """Set (or clear) a heading chunk's section style. Writes
        ``meta.style`` (a skill slug) in place — metadata-only, no
        re-embed. ``style`` falsy clears it. Logs an ``edited`` event. A
        *section* concern, rejected on a non-heading chunk."""
        with self.tx() as conn:
            row = conn.execute(
                "SELECT chunk_id, chunk_kind, meta, retired_at "
                "FROM chunks WHERE handle = %s",
                (_bare(handle),),
            ).fetchone()
            if row is None:
                raise NotFound(f"no draft chunk ¶{_bare(handle)}")
            chunk_id, chunk_kind, meta, retired = row
            if chunk_kind != "heading":
                raise BadInput(
                    f"style applies to a heading section; {_bare(handle)} "
                    f"is a {chunk_kind}"
                )
            if retired is not None:
                raise Gone(
                    f"¶{_bare(handle)} is retired — it was removed or replaced; "
                    "edit the current live chunk instead"
                )
            meta = dict(meta or {})
            if style:
                meta["style"] = style
            else:
                meta.pop("style", None)
            conn.execute(
                "UPDATE chunks SET meta = %s WHERE chunk_id = %s",
                (Jsonb(meta), chunk_id),
            )
            conn.execute(
                "INSERT INTO chunk_events (chunk_id, event_kind, source) "
                "VALUES (%s, 'edited', %s)",
                (chunk_id, Jsonb({"reason": "set-style", "style": style})),
            )
        return self.get_draft_chunk(handle)

    def patch_chunk_meta(self, handle: str, patch: dict[str, Any]) -> None:
        """Shallow-merge ``patch`` into a chunk's ``meta`` in place — a key
        mapped to ``None`` is **removed**, any other value set.
        Metadata-only, no re-embed. Logs an ``edited`` event. Used by the
        plan handler's ``status``/``belief`` markers. No-op on empty patch."""
        if not patch:
            return
        with self.tx() as conn:
            row = conn.execute(
                "SELECT chunk_id, meta, retired_at FROM chunks WHERE handle = %s",
                (_bare(handle),),
            ).fetchone()
            if row is None:
                raise NotFound(f"no draft chunk ¶{_bare(handle)}")
            chunk_id, meta, retired = row
            if retired is not None:
                raise Gone(
                    f"¶{_bare(handle)} is retired — it was removed or replaced; "
                    "edit the current live chunk instead"
                )
            new_meta = dict(meta or {})
            for k, v in patch.items():
                if v is None:
                    new_meta.pop(k, None)
                else:
                    new_meta[k] = v
            conn.execute(
                "UPDATE chunks SET meta = %s WHERE chunk_id = %s",
                (Jsonb(new_meta), chunk_id),
            )
            conn.execute(
                "INSERT INTO chunk_events (chunk_id, event_kind, source) "
                "VALUES (%s, 'edited', %s)",
                (chunk_id, Jsonb({"reason": "patch-meta", "keys": list(patch)})),
            )

    #: The manufacturing-part attribute bag + hover surfaces that
    #: :meth:`set_term_attrs` may patch in place. ``registry``
    #: and ``callout`` are structural (set at add-time / re-home) and are not
    #: patched here.
    _TERM_ATTR_KEYS = (
        "short",
        "surface_forms",
        "manufacturer",
        "mpn",
        "url",
        "ordering",
    )

    def set_term_attrs(self, handle: str, attrs: dict[str, Any]) -> DraftChunk | None:
        """Patch a ``term`` leaf's attribute bag/hover surfaces in place —
        ``manufacturer``/``mpn``/``url``/``ordering``/``short``/
        ``surface_forms``. Metadata-only, no re-embed. A key set to
        ``None`` clears it; unknown keys ignored. Rejected on a
        non-``term`` chunk."""
        patch = {k: v for k, v in (attrs or {}).items() if k in self._TERM_ATTR_KEYS}
        with self.tx() as conn:
            row = conn.execute(
                "SELECT chunk_id, chunk_kind, meta, retired_at "
                "FROM chunks WHERE handle = %s",
                (_bare(handle),),
            ).fetchone()
            if row is None:
                raise NotFound(f"no draft chunk ¶{_bare(handle)}")
            chunk_id, chunk_kind, meta, retired = row
            if chunk_kind != "term":
                raise BadInput(
                    f"term attributes apply to a term leaf; {_bare(handle)} "
                    f"is a {chunk_kind}"
                )
            if retired is not None:
                raise Gone(
                    f"¶{_bare(handle)} is retired — it was removed or replaced; "
                    "edit the current live chunk instead"
                )
            meta = dict(meta or {})
            for key, val in patch.items():
                if val is None:
                    meta.pop(key, None)
                else:
                    meta[key] = val
            conn.execute(
                "UPDATE chunks SET meta = %s WHERE chunk_id = %s",
                (Jsonb(meta), chunk_id),
            )
            conn.execute(
                "INSERT INTO chunk_events (chunk_id, event_kind, source) "
                "VALUES (%s, 'edited', %s)",
                (chunk_id, Jsonb({"reason": "set-term-attrs"})),
            )
        return self.get_draft_chunk(handle)

    def set_list_kind(
        self,
        handle: str,
        kind: str,
        *,
        source: dict[str, Any] | None = None,
    ) -> DraftChunk | None:
        """Switch a ``ulist``/``olist`` container's kind, or dissolve it to
        normal text (migration 0037). ``kind`` in ``{'ulist','olist'}``
        flips it in place — metadata-only, no re-embed (the container
        carries no prose; its ``item`` children do). ``kind='normal'``
        **dissolves** the list: each direct ``item`` becomes a
        ``paragraph`` (text unchanged, embedding/summary stay valid) and
        splices into the container's slot (subtree follows), then the
        container retires. Rejects a non-list handle. Returns the
        container for an in-place flip, ``None`` after a dissolve."""
        if kind not in ("ulist", "olist", "normal"):
            raise BadInput(
                f"list kind must be 'ulist', 'olist' or 'normal'; got {kind!r}"
            )
        with self.tx() as conn:
            row = conn.execute(
                "SELECT chunk_id, ref_id, chunk_kind, parent_chunk_id, retired_at "
                "FROM chunks WHERE handle = %s",
                (_bare(handle),),
            ).fetchone()
            if row is None:
                raise NotFound(f"no draft chunk ¶{_bare(handle)}")
            chunk_id, ref_id, chunk_kind, parent, retired = row
            if retired is not None:
                raise Gone(
                    f"¶{_bare(handle)} is retired — it was removed or replaced; "
                    "edit the current live chunk instead"
                )
            if chunk_kind not in ("ulist", "olist"):
                raise BadInput(
                    f"list kind applies to a list container; {_bare(handle)} "
                    f"is a {chunk_kind}"
                )
            if kind in ("ulist", "olist"):
                if kind != chunk_kind:
                    conn.execute(
                        "UPDATE chunks SET chunk_kind = %s WHERE chunk_id = %s",
                        (kind, chunk_id),
                    )
                    self._log(
                        conn,
                        chunk_id,
                        "edited",
                        source,
                        {"reason": "list-kind", "from": chunk_kind, "to": kind},
                    )
                return self.get_draft_chunk(handle)
            # kind == 'normal' → dissolve the list. Structural (reparents
            # the container's children + retires it), so it takes the same
            # section locks as retire_chunk(mode='promote').
            self._lock_sections(conn, ref_id, parent, chunk_id)
            kids = self._children(conn, ref_id, chunk_id)
            item_ids = [k.chunk_id for k in kids if k.chunk_kind == "item"]
            if item_ids:
                conn.execute(
                    "UPDATE chunks SET chunk_kind = 'paragraph' "
                    "WHERE chunk_id = ANY(%s)",
                    (item_ids,),
                )
            # Splice every child into the container's slot, then retire it —
            # the same shape as ``retire_chunk(mode='promote')``.
            sibs = self._children(conn, ref_id, parent)
            idx = next((i for i, s in enumerate(sibs) if s.chunk_id == chunk_id), None)
            if idx is None:  # invariant: a live container is among its siblings
                raise BadInput(f"¶{_bare(handle)} is not a live positioned chunk")
            lo = sibs[idx - 1].pos if idx > 0 else None
            hi = sibs[idx + 1].pos if idx + 1 < len(sibs) else None
            keys = n_keys_between(lo, hi, len(kids))
            for kid, key in zip(kids, keys, strict=True):
                conn.execute(
                    "UPDATE chunks SET parent_chunk_id = %s, pos = %s "
                    "WHERE chunk_id = %s",
                    (parent, key, kid.chunk_id),
                )
                self._log(
                    conn,
                    kid.chunk_id,
                    "reparented",
                    source,
                    {"dissolved_from": chunk_id},
                )
            conn.execute(
                "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s",
                (chunk_id,),
            )
            self._log(conn, chunk_id, "retired", source, {"mode": "dissolve"})
        return None

    def set_word_target(
        self, handle: str, target: tuple[int, int] | None
    ) -> DraftChunk | None:
        """Set (or clear) a heading section's word target (proposal
        writing). Writes ``meta.word_target = {"min": lo, "max": hi}`` in
        place — metadata-only, no re-embed. ``target`` falsy clears it.
        Logs an ``edited`` event. A *section* concern, rejected on a
        non-heading chunk. Read back by
        :func:`precis.utils.wordcount.aggregate_word_counts` for the
        over/under verdict."""
        with self.tx() as conn:
            row = conn.execute(
                "SELECT chunk_id, chunk_kind, meta, retired_at "
                "FROM chunks WHERE handle = %s",
                (_bare(handle),),
            ).fetchone()
            if row is None:
                raise NotFound(f"no draft chunk ¶{_bare(handle)}")
            chunk_id, chunk_kind, meta, retired = row
            if chunk_kind != "heading":
                raise BadInput(
                    f"word_target applies to a heading section; {_bare(handle)} "
                    f"is a {chunk_kind}"
                )
            if retired is not None:
                raise Gone(
                    f"¶{_bare(handle)} is retired — it was removed or replaced; "
                    "edit the current live chunk instead"
                )
            meta = dict(meta or {})
            if target:
                lo, hi = target
                meta["word_target"] = {"min": int(lo), "max": int(hi)}
            else:
                meta.pop("word_target", None)
            conn.execute(
                "UPDATE chunks SET meta = %s WHERE chunk_id = %s",
                (Jsonb(meta), chunk_id),
            )
            conn.execute(
                "INSERT INTO chunk_events (chunk_id, event_kind, source) "
                "VALUES (%s, 'edited', %s)",
                (chunk_id, Jsonb({"reason": "set-word-target", "target": target})),
            )
        return self.get_draft_chunk(handle)

    def section_style_for(self, handle: str) -> str | None:
        """The nearest enclosing heading's section style (``meta.style``),
        or ``None``. Walks ``parent_chunk_id`` upward (the chunk itself
        counts if a styled heading) — a style governs its heading's whole
        subtree, so the editor injects the *nearest* enclosing one. Accepts
        any handle form (``dc<id>``/``¶base58``/bare)."""
        start = self.get_draft_chunk(handle)
        if start is None:
            return None
        with self.pool.connection() as conn:
            row: tuple[Any, ...] | None = conn.execute(
                "SELECT parent_chunk_id, chunk_kind, meta->>'style' "
                "FROM chunks WHERE chunk_id = %s AND retired_at IS NULL",
                (start.chunk_id,),
            ).fetchone()
            while row is not None:
                parent_id, kind, style = row
                if kind == "heading" and style:
                    return str(style)
                if parent_id is None:
                    return None
                row = conn.execute(
                    "SELECT parent_chunk_id, chunk_kind, meta->>'style' "
                    "FROM chunks WHERE chunk_id = %s AND retired_at IS NULL",
                    (parent_id,),
                ).fetchone()
        return None

    def scaffold_sections(
        self, ref_id: int, sections: list[tuple[str, str | None]]
    ) -> list[str]:
        """Lay down a genre's standard sections: append one ``heading`` per
        ``(title, style)`` at the top level, after any existing top-level
        chunks (e.g. the auto-minted title), with ``meta.style`` set.
        Returns the new ``dc`` handles. Used by the new-draft flow to
        scaffold from the picked ``doc_type``."""
        if not sections:
            return []
        with self.tx() as conn:
            self._lock_sections(conn, ref_id, None)
            row = conn.execute(
                "SELECT pos FROM chunks WHERE ref_id = %s "
                "AND parent_chunk_id IS NULL AND pos IS NOT NULL "
                "AND retired_at IS NULL ORDER BY pos DESC LIMIT 1",
                (ref_id,),
            ).fetchone()
            last = row[0] if row else None
            keys = n_keys_between(last, None, len(sections))
            out: list[str] = []
            for (title, style), pos in zip(sections, keys):
                meta = {"style": style} if style else None
                c = self._insert_draft_chunk(
                    conn,
                    ref_id=ref_id,
                    chunk_kind="heading",
                    text=title,
                    parent_chunk_id=None,
                    pos=pos,
                    meta=meta,
                    source={"reason": "scaffold"},
                )
                out.append(c.dc)
        return out

    # -- mutations -----------------------------------------------------------

    def _row(self, conn: psycopg.Connection, handle: str) -> tuple[Any, ...] | None:
        return conn.execute(
            """SELECT chunk_id, ref_id, chunk_kind, parent_chunk_id, pos,
                      text, retired_at
                 FROM chunks WHERE handle = %s""",
            (_bare(handle),),
        ).fetchone()

    def _live_count(self, conn: psycopg.Connection, ref_id: int) -> int:
        row = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s "
            "AND pos IS NOT NULL AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        assert row is not None  # count(*) always returns one row
        return int(row[0])

    def _descendant_ids(self, conn: psycopg.Connection, chunk_id: int) -> list[int]:
        rows = conn.execute(
            """WITH RECURSIVE sub AS (
                   SELECT chunk_id FROM chunks
                    WHERE parent_chunk_id = %s AND retired_at IS NULL
                   UNION ALL
                   SELECT c.chunk_id FROM chunks c
                     JOIN sub s ON c.parent_chunk_id = s.chunk_id
                    WHERE c.retired_at IS NULL
               ) SELECT chunk_id FROM sub""",
            (chunk_id,),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def _log(
        self,
        conn: psycopg.Connection,
        chunk_id: int,
        kind: str,
        source: dict[str, Any] | None,
        extra: dict[str, Any] | None,
    ) -> None:
        payload = {**(source or {}), **(extra or {})}
        conn.execute(
            "INSERT INTO chunk_events (chunk_id, event_kind, source) "
            "VALUES (%s, %s, %s)",
            (chunk_id, kind, Jsonb(payload)),
        )

    def edit_text(
        self,
        handle: str,
        text: str,
        *,
        base_sha: str | None = None,
        source: dict[str, Any] | None = None,
        meta_patch: dict[str, Any] | None = None,
        kind: str = "draft",
    ) -> DraftChunk | None:
        """In-place text edit: bump `content_sha`, log an `edited` event
        with `prev_text`. The handle (and references to it) survive;
        derived data re-derives on the sha mismatch.

        ``handle`` must be the legacy base-58 anchor (``DraftChunk.handle``,
        optionally ``¶``-prefixed), looked up via ``chunks.handle`` — the
        universal ``.dc`` handle (``dc42``/``pe42``) raises ``NotFound``
        here.

        Optimistic concurrency: pass ``base_sha`` (the ``content_sha`` the
        caller saw on read) to fail the edit if the chunk changed
        underneath it, so two agents editing the same chunk don't silently
        clobber each other. Omit for a force-overwrite.

        ``meta_patch`` shallow-merges into ``chunks.meta`` (NULL-safe) in
        the same statement — updates a ``table``'s canonical ``meta.table``
        alongside its re-derived markdown ``text`` atomically.
        """
        sha = content_sha(text)
        with self.tx() as conn:
            row = self._row(conn, handle)
            if row is None:
                raise NotFound(f"no draft chunk ¶{_bare(handle)}")
            if row[6] is not None:
                raise Gone(
                    f"¶{_bare(handle)} is retired — it was removed or replaced; "
                    "edit the current live chunk instead"
                )
            if base_sha is not None:
                current = content_sha(row[5])
                # Prefix match: the read path now shows a 12-char sha
                # prefix, but a full 64-char digest (older callers) is
                # still a valid prefix. Normalise case; reject a token too
                # short to be a meaningful guard.
                nb = base_sha.strip().lower()
                if len(nb) < 8:
                    raise BadInput(
                        f"base_sha {base_sha!r} too short — need ≥8 hex chars "
                        "(the sha prefix shown on read)",
                        next=f"get(kind='draft', id='¶{_bare(handle)}') for the sha",
                    )
                if not current.startswith(nb):
                    raise BadInput(
                        f"¶{_bare(handle)} changed since you read it "
                        f"(you read {nb[:8]}…, now {current[:8]}…) — "
                        "re-read and retry so you don't clobber the newer edit",
                        next=(
                            f"get(kind='draft', id='¶{_bare(handle)}') for the "
                            "current text + sha, then edit with the new base_sha="
                        ),
                    )
            if meta_patch:
                conn.execute(
                    "UPDATE chunks SET text = %s, content_sha = %s, "
                    "meta = meta || %s::jsonb WHERE chunk_id = %s",
                    (text, sha, Jsonb(meta_patch), row[0]),
                )
            else:
                conn.execute(
                    "UPDATE chunks SET text = %s, content_sha = %s WHERE chunk_id = %s",
                    (text, sha, row[0]),
                )
            conn.execute(
                """INSERT INTO chunk_events
                       (chunk_id, event_kind, content_sha, prev_text, source)
                   VALUES (%s, 'edited', %s, %s, %s)""",
                (row[0], sha, row[5], Jsonb(source or {})),
            )
        return self.get_draft_chunk(handle, kind=kind)

    # -- review ledger (paper-writing pipeline rung 3) --------------------
    #
    # Lifted into :class:`~precis.store._draft_review_ops.DraftReviewStore`
    # (``store.drafts.review``) — see the module docstring. The methods
    # below are TRANSITIONAL delegations kept so existing ``store.drafts.*``
    # call sites don't churn this round; a later round migrates callers to
    # ``store.drafts.review.*`` and deletes these.

    def record_review(
        self, chunk_id: int, checker: str, *, verdict: str = "approved"
    ) -> str:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.record_review`
        (:attr:`review`)."""
        return self.review.record_review(chunk_id, checker, verdict=verdict)

    def retract_review(self, chunk_id: int, checker: str) -> bool:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.retract_review`
        (:attr:`review`)."""
        return self.review.retract_review(chunk_id, checker)

    def approved_pairs_at_current_sha(self, ref_id: int) -> set[tuple[int, str]]:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.approved_pairs_at_current_sha`
        (:attr:`review`)."""
        return self.review.approved_pairs_at_current_sha(ref_id)

    def review_subtree_chunk_ids(self, ref_id: int, heading_chunk_id: int) -> list[int]:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.review_subtree_chunk_ids`
        (:attr:`review`)."""
        return self.review.review_subtree_chunk_ids(ref_id, heading_chunk_id)

    def toc_digest(self, ref_id: int) -> str:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.toc_digest`
        (:attr:`review`)."""
        return self.review.toc_digest(ref_id)

    def chunks_requiring_review(
        self, ref_id: int, checker: str
    ) -> list[dict[str, Any]]:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.chunks_requiring_review`
        (:attr:`review`)."""
        return self.review.chunks_requiring_review(ref_id, checker)

    def authored_provenance(self, ref_id: int) -> dict[int, str]:
        """``{chunk_id: authored_by}`` for live body chunks of ``ref_id``
        carrying a machine-authored stamp (grounded-authoring reviewer,
        pipeline rung 3d) — a NEW chunk's ``chunks.meta->>'authored_by'``
        or the latest grounded EDIT's ``chunk_events.source->>'authored_by'``
        (raw stamp, e.g. ``'review:cites'``). Only stamped chunks appear.
        Same live-chunk filter as :meth:`reviewable_chunks`."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT c.chunk_id,
                       COALESCE(
                         c.meta->>'authored_by',
                         (SELECT ce.source->>'authored_by'
                            FROM chunk_events ce
                           WHERE ce.chunk_id = c.chunk_id
                             AND ce.event_kind = 'edited'
                             AND ce.source->>'authored_by' IS NOT NULL
                           ORDER BY ce.ts DESC LIMIT 1)
                       ) AS authored_by
                  FROM chunks c
                 WHERE c.ref_id = %s
                   AND c.content_sha IS NOT NULL
                   AND c.retired_at IS NULL
                """,
                (ref_id,),
            ).fetchall()
        return {int(r[0]): r[1] for r in rows if r[1] is not None}

    def draft_authoring_enabled(self, ref_id: int) -> bool:
        """Per-document auto-author toggle (rung 3e):
        ``refs.meta.authoring_enabled``, default ``False``. When on, the
        grounded review lenses (``cites``/``structure``) EDIT the draft
        instead of only filing findings
        (:func:`precis.quest.review_fanout.mint_review_fanout`)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
            ).fetchone()
        if row is None or not row[0]:
            return False
        return bool(row[0].get("authoring_enabled"))

    def reviewable_chunks(self, ref_id: int) -> list[ReviewableChunk]:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.reviewable_chunks`
        (:attr:`review`)."""
        return self.review.reviewable_chunks(ref_id)

    def review_status_for_chunk(self, chunk_id: int) -> list[ChunkReviewEntry]:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.review_status_for_chunk`
        (:attr:`review`)."""
        return self.review.review_status_for_chunk(chunk_id)

    def review_root_chunk_id(self, ref_id: int) -> int | None:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.review_root_chunk_id`
        (:attr:`review`)."""
        return self.review.review_root_chunk_id(ref_id)

    def review_status_for_draft(self, ref_id: int) -> list[DraftReviewRow]:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.review_status_for_draft`
        (:attr:`review`)."""
        return self.review.review_status_for_draft(ref_id)

    def review_rollup_for_draft(self, ref_id: int) -> dict[str, int]:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.review_rollup_for_draft`
        (:attr:`review`)."""
        return self.review.review_rollup_for_draft(ref_id)

    def review_diff_since(self, chunk_id: int, since_sha: str) -> str:
        """Transitional delegation — see
        :meth:`~precis.store._draft_review_ops.DraftReviewStore.review_diff_since`
        (:attr:`review`)."""
        return self.review.review_diff_since(chunk_id, since_sha)

    def move_chunk(
        self,
        handle: str,
        move: dict[str, Any],
        *,
        source: dict[str, Any] | None = None,
        kind: str = "draft",
    ) -> DraftChunk | None:
        """Reorder / reparent a chunk (its subtree follows). Writes `pos` +
        `parent_chunk_id`, logs a `moved`/`reparented` event. No text change
        → no re-embed. ``kind`` sets the returned chunk's handle namespace."""
        with self.tx() as conn:
            row = self._row(conn, handle)
            if row is None:
                raise NotFound(f"no draft chunk ¶{_bare(handle)}")
            if row[6] is not None:
                raise Gone(
                    f"¶{_bare(handle)} is retired — it was removed or replaced; "
                    "edit the current live chunk instead"
                )
            chunk_id, ref_id, old_parent, old_pos = row[0], row[1], row[3], row[4]
            new_parent, lo, hi = self._resolve_move(
                conn, ref_id, move, moving_id=chunk_id
            )
            self._lock_sections(conn, ref_id, old_parent, new_parent)
            if new_parent is not None:
                forbidden = {chunk_id, *self._descendant_ids(conn, chunk_id)}
                if new_parent in forbidden:
                    raise BadInput(
                        "cannot move a chunk under itself or its own subtree"
                    )
            new_pos = key_between(lo, hi)
            conn.execute(
                "UPDATE chunks SET pos = %s, parent_chunk_id = %s WHERE chunk_id = %s",
                (new_pos, new_parent, chunk_id),
            )
            event_kind = "reparented" if new_parent != old_parent else "moved"
            self._log(
                conn,
                chunk_id,
                event_kind,
                source,
                {
                    "from": {"parent": old_parent, "pos": old_pos},
                    "to": {"parent": new_parent, "pos": new_pos},
                },
            )
        return self.get_draft_chunk(handle, kind=kind)

    def retire_chunk(
        self,
        handle: str,
        *,
        mode: str | None = None,
        source: dict[str, Any] | None = None,
        kind: str = "draft",
    ) -> None:
        """Soft-delete (retire) a chunk. A chunk with live children needs
        `mode='cascade'` (retire the subtree) or `'promote'` (lift children
        to the parent). Refuses to retire the last live chunk. ``kind`` is
        accepted only for symmetry with the other plan-facing ops."""
        with self.tx() as conn:
            row = self._row(conn, handle)
            if row is None:
                raise NotFound(f"no draft chunk ¶{_bare(handle)}")
            if row[6] is not None:
                return  # already retired — idempotent
            chunk_id, ref_id, parent = row[0], row[1], row[3]
            self._lock_sections(conn, ref_id, parent, chunk_id)
            kids = self._children(conn, ref_id, chunk_id)
            live = self._live_count(conn, ref_id)
            if kids:
                if mode not in ("cascade", "promote"):
                    raise BadInput(
                        "retiring a chunk with children requires "
                        "mode='cascade' (delete contents) or "
                        "mode='promote' (keep contents)"
                    )
                if mode == "cascade":
                    subtree = [chunk_id, *self._descendant_ids(conn, chunk_id)]
                    if len(subtree) >= live:
                        raise BadInput(
                            "cannot retire the whole draft (last live chunks)"
                        )
                    conn.execute(
                        "UPDATE chunks SET retired_at = now() WHERE chunk_id = ANY(%s)",
                        (subtree,),
                    )
                    self._log(conn, chunk_id, "retired", source, {"mode": "cascade"})
                else:  # promote — splice children into the parent's slot
                    sibs = self._children(conn, ref_id, parent)
                    idx = next(
                        (i for i, s in enumerate(sibs) if s.chunk_id == chunk_id), None
                    )
                    if idx is None:  # invariant: chunk being promoted is live
                        raise BadInput(
                            "cannot promote a chunk absent from its live siblings"
                        )
                    lo = sibs[idx - 1].pos if idx > 0 else None
                    hi = sibs[idx + 1].pos if idx + 1 < len(sibs) else None
                    keys = n_keys_between(lo, hi, len(kids))
                    for kid, key in zip(kids, keys, strict=True):
                        conn.execute(
                            "UPDATE chunks SET parent_chunk_id = %s, pos = %s "
                            "WHERE chunk_id = %s",
                            (parent, key, kid.chunk_id),
                        )
                        self._log(
                            conn,
                            kid.chunk_id,
                            "reparented",
                            source,
                            {"promoted_from": chunk_id},
                        )
                    conn.execute(
                        "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s",
                        (chunk_id,),
                    )
                    self._log(conn, chunk_id, "retired", source, {"mode": "promote"})
            else:
                if live <= 1:
                    raise BadInput("cannot retire the last live chunk of a draft")
                conn.execute(
                    "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s",
                    (chunk_id,),
                )
                self._log(conn, chunk_id, "retired", source, None)

    def merge_prev_block(
        self,
        handle: str,
        prev_handle: str,
        text: str = "",
        *,
        base_sha: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> DraftChunk | None:
        """Backspace-merge: append ``text`` onto ``prev_handle`` and retire
        ``handle`` — ONE transaction, so the two halves can never split
        (gr176088 part 2b). The former route did this as two store calls
        (``retire_chunk`` then ``edit_text``); a concurrent edit to
        ``prev_handle`` landing between them was silently lost, and an
        optimistic guard on the edit alone can't fix that (retire-first
        orphans the retire on a conflicting edit; edit-first defeats
        ``retire_chunk``'s own childless guard). One lock + one guard pass
        makes either the whole merge land, or nothing.

        Guards, inside the transaction (any failure raises
        ``BadInput``/``NotFound``/``Gone``, rolls back, no partial write):
        ``base_sha`` (if given) must match ``prev_handle``'s *current*
        content_sha, re-checked here not trusted from the caller's earlier
        read (mirrors :meth:`edit_text`); ``handle`` must still be
        retireable as a leaf (no live children, not the draft's last live
        chunk).

        Returns the merged ``prev_handle`` chunk (post-append)."""
        with self.tx() as conn:
            prev_row = self._row(conn, prev_handle)
            if prev_row is None:
                raise NotFound(f"no draft chunk ¶{_bare(prev_handle)}")
            if prev_row[6] is not None:
                raise Gone(
                    f"¶{_bare(prev_handle)} is retired — it was removed or "
                    "replaced; re-read the current previous block"
                )
            row = self._row(conn, handle)
            if row is None:
                raise NotFound(f"no draft chunk ¶{_bare(handle)}")
            if row[6] is not None:
                return self.get_draft_chunk(prev_handle)  # already retired — idempotent
            chunk_id, ref_id, parent = row[0], row[1], row[3]
            prev_chunk_id, prev_parent = prev_row[0], prev_row[3]
            self._lock_sections(conn, ref_id, parent, prev_parent, chunk_id)
            # The pre-lock reads only established WHICH sections to lock —
            # while this txn blocked on the lock, a concurrent holder may
            # have committed (another merge onto the same prev, a retire, a
            # move). Re-read both rows under the lock and re-run every state
            # check against the fresh copies; comparing base_sha against the
            # stale pre-lock text would pass even after a concurrent append,
            # silently losing it.
            prev_row = self._row(conn, prev_handle)
            if prev_row is None or prev_row[6] is not None:
                raise Gone(
                    f"¶{_bare(prev_handle)} is retired — it was removed or "
                    "replaced; re-read the current previous block"
                )
            row = self._row(conn, handle)
            if row is None or row[6] is not None:
                return self.get_draft_chunk(
                    prev_handle
                )  # retired meanwhile — idempotent
            if (row[3], prev_row[3]) != (parent, prev_parent):
                raise BadInput(
                    f"¶{_bare(handle)}/¶{_bare(prev_handle)} moved while the "
                    "merge waited for the section lock — re-read and retry"
                )
            if base_sha is not None:
                current = content_sha(prev_row[5])
                nb = base_sha.strip().lower()
                if len(nb) < 8:
                    raise BadInput(
                        f"base_sha {base_sha!r} too short — need ≥8 hex chars "
                        "(the sha prefix shown on read)",
                        next=f"get(kind='draft', id='¶{_bare(prev_handle)}') for the sha",
                    )
                if not current.startswith(nb):
                    raise BadInput(
                        f"¶{_bare(prev_handle)} changed since you read it "
                        f"(you read {nb[:8]}…, now {current[:8]}…) — "
                        "re-read and retry so you don't clobber the newer edit",
                        next=(
                            f"get(kind='draft', id='¶{_bare(prev_handle)}') for the "
                            "current text + sha, then retry the merge"
                        ),
                    )
            # Childless-leaf guard, mirroring retire_chunk's own (no
            # cascade/promote here — a merge only ever retires a leaf; a
            # chunk with children just fails the guard, same as before this
            # atomic op existed).
            kids = self._children(conn, ref_id, chunk_id)
            if kids:
                raise BadInput(
                    "retiring a chunk with children requires "
                    "mode='cascade' (delete contents) or "
                    "mode='promote' (keep contents)"
                )
            live = self._live_count(conn, ref_id)
            if live <= 1:
                raise BadInput("cannot retire the last live chunk of a draft")
            if text:
                new_text = (prev_row[5] or "") + text
                sha = content_sha(new_text)
                conn.execute(
                    "UPDATE chunks SET text = %s, content_sha = %s WHERE chunk_id = %s",
                    (new_text, sha, prev_chunk_id),
                )
                conn.execute(
                    """INSERT INTO chunk_events
                           (chunk_id, event_kind, content_sha, prev_text, source)
                       VALUES (%s, 'edited', %s, %s, %s)""",
                    (prev_chunk_id, sha, prev_row[5], Jsonb(source or {})),
                )
            conn.execute(
                "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s",
                (chunk_id,),
            )
            self._log(conn, chunk_id, "retired", source, None)
        return self.get_draft_chunk(prev_handle)

    def _resolve_move(
        self,
        conn: psycopg.Connection,
        ref_id: int,
        move: dict[str, Any] | None,
        *,
        moving_id: int,
    ) -> tuple[int | None, str | None, str | None]:
        """Like ``_resolve_at`` but for an existing chunk — excludes
        ``moving_id`` from the sibling computation."""
        move = move or {}
        anchor = move.get("before") or move.get("after")
        if anchor is not None:
            tgt = self.get_draft_chunk(_bare(anchor))
            if tgt is None:
                raise NotFound(f"move: no draft chunk ¶{_bare(anchor)}")
            sibs = [
                s
                for s in self._children(conn, ref_id, tgt.parent_chunk_id)
                if s.chunk_id != moving_id
            ]
            idx = next(
                (i for i, s in enumerate(sibs) if s.chunk_id == tgt.chunk_id), None
            )
            if idx is None:  # anchor retired → recover into its ghost slot
                lo, hi = self._ghost_bracket(sibs, tgt, before="before" in move)
                return tgt.parent_chunk_id, lo, hi
            if "before" in move:
                lo = sibs[idx - 1].pos if idx > 0 else None
                hi = tgt.pos
            else:
                lo = tgt.pos
                hi = sibs[idx + 1].pos if idx + 1 < len(sibs) else None
            return tgt.parent_chunk_id, lo, hi
        into = move.get("into")
        if into is not None:
            parent = self.get_draft_chunk(_bare(into))
            if parent is None:
                raise NotFound(f"move: no parent chunk ¶{_bare(into)}")
            kids = [
                k
                for k in self._children(conn, ref_id, parent.chunk_id)
                if k.chunk_id != moving_id
            ]
            if move.get("first"):
                return parent.chunk_id, None, (kids[0].pos if kids else None)
            return parent.chunk_id, (kids[-1].pos if kids else None), None
        roots = [
            r for r in self._children(conn, ref_id, None) if r.chunk_id != moving_id
        ]
        if move.get("first"):
            return None, None, (roots[0].pos if roots else None)
        return None, (roots[-1].pos if roots else None), None


def _bare(handle: str) -> str:
    """Strip a leading ``¶`` sigil from a chunk handle if present."""
    return handle[1:] if handle.startswith("¶") else handle


def _image_dims(data: bytes) -> tuple[int | None, int | None]:
    """Best-effort ``(width, height)`` via Pillow; ``(None, None)`` when
    Pillow is absent or the bytes don't parse. Pillow is a transitive dep
    (marker) but optional on a host without the ``[paper]`` extra, so this
    never hard-fails — dimensions are a nicety, not a contract."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None, None

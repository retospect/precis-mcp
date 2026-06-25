"""Store ops for the editable `draft` kind (ADR 0033).

Drafts use chunk columns the append-only ingest path never touches —
`handle` (opaque anchor), `pos` (sibling-scoped fractional order),
`parent_chunk_id` (adjacency-list hierarchy), `content_sha`,
`retired_at` — so they get their own mixin rather than overloading
`insert_blocks`. Every structural write logs a `chunk_events` row.

This module ships the create / add / read core; edit / move / retire
land alongside as the handler grows.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from precis.errors import BadInput
from precis.utils import handle_registry
from precis.utils.fractional import key_between, n_keys_between
from precis.utils.handles import new_handle

_HANDLE_RETRIES = 6

#: Above this many characters of concatenated prose, skip the inline
#: Schwartz-Hearst abbreviation scan in ``defined_abbrevs`` (regex over the
#: whole draft — multi-second on a 1M+ char draft). Explicit ``term``
#: chunks still populate the glossary; only the auto-detected inline pairs
#: are dropped for very large drafts.
_ABBREV_INLINE_SCAN_CAP = 300_000


def content_sha(text: str) -> str:
    """Hash of the resolved-for-search text (markers are stripped later;
    for now the raw source). Drives per-consumer re-derivation."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DraftChunk:
    chunk_id: int
    ref_id: int
    handle: str  # legacy ADR-0033 base-58 anchor (internal key, retiring)
    chunk_kind: str
    text: str
    pos: str
    parent_chunk_id: int | None
    depth: int
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def dc(self) -> str:
        """ADR 0036 universal handle for this draft chunk (e.g. ``dc42``).
        The agent-facing address; supersedes the legacy ``¶<base58>``."""
        return handle_registry.format_handle("draft", self.chunk_id, chunk=True)


@dataclass(frozen=True, slots=True)
class TocEntry:
    """A heading in the table of contents (the document skeleton),
    enriched with its gist (llm summary) and keywords when present.
    ``depth`` is relative to the TOC root; the §-number is computed by
    the renderer from the depth sequence."""

    handle: str  # legacy base-58 anchor (internal)
    depth: int
    title: str
    keywords: list[str]
    gist: str | None
    chunk_id: int = 0

    @property
    def dc(self) -> str:
        """ADR 0036 universal handle for this heading (e.g. ``dc42``)."""
        return handle_registry.format_handle("draft", self.chunk_id, chunk=True)


@dataclass(frozen=True, slots=True)
class DraftWorkItem:
    """An open todo working on this draft (walked draft→project→subtree),
    with the status of its child jobs and whether it is *blocked* by a
    failure-bubble. Surfaces in the draft outline so a stuck enrichment
    job is visible from the draft, not just buried in the task tree."""

    todo_id: int
    title: str
    blocked: bool  # carries an OPEN:child-failed:* bubble
    jobs: tuple[tuple[int, str], ...]  # (job_ref_id, status) for child jobs


def _split_blocks(text: str) -> list[str]:
    """Split a multi-paragraph `put` at blank-line boundaries; trim.
    (Block elements like fenced code aren't special-cased yet.)"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = [p.strip() for p in text.split("\n\n")]
    blocks = [p for p in parts if p]
    return blocks or [text.strip()]


class DraftMixin:
    """Mixin on :class:`precis.store.store.Store` — draft chunk ops."""

    # provided by Store
    pool: Any
    tx: Any
    insert_ref: Any
    add_link: Any

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
    ) -> DraftChunk:
        """Insert one draft chunk: mint a unique handle (savepoint-retry),
        assign an insertion-serial `ord`, set pos/parent/content_sha/meta,
        and log a `created` event. ``meta`` carries e.g. a ``term``'s
        ``{short, long, surface_forms}`` or a ``figure``'s provenance."""
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
        )

    # -- lookups -------------------------------------------------------------

    def draft_subtree_chunk_ids(self, handle: str) -> list[int]:
        """Chunk ids of the subtree rooted at ``handle`` — the chunk
        itself plus all live descendants. Empty when the handle is
        unknown. Used to scope draft search to one section."""
        chunk = self.get_draft_chunk(handle)
        if chunk is None:
            return []
        with self.pool.connection() as conn:
            return [chunk.chunk_id, *self._descendant_ids(conn, chunk.chunk_id)]

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

    def draft_handles_for(self, chunk_ids: list[int]) -> dict[int, str]:
        """Map ``chunk_id → ¶-less handle`` for a set of draft chunks —
        search hits carry ``chunk_id`` (``Block.id``) but not the draft
        handle (which lives in ``chunks.handle``, not ``meta->>'slug'``)."""
        if not chunk_ids:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, handle FROM chunks WHERE chunk_id = ANY(%s)",
                (list(chunk_ids),),
            ).fetchall()
        return {int(r[0]): str(r[1]) for r in rows}

    def draft_chunk_meta(self, handle: str) -> dict[str, Any]:
        """The raw ``chunks.meta`` JSON for a draft chunk (``{}`` if none).
        Not on :class:`DraftChunk` — read it when re-deriving a table's
        markdown from its canonical ``meta.table`` (ADR 0035 §1)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta FROM chunks WHERE handle = %s", (_bare(handle),)
            ).fetchone()
        return dict(row[0]) if row and row[0] else {}

    def soft_delete_draft(self, ref_id: int) -> int:
        """Soft-delete a whole draft **atomically**: mark the draft ref
        ``deleted_at`` and retire all its live chunks, in one transaction.
        Recoverable (clear ``deleted_at`` + ``retired_at`` to restore).
        Returns the number of chunks retired. Raises if the ref isn't a
        live draft."""
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
        ``lc..`` plaintext, ``mc..`` markdown, …) to its owning ref +
        position + text — the cross-kind generalisation of
        ``get_draft_chunk`` for the reader's hover-preview / click-through.
        Returns ``{kind, ref_id, ord, chunk_kind, text}`` or ``None`` when
        the handle isn't a chunk handle or the chunk doesn't exist (so a
        dangling ``pc999`` degrades to a graceful 'missing' popover)."""
        parsed = handle_registry.parse(handle.strip())
        if parsed is None or not parsed[1]:  # not a chunk handle
            return None
        kind, _is_chunk, chunk_id = parsed
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id, ord, chunk_kind, text FROM chunks "
                "WHERE chunk_id = %s",
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

    def get_draft_chunk(self, handle: str) -> DraftChunk | None:
        """A single live-or-retired draft chunk by its address.

        Accepts the ADR 0036 universal handle ``dc<chunk_id>`` (looked up by
        ``chunk_id``) or the legacy ADR-0033 ``¶<base58>`` / bare base-58
        anchor (looked up by ``chunks.handle``)."""
        parsed = handle_registry.parse(handle.strip())
        if parsed is not None and parsed[0] == "draft" and parsed[1]:
            where, key = "chunk_id = %s", parsed[2]
        else:
            where, key = "handle = %s", _bare(handle)
        with self.pool.connection() as conn:
            row = conn.execute(
                f"""SELECT chunk_id, handle, chunk_kind, text, pos,
                          parent_chunk_id, ref_id, meta
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
        )

    def draft_relative_chunk_ids(self, addr: str) -> list[int] | None:
        """Resolve an ADR 0036 relative draft handle to target chunk id(s).

        ``dc<id>^N`` walks ``N`` ancestors (via ``parent_chunk_id``);
        ``dc<id>+N`` / ``-N`` steps ``N`` siblings (ordered by ``pos`` under
        the same parent); ``dc<id>-lo..hi`` is the signed sibling span (the
        reading-context window). Returns the target ids (one for a
        step/ancestor, the contiguous sibling run for a span), an **empty
        list** when the target is out of range / past the root, or ``None``
        when ``addr`` is not a relative draft handle (so the caller can try
        the absolute path).
        """
        parsed = handle_registry.parse_relative(addr)
        if parsed is None:
            return None
        kind, _is_chunk, chunk_id, op = parsed
        if kind != "draft":
            return None
        base = self.get_draft_chunk(
            handle_registry.format_handle("draft", chunk_id, chunk=True)
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
            )
            for r in rows
        ]

    def reading_order(self, ref_id: int) -> list[DraftChunk]:
        """All live chunks of a draft in DFS reading order (roots by pos,
        recurse into children by pos), with depth.

        The order is built in Python from one **flat, indexed** fetch — not
        a recursive SQL CTE. The CTE (``WITH RECURSIVE walk``) couldn't
        index-seek its worktable join, so at each recursion level it
        re-scanned every chunk of the ref: ≈O(N·depth), ~5.5s on a
        9,700-chunk draft, and it dominated the reader's load time. A single
        ``chunks_ref_id_idx`` scan + this DFS is milliseconds.

        Ordering matches the old ``sort_path COLLATE "C"``: siblings sort by
        ``pos`` (base-62 fractional keys; Python str compare is code-point =
        byte order), and DFS pre-order puts a parent before its subtree and a
        subtree before the next sibling. Chunks reachable only through a
        retired/absent parent are excluded — same as the CTE, which could
        only walk live chunks down from a NULL-parent root."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id, handle, chunk_kind, text, pos, "
                "       parent_chunk_id, meta "
                "  FROM chunks "
                " WHERE ref_id = %s AND retired_at IS NULL AND pos IS NOT NULL",
                (ref_id,),
            ).fetchall()
        by_id = {r[0]: r for r in rows}
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
                )
            )
            kids = children.get(r[0])
            if kids:
                stack.extend((k, depth + 1) for k in reversed(kids))
        return out

    def chunk_connections(
        self, ref_id: int, handles: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Per-chunk graph connections — every ref linked *to or from* a
        chunk (the other end of any ``links`` row whose src/dst chunk is
        this one), grouped by handle. This is where ``derived-from``
        provenance and dream-memories that reference a paragraph surface
        in the reader. Each entry: ``{relation, direction, kind, ident,
        title}`` (``ident`` = slug or numeric id; ``title`` is the terse
        descriptor). Deduped per (handle, other-ref, relation)."""
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

        ``summary`` is the ``llm-v1`` two-part summary (``chunk_summaries``);
        ``keywords`` the comma-joined KeyBERT terms (``chunks.keywords``,
        first 12). Either is ``''`` for a chunk the ``llm_summarize`` /
        ``chunk_keywords`` workers haven't reached yet — callers fall back
        (summary → keywords → truncated text). Shared by the web reader's
        view slider and the handler's outline render.

        ``handles`` scopes the result to just those blocks — the on-demand
        row path loads one block at a time and must not re-scan the whole
        (possibly massive) draft per row. ``None`` means the whole draft."""
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
                raise ValueError(f"at: unknown chunk handle {anchor!r}")
            sibs = self._children(conn, ref_id, tgt.parent_chunk_id)
            idx = next(i for i, s in enumerate(sibs) if s.chunk_id == tgt.chunk_id)
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
                raise ValueError(f"at: unknown parent handle {into!r}")
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
        """Open todos working on this draft, blocked-first, capped.

        Walks ``draft → (draft-of) → project root → todo subtree`` and
        returns the open todos that are *blocked* (carry an
        ``OPEN:child-failed:*`` bubble) or have a non-succeeded child
        job (running / queued / failed) — i.e. work that is stuck or in
        flight. This is the edge the draft view follows so a failed
        enrichment job registers on the draft, instead of silently
        parking the task out of the rotation. Clean, fully-done work is
        omitted (no signal to surface)."""
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
                )
                SELECT o.ref_id, o.title,
                       (b.n IS NOT NULL) AS blocked,
                       COALESCE(
                           jsonb_agg(
                               jsonb_build_array(jb.job_id, jb.status)
                               ORDER BY jb.job_id
                           ) FILTER (WHERE jb.job_id IS NOT NULL),
                           '[]'::jsonb
                       ) AS jobs
                  FROM open_todos o
                  LEFT JOIN bubbles b ON b.ref_id = o.ref_id
                  LEFT JOIN jobs jb ON jb.todo_id = o.ref_id
                 GROUP BY o.ref_id, o.title, b.n
                HAVING b.n IS NOT NULL
                    OR bool_or(jb.status IN ('running', 'queued', 'failed'))
                 ORDER BY (b.n IS NOT NULL) DESC, o.ref_id
                 LIMIT %(limit)s
                """,
                {"draft": draft_ref_id, "limit": int(limit)},
            ).fetchall()
        items: list[DraftWorkItem] = []
        for ref_id, title, blocked, jobs in rows:
            first = (title or "").strip().splitlines()[0] if title else ""
            items.append(
                DraftWorkItem(
                    todo_id=int(ref_id),
                    title=first,
                    blocked=bool(blocked),
                    jobs=tuple((int(j[0]), str(j[1])) for j in (jobs or [])),
                )
            )
        return items

    def create_draft(
        self,
        *,
        name: str,
        title: str,
        project_ref_id: int,
        meta: dict[str, Any] | None = None,
    ) -> tuple[Any, DraftChunk]:
        """Create a draft ref bound 1:1 to its project, born with a title
        `heading` chunk so it is never empty. Returns (ref, title_chunk)."""
        with self.tx() as conn:
            dup = conn.execute(
                "SELECT 1 FROM links WHERE dst_ref_id = %s AND relation = 'draft-of'",
                (project_ref_id,),
            ).fetchone()
            if dup is not None:
                raise ValueError(f"project ref {project_ref_id} already has a draft")
            ref = self.insert_ref(
                kind="draft",
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
            )
            self.add_link(
                src_ref_id=ref.id,
                dst_ref_id=project_ref_id,
                relation="draft-of",
                conn=conn,
            )
        return ref, title_chunk

    def add_chunks(
        self,
        *,
        ref_id: int,
        chunk_kind: str,
        text: str,
        at: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        split: bool = True,
    ) -> list[DraftChunk]:
        """Add one or more chunks (a multi-paragraph `text` splits at blank
        lines). Returns the created chunks in order. ``meta`` (e.g. a
        ``term``'s ``{short, long}``) is stamped on each created chunk.

        ``split=False`` inserts ``text`` verbatim as a single chunk — used
        by chunks whose text is a derived projection that must not
        fragment (a ``table``'s markdown render, ADR 0035 §1)."""
        blocks = _split_blocks(text) if split else [text]
        with self.tx() as conn:
            parent, lo, hi = self._resolve_at(conn, ref_id, at)
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
        """Add a single ``figure`` chunk (ADR 0034): the caption is the
        face (``text`` — embedded, searchable), the image bytes go to
        ``chunk_blobs``, and ``meta.figure`` carries ``origin`` plus any
        provenance (e.g. the third-party ``permission`` paper-trail).

        Unlike :meth:`add_chunks` the caption is **not** split at blank
        lines — a figure is one chunk. Both writes share one transaction,
        so a figure never lands without its bytes."""
        sha = hashlib.sha256(image).hexdigest()
        width, height = _image_dims(image)
        fig = {"origin": origin, **(figure_meta or {})}
        with self.tx() as conn:
            parent, lo, hi = self._resolve_at(conn, ref_id, at)
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

    def upsert_chunk_blob(
        self,
        chunk_id: int,
        image: bytes,
        mime: str,
        *,
        conn: psycopg.Connection | None = None,
    ) -> None:
        """Insert or **replace** a chunk's blob (`chunk_blobs` row).

        Unlike :meth:`add_figure` (insert-only, at figure creation), this is
        the render path: a computed figure's image is a *regenerable* artifact
        (ADR 0035 §3), so re-rendering overwrites the bytes in place keyed on
        ``chunk_id``. Re-derives ``sha256`` / ``size`` / dims from the bytes."""
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
        """Everything the render pass needs for a computed `figure` (ADR 0035):
        its render recipe (`meta.render`) and, in plotted order, the `meta.table`
        payload + `content_sha` of each data chunk it `plots`.

        Returns ``None`` when the chunk isn't a figure carrying a `meta.render`
        recipe (i.e. a plain uploaded *image* figure, not a *graph*). The
        returned ``input_shas`` (render src + each data sha) are the inputs to
        the content-addressed invalidation key."""
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
        ``meta.render.cached_key`` (ADR 0035 §3) — a later mark-stale pass
        compares it to the recomputed `hash(src, plotted data shas)`."""
        with self.tx() as conn:
            conn.execute(
                "UPDATE chunks SET meta = "
                "jsonb_set(meta, '{render,cached_key}', to_jsonb(%s::text), true) "
                "WHERE chunk_id = %s",
                (cached_key, figure_chunk_id),
            )

    def set_render_recipe(
        self,
        chunk_id: int,
        recipe: dict[str, Any],
        *,
        conn: psycopg.Connection | None = None,
    ) -> None:
        """Stamp a figure chunk's `meta.render` recipe (the graph code). Set at
        creation of a computed figure and rewritten on a recipe edit; a rewrite
        clears any prior `cached_key`, so the figure is stale until re-rendered.
        Logs a `recipe` chunk_event (ADR 0035 §2 — recipe history)."""

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
        """Create the figure→data `plots` edges (chunk→chunk) for a computed
        figure, by chunk_id. Resolves each chunk's `(ref_id, ord)` and routes
        through :meth:`add_link` (dedup + validation). Returns the count.
        All chunks must already exist (the caller validated the draft)."""
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
                self.add_link(
                    src_ref_id=fig_ref,
                    src_pos=fig_ord,
                    dst_ref_id=d_ref,
                    dst_pos=d_ord,
                    relation="plots",
                    conn=conn,
                )
                n += 1
            return n

    def set_figure_provenance(
        self,
        handle: str,
        *,
        permission: dict[str, Any] | None = None,
        origin: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> DraftChunk | None:
        """Update a figure chunk's provenance meta in place (ADR 0034):
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
                raise ValueError(f"unknown chunk handle {handle!r}")
            chunk_id, chunk_kind, meta, retired = row
            if chunk_kind != "figure":
                raise BadInput(f"¶{_bare(handle)} is a {chunk_kind}, not a figure")
            if retired is not None:
                raise ValueError(f"chunk {handle!r} is retired")
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
    ) -> DraftChunk | None:
        """In-place text edit: bump `content_sha`, log an `edited` event with
        `prev_text`. The handle (and references to it) survive; derived data
        re-derives on the sha mismatch.

        Optimistic concurrency: pass ``base_sha`` (the ``content_sha`` the
        caller saw when it read the chunk) to fail the edit if the chunk
        changed underneath it — so two agents editing the same chunk don't
        silently clobber each other. Omit it for a force-overwrite.

        ``meta_patch`` shallow-merges into ``chunks.meta`` (``meta || patch``,
        NULL-safe) in the same statement — used to update a ``table``'s
        canonical ``meta.table`` alongside its re-derived markdown ``text``
        atomically (ADR 0035 §1).
        """
        sha = content_sha(text)
        with self.tx() as conn:
            row = self._row(conn, handle)
            if row is None:
                raise ValueError(f"unknown chunk handle {handle!r}")
            if row[6] is not None:
                raise ValueError(f"chunk {handle!r} is retired")
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
        return self.get_draft_chunk(handle)

    def move_chunk(
        self,
        handle: str,
        move: dict[str, Any],
        *,
        source: dict[str, Any] | None = None,
    ) -> DraftChunk | None:
        """Reorder / reparent a chunk (its subtree follows). Writes `pos` +
        `parent_chunk_id`, logs a `moved`/`reparented` event. No text change
        → no re-embed."""
        with self.tx() as conn:
            row = self._row(conn, handle)
            if row is None:
                raise ValueError(f"unknown chunk handle {handle!r}")
            if row[6] is not None:
                raise ValueError(f"chunk {handle!r} is retired")
            chunk_id, ref_id, old_parent, old_pos = row[0], row[1], row[3], row[4]
            new_parent, lo, hi = self._resolve_move(
                conn, ref_id, move, moving_id=chunk_id
            )
            if new_parent is not None:
                forbidden = {chunk_id, *self._descendant_ids(conn, chunk_id)}
                if new_parent in forbidden:
                    raise ValueError(
                        "cannot move a chunk under itself or its own subtree"
                    )
            new_pos = key_between(lo, hi)
            conn.execute(
                "UPDATE chunks SET pos = %s, parent_chunk_id = %s WHERE chunk_id = %s",
                (new_pos, new_parent, chunk_id),
            )
            kind = "reparented" if new_parent != old_parent else "moved"
            self._log(
                conn,
                chunk_id,
                kind,
                source,
                {
                    "from": {"parent": old_parent, "pos": old_pos},
                    "to": {"parent": new_parent, "pos": new_pos},
                },
            )
        return self.get_draft_chunk(handle)

    def retire_chunk(
        self,
        handle: str,
        *,
        mode: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> None:
        """Soft-delete (retire) a chunk. A chunk with live children needs
        `mode='cascade'` (retire the subtree) or `'promote'` (lift the
        children to the parent). Refuses to retire the last live chunk."""
        with self.tx() as conn:
            row = self._row(conn, handle)
            if row is None:
                raise ValueError(f"unknown chunk handle {handle!r}")
            if row[6] is not None:
                return  # already retired — idempotent
            chunk_id, ref_id, parent = row[0], row[1], row[3]
            kids = self._children(conn, ref_id, chunk_id)
            live = self._live_count(conn, ref_id)
            if kids:
                if mode not in ("cascade", "promote"):
                    raise ValueError(
                        "retiring a chunk with children requires "
                        "mode='cascade' (delete contents) or "
                        "mode='promote' (keep contents)"
                    )
                if mode == "cascade":
                    subtree = [chunk_id, *self._descendant_ids(conn, chunk_id)]
                    if len(subtree) >= live:
                        raise ValueError(
                            "cannot retire the whole draft (last live chunks)"
                        )
                    conn.execute(
                        "UPDATE chunks SET retired_at = now() WHERE chunk_id = ANY(%s)",
                        (subtree,),
                    )
                    self._log(conn, chunk_id, "retired", source, {"mode": "cascade"})
                else:  # promote — splice children into the parent's slot
                    sibs = self._children(conn, ref_id, parent)
                    idx = next(i for i, s in enumerate(sibs) if s.chunk_id == chunk_id)
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
                    raise ValueError("cannot retire the last live chunk of a draft")
                conn.execute(
                    "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s",
                    (chunk_id,),
                )
                self._log(conn, chunk_id, "retired", source, None)

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
                raise ValueError(f"move: unknown chunk handle {anchor!r}")
            sibs = [
                s
                for s in self._children(conn, ref_id, tgt.parent_chunk_id)
                if s.chunk_id != moving_id
            ]
            idx = next(i for i, s in enumerate(sibs) if s.chunk_id == tgt.chunk_id)
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
                raise ValueError(f"move: unknown parent handle {into!r}")
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


class _AbbrevMixin:
    """Abbreviation detection + ignore-list ops (mixed into Store with
    DraftMixin). Split out only to keep the abbrev concern legible."""

    pool: Any
    tx: Any
    add_chunks: Any  # provided by DraftMixin

    def ensure_glossary_heading(self, ref_id: int) -> str:
        """``dc<chunk_id>`` handle of the draft's "Glossary" heading (ADR
        0036), creating it (at the end) if absent. Glossary ``term`` chunks
        file under it."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'heading' AND retired_at IS NULL "
                "AND pos IS NOT NULL AND lower(text) = 'glossary' LIMIT 1",
                (ref_id,),
            ).fetchone()
        if row:
            return handle_registry.format_handle("draft", int(row[0]), chunk=True)
        created = self.add_chunks(
            ref_id=ref_id, chunk_kind="heading", text="Glossary", at={"last": True}
        )
        return str(created[0].handle)

    def undefined_abbrevs(self, ref_id: int, text: str) -> list[str]:
        """Acronym-shaped tokens in ``text`` that aren't yet defined for
        this draft — i.e. not a ``term`` chunk's ``short``, not an inline
        ``Long Form (ABBR)`` definition anywhere in the prose, and not on
        the ``meta.abbrev_ignore`` list. The set the write-hint complains
        about; opus then defines or marks not-an-abbrev."""
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
        draft — explicit ``term`` chunks (``meta.short`` → chunk text) plus
        inline ``Long Form (ABBR)`` first-uses found anywhere in the prose
        (Schwartz-Hearst). Explicit terms win on a clash. Drives the
        reader's recall highlight: every occurrence of a known ``short``
        gets a hover-definition. Empty when nothing is defined yet."""
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

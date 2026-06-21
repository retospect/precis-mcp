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
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from precis.errors import BadInput
from precis.utils.fractional import key_between, n_keys_between
from precis.utils.handles import new_handle

_HANDLE_RETRIES = 6


def content_sha(text: str) -> str:
    """Hash of the resolved-for-search text (markers are stripped later;
    for now the raw source). Drives per-consumer re-derivation."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DraftChunk:
    chunk_id: int
    ref_id: int
    handle: str
    chunk_kind: str
    text: str
    pos: str
    parent_chunk_id: int | None
    depth: int


@dataclass(frozen=True, slots=True)
class TocEntry:
    """A heading in the table of contents (the document skeleton),
    enriched with its gist (llm summary) and keywords when present.
    ``depth`` is relative to the TOC root; the §-number is computed by
    the renderer from the depth sequence."""

    handle: str
    depth: int
    title: str
    keywords: list[str]
    gist: str | None


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
        source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> DraftChunk:
        """Insert one draft chunk: mint a unique handle (savepoint-retry),
        assign an insertion-serial `ord`, set pos/parent/content_sha/meta,
        and log a `created` event. ``meta`` carries e.g. a ``term``'s
        ``{short, long, surface_forms}``."""
        sha = content_sha(text)
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
                            Jsonb(meta or {}),
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
        )

    # -- lookups -------------------------------------------------------------

    def get_draft_chunk(self, handle: str) -> DraftChunk | None:
        """A single live-or-retired draft chunk by its handle."""
        with self.pool.connection() as conn:
            row = conn.execute(
                """SELECT chunk_id, handle, chunk_kind, text, pos,
                          parent_chunk_id, ref_id
                     FROM chunks WHERE handle = %s""",
                (_bare(handle),),
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
        )

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
        recurse into children by pos), with depth."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                -- sort_path = '/'-joined pos chain; COLLATE "C" so the
                -- fractional keys sort by byte order (not DB locale).
                -- '/' (0x2F) sorts below every base-62 char, so a parent
                -- precedes its children and a subtree precedes the next
                -- sibling — i.e. DFS reading order.
                WITH RECURSIVE walk AS (
                    SELECT chunk_id, handle, chunk_kind, text, pos,
                           parent_chunk_id, pos AS sort_path, 0 AS depth
                      FROM chunks
                     WHERE ref_id = %s AND parent_chunk_id IS NULL
                       AND retired_at IS NULL AND pos IS NOT NULL
                    UNION ALL
                    SELECT c.chunk_id, c.handle, c.chunk_kind, c.text, c.pos,
                           c.parent_chunk_id, w.sort_path || '/' || c.pos,
                           w.depth + 1
                      FROM chunks c JOIN walk w ON c.parent_chunk_id = w.chunk_id
                     WHERE c.ref_id = %s AND c.retired_at IS NULL
                       AND c.pos IS NOT NULL
                )
                SELECT chunk_id, handle, chunk_kind, text, pos,
                       parent_chunk_id, depth
                  FROM walk ORDER BY sort_path COLLATE "C" ASC
                """,
                (ref_id, ref_id),
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
                depth=r[6],
            )
            for r in rows
        ]

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
                           AND s.summarizer = 'llm-v1' LIMIT 1) AS gist
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
    ) -> list[DraftChunk]:
        """Add one or more chunks (a multi-paragraph `text` splits at blank
        lines). Returns the created chunks in order. ``meta`` (e.g. a
        ``term``'s ``{short, long}``) is stamped on each created chunk."""
        blocks = _split_blocks(text)
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
    ) -> DraftChunk | None:
        """In-place text edit: bump `content_sha`, log an `edited` event with
        `prev_text`. The handle (and references to it) survive; derived data
        re-derives on the sha mismatch.

        Optimistic concurrency: pass ``base_sha`` (the ``content_sha`` the
        caller saw when it read the chunk) to fail the edit if the chunk
        changed underneath it — so two agents editing the same chunk don't
        silently clobber each other. Omit it for a force-overwrite.
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
                if current != base_sha:
                    raise BadInput(
                        f"¶{_bare(handle)} changed since you read it "
                        f"(you read {base_sha[:8]}…, now {current[:8]}…) — "
                        "re-read and retry so you don't clobber the newer edit",
                        next=(
                            f"get(kind='draft', id='¶{_bare(handle)}') for the "
                            "current text + sha, then edit with the new base_sha="
                        ),
                    )
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
        """Handle of the draft's "Glossary" heading, creating it (at the
        end) if absent. Glossary ``term`` chunks file under it."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT handle FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = 'heading' AND retired_at IS NULL "
                "AND pos IS NOT NULL AND lower(text) = 'glossary' LIMIT 1",
                (ref_id,),
            ).fetchone()
        if row:
            return str(row[0])
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
            # Inline pairs first; explicit term chunks overwrite them.
            if prow and prow[0]:
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

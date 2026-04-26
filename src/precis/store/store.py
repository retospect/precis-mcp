"""Sync postgres-backed store (psycopg 3). One instance per server.

Phase 2 surface: corpus, ref CRUD, tag CRUD, system settings. Block /
link / cache / search methods land in subsequent phases as their
respective handlers come online.

All methods sync. Each method acquires a connection from the pool for
its work; callers needing multi-statement atomicity use `Store.tx()`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Self

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput, NotFound
from precis.store.pool import create_pool
from precis.store.types import ActorSlug, Ref, Tag

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tag prefixes managed by `system` actor — not freely settable by `agent`.
# Mirrored from `tag_prefixes.writable_by` for fast checks; the DB still
# enforces. Kept in sync with `0001_initial.sql`.
# ---------------------------------------------------------------------------
_AGENT_WRITABLE_PREFIXES: frozenset[str] = frozenset({"STATUS", "PRIO", "CONFIDENCE"})
_SYSTEM_WRITABLE_PREFIXES: frozenset[str] = frozenset({"SRC", "CACHE", "DENSITY"})

# Sentinel: pos = -1 in the DB means "ref-level"; callers see None.
_REF_LEVEL_POS = -1


def _pos_to_db(pos: int | None) -> int:
    """Translate caller's None (= ref-level) to DB sentinel -1."""
    return _REF_LEVEL_POS if pos is None else pos


class Store:
    """High-level handle. Owns the psycopg connection pool."""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def connect(
        cls,
        dsn: str,
        *,
        min_size: int = 2,
        max_size: int = 10,
    ) -> Self:
        pool = create_pool(dsn, min_size=min_size, max_size=max_size)
        return cls(pool)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        """Acquire a connection inside an explicit transaction. Auto-commit
        on clean exit; rollback on exception."""
        with self.pool.connection() as conn:
            with conn.transaction():
                yield conn

    # -- system table --------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT value FROM system WHERE key = %s", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO system (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE "
                "SET value = EXCLUDED.value, updated_at = now()",
                (key, value),
            )

    def embedding_dim(self) -> int:
        v = self.get_setting("embedding_dim")
        if v is None:
            raise RuntimeError(
                "system.embedding_dim is unset; migrations may not have run"
            )
        return int(v)

    # -- corpus --------------------------------------------------------------

    def get_corpus(self, slug: str) -> int | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT id FROM corpuses WHERE slug = %s", (slug,)
            ).fetchone()
        return row[0] if row else None

    def ensure_corpus(self, slug: str, *, title: str | None = None) -> int:
        """Idempotent: returns existing id, or creates a new corpus."""
        existing = self.get_corpus(slug)
        if existing is not None:
            return existing
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO corpuses (slug, title) VALUES (%s, %s) "
                "ON CONFLICT (slug) DO UPDATE SET title = corpuses.title "
                "RETURNING id",
                (slug, title or slug),
            ).fetchone()
        assert row is not None
        return row[0]

    # -- refs ----------------------------------------------------------------

    def insert_ref(
        self,
        *,
        corpus_id: int,
        kind: str,
        slug: str | None,
        title: str,
        provider: str | None = None,
        meta: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> Ref:
        """Insert a ref. Slug rules:
        - Slug kinds (paper/book/oracle/conv/skill/quest): slug required.
        - Numeric kinds (todo/memory/gripe/fc): slug must be None.
        Enforced at app layer (DB CHECK can't subquery `kinds`)."""
        self._validate_slug_for_kind(kind, slug, conn=conn)

        sql = """
            INSERT INTO refs (corpus_id, kind, slug, title, provider, meta)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, corpus_id, kind, slug, title, provider, meta,
                      created_at, updated_at, deleted_at
        """
        params = (corpus_id, kind, slug, title, provider, Jsonb(meta or {}))

        if conn is not None:
            row = conn.execute(sql, params).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, params).fetchone()
        assert row is not None
        return _row_to_ref(row)

    def get_ref(
        self,
        *,
        kind: str,
        id: int | str,
        include_deleted: bool = False,
    ) -> Ref | None:
        """Look up by (kind, public id). Public id = slug for slug kinds,
        int(refs.id) for numeric kinds."""
        if isinstance(id, int):
            sql = (
                "SELECT id, corpus_id, kind, slug, title, provider, meta, "
                "       created_at, updated_at, deleted_at "
                "FROM refs WHERE kind = %s AND id = %s"
            )
            params: tuple[Any, ...] = (kind, id)
        else:
            sql = (
                "SELECT id, corpus_id, kind, slug, title, provider, meta, "
                "       created_at, updated_at, deleted_at "
                "FROM refs WHERE kind = %s AND slug = %s"
            )
            params = (kind, id)
        if not include_deleted:
            sql += " AND deleted_at IS NULL"

        with self.pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return _row_to_ref(row) if row is not None else None

    def update_ref(
        self,
        ref_id: int,
        *,
        title: str | None = None,
        meta_patch: dict[str, Any] | None = None,
    ) -> Ref:
        """Patch title and/or merge new keys into meta."""
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE refs SET
                    title = COALESCE(%s, title),
                    meta  = CASE WHEN %s::jsonb IS NULL
                                 THEN meta
                                 ELSE meta || %s::jsonb
                            END,
                    updated_at = now()
                WHERE id = %s AND deleted_at IS NULL
                RETURNING id, corpus_id, kind, slug, title, provider, meta,
                          created_at, updated_at, deleted_at
                """,
                (
                    title,
                    Jsonb(meta_patch) if meta_patch is not None else None,
                    Jsonb(meta_patch) if meta_patch is not None else None,
                    ref_id,
                ),
            ).fetchone()
        if row is None:
            raise NotFound(
                f"ref id={ref_id} not found (or already deleted)",
                next=f"check id with: get(kind=..., id={ref_id})",
            )
        return _row_to_ref(row)

    def soft_delete_ref(self, ref_id: int) -> None:
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE refs SET deleted_at = now() "
                "WHERE id = %s AND deleted_at IS NULL",
                (ref_id,),
            )
            rowcount = cur.rowcount
        if rowcount == 0:
            raise NotFound(f"ref id={ref_id} not found (or already deleted)")

    def list_refs(
        self,
        *,
        corpus_id: int | None = None,
        kind: str | None = None,
        provider: str | None = None,
        updated_after: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Ref]:
        clauses = ["deleted_at IS NULL"]
        params: list[Any] = []
        if corpus_id is not None:
            params.append(corpus_id)
            clauses.append("corpus_id = %s")
        if kind is not None:
            params.append(kind)
            clauses.append("kind = %s")
        if provider is not None:
            params.append(provider)
            clauses.append("provider = %s")
        if updated_after is not None:
            params.append(updated_after)
            clauses.append("updated_at > %s")

        params.append(limit)
        params.append(offset)
        sql = (
            "SELECT id, corpus_id, kind, slug, title, provider, meta, "
            "       created_at, updated_at, deleted_at "
            "FROM refs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT %s OFFSET %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_ref(r) for r in rows]

    def search_refs_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[tuple[Ref, float]]:
        """Lexical search over `refs.title_tsv`. Returns (ref, rank) sorted
        by rank desc. Phase 3 will add semantic + RRF fusion."""
        clauses = ["r.deleted_at IS NULL", "r.title_tsv @@ qq.qq"]
        params: list[Any] = [q]
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        params.append(limit)
        sql = (
            "SELECT r.id, r.corpus_id, r.kind, r.slug, r.title, r.provider, "
            "       r.meta, r.created_at, r.updated_at, r.deleted_at, "
            "       ts_rank_cd(r.title_tsv, qq.qq) AS rank "
            "FROM refs r, websearch_to_tsquery('english', %s) qq(qq) "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY rank DESC LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        # rows are tuples in column order; rank is the last column.
        result: list[tuple[Ref, float]] = []
        for r in rows:
            ref = _row_to_ref(r[:10])
            result.append((ref, float(r[10])))
        return result

    # -- tags ----------------------------------------------------------------

    def add_tag(
        self,
        ref_id: int,
        tag: Tag,
        *,
        pos: int | None = None,
        set_by: ActorSlug = "agent",
        replace_prefix: bool = False,
    ) -> None:
        """Add a tag to a ref (or block, with `pos`).

        Args:
            replace_prefix: For closed tags only. If True, removes any
                existing closed tag with the same prefix before inserting.
                Used by the skill semantics ("CONFIDENCE:certain replaces
                previous CONFIDENCE:*").
        """
        db_pos = _pos_to_db(pos)
        with self.pool.connection() as conn:
            if tag.namespace == "closed":
                assert tag.prefix is not None
                if replace_prefix:
                    conn.execute(
                        "DELETE FROM ref_closed_tags "
                        "WHERE ref_id = %s AND pos = %s AND prefix = %s",
                        (ref_id, db_pos, tag.prefix),
                    )
                conn.execute(
                    "INSERT INTO ref_closed_tags "
                    "(ref_id, pos, prefix, value, set_by) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (ref_id, db_pos, tag.prefix, tag.value, set_by),
                )
            elif tag.namespace == "flag":
                conn.execute(
                    "INSERT INTO ref_flags (ref_id, pos, name, set_by) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (ref_id, db_pos, tag.value, set_by),
                )
            else:
                conn.execute(
                    "INSERT INTO ref_open_tags (ref_id, pos, value, set_by) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (ref_id, db_pos, tag.value, set_by),
                )

    def remove_tag(
        self,
        ref_id: int,
        tag: Tag,
        *,
        pos: int | None = None,
    ) -> None:
        db_pos = _pos_to_db(pos)
        with self.pool.connection() as conn:
            if tag.namespace == "closed":
                assert tag.prefix is not None
                conn.execute(
                    "DELETE FROM ref_closed_tags "
                    "WHERE ref_id = %s AND pos = %s "
                    "AND prefix = %s AND value = %s",
                    (ref_id, db_pos, tag.prefix, tag.value),
                )
            elif tag.namespace == "flag":
                conn.execute(
                    "DELETE FROM ref_flags "
                    "WHERE ref_id = %s AND pos = %s AND name = %s",
                    (ref_id, db_pos, tag.value),
                )
            else:
                conn.execute(
                    "DELETE FROM ref_open_tags "
                    "WHERE ref_id = %s AND pos = %s AND value = %s",
                    (ref_id, db_pos, tag.value),
                )

    def tags_for(
        self,
        ref_id: int,
        *,
        pos: int | None = None,
    ) -> list[Tag]:
        db_pos = _pos_to_db(pos)
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT 'closed'::text AS namespace, prefix, value
                FROM   ref_closed_tags
                WHERE  ref_id = %s AND pos = %s
                UNION ALL
                SELECT 'flag'::text AS namespace, NULL AS prefix, name AS value
                FROM   ref_flags
                WHERE  ref_id = %s AND pos = %s
                UNION ALL
                SELECT 'open'::text AS namespace, NULL AS prefix, value
                FROM   ref_open_tags
                WHERE  ref_id = %s AND pos = %s
                """,
                (ref_id, db_pos, ref_id, db_pos, ref_id, db_pos),
            ).fetchall()
        return [Tag(namespace=r[0], prefix=r[1], value=r[2]) for r in rows]

    def has_flag(self, ref_id: int, name: str) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM ref_flags WHERE ref_id = %s AND pos = %s AND name = %s",
                (ref_id, _REF_LEVEL_POS, name),
            ).fetchone()
        return row is not None

    # -- helpers -------------------------------------------------------------

    def _validate_slug_for_kind(
        self,
        kind: str,
        slug: str | None,
        *,
        conn: Connection | None = None,
    ) -> None:
        sql = "SELECT is_numeric FROM kinds WHERE slug = %s"
        if conn is not None:
            row = conn.execute(sql, (kind,)).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, (kind,)).fetchone()

        if row is None:
            raise BadInput(
                f"unknown kind: {kind!r}",
                next="check kinds: SELECT slug FROM kinds",
            )
        is_numeric = row[0]
        if is_numeric and slug is not None:
            raise BadInput(
                f"kind={kind!r} is numeric — slug must be None",
                next=f"insert_ref(kind={kind!r}, slug=None, ...)",
            )
        if not is_numeric and slug is None:
            raise BadInput(
                f"kind={kind!r} is slug-addressed — slug is required",
                next=f"insert_ref(kind={kind!r}, slug='...', ...)",
            )


# ---------------------------------------------------------------------------
# Row mappers (psycopg row tuple -> dataclass)
# ---------------------------------------------------------------------------


def _row_to_ref(row: tuple) -> Ref:
    """Map a refs row tuple in the order:
    (id, corpus_id, kind, slug, title, provider, meta,
     created_at, updated_at, deleted_at)
    """
    return Ref(
        id=row[0],
        corpus_id=row[1],
        kind=row[2],
        slug=row[3],
        title=row[4],
        provider=row[5],
        meta=row[6] or {},
        created_at=row[7],
        updated_at=row[8],
        deleted_at=row[9],
    )


__all__ = ["Store"]

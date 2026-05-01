"""Ref-level CRUD + lexical search. Mixin on :class:`precis.store.Store`.

Ref is the hub row in the schema: one row per paper / memory /
todo / conversation / oracle / quest / ..., with a ``corpus_id``
and a ``kind``. All domain mixins ultimately touch a ref_id;
this module owns the ref rows themselves plus the title-level
lexical search that powers ``search(kind=..., q=...)`` for
slug-addressed kinds.

The mixin assumes the concrete Store provides:

* ``self.pool``               — psycopg_pool.ConnectionPool
* ``self._validate_slug_for_kind(kind, slug, conn=...)`` — schema rule

Mypy-side: both are declared as class-level annotations so the
mixin type-checks in isolation; at runtime they're resolved by
MRO against the concrete ``Store``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import NotFound
from precis.store._mappers import _row_to_ref
from precis.store._tag_filter import build_tag_filter
from precis.store.types import Ref


class RefsMixin:
    """Ref insert / get / update / delete + list + lexical search."""

    pool: ConnectionPool

    # Provided by the concrete Store — validates the ``slug vs None``
    # rule per kind (numeric kinds reject non-None slugs, slug kinds
    # require a slug). MRO resolves this to the real implementation
    # at runtime; calling it on a bare ``RefsMixin`` raises.
    def _validate_slug_for_kind(
        self,
        kind: str,
        slug: str | None,
        *,
        conn: Connection | None = None,
    ) -> None:
        raise NotImplementedError  # pragma: no cover — overridden by Store

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

        Enforced at app layer (the DB ``CHECK`` can't subquery the
        ``kinds`` reference table).
        """
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
        """Look up by (kind, public id).

        Public id = slug for slug kinds, ``int(refs.id)`` for numeric
        kinds. The caller's ``isinstance`` of ``id`` picks the column.
        """
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
        conn: Connection | None = None,
    ) -> Ref:
        """Patch title and/or merge new keys into meta.

        ``conn=`` lets the caller share an existing transaction so
        the update participates in a wider atomic unit (used by
        ``NumericRefHandler._update`` which wraps title + tag +
        link writes in one ``tx()``).
        """
        sql = """
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
        """
        params = (
            title,
            Jsonb(meta_patch) if meta_patch is not None else None,
            Jsonb(meta_patch) if meta_patch is not None else None,
            ref_id,
        )
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, params).fetchone()
        if row is None:
            raise NotFound(
                f"ref id={ref_id} not found (or already deleted)",
                next=f"check id with: get(kind=..., id={ref_id})",
            )
        return _row_to_ref(row)

    def soft_delete_ref(self, ref_id: int) -> None:
        """Soft-delete a ref by setting ``deleted_at = now()``."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE refs SET deleted_at = now() "
                "WHERE id = %s AND deleted_at IS NULL",
                (ref_id,),
            )
            rowcount = cur.rowcount
        if rowcount == 0:
            raise NotFound(f"ref id={ref_id} not found (or already deleted)")

    def most_recent_kind(self, *, kinds: list[str] | None = None) -> str | None:
        """Return the kind of the most recently updated live ref.

        ``kinds=`` restricts the lookup to a whitelist (typically the
        kinds whose handlers support ``search``); ``None`` means "any
        kind". Returns ``None`` when the corpus is empty (or no live
        ref matches the whitelist).

        Used by the runtime dispatcher to default ``kind=`` for
        ``search()`` calls that omit it. Picking the most recently
        touched kind biases the default toward what the agent has
        been working with — the right behaviour when a 7B caller
        forgets the kwarg ("forgetting kind= is a real risk for
        small models", per the MCP critic's deferred suggestion).

        Cheap: a single indexed query against ``refs.updated_at``.
        Returns the kind string from the highest-updated row.
        """
        clauses = ["deleted_at IS NULL"]
        params: list[Any] = []
        if kinds is not None:
            if not kinds:
                # An empty whitelist would produce ``WHERE kind IN ()``
                # which Postgres rejects — short-circuit instead.
                return None
            clauses.append("kind = ANY(%s)")
            params.append(list(kinds))
        sql = (
            "SELECT kind FROM refs "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        with self.pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return None if row is None else str(row[0])

    def list_refs(
        self,
        *,
        corpus_id: int | None = None,
        kind: str | None = None,
        provider: str | None = None,
        updated_after: datetime | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Ref]:
        """Paginated list of live refs, filter by kind/provider/tags."""
        # Aliased as ``r`` so the tag-filter helper can reference
        # ``r.id`` uniformly across all store query shapes.
        clauses = ["r.deleted_at IS NULL"]
        params: list[Any] = []
        if corpus_id is not None:
            params.append(corpus_id)
            clauses.append("r.corpus_id = %s")
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        if provider is not None:
            params.append(provider)
            clauses.append("r.provider = %s")
        if updated_after is not None:
            params.append(updated_after)
            clauses.append("r.updated_at > %s")

        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        # ``build_tag_filter`` already prefixes with " AND "; strip it
        # once and add each clause separately so ``" AND ".join`` still
        # works.
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            params.extend(tag_params)

        params.append(limit)
        params.append(offset)
        sql = (
            "SELECT r.id, r.corpus_id, r.kind, r.slug, r.title, r.provider, "
            "       r.meta, r.created_at, r.updated_at, r.deleted_at "
            "FROM refs r WHERE "
            + " AND ".join(clauses)
            + " ORDER BY r.updated_at DESC LIMIT %s OFFSET %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_ref(r) for r in rows]

    def count_refs_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        tags: list[str] | None = None,
    ) -> int:
        """Count refs matching the lexical filter (no LIMIT).

        Companion to :meth:`search_refs_lexical` for pagination
        headers. The MCP critic asked for a "you're seeing N of K"
        readout in search responses; this gives handlers the K
        with the same WHERE clause the search uses, so the two
        numbers can't drift.

        Tag-filter parameters are validated by the handler layer
        via :meth:`Tag.parse_strict`; this method takes the
        already-canonical strings and forwards them straight to
        :func:`build_tag_filter`.
        """
        clauses = ["r.deleted_at IS NULL", "r.title_tsv @@ qq.qq"]
        params: list[Any] = [q]
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            params.extend(tag_params)
        sql = (
            "SELECT count(*) FROM refs r, "
            "     websearch_to_tsquery('english', %s) qq(qq) "
            f"WHERE {' AND '.join(clauses)}"
        )
        with self.pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        assert row is not None
        return int(row[0])

    def search_refs_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> list[tuple[Ref, float]]:
        """Lexical search over ``refs.title_tsv``.

        Returns ``(ref, rank)`` sorted by rank desc. Semantic +
        RRF fusion happen at the block level; title-level stays
        lexical-only.
        """
        clauses = ["r.deleted_at IS NULL", "r.title_tsv @@ qq.qq"]
        params: list[Any] = [q]
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            params.extend(tag_params)
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

    def count_refs(
        self,
        *,
        kind: str | None = None,
        provider: str | None = None,
        tags: list[str] | None = None,
    ) -> int:
        """Count active (not soft-deleted) refs, optionally filtered.

        Used by list views that paginate — they need the page total
        and the corpus total to render '50 of N' style headers without
        a second pass through ``list_refs(limit=very-large)``.

        ``tags=`` accepts the same canonical tag-string list as
        :meth:`list_refs`; runtime callers must validate via
        :meth:`Tag.parse_strict` before this point.
        """
        clauses = ["r.deleted_at IS NULL"]
        params: list[Any] = []
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        if provider is not None:
            params.append(provider)
            clauses.append("r.provider = %s")
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            params.extend(tag_params)
        sql = "SELECT count(*) FROM refs r WHERE " + " AND ".join(clauses)
        with self.pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        assert row is not None
        return int(row[0])


__all__ = ["RefsMixin"]

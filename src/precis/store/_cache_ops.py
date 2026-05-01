"""Cache state CRUD. Mixin on :class:`precis.store.Store`.

Paid-tool caches (``perplexity``, ``math``, ``youtube``, ``web``)
live in the ``refs`` + ``blocks`` tables like every other ref, with
a parallel ``cache_state`` row carrying the freshness + cost
metadata that drives cache-freshness sweeps.

Two methods, one lookup + one upsert. Freshness is derived at
read time (``cache_freshness`` view) so the cache-state writes
don't need to touch TTL arithmetic beyond ``fresh_until``.

Mixin assumes the concrete Store provides:

* ``self.pool``
* ``self.tx()``                 — context-manager transaction
* ``self.insert_blocks(...)``   — BlocksMixin method used for the
                                   body-on-replace path
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.store._mappers import _row_to_cache_entry, _row_to_ref
from precis.store.types import Block, BlockInsert, CacheEntry, Ref


class CacheMixin:
    """Cache-state lookup + atomic create-or-replace."""

    pool: ConnectionPool

    # Provided by the concrete Store / sibling mixins. These stubs
    # document the dependency for readers; MRO resolves the real
    # implementations at runtime when the mixin is composed into
    # :class:`Store`. Calling them on a bare ``CacheMixin`` raises.
    def tx(self) -> AbstractContextManager[Connection]:
        raise NotImplementedError  # pragma: no cover — overridden by Store

    def insert_blocks(
        self,
        ref_id: int,
        blocks: list[BlockInsert],
        *,
        replace: bool = False,
        conn: Connection | None = None,
    ) -> list[Block]:
        raise NotImplementedError  # pragma: no cover — overridden by BlocksMixin

    def get_cache_entry(
        self,
        *,
        provider: str,
        request_hash: str,
    ) -> tuple[Ref, CacheEntry] | None:
        """Look up a cached ref + freshness row by ``(provider, request_hash)``.

        Returns the joined ``(Ref, CacheEntry)`` pair if found, else
        None. Soft-deleted refs are excluded — a deleted ref is not a
        cache hit. Caller decides freshness via ``CacheEntry.fresh_until``
        vs ``now()``.
        """
        sql = """
            SELECT r.id, r.corpus_id, r.kind, r.slug, r.title, r.provider,
                   r.meta, r.created_at, r.updated_at, r.deleted_at,
                   c.ref_id, c.provider, c.request_hash, c.model,
                   c.fetched_at, c.fresh_until, c.cost_usd, c.meta
            FROM cache_state c
            JOIN refs r ON r.id = c.ref_id
            WHERE c.provider = %s
              AND c.request_hash = %s
              AND r.deleted_at IS NULL
            LIMIT 1
        """
        with self.pool.connection() as conn:
            row = conn.execute(sql, (provider, request_hash)).fetchone()
        if row is None:
            return None
        ref = _row_to_ref(row[:10])
        cache = _row_to_cache_entry(row[10:18])
        return (ref, cache)

    def put_cache_entry(
        self,
        *,
        corpus_id: int,
        kind: str,
        slug: str,
        title: str,
        body_blocks: list[BlockInsert],
        provider: str,
        request_hash: str,
        ttl_seconds: int | None,
        model: str | None = None,
        cost_usd: float | None = None,
        ref_meta: dict[str, Any] | None = None,
        cache_meta: dict[str, Any] | None = None,
    ) -> tuple[Ref, CacheEntry]:
        """Atomically create-or-replace a cached ref + its cache_state row.

        On replace (existing ref with same ``kind+slug``), all old
        blocks are deleted via cascade and replaced with
        ``body_blocks``. The cache_state row is upserted on
        ``(provider, request_hash)``.

        ``ttl_seconds=None`` pins the entry (never expires).
        ``ttl_seconds=0`` is allowed but means the entry is born
        stale — useful only for testing.
        """
        fresh_until_sql = (
            "now() + (%s || ' seconds')::interval"
            if ttl_seconds is not None
            else "NULL"
        )
        ref_meta_json = Jsonb(ref_meta or {})
        cache_meta_json = Jsonb(cache_meta or {})

        with self.tx() as conn:
            # Upsert the ref. Any existing blocks cascade-delete because
            # we soft-delete-then-purge; for cache entries we want hard
            # replacement, so we DELETE the old ref outright if present
            # (cascading to blocks + cache_state) and re-insert.
            existing = conn.execute(
                "SELECT id FROM refs WHERE kind = %s AND slug = %s "
                "AND deleted_at IS NULL",
                (kind, slug),
            ).fetchone()
            if existing is not None:
                conn.execute("DELETE FROM refs WHERE id = %s", (existing[0],))

            row = conn.execute(
                "INSERT INTO refs (corpus_id, kind, slug, title, provider, meta) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id, corpus_id, kind, slug, title, provider, meta, "
                "          created_at, updated_at, deleted_at",
                (corpus_id, kind, slug, title, provider, ref_meta_json),
            ).fetchone()
            assert row is not None
            ref = _row_to_ref(row)

            if body_blocks:
                # Reuse insert_blocks via conn= so we stay in the same tx.
                self.insert_blocks(ref.id, body_blocks, conn=conn)

            # Insert cache_state. PRIMARY KEY is ref_id, so ON CONFLICT
            # is on the unique (provider, request_hash) index.
            cache_sql = (
                "INSERT INTO cache_state "
                "  (ref_id, provider, request_hash, model, fetched_at, "
                "   fresh_until, cost_usd, meta) "
                f"VALUES (%s, %s, %s, %s, now(), {fresh_until_sql}, %s, %s) "
                "RETURNING ref_id, provider, request_hash, model, "
                "          fetched_at, fresh_until, cost_usd, meta"
            )
            params: tuple[Any, ...] = (
                ref.id,
                provider,
                request_hash,
                model,
            )
            if ttl_seconds is not None:
                params = params + (str(ttl_seconds), cost_usd, cache_meta_json)
            else:
                params = params + (cost_usd, cache_meta_json)
            cache_row = conn.execute(cache_sql, params).fetchone()
            assert cache_row is not None
            cache = _row_to_cache_entry(cache_row)

        return (ref, cache)


__all__ = ["CacheMixin"]

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

from precis.store._mappers import _REFS_COLS, _row_to_cache_entry, _row_to_ref
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

    def get_cache_entry_by_slug(
        self,
        *,
        kind: str,
        slug: str,
    ) -> tuple[Ref, CacheEntry] | None:
        """Look up a cached ref + freshness row by ``(kind, slug)``.

        Symmetrical to :meth:`get_cache_entry` but addressed by the
        agent-facing slug rather than the internal request hash. Used
        by cache-backed handlers' ``get`` to honour the slugs that
        ``/recent`` listings advertise — without this lookup,
        ``get(kind='web', id='example-com')`` falls through to the
        URL canonicaliser and rejects a slug it just printed (MCP
        critic MAJOR-C, 2026-05-02).
        """
        sql = """
            SELECT r.id, r.corpus_id, r.kind, r.slug, r.title, r.provider,
                   r.meta, r.created_at, r.updated_at, r.deleted_at,
                   c.ref_id, c.provider, c.request_hash, c.model,
                   c.fetched_at, c.fresh_until, c.cost_usd, c.meta
            FROM cache_state c
            JOIN refs r ON r.id = c.ref_id
            WHERE r.kind = %s
              AND r.slug = %s
              AND r.deleted_at IS NULL
            LIMIT 1
        """
        with self.pool.connection() as conn:
            row = conn.execute(sql, (kind, slug)).fetchone()
        if row is None:
            return None
        ref = _row_to_ref(row[:10])
        cache = _row_to_cache_entry(row[10:18])
        return (ref, cache)

    def update_cache_entry(
        self,
        *,
        ref_id: int,
        title: str,
        body_blocks: list[BlockInsert],
        ttl_seconds: int | None,
        request_hash: str | None = None,
        model: str | None = None,
        cost_usd: float | None = None,
        cache_meta: dict[str, Any] | None = None,
    ) -> tuple[Ref, CacheEntry]:
        """Refresh body + cache_state for an existing cached ref, **in-place**.

        Distinct from :meth:`put_cache_entry`:

        * preserves ``ref.id``, ``ref.slug``, ``ref.created_at`` and
          every tag / link attached to the ref (no DELETE on the row),
        * replaces ``blocks`` (DELETE FROM blocks WHERE ref_id) with
          the freshly-fetched body,
        * UPDATEs ``cache_state`` for the ref so freshness, cost,
          model, and meta reflect the new fetch.

        Use this for ``mode='refresh'`` and for routine stale-cache
        re-fetches, so a ``bookmark`` / ``WATCH:daily`` tag set
        survives upstream re-fetches. (gripe:3681 phase 4.)

        ``request_hash=None`` keeps the existing hash on the
        cache_state row — useful for "the canonical key didn't
        change, just the body". Pass an explicit hash if the canonical
        key shifted (e.g. canonicalisation rules updated).
        """
        fresh_until_sql = (
            "now() + (%s || ' seconds')::interval"
            if ttl_seconds is not None
            else "NULL"
        )
        cache_meta_json = Jsonb(cache_meta or {})

        with self.tx() as conn:
            # Update the ref title (rare but possible — page titles
            # change). Touch updated_at so /recent listings reflect
            # the refresh.
            row = conn.execute(
                "UPDATE refs SET title = %s, updated_at = now() "
                "WHERE id = %s AND deleted_at IS NULL "
                f"RETURNING {_REFS_COLS}",
                (title, ref_id),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"update_cache_entry: ref_id={ref_id} not found or deleted"
                )
            ref = _row_to_ref(row)

            # Replace the blocks. Tags/links live on the ref row, not
            # on blocks, so they're untouched.
            conn.execute("DELETE FROM blocks WHERE ref_id = %s", (ref_id,))
            if body_blocks:
                self.insert_blocks(ref_id, body_blocks, conn=conn)

            # Update cache_state. Keep existing request_hash unless
            # caller passed a new one. fresh_until rewinds; cost +
            # model + meta refresh from the new FetchResult.
            if request_hash is None:
                cache_sql = (
                    "UPDATE cache_state SET "
                    "  model = %s, fetched_at = now(), "
                    f"  fresh_until = {fresh_until_sql}, "
                    "  cost_usd = %s, meta = %s "
                    "WHERE ref_id = %s "
                    "RETURNING ref_id, provider, request_hash, model, "
                    "          fetched_at, fresh_until, cost_usd, meta"
                )
                params: tuple[Any, ...] = (model,)
                if ttl_seconds is not None:
                    params = params + (
                        str(ttl_seconds),
                        cost_usd,
                        cache_meta_json,
                        ref_id,
                    )
                else:
                    params = params + (cost_usd, cache_meta_json, ref_id)
            else:
                cache_sql = (
                    "UPDATE cache_state SET "
                    "  request_hash = %s, model = %s, fetched_at = now(), "
                    f"  fresh_until = {fresh_until_sql}, "
                    "  cost_usd = %s, meta = %s "
                    "WHERE ref_id = %s "
                    "RETURNING ref_id, provider, request_hash, model, "
                    "          fetched_at, fresh_until, cost_usd, meta"
                )
                params = (request_hash, model)
                if ttl_seconds is not None:
                    params = params + (
                        str(ttl_seconds),
                        cost_usd,
                        cache_meta_json,
                        ref_id,
                    )
                else:
                    params = params + (cost_usd, cache_meta_json, ref_id)

            cache_row = conn.execute(cache_sql, params).fetchone()
            if cache_row is None:
                raise ValueError(
                    f"update_cache_entry: cache_state row for ref_id={ref_id} missing"
                )
            cache = _row_to_cache_entry(cache_row)

        return (ref, cache)

    def put_cache_entry(
        self,
        *,
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

        **Phase 4 stub** — the v1 body INSERTed against ``refs(corpus_id, …,
        slug, …)`` and required a ``corpus_id`` param. v2 schema dropped
        both (corpus_id is gone; slug lives in ``ref_identifiers``).
        Rewrite slated for Phase 4 of the store-v2 rewrite (see plan).

        For now this raises so cache-backed handlers fail loudly with a
        message pointing at the right phase, rather than landing rows
        against v1-shaped SQL that would just throw column-not-found
        errors at the psycopg layer.
        """
        raise NotImplementedError(
            "put_cache_entry: phase 4 (cache v2 rewrite) not yet implemented; "
            "see /Users/reto/.claude/plans/lively-yawning-kahn.md"
        )


__all__ = ["CacheMixin"]

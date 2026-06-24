"""Cache state CRUD against v2 schema. Mixin on
:class:`precis.store.Store`.

Paid-tool caches (``perplexity`` / ``math`` / ``youtube`` / ``web``)
live in the ``refs`` + ``chunks`` tables like every other ref, with
a parallel ``cache_state`` row carrying the freshness + cost
metadata that drives cache-freshness sweeps.

v2 schema notes:

- ``cache_state`` table was added to ``migrations/0001_initial.sql``
  alongside the Phase 4 rewrite (the puml omitted it but the
  handlers, the CLI maintenance command, and the v1 ``put_cache_entry``
  flow all required it).
- Slug-addressed cache reads (``get_cache_entry_by_slug``) route
  through ``ref_identifiers`` (``id_kind='cite_key'``) — refs no
  longer carry a ``slug`` column.
- Body replacement uses ``DELETE FROM chunks WHERE ref_id = %s``
  instead of v1's ``DELETE FROM blocks``; the FK ON DELETE CASCADE
  takes care of ``chunk_embeddings`` / ``chunk_summaries`` /
  ``chunk_tags`` automatically.

Three lookup paths + one upsert:

- :meth:`get_cache_entry` — by ``(provider, request_hash)``
- :meth:`get_cache_entry_by_slug` — by ``(kind, slug)`` via
  ``ref_identifiers`` JOIN
- :meth:`update_cache_entry` — in-place body + freshness refresh
  for an existing cached ref (preserves ref.id / tags / links)
- :meth:`put_cache_entry` — atomic create-or-replace; DELETEs any
  prior ref with the same ``(kind, slug)`` (cascading to chunks +
  cache_state) and INSERTs fresh

Mixin assumes the concrete Store provides:

* ``self.pool``
* ``self.tx()``                 — context-manager transaction
* ``self.insert_ref(...)``      — RefsMixin method (writes refs +
                                  ref_identifiers row for the slug)
* ``self.insert_blocks(...)``   — BlocksMixin method used for the
                                  body-on-replace path (v2: writes
                                  to ``chunks``)
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.store._mappers import (
    _REFS_COLS,
    _REFS_COLS_LEN,
    _row_to_cache_entry,
    _row_to_ref,
)
from precis.store.types import Block, BlockInsert, CacheEntry, Ref


class CacheMixin:
    """v2 cache-state lookup + atomic create-or-replace."""

    pool: ConnectionPool

    # Provided by the concrete Store / sibling mixins. These stubs
    # document the dependency for readers; MRO resolves the real
    # implementations at runtime when the mixin is composed into
    # :class:`Store`. Calling them on a bare ``CacheMixin`` raises.
    def tx(self) -> AbstractContextManager[Connection]:
        raise NotImplementedError  # pragma: no cover — overridden by Store

    def insert_ref(
        self,
        *,
        kind: str,
        slug: str | None,
        title: str,
        provider: str | None = None,
        meta: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> Ref:
        raise NotImplementedError  # pragma: no cover — overridden by RefsMixin

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
        sql = f"""
            SELECT {_REFS_COLS_FOR_CACHE},
                   cs.ref_id, cs.provider, cs.request_hash, cs.model,
                   cs.fetched_at, cs.fresh_until, cs.cost_usd, cs.meta
            FROM cache_state cs
            JOIN refs ON refs.ref_id = cs.ref_id
            WHERE cs.provider = %s
              AND cs.request_hash = %s
              AND refs.deleted_at IS NULL
            LIMIT 1
        """
        with self.pool.connection() as conn:
            row = conn.execute(sql, (provider, request_hash)).fetchone()
        if row is None:
            return None
        ref = _row_to_ref(row[:_REFS_COLS_LEN])
        cache = _row_to_cache_entry(
            row[_REFS_COLS_LEN : _REFS_COLS_LEN + _CACHE_COLS_LEN]
        )
        return (ref, cache)

    def get_cache_entry_by_slug(
        self,
        *,
        kind: str,
        slug: str,
    ) -> tuple[Ref, CacheEntry] | None:
        """Look up a cached ref + freshness row by ``(kind, slug)``.

        Symmetrical to :meth:`get_cache_entry` but addressed by the
        agent-facing slug rather than the internal request hash. v2
        joins through ``ref_identifiers`` (``id_kind='cite_key'``)
        since the v1 ``refs.slug`` column is gone.

        Used by cache-backed handlers' ``get`` to honour the slugs
        that ``/recent`` listings advertise — without this lookup,
        ``get(kind='web', id='example-com')`` falls through to the
        URL canonicaliser and rejects a slug it just printed.
        """
        sql = f"""
            SELECT {_REFS_COLS_FOR_CACHE},
                   cs.ref_id, cs.provider, cs.request_hash, cs.model,
                   cs.fetched_at, cs.fresh_until, cs.cost_usd, cs.meta
            FROM cache_state cs
            JOIN refs ON refs.ref_id = cs.ref_id
            JOIN ref_identifiers ri
              ON ri.ref_id = refs.ref_id
              AND ri.id_kind = 'cite_key'
            WHERE refs.kind = %s
              AND ri.id_value = %s
              AND refs.deleted_at IS NULL
            LIMIT 1
        """
        with self.pool.connection() as conn:
            row = conn.execute(sql, (kind, slug)).fetchone()
        if row is None:
            return None
        ref = _row_to_ref(row[:_REFS_COLS_LEN])
        cache = _row_to_cache_entry(
            row[_REFS_COLS_LEN : _REFS_COLS_LEN + _CACHE_COLS_LEN]
        )
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

        * preserves ``ref.id``, ref_identifiers, ``ref.created_at`` and
          every tag / link attached to the ref (no DELETE on the row),
        * replaces chunks (``DELETE FROM chunks WHERE ref_id``) with
          the freshly-fetched body — FK CASCADE cleans the per-chunk
          embeddings / summaries / tags,
        * UPDATEs ``cache_state`` for the ref so freshness, cost,
          model, and meta reflect the new fetch.

        Use this for ``mode='refresh'`` and for routine stale-cache
        re-fetches, so a ``bookmark`` / ``WATCH:daily`` tag set
        survives upstream re-fetches.

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
                f"UPDATE refs SET title = %s, updated_at = now() "
                "WHERE ref_id = %s AND deleted_at IS NULL "
                f"RETURNING {_REFS_COLS}",
                (title, ref_id),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"update_cache_entry: ref_id={ref_id} not found or deleted"
                )
            ref = _row_to_ref(row)

            # Replace chunks (cascades to chunk_embeddings /
            # chunk_summaries / chunk_tags via FK ON DELETE).
            # Ref-level tags/links/identifiers are untouched.
            conn.execute("DELETE FROM chunks WHERE ref_id = %s", (ref_id,))
            if body_blocks:
                self.insert_blocks(ref_id, body_blocks, conn=conn)

            # Update cache_state. Keep existing request_hash unless
            # caller passed a new one.
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

        On replace (existing live ref with the same ``(kind, slug)``),
        the prior ref is DELETEd outright (cascading to chunks /
        chunk_embeddings / chunk_summaries / chunk_tags / ref_tags /
        cache_state / ref_identifiers) and re-created.  ``insert_ref``
        writes the new refs row + a fresh ``ref_identifiers`` entry
        with ``id_kind='cite_key'`` for the slug.

        ``ttl_seconds=None`` pins the entry (never expires).
        ``ttl_seconds=0`` is allowed but means the entry is born
        stale — useful only for testing.
        """
        fresh_until_sql = (
            "now() + (%s || ' seconds')::interval"
            if ttl_seconds is not None
            else "NULL"
        )
        cache_meta_json = Jsonb(cache_meta or {})

        with self.tx() as conn:
            # If a live ref with this (kind, slug) exists, DELETE it
            # outright so the FK cascades clean every dependent row.
            # Use ref_identifiers for the slug lookup since v2 dropped
            # refs.slug. Soft-deleted refs are left alone; their slug
            # has already been freed up for re-use.
            existing = conn.execute(
                "SELECT r.ref_id "
                "FROM refs r "
                "JOIN ref_identifiers ri "
                "  ON ri.ref_id = r.ref_id "
                "  AND ri.id_kind = 'cite_key' "
                "WHERE r.kind = %s "
                "  AND ri.id_value = %s "
                "  AND r.deleted_at IS NULL",
                (kind, slug),
            ).fetchone()
            if existing is not None:
                conn.execute("DELETE FROM refs WHERE ref_id = %s", (existing[0],))

            ref = self.insert_ref(
                kind=kind,
                slug=slug,
                title=title,
                provider=provider,
                meta=ref_meta or {},
                conn=conn,
            )

            if body_blocks:
                self.insert_blocks(ref.id, body_blocks, conn=conn)

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
            assert cache_row is not None, (
                "cache_state INSERT returned no row — schema invariant violated"
            )
            cache = _row_to_cache_entry(cache_row)

        return (ref, cache)


# _REFS_COLS adapted for use inside cache JOINs. The base _REFS_COLS
# constant references ``refs.ref_id`` and ``ref_identifiers`` via
# correlated subquery (unaliased); when the cache SELECT also names
# ``refs`` directly (no alias) the same constant applies.
_REFS_COLS_FOR_CACHE = (
    "refs.ref_id AS id, "
    "(SELECT id_value FROM ref_identifiers "
    " WHERE ref_id = refs.ref_id AND id_kind = 'cite_key'"
    " ORDER BY created_at DESC LIMIT 1) AS slug, "
    "refs.kind, refs.title, refs.provider, refs.meta, "
    "refs.created_at, refs.updated_at, refs.deleted_at, "
    "refs.set_by, refs.authors, refs.year, "
    "refs.human_verified_at, refs.human_verified_by, refs.human_verified_note, "
    "refs.retraction_status, refs.retracted_at, refs.retraction_reason, "
    "refs.retraction_url, refs.retraction_checked_at, "
    "refs.pdf_sha256, refs.pdf_pages::text AS pdf_pages, refs.pdf_role, "
    "refs.auto_refresh_days, refs.refreshed_at, "
    "refs.parent_id, refs.prio"
)

#: Number of ``cache_state`` columns appended after the ref projection
#: in :meth:`CacheMixin.get_cache_entry` and friends. Pinned to a
#: named constant so the slice positions in those methods don't have
#: to track the SELECT list by hand.
_CACHE_COLS_LEN = 8

# Drift guard. If a future ref-column migration bumps ``_REFS_COLS_LEN``
# (in ``_mappers.py``) without a matching entry being added to
# ``_REFS_COLS_FOR_CACHE`` above, this assertion fires at import time —
# loud, immediate, instead of cascading dozens of "tuple index out of
# range" failures across the cache_state / perplexity / web caches.
# v8.7.1 patched the same drift class in ``_blocks_ops.py``; this is
# the equivalent guard for the cache path.
assert _REFS_COLS_FOR_CACHE.count(",") + 1 == _REFS_COLS_LEN, (
    f"_REFS_COLS_FOR_CACHE drift: projects "
    f"{_REFS_COLS_FOR_CACHE.count(',') + 1} columns but "
    f"_REFS_COLS_LEN={_REFS_COLS_LEN}. Update the projection above to "
    f"match the columns added to refs in the latest migration."
)


__all__ = ["CacheMixin"]

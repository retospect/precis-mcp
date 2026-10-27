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
* ``self.chunks.insert_chunks(...)`` — ChunkStore method used for the
                                  body-on-replace path (v2: writes
                                  to ``chunks``)
"""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput
from precis.store._mappers import (
    _REFS_COLS,
    _REFS_COLS_LEN,
    _row_to_cache_entry,
    _row_to_ref,
)
from precis.store.types import CacheEntry, ChunkInsert, Ref
from precis.utils.title_views import title_plain_sql

#: gr515227: title legs read the plain view (markup / LaTeX noise
#: stripped at query time) — titles are stored raw.
_TITLE_PLAIN_SQL = title_plain_sql("refs.title")

#: jsonb key shape allowed in :meth:`CacheMixin.search_cache_entry_titles`'
#: ``meta_fields`` — handler constants only; anything else is refused
#: before it can be quoted into SQL.
_META_FIELD_RE = re.compile(r"[a-z][a-z0-9_]*")


def _ilike_pattern(q: str) -> str:
    """Literal-substring ILIKE pattern for ``q`` — its ``%`` and ``_``
    escaped so a query like ``"100%"`` matches only the literal text.
    Pair with ``ILIKE %s ESCAPE '\\'`` at the call site."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class CacheMixin:
    """v2 cache-state lookup + atomic create-or-replace."""

    pool: ConnectionPool

    # Provided by ``RefsMixin`` / the concrete Store; declared here so the
    # cache create-or-replace paths type-check against the cross-mixin
    # calls. **Must be TYPE_CHECKING only** — a runtime ``def`` here
    # shadows the real implementation for any mixin ordered after
    # ``CacheMixin`` in ``Store.__bases__`` (the ``add_tag`` incident in
    # ``_refs_ops.py``; gripe 202377). ``tests/test_store_mixin_guard.py``
    # enforces this class-wide.
    if TYPE_CHECKING:
        from precis.store._chunks_ops import ChunkStore

        # Provided by the concrete Store (store.py).
        def tx(self) -> AbstractContextManager[Connection]: ...

        def insert_ref(
            self,
            *,
            kind: str,
            slug: str | None,
            title: str,
            provider: str | None = None,
            meta: dict[str, Any] | None = None,
            conn: Connection | None = None,
        ) -> Ref: ...

        @property
        def chunks(self) -> ChunkStore: ...

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
                   cs.fetched_at, cs.fresh_until, cs.cost_usd, cs.meta,
                   cs.content_sha256
            FROM cache_state cs
            JOIN refs ON refs.ref_id = cs.ref_id
            WHERE cs.provider = %s
              AND cs.request_hash = %s
              AND refs.retired_at IS NULL
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

    def get_cache_entry_by_ref_id(
        self,
        *,
        kind: str,
        ref_id: int,
    ) -> tuple[Ref, CacheEntry] | None:
        """Look up a cached ref + freshness row by ``(kind, ref_id)``.

        For agents that cite a cache by its numeric ref id (memories write
        ``websearch:170350``) rather than its slug. The ``kind`` filter keeps
        a number from resolving to some other kind's ref.
        """
        sql = f"""
            SELECT {_REFS_COLS_FOR_CACHE},
                   cs.ref_id, cs.provider, cs.request_hash, cs.model,
                   cs.fetched_at, cs.fresh_until, cs.cost_usd, cs.meta
            FROM cache_state cs
            JOIN refs ON refs.ref_id = cs.ref_id
            WHERE refs.kind = %s
              AND refs.ref_id = %s
              AND refs.retired_at IS NULL
            LIMIT 1
        """
        with self.pool.connection() as conn:
            row = conn.execute(sql, (kind, ref_id)).fetchone()
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
                   cs.fetched_at, cs.fresh_until, cs.cost_usd, cs.meta,
                   cs.content_sha256
            FROM cache_state cs
            JOIN refs ON refs.ref_id = cs.ref_id
            JOIN ref_identifiers ri
              ON ri.ref_id = refs.ref_id
              AND ri.id_kind = 'cite_key'
            WHERE refs.kind = %s
              AND ri.id_value = %s
              AND refs.retired_at IS NULL
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
        body_blocks: list[ChunkInsert],
        ttl_seconds: int | None,
        request_hash: str | None = None,
        model: str | None = None,
        cost_usd: float | None = None,
        cache_meta: dict[str, Any] | None = None,
        content_sha256: str | None = None,
    ) -> tuple[Ref, CacheEntry]:
        """Refresh body + cache_state for an existing cached ref, **in-place**.

        Distinct from :meth:`put_cache_entry`:

        * preserves ``ref.id``, ref_identifiers, ``ref.created_at`` and
          every tag / link attached to the ref (no DELETE on the row),
        * replaces chunks (``DELETE FROM chunks WHERE ref_id``) with
          the freshly-fetched body — FK CASCADE cleans the per-chunk
          embeddings / summaries / tags — EXCEPT when ``content_sha256``
          equals the stored one (byte-identical page): then the chunks
          stay, so their ids and embeddings remain valid,
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
                "WHERE ref_id = %s AND retired_at IS NULL "
                f"RETURNING {_REFS_COLS}",
                (title, ref_id),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"update_cache_entry: ref_id={ref_id} not found or deleted"
                )
            ref = _row_to_ref(row)

            # An unchanged refresh keeps its chunks: when the new content
            # sha equals the stored one the body is byte-identical, so
            # DELETE+INSERT would only strand the chunk ids a nanopub
            # passage froze (and force a re-embed). Body chunks are never
            # UPDATEd in place — the only choices are keep or replace.
            # A NULL stored sha (page cached before migration 0202), a
            # differing sha, or a page with no live body chunk replaces.
            prior = conn.execute(
                "SELECT content_sha256 FROM cache_state WHERE ref_id = %s FOR UPDATE",
                (ref_id,),
            ).fetchone()
            unchanged = (
                content_sha256 is not None
                and prior is not None
                and prior[0] == content_sha256
                and conn.execute(
                    "SELECT 1 FROM chunks WHERE ref_id = %s AND ord >= 0 "
                    "AND retired_at IS NULL LIMIT 1",
                    (ref_id,),
                ).fetchone()
                is not None
            )
            if not unchanged:
                # Replace chunks (cascades to chunk_embeddings /
                # chunk_summaries / chunk_tags via FK ON DELETE).
                # Ref-level tags/links/identifiers are untouched.
                conn.execute("DELETE FROM chunks WHERE ref_id = %s", (ref_id,))
                if body_blocks:
                    self.chunks.insert_chunks(ref_id, body_blocks, conn=conn)

            # Update cache_state. Keep existing request_hash unless
            # caller passed a new one.
            if request_hash is None:
                cache_sql = (
                    "UPDATE cache_state SET "
                    "  model = %s, fetched_at = now(), "
                    f"  fresh_until = {fresh_until_sql}, "
                    "  cost_usd = %s, meta = %s, content_sha256 = %s "
                    "WHERE ref_id = %s "
                    "RETURNING ref_id, provider, request_hash, model, "
                    "          fetched_at, fresh_until, cost_usd, meta, content_sha256"
                )
                params: tuple[Any, ...] = (model,)
                if ttl_seconds is not None:
                    params = params + (
                        str(ttl_seconds),
                        cost_usd,
                        cache_meta_json,
                        content_sha256,
                        ref_id,
                    )
                else:
                    params = params + (
                        cost_usd,
                        cache_meta_json,
                        content_sha256,
                        ref_id,
                    )
            else:
                cache_sql = (
                    "UPDATE cache_state SET "
                    "  request_hash = %s, model = %s, fetched_at = now(), "
                    f"  fresh_until = {fresh_until_sql}, "
                    "  cost_usd = %s, meta = %s, content_sha256 = %s "
                    "WHERE ref_id = %s "
                    "RETURNING ref_id, provider, request_hash, model, "
                    "          fetched_at, fresh_until, cost_usd, meta, content_sha256"
                )
                params = (request_hash, model)
                if ttl_seconds is not None:
                    params = params + (
                        str(ttl_seconds),
                        cost_usd,
                        cache_meta_json,
                        content_sha256,
                        ref_id,
                    )
                else:
                    params = params + (
                        cost_usd,
                        cache_meta_json,
                        content_sha256,
                        ref_id,
                    )

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
        body_blocks: list[ChunkInsert],
        provider: str,
        request_hash: str,
        ttl_seconds: int | None,
        model: str | None = None,
        cost_usd: float | None = None,
        ref_meta: dict[str, Any] | None = None,
        cache_meta: dict[str, Any] | None = None,
        content_sha256: str | None = None,
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
                "  AND r.retired_at IS NULL",
                (kind, slug),
            ).fetchone()
            if existing is not None:
                # Measures are append-only and their subject / measurand FKs
                # RESTRICT: a cached ref that holds measures is not replaced
                # (skipping the delete would leave the slug taken and the insert
                # below would collide). The caller gets a clean BadInput.
                (n_measures,) = conn.execute(
                    "SELECT count(*) FROM measures WHERE subject_ref_id = %(r)s "
                    "OR measurand_ref_id = %(r)s OR experiment_ref_id = %(r)s",
                    {"r": existing[0]},
                ).fetchone() or (0,)
                if n_measures:
                    raise BadInput(
                        f"cached ref {existing[0]} ({kind}:{slug}) holds "
                        f"{n_measures} measure(s) and cannot be replaced",
                        next="measures are append-only: reuse the cached copy, "
                        "or fetch under a different slug",
                    )
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
                self.chunks.insert_chunks(ref.id, body_blocks, conn=conn)

            cache_sql = (
                "INSERT INTO cache_state "
                "  (ref_id, provider, request_hash, model, fetched_at, "
                "   fresh_until, cost_usd, meta, content_sha256) "
                f"VALUES (%s, %s, %s, %s, now(), {fresh_until_sql}, %s, %s, %s) "
                "RETURNING ref_id, provider, request_hash, model, "
                "          fetched_at, fresh_until, cost_usd, meta, content_sha256"
            )
            params: tuple[Any, ...] = (
                ref.id,
                provider,
                request_hash,
                model,
            )
            if ttl_seconds is not None:
                params = params + (
                    str(ttl_seconds),
                    cost_usd,
                    cache_meta_json,
                    content_sha256,
                )
            else:
                params = params + (cost_usd, cache_meta_json, content_sha256)

            cache_row = conn.execute(cache_sql, params).fetchone()
            assert cache_row is not None, (
                "cache_state INSERT returned no row — schema invariant violated"
            )
            cache = _row_to_cache_entry(cache_row)

        return (ref, cache)

    # ── listing / discovery over the cache ────────────────────────────

    def list_cache_entries(
        self,
        *,
        kind: str,
        provider: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[tuple[Ref, CacheEntry]]:
        """Newest-fetch-first page of cached refs for ``kind``.

        Backs the ``/recent`` listings: each row pairs the live ref
        with its ``cache_state`` row so a renderer can show fetch
        date, freshness and provider meta (a youtube channel or
        duration) in one pass — ``list_refs`` alone sees neither
        ``cs.fetched_at`` nor ``cs.meta``.
        """
        clauses = ["refs.retired_at IS NULL", "refs.kind = %s"]
        params: list[Any] = [kind]
        if provider is not None:
            clauses.append("cs.provider = %s")
            params.append(provider)
        params.extend([limit, offset])
        sql = f"""
            SELECT {_REFS_COLS_FOR_CACHE},
                   cs.ref_id, cs.provider, cs.request_hash, cs.model,
                   cs.fetched_at, cs.fresh_until, cs.cost_usd, cs.meta,
                   cs.content_sha256
            FROM cache_state cs
            JOIN refs ON refs.ref_id = cs.ref_id
            WHERE {" AND ".join(clauses)}
            ORDER BY cs.fetched_at DESC, refs.ref_id DESC
            LIMIT %s OFFSET %s
        """
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            (
                _row_to_ref(r[:_REFS_COLS_LEN]),
                _row_to_cache_entry(r[_REFS_COLS_LEN : _REFS_COLS_LEN + 9]),
            )
            for r in rows
        ]

    def count_cache_entries(self, *, kind: str, provider: str | None = None) -> int:
        """Live ``cache_state`` rows for ``kind`` — the "of K" a
        ``/recent`` page header reports; same WHERE as
        :meth:`list_cache_entries` so the two numbers can't drift."""
        clauses = ["refs.retired_at IS NULL", "refs.kind = %s"]
        params: list[Any] = [kind]
        if provider is not None:
            clauses.append("cs.provider = %s")
            params.append(provider)
        sql = (
            "SELECT count(*) FROM cache_state cs "
            "JOIN refs ON refs.ref_id = cs.ref_id "
            f"WHERE {' AND '.join(clauses)}"
        )
        with self.pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        assert row is not None
        return int(row[0])

    def observed_cost_stats(
        self, *, provider: str, model: str, limit: int = 50
    ) -> tuple[int, float, float] | None:
        """``(n, median, p90)`` of observed USD costs for ``(provider, model)``.

        Over the ``limit`` most recent *billed* rows — ``cost_usd > 0``
        and ``meta.cost_estimated`` not ``true`` (a row marked estimated
        recorded the flat fallback constant, not billing data; a row
        without the key at all is treated as observed, matching rows
        written before the flag existed — gr521139). ``None`` when there
        is no billing data, so the caller can fall back to its static
        estimate instead of reporting a fake ``0``.
        """
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT count(*),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY cost_usd),
                       percentile_cont(0.9) WITHIN GROUP (ORDER BY cost_usd)
                  FROM (
                        SELECT cs.cost_usd
                          FROM cache_state cs
                          JOIN refs r ON r.ref_id = cs.ref_id
                         WHERE cs.provider = %s
                           AND cs.model = %s
                           AND cs.cost_usd IS NOT NULL
                           AND cs.cost_usd > 0
                           AND (cs.meta->>'cost_estimated')
                               IS DISTINCT FROM 'true'
                           AND r.retired_at IS NULL
                         ORDER BY cs.fetched_at DESC
                         LIMIT %s
                       ) recent
                """,
                (provider, model, limit),
            ).fetchone()
        if row is None or int(row[0] or 0) == 0:
            return None
        return int(row[0]), float(row[1]), float(row[2])

    def search_cache_entry_titles(
        self,
        *,
        kind: str,
        q: str,
        meta_fields: tuple[str, ...] = (),
        min_similarity: float = 0.3,
        limit: int = 10,
    ) -> list[tuple[Ref, CacheEntry]]:
        """Cached refs of ``kind`` whose *title or cache-meta text* hits ``q``.

        The "which cached <kind> did I mean" leg, fusing three
        signals: title FTS (``websearch_to_tsquery`` — ANDs every
        term), ``pg_trgm`` title similarity ≥ 0.3 (a vague
        half-remembered title the AND misses — same bar
        ``find_papers_by_title`` uses), and an ``ILIKE`` substring
        match on each named ``cache_state.meta`` text field
        (``channel_name`` for youtube). Ranked best-signal first,
        newest fetch breaking ties.

        A leg, not a search — the caller fuses with chunk-level hits.
        ``meta_fields`` are jsonb keys interpolated into the SQL, so
        they must be handler constants: anything that isn't a plain
        identifier is refused rather than quoted into a query.
        ``min_similarity`` lowers the trigram bar for a "closest
        titles" suggestion list (the default is the match bar).
        """
        for f in meta_fields:
            if not _META_FIELD_RE.fullmatch(f):
                raise ValueError(f"unsafe cache meta field name: {f!r}")
        meta_concat = " || ' ' || ".join(
            f"coalesce(cs.meta->>'{f}', '')" for f in meta_fields
        )
        meta_rank_sql = (
            f"CASE WHEN {meta_concat} ILIKE %s ESCAPE '\\' THEN 1.0 ELSE 0.0 END"
            if meta_fields
            else "0.0"
        )
        meta_match_sql = f"OR {meta_concat} ILIKE %s ESCAPE '\\'" if meta_fields else ""
        sql = f"""
            SELECT {_REFS_COLS_FOR_CACHE},
                   cs.ref_id, cs.provider, cs.request_hash, cs.model,
                   cs.fetched_at, cs.fresh_until, cs.cost_usd, cs.meta,
                   cs.content_sha256,
                   greatest(
                       coalesce(ts_rank_cd(
                           to_tsvector('english', {_TITLE_PLAIN_SQL}), qq.qq), 0.0),
                       coalesce(similarity({_TITLE_PLAIN_SQL}, %s), 0.0),
                       {meta_rank_sql}
                   ) AS rank
            FROM cache_state cs
            JOIN refs ON refs.ref_id = cs.ref_id,
                 websearch_to_tsquery('english', %s) qq(qq)
            WHERE refs.retired_at IS NULL
              AND refs.kind = %s
              AND (
                  to_tsvector('english', {_TITLE_PLAIN_SQL}) @@ qq.qq
                  OR similarity({_TITLE_PLAIN_SQL}, %s) >= %s
                  {meta_match_sql}
              )
            ORDER BY rank DESC, cs.fetched_at DESC
            LIMIT %s
        """
        params: list[Any] = [q]
        if meta_fields:
            params.append(_ilike_pattern(q))
        params.extend([q, kind, q, min_similarity])
        if meta_fields:
            params.append(_ilike_pattern(q))
        params.append(limit)
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            (
                _row_to_ref(r[:_REFS_COLS_LEN]),
                _row_to_cache_entry(r[_REFS_COLS_LEN : _REFS_COLS_LEN + 9]),
            )
            for r in rows
        ]


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
    "refs.created_at, refs.updated_at, refs.retired_at, "
    "refs.set_by, refs.authors, refs.year, "
    "refs.human_verified_at, refs.human_verified_by, refs.human_verified_note, "
    "refs.retraction_status, refs.retracted_at, refs.retraction_reason, "
    "refs.retraction_url, refs.retraction_checked_at, "
    "refs.pdf_sha256, refs.pdf_pages::text AS pdf_pages, refs.pdf_role, "
    "refs.auto_refresh_days, refs.refreshed_at, "
    "refs.parent_id, refs.prio, refs.alert_source, refs.fingerprint, "
    "refs.resolved_at, "
    "refs.doi_status, refs.doi_validated_at, refs.owner_login"
)

#: Number of ``cache_state`` columns appended after the ref projection
#: in :meth:`CacheMixin.get_cache_entry` and friends. Pinned to a
#: named constant so the slice positions in those methods don't have
#: to track the SELECT list by hand.
_CACHE_COLS_LEN = 9

# Drift guard. If a future ref-column migration bumps ``_REFS_COLS_LEN``
# (in ``_mappers.py``) without a matching entry being added to
# ``_REFS_COLS_FOR_CACHE`` above, this assertion fires at import time —
# loud, immediate, instead of cascading dozens of "tuple index out of
# range" failures across the cache_state / perplexity / web caches.
# v8.7.1 patched the same drift class in ``_chunks_ops.py``; this is
# the equivalent guard for the cache path.
assert _REFS_COLS_FOR_CACHE.count(",") + 1 == _REFS_COLS_LEN, (
    f"_REFS_COLS_FOR_CACHE drift: projects "
    f"{_REFS_COLS_FOR_CACHE.count(',') + 1} columns but "
    f"_REFS_COLS_LEN={_REFS_COLS_LEN}. Update the projection above to "
    f"match the columns added to refs in the latest migration."
)


__all__ = ["CacheMixin"]

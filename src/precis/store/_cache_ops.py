"""Cache state CRUD against v2 schema. Mixin on
:class:`precis.store.Store`.

Paid-tool caches (``perplexity`` / ``math`` / ``youtube`` / ``web``)
live in the ``refs`` + ``chunks`` tables like every other ref, with
a parallel ``cache_state`` row carrying the freshness + cost
metadata that drives cache-freshness sweeps.

Request hashes address the current result. Its earlier versions remain
readable by their own ref ids and slugs, with their original chunks and
links. See the store package docstring for the versioning contract.
"""

from __future__ import annotations

import logging
import re
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput, NotFound
from precis.store._mappers import (
    _REFS_COLS,
    _REFS_COLS_LEN,
    _row_to_cache_entry,
    _row_to_ref,
)
from precis.store._secret_scope import ref_visible, secret_fence
from precis.store.types import CacheEntry, ChunkInsert, Ref
from precis.utils.title_views import title_plain_sql


def _cache_unavailable() -> NotFound:
    return NotFound(
        "cache key is unavailable",
        next="use a different query or list the visible cached entries",
    )


#: gr515227: title legs read the plain view (markup / LaTeX noise
#: stripped at query time) — titles are stored raw.
_TITLE_PLAIN_SQL = title_plain_sql("refs.title")

#: jsonb key shape allowed in :meth:`CacheMixin.search_cache_entry_titles`'
#: ``meta_fields`` — handler constants only; anything else is refused
#: before it can be quoted into SQL.
_META_FIELD_RE = re.compile(r"[a-z][a-z0-9_]*")

VERSIONED_CACHE_KINDS = frozenset(
    {
        "web",
        "youtube",
        "wikipedia",
        "websearch",
        "perplexity-research",
        "perplexity-reasoning",
    }
)

log = logging.getLogger(__name__)


def _note_meter_spend(conn: Connection, cost_usd: float | None) -> None:
    """Fold a paid fetch's committed cost into the budget meter's shared
    pending counter, inside the caller's transaction (gr732477): a cap
    crossed here gates every process at its next gate read rather than one
    meter baseline later. Best-effort — a metering note must never break
    the cache write it rides on; a skipped note re-enters at the next
    baseline re-sum."""
    if cost_usd is None or cost_usd <= 0:
        return
    try:
        from precis.budget import meter
    except Exception:
        log.debug(
            "cache_state: budget meter import failed; pending note skipped",
            exc_info=True,
        )
        return
    meter.note_spend(conn, cost_usd)


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
            owner_login: str | None = None,
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
              AND cs.is_current
              AND refs.retired_at IS NULL
              AND {secret_fence("refs", path="cache")}
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
                   cs.fetched_at, cs.fresh_until, cs.cost_usd, cs.meta, cs.content_sha256
            FROM cache_state cs
            JOIN refs ON refs.ref_id = cs.ref_id
            WHERE refs.kind = %s
              AND refs.ref_id = %s
              AND refs.retired_at IS NULL
              AND {secret_fence("refs", path="cache")}
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
              AND {secret_fence("refs", path="cache")}
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

    def get_current_cache_version(self, ref_id: int) -> tuple[Ref, CacheEntry] | None:
        """Resolve a visible snapshot's lineage to its current live result."""
        sql = f"""
            SELECT {_REFS_COLS_FOR_CACHE},
                   cs.ref_id, cs.provider, cs.request_hash, cs.model,
                   cs.fetched_at, cs.fresh_until, cs.cost_usd, cs.meta,
                   cs.content_sha256
            FROM cache_state original
            JOIN cache_state cs
              ON COALESCE(cs.version_root, cs.ref_id) =
                 COALESCE(original.version_root, original.ref_id) AND cs.is_current
            JOIN refs ON refs.ref_id = cs.ref_id
            WHERE original.ref_id = %s
              AND refs.retired_at IS NULL
              AND {secret_fence("refs", path="cache")}
        """
        with self.pool.connection() as conn:
            if not ref_visible(self, ref_id, conn=conn):
                return None
            row = conn.execute(sql, (ref_id,)).fetchone()
        if row is None:
            return None
        return _row_to_ref(row[:_REFS_COLS_LEN]), _row_to_cache_entry(
            row[_REFS_COLS_LEN:]
        )

    def _lock_current_cache_version(
        self, conn: Connection, ref_id: int
    ) -> tuple[int, int, bool]:
        """Serialize writes across the lineage, including writes via older ids."""
        row = conn.execute(
            "SELECT COALESCE(version_root, ref_id) FROM cache_state WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
        if row is None or not ref_visible(self, ref_id, conn=conn):
            raise _cache_unavailable()
        root = int(row[0])
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"cache-version:{root}",),
        )
        row = conn.execute(
            "SELECT r.ref_id, r.kind FROM cache_state cs JOIN refs r USING (ref_id) "
            "WHERE COALESCE(cs.version_root, cs.ref_id) = %s AND cs.is_current "
            "AND r.retired_at IS NULL FOR UPDATE OF r, cs",
            (root,),
        ).fetchone()
        if row is None or not ref_visible(self, int(row[0]), conn=conn):
            raise _cache_unavailable()
        return int(row[0]), root, row[1] in VERSIONED_CACHE_KINDS

    def _version_cache_body(
        self,
        conn: Connection,
        *,
        ref_id: int,
        root: int,
        title: str,
        body_blocks: list[ChunkInsert],
        ref_meta: dict[str, Any] | None = None,
    ) -> Ref:
        parent = conn.execute(
            "SELECT kind, provider, meta, owner_login FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
        assert parent is not None, "locked cache ref disappeared during versioning"
        kind, provider, meta, owner_login = parent
        ref = self.insert_ref(
            kind=kind,
            slug=f"{kind}-version-{uuid4().hex}",
            title=title,
            provider=provider,
            meta={**(meta or {}), **(ref_meta or {})},
            owner_login=owner_login,
            conn=conn,
        )
        conn.execute(
            "UPDATE cache_state SET is_current = false WHERE ref_id = %s", (ref_id,)
        )
        conn.execute(
            "INSERT INTO cache_state (ref_id, provider, request_hash, model, fetched_at, "
            "fresh_until, meta, content_sha256, version_root) "
            "SELECT %s, provider, request_hash, model, fetched_at, fresh_until, meta, "
            "content_sha256, %s FROM cache_state WHERE ref_id = %s",
            (ref.id, root, ref_id),
        )
        if body_blocks:
            self.chunks.insert_chunks(ref.id, body_blocks, conn=conn)
        copied = conn.execute(
            "INSERT INTO ref_tags (ref_id, tag_id, set_by, created_at, expires_at, principal) "
            "SELECT %s, tag_id, set_by, created_at, expires_at, principal "
            "FROM ref_tags WHERE ref_id = %s",
            (ref.id, ref_id),
        )
        if copied.rowcount:
            from precis.store._tags_ops import _bust_tag_caches

            _bust_tag_caches(conn)
        conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
            "VALUES (%s, %s, 'supersedes', 'system')",
            (ref.id, ref_id),
        )
        return ref

    def version_youtube_windows(
        self, ref: Ref, expected_chunk_id: int, body_blocks: list[ChunkInsert]
    ) -> Ref | None:
        """Install a derived transcript window set while keeping the original blob."""
        with self.tx() as conn:
            conn.execute("SET LOCAL lock_timeout = '2s'")
            current, root, _ = self._lock_current_cache_version(conn, ref.id)
            if current != ref.id:
                return None
            blocks = self.chunks.list_chunks_for_ref(ref.id, conn=conn)
            if len(blocks) != 1 or blocks[0].id != expected_chunk_id:
                return None
            successor = self._version_cache_body(
                conn, ref_id=ref.id, root=root, title=ref.title, body_blocks=body_blocks
            )
            conn.execute(
                "UPDATE cache_state SET meta = meta || jsonb_build_object('windowed_from', %s::bigint) "
                "WHERE ref_id = %s",
                (ref.id, successor.id),
            )
            return successor

    def check_cache_fill(
        self, *, kind: str, slug: str, provider: str, request_hash: str
    ) -> None:
        """Refuse a fill of a reserved key before calling the provider."""
        with self.pool.connection() as conn:
            hidden = conn.execute(
                "SELECT 1 FROM refs r WHERE r.retired_at IS NULL AND NOT "
                f"({secret_fence('r', path='cache_fill')}) AND ("
                "  (r.kind = %s AND EXISTS (SELECT 1 FROM ref_identifiers ri "
                "    WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key' "
                "      AND ri.id_value = %s)) "
                "  OR EXISTS (SELECT 1 FROM cache_state cs WHERE cs.ref_id = r.ref_id "
                "    AND cs.is_current AND cs.provider = %s AND cs.request_hash = %s)) LIMIT 1",
                (kind, slug, provider, request_hash),
            ).fetchone()
        if hidden is not None:
            raise _cache_unavailable()

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
        ref_meta: dict[str, Any] | None = None,
    ) -> tuple[Ref, CacheEntry]:
        """Refresh a cache lineage, retaining the cited body on supported kinds.

        An identical web response refreshes only metadata. Changed bodies on
        versioned kinds get a new ref, slug and chunks. Request hashes keep
        their existing value unless the caller supplies a new canonical hash.
        """
        fresh_until_sql = (
            "now() + (%s || ' seconds')::interval"
            if ttl_seconds is not None
            else "NULL"
        )

        with self.tx() as conn:
            ref_id, root, versioned = self._lock_current_cache_version(conn, ref_id)

            prior = conn.execute(
                "SELECT content_sha256 FROM cache_state WHERE ref_id = %s", (ref_id,)
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
            if versioned and not unchanged:
                ref = self._version_cache_body(
                    conn,
                    ref_id=ref_id,
                    root=root,
                    title=title,
                    body_blocks=body_blocks,
                    ref_meta=ref_meta,
                )
                ref_id = ref.id
            else:
                row = conn.execute(
                    f"UPDATE refs SET title = %s, updated_at = now(), meta = meta || %s "
                    "WHERE ref_id = %s "
                    f"RETURNING {_REFS_COLS}",
                    (title, Jsonb(ref_meta or {}), ref_id),
                ).fetchone()
                assert row is not None, "locked cache ref disappeared during refresh"
                ref = _row_to_ref(row)
                if not unchanged:
                    conn.execute("DELETE FROM chunks WHERE ref_id = %s", (ref_id,))
                    from precis.store._tags_ops import _bust_tag_caches

                    _bust_tag_caches(conn)
                    if body_blocks:
                        self.chunks.insert_chunks(ref_id, body_blocks, conn=conn)

            cache_sql = (
                "UPDATE cache_state SET request_hash = COALESCE(%s, request_hash), "
                "  model = %s, fetched_at = now(), "
                f"  fresh_until = {fresh_until_sql}, "
                "  cost_usd = %s, meta = %s, content_sha256 = %s "
                "WHERE ref_id = %s "
                "RETURNING ref_id, provider, request_hash, model, "
                "          fetched_at, fresh_until, cost_usd, meta, content_sha256"
            )
            ttl_params = (str(ttl_seconds),) if ttl_seconds is not None else ()
            params = (
                request_hash,
                model,
                *ttl_params,
                cost_usd,
                Jsonb(cache_meta or {}),
                content_sha256,
                ref_id,
            )
            cache_row = conn.execute(cache_sql, params).fetchone()
            if cache_row is None:
                raise ValueError(
                    f"update_cache_entry: cache_state row for ref_id={ref_id} missing"
                )
            cache = _row_to_cache_entry(cache_row)

            # The meter sums cache_state.cost_usd for paid fetches — this
            # fetch's actual committed cost accrues on the shared pending
            # counter in the same txn (gr732477).
            _note_meter_spend(conn, cost_usd)

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

        Versioned kinds reserve their existing slugs. A refresh must use
        :meth:`update_cache_entry` so the snapshot remains addressable.
        Other kinds retain their create-or-replace behavior.

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
            if kind in VERSIONED_CACHE_KINDS:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"cache-fill:{slug}",),
                )
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
                "  AND r.retired_at IS NULL FOR UPDATE OF r",
                (kind, slug),
            ).fetchone()
            if existing is not None:
                if not ref_visible(self, int(existing[0]), conn=conn):
                    raise _cache_unavailable()
                if kind in VERSIONED_CACHE_KINDS:
                    raise BadInput(
                        f"cached ref already exists at {kind}:{slug}",
                        next="re-read the result or use mode='refresh' on its query",
                    )
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
                from precis.store._tags_ops import _bust_tag_caches

                _bust_tag_caches(conn)  # ref_tags rows just cascaded away

            if kind == "news":
                # Keep the retired snapshot restorable by URL until a refill
                # actually takes its request key. Lock against a concurrent
                # restore and release the key in the same tx as the insert.
                retired = conn.execute(
                    "SELECT r.ref_id FROM cache_state cs JOIN refs r USING (ref_id) "
                    "WHERE cs.provider = %s AND cs.request_hash = %s AND cs.is_current "
                    "AND r.kind = 'news' AND r.retired_at IS NOT NULL FOR UPDATE OF r",
                    (provider, request_hash),
                ).fetchone()
                if retired is not None:
                    conn.execute(
                        "UPDATE cache_state SET is_current = false WHERE ref_id = %s",
                        (retired[0],),
                    )

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

            # Same shared pending note as update_cache_entry (gr732477):
            # the fetch's actual cost accrues fleet-wide at commit.
            _note_meter_spend(conn, cost_usd)

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
        clauses = [
            "refs.retired_at IS NULL",
            secret_fence("refs", path="cache"),
            "refs.kind = %s",
            "cs.is_current",
        ]
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
        clauses = [
            "refs.retired_at IS NULL",
            secret_fence("refs", path="cache"),
            "refs.kind = %s",
            "cs.is_current",
        ]
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
              AND {secret_fence("refs", path="cache")}
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

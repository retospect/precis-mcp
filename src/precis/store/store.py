"""Sync postgres-backed store (psycopg 3). One instance per server.

Phase 2 surface: corpus, ref CRUD, tag CRUD, system settings.
Phase 3 adds: block CRUD, hybrid block search, paper bundle ingest.

All methods sync. Each method acquires a connection from the pool for
its work; callers needing multi-statement atomicity use `Store.tx()`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput, NotFound
from precis.store.pool import create_pool
from precis.store.types import ActorSlug, Block, BlockInsert, Density, Ref, Tag

if TYPE_CHECKING:
    from precis.embedder import Embedder
    from precis.ingest import IngestResult

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

    # -- blocks --------------------------------------------------------------

    def search_blocks_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        scope_ref_id: int | None = None,
        limit: int = 20,
    ) -> list[tuple[Block, Ref, float]]:
        """Lexical search over `blocks.tsv`. Returns (block, ref, rank) tuples
        sorted by ts_rank_cd DESC. Only live (non-deleted) refs are
        considered."""
        clauses = [
            "r.deleted_at IS NULL",
            "b.tsv @@ qq.qq",
        ]
        params: list[Any] = [q]
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        if scope_ref_id is not None:
            params.append(scope_ref_id)
            clauses.append("b.ref_id = %s")
        params.append(limit)

        sql = (
            "SELECT b.id, b.ref_id, b.pos, b.slug, b.text, b.token_count, "
            "       NULL::vector, b.density, b.meta, "
            "       b.created_at, b.updated_at, "
            "       r.id, r.corpus_id, r.kind, r.slug, r.title, r.provider, "
            "       r.meta, r.created_at, r.updated_at, r.deleted_at, "
            "       ts_rank_cd(b.tsv, qq.qq) AS rank "
            "FROM blocks b JOIN refs r ON r.id = b.ref_id, "
            "     websearch_to_tsquery('english', %s) qq(qq) "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY rank DESC LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            (_row_to_block(r[:11]), _row_to_ref(r[11:21]), float(r[21])) for r in rows
        ]

    def search_blocks_semantic(
        self,
        *,
        query_vec: list[float],
        kind: str | None = None,
        scope_ref_id: int | None = None,
        limit: int = 20,
    ) -> list[tuple[Block, Ref, float]]:
        """Cosine-distance semantic search via pgvector. Returns
        (block, ref, distance) tuples sorted by distance ASC. Excludes
        blocks that have no embedding."""
        clauses = [
            "r.deleted_at IS NULL",
            "b.embedding IS NOT NULL",
        ]
        where_params: list[Any] = []
        if kind is not None:
            where_params.append(kind)
            clauses.append("r.kind = %s")
        if scope_ref_id is not None:
            where_params.append(scope_ref_id)
            clauses.append("b.ref_id = %s")

        # Param order in the SQL below:
        #   1. %s::vector for the SELECT distance column
        #   2. WHERE clause params (kind, scope_ref_id)
        #   3. %s::vector for ORDER BY
        #   4. LIMIT %s
        params: list[Any] = [query_vec, *where_params, query_vec, limit]

        sql = (
            "SELECT b.id, b.ref_id, b.pos, b.slug, b.text, b.token_count, "
            "       NULL::vector, b.density, b.meta, "
            "       b.created_at, b.updated_at, "
            "       r.id, r.corpus_id, r.kind, r.slug, r.title, r.provider, "
            "       r.meta, r.created_at, r.updated_at, r.deleted_at, "
            "       (b.embedding <=> %s::vector) AS dist "
            "FROM blocks b JOIN refs r ON r.id = b.ref_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY b.embedding <=> %s::vector ASC LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            (_row_to_block(r[:11]), _row_to_ref(r[11:21]), float(r[21])) for r in rows
        ]

    def search_blocks_fused(
        self,
        *,
        q: str,
        query_vec: list[float] | None = None,
        kind: str | None = None,
        scope_ref_id: int | None = None,
        limit: int = 20,
        k: int = 60,
    ) -> list[tuple[Block, Ref, float]]:
        """Hybrid search via reciprocal rank fusion.

        If `query_vec` is None, falls back to lexical-only and returns
        tuples in the same shape (so callers don't branch).

        Score: ``1/(k + lex_rank) + 1/(k + sem_rank)``. Higher is better.
        ``k=60`` is the standard RRF constant.
        """
        if query_vec is None:
            # Lex only, returning ts_rank as the score for shape parity.
            return self.search_blocks_lexical(
                q=q, kind=kind, scope_ref_id=scope_ref_id, limit=limit
            )

        clauses = ["r.deleted_at IS NULL"]
        params: list[Any] = []
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        if scope_ref_id is not None:
            params.append(scope_ref_id)
            clauses.append("b.ref_id = %s")

        # We assemble the WHERE-clause prefix once and inline it into the
        # two CTEs (lex / sem) below.
        where_extra = (" AND " + " AND ".join(clauses)) if clauses else ""

        # Three params slots before LIMIT: q (lex), query_vec ×2 (sem
        # SELECT + ORDER BY).  Then `limit` once for each CTE.
        # Outermost clauses (kind / scope) repeat once per CTE.
        sql = f"""
            WITH lex AS (
                SELECT b.id AS bid,
                       row_number() OVER (
                           ORDER BY ts_rank_cd(b.tsv, qq.qq) DESC
                       ) AS rnk
                FROM blocks b JOIN refs r ON r.id = b.ref_id,
                     websearch_to_tsquery('english', %s) qq(qq)
                WHERE b.tsv @@ qq.qq
                      {where_extra}
                LIMIT %s
            ),
            sem AS (
                SELECT b.id AS bid,
                       row_number() OVER (
                           ORDER BY b.embedding <=> %s::vector ASC
                       ) AS rnk
                FROM blocks b JOIN refs r ON r.id = b.ref_id
                WHERE b.embedding IS NOT NULL
                      {where_extra}
                LIMIT %s
            ),
            fused AS (
                SELECT bid,
                       coalesce(1.0/(%s + (SELECT rnk FROM lex l WHERE l.bid = u.bid)), 0)
                       + coalesce(1.0/(%s + (SELECT rnk FROM sem s WHERE s.bid = u.bid)), 0)
                       AS score
                FROM (
                    SELECT bid FROM lex
                    UNION
                    SELECT bid FROM sem
                ) u
            )
            SELECT b.id, b.ref_id, b.pos, b.slug, b.text, b.token_count,
                   NULL::vector, b.density, b.meta,
                   b.created_at, b.updated_at,
                   r.id, r.corpus_id, r.kind, r.slug, r.title, r.provider,
                   r.meta, r.created_at, r.updated_at, r.deleted_at,
                   fused.score
            FROM fused
            JOIN blocks b ON b.id = fused.bid
            JOIN refs r ON r.id = b.ref_id
            ORDER BY fused.score DESC
            LIMIT %s
        """
        # lex: q + (kind/scope) + limit
        # sem: query_vec + (kind/scope) + limit
        # fused: k + k
        # outer: limit
        full_params: list[Any] = []
        # lex CTE
        full_params.append(q)
        full_params.extend(params)
        full_params.append(limit)
        # sem CTE
        full_params.append(query_vec)
        full_params.extend(params)
        full_params.append(limit)
        # fused CTE
        full_params.extend([k, k])
        # outer
        full_params.append(limit)

        with self.pool.connection() as conn:
            rows = conn.execute(sql, full_params).fetchall()
        return [
            (_row_to_block(r[:11]), _row_to_ref(r[11:21]), float(r[21])) for r in rows
        ]

    def insert_blocks(
        self,
        ref_id: int,
        blocks: list[BlockInsert],
        *,
        replace: bool = False,
        conn: Connection | None = None,
    ) -> list[Block]:
        """Bulk-insert blocks for a ref.

        If `replace=True`, deletes existing blocks for `ref_id` first
        (re-ingest path). Caller owns `pos` numbering — we don't reorder.

        Embedding dimension is enforced by the DB column type (`vector(N)`
        where N = `system.embedding_dim`).
        """
        if not blocks:
            return []

        sql_insert = """
            INSERT INTO blocks
                (ref_id, pos, slug, text, token_count, embedding, density, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, ref_id, pos, slug, text, token_count, embedding,
                      density, meta, created_at, updated_at
        """

        def _do(c: Connection) -> list[Block]:
            if replace:
                c.execute("DELETE FROM blocks WHERE ref_id = %s", (ref_id,))
            out: list[Block] = []
            for b in blocks:
                row = c.execute(
                    sql_insert,
                    (
                        ref_id,
                        b.pos,
                        b.slug,
                        b.text,
                        b.token_count,
                        b.embedding,
                        b.density,
                        Jsonb(b.meta or {}),
                    ),
                ).fetchone()
                assert row is not None
                out.append(_row_to_block(row))
            return out

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def get_block(
        self,
        ref_id: int,
        *,
        pos: int | None = None,
        slug: str | None = None,
        with_embedding: bool = False,
    ) -> Block | None:
        """Look up a single block by (ref_id, pos) or (ref_id, slug)."""
        if (pos is None) == (slug is None):
            raise BadInput(
                "get_block requires exactly one of pos= or slug=",
                next="get_block(ref_id, pos=N)  or  get_block(ref_id, slug='PLXDX')",
            )
        emb_col = "embedding" if with_embedding else "NULL::vector"
        if pos is not None:
            sql = (
                f"SELECT id, ref_id, pos, slug, text, token_count, {emb_col}, "
                "       density, meta, created_at, updated_at "
                "FROM blocks WHERE ref_id = %s AND pos = %s"
            )
            params: tuple[Any, ...] = (ref_id, pos)
        else:
            sql = (
                f"SELECT id, ref_id, pos, slug, text, token_count, {emb_col}, "
                "       density, meta, created_at, updated_at "
                "FROM blocks WHERE ref_id = %s AND slug = %s"
            )
            params = (ref_id, slug)
        with self.pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return _row_to_block(row) if row is not None else None

    def list_blocks_for_ref(
        self,
        ref_id: int,
        *,
        pos_range: tuple[int, int] | None = None,
        with_embedding: bool = False,
    ) -> list[Block]:
        """List blocks for a ref, ordered by pos ASC.

        `pos_range=(lo, hi)` filters inclusively on both ends.
        """
        emb_col = "embedding" if with_embedding else "NULL::vector"
        clauses = ["ref_id = %s"]
        params: list[Any] = [ref_id]
        if pos_range is not None:
            params.extend([pos_range[0], pos_range[1]])
            clauses.append("pos BETWEEN %s AND %s")
        sql = (
            f"SELECT id, ref_id, pos, slug, text, token_count, {emb_col}, "
            "       density, meta, created_at, updated_at "
            "FROM blocks WHERE " + " AND ".join(clauses) + " ORDER BY pos ASC"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_block(r) for r in rows]

    def count_blocks(self, ref_id: int) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM blocks WHERE ref_id = %s", (ref_id,)
            ).fetchone()
        assert row is not None
        return int(row[0])

    def update_block_density(self, block_id: int, density: Density) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE blocks SET density = %s, updated_at = now() WHERE id = %s",
                (density, block_id),
            )

    def update_block_embedding(self, block_id: int, embedding: list[float]) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE blocks SET embedding = %s, updated_at = now() WHERE id = %s",
                (embedding, block_id),
            )

    # -- ingest --------------------------------------------------------------

    def ingest_bundle(
        self,
        path: Path,
        *,
        embedder: Embedder,
        corpus_slug: str = "default",
    ) -> IngestResult:
        """Read a `.acatome` bundle and write it into the v2 schema.

        Idempotency: if a paper with the same DOI is already present
        (kind='paper', meta->>'doi' = bundle.doi), the call is a
        no-op — `IngestResult.inserted=False`. Re-extract is a separate
        operation handled by a future `--force` flag.

        Block embeddings:
        - Bundle blocks carrying a vector matching the embedder's dim
          are inserted as-is (no re-embed cost).
        - Anything else gets re-embedded by `embedder`.

        All work runs in one transaction.
        """
        from precis.ingest import (
            IngestResult,
            fill_embeddings,
            mint_paper_slug,
            parse_bundle,
            read_bundle,
        )

        raw = read_bundle(Path(path))
        parsed = parse_bundle(raw, embedding_dim=embedder.dim)

        # DOI dedupe: short-circuit if we already have this paper. We
        # could also dedupe on (provider, request_hash) for cache kinds
        # but papers don't have a unique-per-content key beyond DOI.
        if parsed.doi:
            with self.pool.connection() as conn:
                row = conn.execute(
                    "SELECT id, slug FROM refs "
                    "WHERE kind = 'paper' AND deleted_at IS NULL "
                    "AND meta->>'doi' = %s "
                    "LIMIT 1",
                    (parsed.doi,),
                ).fetchone()
            if row is not None:
                ref_id, slug = row[0], row[1]
                return IngestResult(
                    ref_id=ref_id,
                    slug=slug,
                    block_count=self.count_blocks(ref_id),
                    inserted=False,
                    embedding_dim=embedder.dim,
                )

        # Embed any block missing a usable vector.
        blocks = fill_embeddings(parsed.blocks, embedder=embedder)

        with self.tx() as conn:
            cid = self.ensure_corpus(corpus_slug)

            # Slug minting: probe via the same connection so the dedup
            # sees this transaction's writes. We're inside `tx()`, so
            # other concurrent ingests can't observe our partial state
            # until commit — collisions resolve by suffixing.
            def _slug_taken(s: str) -> bool:
                row = conn.execute(
                    "SELECT 1 FROM refs WHERE kind='paper' AND slug=%s",
                    (s,),
                ).fetchone()
                return row is not None

            slug = mint_paper_slug(parsed, _slug_taken)

            ref = self.insert_ref(
                corpus_id=cid,
                kind="paper",
                slug=slug,
                title=parsed.title,
                provider=parsed.provider,
                meta=dict(parsed.raw_meta),
                conn=conn,
            )

            # Block insert payloads. Slug minting per block is
            # deferred — phase 3 doesn't need stable per-block citation
            # handles for the agent surface to work; pos is enough.
            inserts = [
                BlockInsert(
                    pos=i,
                    text=b.text,
                    embedding=b.embedding,
                    density=b.density,
                    token_count=len(b.text.split()),
                )
                for i, b in enumerate(blocks)
            ]
            self.insert_blocks(ref.id, inserts, conn=conn)

            # Provenance + density tags. Closed-namespace SRC tag is
            # writable by the system actor only.
            conn.execute(
                "INSERT INTO ref_closed_tags (ref_id, prefix, value, set_by) "
                "VALUES (%s, 'SRC', 'bundle', 'system') "
                "ON CONFLICT DO NOTHING",
                (ref.id,),
            )

        return IngestResult(
            ref_id=ref.id,
            slug=slug,
            block_count=len(blocks),
            inserted=True,
            embedding_dim=embedder.dim,
        )

    def blocks_missing_embeddings(
        self,
        *,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[Block]:
        """Fetch blocks where embedding IS NULL, optionally filtered by
        ref kind. Used by background re-embed jobs."""
        clauses = ["b.embedding IS NULL", "r.deleted_at IS NULL"]
        params: list[Any] = []
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        params.append(limit)
        sql = (
            "SELECT b.id, b.ref_id, b.pos, b.slug, b.text, b.token_count, "
            "       NULL::vector, b.density, b.meta, "
            "       b.created_at, b.updated_at "
            "FROM blocks b JOIN refs r ON r.id = b.ref_id "
            "WHERE " + " AND ".join(clauses) + " "
            "ORDER BY b.id ASC LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_block(r) for r in rows]

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


def _row_to_block(row: tuple) -> Block:
    """Map a blocks row tuple in the order:
    (id, ref_id, pos, slug, text, token_count, embedding, density, meta,
     created_at, updated_at)
    """
    embedding = row[6]
    if embedding is not None and not isinstance(embedding, list):
        # pgvector returns numpy.ndarray when registered; coerce for stable
        # cross-version output.
        embedding = list(map(float, embedding))
    return Block(
        id=row[0],
        ref_id=row[1],
        pos=row[2],
        slug=row[3],
        text=row[4],
        token_count=row[5],
        embedding=embedding,
        density=row[7],
        meta=row[8] or {},
        created_at=row[9],
        updated_at=row[10],
    )


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

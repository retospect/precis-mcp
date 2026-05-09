"""Block-level CRUD + hybrid search. Mixin on :class:`precis.store.Store`.

Blocks are the chunked body rows — one per paragraph / section /
turn / flashcard payload, keyed by ``(ref_id, pos)``. This module
owns the lexical + semantic + fused search paths and the small
CRUD surface (insert, get, list, density/embedding updates).

The fused search is the main engine behind cross-kind agent
queries: ``search_blocks_fused`` runs a lexical ``tsvector`` leg
and a pgvector semantic leg side by side and RRF-merges the
rankings. Both legs share the same noise-filter predicates
(``_block_noise_clauses``) so an unusable block can't sneak in
from one side.

Mixin assumes the concrete Store provides ``self.pool``.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput
from precis.store._mappers import _block_noise_clauses, _row_to_block, _row_to_ref
from precis.store._tag_filter import build_tag_filter
from precis.store.types import Block, BlockInsert, Density, Ref


class BlocksMixin:
    """Block insert / get / list + lexical / semantic / fused search."""

    pool: ConnectionPool

    # -- search ---------------------------------------------------------------

    def count_blocks_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        exclude_ref_ids: list[int] | None = None,
    ) -> int:
        """Count blocks matching the lexical filter (no LIMIT).

        Companion to :meth:`search_blocks_lexical` for pagination
        headers. Same WHERE clause (including the
        ``char_length(btrim(text)) >= 4`` noise-floor guard) so the
        "you're seeing N of K" header reflects the exact universe
        the search would return at infinite limit.

        For ``search_blocks_fused``, this lexical count is the
        primary number agents care about (the semantic CTE only
        affects ranking among lexically-matching rows). For
        ``search_blocks_semantic`` callers, semantic search has no
        meaningful "total" since every embedded block is a hit at
        some distance — those handlers should not display a total.

        ``exclude_ref_ids`` drops blocks belonging to the listed
        refs from the count. Mirrors the same kwarg on
        :meth:`search_blocks_fused` / :meth:`search_blocks_lexical`
        so the ``N of K`` header stays honest under exclusion.
        """
        clauses = [
            "r.deleted_at IS NULL",
            "b.tsv @@ qq.qq",
            *_block_noise_clauses(),
        ]
        params: list[Any] = [q]
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        if scope_ref_id is not None:
            params.append(scope_ref_id)
            clauses.append("b.ref_id = %s")
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            params.extend(tag_params)
        if exclude_ref_ids:
            params.append(list(exclude_ref_ids))
            clauses.append("b.ref_id <> ALL(%s)")
        sql = (
            "SELECT count(*) FROM blocks b JOIN refs r ON r.id = b.ref_id, "
            "     websearch_to_tsquery('english', %s) qq(qq) "
            f"WHERE {' AND '.join(clauses)}"
        )
        with self.pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        assert row is not None
        return int(row[0])

    def search_blocks_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        exclude_ref_ids: list[int] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        """Lexical search over ``blocks.tsv``.

        Returns ``(block, ref, rank)`` tuples sorted by
        ``ts_rank_cd DESC``. Only live (non-deleted) refs are
        considered.

        Blocks whose text strips to fewer than 4 characters are
        excluded — they're punctuation (".", ","), section markers,
        or other formatting artefacts whose embeddings cluster near
        the noise floor. Returning them dilutes results with hits an
        agent can't quote. (MCP critic MAJOR #11.)

        ``exclude_ref_ids`` drops blocks whose owning ref is in the
        list. Applied as a WHERE predicate so ``LIMIT`` operates
        post-exclude — ``limit=10`` with five excluded refs returns
        ten remaining hits, not five.
        """
        clauses = [
            "r.deleted_at IS NULL",
            "b.tsv @@ qq.qq",
            *_block_noise_clauses(),
        ]
        params: list[Any] = [q]
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        if scope_ref_id is not None:
            params.append(scope_ref_id)
            clauses.append("b.ref_id = %s")
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            params.extend(tag_params)
        if exclude_ref_ids:
            params.append(list(exclude_ref_ids))
            clauses.append("b.ref_id <> ALL(%s)")
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
        tags: list[str] | None = None,
        limit: int = 20,
        max_distance: float | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        """Cosine-distance semantic search via pgvector.

        Returns ``(block, ref, distance)`` tuples sorted by
        distance ASC. Excludes blocks that have no embedding
        *and* blocks whose text strips to <4 characters — see
        :meth:`search_blocks_lexical` for the rationale (MCP
        critic MAJOR #11).

        ``max_distance`` is a relevance floor on the cosine distance
        column. Without it, a nonsense query (``'xyzzy frobnicate'``)
        still returns the top-K closest embedded blocks because every
        embedded row is "a hit at some distance" — semantic search
        has no natural zero. The default
        :data:`precis.store._mappers.SEMANTIC_DISTANCE_FLOOR` rejects
        anything more than a loose semantic neighbour, which is the
        right behaviour for the agent surface: a 7B caller asking
        "is there a paper on xyzzy?" should get an empty response,
        not the top-20 random blocks. Pass ``max_distance=None`` to
        opt out (e.g. recommendation / exploration queries that
        genuinely want the closest match regardless of similarity).
        (Critic MAJOR #3.)
        """
        clauses = [
            "r.deleted_at IS NULL",
            "b.embedding IS NOT NULL",
            *_block_noise_clauses(),
        ]
        where_params: list[Any] = []
        if kind is not None:
            where_params.append(kind)
            clauses.append("r.kind = %s")
        if scope_ref_id is not None:
            where_params.append(scope_ref_id)
            clauses.append("b.ref_id = %s")
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            where_params.extend(tag_params)

        # Optional distance floor — applied as a HAVING-style filter
        # on the SELECT-side distance expression. We use the same
        # ``b.embedding <=> %s::vector`` form here as in the SELECT
        # column so the planner can hoist the index scan.
        distance_clause = ""
        distance_params: list[Any] = []
        if max_distance is not None:
            distance_clause = " AND (b.embedding <=> %s::vector) < %s"
            distance_params = [query_vec, float(max_distance)]

        # Param order in the SQL below:
        #   1. %s::vector for the SELECT distance column
        #   2. WHERE clause params (kind, scope_ref_id, tag-filter params)
        #   3. distance-clause params (query_vec + max_distance) [optional]
        #   4. %s::vector for ORDER BY
        #   5. LIMIT %s
        params: list[Any] = [
            query_vec,
            *where_params,
            *distance_params,
            query_vec,
            limit,
        ]

        sql = (
            "SELECT b.id, b.ref_id, b.pos, b.slug, b.text, b.token_count, "
            "       NULL::vector, b.density, b.meta, "
            "       b.created_at, b.updated_at, "
            "       r.id, r.corpus_id, r.kind, r.slug, r.title, r.provider, "
            "       r.meta, r.created_at, r.updated_at, r.deleted_at, "
            "       (b.embedding <=> %s::vector) AS dist "
            "FROM blocks b JOIN refs r ON r.id = b.ref_id "
            f"WHERE {' AND '.join(clauses)}{distance_clause} "
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
        tags: list[str] | None = None,
        limit: int = 20,
        k: int = 60,
        max_distance: float | None = None,
        exclude_ref_ids: list[int] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        """Hybrid search via reciprocal rank fusion.

        If ``query_vec`` is None, falls back to lexical-only and
        returns tuples in the same shape (so callers don't branch).

        Score: ``1/(k + lex_rank) + 1/(k + sem_rank)``. Higher is
        better. ``k=60`` is the standard RRF constant.

        ``max_distance`` is forwarded to the semantic CTE so semantic
        rows past the relevance floor are dropped before the fusion
        UNION. See :meth:`search_blocks_semantic` for the rationale —
        without this, gibberish queries surface semantic-only hits
        because pgvector ``<=>`` always returns *something*. (Critic
        MAJOR #3.)

        ``exclude_ref_ids`` drops blocks whose owning ref is in the
        list before fusion. Applied via the shared ``where_extra``
        clause so the predicate inlines into both CTEs (otherwise
        RRF would fuse a filtered set against an unfiltered one and
        the final ranking would skew toward the unfiltered side —
        same reasoning as the tag filter). The outer ``LIMIT`` then
        runs over the post-exclusion universe, so ``limit=10`` with
        five excluded refs returns the next ten hits.
        """
        if query_vec is None:
            # Lex only, returning ts_rank as the score for shape parity.
            return self.search_blocks_lexical(
                q=q,
                kind=kind,
                scope_ref_id=scope_ref_id,
                tags=tags,
                limit=limit,
                exclude_ref_ids=exclude_ref_ids,
            )

        clauses = [
            "r.deleted_at IS NULL",
            *_block_noise_clauses(),  # MCP critic MAJOR #11 + MINOR #10
        ]
        params: list[Any] = []
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        if scope_ref_id is not None:
            params.append(scope_ref_id)
            clauses.append("b.ref_id = %s")
        # The tag filter has to apply to BOTH CTEs (lex + sem). Otherwise
        # RRF would fuse a filtered set against an unfiltered set and the
        # final ranking would skew toward the unfiltered side. The
        # ``where_extra`` mechanism below already inlines into both, so
        # appending here is enough.
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            params.extend(tag_params)
        # exclude_ref_ids: same both-CTE rule as the tag filter — drop
        # blocks belonging to seen refs before either leg ranks them so
        # the fused ``LIMIT`` operates over the post-exclusion universe.
        if exclude_ref_ids:
            params.append(list(exclude_ref_ids))
            clauses.append("b.ref_id <> ALL(%s)")

        # We assemble the WHERE-clause prefix once and inline it into the
        # two CTEs (lex / sem) below.
        where_extra = (" AND " + " AND ".join(clauses)) if clauses else ""

        # Sem-only relevance floor. If max_distance is set, the sem CTE
        # adds an extra predicate ``(b.embedding <=> %s::vector) < %s``
        # so distant rows never reach RRF. Lex is unaffected — a lexical
        # tsquery already has its own zero (rows that don't match
        # @@ qq.qq are excluded by definition).
        sem_distance_clause = ""
        if max_distance is not None:
            sem_distance_clause = " AND (b.embedding <=> %s::vector) < %s"

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
                      {where_extra}{sem_distance_clause}
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
        # Param construction sequence:
        #   lex: q + (kind/scope) + limit
        #   sem: query_vec + (kind/scope) + [optional: query_vec, max_distance] + limit
        #   fused: k + k
        #   outer: limit
        full_params: list[Any] = []
        # lex CTE
        full_params.append(q)
        full_params.extend(params)
        full_params.append(limit)
        # sem CTE
        full_params.append(query_vec)
        full_params.extend(params)
        if max_distance is not None:
            full_params.append(query_vec)
            full_params.append(float(max_distance))
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

    # -- CRUD -----------------------------------------------------------------

    def insert_blocks(
        self,
        ref_id: int,
        blocks: list[BlockInsert],
        *,
        replace: bool = False,
        conn: Connection | None = None,
    ) -> list[Block]:
        """Bulk-insert blocks for a ref.

        If ``replace=True``, deletes existing blocks for ``ref_id``
        first (re-ingest path). Caller owns ``pos`` numbering — we
        don't reorder.

        Embedding dimension is enforced by the DB column type
        (``vector(N)`` where N = ``system.embedding_dim``).
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
        """Look up a single block by ``(ref_id, pos)`` or ``(ref_id, slug)``."""
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

        ``pos_range=(lo, hi)`` filters inclusively on both ends.
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
        """Total blocks on a ref. Tiny indexed count."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM blocks WHERE ref_id = %s", (ref_id,)
            ).fetchone()
        assert row is not None
        return int(row[0])

    def random_embedded_block(self) -> tuple[Block, Ref] | None:
        """Pick one random undeleted block that has an embedding.

        Drives ``get(kind='random')``. The join + ``ORDER BY
        random() LIMIT 1`` pattern does a full-scan each call,
        which is acceptable for corpora up to ~100k blocks — past
        that the planner still picks a parallel seq-scan and it
        stays well under 100ms. Switching to ``TABLESAMPLE
        SYSTEM_ROWS(1)`` would need the ``tsm_system_rows``
        extension and is a future optimisation only if this
        becomes a hot path.

        Returns ``None`` when the corpus has no embedded blocks
        — a fresh deploy before any ingest. The handler converts
        that to ``NotFound`` with an "ingest first" hint.

        Filters are identical to :meth:`search_blocks_semantic`:
        live ref (``deleted_at IS NULL``) and present vector
        (``embedding IS NOT NULL``). A block with no embedding
        can't be drawn even though its text is present — random
        discovery is vector-search-adjacent in spirit, so we
        keep the same universe.
        """
        sql = (
            "SELECT b.id, b.ref_id, b.pos, b.slug, b.text, b.token_count, "
            "       NULL::vector, b.density, b.meta, "
            "       b.created_at, b.updated_at, "
            "       r.id, r.corpus_id, r.kind, r.slug, r.title, r.provider, "
            "       r.meta, r.created_at, r.updated_at, r.deleted_at "
            "FROM blocks b JOIN refs r ON r.id = b.ref_id "
            "WHERE r.deleted_at IS NULL AND b.embedding IS NOT NULL "
            "ORDER BY random() LIMIT 1"
        )
        with self.pool.connection() as conn:
            row = conn.execute(sql).fetchone()
        if row is None:
            return None
        return _row_to_block(row[:11]), _row_to_ref(row[11:21])

    def update_block_density(self, block_id: int, density: Density) -> None:
        """Set the density bucket (sparse/medium/dense) on a block."""
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE blocks SET density = %s, updated_at = now() WHERE id = %s",
                (density, block_id),
            )

    def update_block_embedding(self, block_id: int, embedding: list[float]) -> None:
        """Write a single block's embedding — used by background re-embed."""
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE blocks SET embedding = %s, updated_at = now() WHERE id = %s",
                (embedding, block_id),
            )

    def blocks_missing_embeddings(
        self,
        *,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[Block]:
        """Fetch blocks where ``embedding IS NULL``, optionally filtered.

        Used by background re-embed jobs.
        """
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


__all__ = ["BlocksMixin"]

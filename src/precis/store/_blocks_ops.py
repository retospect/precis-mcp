"""Block-level CRUD against the v2 ``chunks`` table. Mixin on
:class:`precis.store.Store`.

"Blocks" is the original (v1) name; v2 calls them ``chunks``. The
public Python surface keeps the historical name to avoid churning
~150 handler call sites:

- ``Block.id``           maps to ``chunks.chunk_id``
- ``Block.pos``          maps to ``chunks.ord``
- ``Block.slug``         comes from ``chunks.meta->>'slug'``
- ``Block.embedding``    populated via LEFT JOIN ``chunk_embeddings``
                         on the default embedder (see Phase 4)
- ``Block.density``      populated via LEFT JOIN ``chunk_tags`` + ``tags``
                         filtered on ``namespace='DENSITY'``

Two columns the v2 schema requires that v1 didn't have:

- ``chunks.chunk_kind``  required FK to ``chunk_kinds.slug``;
                         :meth:`insert_blocks` defaults to ``'paragraph'``
                         when the BlockInsert payload doesn't carry a
                         hint. v2 ingesters that want richer typing
                         (cards, figures, equations) write directly via
                         ``precis.ingest.db_writer`` rather than through
                         this mixin.
- ``chunks.section_path``  TEXT[]; populated from ``BlockInsert.meta
                            ['section_path']`` when present, else ``{}``.

**Phase 2 scope**: insert / get / list / count / density+embedding
update / random / blocks_missing_embeddings.

**Phase 3 (not yet implemented)**: the four lexical+semantic+fused
search paths still hold v1 SQL and raise NotImplementedError when
called.

Mixin assumes the concrete Store provides ``self.pool``.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput
from precis.store._mappers import (
    _CHUNKS_COLS,
    _REFS_COLS_ALIASED,
    _block_noise_clauses,
    _row_to_block,
    _row_to_ref,
)
from precis.store._tag_filter import build_tag_filter
from precis.store.types import Block, BlockInsert, Density, Ref

# Default chunk_kind for inserts via this mixin. Phase 2 keeps the
# block surface kind-agnostic; ingesters that want richer typing
# (cards at ord<0, figures, equations) bypass insert_blocks and use
# precis.ingest.db_writer directly.
_DEFAULT_CHUNK_KIND = "paragraph"


class BlocksMixin:
    """Block insert / get / list + (phase 3) lexical / semantic / fused search."""

    pool: ConnectionPool

    # -- helpers ------------------------------------------------------------

    def _default_embedder_name(self, conn: Connection) -> str:
        """Resolve the registered default embedder name (FK target).

        The migration seeds exactly one row in ``embedders`` with
        ``is_default = TRUE`` (``bge-m3``); a partial unique index
        keeps that invariant. We resolve lazily on the assumption
        that the embedder set rarely changes during a process
        lifetime; callers that need it on a hot path can cache.
        """
        row = conn.execute(
            "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "no default embedder registered — "
                "migrations/0001_initial.sql seeds bge-m3; check schema"
            )
        return str(row[0])

    # -- search (Phase 3 — v2 chunks/chunk_embeddings) ---------------------
    #
    # SELECT projection across all four search methods is:
    #   row[0:11]  — chunk columns (matches _row_to_block; embedding
    #                column projected as NULL::vector, density via
    #                correlated subquery on chunk_tags)
    #   row[11:34] — ref columns from _REFS_COLS_ALIASED (23 cols incl.
    #                ref_id-aliased-to-id, slug-via-ref_identifiers,
    #                and the v2-new authors/year/retraction_*/pdf_*)
    #   row[34]    — score (ts_rank for lexical, cosine distance for
    #                semantic, RRF sum for fused)
    #
    # ord >= 0 excludes synthetic card chunks (cards are ref-level
    # introducers; agents searching for content don't want them in
    # the hit list).

    def count_blocks_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        exclude_ref_ids: list[int] | None = None,
    ) -> int:
        """Count chunks matching the lexical filter (no LIMIT).

        Companion to :meth:`search_blocks_lexical` for pagination
        headers. Same WHERE clause (including the noise-floor guard)
        so the "you're seeing N of K" header reflects the exact
        universe the search would return at infinite limit.
        """
        clauses = [
            "r.deleted_at IS NULL",
            "c.ord >= 0",
            "c.tsv @@ qq.qq",
            *_block_noise_clauses(text_alias="c.text"),
        ]
        params: list[Any] = [q]
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        if scope_ref_id is not None:
            params.append(scope_ref_id)
            clauses.append("c.ref_id = %s")
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            params.extend(tag_params)
        if exclude_ref_ids:
            params.append(list(exclude_ref_ids))
            clauses.append("c.ref_id <> ALL(%s)")
        sql = (
            "SELECT count(*) FROM chunks c "
            "JOIN refs r ON r.ref_id = c.ref_id, "
            "websearch_to_tsquery('english', %s) qq(qq) "
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
        offset: int = 0,
        exclude_ref_ids: list[int] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        """Lexical search over ``chunks.tsv``.

        Returns ``(block, ref, rank)`` tuples sorted by
        ``ts_rank_cd DESC``. Only live (non-deleted) refs and body
        chunks (``ord >= 0``) are considered.

        Chunks whose text strips to fewer than 4 characters are
        excluded — they're punctuation, section markers, or other
        formatting artefacts that pollute results with hits agents
        can't quote (MCP critic MAJOR #11).
        """
        clauses = [
            "r.deleted_at IS NULL",
            "c.ord >= 0",
            "c.tsv @@ qq.qq",
            *_block_noise_clauses(text_alias="c.text"),
        ]
        params: list[Any] = [q]
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        if scope_ref_id is not None:
            params.append(scope_ref_id)
            clauses.append("c.ref_id = %s")
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            params.extend(tag_params)
        if exclude_ref_ids:
            params.append(list(exclude_ref_ids))
            clauses.append("c.ref_id <> ALL(%s)")
        params.append(limit)
        params.append(offset)

        proj = _CHUNK_PROJ.format(embedding="NULL::vector")
        sql = (
            f"SELECT {proj}, {_REFS_COLS_ALIASED}, "
            "       ts_rank_cd(c.tsv, qq.qq) AS rank "
            "FROM chunks c "
            "JOIN refs r ON r.ref_id = c.ref_id, "
            "websearch_to_tsquery('english', %s) qq(qq) "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY rank DESC LIMIT %s OFFSET %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            (_row_to_block(r[:11]), _row_to_ref(r[11:34]), float(r[34])) for r in rows
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
        """Cosine-distance semantic search via ``chunk_embeddings``.

        Returns ``(block, ref, distance)`` tuples sorted by distance
        ASC. Excludes chunks that have no embedding under the
        default embedder, chunks with ``ord < 0`` (synthetic cards),
        and chunks whose text strips to <4 characters.

        ``max_distance`` is a relevance floor on the cosine distance
        column. Without it, a nonsense query still returns the top-K
        closest embedded chunks. Pass ``max_distance=None`` to opt
        out (exploration queries that want the closest match
        regardless of similarity).
        """
        with self.pool.connection() as conn:
            embedder = self._default_embedder_name(conn)

        clauses = [
            "r.deleted_at IS NULL",
            "c.ord >= 0",
            "ce.vector IS NOT NULL",
            "ce.status = 'ok'",
            *_block_noise_clauses(text_alias="c.text"),
        ]
        where_params: list[Any] = []
        if kind is not None:
            where_params.append(kind)
            clauses.append("r.kind = %s")
        if scope_ref_id is not None:
            where_params.append(scope_ref_id)
            clauses.append("c.ref_id = %s")
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            where_params.extend(tag_params)

        distance_clause = ""
        distance_params: list[Any] = []
        if max_distance is not None:
            distance_clause = " AND (ce.vector <=> %s::vector) < %s"
            distance_params = [query_vec, float(max_distance)]

        params: list[Any] = [
            query_vec,
            embedder,
            *where_params,
            *distance_params,
            query_vec,
            limit,
        ]

        proj = _CHUNK_PROJ.format(embedding="NULL::vector")
        sql = (
            f"SELECT {proj}, {_REFS_COLS_ALIASED}, "
            "       (ce.vector <=> %s::vector) AS dist "
            "FROM chunks c "
            "JOIN refs r ON r.ref_id = c.ref_id "
            "JOIN chunk_embeddings ce "
            "  ON ce.chunk_id = c.chunk_id AND ce.embedder = %s "
            f"WHERE {' AND '.join(clauses)}{distance_clause} "
            "ORDER BY ce.vector <=> %s::vector ASC LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            (_row_to_block(r[:11]), _row_to_ref(r[11:34]), float(r[34])) for r in rows
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
        offset: int = 0,
        k: int = 60,
        max_distance: float | None = None,
        exclude_ref_ids: list[int] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        """Hybrid search via reciprocal rank fusion over lex + sem.

        If ``query_vec`` is None, falls back to lexical-only and
        returns tuples in the same shape (so callers don't branch).

        Score: ``1/(k + lex_rank) + 1/(k + sem_rank)``. Higher is
        better. ``k=60`` is the standard RRF constant.

        ``offset`` (default 0) skips the first N fused rows for
        pagination. The inner CTEs widen by ``offset`` to keep enough
        candidates for the outer LIMIT/OFFSET slice to be populated.
        """
        if query_vec is None:
            return self.search_blocks_lexical(
                q=q,
                kind=kind,
                scope_ref_id=scope_ref_id,
                tags=tags,
                limit=limit,
                offset=offset,
                exclude_ref_ids=exclude_ref_ids,
            )

        with self.pool.connection() as conn:
            embedder = self._default_embedder_name(conn)

        clauses = [
            "r.deleted_at IS NULL",
            "c.ord >= 0",
            *_block_noise_clauses(text_alias="c.text"),
        ]
        params: list[Any] = []
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        if scope_ref_id is not None:
            params.append(scope_ref_id)
            clauses.append("c.ref_id = %s")
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag.removeprefix(" AND "))
            params.extend(tag_params)
        if exclude_ref_ids:
            params.append(list(exclude_ref_ids))
            clauses.append("c.ref_id <> ALL(%s)")

        where_extra = (" AND " + " AND ".join(clauses)) if clauses else ""

        sem_distance_clause = ""
        if max_distance is not None:
            sem_distance_clause = " AND (ce.vector <=> %s::vector) < %s"

        proj = _CHUNK_PROJ.format(embedding="NULL::vector")
        sql = f"""
            WITH lex AS (
                SELECT c.chunk_id AS cid,
                       row_number() OVER (
                           ORDER BY ts_rank_cd(c.tsv, qq.qq) DESC
                       ) AS rnk
                FROM chunks c JOIN refs r ON r.ref_id = c.ref_id,
                     websearch_to_tsquery('english', %s) qq(qq)
                WHERE c.tsv @@ qq.qq
                      {where_extra}
                LIMIT %s
            ),
            sem AS (
                SELECT c.chunk_id AS cid,
                       row_number() OVER (
                           ORDER BY ce.vector <=> %s::vector ASC
                       ) AS rnk
                FROM chunks c
                JOIN refs r ON r.ref_id = c.ref_id
                JOIN chunk_embeddings ce
                  ON ce.chunk_id = c.chunk_id AND ce.embedder = %s
                WHERE ce.vector IS NOT NULL
                      AND ce.status = 'ok'
                      {where_extra}{sem_distance_clause}
                LIMIT %s
            ),
            fused AS (
                SELECT cid,
                       coalesce(
                           1.0 / (%s + (SELECT rnk FROM lex l WHERE l.cid = u.cid)),
                           0
                       )
                       + coalesce(
                           1.0 / (%s + (SELECT rnk FROM sem s WHERE s.cid = u.cid)),
                           0
                       ) AS score
                FROM (
                    SELECT cid FROM lex
                    UNION
                    SELECT cid FROM sem
                ) u
            )
            SELECT {proj}, {_REFS_COLS_ALIASED}, fused.score
            FROM fused
            JOIN chunks c ON c.chunk_id = fused.cid
            JOIN refs r ON r.ref_id = c.ref_id
            ORDER BY fused.score DESC
            LIMIT %s OFFSET %s
        """

        # Widen the inner CTE LIMITs by ``offset`` so the outer fused
        # ORDER BY ... LIMIT/OFFSET has enough rows to slice from.
        # Without this, page=2 (offset=10) on a query with exactly 10
        # lex hits + 10 sem hits would return nothing because both
        # CTEs are capped at limit=10.
        inner_limit = limit + offset

        # Param construction sequence:
        #   lex: q + WHERE params + inner_limit
        #   sem: query_vec + embedder + WHERE params
        #        + [optional: query_vec, max_distance] + inner_limit
        #   fused: k + k
        #   outer: limit + offset
        full_params: list[Any] = []
        full_params.append(q)
        full_params.extend(params)
        full_params.append(inner_limit)
        full_params.append(query_vec)
        full_params.append(embedder)
        full_params.extend(params)
        if max_distance is not None:
            full_params.append(query_vec)
            full_params.append(float(max_distance))
        full_params.append(inner_limit)
        full_params.extend([k, k])
        full_params.append(limit)
        full_params.append(offset)

        with self.pool.connection() as conn:
            rows = conn.execute(sql, full_params).fetchall()
        return [
            (_row_to_block(r[:11]), _row_to_ref(r[11:34]), float(r[34])) for r in rows
        ]

    # -- CRUD (Phase 2 — v2 chunks table) ----------------------------------

    def insert_blocks(
        self,
        ref_id: int,
        blocks: list[BlockInsert],
        *,
        replace: bool = False,
        conn: Connection | None = None,
    ) -> list[Block]:
        """Bulk-insert chunks (body, ord>=0) for a ref.

        If ``replace=True``, deletes existing chunks for ``ref_id``
        first (re-ingest path). Caller owns ``pos`` numbering — we
        don't reorder.

        v2 mapping per block:
          - ``BlockInsert.pos``    → ``chunks.ord``
          - ``BlockInsert.text``   → ``chunks.text``
          - ``BlockInsert.slug``   → ``chunks.meta['slug']``
          - ``BlockInsert.meta``   → ``chunks.meta`` (merged with slug)
          - ``BlockInsert.embedding`` (if non-None) → row in
                                                    ``chunk_embeddings``
          - ``BlockInsert.density`` (if non-None)   → row in
                                                    ``tags``+``chunk_tags``
          - ``chunk_kind``         → defaults to ``'paragraph'``;
                                     callers can override via
                                     ``BlockInsert.meta['chunk_kind']``.
        """
        if not blocks:
            return []

        def _do(c: Connection) -> list[Block]:
            if replace:
                # v2 cascade: chunks → chunk_embeddings/chunk_summaries/
                # chunk_tags via ON DELETE CASCADE.
                c.execute("DELETE FROM chunks WHERE ref_id = %s", (ref_id,))

            embedder_name: str | None = None
            density_tag_ids: dict[str, int] = {}
            out: list[Block] = []
            for b in blocks:
                # Build the chunks.meta payload: caller's meta merged
                # with the prose slug under 'slug' so it round-trips.
                meta = dict(b.meta or {})
                if b.slug is not None:
                    meta["slug"] = b.slug
                chunk_kind = meta.pop("chunk_kind", _DEFAULT_CHUNK_KIND)
                section_path = list(meta.pop("section_path", ()) or ())
                row = c.execute(
                    "INSERT INTO chunks "
                    "(ref_id, ord, chunk_kind, text, token_count, "
                    " section_path, meta) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    f"RETURNING {_CHUNKS_COLS}",
                    (
                        ref_id,
                        b.pos,
                        chunk_kind,
                        b.text,
                        b.token_count,
                        section_path,
                        Jsonb(meta),
                    ),
                ).fetchone()
                assert row is not None
                block = _row_to_block(row)

                # Embedding side-table write. v2 splits embeddings out
                # so a chunk can carry multiple vectors (one per
                # registered embedder); we write only the default one
                # from this mixin's API.
                if b.embedding is not None:
                    if embedder_name is None:
                        embedder_name = self._default_embedder_name(c)
                    c.execute(
                        "INSERT INTO chunk_embeddings "
                        "(chunk_id, embedder, vector, status, attempts) "
                        "VALUES (%s, %s, %s, 'ok', 1) "
                        "ON CONFLICT (chunk_id, embedder) DO UPDATE "
                        "SET vector = EXCLUDED.vector, status = 'ok', "
                        "    attempts = chunk_embeddings.attempts + 1",
                        (block.id, embedder_name, b.embedding),
                    )

                # Density side-table write. v2 stores density as a
                # tag in namespace='DENSITY'; the partial unique
                # constraint on tags(namespace, value) gives us a
                # natural upsert. Chunk-level via chunk_tags.
                if b.density is not None:
                    tag_id = density_tag_ids.get(b.density)
                    if tag_id is None:
                        tag_id = _upsert_tag(c, "DENSITY", b.density)
                        density_tag_ids[b.density] = tag_id
                    c.execute(
                        "INSERT INTO chunk_tags (chunk_id, tag_id, set_by) "
                        "VALUES (%s, %s, 'system') "
                        "ON CONFLICT (chunk_id, tag_id) DO NOTHING",
                        (block.id, tag_id),
                    )

                # Re-read so the returned Block carries the post-write
                # density/embedding state (the initial _row_to_block
                # row had them NULL on the SELECT projection).
                out.append(
                    _refetch_block(c, block.id) if (b.embedding or b.density) else block
                )
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
        """Look up a single chunk by ``(ref_id, pos)`` or ``(ref_id, slug)``.

        Slug lookup matches ``chunks.meta->>'slug'``.
        """
        if (pos is None) == (slug is None):
            raise BadInput(
                "get_block requires exactly one of pos= or slug=",
                next="get_block(ref_id, pos=N)  or  get_block(ref_id, slug='PLXDX')",
            )
        if pos is not None:
            where = "c.ref_id = %s AND c.ord = %s"
            params: tuple[Any, ...] = (ref_id, pos)
        else:
            where = "c.ref_id = %s AND (c.meta->>'slug') = %s"
            params = (ref_id, slug)
        with self.pool.connection() as conn:
            return _fetch_block_one(conn, where, params, with_embedding=with_embedding)

    def list_blocks_for_ref(
        self,
        ref_id: int,
        *,
        pos_range: tuple[int, int] | None = None,
        with_embedding: bool = False,
    ) -> list[Block]:
        """List chunks for a ref, ordered by ord ASC.

        Excludes synthetic card chunks (ord < 0). ``pos_range=(lo, hi)``
        filters inclusively on both ends.
        """
        clauses = ["c.ref_id = %s", "c.ord >= 0"]
        params: list[Any] = [ref_id]
        if pos_range is not None:
            params.extend([pos_range[0], pos_range[1]])
            clauses.append("c.ord BETWEEN %s AND %s")
        where = " AND ".join(clauses)
        with self.pool.connection() as conn:
            return _fetch_blocks(conn, where, params, with_embedding=with_embedding)

    def count_blocks(self, ref_id: int) -> int:
        """Total body chunks on a ref (ord>=0). Tiny indexed count."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord >= 0",
                (ref_id,),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def random_embedded_block(self) -> tuple[Block, Ref] | None:
        """Pick one random undeleted body chunk that has an embedding.

        Drives ``get(kind='random')``. Filters mirror Phase-3
        ``search_blocks_semantic``: live ref (``deleted_at IS NULL``),
        body chunk (ord>=0), and a present vector in
        ``chunk_embeddings`` for the default embedder.
        """
        with self.pool.connection() as conn:
            embedder = self._default_embedder_name(conn)
            sql = (
                "SELECT c.chunk_id AS id, c.ref_id, c.ord AS pos, "
                "       (c.meta->>'slug') AS slug, c.text, c.token_count, "
                "       NULL::vector AS embedding, NULL::text AS density, "
                "       c.meta, c.created_at, c.created_at AS updated_at, "
                f"       {_REFS_COLS_ALIASED} "
                "FROM chunks c "
                "JOIN refs r ON r.ref_id = c.ref_id "
                "JOIN chunk_embeddings ce "
                "  ON ce.chunk_id = c.chunk_id AND ce.embedder = %s "
                "  AND ce.status = 'ok' AND ce.vector IS NOT NULL "
                "WHERE r.deleted_at IS NULL AND c.ord >= 0 "
                "ORDER BY random() LIMIT 1"
            )
            row = conn.execute(sql, (embedder,)).fetchone()
        if row is None:
            return None
        return _row_to_block(row[:11]), _row_to_ref(row[11:])

    def update_block_density(self, block_id: int, density: Density) -> None:
        """Set the density bucket (sparse/medium/dense) on a chunk.

        v2 stores density as a tag in ``namespace='DENSITY'``. Idempotent:
        delete the prior DENSITY tag for this chunk, then insert the new
        one. Both the tags row (namespace, value) and the chunk_tags
        join row are upserts.
        """
        with self.pool.connection() as conn:
            with conn.transaction():
                # Drop any prior DENSITY tag(s) for this chunk so a
                # bump from sparse→dense doesn't leave both rows.
                conn.execute(
                    "DELETE FROM chunk_tags ct "
                    "USING tags t "
                    "WHERE ct.chunk_id = %s "
                    "  AND ct.tag_id = t.tag_id "
                    "  AND t.namespace = 'DENSITY'",
                    (block_id,),
                )
                tag_id = _upsert_tag(conn, "DENSITY", density)
                conn.execute(
                    "INSERT INTO chunk_tags (chunk_id, tag_id, set_by) "
                    "VALUES (%s, %s, 'system') "
                    "ON CONFLICT (chunk_id, tag_id) DO NOTHING",
                    (block_id, tag_id),
                )

    def update_block_embedding(self, block_id: int, embedding: list[float]) -> None:
        """Write a single chunk's embedding — used by background re-embed.

        v2: embeddings live in ``chunk_embeddings``, keyed by
        ``(chunk_id, embedder)``. We upsert into the default embedder's
        slot. ``status='ok'`` and ``attempts`` increment on retry.
        """
        with self.pool.connection() as conn:
            embedder = self._default_embedder_name(conn)
            conn.execute(
                "INSERT INTO chunk_embeddings "
                "(chunk_id, embedder, vector, status, attempts) "
                "VALUES (%s, %s, %s, 'ok', 1) "
                "ON CONFLICT (chunk_id, embedder) DO UPDATE "
                "SET vector = EXCLUDED.vector, status = 'ok', "
                "    attempts = chunk_embeddings.attempts + 1",
                (block_id, embedder, embedding),
            )

    def blocks_missing_embeddings(
        self,
        *,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[Block]:
        """Fetch body chunks that lack an embedding under the default embedder.

        v2: ``chunk_embeddings`` is sparse; a chunk without a row for
        the default embedder is "missing". A row with ``status='failed'``
        also counts as missing so background re-embed retries pick it up.
        """
        clauses = [
            "r.deleted_at IS NULL",
            "c.ord >= 0",
            "(ce.vector IS NULL OR ce.status = 'failed')",
        ]
        params: list[Any] = []
        if kind is not None:
            params.append(kind)
            clauses.append("r.kind = %s")
        params.append(limit)
        with self.pool.connection() as conn:
            embedder = self._default_embedder_name(conn)
            sql = (
                "SELECT c.chunk_id AS id, c.ref_id, c.ord AS pos, "
                "       (c.meta->>'slug') AS slug, c.text, c.token_count, "
                "       NULL::vector AS embedding, NULL::text AS density, "
                "       c.meta, c.created_at, c.created_at AS updated_at "
                "FROM chunks c "
                "JOIN refs r ON r.ref_id = c.ref_id "
                "LEFT JOIN chunk_embeddings ce "
                "  ON ce.chunk_id = c.chunk_id AND ce.embedder = %s "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY c.chunk_id ASC LIMIT %s"
            )
            rows = conn.execute(sql, [embedder, *params]).fetchall()
        return [_row_to_block(r) for r in rows]


# -- module-level helpers ---------------------------------------------------


def _upsert_tag(conn: Connection, namespace: str, value: str) -> int:
    """Upsert into ``tags(namespace, value)`` and return the tag_id.

    Uses an INSERT ... ON CONFLICT DO UPDATE SET namespace=excluded.namespace
    trick so the RETURNING clause fires on both the insert and the
    no-op conflict path. (The DO NOTHING form returns nothing on
    conflict, forcing a follow-up SELECT.)
    """
    row = conn.execute(
        "INSERT INTO tags (namespace, value) VALUES (%s, %s) "
        "ON CONFLICT (namespace, value) "
        "DO UPDATE SET namespace = EXCLUDED.namespace "
        "RETURNING tag_id",
        (namespace, value),
    ).fetchone()
    assert row is not None
    return int(row[0])


# Aliased chunk projection used by the fetch helpers. Mirrors
# ``_CHUNKS_COLS_ALIASED`` but written out inline because the
# fetch helpers need to swap the embedding column in/out per call.
# Density is populated via a correlated subquery against
# ``chunk_tags`` + ``tags`` filtered on ``namespace='DENSITY'``;
# embedding is the one slot that varies (NULL::vector when the
# caller doesn't ask, ce.vector when LEFT JOIN'd against
# ``chunk_embeddings``).
_CHUNK_PROJ = (
    "c.chunk_id AS id, c.ref_id, c.ord AS pos, "
    "(c.meta->>'slug') AS slug, c.text, c.token_count, "
    "{embedding} AS embedding, "
    "(SELECT t.value FROM chunk_tags ct "
    "   JOIN tags t ON t.tag_id = ct.tag_id "
    "   WHERE ct.chunk_id = c.chunk_id AND t.namespace = 'DENSITY' "
    "   LIMIT 1) AS density, "
    "c.meta, c.created_at, c.created_at AS updated_at"
)


def _fetch_block_one(
    conn: Connection,
    where: str,
    params: tuple[Any, ...],
    *,
    with_embedding: bool,
) -> Block | None:
    """SELECT one chunk row mapped to Block. with_embedding=True LEFT
    JOINs the default embedder's vector into the projection."""
    if with_embedding:
        embedder_name = _select_default_embedder(conn)
        proj = _CHUNK_PROJ.format(embedding="ce.vector")
        sql = (
            f"SELECT {proj} FROM chunks c "
            "LEFT JOIN chunk_embeddings ce "
            "  ON ce.chunk_id = c.chunk_id AND ce.embedder = %s "
            f"WHERE {where}"
        )
        row = conn.execute(sql, (embedder_name, *params)).fetchone()
    else:
        proj = _CHUNK_PROJ.format(embedding="NULL::vector")
        sql = f"SELECT {proj} FROM chunks c WHERE {where}"
        row = conn.execute(sql, params).fetchone()
    return _row_to_block(row) if row is not None else None


def _fetch_blocks(
    conn: Connection,
    where: str,
    params: list[Any],
    *,
    with_embedding: bool,
) -> list[Block]:
    """SELECT many chunk rows ordered by ord ASC. Mirrors
    :func:`_fetch_block_one` projection."""
    if with_embedding:
        embedder_name = _select_default_embedder(conn)
        proj = _CHUNK_PROJ.format(embedding="ce.vector")
        sql = (
            f"SELECT {proj} FROM chunks c "
            "LEFT JOIN chunk_embeddings ce "
            "  ON ce.chunk_id = c.chunk_id AND ce.embedder = %s "
            f"WHERE {where} ORDER BY c.ord ASC"
        )
        rows = conn.execute(sql, [embedder_name, *params]).fetchall()
    else:
        proj = _CHUNK_PROJ.format(embedding="NULL::vector")
        sql = f"SELECT {proj} FROM chunks c WHERE {where} ORDER BY c.ord ASC"
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_block(r) for r in rows]


def _select_default_embedder(conn: Connection) -> str:
    row = conn.execute(
        "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "no default embedder registered — "
            "migrations/0001_initial.sql seeds bge-m3; check schema"
        )
    return str(row[0])


def _refetch_block(conn: Connection, chunk_id: int) -> Block:
    """Re-read a chunk row with embedding + density projected.

    Used after insert_blocks writes side-table rows so the returned
    Block carries the post-write state.
    """
    embedder_name = _select_default_embedder(conn)
    sql = (
        "SELECT c.chunk_id AS id, c.ref_id, c.ord AS pos, "
        "       (c.meta->>'slug') AS slug, c.text, c.token_count, "
        "       ce.vector AS embedding, "
        "       (SELECT t.value FROM chunk_tags ct "
        "          JOIN tags t ON t.tag_id = ct.tag_id "
        "          WHERE ct.chunk_id = c.chunk_id "
        "          AND t.namespace = 'DENSITY' LIMIT 1) AS density, "
        "       c.meta, c.created_at, c.created_at AS updated_at "
        "FROM chunks c "
        "LEFT JOIN chunk_embeddings ce "
        "  ON ce.chunk_id = c.chunk_id AND ce.embedder = %s "
        "WHERE c.chunk_id = %s"
    )
    row = conn.execute(sql, (embedder_name, chunk_id)).fetchone()
    assert row is not None
    return _row_to_block(row)


__all__ = ["BlocksMixin"]

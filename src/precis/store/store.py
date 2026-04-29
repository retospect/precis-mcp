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
from typing import TYPE_CHECKING, Any, Literal, Self

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput, NotFound
from precis.store._tag_filter import build_tag_filter
from precis.store.pool import create_pool
from precis.store.types import (
    _INVERSE_RELATIONS,
    ActorSlug,
    Block,
    BlockInsert,
    CacheEntry,
    Density,
    Link,
    Ref,
    Relation,
    Tag,
)

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

# Default cosine-distance floor for semantic-only hits. pgvector's
# `<=>` operator returns ``1 - cosine_similarity``, so a distance of
# 0.9 means ``cos(theta) ≈ 0.1`` — the two vectors are nearly
# perpendicular, i.e. the embedding model thinks they have almost
# nothing to do with each other. We reject anything past this
# threshold from the semantic CTE so a nonsense query
# (``'xyzzy frobnicate quux'``) returns an honest empty response
# instead of a top-K of arbitrary blocks. The MCP critic flagged
# this as MAJOR #3: search has no relevance floor, gibberish
# queries return ranked hits. The threshold is a default, not a
# constraint — callers (and the eventual public ``min_score=`` knob)
# can override per call. 0.9 is generous enough to keep weakly-
# related but legitimately near hits, strict enough to exclude the
# random-block tail.
SEMANTIC_DISTANCE_FLOOR = 0.9


# Search-time noise filters. Two predicates wrap every block-search
# WHERE clause; both are necessary to keep low-information blocks
# from polluting the agent's view.
#
#   _MIN_BLOCK_CHARS:   minimum trimmed text length (excludes
#                       single punctuation, section markers, etc.)
#   _MARKUP_ONLY_BLOCK: PostgreSQL POSIX regex matching blocks
#                       whose body is pure HTML markup with no
#                       readable content. The MCP critic flagged
#                       ``<span id="page-N-0"></span>`` anchor blocks
#                       surfacing as top hits on noise-probe queries —
#                       they're 30+ chars (so the length floor lets
#                       them through) but carry zero semantic content.
#
# Both predicates appear in every block-search SQL clause via
# :func:`_block_noise_clauses` so any new search method picks them up
# uniformly. (Critic MAJOR #11 + MINOR #10.)
_MIN_BLOCK_CHARS = 4
_MARKUP_ONLY_BLOCK = r"^[[:space:]]*<span[^>]*></span>[[:space:]]*$"


def _block_noise_clauses(text_alias: str = "b.text") -> list[str]:
    """SQL predicates that drop blocks unfit for agent consumption.

    Returned as a plain list of WHERE-clause fragments (no leading
    ``AND``); callers concatenate via the same ``" AND ".join``
    they already use for the rest of the WHERE.

    Parameters mirror the alias the caller picked for the ``blocks``
    table (``b`` everywhere in this module today, but kept
    parametric so a future view-aliased call site doesn't have to
    rename).
    """
    return [
        f"char_length(btrim({text_alias})) >= {_MIN_BLOCK_CHARS}",
        # PostgreSQL POSIX regex via the ``~`` operator. We compare
        # against the constant rather than a parameter because the
        # pattern is a static cleanup rule, not user input — and
        # parameterising would force a separate ``$N`` slot per
        # call site.
        f"{text_alias} !~ '{_MARKUP_ONLY_BLOCK}'",
    ]


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
        """Lexical search over `refs.title_tsv`. Returns (ref, rank) sorted
        by rank desc. Phase 3 will add semantic + RRF fusion."""
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

    # -- blocks --------------------------------------------------------------

    def count_blocks_lexical(
        self,
        *,
        q: str,
        kind: str | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
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
    ) -> list[tuple[Block, Ref, float]]:
        """Lexical search over `blocks.tsv`. Returns (block, ref, rank) tuples
        sorted by ts_rank_cd DESC. Only live (non-deleted) refs are
        considered.

        Blocks whose text strips to fewer than 4 characters are
        excluded — they're punctuation (".", ","), section markers,
        or other formatting artefacts whose embeddings cluster near
        the noise floor. Returning them dilutes results with hits an
        agent can't quote. (MCP critic MAJOR #11.)
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
        """Cosine-distance semantic search via pgvector. Returns
        (block, ref, distance) tuples sorted by distance ASC.

        Excludes blocks that have no embedding *and* blocks whose
        text strips to <4 characters — see ``search_blocks_lexical``
        for the rationale (MCP critic MAJOR #11).

        ``max_distance`` is a relevance floor on the cosine distance
        column. Without it, a nonsense query (``'xyzzy frobnicate'``)
        still returns the top-K closest embedded blocks because every
        embedded row is "a hit at some distance" — semantic search
        has no natural zero. The default
        :data:`SEMANTIC_DISTANCE_FLOOR` rejects anything more than a
        loose semantic neighbour, which is the right behaviour for
        the agent surface: a 7B caller asking "is there a paper on
        xyzzy?" should get an empty response, not the top-20 random
        blocks. Pass ``max_distance=None`` to opt out (e.g.
        recommendation/exploration queries that genuinely want the
        closest match regardless of similarity). (Critic MAJOR #3.)
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
    ) -> list[tuple[Block, Ref, float]]:
        """Hybrid search via reciprocal rank fusion.

        If `query_vec` is None, falls back to lexical-only and returns
        tuples in the same shape (so callers don't branch).

        Score: ``1/(k + lex_rank) + 1/(k + sem_rank)``. Higher is better.
        ``k=60`` is the standard RRF constant.

        ``max_distance`` is forwarded to the semantic CTE so semantic
        rows past the relevance floor are dropped before the fusion
        UNION. See :meth:`search_blocks_semantic` for the rationale —
        without this, gibberish queries surface semantic-only hits
        because pgvector ``<=>`` always returns *something*. (Critic
        MAJOR #3.)
        """
        if query_vec is None:
            # Lex only, returning ts_rank as the score for shape parity.
            return self.search_blocks_lexical(
                q=q, kind=kind, scope_ref_id=scope_ref_id, tags=tags, limit=limit
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
        # lex: q + (kind/scope) + limit
        # sem: query_vec + (kind/scope) + [optional: query_vec, max_distance] + limit
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

    # -- cache_state ---------------------------------------------------------

    def get_cache_entry(
        self,
        *,
        provider: str,
        request_hash: str,
    ) -> tuple[Ref, CacheEntry] | None:
        """Look up a cached ref + freshness row by `(provider, request_hash)`.

        Returns the joined `(Ref, CacheEntry)` pair if found, else None.
        Soft-deleted refs are excluded — a deleted ref is not a cache hit.
        Caller decides freshness via `CacheEntry.fresh_until` vs `now()`.
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

        On replace (existing ref with same kind+slug), all old blocks
        are deleted via cascade and replaced with `body_blocks`. The
        cache_state row is upserted on `(provider, request_hash)`.

        `ttl_seconds=None` pins the entry (never expires).
        `ttl_seconds=0` is allowed but means the entry is born stale
        — useful only for testing.
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

    # -- tags ----------------------------------------------------------------

    def add_tag(
        self,
        ref_id: int,
        tag: Tag,
        *,
        pos: int | None = None,
        set_by: ActorSlug = "agent",
        replace_prefix: bool = False,
        conn: Connection | None = None,
    ) -> None:
        """Add a tag to a ref (or block, with `pos`).

        Args:
            replace_prefix: For closed tags only. If True, removes any
                existing closed tag with the same prefix before inserting.
                Used by the skill semantics ("CONFIDENCE:certain replaces
                previous CONFIDENCE:*").
            conn: Optional existing connection — pass when the caller
                already owns a transaction (e.g. ``Store.tx()`` block)
                so the tag write joins the surrounding atomic unit.
                The MCP critic flagged a state-drift bug where a put-
                create that failed tag validation still committed the
                ref insert; the handler now wraps insert + tag adds
                in one ``tx()`` and threads the connection through
                this kwarg so a downstream rollback discards both.
        """
        db_pos = _pos_to_db(pos)

        def _do(c: Connection) -> None:
            if tag.namespace == "closed":
                assert tag.prefix is not None
                if replace_prefix:
                    c.execute(
                        "DELETE FROM ref_closed_tags "
                        "WHERE ref_id = %s AND pos = %s AND prefix = %s",
                        (ref_id, db_pos, tag.prefix),
                    )
                c.execute(
                    "INSERT INTO ref_closed_tags "
                    "(ref_id, pos, prefix, value, set_by) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (ref_id, db_pos, tag.prefix, tag.value, set_by),
                )
            elif tag.namespace == "flag":
                c.execute(
                    "INSERT INTO ref_flags (ref_id, pos, name, set_by) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (ref_id, db_pos, tag.value, set_by),
                )
            else:
                c.execute(
                    "INSERT INTO ref_open_tags (ref_id, pos, value, set_by) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (ref_id, db_pos, tag.value, set_by),
                )

        if conn is not None:
            _do(conn)
        else:
            with self.pool.connection() as c:
                _do(c)

    def remove_tag(
        self,
        ref_id: int,
        tag: Tag,
        *,
        pos: int | None = None,
        conn: Connection | None = None,
    ) -> None:
        """Remove a tag from a ref (or block, with `pos`).

        ``conn=`` mirrors :meth:`add_tag` so handler updates can
        bundle remove + add into one transaction.
        """
        db_pos = _pos_to_db(pos)

        def _do(c: Connection) -> None:
            if tag.namespace == "closed":
                assert tag.prefix is not None
                c.execute(
                    "DELETE FROM ref_closed_tags "
                    "WHERE ref_id = %s AND pos = %s "
                    "AND prefix = %s AND value = %s",
                    (ref_id, db_pos, tag.prefix, tag.value),
                )
            elif tag.namespace == "flag":
                c.execute(
                    "DELETE FROM ref_flags "
                    "WHERE ref_id = %s AND pos = %s AND name = %s",
                    (ref_id, db_pos, tag.value),
                )
            else:
                c.execute(
                    "DELETE FROM ref_open_tags "
                    "WHERE ref_id = %s AND pos = %s AND value = %s",
                    (ref_id, db_pos, tag.value),
                )

        if conn is not None:
            _do(conn)
        else:
            with self.pool.connection() as c:
                _do(c)

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

    def find_first_meta_for_open_tag(
        self,
        *,
        kind: str,
        tag: str,
    ) -> dict[str, Any] | None:
        """Return ``refs.meta`` for any one live ref of ``kind`` carrying
        the open-tag value ``tag`` (e.g. ``'applicant:siemens-ag'``).

        Used by the patent CQL lift to recover canonical applicant
        spelling from a previously-ingested patent. Limit 1 — we only
        need any matching meta to read the embedded applicant list.
        Returns None when no such ref exists.
        """
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT r.meta
                FROM   refs r
                JOIN   ref_open_tags t ON t.ref_id = r.id
                WHERE  r.kind = %s
                  AND  t.value = %s
                  AND  r.deleted_at IS NULL
                LIMIT 1
                """,
                (kind, tag),
            ).fetchone()
        if row is None:
            return None
        return row[0] if isinstance(row[0], dict) else None

    # -- links ---------------------------------------------------------------

    def add_link(
        self,
        *,
        src_ref_id: int,
        dst_ref_id: int,
        relation: Relation = "related-to",
        src_pos: int | None = None,
        dst_pos: int | None = None,
        set_by: ActorSlug = "agent",
        meta: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> Link:
        """Insert a link row, idempotent on the unique tuple.

        The schema's `UNIQUE (src_ref_id, src_pos, dst_ref_id, dst_pos,
        relation)` means a re-insert with the same arguments is a
        no-op. We use `ON CONFLICT (...) DO UPDATE SET set_by =
        links.set_by` so the `RETURNING` clause yields the existing
        row on conflict — this avoids the extra SELECT that
        `DO NOTHING` would force.

        Identity self-loops (same ref + same pos) are rejected by
        the schema's `CHECK` constraint, surfaced here as a
        `BadInput` because that's the right error class for an
        agent-driven misuse.

        Same-ref different-pos links are allowed (e.g. block~5 →
        block~7 within one long memory ref) — the check is
        position-aware.

        **One row per edge.** Asymmetric pairs (``cites`` /
        ``cited-by``) are NOT auto-mirrored — exactly one row is
        inserted regardless of relation. The "who cites me?"
        filter that motivated the MCP critic's request is solved
        at *read* time in :meth:`links_for`, which rewrites
        ``relation='cited-by'`` into a dst-side match against
        ``relation='cites'``. This keeps the unique-edge invariant
        intact, avoids drift between the two sides, and matches
        the design choice documented in
        ``migrations/0005_link_relations.sql``.
        """
        if src_ref_id == dst_ref_id and _pos_to_db(src_pos) == _pos_to_db(dst_pos):
            raise BadInput(
                "cannot link a ref to itself at the same position",
                next=(
                    "use different src_pos/dst_pos if linking blocks "
                    "within one ref, or pick a different target"
                ),
            )
        sql = """
            INSERT INTO links
                (src_ref_id, src_pos, dst_ref_id, dst_pos,
                 relation, set_by, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (src_ref_id, src_pos, dst_ref_id, dst_pos, relation)
            DO UPDATE SET set_by = links.set_by
            RETURNING id, src_ref_id, src_pos, dst_ref_id, dst_pos,
                      relation, set_by, meta, created_at
        """
        params = (
            src_ref_id,
            _pos_to_db(src_pos),
            dst_ref_id,
            _pos_to_db(dst_pos),
            relation,
            set_by,
            Jsonb(meta or {}),
        )
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, params).fetchone()
        assert row is not None
        return _row_to_link(row)

    def remove_link(
        self,
        *,
        src_ref_id: int,
        dst_ref_id: int,
        relation: Relation | None = None,
        src_pos: int | None = None,
        dst_pos: int | None = None,
        conn: Connection | None = None,
    ) -> int:
        """Remove links matching the given (src, dst, [pos pair, [relation]]).

        ``relation=None`` removes **all** links between the given
        positions regardless of relation. The handler-level
        ``unlink=`` kwarg always passes a specific relation so this
        broader form is a Store-only escape hatch (used by tests
        and future bulk operations). Returns the number of rows
        deleted; missing links are a silent no-op (rowcount=0).

        Asymmetric pairs (``cites`` / ``cited-by``) are stored as a
        single row whose direction is the one the agent named at
        write time. Removing it removes the edge regardless of
        which inverse name is in flight at read time (see
        :meth:`links_for` for the read-side rewrite).
        """
        clauses = [
            "src_ref_id = %s",
            "src_pos = %s",
            "dst_ref_id = %s",
            "dst_pos = %s",
        ]
        params: list[Any] = [
            src_ref_id,
            _pos_to_db(src_pos),
            dst_ref_id,
            _pos_to_db(dst_pos),
        ]
        if relation is not None:
            clauses.append("relation = %s")
            params.append(relation)
        sql = f"DELETE FROM links WHERE {' AND '.join(clauses)}"
        if conn is not None:
            cur = conn.execute(sql, params)
        else:
            with self.pool.connection() as c:
                cur = c.execute(sql, params)
        return cur.rowcount

    def links_for(
        self,
        ref_id: int,
        *,
        direction: Literal["out", "in", "both"] = "both",
        relation: Relation | None = None,
    ) -> list[Link]:
        """Fetch links touching ``ref_id``.

        ``direction='out'``: rows where ref_id is the source.
        ``direction='in'``:  rows where ref_id is the destination.
        ``direction='both'`` (default): both, no deduplication —
        a self-link (different positions) shows up twice and that's
        correct.

        ``relation=None`` returns every relation. Inbound rows keep
        their stored relation slug; the renderer maps to inverse
        labels via ``relations.inverse_slug`` for human-readable
        prose.

        **Inverse-relation rewrite.** When ``relation`` is the
        inverse half of an asymmetric pair (e.g. ``'cited-by'``,
        which is never stored — only ``'cites'`` is) the filter
        is rewritten so both physical encodings of the edge are
        returned. Concretely, ``relation='cited-by',
        direction='out'`` matches:

        * literal ``cited-by`` rows where this ref is src (rare),
          AND
        * ``cites`` rows where this ref is dst (the canonical
          encoding of the same edge from the cited side).

        This solves the "who cites me?" filter the MCP critic
        flagged: agents can write
        ``links_for(B, relation='cited-by', direction='out')`` and
        get the citation graph from B's perspective without
        knowing the schema-side asymmetry. Returned ``Link`` rows
        keep their *stored* relation slug — the caller compares
        against the requested filter to label them, exactly the
        same job the renderer already does for ``direction='both'``.
        """
        # The role-match logic: when the caller asks for relation X
        # in direction D, also accept rows storing inverse(X) in
        # the opposite direction. The two conditions are unioned.
        inverse = _INVERSE_RELATIONS.get(relation) if relation is not None else None

        clauses: list[str] = []
        params: list[Any] = []

        def _direction_clause(direction: str) -> tuple[str, list[Any]]:
            if direction == "out":
                return "src_ref_id = %s", [ref_id]
            if direction == "in":
                return "dst_ref_id = %s", [ref_id]
            return "(src_ref_id = %s OR dst_ref_id = %s)", [ref_id, ref_id]

        if inverse is None:
            # No inverse rewrite needed — single direction clause +
            # optional relation filter.
            d_clause, d_params = _direction_clause(direction)
            clauses.append(d_clause)
            params.extend(d_params)
            if relation is not None:
                clauses.append("relation = %s")
                params.append(relation)
        else:
            # Disjunction: literal-relation in the requested
            # direction OR inverse-relation in the opposite
            # direction. ``opposite`` is straightforward; for
            # ``both``, both halves use ``both`` — every row
            # qualifies under the relation OR inverse-relation
            # branch. We then dedupe by id at the Python boundary.
            opposite_dir = {"out": "in", "in": "out", "both": "both"}[direction]
            d_left, p_left = _direction_clause(direction)
            d_right, p_right = _direction_clause(opposite_dir)
            clauses.append(
                f"(({d_left} AND relation = %s) OR ({d_right} AND relation = %s))"
            )
            params.extend([*p_left, relation, *p_right, inverse])

        sql = (
            "SELECT id, src_ref_id, src_pos, dst_ref_id, dst_pos, "
            "       relation, set_by, meta, created_at "
            f"FROM links WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at ASC"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        # Dedupe by id — when ``direction='both'`` and an inverse
        # rewrite is in play, the same row could match both halves
        # of the OR (a self-link with different positions is the
        # only realistic case in this schema, but the dedupe is
        # cheap and defensive).
        seen: set[int] = set()
        out: list[Link] = []
        for r in rows:
            link_id = r[0]
            if link_id in seen:
                continue
            seen.add(link_id)
            out.append(_row_to_link(r))
        return out

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


def _row_to_link(row: tuple) -> Link:
    """Map a links row tuple in the order:
    (id, src_ref_id, src_pos, dst_ref_id, dst_pos,
     relation, set_by, meta, created_at)

    The DB sentinel `pos = -1` is converted back to `None` at the
    boundary so callers always see "ref-level" as Pythonic `None`.
    """
    return Link(
        id=row[0],
        src_ref_id=row[1],
        src_pos=row[2] if row[2] != _REF_LEVEL_POS else None,
        dst_ref_id=row[3],
        dst_pos=row[4] if row[4] != _REF_LEVEL_POS else None,
        relation=row[5],
        set_by=row[6],
        meta=row[7] or {},
        created_at=row[8],
    )


def _row_to_cache_entry(row: tuple) -> CacheEntry:
    """Map a cache_state row tuple in the order:
    (ref_id, provider, request_hash, model, fetched_at, fresh_until,
     cost_usd, meta)
    """
    return CacheEntry(
        ref_id=row[0],
        provider=row[1],
        request_hash=row[2],
        model=row[3],
        fetched_at=row[4],
        fresh_until=row[5],
        cost_usd=float(row[6]) if row[6] is not None else None,
        meta=row[7] or {},
    )


__all__ = ["Store"]

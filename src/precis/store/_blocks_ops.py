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

import os
import random
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput
from precis.store._mappers import (
    _CHUNKS_COLS,
    _CHUNKS_COLS_LEN,
    _REFS_COLS_ALIASED,
    _REFS_COLS_LEN,
    _block_noise_clauses,
    _row_to_block,
    _row_to_ref,
    _upsert_tag,
)
from precis.store._salience import background_actor_active

#: Whitelist mapping a background actor's name to its per-actor rotation
#: column on ``chunks``. The column name is interpolated into selection /
#: touch SQL (it can't be a bind param), so it MUST come from this dict
#: and never from caller input — an unknown actor is a KeyError, not an
#: injection vector. Add a row here (plus the column via a migration) to
#: introduce a new attention actor.
_ATTENTION_COLUMNS: dict[str, str] = {
    "dream": "last_dreamt",
    "watch": "last_watched",
}

#: Hard ceiling on the total leg count (lexical + semantic) accepted by
#: :meth:`BlocksOpsMixin.search_blocks_multi`. The MCP surface and the
#: paper handler cap ``queries=`` / ``answers=`` at 8 each; this is the
#: last-resort bound for direct (agentic-tier) callers.
_MULTI_LEG_HARD_CAP = 32

#: Default draft over-weight for the dream seed — a draft sorts as if it
#: were this many days more overdue (see :meth:`select_dream_seed`).
#: "kinda" over-weight: large enough that drafts win when even mildly
#: due, small enough that a weeks-overdue paper still out-scores a fresh
#: draft. Tune / disable via ``PRECIS_DREAM_DRAFT_BOOST_DAYS``.
_DREAM_DRAFT_BOOST_DAYS_DEFAULT = 2.0


def _draft_dream_boost_seconds() -> float:
    """Draft dream over-weight in seconds, from ``PRECIS_DREAM_DRAFT_BOOST_DAYS``
    (float days; ``0`` disables). Garbage / negative → the default."""
    raw = os.environ.get("PRECIS_DREAM_DRAFT_BOOST_DAYS")
    if raw is None:
        days = _DREAM_DRAFT_BOOST_DAYS_DEFAULT
    else:
        try:
            days = float(raw)
        except ValueError:
            days = _DREAM_DRAFT_BOOST_DAYS_DEFAULT
        if days < 0:
            days = _DREAM_DRAFT_BOOST_DAYS_DEFAULT
    return days * 86_400.0


from precis.store._tag_filter import (
    build_tag_filter,
    is_speculative_tag,
    is_wiki_tag,
    speculative_fence,
    wiki_fence,
)
from precis.store.types import Block, BlockInsert, Density, Ref
from precis.utils import handle_registry
from precis.utils.angle import angle_anchors

# Default chunk_kind for inserts via this mixin. Phase 2 keeps the
# block surface kind-agnostic; ingesters that want richer typing
# (cards at ord<0, figures, equations) bypass insert_blocks and use
# precis.ingest.db_writer directly.
_DEFAULT_CHUNK_KIND = "paragraph"


def _coerce_year(value: int | str) -> int:
    """Validate a publish-date-filter bound: an int in 1500..2100.

    Raises :class:`BadInput` (not ValueError) so a bad ``after=`` /
    ``before=`` surfaces as a clean agent-facing error. The handler
    validates first; this is the store-side guard that also makes
    literal interpolation in :func:`_year_range_clauses` provably safe.
    """
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise BadInput(
            f"year must be an integer, got {value!r}",
            next="search(kind='paper', q='…', after=2019, before=2023)",
        ) from None
    if not (1500 <= iv <= 2100):
        raise BadInput(
            f"year {iv} out of plausible range (1500..2100)",
            next="search(kind='paper', q='…', after=2019, before=2023)",
        )
    return iv


def _year_range_clauses(year_from: int | None, year_to: int | None) -> list[str]:
    """Parameterless ``r.year`` range predicates for the paper
    publish-date filter (``search(kind='paper', after=…, before=…)``).

    Bounds are interpolated as **integer literals**, not bind params, so
    the clause is safe to splice into *both* CTEs of the fused query —
    the discipline ``speculative_fence`` / ``wiki_fence`` already rely on
    (a parameterised clause would need its params duplicated per CTE).
    Each value is hard-coerced to an int in range first (see
    :func:`_coerce_year`), so literal interpolation carries no injection
    surface. Papers with ``year IS NULL`` fall out (NULL comparisons are
    false); the handler surfaces that omission count separately.
    """
    out: list[str] = []
    if year_from is not None:
        out.append(f"r.year >= {_coerce_year(year_from)}")
    if year_to is not None:
        out.append(f"r.year <= {_coerce_year(year_to)}")
    return out


def _chunk_scope_clauses(
    chunk_kinds: list[str] | None, chunk_ids: list[int] | None
) -> list[str]:
    """Parameterless chunk-level scoping clauses (draft headers-only /
    subtree search). Like :func:`_year_range_clauses` the predicates are
    literal, not bind params, so they splice safely into both CTEs of the
    fused query. ``chunk_kinds`` values are validated against a strict
    ``[a-z_]+`` shape and ``chunk_ids`` are int-coerced, so literal
    interpolation has no injection surface.
    """
    import re

    out: list[str] = []
    if chunk_kinds:
        for k in chunk_kinds:
            if not re.fullmatch(r"[a-z_]+", k):
                raise BadInput(f"bad chunk_kind {k!r}")
        joined = ",".join(f"'{k}'" for k in chunk_kinds)
        out.append(f"c.chunk_kind IN ({joined})")
    if chunk_ids is not None:
        if not chunk_ids:
            out.append("FALSE")  # empty whitelist → match nothing
        else:
            joined_ids = ",".join(str(int(i)) for i in chunk_ids)
            out.append(f"c.chunk_id IN ({joined_ids})")
    return out


def _ord_card_clause(card_kinds: tuple[str, ...] | None) -> str:
    """The body-vs-card scope predicate for a search leg.

    Search defaults to body chunks only (``c.ord >= 0``) — synthetic
    cards (``ord < 0``) are ref-level introducers an agent searching
    for *content* doesn't want in the hit list. When a caller opts in
    via ``card_kinds`` (e.g. paper title search wanting the embedded
    ``card_combined`` to be reachable), the listed card kinds are
    unioned back in: ``(c.ord >= 0 OR c.chunk_kind IN ('card_combined'))``.

    Returns a single literal (no bind params) so it splices safely into
    both CTEs of the fused query — same double-splice contract as
    :func:`_chunk_scope_clauses`. Card kinds are validated against the
    strict ``card_[a-z_]+`` shape so the interpolation has no injection
    surface.
    """
    if not card_kinds:
        return "c.ord >= 0"
    import re

    for k in card_kinds:
        if not re.fullmatch(r"card_[a-z_]+", k):
            raise BadInput(f"bad card_kind {k!r}")
    joined = ",".join(f"'{k}'" for k in card_kinds)
    return f"(c.ord >= 0 OR c.chunk_kind IN ({joined}))"


def _strip_abstract_label(text: str) -> str:
    """Drop a leading ``Abstract`` / ``ABSTRACT`` heading word."""
    import re

    return re.sub(r"^\s*abstract[\s.:—-]*", "", text, flags=re.IGNORECASE).strip()


def _looks_like_abstract(text: str, section_path: str) -> bool:
    """True when a chunk's section or leading text marks it abstract."""
    sp = (section_path or "").lower()
    if "abstract" in sp:
        return True
    head = (text or "").lstrip()[:16].lower()
    return head.startswith("abstract")


def _pick_abstract_text(items: list[tuple[str, str]]) -> str:
    """Choose the best abstract-preview text from leading chunks.

    ``items`` is the ordered ``(text, section_path)`` list for one
    ref. See :meth:`BlocksMixin.abstract_previews` for the preference
    order. Returns ``""`` when nothing usable is present.
    """
    # 1. An explicit abstract chunk wins (label stripped).
    for text, section_path in items:
        if _looks_like_abstract(text, section_path):
            stripped = _strip_abstract_label(text)
            if len(stripped) >= 40:
                return stripped
    # 2. First substantial leading paragraph.
    pick = next((t for t, _ in items if len(t.strip()) >= 200), "")
    # 3. Longest of the first few chunks.
    if not pick and items:
        pick = max((t for t, _ in items), key=lambda t: len(t.strip()))
    return pick.strip()


# Named slice positions for the ``SELECT chunk_cols, ref_cols, score``
# projection used by every search method below. Derived from the
# canonical column-count constants in ``_mappers`` so adding a
# column to either projection list updates every search method
# automatically — no more "I bumped REFS_COLS and forgot to rejig
# the slices" foot-guns (one such bug shipped in 8.7.0).
_BLOCK_END = _CHUNKS_COLS_LEN
_REF_END = _BLOCK_END + _REFS_COLS_LEN
_SCORE_IDX = _REF_END


def _unpack_search_row(row: tuple) -> tuple[Block, Ref, float]:
    """Decompose a ``(chunk_cols, ref_cols, score)`` row into typed parts.

    Companion to the search projections above. Variants that need
    to transform the score (e.g. distance→similarity) should index
    by the named constants directly rather than copy this body.
    """
    return (
        _row_to_block(row[:_BLOCK_END]),
        _row_to_ref(row[_BLOCK_END:_REF_END]),
        float(row[_SCORE_IDX]),
    )


class BlocksMixin:
    """Block insert / get / list + (phase 3) lexical / semantic / fused search."""

    pool: ConnectionPool

    # -- helpers ------------------------------------------------------------

    def _replace_card_combined(
        self, conn: Connection, *, ref_id: int, card_text: str
    ) -> None:
        """Replace a ref's single ``card_combined`` search chunk (delete +
        insert, so the embed/summary cascade re-runs). The shared write for
        the keystone kinds' summary cards (cad / structure / pcb)."""
        conn.execute(
            "DELETE FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
            (ref_id,),
        )
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, meta) "
            "VALUES (%s, 'agent', -1, 'card_combined', %s, %s)",
            (ref_id, card_text, Jsonb({})),
        )

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
    #
    #   row[:_BLOCK_END]          — chunk columns (matches _row_to_block;
    #                               embedding column projected as
    #                               NULL::vector or ce.vector, density
    #                               via correlated subquery on chunk_tags)
    #   row[_BLOCK_END:_REF_END]  — ref columns from _REFS_COLS_ALIASED
    #                               (ref_id-aliased-to-id, slug via
    #                               ref_identifiers, plus the v2-new
    #                               authors/year/retraction_*/pdf_* and
    #                               the Model A decay window)
    #   row[_SCORE_IDX]           — score (ts_rank for lexical, cosine
    #                               distance for semantic, RRF sum for
    #                               fused)
    #
    # Use :func:`_unpack_search_row` for the common shape; for variants
    # (e.g. distance→similarity inversion) index by the named constants
    # directly. Never hard-code the integers — adding a column to
    # ``_REFS_COLS_ALIASED`` should be a one-line change in _mappers.
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
        card_kinds: tuple[str, ...] | None = None,
    ) -> int:
        """Count chunks matching the lexical filter (no LIMIT).

        Companion to :meth:`search_blocks_lexical` for pagination
        headers. Same WHERE clause (including the noise-floor guard and
        the ``card_kinds`` opt-in) so the "you're seeing N of K" header
        reflects the exact universe the search would return at infinite
        limit.
        """
        clauses = [
            "r.deleted_at IS NULL",
            _ord_card_clause(card_kinds),
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
            clauses.append(tag_frag)
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

    def count_paper_yearless_matches(
        self,
        *,
        q: str,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        exclude_ref_ids: list[int] | None = None,
    ) -> int:
        """Count distinct **papers** that match the lexical query but
        carry no ``year`` — the "omitted from a publish-date filter"
        heads-up. Lexical-only (a hard tsquery match) on purpose: it
        answers "papers your keywords hit that a year filter silently
        drops", so the agent doesn't read an empty year-range result as
        "nothing exists" when the real cause is missing metadata.
        """
        clauses = [
            "r.deleted_at IS NULL",
            "r.kind = 'paper'",
            "r.year IS NULL",
            "c.ord >= 0",
            "c.tsv @@ qq.qq",
            *_block_noise_clauses(text_alias="c.text"),
        ]
        params: list[Any] = [q]
        if scope_ref_id is not None:
            params.append(scope_ref_id)
            clauses.append("c.ref_id = %s")
        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag)
            params.extend(tag_params)
        if exclude_ref_ids:
            params.append(list(exclude_ref_ids))
            clauses.append("c.ref_id <> ALL(%s)")
        sql = (
            "SELECT count(DISTINCT r.ref_id) FROM chunks c "
            "JOIN refs r ON r.ref_id = c.ref_id, "
            "websearch_to_tsquery('english', %s) qq(qq) "
            f"WHERE {' AND '.join(clauses)}"
        )
        with self.pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        assert row is not None
        return int(row[0])

    @staticmethod
    def _fence_speculative(tags: list[str] | None, include_speculative: bool) -> bool:
        """Whether to apply the ``DREAM:speculative`` fence to a search.

        Fence by default. Lift it when the caller forces inclusion
        (``include_speculative=True``) or explicitly lists the
        ``DREAM:speculative`` tag in ``tags=`` — listing the control
        tag *is* the opt-in (docs/design/dreaming.md §Inspire: "surface
        on explicit ask").
        """
        if include_speculative:
            return False
        if tags and any(is_speculative_tag(t) for t in tags):
            return False
        return True

    @staticmethod
    def _fence_wiki(tags: list[str] | None, kind: str | None) -> bool:
        """Whether to apply the ``ORIGIN:wikipedia`` fence to a search.

        Fence by default so on-demand Wikipedia fetches stay out of
        default and cross-kind (``kind='*'``) results. Lift it for an
        explicit ``kind='wikipedia'`` scope (you're searching the wiki
        corpus on purpose) or when the caller lists the
        ``ORIGIN:wikipedia`` control tag in ``tags=`` — the opt-in
        mirrors ``DREAM:speculative``.
        """
        if kind == "wikipedia":
            return False
        if tags and any(is_wiki_tag(t) for t in tags):
            return False
        return True

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
        include_speculative: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        chunk_kinds: list[str] | None = None,
        chunk_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        """Lexical search over ``chunks.tsv``.

        Returns ``(block, ref, rank)`` tuples sorted by
        ``ts_rank_cd DESC``. Only live (non-deleted) refs and body
        chunks (``ord >= 0``) are considered — unless ``card_kinds`` opts
        the listed synthetic cards back in (see :func:`_ord_card_clause`).

        Chunks whose text strips to fewer than 4 characters are
        excluded — they're punctuation, section markers, or other
        formatting artefacts that pollute results with hits agents
        can't quote (MCP critic MAJOR #11).
        """
        clauses = [
            "r.deleted_at IS NULL",
            _ord_card_clause(card_kinds),
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
            clauses.append(tag_frag)
            params.extend(tag_params)
        if self._fence_speculative(tags, include_speculative):
            clauses.append(speculative_fence("r"))
        if self._fence_wiki(tags, kind):
            clauses.append(wiki_fence("r"))
        clauses.extend(_year_range_clauses(year_from, year_to))
        clauses.extend(_chunk_scope_clauses(chunk_kinds, chunk_ids))
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
            # chunk_id is a deterministic tiebreak: ts_rank_cd ties are
            # common (short queries over boilerplate-ish chunks) and an
            # unqualified ORDER BY lets ties shuffle between executions —
            # under multi-leg RRF fusion that flipped fused scores and
            # per_paper winners between page 1 and page 2.
            "ORDER BY rank DESC, c.chunk_id ASC LIMIT %s OFFSET %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_unpack_search_row(r) for r in rows]

    def search_blocks_semantic(
        self,
        *,
        query_vec: list[float],
        kind: str | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        max_distance: float | None = None,
        exclude_ref_ids: list[int] | None = None,
        include_speculative: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        chunk_kinds: list[str] | None = None,
        chunk_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        """Cosine-distance semantic search via ``chunk_embeddings``.

        Returns ``(block, ref, distance)`` tuples sorted by distance
        ASC. Excludes chunks that have no embedding under the
        default embedder, chunks with ``ord < 0`` (synthetic cards),
        and chunks whose text strips to <4 characters. ``card_kinds``
        opts the listed cards back in (see :func:`_ord_card_clause`) so
        a paper's embedded ``card_combined`` is reachable by semantic
        title/meta search.

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
            _ord_card_clause(card_kinds),
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
            clauses.append(tag_frag)
            where_params.extend(tag_params)
        if self._fence_speculative(tags, include_speculative):
            clauses.append(speculative_fence("r"))
        if self._fence_wiki(tags, kind):
            clauses.append(wiki_fence("r"))
        clauses.extend(_year_range_clauses(year_from, year_to))
        clauses.extend(_chunk_scope_clauses(chunk_kinds, chunk_ids))
        if exclude_ref_ids:
            where_params.append(list(exclude_ref_ids))
            clauses.append("c.ref_id <> ALL(%s)")

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
            offset,
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
            "ORDER BY ce.vector <=> %s::vector ASC LIMIT %s OFFSET %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_unpack_search_row(r) for r in rows]

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
        include_speculative: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        chunk_kinds: list[str] | None = None,
        chunk_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        """Hybrid search via reciprocal rank fusion over lex + sem.

        If ``query_vec`` is None, falls back to lexical-only and
        returns tuples in the same shape (so callers don't branch).

        Score: ``1/(k + lex_rank) + 1/(k + sem_rank)``. Higher is
        better. ``k=60`` is the standard RRF constant.

        ``offset`` (default 0) skips the first N fused rows for
        pagination. The inner CTEs widen by ``offset`` to keep enough
        candidates for the outer LIMIT/OFFSET slice to be populated.

        ``card_kinds`` opts the listed synthetic cards back into both
        legs (see :func:`_ord_card_clause`).
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
                include_speculative=include_speculative,
                year_from=year_from,
                year_to=year_to,
                chunk_kinds=chunk_kinds,
                chunk_ids=chunk_ids,
                card_kinds=card_kinds,
            )

        with self.pool.connection() as conn:
            embedder = self._default_embedder_name(conn)

        clauses = [
            "r.deleted_at IS NULL",
            _ord_card_clause(card_kinds),
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
            clauses.append(tag_frag)
            params.extend(tag_params)
        if self._fence_speculative(tags, include_speculative):
            # Parameterless clause — safe under the double-splice of
            # ``where_extra`` into both the lex and sem CTEs below.
            clauses.append(speculative_fence("r"))
        if self._fence_wiki(tags, kind):
            # Likewise parameterless — safe under the double-splice.
            clauses.append(wiki_fence("r"))
        # Year + chunk-scope predicates are int/validated-literal
        # (parameterless) for the same double-splice safety — see
        # ``_year_range_clauses`` / ``_chunk_scope_clauses``.
        clauses.extend(_year_range_clauses(year_from, year_to))
        clauses.extend(_chunk_scope_clauses(chunk_kinds, chunk_ids))
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
                           -- chunk_id tiebreak: same determinism fix as
                           -- search_blocks_lexical (rank ties shuffle).
                           ORDER BY ts_rank_cd(c.tsv, qq.qq) DESC, c.chunk_id
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
        return [_unpack_search_row(r) for r in rows]

    def search_blocks_multi(
        self,
        *,
        q_texts: list[str],
        query_vecs: list[list[float]],
        mode: str | None = None,
        kind: str | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        k: int = 60,
        max_distance: float | None = None,
        exclude_ref_ids: list[int] | None = None,
        include_speculative: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        chunk_kinds: list[str] | None = None,
        chunk_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
        per_paper: int | None = None,
        pool_per_leg: int = 80,
    ) -> list[tuple[Block, Ref, float]]:
        """Multi-leg reciprocal-rank fusion for broad / high-recall retrieval.

        Generalises :meth:`search_blocks_fused` (one lexical + one semantic
        leg) to *N* legs: each entry of ``q_texts`` runs a lexical leg and
        each entry of ``query_vecs`` runs a semantic leg, then the per-leg
        ranked lists are reciprocal-rank-fused into one ordering. A chunk
        that surfaces across several query phrasings / HyDE answer
        embeddings accumulates contributions and wins — exactly the
        robustness-to-formulation the single-query path lacks.

        Fusion is application-level: every leg reuses the tested single-leg
        SQL (:meth:`search_blocks_lexical` / :meth:`search_blocks_semantic`)
        rather than a hand-rolled N-CTE query, so per-leg ranking semantics
        stay identical to the proven path. Each leg over-fetches a pool of
        candidates so the fusion has material to re-rank before the outer
        ``offset`` / ``limit`` slice.

        ``mode`` mirrors the single-path dispatcher: ``'lexical'`` runs only
        the ``q_texts`` legs, ``'semantic'`` only the ``query_vecs`` legs,
        ``'hybrid'`` / ``None`` runs both. With no usable semantic vectors
        (embedder down, or ``mode='lexical'``) it degrades to the lexical
        legs alone — the same contract as the single path.

        ``per_paper`` optionally caps how many hits one ref may contribute
        to the fused result, spreading coverage across more papers (the
        breadth-triage / diversity knob). ``None`` disables the cap.

        Returns ``(Block, Ref, fused_score)`` tuples, best first.
        """
        # Defensive hard ceiling on the fan-out. The MCP surface and the
        # paper handler both cap queries=/answers= at 8 each, but this
        # method is also a direct target for the agentic tier — an
        # unbounded caller would fire one SQL leg per entry. 32 legs is
        # comfortably above any legitimate broad call (1 + 8 lexical
        # + 1 + 8 + 8 semantic = 26 worst case).
        n_legs = len(q_texts) + len(query_vecs)
        if n_legs > _MULTI_LEG_HARD_CAP:
            raise ValueError(
                f"search_blocks_multi: {n_legs} legs exceeds the hard cap "
                f"{_MULTI_LEG_HARD_CAP} (callers must bound queries=/answers=)"
            )
        m = (mode or "hybrid").strip().lower()
        run_lexical = m != "semantic"
        run_semantic = m != "lexical" and bool(query_vecs)
        # A semantic-only request with no usable vectors still answers via
        # the lexical legs (mirrors the single-path embedder-down fallback).
        if not run_semantic and not run_lexical:
            run_lexical = True

        # Over-fetch enough per leg that the fused slice at this depth is
        # populated; deep pagination over a broad search widens the pool.
        pool = max(pool_per_leg, (offset + limit) * 2)
        clean_q = [qt for qt in q_texts if qt and qt.strip()]

        legs: list[list[tuple[Block, Ref, float]]] = []
        if run_lexical:
            for qt in clean_q:
                legs.append(
                    self.search_blocks_lexical(
                        q=qt,
                        kind=kind,
                        scope_ref_id=scope_ref_id,
                        tags=tags,
                        limit=pool,
                        exclude_ref_ids=exclude_ref_ids,
                        include_speculative=include_speculative,
                        year_from=year_from,
                        year_to=year_to,
                        chunk_kinds=chunk_kinds,
                        chunk_ids=chunk_ids,
                        card_kinds=card_kinds,
                    )
                )
        if run_semantic:
            for qv in query_vecs:
                legs.append(
                    self.search_blocks_semantic(
                        query_vec=qv,
                        kind=kind,
                        scope_ref_id=scope_ref_id,
                        tags=tags,
                        limit=pool,
                        max_distance=max_distance,
                        exclude_ref_ids=exclude_ref_ids,
                        include_speculative=include_speculative,
                        year_from=year_from,
                        year_to=year_to,
                        chunk_kinds=chunk_kinds,
                        chunk_ids=chunk_ids,
                        card_kinds=card_kinds,
                    )
                )

        # Reciprocal-rank fusion: score[cid] = Σ_leg 1/(k + rank_in_leg).
        # Keep the first-seen (Block, Ref) per chunk for rendering.
        fused: dict[int, float] = {}
        seen: dict[int, tuple[Block, Ref]] = {}
        for leg in legs:
            for rank, (block, ref, _score) in enumerate(leg):
                cid = block.id
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank + 1)
                if cid not in seen:
                    seen[cid] = (block, ref)

        # Sort by fused score desc; deterministic tiebreak on chunk id.
        ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))

        results: list[tuple[Block, Ref, float]] = []
        per_paper_count: dict[int, int] = {}
        for cid, score in ordered:
            block, ref = seen[cid]
            if per_paper is not None:
                taken = per_paper_count.get(ref.id, 0)
                if taken >= per_paper:
                    continue
                per_paper_count[ref.id] = taken + 1
            results.append((block, ref, score))

        return results[offset : offset + limit]

    def search_blocks(
        self,
        *,
        q: str,
        query_vec: list[float] | None = None,
        mode: str | None = None,
        kind: str | None = None,
        scope_ref_id: int | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        k: int = 60,
        max_distance: float | None = None,
        exclude_ref_ids: list[int] | None = None,
        include_speculative: bool = False,
        year_from: int | None = None,
        year_to: int | None = None,
        chunk_kinds: list[str] | None = None,
        chunk_ids: list[int] | None = None,
        card_kinds: tuple[str, ...] | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        """Mode-dispatched block search — one entry point over the three
        ranking strategies, so callers pick a mode instead of choosing a
        function (ADR 0033-adjacent; the LLM-facing ``search(mode=…)``).

        ``year_from`` / ``year_to`` are inclusive ``refs.year`` bounds for
        the paper publish-date filter; they thread into all three legs.

        * ``mode='lexical'`` — Postgres FTS only (``search_blocks_lexical``);
          deterministic keyword / exact-phrase / identifier matching, and
          the honest tool when the embedder is down.
        * ``mode='semantic'`` — embedding cosine only
          (``search_blocks_semantic``); degrades to lexical if no
          ``query_vec`` (embedder unavailable).
        * ``mode='hybrid'`` / ``None`` (default) — reciprocal-rank fusion
          (``search_blocks_fused``), which itself falls back to lexical
          when ``query_vec`` is None. Identical to the prior default.

        Result tuples are ``(Block, Ref, score)`` in every mode (score is
        an RRF score, a cosine distance, or a lexical rank — all "more
        relevant first" within a mode, never comparable across modes).
        """
        m = (mode or "hybrid").strip().lower()
        if m == "semantic" and query_vec is not None:
            return self.search_blocks_semantic(
                query_vec=query_vec,
                kind=kind,
                scope_ref_id=scope_ref_id,
                tags=tags,
                limit=limit,
                offset=offset,
                max_distance=max_distance,
                exclude_ref_ids=exclude_ref_ids,
                include_speculative=include_speculative,
                year_from=year_from,
                year_to=year_to,
                chunk_kinds=chunk_kinds,
                chunk_ids=chunk_ids,
                card_kinds=card_kinds,
            )
        if m == "lexical" or query_vec is None:
            return self.search_blocks_lexical(
                q=q,
                kind=kind,
                scope_ref_id=scope_ref_id,
                tags=tags,
                limit=limit,
                offset=offset,
                exclude_ref_ids=exclude_ref_ids,
                include_speculative=include_speculative,
                year_from=year_from,
                year_to=year_to,
                chunk_kinds=chunk_kinds,
                chunk_ids=chunk_ids,
                card_kinds=card_kinds,
            )
        return self.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind=kind,
            scope_ref_id=scope_ref_id,
            tags=tags,
            limit=limit,
            offset=offset,
            k=k,
            max_distance=max_distance,
            exclude_ref_ids=exclude_ref_ids,
            include_speculative=include_speculative,
            year_from=year_from,
            year_to=year_to,
            chunk_kinds=chunk_kinds,
            chunk_ids=chunk_ids,
            card_kinds=card_kinds,
        )

    # -- relative navigation (ADR 0036) ------------------------------------

    #: Kinds whose chunks form a tree (``parent_chunk_id`` + ``pos``) rather
    #: than a flat ``ord`` sequence. Only ``draft`` today; its relative
    #: navigation walks the hierarchy and is handled by the draft mixin.
    _TREE_CHUNK_KINDS: frozenset[str] = frozenset({"draft"})

    def resolve_relative(self, handle: str) -> tuple[str, str] | None:
        """Resolve a relative chunk handle to ``(kind, per-kind selector)``.

        Examples (flat ``ord``-based kinds — paper, plaintext, …):
          ``pc<id>+1``    → the next chunk     → ``(kind, 'slug~<ord+1>')``
          ``pc<id>-2``    → two chunks back    → ``(kind, 'slug~<ord-2>')``
          ``pc<id>-2..3`` → a signed span      → ``(kind, 'slug~<lo>..<hi>')``
          ``pc<id>^``     → no hierarchy (flat) → ``None``

        Returns ``None`` when ``handle`` carries no valid operator (use the
        absolute path), the base chunk is missing, the operator is
        unsupported for the kind, or the target falls outside the document.
        Tree-structured kinds (``draft``) are resolved by the draft mixin —
        this method returns ``None`` for them so the caller can route there.
        """
        parsed = handle_registry.parse_relative(handle)
        if parsed is None:
            return None
        kind, _is_chunk, chunk_id, op = parsed
        if kind in self._TREE_CHUNK_KINDS:
            return None  # draft tree walk lives in the draft mixin
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT c.ref_id, c.ord, r.kind, "
                "(SELECT id_value FROM ref_identifiers ri "
                " WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key' "
                " LIMIT 1) AS slug "
                "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
                "WHERE c.chunk_id = %s AND r.deleted_at IS NULL AND c.ord >= 0",
                (chunk_id,),
            ).fetchone()
            if row is None:
                return None
            ref_id, ord_, row_kind, slug = int(row[0]), int(row[1]), str(row[2]), row[3]
            if row_kind != kind or slug is None:
                return None  # kind/prefix mismatch, or no addressable slug
            kind_tag, *rest = op
            if kind_tag == "ancestor":
                return None  # flat kinds have no enclosing structure to climb
            max_row = conn.execute(
                "SELECT max(ord) FROM chunks WHERE ref_id = %s AND ord >= 0",
                (ref_id,),
            ).fetchone()
            max_ord = int(max_row[0]) if max_row and max_row[0] is not None else ord_

            if kind_tag == "step":
                (n,) = rest
                target = ord_ + n
                if target < 0 or target > max_ord:
                    return None
                # The neighbour must actually exist (ord can be sparse after
                # card-chunk gaps, though body chunks are dense in practice).
                exists = conn.execute(
                    "SELECT 1 FROM chunks WHERE ref_id = %s AND ord = %s",
                    (ref_id, target),
                ).fetchone()
                if exists is None:
                    return None
                return kind, f"{slug}~{target}"

            # span: signed offsets around the anchor, clamped to the document.
            lo_off, hi_off = rest
            lo = max(0, ord_ + lo_off)
            hi = min(max_ord, ord_ + hi_off)
            if lo > hi:
                return None
            return kind, f"{slug}~{lo}..{hi}"

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

    def upsert_card_combined(
        self,
        ref_id: int,
        text: str,
        *,
        conn: Connection | None = None,
    ) -> int:
        """(Re-)emit a ref's ``card_combined`` chunk (``ord = -1``).

        DELETE+INSERT — never in-place UPDATE — so the embedding /
        summary cascade re-runs cleanly: deleting the old ``ord=-1``
        row cascades away its ``chunk_embeddings`` / ``chunk_summaries``
        rows, and the fresh INSERT re-enters the embed worker's queue.
        Idempotent: safe on create (no existing card) and on edit
        (replaces it).

        This makes note-like kinds embeddable (today: ``memory``) so
        ``search_blocks_semantic`` finds true cosine neighbours rather
        than only lexical ``refs.title`` matches. Returns the new
        ``chunk_id``.
        """

        def _do(c: Connection) -> int:
            c.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND ord = -1",
                (ref_id,),
            )
            row = c.execute(
                "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
                "VALUES (%s, -1, 'card_combined', %s, '{}'::jsonb) "
                "RETURNING chunk_id",
                (ref_id, text),
            ).fetchone()
            assert row is not None
            return int(row[0])

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    # ── salience (dreaming target selection) ───────────────────────

    def bump_salience(self, chunk_ids: list[int]) -> int:
        """Record an external access for a result page (set-based).

        One in-DB ``bump_salience(ids)`` call advances ``last_seen=now()``
        and ``accesses += 1`` for the whole page in a single round-trip
        (docs/design/dreaming.md, §Access accounting). Metadata-only —
        never touches ``chunks.text`` — so it's the one write permitted
        on the search path (thresholds.md relaxed for metadata bumps).

        No-op when:

        - ``chunk_ids`` is empty (nothing matched), or
        - the call is inside :func:`as_dream_actor` — the dreamer's own
          reads must not heat the region it is wandering into an echo
          chamber.

        Returns the number of chunk ids bumped (0 when suppressed).
        """
        if not chunk_ids or background_actor_active():
            return 0
        with self.pool.connection() as conn:
            conn.execute("SELECT bump_salience(%s)", (list(chunk_ids),))
        return len(chunk_ids)

    def touch_attended(
        self,
        actor: str,
        chunk_ids: list[int],
        *,
        conn: Connection | None = None,
    ) -> int:
        """Stamp ``last_<actor> = now()`` on chunks an attention loop touched.

        Run-end rotation step shared by every attention actor (dream,
        watch, …): everything the loop surfaced is stamped so its
        ``last_seen - last_<actor>`` score drops and a *different* region
        tops the next run (docs/design/dreaming.md, §Selection;
        docs/design/watching.md). The act of looking *is* the anti-repeat
        mechanism. ``actor`` selects the rotation column via
        :data:`_ATTENTION_COLUMNS` (unknown actor → KeyError).
        Metadata-only, same as :meth:`bump_salience`. Returns the count
        stamped.
        """
        if not chunk_ids:
            return 0
        col = _ATTENTION_COLUMNS[actor]
        # ``col`` is a whitelisted identifier, never caller input.
        sql = f"UPDATE chunks SET {col} = now() WHERE chunk_id = ANY(%s)"
        ids = list(chunk_ids)
        if conn is not None:
            conn.execute(sql, (ids,))
        else:
            with self.pool.connection() as c:
                c.execute(sql, (ids,))
        return len(ids)

    def touch_last_dreamt(
        self,
        chunk_ids: list[int],
        *,
        conn: Connection | None = None,
    ) -> int:
        """Dreamer rotation stamp — :meth:`touch_attended` with ``"dream"``."""
        return self.touch_attended("dream", chunk_ids, conn=conn)

    def card_chunk_ids(self, ref_ids: list[int]) -> list[int]:
        """Resolve the ``card_combined`` (``ord=-1``) chunk id per ref.

        Ref-level search (memory) returns refs, not chunks, but salience
        lives on chunks — a memory's only salience-bearing chunk is its
        card. This maps a page of hit ref ids to the chunk ids
        :meth:`bump_salience` should heat. Refs without a card (kinds
        that don't emit one) simply contribute nothing, so calling this
        for a mixed/numeric-kind page is safe.
        """
        if not ref_ids:
            return []
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ord = -1 AND ref_id = ANY(%s)",
                (list(ref_ids),),
            ).fetchall()
        return [int(r[0]) for r in rows]

    def select_salient(
        self,
        actor: str,
        *,
        kinds: tuple[str, ...] = ("paper", "memory"),
        limit: int = 1,
        boost_kind: str | None = None,
        boost_seconds: float = 0.0,
    ) -> list[int]:
        """Most-due salient chunks for ``actor``: ``argmax(last_seen - last_<actor>)``.

        The shared attention-selection primitive (docs/design/dreaming.md,
        §Target selection; docs/design/watching.md) — knob-free, no decay,
        no sampling. ``actor`` selects the per-actor rotation column via
        :data:`_ATTENTION_COLUMNS` (unknown actor → KeyError). Restricted
        to live refs of the target ``kinds``. Ties break on ``chunk_id`` so
        selection is deterministic and in-process testable. Returns up to
        ``limit`` chunk ids, most-due first (empty when the corpus has no
        target chunks).

        ``boost_kind`` + ``boost_seconds`` add a per-kind due-ness bias:
        a chunk of ``boost_kind`` sorts as if it were ``boost_seconds``
        more overdue than it literally is. Used to **over-weight drafts**
        in the dream rotation (a draft is what the operator is actively
        looking at, so a dream on it lands where it'll be seen) without
        starving the rest of the corpus — a long-overdue paper can still
        out-score a freshly-dreamt draft. Default (no boost) preserves the
        pure ``argmax`` for every other actor.
        """
        col = _ATTENTION_COLUMNS[actor]
        with self.pool.connection() as conn:
            rows = conn.execute(
                # ``col`` is a whitelisted identifier, never caller input.
                # The boost adds an interval to the due-ness score for
                # rows of ``boost_kind`` (empty string matches nothing →
                # no boost), keeping the ORDER BY param-stable.
                f"""
                SELECT c.chunk_id
                FROM chunks c
                JOIN refs r ON r.ref_id = c.ref_id
                WHERE r.deleted_at IS NULL
                  AND c.retired_at IS NULL
                  AND r.kind = ANY(%s)
                ORDER BY (
                    (c.last_seen - c.{col})
                    + CASE WHEN r.kind = %s
                           THEN make_interval(secs => %s)
                           ELSE interval '0' END
                ) DESC, c.chunk_id
                LIMIT %s
                """,
                (list(kinds), boost_kind or "", float(boost_seconds), limit),
            ).fetchall()
        return [int(r[0]) for r in rows]

    def select_dream_seed(
        self,
        *,
        kinds: tuple[str, ...] = ("paper", "memory"),
    ) -> int | None:
        """Dream seed — single most-due chunk via :meth:`select_salient`.

        Over-weights ``draft`` chunks (when drafts are in scope) by
        ``_draft_dream_boost_seconds()`` so the dreamer favours the
        documents the operator is actively writing — a "kinda" tilt, not
        a takeover (the boost is a few days of due-ness, so a much-more-
        overdue paper still wins). Tune via ``PRECIS_DREAM_DRAFT_BOOST_DAYS``
        (0 disables). Preserves the original signature for existing tests.
        """
        boost = _draft_dream_boost_seconds() if "draft" in kinds else 0.0
        ids = self.select_salient(
            "dream", kinds=kinds, limit=1, boost_kind="draft", boost_seconds=boost
        )
        return ids[0] if ids else None

    def dreamable_region(
        self,
        *,
        kinds: tuple[str, ...] = ("paper", "memory"),
        n: int = 12,
    ) -> tuple[int | None, list[tuple[Block, Ref, float]]]:
        """The focus region: the salience seed + its ANN neighbourhood.

        Backs ``search(view='dreamable')`` (docs/design/dreaming.md,
        §view='dreamable'). Picks the most-due seed via
        :meth:`select_dream_seed`, then returns the ``n`` nearest
        embedded chunks to it (the seed included) over the target
        ``kinds`` — a single cosine neighbourhood, **not** a
        sub-clustered carve-up. Per the scoped cut there is no
        HDBSCAN/GMM here; the plain ring *is* the region. Card chunks
        (``ord=-1``) are included so a memory's only embedded chunk is
        reachable.

        Returns ``(seed_chunk_id, [(block, ref, cosine)])`` ordered
        nearest-first; ``(None, [])`` when no target chunk exists and
        ``(seed_id, [])`` when the seed itself has no embedding yet.

        Pure retrieval — stamping ``last_dreamt`` on the surfaced
        chunks (the rotation) is the caller's job (the dream dispatch
        path), so a plain store-level read stays side-effect-free and
        testable.
        """
        seed_id = self.select_dream_seed(kinds=kinds)
        if seed_id is None:
            return None, []
        seed_vec = self.get_chunk_vector(seed_id)
        if seed_vec is None:
            return seed_id, []
        proj = _CHUNK_PROJ.format(embedding="NULL::vector")
        sql = (
            f"SELECT {proj}, {_REFS_COLS_ALIASED}, "
            "       (ce.vector <=> %s::vector) AS dist "
            "FROM chunks c "
            "JOIN refs r ON r.ref_id = c.ref_id "
            "JOIN chunk_embeddings ce "
            "  ON ce.chunk_id = c.chunk_id AND ce.embedder = %s "
            "WHERE r.deleted_at IS NULL "
            "  AND c.retired_at IS NULL "
            "  AND ce.vector IS NOT NULL AND ce.status = 'ok' "
            "  AND r.kind = ANY(%s) "
            "ORDER BY ce.vector <=> %s::vector ASC LIMIT %s"
        )
        with self.pool.connection() as conn:
            embedder = self._default_embedder_name(conn)
            rows = conn.execute(
                sql, (seed_vec, embedder, list(kinds), seed_vec, n)
            ).fetchall()
        region = [
            (
                _row_to_block(r[:_BLOCK_END]),
                _row_to_ref(r[_BLOCK_END:_REF_END]),
                1.0 - float(r[_SCORE_IDX]),
            )
            for r in rows
        ]
        return seed_id, region

    # ── angle spray (diverse-cone semantic neighbours) ─────────────

    def get_chunk_vector(self, chunk_id: int) -> list[float] | None:
        """Read a chunk's stored embedding under the default embedder.

        Returns ``None`` when the chunk has no ``ok`` embedding row yet
        (worker hasn't run). Used to seed an :meth:`angle_neighbours`
        spray from an existing item (``like=<chunk id>``).
        """
        with self.pool.connection() as conn:
            embedder = self._default_embedder_name(conn)
            row = conn.execute(
                "SELECT vector FROM chunk_embeddings "
                "WHERE chunk_id = %s AND embedder = %s AND status = 'ok'",
                (chunk_id, embedder),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return [float(x) for x in row[0]]

    def seed_chunk_for_ref(self, ref_id: int) -> int | None:
        """Pick a ref's representative chunk id for ``like=<ref id>``.

        Prefers the ``card_combined`` chunk (``ord=-1``) — the whole-ref
        summary that note-like kinds (memory) embed — and otherwise
        falls back to the lowest-``ord`` embedded body chunk so papers
        seed from their head. Returns the ``chunk_id`` (pair it with
        :meth:`get_chunk_vector` for the seed vector and exclude it from
        the spray), or ``None`` when nothing under this ref is embedded
        yet.
        """
        with self.pool.connection() as conn:
            embedder = self._default_embedder_name(conn)
            row = conn.execute(
                "SELECT c.chunk_id "
                "FROM chunks c "
                "JOIN chunk_embeddings ce "
                "  ON ce.chunk_id = c.chunk_id AND ce.embedder = %s "
                "WHERE c.ref_id = %s AND ce.vector IS NOT NULL "
                "  AND ce.status = 'ok' "
                "ORDER BY (c.ord = -1) DESC, c.ord ASC "
                "LIMIT 1",
                (embedder, ref_id),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def angle_neighbours(
        self,
        seed_vec: list[float],
        *,
        angle: float = 1.0,
        n: int = 8,
        kinds: tuple[str, ...] = ("paper", "memory"),
        exclude_chunk_ids: list[int] | None = None,
        rng: random.Random | None = None,
    ) -> list[tuple[Block, Ref, float]]:
        """``n`` diverse items at cosine ``angle`` from ``seed_vec``.

        Draws ``n`` anchors at the requested cosine (see
        :func:`precis.utils.angle.angle_anchors`), ANN-snaps each to its
        nearest not-yet-seen real chunk over the target ``kinds``, and
        dedups. The result is **not a cluster** — it's ``n`` points
        spread around the seed's cone, each snapped to a real item
        (docs/design/dreaming.md, §The ``angle`` spray).

        Card chunks (``ord=-1``) are **included** as snap targets so a
        memory's only embedded chunk is reachable — unlike the body-only
        :meth:`search_blocks_semantic`. Returns ``(block, ref, cosine)``
        where ``cosine = 1 - cosine_distance`` is the *realised*
        similarity (anisotropy means it rarely equals ``angle`` exactly;
        that's expected, not a bug). Empty when the seed is empty/zero.
        """
        if not seed_vec:
            return []
        anchors = angle_anchors(seed_vec, angle, n, rng=rng)
        seen: set[int] = {int(x) for x in (exclude_chunk_ids or [])}
        out: list[tuple[Block, Ref, float]] = []
        with self.pool.connection() as conn:
            embedder = self._default_embedder_name(conn)
            for w in anchors:
                res = self._nearest_chunk(conn, w, embedder, list(kinds), seen)
                if res is None:
                    continue
                block, ref, dist = res
                seen.add(block.id)
                out.append((block, ref, 1.0 - dist))
        return out

    def _nearest_chunk(
        self,
        conn: Connection,
        vec: list[float],
        embedder: str,
        kinds: list[str],
        exclude: set[int],
    ) -> tuple[Block, Ref, float] | None:
        """Single nearest embedded chunk to ``vec`` over ``kinds``.

        Card-inclusive (no ``ord >= 0`` filter) so memory cards snap.
        Skips ``exclude`` so an :meth:`angle_neighbours` spray returns
        distinct items across its anchors. Returns ``(block, ref,
        cosine_distance)`` or ``None`` when nothing matches.
        """
        proj = _CHUNK_PROJ.format(embedding="NULL::vector")
        clauses = [
            "r.deleted_at IS NULL",
            "c.retired_at IS NULL",
            "ce.vector IS NOT NULL",
            "ce.status = 'ok'",
            "r.kind = ANY(%s)",
        ]
        params: list[Any] = [vec, embedder, kinds]
        if exclude:
            clauses.append("c.chunk_id <> ALL(%s)")
            params.append(list(exclude))
        params.append(vec)
        sql = (
            f"SELECT {proj}, {_REFS_COLS_ALIASED}, "
            "       (ce.vector <=> %s::vector) AS dist "
            "FROM chunks c "
            "JOIN refs r ON r.ref_id = c.ref_id "
            "JOIN chunk_embeddings ce "
            "  ON ce.chunk_id = c.chunk_id AND ce.embedder = %s "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY ce.vector <=> %s::vector ASC LIMIT 1"
        )
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return _unpack_search_row(row)

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

    def chunk_pages(self, ref_id: int, ords: list[int]) -> dict[int, int]:
        """Map body-chunk ``ord`` → ``page_first`` for the given ords.

        Feeds the paper sidebar nav: a semantic / keyword / TOC hit
        carries a chunk ``ord``, and the in-browser PDF viewer jumps to
        the chunk's first page. Chunks with a NULL ``page_first`` (no
        page provenance) are omitted from the map. One batched query.
        """
        if not ords:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ord, page_first FROM chunks "
                "WHERE ref_id = %s AND ord = ANY(%s) AND page_first IS NOT NULL",
                (ref_id, list(ords)),
            ).fetchall()
        return {int(r[0]): int(r[1]) for r in rows}

    def ref_ids_with_chunks(self, ref_ids: list[int]) -> set[int]:
        """Subset of ``ref_ids`` that have at least one body chunk (ord>=0).

        One batched query for the Papers list's "has chunks" badge /
        filter on the lexical-search path (where the SQL-side
        ``list_refs(has_chunks=...)`` filter isn't available). Missing
        ids simply don't appear in the returned set.
        """
        if not ref_ids:
            return set()
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ref_id FROM chunks "
                "WHERE ref_id = ANY(%s) AND ord >= 0",
                (list(ref_ids),),
            ).fetchall()
        return {int(r[0]) for r in rows}

    def count_blocks(self, ref_id: int) -> int:
        """Total body chunks on a ref (ord>=0). Tiny indexed count."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord >= 0",
                (ref_id,),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def count_chunks_for_kind(self, kind: str) -> int:
        """Total body chunks (ord>=0) across all live refs of ``kind``.

        One COUNT join so list views (e.g. the paper stats header) can
        report total corpus depth alongside the ref count without an
        agent having to estimate from per-paper counts.
        """
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM chunks c "
                "JOIN refs r ON r.ref_id = c.ref_id "
                "WHERE r.kind = %s AND r.deleted_at IS NULL AND c.ord >= 0",
                (kind,),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def abstract_previews(
        self, ref_ids: list[int], *, max_chars: int = 900
    ) -> dict[int, str]:
        """Best-effort abstract text per ref, drawn from leading chunks.

        Most Marker-ingested papers carry no publisher abstract in
        ``refs.meta['abstract']`` — the abstract prose lives in the
        first body chunks instead. This returns, per ref, the first
        substantial leading paragraph (``len >= 200``), falling back to
        the longest of the first few body chunks. Used by the web
        papers list to populate the hover card when the meta abstract
        is absent.

        One batched query over the leading body chunks
        (``0 <= ord < 8``, excluding bibliography blocks); selection
        happens in Python so the heuristic stays legible. Preference
        order per ref:

        1. A chunk whose ``section_path`` or leading text marks it as
           the abstract (``"abstract"``) — the publisher's actual
           abstract, with any leading "Abstract" label stripped.
        2. The first substantial leading paragraph (``len >= 200``).
        3. The longest of the first few chunks.
        """
        if not ref_ids:
            return {}
        sql = (
            "SELECT ref_id, ord, text, section_path FROM chunks "
            "WHERE ref_id = ANY(%s) AND ord >= 0 AND ord < 8 "
            "AND chunk_kind <> 'references' "
            "ORDER BY ref_id, ord"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (list(ref_ids),)).fetchall()
        by_ref: dict[int, list[tuple[str, str]]] = {}
        for rid, _ord, text, section_path in rows:
            # section_path is a TEXT[] (list) at the psycopg edge;
            # flatten to a string for the abstract-marker check.
            sp = (
                " ".join(section_path)
                if isinstance(section_path, list)
                else (section_path or "")
            )
            by_ref.setdefault(int(rid), []).append((text or "", sp))
        out: dict[int, str] = {}
        for rid, items in by_ref.items():
            pick = _pick_abstract_text(items)
            if not pick:
                continue
            if len(pick) > max_chars:
                pick = pick[:max_chars].rstrip() + "…"
            out[rid] = pick
        return out

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
                "       c.section_path, c.chunk_kind, c.keywords, "
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
        return _row_to_block(row[:_BLOCK_END]), _row_to_ref(row[_BLOCK_END:])

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
            # Conv blocks jump the queue. Chat history is "hot" — Asa
            # reads recent turns every preamble build, so the digest
            # tier needs keywords + the search surface needs embeddings
            # ASAP after each turn lands. The CASE expression sorts
            # conv first, everything else FIFO behind.
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
                "ORDER BY (CASE WHEN r.kind = 'conv' THEN 0 ELSE 1 END), "
                "         c.chunk_id ASC "
                "LIMIT %s"
            )
            rows = conn.execute(sql, [embedder, *params]).fetchall()
        return [_row_to_block(r) for r in rows]


# -- module-level helpers ---------------------------------------------------


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
    "c.meta, c.created_at, c.created_at AS updated_at, "
    "c.section_path, c.chunk_kind, c.keywords"
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
        "       c.meta, c.created_at, c.created_at AS updated_at, "
        "       c.section_path "
        "FROM chunks c "
        "LEFT JOIN chunk_embeddings ce "
        "  ON ce.chunk_id = c.chunk_id AND ce.embedder = %s "
        "WHERE c.chunk_id = %s"
    )
    row = conn.execute(sql, (embedder_name, chunk_id)).fetchone()
    assert row is not None
    return _row_to_block(row)


__all__ = ["BlocksMixin"]

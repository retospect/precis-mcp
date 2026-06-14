"""Shared row mappers, position sentinels, and noise-filter helpers.

Every domain mixin (``refs``, ``blocks``, ``tags``, ``links``,
``cache``, ``ingest``) needs to either translate between the DB
``pos = -1`` sentinel and Python ``None`` (refs are "ref-level" at
-1) or convert a psycopg row tuple into one of the typed
:mod:`precis.store.types` dataclasses. Keeping these in one module
avoids cross-mixin imports and keeps each mixin focused on the
SQL it owns.

Nothing here is agent-facing — everything is re-exported privately
by :mod:`precis.store.store` and never leaks through
``from precis.store import ...``.
"""

from __future__ import annotations

from precis.store.types import (
    Block,
    CacheEntry,
    Link,
    Ref,
)

# ---------------------------------------------------------------------------
# Tag prefix ownership — mirrored from `tag_prefixes.writable_by` for
# fast checks; the DB still enforces. Kept in sync with
# ``0001_initial.sql``.
# ---------------------------------------------------------------------------
_AGENT_WRITABLE_PREFIXES: frozenset[str] = frozenset({"STATUS", "PRIO", "CONFIDENCE"})
_SYSTEM_WRITABLE_PREFIXES: frozenset[str] = frozenset({"SRC", "CACHE", "DENSITY"})


# Sentinel: pos = -1 in the DB means "ref-level"; callers see None.
_REF_LEVEL_POS = -1


# Default cosine-distance floor for semantic-only hits. pgvector's
# `<=>` operator returns ``1 - cosine_similarity``, so a distance of
# 0.65 maps to ``cos(theta) ≈ 0.35`` — the two vectors share a
# noticeable amount of subject matter without having to be near-
# paraphrases. We reject anything past this threshold from the
# semantic CTE so a nonsense query (``'food'`` against a corpus
# of chemistry patents) returns an honest empty response instead
# of a top-K of arbitrary blocks.
#
# The MCP critic flagged this originally (MAJOR #3: gibberish
# returns ranked hits).  The initial fix set the floor at 0.9 —
# which is right for hash-based MockEmbedder vectors (cos_sim ≈
# 0 for any two texts) but far too loose for real bge-m3
# embeddings, where arbitrary English text pairs cluster around
# cos_sim ≈ 0.3–0.7, i.e. distance 0.3–0.7, well under the 0.9
# bar.  A second-pass probe in 2026-05 confirmed the loose floor
# by showing that ``'food'`` and ``'bicycle purple alligator''``
# both returned ranked blocks from a CO2-capture patent.
# Tightening to 0.65 turns those queries into empty responses.
#
# The threshold is a default, not a constraint — callers (and
# the eventual public ``min_score=`` knob) can override per
# call.
SEMANTIC_DISTANCE_FLOOR = 0.65


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


# ---------------------------------------------------------------------------
# Row mappers (psycopg row tuple -> dataclass)
# ---------------------------------------------------------------------------


def _row_to_block(row: tuple) -> Block:
    """Map a v2 chunks row tuple onto the Block dataclass.

    Tuple layout (matches :data:`_CHUNKS_COLS` /
    :data:`_CHUNKS_COLS_ALIASED`):
      0 id           (= chunks.chunk_id)
      1 ref_id
      2 pos          (= chunks.ord)
      3 slug         (= chunks.meta->>'slug', NULL for non-prose chunks)
      4 text
      5 token_count
      6 embedding    (NULL unless JOINed against chunk_embeddings)
      7 density      (NULL unless JOINed against chunk_tags / tags)
      8 meta         (chunks.meta JSONB)
      9 created_at
      10 updated_at  (v2 chunks have no updated_at column; aliased to
                     created_at by the SQL projection so the dataclass
                     contract stays stable)
    """
    embedding = row[6]
    if embedding is not None and not isinstance(embedding, list):
        # pgvector returns numpy.ndarray when registered; coerce for stable
        # cross-version output.
        embedding = list(map(float, embedding))
    # ``section_path`` lives in its own TEXT[] column on ``chunks``
    # (v2; ADR 0018). For compatibility with code that still reads
    # ``block.meta['section_path']`` (oracle entry-title resolver,
    # paper TOC fallback, …), surface the array back into the meta
    # dict so consumers don't have to learn the column split.
    meta = dict(row[8] or {})
    # ``section_path`` is appended to the projection by every Block-
    # producing SELECT in this module. Defensively check the type:
    # a caller that hand-rolls a projection without the column will
    # pass row[:11] (no 12th elem) and the .get-style branch falls
    # through cleanly.
    section_path = row[11] if len(row) > 11 else None
    if isinstance(section_path, (list, tuple)) and section_path:
        meta.setdefault("section_path", list(section_path))
    # F19a / F20: chunk_kind + keywords appended at the end of the
    # projection. Optional positions (len(row) > 12 / > 13) keep
    # legacy callers that hand-roll 11-element tuples working.
    chunk_kind = row[12] if len(row) > 12 and row[12] else "paragraph"
    keywords = row[13] if len(row) > 13 else None
    if keywords is not None and not isinstance(keywords, list):
        keywords = list(keywords)
    return Block(
        id=row[0],
        ref_id=row[1],
        pos=row[2],
        slug=row[3],
        text=row[4],
        token_count=row[5],
        embedding=embedding,
        density=row[7],
        meta=meta,
        created_at=row[9],
        updated_at=row[10],
        chunk_kind=str(chunk_kind),
        keywords=keywords,
    )


# v2 chunk-column projection. Used by every SELECT that produces a
# tuple consumed by :func:`_row_to_block`. The slug, embedding, and
# density columns are virtual:
#  - slug comes from ``chunks.meta->>'slug'`` (prose handlers store
#    their stable citation handle there; non-prose chunks just return
#    NULL).
#  - embedding stays NULL on the projection — methods that need it
#    use :data:`_CHUNKS_COLS_WITH_EMBEDDING` and add the JOIN.
#  - density stays NULL on the projection — methods that need it
#    use :data:`_CHUNKS_COLS_WITH_DENSITY` and add the JOIN against
#    chunk_tags + tags filtered on ``namespace='DENSITY'``.
# Phase 2 keeps embedding/density routing simple; Phase 3 will
# introduce the JOIN variants for the search paths.
_CHUNKS_COLS = (
    "chunks.chunk_id AS id, chunks.ref_id, chunks.ord AS pos, "
    "(chunks.meta->>'slug') AS slug, chunks.text, chunks.token_count, "
    "NULL::vector AS embedding, "
    "(SELECT t.value FROM chunk_tags ct "
    "   JOIN tags t ON t.tag_id = ct.tag_id "
    "   WHERE ct.chunk_id = chunks.chunk_id AND t.namespace = 'DENSITY' "
    "   LIMIT 1) AS density, "
    "chunks.meta, chunks.created_at, chunks.created_at AS updated_at, "
    "chunks.section_path, chunks.chunk_kind, chunks.keywords"
)
_CHUNKS_COLS_ALIASED = (
    "c.chunk_id AS id, c.ref_id, c.ord AS pos, "
    "(c.meta->>'slug') AS slug, c.text, c.token_count, "
    "NULL::vector AS embedding, "
    "(SELECT t.value FROM chunk_tags ct "
    "   JOIN tags t ON t.tag_id = ct.tag_id "
    "   WHERE ct.chunk_id = c.chunk_id AND t.namespace = 'DENSITY' "
    "   LIMIT 1) AS density, "
    "c.meta, c.created_at, c.created_at AS updated_at, "
    "c.section_path, c.chunk_kind, c.keywords"
)
#: Column count produced by the above projections. Slicing callers
#: (search / random / list-blocks combined with refs) reference this
#: constant rather than a hard-coded ``12`` so adding columns is a
#: one-line change at the projection site.
_CHUNKS_COLS_LEN = 14


# ---------------------------------------------------------------------------
# Shared ``SELECT ... FROM refs`` column list.
#
# Every caller that wants a row :func:`_row_to_ref` can map needs the
# same columns in the same order; hand-copying the list diverges over
# time (MCP critic: the string was duplicated in 6+ locations plus a
# handler layering break in ``_numeric_ref._fetch_endpoints``).
#
# v2 schema notes:
# - ``id`` is sourced from ``ref_id`` (the column was renamed in
#   ``migrations/0001_initial.sql``); aliased here so callers' tuple
#   shape stays stable.
# - ``slug`` is sourced via a correlated subquery against
#   ``ref_identifiers`` with ``id_kind='cite_key'``. Every
#   slug-addressed kind stores its agent-facing slug there per ADR
#   0008. Numeric kinds (memory/todo/gripe/fc) have no row and slug
#   comes back ``NULL``.
# - ``corpus_id`` is gone — the v1 corpus isolation didn't survive
#   the v2 redesign (single-corpus deployment).
# - New v2 columns (set_by/authors/year/human_verified_*/
#   retraction_*/pdf_*) are projected too so the Ref dataclass has
#   the full row.
#
# Keep the two variants in lock-step: ``_REFS_COLS`` for unaliased
# queries (``FROM refs``), ``_REFS_COLS_ALIASED`` for queries that
# alias the table as ``r`` (tag-filter + joins).
# ---------------------------------------------------------------------------
_REFS_COLS = (
    "ref_id AS id, "
    "(SELECT id_value FROM ref_identifiers "
    " WHERE ref_id = refs.ref_id AND id_kind = 'cite_key') AS slug, "
    "kind, title, provider, meta, "
    "created_at, updated_at, deleted_at, "
    "set_by, authors, year, "
    "human_verified_at, human_verified_by, human_verified_note, "
    "retraction_status, retracted_at, retraction_reason, "
    "retraction_url, retraction_checked_at, "
    "pdf_sha256, pdf_pages::text AS pdf_pages, pdf_role, "
    "auto_refresh_days, refreshed_at, "
    "parent_id, prio"
)
_REFS_COLS_ALIASED = (
    "r.ref_id AS id, "
    "(SELECT id_value FROM ref_identifiers "
    " WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS slug, "
    "r.kind, r.title, r.provider, r.meta, "
    "r.created_at, r.updated_at, r.deleted_at, "
    "r.set_by, r.authors, r.year, "
    "r.human_verified_at, r.human_verified_by, r.human_verified_note, "
    "r.retraction_status, r.retracted_at, r.retraction_reason, "
    "r.retraction_url, r.retraction_checked_at, "
    "r.pdf_sha256, r.pdf_pages::text AS pdf_pages, r.pdf_role, "
    "r.auto_refresh_days, r.refreshed_at, "
    "r.parent_id, r.prio"
)
#: Column count produced by ``_REFS_COLS`` / ``_REFS_COLS_ALIASED``.
#: Joined-projection slicers (chunks ⋈ refs in ``_blocks_ops``)
#: reference this so adding a column to the projection list above
#: doesn't silently drift the downstream row layout.
_REFS_COLS_LEN = 27


def _row_to_ref(row: tuple) -> Ref:
    """Map a v2 refs row tuple. Column order matches :data:`_REFS_COLS`.

    Layout:
      0 id (= ref_id)
      1 slug (from ref_identifiers correlated subquery; may be NULL)
      2 kind
      3 title
      4 provider
      5 meta
      6 created_at
      7 updated_at
      8 deleted_at
      9 set_by
      10 authors
      11 year
      12 human_verified_at
      13 human_verified_by
      14 human_verified_note
      15 retraction_status
      16 retracted_at
      17 retraction_reason
      18 retraction_url
      19 retraction_checked_at
      20 pdf_sha256
      21 pdf_pages (text)
      22 pdf_role
      23 auto_refresh_days
      24 refreshed_at
      25 parent_id
      26 prio

    Every ``SELECT`` that feeds this mapper should reference
    :data:`_REFS_COLS` / :data:`_REFS_COLS_ALIASED` so drift between
    the SQL projection and the tuple layout can't happen.
    """
    return Ref(
        id=row[0],
        slug=row[1],
        kind=row[2],
        title=row[3],
        provider=row[4],
        meta=row[5] or {},
        created_at=row[6],
        updated_at=row[7],
        deleted_at=row[8],
        set_by=row[9],
        authors=row[10],
        year=row[11],
        human_verified_at=row[12],
        human_verified_by=row[13],
        human_verified_note=row[14],
        retraction_status=row[15],
        retracted_at=row[16],
        retraction_reason=row[17],
        retraction_url=row[18],
        retraction_checked_at=row[19],
        pdf_sha256=row[20],
        pdf_pages=row[21],
        pdf_role=row[22],
        auto_refresh_days=row[23],
        refreshed_at=row[24],
        parent_id=row[25],
        prio=row[26],
    )


def _row_to_link(row: tuple) -> Link:
    """Map a v2 links row tuple in the order:
    (id, src_ref_id, src_pos, dst_ref_id, dst_pos,
     relation, set_by, meta, created_at)

    v2 schema uses ``links.link_id`` (aliased to id in the SELECT)
    and ``src_chunk_id``/``dst_chunk_id`` FKs. The link queries
    LEFT JOIN ``chunks`` to translate chunk_id back to ord (the
    historical ``pos`` field). When the chunk_id is NULL (ref-level
    link), the LEFT JOIN yields NULL for ord, which arrives here as
    ``None`` directly — no -1 sentinel translation needed (v2
    dropped the sentinel in favour of NULL).
    """
    return Link(
        id=row[0],
        src_ref_id=row[1],
        src_pos=row[2],
        dst_ref_id=row[3],
        dst_pos=row[4],
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


__all__ = [
    "SEMANTIC_DISTANCE_FLOOR",
    "_AGENT_WRITABLE_PREFIXES",
    "_MARKUP_ONLY_BLOCK",
    "_MIN_BLOCK_CHARS",
    "_REFS_COLS",
    "_REFS_COLS_ALIASED",
    "_REF_LEVEL_POS",
    "_SYSTEM_WRITABLE_PREFIXES",
    "_block_noise_clauses",
    "_pos_to_db",
    "_row_to_block",
    "_row_to_cache_entry",
    "_row_to_link",
    "_row_to_ref",
]

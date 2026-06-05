"""Ref-level CRUD + lexical search. Mixin on :class:`precis.store.Store`.

Ref is the hub row in the v2 schema: one row per paper / memory / todo /
conversation / oracle / quest / .... All domain mixins ultimately touch
a ref_id; this module owns the ref rows themselves plus the title-level
lexical search that powers ``search(kind=..., q=...)`` for slug-addressed
kinds.

v2 schema notes:

- ``refs.id`` was renamed to ``refs.ref_id``; ``_REFS_COLS`` aliases
  it back to ``id`` so callers' tuple shape stays stable.
- ``refs.slug`` was removed; slugs live in ``ref_identifiers`` with
  ``id_kind='cite_key'`` per ADR 0008. ``insert_ref`` writes the
  identifier row when ``slug is not None``; ``get_ref`` /
  ``fetch_ref_ids_by_slugs`` JOIN through ``ref_identifiers`` for
  slug lookups.
- ``refs.corpus_id`` is gone (no corpus table in v2).
- ``refs.title_tsv`` is gone — ``search_refs_lexical`` /
  ``count_refs_lexical`` compute the tsv inline. The v2-recommended
  path for title search is ``chunks.tsv`` on the ``card_title`` chunk;
  this fallback stays here so callers that don't go through chunks
  still work, and Phase 3 will switch to the card-chunk variant.

The mixin assumes the concrete Store provides:

* ``self.pool``               — psycopg_pool.ConnectionPool
* ``self._validate_slug_for_kind(kind, slug, conn=...)`` — schema rule

Mypy-side: both are declared as class-level annotations so the
mixin type-checks in isolation; at runtime they're resolved by
MRO against the concrete ``Store``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import NotFound
from precis.store._mappers import _REFS_COLS, _REFS_COLS_ALIASED, _row_to_ref
from precis.store._tag_filter import build_tag_filter
from precis.store.types import Ref


class RefsMixin:
    """Ref insert / get / update / delete + list + lexical search."""

    pool: ConnectionPool

    # Provided by the concrete Store — validates the ``slug vs None``
    # rule per kind (numeric kinds reject non-None slugs, slug kinds
    # require a slug). MRO resolves this to the real implementation
    # at runtime; calling it on a bare ``RefsMixin`` raises.
    def _validate_slug_for_kind(
        self,
        kind: str,
        slug: str | None,
        *,
        conn: Connection | None = None,
    ) -> None:
        raise NotImplementedError  # pragma: no cover — overridden by Store

    def insert_ref(
        self,
        *,
        kind: str,
        slug: str | None,
        title: str,
        provider: str | None = None,
        meta: dict[str, Any] | None = None,
        authors: list[dict[str, Any]] | None = None,
        year: int | None = None,
        conn: Connection | None = None,
    ) -> Ref:
        """Insert a ref. Slug rules:

        - Slug kinds (paper/book/oracle/conv/skill/quest): slug required.
        - Numeric kinds (todo/memory/gripe/fc): slug must be None.

        Enforced at app layer (the DB ``CHECK`` can't subquery the
        ``kinds`` reference table).

        ``authors`` / ``year`` are first-class ``refs`` columns in
        the v2 schema; pass them here so renderers that read
        ``Ref.authors`` / ``Ref.year`` (bibtex, RIS, EndNote) see
        them. Stashing them in ``meta`` instead leaves the columns
        NULL and the renderer with nothing to show — which was the
        pre-fix shape and the cause of ~30 test_paper failures.

        v2 inserts in two steps inside the same connection: first the
        ``refs`` row, then (when ``slug is not None``) a row in
        ``ref_identifiers`` with ``id_kind='cite_key'``. Both rows
        commit together — callers pass a shared ``conn`` if they need
        the pair to participate in an outer transaction.
        """
        self._validate_slug_for_kind(kind, slug, conn=conn)

        insert_sql = (
            "INSERT INTO refs (kind, title, authors, year, provider, meta) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "RETURNING ref_id"
        )
        insert_params: tuple[Any, ...] = (
            kind,
            title,
            Jsonb(authors) if authors is not None else None,
            year,
            provider,
            Jsonb(meta or {}),
        )

        def _do(c: Connection) -> Ref:
            row = c.execute(insert_sql, insert_params).fetchone()
            assert row is not None
            ref_id = int(row[0])
            if slug is not None:
                # Routing decision (ADR 0008 + plan): every slug-addressed
                # kind uses id_kind='cite_key' uniformly so the
                # correlated subquery in ``_REFS_COLS`` resolves with a
                # single predicate.
                c.execute(
                    "INSERT INTO ref_identifiers "
                    "(id_kind, id_value, ref_id, source) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (id_kind, id_value) DO NOTHING",
                    ("cite_key", slug, ref_id, provider),
                )
            # Re-fetch the row with the full _REFS_COLS projection so
            # the returned ``Ref`` carries the slug we just wrote.
            fresh = c.execute(
                f"SELECT {_REFS_COLS} FROM refs WHERE ref_id = %s",
                (ref_id,),
            ).fetchone()
            assert fresh is not None
            return _row_to_ref(fresh)

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def get_ref(
        self,
        *,
        kind: str,
        id: int | str,
        include_deleted: bool = False,
    ) -> Ref | None:
        """Look up by (kind, public id).

        Public id = slug for slug kinds (resolved via ``ref_identifiers``
        with ``id_kind='cite_key'``), ``int(refs.ref_id)`` for numeric
        kinds. The caller's ``isinstance`` of ``id`` picks the path.
        """
        if isinstance(id, int):
            sql = f"SELECT {_REFS_COLS} FROM refs WHERE kind = %s AND ref_id = %s"
            params: tuple[Any, ...] = (kind, id)
            if not include_deleted:
                sql += " AND deleted_at IS NULL"
            with self.pool.connection() as conn:
                row = conn.execute(sql, params).fetchone()
            return _row_to_ref(row) if row is not None else None

        # Slug lookup. Resolve via ref_identifiers first; then fetch
        # the full ref row. Two queries beats one big JOIN here because
        # the ref_identifiers pkey lookup is the fast path; the JOIN
        # would force a hash join under cost-based planning.
        with self.pool.connection() as conn:
            ident_row = conn.execute(
                "SELECT ref_id FROM ref_identifiers "
                "WHERE id_kind = 'cite_key' AND id_value = %s",
                (id,),
            ).fetchone()
            if ident_row is None:
                return None
            ref_id = int(ident_row[0])
            sql = f"SELECT {_REFS_COLS} FROM refs WHERE kind = %s AND ref_id = %s"
            if not include_deleted:
                sql += " AND deleted_at IS NULL"
            row = conn.execute(sql, (kind, ref_id)).fetchone()
        return _row_to_ref(row) if row is not None else None

    def find_paper_slug_by_doi(self, doi: str) -> str | None:
        """Look up a paper's slug (cite_key) by its DOI.

        Used by the paper ``get`` entry point so callers can address a
        paper by its DOI (``10.1111/jnc.13915``) in addition to its
        minted slug — a convenience for agents that have a bibliography
        full of DOIs from an external source and haven't yet learned
        the local slug naming convention.

        Delegates to the generic ``ref_identifiers`` index. arXiv DOIs
        (the ``10.48550/arXiv.X`` form) automatically resolve through
        the ``arxiv`` scheme path because
        :func:`detect_identifier_scheme` recognises that prefix and
        translates the DOI to the bare arXiv id used as the canonical
        alias value.

        For non-DOI identifier lookup (bare arXiv id, S2 paperId,
        PubMed, OpenAlex, pdf_hash) callers should use
        :meth:`IdentifiersMixin.find_paper_ref_by_identifier` directly.

        Returns ``None`` when no live paper carries this DOI; the
        caller decides whether that's an error (agent-facing) or a
        fall-through (internal dedupe already has its own path).
        """
        ref_id = self.find_paper_ref_by_identifier(doi)  # type: ignore[attr-defined]
        if ref_id is None:
            return None
        # Reverse-lookup the cite_key for this ref_id.
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'cite_key'",
                (ref_id,),
            ).fetchone()
        return row[0] if row is not None else None

    def fetch_ref_ids_by_slugs(
        self,
        slugs: Iterable[str],
        *,
        kind: str,
    ) -> list[int]:
        """Bulk slug→ref_id resolver. Live refs only.

        Returns the ref ids for slugs that resolve in this kind;
        unknown / deleted slugs are silently dropped. Used by the
        search ``exclude=`` path so an agent passing back the slugs
        from a prior response gets a "skip these" filter without
        N round-trips and without a ``BadInput`` on a stale slug.

        Order of the input is not preserved — callers that care
        should map results back via the returned set membership.

        v2: slugs resolve via ``ref_identifiers`` (``id_kind='cite_key'``)
        JOINed back to ``refs`` to enforce the kind filter and the
        soft-delete predicate.
        """
        unique = list({s for s in slugs if s})
        if not unique:
            return []
        sql = (
            "SELECT r.ref_id FROM refs r "
            "JOIN ref_identifiers ri "
            "  ON ri.ref_id = r.ref_id "
            "  AND ri.id_kind = 'cite_key' "
            "WHERE r.kind = %s "
            "  AND ri.id_value = ANY(%s) "
            "  AND r.deleted_at IS NULL"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (kind, unique)).fetchall()
        return [int(r[0]) for r in rows]

    def fetch_refs_by_ids(
        self,
        ref_ids: Iterable[int],
        *,
        include_deleted: bool = True,
    ) -> dict[int, Ref]:
        """Bulk-fetch refs by id, returning ``{id: Ref}``.

        Used by callers that have a set of ``ref_id`` integers and
        need the full :class:`Ref` row for each — most commonly the
        link-endpoint resolver in :class:`NumericRefHandler`, which
        used to reach into ``self.store.pool`` with its own raw
        ``SELECT`` (a handler/schema layering break flagged by the
        MCP critic).

        ``include_deleted`` defaults to ``True`` because the primary
        caller (link rendering) wants to show a soft-deleted endpoint
        with a deletion marker rather than silently dropping the row.
        Pass ``False`` to filter tombstones out.

        Missing ids are simply absent from the returned dict — the
        caller decides whether that's an error or an ``<unknown>``
        placeholder.
        """
        ids = list(ref_ids)
        if not ids:
            return {}
        sql = f"SELECT {_REFS_COLS} FROM refs WHERE ref_id = ANY(%s)"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (ids,)).fetchall()
        return {r[0]: _row_to_ref(r) for r in rows}

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
        sql = f"""
            UPDATE refs SET
                title = COALESCE(%s, title),
                meta  = CASE WHEN %s::jsonb IS NULL
                             THEN meta
                             ELSE meta || %s::jsonb
                        END,
                updated_at = now()
            WHERE ref_id = %s AND deleted_at IS NULL
            RETURNING {_REFS_COLS}
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

    def set_retraction_status(
        self,
        ref_id: int,
        *,
        status: str | None,
        retracted_at: Any = None,
        reason: str | None = None,
        url: str | None = None,
        conn: Connection | None = None,
    ) -> None:
        """Set the retraction columns on a ref + touch retraction_checked_at.

        ``status`` is one of ``'retracted'``, ``'corrected'``,
        ``'expression_of_concern'`` (per the CHECK constraint in
        ``0001_initial.sql``) or ``None`` when the paper is clean —
        in which case we still touch ``retraction_checked_at`` so the
        TTL gate works. See ``ingest/provenance.py`` for the caller
        and ``docs/design/provenance-kind-plan.md`` for the schema rationale.
        """
        sql = (
            "UPDATE refs SET "
            "  retraction_status = %s, "
            "  retracted_at      = %s, "
            "  retraction_reason = %s, "
            "  retraction_url    = %s, "
            "  retraction_checked_at = now(), "
            "  updated_at = now() "
            "WHERE ref_id = %s AND deleted_at IS NULL"
        )
        params = (status, retracted_at, reason, url, ref_id)
        if conn is not None:
            conn.execute(sql, params)
        else:
            with self.pool.connection() as c:
                c.execute(sql, params)

    def set_human_verified(
        self,
        ref_id: int,
        *,
        by: str,
        note: str | None = None,
        conn: Connection | None = None,
    ) -> None:
        """Stamp ``human_verified_at`` / ``_by`` / ``_note`` on a ref.

        Sets ``human_verified_at = now()`` and records the verifier
        identity + optional note. Idempotent on re-stamp (refreshes
        the timestamp and overwrites note).

        Used by ``precis verify <pub_id>`` to mark a finding's chain
        as human-checked; ``precis resolve --strict-verified`` gates
        substitution on this column being non-NULL.

        The schema reserves these columns on every ref (not just
        findings) — papers, memories, etc. can carry verification
        too — but the only writer today is the finding-verify path.
        """
        sql = (
            "UPDATE refs SET "
            "  human_verified_at   = now(), "
            "  human_verified_by   = %s, "
            "  human_verified_note = %s, "
            "  updated_at = now() "
            "WHERE ref_id = %s AND deleted_at IS NULL"
        )
        params = (by, note, ref_id)
        if conn is not None:
            cur = conn.execute(sql, params)
        else:
            with self.pool.connection() as c:
                cur = c.execute(sql, params)
        if cur.rowcount == 0:
            raise NotFound(
                f"ref id={ref_id} not found (or already deleted)",
                next=f"get(kind='finding', id={ref_id}) to confirm",
            )

    def clear_human_verified(
        self,
        ref_id: int,
        *,
        conn: Connection | None = None,
    ) -> None:
        """Clear ``human_verified_at`` / ``_by`` / ``_note`` on a ref.

        Inverse of :meth:`set_human_verified` — used when the chain
        has been re-graded (e.g. an upstream ref was retracted) and
        the prior verification is no longer trustworthy.
        """
        sql = (
            "UPDATE refs SET "
            "  human_verified_at   = NULL, "
            "  human_verified_by   = NULL, "
            "  human_verified_note = NULL, "
            "  updated_at = now() "
            "WHERE ref_id = %s AND deleted_at IS NULL"
        )
        if conn is not None:
            conn.execute(sql, (ref_id,))
        else:
            with self.pool.connection() as c:
                c.execute(sql, (ref_id,))

    def soft_delete_ref(self, ref_id: int) -> None:
        """Soft-delete a ref by setting ``deleted_at = now()``."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE refs SET deleted_at = now() "
                "WHERE ref_id = %s AND deleted_at IS NULL",
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
        kind: str | None = None,
        provider: str | None = None,
        updated_after: datetime | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Ref]:
        """Paginated list of live refs, filter by kind/provider/tags."""
        # Aliased as ``r`` so the tag-filter helper can reference
        # ``r.ref_id`` uniformly across all store query shapes.
        clauses = ["r.deleted_at IS NULL"]
        params: list[Any] = []
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
            f"SELECT {_REFS_COLS_ALIASED} FROM refs r WHERE "
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

        v2: ``refs.title_tsv`` was dropped; compute it inline via
        ``to_tsvector('english', r.title)``. Slower than a precomputed
        column but functionally identical; Phase 3 plans to switch to
        ``chunks.tsv`` on the ``card_title`` chunk for the optimised
        path.
        """
        clauses = [
            "r.deleted_at IS NULL",
            "to_tsvector('english', r.title) @@ qq.qq",
        ]
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
        """Lexical search over ``refs.title``.

        Returns ``(ref, rank)`` sorted by rank desc. Semantic + RRF
        fusion happen at the block level; title-level stays
        lexical-only.

        v2: ``refs.title_tsv`` was dropped; compute it inline via
        ``to_tsvector('english', r.title)``. Phase 3 will switch this
        to the precomputed ``chunks.tsv`` on the ``card_title`` chunk
        for the indexed-lookup path.
        """
        clauses = [
            "r.deleted_at IS NULL",
            "to_tsvector('english', r.title) @@ qq.qq",
        ]
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
            f"SELECT {_REFS_COLS_ALIASED}, "
            "       ts_rank_cd(to_tsvector('english', r.title), qq.qq) AS rank "
            "FROM refs r, websearch_to_tsquery('english', %s) qq(qq) "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY rank DESC LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        # rows are tuples in column order; rank is the last column.
        # _REFS_COLS projects 23 columns; rank is at index 23.
        result: list[tuple[Ref, float]] = []
        for r in rows:
            ref = _row_to_ref(r[:23])
            result.append((ref, float(r[23])))
        return result

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


__all__ = ["RefsMixin"]

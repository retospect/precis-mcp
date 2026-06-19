"""Ref-level CRUD + lexical search. Mixin on :class:`precis.store.Store`.

Ref is the hub row in the v2 schema: one row per paper / memory / todo /
conversation / oracle / .... All domain mixins ultimately touch
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
from typing import TYPE_CHECKING, Any, ClassVar

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import NotFound
from precis.store._mappers import (
    _REFS_COLS,
    _REFS_COLS_ALIASED,
    _REFS_COLS_LEN,
    _row_to_ref,
)
from precis.store._tag_filter import build_tag_filter
from precis.store.types import ActorSlug, Ref, Tag


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

    # Provided by ``TagsMixin`` on the concrete Store; declared here so
    # the retraction-cascade path in ``regrade_finding_for_retraction``
    # type-checks against the cross-mixin call. **Must be TYPE_CHECKING
    # only** — Store's MRO is (Store, RefsMixin, BlocksMixin, TagsMixin,
    # ...), so a runtime ``def add_tag`` here wins over TagsMixin's real
    # implementation and every numeric-ref put dies with
    # NotImplementedError. Filed gripe: see CHANGELOG entry for
    # migration 0005 / handler rewrite for the prior incident that
    # surfaced this.
    if TYPE_CHECKING:

        def add_tag(
            self,
            ref_id: int,
            tag: Tag,
            *,
            pos: int | None = None,
            set_by: ActorSlug = "agent",
            replace_prefix: bool = False,
            expires_at: datetime | None = None,
            conn: Connection | None = None,
        ) -> None: ...

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
        auto_refresh_days: int | None = None,
        parent_id: int | None = None,
        prio: int | None = None,
        conn: Connection | None = None,
    ) -> Ref:
        """Insert a ref. Slug rules:

        - Slug kinds (paper/book/oracle/conv/skill): slug required.
        - Numeric kinds (todo/memory/gripe/flashcard): slug must be None.

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

        # ``auto_refresh_days`` (migration 0011) opts the ref into
        # Model A relevance decay: weight slides from 1.0 → 0 over
        # the next N days unless refreshed via ``touch``. NULL =
        # permanent (default). Initial ``refreshed_at = now()`` so
        # the decay clock starts immediately.
        #
        # ``parent_id`` (migration 0013 / todo-tree) wires the ref
        # into a hierarchical task graph. NULL for refs not in a
        # tree. Cycle / depth / level-gradient guards run at the
        # handler layer (see ``handlers/_todo_guards.py``) before
        # this insert, so the store layer trusts the caller.
        if auto_refresh_days is not None:
            insert_sql = (
                "INSERT INTO refs "
                "(kind, title, authors, year, provider, meta, "
                " auto_refresh_days, refreshed_at, parent_id, prio) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s, %s) "
                "RETURNING ref_id"
            )
            insert_params: tuple[Any, ...] = (
                kind,
                title,
                Jsonb(authors) if authors is not None else None,
                year,
                provider,
                Jsonb(meta or {}),
                auto_refresh_days,
                parent_id,
                prio,
            )
        else:
            insert_sql = (
                "INSERT INTO refs "
                "(kind, title, authors, year, provider, meta, parent_id, prio) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING ref_id"
            )
            insert_params = (
                kind,
                title,
                Jsonb(authors) if authors is not None else None,
                year,
                provider,
                Jsonb(meta or {}),
                parent_id,
                prio,
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

    def upsert_stub_paper(
        self,
        *,
        identifiers: list[tuple[str, str]],
        title: str | None = None,
        year: int | None = None,
        set_by: str = "dream",
        conn: Connection | None = None,
    ) -> tuple[int, bool]:
        """Idempotently find-or-mint a stub paper ref by identifier-collapse.

        A *stub* is a ``paper`` ref with no body and ``pdf_sha256 IS
        NULL``, so the ``fetch_oa`` worker auto-claims it on a later
        pass when it carries a DOI/arXiv/S2 id. Returns ``(ref_id,
        created)``.

        ``identifiers`` is a list of ``(id_kind, id_value)`` pairs
        (e.g. ``[("doi", "10.1/x"), ("arxiv", "2401.00001")]``). The
        method probes ``ref_identifiers`` for any of them first — a hit
        short-circuits to the existing ref (``created=False``), so
        re-acquiring an already-held or already-wanted paper is a no-op.
        On a miss (or when no identifiers are supplied), it mints a
        ``paper`` ref with a freshly-minted ``cite_key`` slug and
        ``meta.set_by=<set_by>``, registers every identifier, and
        returns ``created=True``.

        Mirrors the chase worker's stub path
        (``workers/chase._resolve_or_create_stub``) but takes explicit
        identifier pairs so the gated dream ``acquire`` tool can reuse
        it (docs/design/dreaming.md, §Acquire).
        """
        from precis.identity import make_cite_key

        norm = [(k, v.strip()) for k, v in identifiers if v and v.strip()]

        def _do(c: Connection) -> tuple[int, bool]:
            for id_kind, id_value in norm:
                row = c.execute(
                    "SELECT ref_id FROM ref_identifiers "
                    "WHERE id_kind = %s AND id_value = %s",
                    (id_kind, id_value),
                ).fetchone()
                if row is not None:
                    return int(row[0]), False

            # No collapse hit — mint a stub. Derive a non-colliding
            # cite_key from the title's first word + year.
            first_word = (title or "").split()
            authors = [{"family": first_word[0] if first_word else "anon"}]
            base = make_cite_key(authors, year)
            taken_rows = c.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE id_kind = 'cite_key' AND id_value LIKE %s",
                (base + "%",),
            ).fetchall()
            taken = {str(r[0]) for r in taken_rows}
            cite_key = make_cite_key(authors, year, taken=taken)

            new_ref = self.insert_ref(
                kind="paper",
                slug=cite_key,
                title=title or "(no title)",
                year=year,
                meta={"set_by": set_by},
                conn=c,
            )
            for id_kind, id_value in norm:
                c.execute(
                    "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (id_kind, id_value, new_ref.id, set_by),
                )
            return int(new_ref.id), True

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def stub_backlog(
        self,
        *,
        limit: int = 50,
        awaiting: bool = False,
    ) -> list[dict[str, Any]]:
        """The "papers we still need to get" backlog, newest-stub-first.

        A *stub* is a ``paper`` ref with an external identifier
        (DOI / arXiv / S2) registered but ``pdf_sha256 IS NULL`` — the
        chase worker and the dream ``acquire`` tool both mint these so
        the ``fetch_oa`` worker can auto-grab an OA PDF later. This
        method surfaces them joined with the latest ``fetcher:%``
        attempt per ref, returning one dict per stub with a one-line
        ``state`` summary an operator (or agent) can scan.

        Shared by ``precis stubs`` (CLI) and ``search(view='stubs')``
        (MCP) so both render from one query
        (docs/design/stubs-mcp-and-skill.md).

        ``awaiting=True`` restricts to rows the fetcher would actually
        try on its next pass: never attempted, or attempted >24h ago
        and not yet ``fetch_ok``.
        """
        sql = """
            WITH stubs AS (
                SELECT r.ref_id,
                       (SELECT id_value FROM ref_identifiers
                         WHERE ref_id = r.ref_id AND id_kind = 'cite_key') AS cite_key,
                       COALESCE(
                         (SELECT id_value FROM ref_identifiers
                           WHERE ref_id = r.ref_id AND id_kind = 'doi'),
                         (SELECT 'arxiv:' || id_value FROM ref_identifiers
                           WHERE ref_id = r.ref_id AND id_kind = 'arxiv'),
                         (SELECT 's2:' || id_value FROM ref_identifiers
                           WHERE ref_id = r.ref_id AND id_kind = 's2')
                       ) AS identifier,
                       r.ref_id AS sort_key
                  FROM refs r
                 WHERE r.kind = 'paper'
                   AND r.pdf_sha256 IS NULL
                   AND r.deleted_at IS NULL
                   AND EXISTS (
                         SELECT 1 FROM ref_identifiers ri
                          WHERE ri.ref_id = r.ref_id
                            AND ri.id_kind IN ('doi', 'arxiv', 's2')
                   )
            ),
            latest_event AS (
                SELECT DISTINCT ON (ref_id) ref_id, ts, source, event
                  FROM ref_events
                 WHERE source LIKE 'fetcher:%%'
                 ORDER BY ref_id, ts DESC
            )
            SELECT s.ref_id, s.cite_key, s.identifier,
                   le.ts, le.source, le.event
              FROM stubs s
              LEFT JOIN latest_event le ON le.ref_id = s.ref_id
             WHERE
                CASE WHEN %s::bool THEN
                    (le.ref_id IS NULL
                     OR (le.ts < now() - INTERVAL '24 hours' AND le.event <> 'fetch_ok'))
                ELSE TRUE END
             ORDER BY s.sort_key DESC
             LIMIT %s
        """
        out: list[dict[str, Any]] = []
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (awaiting, limit)).fetchall()
        for row in rows:
            out.append(
                {
                    "ref_id": int(row[0]),
                    "cite_key": row[1] or "",
                    "identifier": row[2] or "",
                    "last_attempt": row[3].isoformat() if row[3] is not None else "",
                    "last_source": row[4] or "",
                    "last_event": row[5] or "",
                    "state": _stub_state_summary(row[5], row[3]),
                }
            )
        return out

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

    def set_prio(
        self,
        ref_id: int,
        prio: int | None,
        *,
        conn: Connection | None = None,
    ) -> None:
        """Set or clear the ``refs.prio`` column (migration 0014).

        Range-checked at the DB layer (``CHECK (prio BETWEEN 1 AND
        10)``); the handler boundary validates before calling so an
        agent gets ``BadInput`` with the catalogue instead of a raw
        Postgres ``check_violation``. ``prio=None`` clears the column
        back to NULL (= "use the default at sort time").
        """
        sql = (
            "UPDATE refs SET prio = %s, updated_at = now() "
            "WHERE ref_id = %s AND deleted_at IS NULL"
        )
        if conn is not None:
            conn.execute(sql, (prio, ref_id))
        else:
            with self.pool.connection() as c:
                c.execute(sql, (prio, ref_id))

    def set_parent(
        self,
        ref_id: int,
        new_parent_id: int | None,
        *,
        conn: Connection | None = None,
    ) -> None:
        """Re-point ``refs.parent_id`` (the todo-tree move operation).

        ``new_parent_id=None`` detaches the ref to a root. The
        self-referencing FK (migration 0013, ``ON DELETE SET NULL``)
        permits any target the row exists for; the cycle/depth/level
        guards that make a move *safe* live in
        :mod:`precis.handlers._todo_guards` and run at the handler
        boundary before this is called. This method is the bare
        column write so the guards stay the single source of truth.
        """
        sql = (
            "UPDATE refs SET parent_id = %s, updated_at = now() "
            "WHERE ref_id = %s AND deleted_at IS NULL"
        )
        if conn is not None:
            conn.execute(sql, (new_parent_id, ref_id))
        else:
            with self.pool.connection() as c:
                c.execute(sql, (new_parent_id, ref_id))

    def locked_ref_ids(self, ref_ids: list[int]) -> set[int]:
        """Return the subset of ``ref_ids`` currently row-locked.

        Used by the web Tasks tab to flag "locked right now" nodes —
        e.g. a ``kind='job'`` ref a worker holds ``FOR UPDATE`` during
        its claim window. Implemented as a ``SELECT … FOR UPDATE SKIP
        LOCKED`` diff: rows another transaction holds are *skipped*, so
        the ids we fail to re-select are exactly the locked ones. We
        ``rollback()`` immediately so the brief locks we take on the
        free rows are released before returning — this is a read-only
        probe, never a real claim.
        """
        if not ref_ids:
            return set()
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) FOR UPDATE SKIP LOCKED",
                (ref_ids,),
            ).fetchall()
            conn.rollback()
        free = {int(r[0]) for r in rows}
        return {rid for rid in ref_ids if rid not in free}

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

    def update_paper_fields(
        self,
        ref_id: int,
        *,
        title: str | None = None,
        year: int | None = None,
        authors: list[dict[str, str]] | None = None,
        meta_patch: dict[str, Any] | None = None,
        source: str = "web-edit",
        conn: Connection | None = None,
    ) -> Ref:
        """Patch a paper's first-class metadata columns + merge ``meta``.

        COALESCE semantics: a ``None`` argument leaves that column
        untouched; pass an explicit value to overwrite. ``meta_patch``
        is a top-level merge (``meta || patch``) used for ``abstract``
        and other meta-resident fields. ``authors`` is stored verbatim
        as JSONB — canonicalise to the ``[{"name": …}]`` shape *before*
        calling (see :func:`precis.utils.authors.to_name_dicts`) so the
        column converges on one shape.

        Unlike :meth:`update_ref` (title + meta only), this is the sole
        write path for the ``year`` / ``authors`` columns, which were
        otherwise set only at ingest. Logs a ``metadata_edited``
        ref_event carrying the changed keys so the edit is auditable /
        recoverable via ``view='log'``.
        """
        changed: list[str] = []
        if title is not None:
            changed.append("title")
        if year is not None:
            changed.append("year")
        if authors is not None:
            changed.append("authors")
        if meta_patch:
            changed.extend(f"meta.{k}" for k in meta_patch)
        sql = f"""
            UPDATE refs SET
                title   = COALESCE(%s, title),
                year    = COALESCE(%s, year),
                authors = COALESCE(%s::jsonb, authors),
                meta    = CASE WHEN %s::jsonb IS NULL THEN meta
                               ELSE meta || %s::jsonb END,
                updated_at = now()
            WHERE ref_id = %s AND deleted_at IS NULL
            RETURNING {_REFS_COLS}
        """
        params = (
            title,
            year,
            Jsonb(authors) if authors is not None else None,
            Jsonb(meta_patch) if meta_patch else None,
            Jsonb(meta_patch) if meta_patch else None,
            ref_id,
        )

        def _do(c: Connection) -> Any:
            row = c.execute(sql, params).fetchone()
            if row is None:
                raise NotFound(
                    f"ref id={ref_id} not found (or already deleted)",
                    next=f"check id with: get(kind=..., id={ref_id})",
                )
            c.execute(
                "INSERT INTO ref_events (ref_id, source, event, payload) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (ref_id, source, "metadata_edited", Jsonb({"changed": changed})),
            )
            return row

        if conn is not None:
            row = _do(conn)
        else:
            with self.pool.connection() as c:
                row = _do(c)
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
        propagate_to_findings: bool = True,
    ) -> int:
        """Set the retraction columns on a ref + touch retraction_checked_at.

        ``status`` is one of ``'retracted'``, ``'corrected'``,
        ``'expression_of_concern'`` (per the CHECK constraint in
        ``0001_initial.sql``) or ``None`` when the paper is clean —
        in which case we still touch ``retraction_checked_at`` so the
        TTL gate works. See ``ingest/provenance.py`` for the caller
        and ``docs/design/provenance-kind-plan.md`` for the schema rationale.

        Returns the number of findings whose chain was re-graded
        as a side effect (0 when ``status`` is None or no finding
        cites this ref).

        ``propagate_to_findings`` (default True) triggers the
        chase re-grading sweep — see
        :meth:`_propagate_retraction_to_findings`. Set False on
        bulk retraction backfills that have their own propagation
        path; the default keeps findings honest by default.
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

        def _do(c: Connection) -> int:
            c.execute(sql, params)
            # Propagate only when the retraction is real (not just
            # touching ``retraction_checked_at``). The caller can
            # opt out for bulk backfills.
            if not (propagate_to_findings and status):
                return 0
            return self._propagate_retraction_to_findings(ref_id, reason=reason, conn=c)

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def _propagate_retraction_to_findings(
        self,
        retracted_ref_id: int,
        *,
        reason: str | None,
        conn: Connection,
    ) -> int:
        """Re-grade every finding whose chain cites the retracted ref.

        When a paper goes retracted, any finding that walked through
        it has a tainted citation chain — the previously-resolved
        primary_cite_key was reached via a now-untrustworthy hop.
        We restore those findings to ``STATUS:tracing`` so the chase
        worker re-walks the chain on the next pass, clear the
        ``human_verified_at`` stamp (a prior human review can't
        cover a chain that's since shifted), and append a
        ``retraction_caveat`` entry to ``meta`` so the next reader
        sees what changed.

        Findings are matched by membership in ``meta.chain`` —
        every hop the chase added carries the visited ``ref_id``.
        Soft-deleted findings are skipped.

        Returns the number of findings re-graded. An emitted
        ``ref_events`` row (``source='retraction_propagation'``)
        per affected finding makes the trail auditable from
        ``view='log'``.
        """
        # ``meta @> '{"chain": [{"ref_id": N}]}'::jsonb`` would be
        # ideal but pg's JSON containment doesn't match nested
        # array elements that way. Fall back to a JSONB-path
        # existence check — fast enough at the volumes we care
        # about (findings table is small).
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs "
            "WHERE kind = 'finding' AND deleted_at IS NULL "
            "  AND meta @? "
            "      ('$.chain[*] ? (@.ref_id == ' || %s || ')')::jsonpath",
            (retracted_ref_id,),
        ).fetchall()
        if not rows:
            return 0

        retracted_slug_row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE id_kind = 'cite_key' AND ref_id = %s LIMIT 1",
            (retracted_ref_id,),
        ).fetchone()
        retracted_handle = (
            str(retracted_slug_row[0])
            if retracted_slug_row is not None
            else f"ref:{retracted_ref_id}"
        )

        caveat_record = {
            "ref_id": retracted_ref_id,
            "handle": retracted_handle,
            "reason": reason or "(no reason given)",
        }

        n = 0
        for row in rows:
            finding_ref_id = int(row[0])
            meta = dict(row[1] or {})
            existing_caveats = list(meta.get("retraction_caveats") or [])
            # Skip re-propagation of the same retraction (idempotent
            # on repeat calls; matters when the provenance worker
            # re-confirms a known retraction).
            if any(c.get("ref_id") == retracted_ref_id for c in existing_caveats):
                continue
            existing_caveats.append(caveat_record)
            conn.execute(
                "UPDATE refs SET "
                "  meta = meta || jsonb_build_object("
                "    'retraction_caveats', %s::jsonb"
                "  ), "
                "  human_verified_at   = NULL, "
                "  human_verified_by   = NULL, "
                "  human_verified_note = NULL, "
                "  updated_at = now() "
                "WHERE ref_id = %s",
                (Jsonb(existing_caveats), finding_ref_id),
            )
            # Flip STATUS back to tracing so the chase re-walks
            # this row on the next worker pass.
            self.add_tag(
                finding_ref_id,
                Tag.closed("STATUS", "tracing"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
            # Auditable trail: every retraction re-grade lands a
            # ref_events row so the per-finding ``view='log'``
            # surface tells the operator why a previously
            # established finding is back in flight.
            conn.execute(
                "INSERT INTO ref_events "
                "(ref_id, source, event, payload) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (
                    finding_ref_id,
                    "retraction_propagation",
                    "regraded_to_tracing",
                    Jsonb(caveat_record),
                ),
            )
            n += 1
        return n

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

    def replace_ref_text(
        self,
        ref_id: int,
        new_text: str,
        *,
        source: str = "agent",
        conn: Connection | None = None,
    ) -> str | None:
        """In-place rewrite of a numeric-ref kind's body (``refs.title``).

        Updates the body, bumps ``updated_at``, and writes a
        ``body_replaced`` row to ``ref_events`` with the old body as
        payload — so ``view='log'`` surfaces the rewrite history.
        Returns the old text (for callers that want to render a diff
        or re-embed the survivor's card chunk).

        Distinct from ``supersede``: same id stays, links stay attached,
        no consolidation. The "polish a thought" verb. Broad-pass
        finding #5 — agents had no way to fix wording without
        delete + re-put, which breaks every inbound edge.
        """

        def _do(c: Connection) -> str | None:
            row = c.execute(
                "SELECT title FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
                (ref_id,),
            ).fetchone()
            if row is None:
                return None
            old_text = row[0]
            c.execute(
                "UPDATE refs SET title = %s, updated_at = now() WHERE ref_id = %s",
                (new_text, ref_id),
            )
            c.execute(
                "INSERT INTO ref_events "
                "(ref_id, source, event, payload) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (
                    ref_id,
                    source,
                    "body_replaced",
                    Jsonb({"old_text": old_text, "new_text": new_text}),
                ),
            )
            return old_text

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def soft_delete_ref(
        self,
        ref_id: int,
        *,
        conn: Connection | None = None,
    ) -> None:
        """Soft-delete a ref by setting ``deleted_at = now()``.

        ``conn`` lets the delete join an outer transaction (e.g. the
        memory ``supersede`` merge, where retiring the originals must
        be atomic with minting the survivor + migrating links).
        """
        sql = (
            "UPDATE refs SET deleted_at = now() "
            "WHERE ref_id = %s AND deleted_at IS NULL"
        )
        if conn is not None:
            rowcount = conn.execute(sql, (ref_id,)).rowcount
        else:
            with self.pool.connection() as c:
                rowcount = c.execute(sql, (ref_id,)).rowcount
        if rowcount == 0:
            raise NotFound(f"ref id={ref_id} not found (or already deleted)")

    def touch_ref(
        self,
        ref_id: int,
        *,
        auto_refresh_days: int | None = None,
        conn: Connection | None = None,
    ) -> None:
        """Mark a ref as freshly relevant (migration 0011 / Model A).

        Bumps ``refreshed_at = now()`` so the decay weight returns to
        1.0 for the configured ``auto_refresh_days`` window. When
        ``auto_refresh_days`` is also passed, the ref's window is
        updated alongside — so ``touch(ref, auto_refresh_days=90)``
        both refreshes and extends.

        Raises :class:`NotFound` if the ref is missing or soft-deleted.
        Calling ``touch`` on a ref that has ``auto_refresh_days IS NULL``
        and you don't pass the kwarg leaves the ref durable and just
        bumps ``refreshed_at`` (harmless; effectively a no-op for
        ranking since durable refs ignore the timestamp).
        """
        if auto_refresh_days is not None:
            sql = (
                "UPDATE refs SET refreshed_at = now(), "
                "auto_refresh_days = %s, updated_at = now() "
                "WHERE ref_id = %s AND deleted_at IS NULL"
            )
            params: tuple[Any, ...] = (auto_refresh_days, ref_id)
        else:
            sql = (
                "UPDATE refs SET refreshed_at = now(), updated_at = now() "
                "WHERE ref_id = %s AND deleted_at IS NULL"
            )
            params = (ref_id,)
        if conn is not None:
            rowcount = conn.execute(sql, params).rowcount
        else:
            with self.pool.connection() as c:
                rowcount = c.execute(sql, params).rowcount
        if rowcount == 0:
            raise NotFound(f"ref id={ref_id} not found (or already deleted)")

    def stamp_ref_meta(
        self,
        ref_id: int,
        updates: dict[str, Any],
        *,
        conn: Connection | None = None,
    ) -> None:
        """Shallow-merge ``updates`` into a ref's ``meta`` JSONB.

        Used by ``supersede`` to record ``meta.superseded_by = <new_id>``
        on each retired original so provenance is queryable after the
        soft-delete. ``meta || %s`` is a top-level merge: existing keys
        are overwritten, others preserved. No-op-safe on a missing key.
        """
        sql = "UPDATE refs SET meta = meta || %s, updated_at = now() WHERE ref_id = %s"
        if conn is not None:
            conn.execute(sql, (Jsonb(updates), ref_id))
        else:
            with self.pool.connection() as c:
                c.execute(sql, (Jsonb(updates), ref_id))

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

    #: Whitelisted ``ORDER BY`` clauses for :meth:`list_refs`. Keys are
    #: the safe identifiers a caller (e.g. the web Refs tab) may pass as
    #: ``order_by``; values are the literal SQL (column already aliased
    #: ``r``). Never interpolate a caller string into the ORDER BY — only
    #: values from this map reach the query.
    _LIST_ORDER_BY: ClassVar[dict[str, str]] = {
        "updated_desc": "r.updated_at DESC",
        "updated_asc": "r.updated_at ASC",
        "created_desc": "r.created_at DESC",
        "created_asc": "r.created_at ASC",
        "title_asc": "r.title ASC",
        "title_desc": "r.title DESC",
        "id_desc": "r.ref_id DESC",
        "id_asc": "r.ref_id ASC",
    }

    def list_refs(
        self,
        *,
        kind: str | None = None,
        provider: str | None = None,
        updated_after: datetime | None = None,
        tags: list[str] | None = None,
        has_pdf: bool | None = None,
        has_chunks: bool | None = None,
        order_by: str = "updated_desc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[Ref]:
        """Paginated list of live refs, filter by kind/provider/tags.

        ``order_by`` is one of :attr:`_LIST_ORDER_BY`'s keys
        (default ``updated_desc``); an unknown key falls back to
        ``updated_desc`` rather than erroring, so a stale client
        bookmark can't 500 the list.

        ``has_pdf`` / ``has_chunks`` are tri-state presence filters
        (``None`` = don't filter). ``has_pdf`` keys off
        ``refs.pdf_sha256``; ``has_chunks`` off the existence of any
        body chunk (``ord >= 0``). Both back the Papers tab's "only
        ingested / only with PDF" toggles.
        """
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
        if has_pdf is not None:
            clauses.append(
                "r.pdf_sha256 IS NOT NULL" if has_pdf else "r.pdf_sha256 IS NULL"
            )
        if has_chunks is not None:
            # Correlated EXISTS on the body-chunk index — cheap, and it
            # keeps the filter on the SQL side so pagination stays honest.
            exists = (
                "EXISTS (SELECT 1 FROM chunks c "
                "WHERE c.ref_id = r.ref_id AND c.ord >= 0)"
            )
            clauses.append(exists if has_chunks else f"NOT {exists}")

        tag_frag, tag_params = build_tag_filter(tags, ref_alias="r")
        if tag_frag:
            clauses.append(tag_frag)
            params.extend(tag_params)

        order_sql = self._LIST_ORDER_BY.get(
            order_by, self._LIST_ORDER_BY["updated_desc"]
        )
        params.append(limit)
        params.append(offset)
        sql = (
            f"SELECT {_REFS_COLS_ALIASED} FROM refs r WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY {order_sql} LIMIT %s OFFSET %s"
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
            clauses.append(tag_frag)
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
            clauses.append(tag_frag)
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
        # rows are tuples in column order; rank is the trailing column
        # added after the ref projection. Use the named constant so this
        # tracks ``_REFS_COLS_LEN`` automatically as ref columns evolve.
        result: list[tuple[Ref, float]] = []
        for r in rows:
            ref = _row_to_ref(r[:_REFS_COLS_LEN])
            result.append((ref, float(r[_REFS_COLS_LEN])))
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
            clauses.append(tag_frag)
            params.extend(tag_params)
        sql = "SELECT count(*) FROM refs r WHERE " + " AND ".join(clauses)
        with self.pool.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        assert row is not None
        return int(row[0])


def _stub_state_summary(last_event: str | None, last_ts: Any) -> str:
    """One-line state per stub for operator / agent triage.

    Shared by :meth:`RefsMixin.stub_backlog` so the CLI
    (``precis stubs``) and the MCP ``search(view='stubs')`` render the
    same human-readable status string.
    """
    if last_event is None:
        return "awaiting fetch (never tried)"
    if last_event == "fetch_ok":
        # File on disk, watcher hasn't ingested yet; the row leaves the
        # backlog as soon as precis_add runs. Flag the in-between state.
        return "PDF downloaded; awaiting watcher ingest"
    if last_event == "no_oa_version":
        return "no OA version available"
    if last_event in ("fetch_failed", "api_error"):
        return f"{last_event} — will retry in 24h"
    if last_event == "rate_limited":
        return "rate-limited — backed off"
    if last_event == "invalid_identifier":
        return "identifier rejected — operator review"
    return last_event


__all__ = ["RefsMixin"]

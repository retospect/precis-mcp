"""Link CRUD against the v2 ``links`` table. Mixin on
:class:`precis.store.Store`.

v2 schema notes:

- ``links.id`` → ``links.link_id`` (column renamed; aliased back
  to ``id`` in SELECT so the dataclass shape stays stable)
- ``links.src_pos`` / ``dst_pos`` (int, with v1 ``-1`` sentinel for
  "ref-level") → ``src_chunk_id`` / ``dst_chunk_id`` (NULL for
  ref-level; FK to ``chunks(chunk_id)``)
- UNIQUE ``(src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id,
  relation) NULLS NOT DISTINCT`` — NULLS NOT DISTINCT preserves
  the v1 dedup invariant for ref-level edges (two NULL chunk_ids
  collide as duplicates the way v1's two -1 sentinels did).

API-side ``pos`` (the chunk's ord) is the agent-facing convention;
this module translates pos↔chunk_id at the boundary:

- On INSERT: ``pos!=None`` triggers ``SELECT chunk_id FROM chunks
  WHERE ref_id = %s AND ord = %s`` lookup (raises ``BadInput`` on
  missing chunk — caller's contract is "the chunk exists").
- On SELECT: LEFT JOIN ``chunks`` twice (one per endpoint) and
  project ``ord`` back as ``pos``. NULL chunk_id → NULL ord →
  None ``pos`` directly; no sentinel translation.

Inverse-relation rewrite at read time
(``relation='cited-by'`` → match ``cites`` rows with the ref on
the dst side) carries over unchanged from v1.

Mixin assumes the concrete Store provides ``self.pool``.
"""

from __future__ import annotations

from typing import Any, Literal

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput
from precis.store._argument_ops import retracted_endpoint
from precis.store._mappers import (
    _lookup_chunk_id,
    _row_to_bib_entry,
    _row_to_link,
    _row_to_s2_neighbor,
)
from precis.store.types import (
    ActorSlug,
    BibEntry,
    Link,
    Relation,
    S2Direction,
    S2Neighbor,
)


def _resolve_chunk_id_for_link(
    conn: Connection, ref_id: int, ord_: int | None
) -> int | None:
    """Translate ``pos`` (= ``chunks.ord``) → ``chunks.chunk_id``.

    Returns ``None`` when ``ord_`` is ``None`` (ref-level link).
    Raises ``BadInput`` when ``ord_`` is set but no chunk exists at
    ``(ref_id, ord)`` — silently inserting NULL there would dedupe
    against the wrong row under the NULLS NOT DISTINCT unique
    constraint and corrupt the link graph.
    """
    if ord_ is None:
        return None
    chunk_id = _lookup_chunk_id(conn, ref_id, ord_)
    if chunk_id is None:
        raise BadInput(
            f"no chunk at (ref_id={ref_id}, ord={ord_}) — "
            "can't link to a chunk that doesn't exist",
            next=f"check chunks: get(kind=..., id={ref_id})",
        )
    return chunk_id


# Standard SELECT projection for links: maps link_id back to id and
# resolves chunk_id endpoints back to ord via LEFT JOIN against
# chunks. Mirrors :func:`_row_to_link`'s tuple layout.
_LINK_SELECT_PROJ = (
    "l.link_id AS id, "
    "l.src_ref_id, "
    "sc.ord AS src_pos, "
    "l.dst_ref_id, "
    "dc.ord AS dst_pos, "
    "l.relation, "
    "l.set_by, "
    "l.meta, "
    "l.created_at, "
    "l.src_chunk_id, "
    "l.dst_chunk_id"
)
_LINK_SELECT_FROM = (
    "FROM links l "
    "LEFT JOIN chunks sc ON sc.chunk_id = l.src_chunk_id "
    "LEFT JOIN chunks dc ON dc.chunk_id = l.dst_chunk_id"
)


class LinksMixin:
    """v2 link insert / remove / read with inverse-relation rewrite."""

    pool: ConnectionPool
    soft_delete_ref: Any  # provided by RefsMixin (used by merge_refs)
    # Provided by ArgumentGraphMixin. Called after a retraction / concern
    # edge is added or removed so the argument-graph STALE: flag stays a
    # pure function of current graph reachability.
    argument_ripple_retraction: Any

    def valid_relations(self, *, refresh: bool = False) -> frozenset[str]:
        """All relation slugs registered in the ``relations`` table.

        ``links.relation`` has an FK to ``relations(slug)``, so this
        table is the authoritative link vocabulary: a plugin kind seeds
        its own relations here in its migration (as it must for the FK).
        The handler-layer pre-flight check
        (:func:`precis.handlers._link_tag_ops.validate_relation`)
        consults this set so a plugin relation is accepted without a
        core edit to the ``Relation`` literal — the literal stays the
        static typo-safety hint for the built-ins.

        Cached for the process lifetime: the vocabulary is static once
        migrations have run. ``refresh=True`` re-reads — used on a
        validation *miss* so a relation registered mid-process (a test,
        or a plugin migrated after the store opened) is still picked up
        before the caller rejects. The cache lives in ``__dict__`` (via
        ``getattr``) rather than a class annotation so a dataclass
        ``Store`` doesn't turn it into a field.
        """
        cached = getattr(self, "_valid_relations_cache", None)
        if cached is None or refresh:
            with self.pool.connection() as c:
                rows = c.execute("SELECT slug FROM relations").fetchall()
            cached = frozenset(str(r[0]) for r in rows)
            self._valid_relations_cache = cached
        return cached

    def inverse_relation(
        self, relation: str | None, *, refresh: bool = False
    ) -> str | None:
        """The inverse slug of ``relation`` from the ``relations`` table, or None.

        The authoritative source (gripe 160213): the ``relations.inverse_slug``
        column, seeded per-relation in each migration — **including plugin
        relations**, which the old static ``_INVERSE_RELATIONS`` dict in
        ``store/types.py`` did not know, so an asymmetric plugin relation never
        auto-mirrored on the :meth:`links_for` read filter. Cached for the
        process lifetime exactly like :meth:`valid_relations` (same ``__dict__``
        pattern, first-call load, ``refresh=True`` to re-read); the vocabulary
        is static once migrations have run, and plugin migrations run at startup
        before any link read. Every relation is a key (value ``None`` when it has
        no inverse), so an inverse-less relation never re-queries.
        """
        if relation is None:
            return None
        cached = getattr(self, "_inverse_relations_cache", None)
        if cached is None or refresh:
            with self.pool.connection() as c:
                rows = c.execute("SELECT slug, inverse_slug FROM relations").fetchall()
            cached = {
                str(r[0]): (str(r[1]) if r[1] is not None else None) for r in rows
            }
            self._inverse_relations_cache = cached
        return cached.get(relation)

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
        merge_meta: bool = False,
        conn: Connection | None = None,
    ) -> Link:
        """Insert a link row, idempotent on the unique tuple.

        v2 ``UNIQUE (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id,
        relation) NULLS NOT DISTINCT`` preserves the v1 dedup invariant:
        re-inserting the same edge is a no-op via
        ``ON CONFLICT ... DO UPDATE SET set_by = links.set_by``, the
        same no-op-update trick used elsewhere so RETURNING fires on
        both insert and conflict paths.

        ``merge_meta=True`` additionally merges ``meta`` into the
        existing row's on a conflict (``links.meta || EXCLUDED.meta``)
        instead of leaving it untouched. **Opt-in** — default ``False``
        keeps today's behaviour byte-identical for every existing
        caller; flipping it globally would silently change conflict
        semantics for every other link-writing call site (cast_common,
        paper.acquire_reason, inbound_chase, draft, numeric_ref, …) from
        sticky-first-write to latest-wins-merge, which is out of scope
        for any one feature. A structure design's paper-provenance
        rationale note (gr161577) passes ``merge_meta=True`` so
        re-linking the same edge updates the note.

        Self-loop CHECK: same ref + same chunk endpoint (both NULL or
        both same chunk_id) is rejected by the schema; app-layer
        ``BadInput`` here so an agent mistake surfaces with a recovery
        hint rather than a psycopg ``CheckViolation``.

        One row per edge — asymmetric pairs (``cites`` / ``cited-by``)
        are NOT auto-mirrored. The "who cites me?" filter is handled
        at read time in :meth:`links_for`.
        """

        def _do(c: Connection) -> Link:
            src_chunk_id = _resolve_chunk_id_for_link(c, src_ref_id, src_pos)
            dst_chunk_id = _resolve_chunk_id_for_link(c, dst_ref_id, dst_pos)

            # Self-loop check: same ref + same chunk endpoint (both
            # NULL ⇒ ref-level self-loop; both same chunk_id ⇒
            # chunk-level self-loop).
            if src_ref_id == dst_ref_id and src_chunk_id == dst_chunk_id:
                raise BadInput(
                    "cannot link a ref to itself at the same position",
                    next=(
                        "use different src_pos/dst_pos if linking chunks "
                        "within one ref, or pick a different target"
                    ),
                )

            # ``meta_clause`` is a fixed literal (never interpolates caller
            # data), toggled only by the ``merge_meta`` opt-in above.
            meta_clause = ", meta = links.meta || EXCLUDED.meta" if merge_meta else ""
            sql = (
                "INSERT INTO links "
                "  (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, "
                "   relation, set_by, meta) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT "
                "  (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation) "
                f"DO UPDATE SET set_by = links.set_by{meta_clause} "
                "RETURNING link_id"
            )
            row = c.execute(
                sql,
                (
                    src_ref_id,
                    src_chunk_id,
                    dst_ref_id,
                    dst_chunk_id,
                    relation,
                    set_by,
                    Jsonb(meta or {}),
                ),
            ).fetchone()
            assert row is not None, (
                "links INSERT returned no row — schema invariant violated"
            )
            link_id = int(row[0])

            # Re-SELECT through the standard projection so the
            # returned Link carries the LEFT-JOIN-translated pos
            # fields (ord values, not chunk_id ints).
            fetched = c.execute(
                f"SELECT {_LINK_SELECT_PROJ} {_LINK_SELECT_FROM} WHERE l.link_id = %s",
                (link_id,),
            ).fetchone()
            assert fetched is not None

            # Argument-graph retraction push hook (build order
            # step 4) — a link-write hook, not a background sweep. Runs
            # inside the same connection/transaction as the INSERT above so
            # the STALE: recompute is atomic with the edge that triggered
            # it. Cheap no-op for the overwhelming majority of links
            # (anything but the 4 retraction/concern relation forms).
            distrusted = retracted_endpoint(relation, src_ref_id, dst_ref_id)
            if distrusted is not None:
                self.argument_ripple_retraction(c, distrusted)

            return _row_to_link(fetched)

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

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
        """Remove links matching ``(src, dst, [chunk pair, [relation]])``.

        ``relation=None`` removes **all** links between the given
        endpoints regardless of relation. Returns the number of
        rows deleted; missing links are a silent no-op.

        Uses ``IS NOT DISTINCT FROM`` for the chunk_id predicates so
        ``None`` ↔ NULL matching aligns with the UNIQUE NULLS NOT
        DISTINCT semantics on the index.
        """

        def _do(c: Connection) -> int:
            src_chunk_id = _resolve_chunk_id_for_link(c, src_ref_id, src_pos)
            dst_chunk_id = _resolve_chunk_id_for_link(c, dst_ref_id, dst_pos)
            clauses = [
                "src_ref_id = %s",
                "src_chunk_id IS NOT DISTINCT FROM %s",
                "dst_ref_id = %s",
                "dst_chunk_id IS NOT DISTINCT FROM %s",
            ]
            params: list[Any] = [
                src_ref_id,
                src_chunk_id,
                dst_ref_id,
                dst_chunk_id,
            ]
            if relation is not None:
                clauses.append("relation = %s")
                params.append(relation)
            sql = f"DELETE FROM links WHERE {' AND '.join(clauses)}"
            cur = c.execute(sql, params)
            n = cur.rowcount or 0

            # Argument-graph retraction push hook, remove side (the argument graph
            # §5/R5: "every retraction-edge add *or* remove reruns the
            # bounded walk"). Only fires when the caller named the exact
            # relation removed (the common ``unlink(rel=...)`` shape) — a
            # wildcard ``relation=None`` removal doesn't tell us which
            # relation(s) it deleted, so it's out of scope for the hook.
            if n and relation is not None:
                distrusted = retracted_endpoint(relation, src_ref_id, dst_ref_id)
                if distrusted is not None:
                    self.argument_ripple_retraction(c, distrusted)

            return n

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def links_for(
        self,
        ref_id: int,
        *,
        direction: Literal["out", "in", "both"] = "both",
        relation: Relation | None = None,
    ) -> list[Link]:
        """Fetch links touching ``ref_id``.

        Inverse-relation rewrite carries over unchanged from v1:
        ``relation='cited-by'`` matches literal ``cited-by`` rows
        on this ref's side OR ``cites`` rows on the opposite side
        (the v2 schema doesn't store ``cited-by`` rows — only
        ``cites`` — so the rewrite is the only way to surface
        "who cites me?" links).
        """
        # Inverse from the DB `relations.inverse_slug` (gripe 160213), so a
        # plugin relation's inverse mirrors on read too — not just the built-ins
        # the old static `_INVERSE_RELATIONS` dict knew.
        inverse = self.inverse_relation(relation)

        def _direction_clause(direction_: str) -> tuple[str, list[Any]]:
            if direction_ == "out":
                return "l.src_ref_id = %s", [ref_id]
            if direction_ == "in":
                return "l.dst_ref_id = %s", [ref_id]
            return (
                "(l.src_ref_id = %s OR l.dst_ref_id = %s)",
                [ref_id, ref_id],
            )

        clauses: list[str] = []
        params: list[Any] = []

        if inverse is None:
            d_clause, d_params = _direction_clause(direction)
            clauses.append(d_clause)
            params.extend(d_params)
            if relation is not None:
                clauses.append("l.relation = %s")
                params.append(relation)
        else:
            opposite_dir = {"out": "in", "in": "out", "both": "both"}[direction]
            d_left, p_left = _direction_clause(direction)
            d_right, p_right = _direction_clause(opposite_dir)
            clauses.append(
                f"(({d_left} AND l.relation = %s) OR ({d_right} AND l.relation = %s))"
            )
            params.extend([*p_left, relation, *p_right, inverse])

        sql = (
            f"SELECT {_LINK_SELECT_PROJ} {_LINK_SELECT_FROM} "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY l.created_at ASC"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        # Dedupe by link_id — under inverse rewrite + direction='both'
        # a row could match both halves of the OR; cheap defensive
        # dedupe.
        seen: set[int] = set()
        out: list[Link] = []
        for r in rows:
            link_id = r[0]
            if link_id in seen:
                continue
            seen.add(link_id)
            out.append(_row_to_link(r))
        return out

    def count_links_for_refs(self, ref_ids: list[int]) -> dict[int, int]:
        """Return ``{ref_id: total_link_count}`` for a batch of refs.

        Total = incoming + outgoing edges, undeduped at the link_id
        level (a link with the same src and dst would count twice —
        not a real case in the schema). Designed for the list-view
        TOON column so a single SQL round-trip covers a page.
        Missing ref ids in the result dict mean zero links.
        """
        if not ref_ids:
            return {}
        sql = (
            "SELECT ref_id, COUNT(*)::int FROM ("
            "  SELECT src_ref_id AS ref_id FROM links "
            "    WHERE src_ref_id = ANY(%s)"
            "  UNION ALL"
            "  SELECT dst_ref_id AS ref_id FROM links "
            "    WHERE dst_ref_id = ANY(%s)"
            ") sub GROUP BY ref_id"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (ref_ids, ref_ids)).fetchall()
        return {int(r[0]): int(r[1]) for r in rows}

    def link_rel_summary_for_refs(
        self, ref_ids: list[int]
    ) -> dict[int, dict[tuple[str, str], int]]:
        """Return ``{ref_id: {(direction, relation): count}}`` for a batch.

        ``direction`` is ``'out'`` when ``ref_id`` is the link's ``src``,
        ``'in'`` when it's the ``dst`` — the raw stored ``relation``, no
        inverse-rewrite. One SQL round-trip covers a whole search page,
        same batching shape as :meth:`count_links_for_refs`. Missing ref
        ids in the result dict mean zero links.
        """
        if not ref_ids:
            return {}
        sql = (
            "SELECT ref_id, dir, relation, COUNT(*)::int FROM ("
            "  SELECT src_ref_id AS ref_id, 'out' AS dir, relation FROM links "
            "    WHERE src_ref_id = ANY(%s)"
            "  UNION ALL"
            "  SELECT dst_ref_id AS ref_id, 'in' AS dir, relation FROM links "
            "    WHERE dst_ref_id = ANY(%s)"
            ") sub GROUP BY ref_id, dir, relation"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (ref_ids, ref_ids)).fetchall()
        out: dict[int, dict[tuple[str, str], int]] = {}
        for ref_id, direction, relation, count in rows:
            out.setdefault(int(ref_id), {})[(str(direction), str(relation))] = int(
                count
            )
        return out

    def migrate_links(
        self,
        old_ref_id: int,
        new_ref_id: int,
        *,
        conn: Connection,
    ) -> int:
        """Re-point every link touching ``old_ref_id`` onto ``new_ref_id``.

        The link-migration step of a memory ``supersede`` merge: when
        an old memory is absorbed into a freshly-minted consolidated
        one, its graph position must follow so the survivor inherits
        every edge (and inbound provenance from papers etc. is
        preserved rather than orphaned by the soft-delete).

        Requires a caller-supplied ``conn`` because it is only ever run
        inside the ``supersede`` transaction (insert survivor →
        migrate links → add ``supersedes`` edge → soft-delete old), so
        the whole merge is atomic.

        Mechanics (mirrors the design doc, §Consolidate behavior):

        1. INSERT a substituted copy of every link where ``old_ref_id``
           is on either endpoint, swapping that endpoint to
           ``new_ref_id`` and keeping ``src_chunk_id`` / ``dst_chunk_id``
           (memory links are ref-level so those are NULL; a paper→memory
           link keeps the paper's chunk endpoint, which stays valid).
           ``ON CONFLICT DO NOTHING`` dedups against the
           ``NULLS NOT DISTINCT`` unique index; the ``NOT (...)`` guard
           drops rows that would become self-loops after substitution
           (the schema CHECK would otherwise raise, not conflict).
        2. DELETE the original rows touching ``old_ref_id``.

        Returns the number of old rows deleted (the migrated count;
        deduped duplicates collapse into existing survivor edges).
        """
        conn.execute(
            """
            INSERT INTO links
              (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id,
               relation, set_by, meta)
            SELECT
              CASE WHEN src_ref_id = %(old)s THEN %(new)s ELSE src_ref_id END,
              src_chunk_id,
              CASE WHEN dst_ref_id = %(old)s THEN %(new)s ELSE dst_ref_id END,
              dst_chunk_id,
              relation, set_by, meta
            FROM links
            WHERE (src_ref_id = %(old)s OR dst_ref_id = %(old)s)
              AND NOT (
                (CASE WHEN src_ref_id = %(old)s THEN %(new)s ELSE src_ref_id END)
                = (CASE WHEN dst_ref_id = %(old)s THEN %(new)s ELSE dst_ref_id END)
                AND src_chunk_id IS NOT DISTINCT FROM dst_chunk_id
              )
            ON CONFLICT
              (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation)
              DO NOTHING
            """,
            {"old": old_ref_id, "new": new_ref_id},
        )
        cur = conn.execute(
            "DELETE FROM links WHERE src_ref_id = %(old)s OR dst_ref_id = %(old)s",
            {"old": old_ref_id},
        )
        return cur.rowcount or 0

    # -- s2_neighbors (the Sources/Cited tabs' store) --------------------

    def replace_s2_neighbors(
        self,
        ref_id: int,
        direction: S2Direction,
        neighbors: list[dict[str, Any]],
        *,
        conn: Connection | None = None,
    ) -> int:
        """Replace all ``s2_neighbors`` rows for ``(ref_id, direction)`` with
        a fresh list — DELETE then INSERT, so a refresh is idempotent and
        never collides on ``s2_id`` (a neighbour list can repeat or omit
        ids). ``ord`` is assigned from list position (S2's own ordering;
        approximate bibliography order for ``direction='cites'``).

        Each dict may carry ``s2_id`` / ``doi`` / ``title`` / ``year`` /
        ``held_ref_id`` (all optional, ``None`` when absent) — resolution
        of ``held_ref_id`` against ``ref_identifiers`` is the caller's job
        (:mod:`precis.backfill.citation_lens`), this method is pure
        persistence. Returns the number of rows inserted (``len(neighbors)``).
        """

        def _do(c: Connection) -> int:
            c.execute(
                "DELETE FROM s2_neighbors WHERE ref_id = %s AND direction = %s",
                (ref_id, direction),
            )
            if not neighbors:
                return 0
            rows = [
                (
                    ref_id,
                    direction,
                    i,
                    nb.get("s2_id"),
                    nb.get("doi"),
                    nb.get("title"),
                    nb.get("year"),
                    nb.get("held_ref_id"),
                )
                for i, nb in enumerate(neighbors)
            ]
            cur = c.cursor()
            cur.executemany(
                "INSERT INTO s2_neighbors "
                "  (ref_id, direction, ord, s2_id, doi, title, year, held_ref_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )
            return len(rows)

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def list_s2_neighbors(
        self, ref_id: int, direction: S2Direction
    ) -> list[S2Neighbor]:
        """This ref's persisted S2 neighbour list for one ``direction``,
        ordered by ``ord`` (S2's own list order — approximate bibliography
        order for ``direction='cites'``). Empty list = never fetched (or
        the fetch returned nothing) — the signal the web layer uses to
        decide whether to trigger a first-view backfill fetch."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id, direction, ord, s2_id, doi, title, year, "
                "       held_ref_id, fetched_at "
                "FROM s2_neighbors WHERE ref_id = %s AND direction = %s "
                "ORDER BY ord",
                (ref_id, direction),
            ).fetchall()
        return [_row_to_s2_neighbor(r) for r in rows]

    def list_bib_entries(self, ref_id: int) -> list[BibEntry]:
        """This ref's parsed bibliography (``paper_bib_entries``, migration
        0108), ordered by ``marker`` — the ``bib_parse`` worker's output,
        read here for the Sources tab (citation-sources-tab) to join onto
        ``s2_neighbors``/held ``cites`` rows and replace the positional
        badge with the real bracket marker. Empty list = the paper hasn't
        been claimed by ``bib_parse`` yet, or it has no bibliography-shaped
        chunks — the web layer's signal to fall back to today's
        positional-index rendering unchanged."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id, marker, raw_text, authors, journal, year, "
                "       volume, first_page, doi, s2_id, held_ref_id "
                "FROM paper_bib_entries WHERE ref_id = %s ORDER BY marker",
                (ref_id,),
            ).fetchall()
        return [_row_to_bib_entry(r) for r in rows]

    def s2_neighbors_fresh(self, ref_id: int) -> bool:
        """Cheap presence check: has ``ref_id`` ever had its S2 neighbour
        list persisted (either direction)? **Not** a TTL check — the
        ``citation_edges`` ref_event (``citation_lens._is_fresh``) is the
        30-day staleness gate that decides whether to re-fetch; this is
        purely "is there anything to show yet", the signal the web layer
        needs to decide whether opening Sources/Cited should trigger an
        inline first-view backfill fetch."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM s2_neighbors WHERE ref_id = %s LIMIT 1",
                (ref_id,),
            ).fetchone()
        return row is not None

    def update_s2_neighbor_held(
        self,
        ref_id: int,
        held_ref_id: int,
        *,
        s2_id: str | None = None,
        doi: str | None = None,
        conn: Connection | None = None,
    ) -> int:
        """Stamp ``held_ref_id`` on every persisted ``s2_neighbors`` row of
        ``ref_id`` matching ``s2_id`` or ``doi`` — in **either** citation
        direction, since the same external paper is the same physical
        object whether it showed up as this paper's source or its citer.

        Called right after a single-row Fetch (``POST
        /papers/{ref_id}/fetch-ref``) mints or reuses the stub, so the row
        flips to held/queued immediately instead of waiting for the next
        ``citation_lens`` TTL refresh. No-op (returns 0, no query run) when
        neither identifier is given — the Fetch button never posts without
        at least one anyway (see ``routes/papers.py::fetch_ref``).
        """
        clauses: list[str] = []
        params: list[Any] = [held_ref_id, ref_id]
        if s2_id:
            clauses.append("s2_id = %s")
            params.append(s2_id)
        if doi:
            clauses.append("doi = %s")
            params.append(doi)
        if not clauses:
            return 0
        sql = (
            "UPDATE s2_neighbors SET held_ref_id = %s "
            f"WHERE ref_id = %s AND ({' OR '.join(clauses)})"
        )

        def _do(c: Connection) -> int:
            cur = c.execute(sql, params)
            return cur.rowcount or 0

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def merge_refs(self, victim_ref_id: int, survivor_ref_id: int) -> int:
        """Absorb ``victim_ref_id`` into ``survivor_ref_id`` and retire the victim.

        The duplicate-paper resolver's primitive (same DOI / arXiv held
        twice). In one transaction it:

        1. re-points every link touching the victim onto the survivor
           (:meth:`migrate_links`), so the survivor inherits the victim's
           graph position rather than orphaning its edges;
        2. drops the victim's ``ref_identifiers`` rows — the
           uniqueness check (``set_ref_identifier``) ignores
           ``deleted_at``, so a bare soft-delete would leave the
           duplicate's DOI / arXiv / cite_key claimed and unassignable to
           the survivor;
        3. soft-deletes the victim.

        Returns the number of migrated link rows. Raises ``BadInput`` on a
        self-merge and ``NotFound`` (from :meth:`soft_delete_ref`) if the
        victim is missing or already deleted.
        """
        if victim_ref_id == survivor_ref_id:
            raise BadInput("cannot merge a ref into itself")
        with self.pool.connection() as conn:
            migrated = self.migrate_links(victim_ref_id, survivor_ref_id, conn=conn)
            conn.execute(
                "DELETE FROM ref_identifiers WHERE ref_id = %s", (victim_ref_id,)
            )
            self.soft_delete_ref(victim_ref_id, conn=conn)
        return migrated


__all__ = ["LinksMixin"]

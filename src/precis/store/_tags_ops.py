"""Tag CRUD against the v2 unified ``tags`` + ``ref_tags`` /
``chunk_tags`` model. Mixin on :class:`precis.store.Store`.

v1 stored tags in three separate tables (``ref_closed_tags`` /
``ref_flags`` / ``ref_open_tags``); v2 collapsed them into one
canonical table ``tags(tag_id, namespace, value)`` plus the two
join tables ``ref_tags(ref_id, tag_id, set_by, created_at)`` and
``chunk_tags(chunk_id, tag_id, set_by, created_at)``.

**Namespace convention** for the v2 ``tags`` table:

- v1 ``Tag(namespace='closed', prefix='STATUS', value='done')``
  → v2 ``tags(namespace='STATUS', value='done')``  — the closed
    prefix becomes the v2 namespace, lifted straight onto the
    column. Closed-prefix vocabulary uniqueness is enforced
    upstream by ``Tag.parse_strict``.
- v1 ``Tag(namespace='flag',   value='pinned')``
  → v2 ``tags(namespace='FLAG', value='pinned')``  — sentinel
    uppercase namespace so the dispatch round-trips cleanly.
- v1 ``Tag(namespace='open',   value='topic-x')``
  → v2 ``tags(namespace='OPEN', value='topic-x')`` — same idea.

Round-trip rules (used by :meth:`tags_for` to rebuild v1 ``Tag``
dataclasses from v2 rows):

- ``'FLAG'``           → ``Tag.flag(value)``
- ``'OPEN'``           → ``Tag.open(value)``
- everything else      → ``Tag.closed(namespace, value)``

**Upsert pattern** for the ``tags`` table:

``INSERT ... ON CONFLICT (namespace, value) DO UPDATE SET
namespace = EXCLUDED.namespace RETURNING tag_id`` — the
``DO UPDATE`` (even with a no-op SET) makes RETURNING fire on both
insert and conflict paths. The simpler ``DO NOTHING RETURNING``
returns no row on conflict and needs a fragile fallback SELECT
that hides races between INSERT and SELECT. Loud-and-fixable:
any unexpected ``None`` from RETURNING is a programmer / schema
error and propagates as an assertion.

Mixin assumes the concrete Store provides ``self.pool``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

from precis.store._mappers import _lookup_chunk_id, _upsert_tag
from precis.store.types import ActorSlug, Tag


def _tag_to_namespace_value(tag: Tag) -> tuple[str, str]:
    """Project a v1 ``Tag`` dataclass to the v2 ``(namespace, value)`` pair.

    Closed-prefix tags lift their prefix to the namespace column;
    flag and open tags get the ``'FLAG'`` / ``'OPEN'`` sentinels so
    the back-mapping in :func:`_row_to_tag` round-trips.
    """
    if tag.namespace == "closed":
        assert tag.prefix is not None
        return (tag.prefix, tag.value)
    if tag.namespace == "flag":
        return ("FLAG", tag.value)
    return ("OPEN", tag.value)


def _row_to_tag(namespace: str, value: str) -> Tag:
    """Inverse of :func:`_tag_to_namespace_value`. Used by
    :meth:`TagsMixin.tags_for` and tag-bearing read paths to rebuild
    the v1 ``Tag`` dataclass from a v2 ``tags`` row."""
    if namespace == "FLAG":
        return Tag.flag(value)
    if namespace == "OPEN":
        return Tag.open(value)
    return Tag.closed(namespace, value)


def _resolve_chunk_id(conn: Connection, ref_id: int, ord_: int) -> int:
    """Look up ``chunks.chunk_id`` for the ``(ref_id, ord)`` pair.

    Used by tag operations that take a v1-style ``pos`` (which is
    the chunk's ord under v2) and need the v2 ``chunk_id`` FK target
    for the join-table insert.

    Raises ``ValueError`` when no matching chunk exists. Callers
    that want to silently no-op a tag op against a missing chunk
    should test for the chunk's existence first; this helper
    refuses to make up a chunk_id.
    """
    chunk_id = _lookup_chunk_id(conn, ref_id, ord_)
    if chunk_id is None:
        raise ValueError(
            f"no chunk at (ref_id={ref_id}, ord={ord_}) — "
            "can't attach a tag to a chunk that doesn't exist"
        )
    return chunk_id


class TagsMixin:
    """v2 tag CRUD against the unified tags + ref_tags / chunk_tags model."""

    pool: ConnectionPool

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
    ) -> None:
        """Add a tag to a ref (``pos=None``) or to a chunk (``pos=ord``).

        When ``replace_prefix=True`` and the tag is closed-prefix-shaped
        (namespace != FLAG/OPEN), every existing tag in the same
        namespace on the same target is removed first. Maintains the
        v1 invariant that a closed prefix has at most one value per
        target (e.g. exactly one ``STATUS:...`` on a memory).

        ``expires_at`` (ref-level only — migration 0010) sets an optional
        TTL on the tag row. ``None`` means no expiry (the prior default).
        Query-time filter (``WHERE expires_at IS NULL OR expires_at >
        now()``) excludes expired tags from search; the row stays for
        audit + revival. **Re-tagging refreshes the expiry** by
        ``ON CONFLICT DO UPDATE`` — so an agent calling ``tag(...,
        ttl_days=30)`` on a memory that already has the tag bumps the
        TTL back to 30 days from now. (Without UPDATE the existing row
        would lock the original expiry in place.) Setting
        ``expires_at=None`` on an existing row clears any prior TTL.

        Chunk-level expiry is not supported in v1; pass ``pos=None`` for
        any TTL'd tag. The schema column lives only on ``ref_tags`` for
        now; promote when a chunk-level use case appears.
        """
        namespace, value = _tag_to_namespace_value(tag)

        def _do(c: Connection) -> None:
            if pos is None:
                # Ref-level tag.
                if replace_prefix and namespace not in ("FLAG", "OPEN"):
                    c.execute(
                        "DELETE FROM ref_tags rt "
                        "USING tags t "
                        "WHERE rt.tag_id = t.tag_id "
                        "  AND rt.ref_id = %s "
                        "  AND t.namespace = %s",
                        (ref_id, namespace),
                    )
                tag_id = _upsert_tag(c, namespace, value)
                c.execute(
                    "INSERT INTO ref_tags (ref_id, tag_id, set_by, expires_at) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (ref_id, tag_id) DO UPDATE "
                    "SET set_by = EXCLUDED.set_by, "
                    "    expires_at = EXCLUDED.expires_at",
                    (ref_id, tag_id, set_by, expires_at),
                )
                return

            # Chunk-level tag.
            if expires_at is not None:
                raise ValueError(
                    "expires_at is supported only on ref-level tags (pos=None)"
                )
            chunk_id = _resolve_chunk_id(c, ref_id, pos)
            if replace_prefix and namespace not in ("FLAG", "OPEN"):
                c.execute(
                    "DELETE FROM chunk_tags ct "
                    "USING tags t "
                    "WHERE ct.tag_id = t.tag_id "
                    "  AND ct.chunk_id = %s "
                    "  AND t.namespace = %s",
                    (chunk_id, namespace),
                )
            tag_id = _upsert_tag(c, namespace, value)
            c.execute(
                "INSERT INTO chunk_tags (chunk_id, tag_id, set_by) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (chunk_id, tag_id) DO NOTHING",
                (chunk_id, tag_id, set_by),
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
        """Remove a tag from a ref / chunk. Idempotent (no-op on miss)."""
        namespace, value = _tag_to_namespace_value(tag)

        def _do(c: Connection) -> None:
            if pos is None:
                c.execute(
                    "DELETE FROM ref_tags rt "
                    "USING tags t "
                    "WHERE rt.tag_id = t.tag_id "
                    "  AND rt.ref_id = %s "
                    "  AND t.namespace = %s "
                    "  AND t.value = %s",
                    (ref_id, namespace, value),
                )
                return
            chunk_id = _resolve_chunk_id(c, ref_id, pos)
            c.execute(
                "DELETE FROM chunk_tags ct "
                "USING tags t "
                "WHERE ct.tag_id = t.tag_id "
                "  AND ct.chunk_id = %s "
                "  AND t.namespace = %s "
                "  AND t.value = %s",
                (chunk_id, namespace, value),
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
        """Return every tag on a ref (``pos=None``) or chunk (``pos=ord``).

        Returns a flat ``list[Tag]`` with namespace round-tripped via
        :func:`_row_to_tag`. Order is insertion order on the join
        table (``created_at ASC, tag_id ASC``) so repeated calls
        return tags in a stable shape suitable for diffing.
        """
        with self.pool.connection() as conn:
            if pos is None:
                rows = conn.execute(
                    "SELECT t.namespace, t.value "
                    "FROM ref_tags rt "
                    "JOIN tags t ON t.tag_id = rt.tag_id "
                    "WHERE rt.ref_id = %s "
                    "ORDER BY rt.created_at ASC, t.tag_id ASC",
                    (ref_id,),
                ).fetchall()
            else:
                chunk_id = _resolve_chunk_id(conn, ref_id, pos)
                rows = conn.execute(
                    "SELECT t.namespace, t.value "
                    "FROM chunk_tags ct "
                    "JOIN tags t ON t.tag_id = ct.tag_id "
                    "WHERE ct.chunk_id = %s "
                    "ORDER BY ct.created_at ASC, t.tag_id ASC",
                    (chunk_id,),
                ).fetchall()
        return [_row_to_tag(str(r[0]), str(r[1])) for r in rows]

    def has_tag(self, ref_id: int, namespace: str, value: str) -> bool:
        """Fast ref-level ``(namespace, value)`` presence probe.

        Replaces v1 ``has_flag``: every flag check now passes the
        ``'FLAG'`` sentinel namespace explicitly. Closed-prefix
        probes pass their prefix directly (e.g. ``has_tag(ref_id,
        'STATUS', 'done')``); open tag probes use ``'OPEN'``.

        Expired tags (``expires_at <= now()`` per migration 0010) do
        not count as present. The row stays in the table for audit;
        ``has_tag`` is the runtime probe that mirrors what the agent
        actually "sees."
        """
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM ref_tags rt "
                "JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = %s "
                "  AND t.namespace = %s "
                "  AND t.value = %s "
                "  AND (rt.expires_at IS NULL OR rt.expires_at > now()) "
                "LIMIT 1",
                (ref_id, namespace, value),
            ).fetchone()
        return row is not None

    def ref_tag_values(
        self,
        ref_ids: list[int],
        namespace: str,
        values: list[str],
    ) -> dict[int, set[str]]:
        """Batched ``(namespace, value)`` presence over many refs.

        One query for a whole page of rows — the N+1 avoidance for a
        list view that renders per-ref flag chips (e.g. the
        ``read-later`` / ``must-read`` / ``skim`` toggles). Returns
        ``{ref_id: {value, …}}`` containing only the refs that carry at
        least one of ``values`` under ``namespace``; a ref with none is
        simply absent from the map. Expired tags are excluded, matching
        :meth:`has_tag`.
        """
        if not ref_ids or not values:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT rt.ref_id, t.value "
                "FROM ref_tags rt "
                "JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = ANY(%s) "
                "  AND t.namespace = %s "
                "  AND t.value = ANY(%s) "
                "  AND (rt.expires_at IS NULL OR rt.expires_at > now())",
                (list(ref_ids), namespace, list(values)),
            ).fetchall()
        out: dict[int, set[str]] = {}
        for ref_id, value in rows:
            out.setdefault(int(ref_id), set()).add(str(value))
        return out

    def tags_for_with_expiry(
        self,
        ref_id: int,
    ) -> list[tuple[Tag, datetime | None]]:
        """Return every ref-level tag paired with its ``expires_at``.

        Includes expired tags (so callers can render "this tag
        expired 3d ago"). For the runtime "what tags does this ref
        carry right now" use :meth:`has_tag` or
        :meth:`tags_for`. For the preamble builder's sticky-memory
        rendering with "expires in Nd" / "⚠️ Nd left" markers, use
        this. asa_bot reads it via the precis MCP and renders the
        countdown.

        Ordered by insertion (``created_at ASC``).
        """
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT t.namespace, t.value, rt.expires_at "
                "FROM ref_tags rt "
                "JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = %s "
                "ORDER BY rt.created_at ASC, t.tag_id ASC",
                (ref_id,),
            ).fetchall()
        return [(_row_to_tag(str(r[0]), str(r[1])), r[2]) for r in rows]

    # ── discovery surface (kind='tag') ──────────────────────────────
    #
    # These methods back the tag-discovery handler and the
    # tag_embeddings worker. Read-only against the live tag corpus —
    # never mutate tags here; tag writes still flow through
    # :meth:`add_tag` / :meth:`remove_tag` above.

    def list_all_tags(
        self,
        *,
        kind: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> list[tuple[str, str, int]]:
        """Return tags ordered by usage count desc for the discovery view.

        Yields ``(namespace, value, usage_count)`` rows. When ``kind``
        is set, only counts tags attached to refs of that kind (joins
        through ``ref_tags`` and ``refs``). Otherwise counts every
        attachment across the whole corpus (ref + chunk attachments
        unioned, deduped per (tag, ref/chunk)).

        Pagination via ``page`` / ``page_size`` — the canonical
        verb-surface shape; ``page=1`` is the first page.
        """
        if page < 1:
            page = 1
        offset = (page - 1) * page_size
        with self.pool.connection() as conn:
            if kind is None:
                # Sum ref-level and chunk-level attachments — every tag
                # row that has at least one join from either table.
                rows = conn.execute(
                    "SELECT t.namespace, t.value, "
                    "       COALESCE(rc.n, 0) + COALESCE(cc.n, 0) AS usage_count "
                    "FROM tags t "
                    "LEFT JOIN (SELECT tag_id, COUNT(*) AS n FROM ref_tags "
                    "           GROUP BY tag_id) rc ON rc.tag_id = t.tag_id "
                    "LEFT JOIN (SELECT tag_id, COUNT(*) AS n FROM chunk_tags "
                    "           GROUP BY tag_id) cc ON cc.tag_id = t.tag_id "
                    "WHERE COALESCE(rc.n, 0) + COALESCE(cc.n, 0) > 0 "
                    "ORDER BY usage_count DESC, t.namespace ASC, t.value ASC "
                    "LIMIT %s OFFSET %s",
                    (page_size, offset),
                ).fetchall()
            else:
                # Scope to refs of the given kind. Chunk-level
                # attachments inherit the parent ref's kind.
                rows = conn.execute(
                    "SELECT t.namespace, t.value, COUNT(*) AS usage_count "
                    "FROM tags t "
                    "JOIN ref_tags rt ON rt.tag_id = t.tag_id "
                    "JOIN refs r ON r.ref_id = rt.ref_id "
                    "WHERE r.kind = %s AND r.deleted_at IS NULL "
                    "GROUP BY t.namespace, t.value "
                    "ORDER BY usage_count DESC, t.namespace ASC, t.value ASC "
                    "LIMIT %s OFFSET %s",
                    (kind, page_size, offset),
                ).fetchall()
        return [(str(r[0]), str(r[1]), int(r[2])) for r in rows]

    def tag_metadata(
        self,
        *,
        namespace: str,
        value: str,
    ) -> dict[str, Any] | None:
        """Return metadata for the ``(namespace, value)`` tag.

        Output shape::

            {"count": int,         # cumulative incl. soft-deleted refs
             "live_count": int,    # only attachments to live refs
             "first_seen": datetime,
             "last_seen": datetime,
             "sample_refs": [(kind, slug, ref_id), ...]}

        ``sample_refs`` is up to five most-recently-attached live
        refs. Returns ``None`` when the tag has no row in ``tags``
        (never used). When the row exists but has no attachments
        (orphan row left after every ref untagged), returns count 0
        with the tag-row created_at timestamp for first/last and an
        empty sample list.

        ``count`` vs ``live_count``: the historical ``count`` includes
        attachments to soft-deleted refs (audit retention). The
        live_count column added per broad-pass finding #11 answers
        "how many live refs carry this tag right now?" — usually the
        more useful number for an agent considering tag fragmentation.
        """
        with self.pool.connection() as conn:
            tag_row = conn.execute(
                "SELECT tag_id, created_at FROM tags "
                "WHERE namespace = %s AND value = %s",
                (namespace, value),
            ).fetchone()
            if tag_row is None:
                return None
            tag_id = int(tag_row[0])
            tag_created_at = tag_row[1]

            # Aggregate ref-level + chunk-level attachments. We need
            # the total count (for the headline number) and the
            # min/max created_at (for first/last seen). UNION ALL
            # keeps duplicates out of the per-target groups but
            # double-counts a (ref, tag) pair that's also attached to
            # one of the ref's chunks — acceptable for a usage_count
            # hint. Join refs to compute live_count via a FILTER.
            agg = conn.execute(
                "WITH all_attachments AS ( "
                "  SELECT rt.created_at, r.deleted_at "
                "    FROM ref_tags rt "
                "    JOIN refs r ON r.ref_id = rt.ref_id "
                "    WHERE rt.tag_id = %s "
                "  UNION ALL "
                "  SELECT ct.created_at, r.deleted_at "
                "    FROM chunk_tags ct "
                "    JOIN chunks c ON c.chunk_id = ct.chunk_id "
                "    JOIN refs r ON r.ref_id = c.ref_id "
                "    WHERE ct.tag_id = %s "
                ") "
                "SELECT COUNT(*), "
                "       COUNT(*) FILTER (WHERE deleted_at IS NULL), "
                "       MIN(created_at), MAX(created_at) "
                "FROM all_attachments",
                (tag_id, tag_id),
            ).fetchone()
            assert agg is not None
            count = int(agg[0])
            live_count = int(agg[1])
            first_seen = agg[2] or tag_created_at
            last_seen = agg[3] or tag_created_at

            # Up to five live refs carrying this tag, most recent
            # attachment first. Joins via ref_tags (chunk-only
            # attachments don't surface as sample refs — chunk
            # selectors are noise here).
            sample_rows = conn.execute(
                "SELECT r.kind, "
                "       (SELECT id_value FROM ref_identifiers "
                "          WHERE ref_id = r.ref_id "
                "            AND id_kind = 'cite_key'"
                "          ORDER BY created_at DESC LIMIT 1) AS slug, "
                "       r.ref_id "
                "FROM ref_tags rt "
                "JOIN refs r ON r.ref_id = rt.ref_id "
                "WHERE rt.tag_id = %s AND r.deleted_at IS NULL "
                "ORDER BY rt.created_at DESC, r.ref_id DESC "
                "LIMIT 5",
                (tag_id,),
            ).fetchall()
        sample_refs: list[tuple[str, str | None, int]] = [
            (str(r[0]), (None if r[1] is None else str(r[1])), int(r[2]))
            for r in sample_rows
        ]
        return {
            "count": count,
            "live_count": live_count,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "sample_refs": sample_refs,
        }

    def unembedded_tags(
        self,
        *,
        limit: int,
        version: int = 1,
    ) -> list[tuple[str, str]]:
        """Claim query for the ``tag_embeddings`` worker.

        Returns up to ``limit`` ``(namespace, value)`` pairs that
        lack a fresh embedding — either no ``tag_embeddings`` row
        at all, or a row whose ``version`` is below the worker's
        current version constant. Locks the ``tags`` rows
        ``FOR UPDATE SKIP LOCKED`` so concurrent workers don't
        double-process the same tag.

        Caller is responsible for either calling
        :meth:`write_tag_embedding` (which clears the claim by
        upserting at ``version``) inside the same transaction, or
        committing the lock release explicitly. The worker pass
        wraps the loop in one connection and commits per-batch, so
        the lock lifetime tracks the batch.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT t.namespace, t.value "
                "FROM tags t "
                "LEFT JOIN tag_embeddings te "
                "  ON te.namespace = t.namespace "
                " AND te.value = t.value "
                "WHERE te.namespace IS NULL "
                "   OR te.version < %s "
                "ORDER BY t.tag_id "
                "LIMIT %s "
                "FOR UPDATE OF t SKIP LOCKED",
                (version, limit),
            ).fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]

    def write_tag_embedding(
        self,
        *,
        namespace: str,
        value: str,
        vector: list[float],
        embedder: str,
        version: int,
    ) -> None:
        """Upsert one row into ``tag_embeddings``.

        Bumps ``embedded_at`` on every write so operators can see
        when a tag was last (re)embedded. Idempotent on the
        ``(namespace, value)`` primary key — re-running the worker
        against the same tags is a no-op modulo the timestamp.
        """
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO tag_embeddings "
                "  (namespace, value, vector, version, embedder, embedded_at) "
                "VALUES (%s, %s, %s, %s, %s, NOW()) "
                "ON CONFLICT (namespace, value) DO UPDATE SET "
                "  vector = EXCLUDED.vector, "
                "  version = EXCLUDED.version, "
                "  embedder = EXCLUDED.embedder, "
                "  embedded_at = NOW()",
                (namespace, value, vector, version, embedder),
            )

    def search_tags_semantic(
        self,
        *,
        query_vector: list[float],
        page: int = 1,
        page_size: int = 20,
    ) -> list[tuple[str, str, float]]:
        """Cosine-distance nearest neighbours over ``tag_embeddings``.

        Returns ``[(namespace, value, distance), ...]`` smallest
        distance first. Tags without a vector (worker hasn't run)
        are excluded; the caller should fall back to lexical search
        when this returns empty unexpectedly.
        """
        if page < 1:
            page = 1
        offset = (page - 1) * page_size
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT namespace, value, "
                "       (vector <=> %s::vector) AS dist "
                "FROM tag_embeddings "
                "WHERE vector IS NOT NULL "
                "ORDER BY vector <=> %s::vector ASC "
                "LIMIT %s OFFSET %s",
                (query_vector, query_vector, page_size, offset),
            ).fetchall()
        return [(str(r[0]), str(r[1]), float(r[2])) for r in rows]

    def search_tags_lexical(
        self,
        *,
        q: str,
        page: int = 1,
        page_size: int = 20,
    ) -> list[tuple[str, str, int]]:
        """Substring match over tag (namespace, value) pairs.

        Returns ``[(namespace, value, usage_count), ...]`` ordered
        by usage_count desc — so common matches surface above
        long-tail ones. Substring (ILIKE) match against
        ``namespace || ':' || value`` so a caller can hit either
        side with one query (``q='topic'`` matches every
        ``topic:*`` open tag; ``q='cap'`` matches ``co2-capture``).
        Empty / whitespace-only ``q`` returns an empty list.
        """
        if not q or not q.strip():
            return []
        if page < 1:
            page = 1
        offset = (page - 1) * page_size
        # ``%`` is the wildcard; escape any literal % / _ in q so a
        # user-supplied string doesn't behave like a pattern.
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT t.namespace, t.value, "
                "       COALESCE(rc.n, 0) + COALESCE(cc.n, 0) AS usage_count "
                "FROM tags t "
                "LEFT JOIN (SELECT tag_id, COUNT(*) AS n FROM ref_tags "
                "           GROUP BY tag_id) rc ON rc.tag_id = t.tag_id "
                "LEFT JOIN (SELECT tag_id, COUNT(*) AS n FROM chunk_tags "
                "           GROUP BY tag_id) cc ON cc.tag_id = t.tag_id "
                "WHERE (t.namespace || ':' || t.value) ILIKE %s "
                "   OR t.value ILIKE %s "
                "ORDER BY usage_count DESC, t.namespace ASC, t.value ASC "
                "LIMIT %s OFFSET %s",
                (f"%{needle}%", f"%{needle}%", page_size, offset),
            ).fetchall()
        return [(str(r[0]), str(r[1]), int(r[2])) for r in rows]

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
                "SELECT r.meta "
                "FROM refs r "
                "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
                "JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE r.kind = %s "
                "  AND t.namespace = 'OPEN' "
                "  AND t.value = %s "
                "  AND r.deleted_at IS NULL "
                "LIMIT 1",
                (kind, tag),
            ).fetchone()
        return None if row is None else (row[0] or {})


__all__ = ["TagsMixin"]

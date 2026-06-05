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

from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

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


def _upsert_tag(conn: Connection, namespace: str, value: str) -> int:
    """Upsert ``tags(namespace, value)`` and return the ``tag_id``.

    Uses the ``DO UPDATE SET namespace = EXCLUDED.namespace``
    no-op trick so RETURNING fires on both the insert and the
    conflict paths. A ``None`` row out of RETURNING here would
    mean either a race condition that DELETEd the tag between our
    INSERT and the implicit RETURNING (Postgres semantics
    preclude this for the conflict path) or a schema invariant
    violation; assertion raises loudly so the failure is fixable
    rather than silently propagating a bogus tag_id downstream.
    """
    row = conn.execute(
        "INSERT INTO tags (namespace, value) VALUES (%s, %s) "
        "ON CONFLICT (namespace, value) "
        "DO UPDATE SET namespace = EXCLUDED.namespace "
        "RETURNING tag_id",
        (namespace, value),
    ).fetchone()
    assert row is not None, (
        f"tags upsert returned no row for ({namespace!r}, {value!r}) — "
        "schema invariant violated"
    )
    return int(row[0])


def _resolve_chunk_id(conn: Connection, ref_id: int, ord_: int) -> int:
    """Look up ``chunks.chunk_id`` for the ``(ref_id, ord)`` pair.

    Used by tag/link operations that take a v1-style ``pos`` (which
    is the chunk's ord under v2) and need the v2 ``chunk_id`` FK
    target for the join-table insert.

    Raises ``ValueError`` when no matching chunk exists. Callers
    that want to silently no-op a tag op against a missing chunk
    should test for the chunk's existence first; this helper
    refuses to make up a chunk_id.
    """
    row = conn.execute(
        "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = %s",
        (ref_id, ord_),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"no chunk at (ref_id={ref_id}, ord={ord_}) — "
            "can't attach a tag to a chunk that doesn't exist"
        )
    return int(row[0])


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
        conn: Connection | None = None,
    ) -> None:
        """Add a tag to a ref (``pos=None``) or to a chunk (``pos=ord``).

        When ``replace_prefix=True`` and the tag is closed-prefix-shaped
        (namespace != FLAG/OPEN), every existing tag in the same
        namespace on the same target is removed first. Maintains the
        v1 invariant that a closed prefix has at most one value per
        target (e.g. exactly one ``STATUS:...`` on a memory).

        Idempotent under ``ON CONFLICT DO NOTHING`` on the join table:
        adding the same tag twice is a no-op.
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
                    "INSERT INTO ref_tags (ref_id, tag_id, set_by) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (ref_id, tag_id) DO NOTHING",
                    (ref_id, tag_id, set_by),
                )
                return

            # Chunk-level tag.
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
        """
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM ref_tags rt "
                "JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = %s "
                "  AND t.namespace = %s "
                "  AND t.value = %s "
                "LIMIT 1",
                (ref_id, namespace, value),
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

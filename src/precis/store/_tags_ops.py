"""Tag CRUD. Mixin on :class:`precis.store.Store`.

Three tables on the DB side — ``ref_closed_tags``, ``ref_flags``,
``ref_open_tags`` — but one conceptual namespace on the agent
surface. :class:`precis.store.types.Tag` encodes the namespace so
``add_tag`` / ``remove_tag`` can dispatch to the right table
without branch-and-check at every call site.

Additional helpers:

* ``tags_for(ref_id)`` joins the three tables and returns a
  unified ``list[Tag]``.
* ``has_flag(ref_id, name)`` hot-path probe used by the
  cache-freshness sweep.
* ``find_first_meta_for_open_tag`` supports the patent CQL lift
  that recovers canonical applicant spelling from a previously-
  ingested row carrying the matching ``applicant:siemens-ag``
  open tag.

Mixin assumes the concrete Store provides ``self.pool``.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

from precis.store._mappers import _REF_LEVEL_POS, _pos_to_db
from precis.store.types import ActorSlug, Tag


class TagsMixin:
    """Tag CRUD + read helpers across the three tag tables."""

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
        """Add a tag to a ref (or block, with ``pos=``).

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
        """Remove a tag from a ref (or block, with ``pos=``).

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
        """Return every tag on ``ref_id`` (or one block of it).

        The three tag tables are ``UNION ALL``-merged at the SQL
        boundary so the result is a flat ``list[Tag]`` with the
        namespace embedded.
        """
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
        """Fast ``pinned`` / ``urgent`` / ``private`` probe for a ref."""
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


__all__ = ["TagsMixin"]

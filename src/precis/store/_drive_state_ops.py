"""Drive Return and Showcase — per-web-user personal state.

Mixin on :class:`precis.store.Store`. Migration
``0220_drive_return_showcase.sql`` defines the three tables; the spec is
``docs/backlog/drive-filter-hierarchy.md`` (slice 1 of 3: store and
migration; the open beacon and the ``/drive`` task scoping come later).

* ``web_ref_views`` — when a user last opened a ref in a full reader.
  Latest only, no expiry (Reto 2026-10-16).
* ``web_collections`` / ``web_collection_members`` — one private
  curated collection per user ("Showcase"), created on the first
  explicit addition, never on a read.

Every method takes the authenticated ``web_users.id`` (never a login,
abbreviation or client-supplied id) and touches only that user's rows;
collection ownership is checked in the SQL, so no caller ever handles a
collection id. Writes refuse soft-retired refs; reads hide them but keep
their rows, so an undelete restores history and membership.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: The one collection name in this slice.
SHOWCASE_NAME = "Showcase"


@dataclass(frozen=True)
class DriveStateRow:
    """One scoped ref with its personal timestamp: ``last_opened_at`` for
    Return, ``added_at`` for Showcase."""

    ref_id: int
    at: datetime


class DriveStateMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    # -- Return: personal open history -------------------------------------

    def record_open(self, user_id: int, ref_id: int) -> bool:
        """Stamp ``ref_id`` as opened now by ``user_id``.

        Keeps the later of the stored and the new time, so a delayed
        write never moves history backwards. Returns False (and writes
        nothing) when the ref is missing or soft-retired.
        """
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO web_ref_views (user_id, ref_id, last_opened_at) "
                "SELECT %s, r.ref_id, now() FROM refs r "
                "WHERE r.ref_id = %s AND r.retired_at IS NULL "
                "ON CONFLICT (user_id, ref_id) DO UPDATE SET last_opened_at = "
                "GREATEST(web_ref_views.last_opened_at, EXCLUDED.last_opened_at) "
                "RETURNING ref_id",
                (user_id, ref_id),
            ).fetchone()
            conn.commit()
        return row is not None

    def forget_open(self, user_id: int, ref_id: int) -> bool:
        """Drop one ref from ``user_id``'s history. Idempotent — True only
        when a row was removed. Showcase membership is untouched."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "DELETE FROM web_ref_views WHERE user_id = %s AND ref_id = %s "
                "RETURNING ref_id",
                (user_id, ref_id),
            ).fetchone()
            conn.commit()
        return row is not None

    def clear_history(self, user_id: int) -> int:
        """Drop all of ``user_id``'s history; returns the row count.
        Showcase membership is untouched."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM web_ref_views WHERE user_id = %s", (user_id,)
            )
            conn.commit()
        return int(cur.rowcount)

    def recent_opens(self, user_id: int, *, limit: int = 100) -> list[DriveStateRow]:
        """``user_id``'s opened refs, last opened first (ties: newest ref
        id first). Soft-retired refs are hidden."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT v.ref_id, v.last_opened_at FROM web_ref_views v "
                "JOIN refs r ON r.ref_id = v.ref_id "
                "WHERE v.user_id = %s AND r.retired_at IS NULL "
                "ORDER BY v.last_opened_at DESC, v.ref_id DESC LIMIT %s",
                (user_id, limit),
            ).fetchall()
        return [DriveStateRow(ref_id=int(r[0]), at=r[1]) for r in rows]

    # -- Showcase: private curated collection ------------------------------

    def ensure_showcase(self, user_id: int) -> int:
        """``user_id``'s collection id, creating it if absent. Called on an
        explicit addition only — reads never create a collection."""
        with self.pool.connection() as conn:
            collection_id = _ensure_showcase(conn, user_id)
            conn.commit()
        return collection_id

    def add_member(self, user_id: int, ref_id: int) -> bool:
        """Add ``ref_id`` to ``user_id``'s Showcase. Idempotent: a repeat
        add keeps the original ``added_at``. Returns True only when newly
        added; False for a repeat or a missing/soft-retired ref (which
        creates no collection either)."""
        with self.pool.connection() as conn:
            live = conn.execute(
                "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL "
                "FOR KEY SHARE",
                (ref_id,),
            ).fetchone()
            if live is None:
                conn.rollback()
                return False
            collection_id = _ensure_showcase(conn, user_id)
            row = conn.execute(
                "INSERT INTO web_collection_members (collection_id, ref_id) "
                "VALUES (%s, %s) ON CONFLICT (collection_id, ref_id) DO NOTHING "
                "RETURNING ref_id",
                (collection_id, ref_id),
            ).fetchone()
            conn.commit()
        return row is not None

    def remove_member(self, user_id: int, ref_id: int) -> bool:
        """Remove ``ref_id`` from ``user_id``'s Showcase. Idempotent —
        True only when a row was removed. The ref itself is untouched."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "DELETE FROM web_collection_members m "
                "USING web_collections c "
                "WHERE c.collection_id = m.collection_id "
                "  AND c.owner_user_id = %s AND m.ref_id = %s "
                "RETURNING m.ref_id",
                (user_id, ref_id),
            ).fetchone()
            conn.commit()
        return row is not None

    def showcase_members(
        self, user_id: int, *, limit: int = 100
    ) -> list[DriveStateRow]:
        """``user_id``'s Showcase, most recently added first (ties: newest
        ref id first). Empty when the user has no collection yet.
        Soft-retired refs are hidden."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT m.ref_id, m.added_at FROM web_collection_members m "
                "JOIN web_collections c ON c.collection_id = m.collection_id "
                "JOIN refs r ON r.ref_id = m.ref_id "
                "WHERE c.owner_user_id = %s AND r.retired_at IS NULL "
                "ORDER BY m.added_at DESC, m.ref_id DESC LIMIT %s",
                (user_id, limit),
            ).fetchall()
        return [DriveStateRow(ref_id=int(r[0]), at=r[1]) for r in rows]


def _ensure_showcase(conn: Any, user_id: int) -> int:
    """Upsert ``user_id``'s collection inside the caller's transaction."""
    row = conn.execute(
        "INSERT INTO web_collections (owner_user_id, name) "
        "VALUES (%s, %s) ON CONFLICT (owner_user_id) DO NOTHING "
        "RETURNING collection_id",
        (user_id, SHOWCASE_NAME),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT collection_id FROM web_collections WHERE owner_user_id = %s",
            (user_id,),
        ).fetchone()
    assert row is not None  # inserted, or the conflicting row exists
    return int(row[0])

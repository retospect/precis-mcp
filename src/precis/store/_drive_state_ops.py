"""Drive ownership, Return and Showcase state for one account.

Ownership and Return group browser activity and identified agent
activity under the same account ID. Ownership records the creation event
and Return keeps a separate latest timestamp for each attribution, so a
busy agent cannot move a person's history. The lists confer no access to
refs.

* ``web_ref_views`` — when a user last opened a ref in a full reader.
  Latest per attribution, no expiry. Legacy opens retain unknown attribution.
* ``web_ref_creations`` — refs created under an account, independent of
  access control; personal refs with only an owner remain unattributed.
* ``web_collections`` / ``web_collection_members`` — one private
  curated collection per user ("Showcase"), created on the first
  explicit addition, never on a read.

Limit: refs created through raw ``INSERT INTO refs`` (``pcb/snapshot.py``,
``agentlog.py``, ``_checklist_ops.py::checklist_ensure_graph_ref``) skip
``record_creation`` and never appear in the owned lists; only
``insert_ref`` records creation.

The list methods scope by the authenticated ``web_users.id``. Collection
ownership is checked in SQL, so no caller handles a collection ID. Writes refuse soft-retired refs; reads hide them but keep
their rows, so an undelete restores history and membership.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from precis.store._secret_scope import secret_fence
from precis.store.revision_context import current_revision_context, process_actor

DriveAttribution = Literal["human", "agent", "unknown"]


def _drive_actor(conn: Any) -> tuple[int, Literal["human", "agent"]] | None:
    """Resolve Drive attribution from trusted ingress, never request metadata."""
    from precis.acting_user import ACTOR_PREFIX
    from precis.user_context import current_principal

    context = current_revision_context()
    human = context is not None and context.trust_actor_type == "human"
    principal = current_principal()
    if principal is not None:
        return principal.user_id, "agent"
    actor = (context.actor if context is not None else None) or process_actor()
    if not actor or not actor.startswith(ACTOR_PREFIX):
        return None
    row = conn.execute(
        "SELECT id FROM web_users WHERE abbrev = %s AND disabled_at IS NULL",
        (actor.removeprefix(ACTOR_PREFIX),),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), "human" if human else "agent"


def record_creation(conn: Any, ref_id: int) -> None:
    """Record the creator in the same transaction as the ref."""
    actor = _drive_actor(conn)
    if actor is not None:
        user_id, attribution = actor
        conn.execute(
            "INSERT INTO web_ref_creations (user_id, ref_id, attribution) "
            "VALUES (%s, %s, %s)",
            (user_id, ref_id, attribution),
        )


#: The one collection name in this slice.
SHOWCASE_NAME = "Showcase"


@dataclass(frozen=True)
class DriveStateRow:
    """A personal ref and the time of its creation, last open or addition."""

    ref_id: int
    at: datetime


class DriveStateMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    # -- Return: personal open history -------------------------------------

    def record_open(
        self, user_id: int, ref_id: int, *, attribution: DriveAttribution = "unknown"
    ) -> bool:
        """Stamp ``ref_id`` as opened now by ``user_id``.

        Keeps the later of the stored and the new time, so a delayed
        write never moves history backwards. Returns False (and writes
        nothing) when the ref is missing or soft-retired, is a
        ``profile``, is owned by another login, is secret-fenced, or the
        user is disabled. ``attribution`` defaults to ``'unknown'``; each
        attribution keeps its own timestamp.
        """
        with self.pool.connection() as conn:
            return _record_open(conn, user_id, ref_id, attribution)

    def record_actor_open(self, ref_id: int) -> None:
        """Record an attributed runtime read when its account is known."""
        with self.pool.connection() as conn:
            actor = _drive_actor(conn)
            if actor is not None:
                user_id, attribution = actor
                _record_open(conn, user_id, ref_id, attribution)

    def forget_open(
        self, user_id: int, ref_id: int, *, attribution: DriveAttribution | None = None
    ) -> bool:
        """Drop one ref from ``user_id``'s history. Idempotent — True only
        when a row was removed. ``attribution=None`` covers every
        attribution; otherwise only that attribution's row is dropped.
        Showcase membership is untouched."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "DELETE FROM web_ref_views WHERE user_id = %s AND ref_id = %s "
                "AND attribution = COALESCE(%s, attribution) RETURNING ref_id",
                (user_id, ref_id, attribution),
            ).fetchone()
            conn.commit()
        return row is not None

    def clear_history(
        self, user_id: int, *, attribution: DriveAttribution | None = None
    ) -> int:
        """Drop all of ``user_id``'s history; returns the row count.
        ``attribution=None`` clears every attribution; otherwise only
        that one. Showcase membership is untouched."""
        with self.pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM web_ref_views WHERE user_id = %s "
                "AND attribution = COALESCE(%s, attribution)",
                (user_id, attribution),
            )
            conn.commit()
        return int(cur.rowcount)

    def recent_opens(
        self,
        user_id: int,
        *,
        limit: int = 100,
        attribution: DriveAttribution | None = None,
    ) -> list[DriveStateRow]:
        """``user_id``'s opened refs, last opened first (ties: newest ref
        id first). Hidden: soft-retired refs, ``profile`` refs, refs owned
        by another login and secret-fenced refs. With ``attribution``
        None the rows merge per ref, using the latest open across
        attributions; otherwise only that attribution's rows list."""
        if attribution is None:
            timestamp = "max(v.last_opened_at)"
            scope = "GROUP BY v.ref_id"
            params: tuple[int | str, ...] = (user_id, limit)
        else:
            timestamp = "v.last_opened_at"
            scope = "AND v.attribution = %s"
            params = (user_id, attribution, limit)
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT v.ref_id, {timestamp} FROM web_ref_views v "
                "JOIN refs r ON r.ref_id = v.ref_id "
                "JOIN web_users u ON u.id = v.user_id "
                "WHERE v.user_id = %s AND r.retired_at IS NULL AND r.kind <> 'profile' "
                "AND (r.owner_login IS NULL OR r.owner_login = u.login) "
                f"AND {secret_fence(path='Drive Return')} {scope} "
                f"ORDER BY {timestamp} DESC, v.ref_id DESC LIMIT %s",
                params,
            ).fetchall()
        return [DriveStateRow(ref_id=int(r[0]), at=r[1]) for r in rows]

    def drive_owned_refs(
        self, user_id: int, *, attribution: DriveAttribution, limit: int = 100
    ) -> list[DriveStateRow]:
        """Refs created under this account, plus explicitly owned legacy refs."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT r.ref_id, r.created_at FROM ("
                "  SELECT ref_id FROM web_ref_creations WHERE user_id = %s AND attribution = %s"
                "  UNION ALL"
                "  SELECT legacy.ref_id FROM refs legacy JOIN web_users u ON u.login = legacy.owner_login"
                "  WHERE u.id = %s AND %s = 'unknown' AND NOT EXISTS ("
                "    SELECT 1 FROM web_ref_creations c WHERE c.ref_id = legacy.ref_id AND c.user_id = u.id"
                "  )"
                ") owned JOIN refs r ON r.ref_id = owned.ref_id "
                "JOIN web_users owner ON owner.id = %s "
                "WHERE r.retired_at IS NULL "
                "AND (r.owner_login IS NULL OR r.owner_login = owner.login) "
                f"AND r.kind <> 'profile' AND {secret_fence(path='Drive ownership')} "
                "ORDER BY r.ref_id DESC LIMIT %s",
                (user_id, attribution, user_id, attribution, user_id, limit),
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


def _record_open(
    conn: Any, user_id: int, ref_id: int, attribution: DriveAttribution
) -> bool:
    row = conn.execute(
        "INSERT INTO web_ref_views (user_id, ref_id, attribution, last_opened_at) "
        "SELECT u.id, r.ref_id, %s, now() FROM refs r JOIN web_users u ON u.id = %s "
        "WHERE r.ref_id = %s AND r.retired_at IS NULL AND r.kind <> 'profile' "
        "AND u.disabled_at IS NULL AND (r.owner_login IS NULL OR r.owner_login = u.login) "
        f"AND {secret_fence(path='Drive open')} "
        "ON CONFLICT (user_id, ref_id, attribution) DO UPDATE SET last_opened_at = "
        "GREATEST(web_ref_views.last_opened_at, EXCLUDED.last_opened_at) "
        "RETURNING ref_id",
        (attribution, user_id, ref_id),
    ).fetchone()
    return row is not None


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

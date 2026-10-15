"""``ref_favorites`` — per-web-user hearted refs (gr486409).

Mixin on :class:`precis.store.Store`. Migration ``0211_ref_favorites.sql``
defines the table; :mod:`precis_web.routes.favorites` is the only caller —
the heart toggles on the graph focus page and the ref/paper readers, and
the ``/favorites`` list.

Per-user and private: every method takes the caller's ``login`` (the
Basic-auth ``web_users.login``, never a client-supplied name) and touches
only that login's rows. A hearted ref that is later soft-deleted stays in
the list — the row reports ``retired_at`` so the UI renders a tombstone
instead of erroring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class FavoriteRow:
    """One ``ref_favorites`` row joined to its ref, for the list page."""

    ref_id: int
    kind: str
    title: str
    slug: str | None
    handle: str | None
    #: ``refs.updated_at`` — the "last change" the list shows.
    ref_updated_at: datetime | None
    #: Soft-delete stamp; non-None rows render as tombstones.
    retired_at: datetime | None
    #: When the heart was set (``ref_favorites.created_at``) — the list's
    #: newest-first ordering.
    favorited_at: datetime


class FavoritesMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    def set_favorite(self, login: str, ref_id: int) -> bool:
        """Heart ``ref_id`` for ``login``. Idempotent — returns True only
        when the row was newly inserted."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO ref_favorites (login, ref_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING RETURNING ref_id",
                (login, ref_id),
            ).fetchone()
            conn.commit()
        return row is not None

    def unset_favorite(self, login: str, ref_id: int) -> bool:
        """Un-heart ``ref_id`` for ``login``. Idempotent — returns True
        only when a row was actually removed."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "DELETE FROM ref_favorites WHERE login = %s AND ref_id = %s "
                "RETURNING ref_id",
                (login, ref_id),
            ).fetchone()
            conn.commit()
        return row is not None

    def is_favorite(self, login: str, ref_id: int) -> bool:
        """The heart's current state for one ref — the readers' render."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM ref_favorites WHERE login = %s AND ref_id = %s",
                (login, ref_id),
            ).fetchone()
        return row is not None

    def list_favorites(self, login: str) -> list[FavoriteRow]:
        """``login``'s hearted refs, most recently hearted first.

        Soft-deleted refs are included (``retired_at`` set) so the list
        shows a tombstone instead of silently dropping a row.
        """
        with self.pool.connection() as conn:
            rows = conn.execute(
                # ``slug`` mirrors the _mappers convention: the ref's
                # ``ref_identifiers`` row under id_kind='cite_key' (the
                # slug-as-identifier rule for slug-addressed kinds).
                "SELECT f.ref_id, r.kind, r.title, r.handle, "
                "       r.updated_at, r.retired_at, f.created_at, "
                "       (SELECT i.id_value FROM ref_identifiers i "
                "         WHERE i.ref_id = r.ref_id AND i.id_kind = 'cite_key' "
                "         ORDER BY i.created_at DESC LIMIT 1) AS slug "
                "FROM ref_favorites f JOIN refs r ON r.ref_id = f.ref_id "
                "WHERE f.login = %s ORDER BY f.created_at DESC, f.ref_id DESC",
                (login,),
            ).fetchall()
        return [
            FavoriteRow(
                ref_id=int(r[0]),
                kind=str(r[1]),
                title=str(r[2]),
                slug=r[7],
                handle=r[3],
                ref_updated_at=r[4],
                retired_at=r[5],
                favorited_at=r[6],
            )
            for r in rows
        ]

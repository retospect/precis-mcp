"""Private gtd items, backed by gtd-only tables under forced row-level security.

``GtdStore`` shares the existing Store pool but never creates refs, chunks,
tags, links or embeddings. Every operation revalidates the request principal,
sets ``precis.gtd_user_id`` for the transaction and also filters by owner, so
the policy and the predicate are independent boundaries. Another user's item
and a missing one raise the same NotFound.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg

from precis.errors import BadInput, NotFound, PrecisError, Upstream
from precis.user_context import require_user

if TYPE_CHECKING:
    from precis.store import Store

__all__ = ["GtdItem", "GtdStore"]

_COLS = "item_id, list, state, text, captured, created_at"
_NEXT = "put(kind='gtd', text='<the thing to capture>')"
#: A capture is refused over this many UTF-8 bytes, before any INSERT —
#: kept well under the kind's response bound so a whole item always
#: reads back whole.
_MAX_CAPTURE_BYTES = 4000


@dataclass(frozen=True, slots=True)
class GtdItem:
    """One owner's item as captured."""

    id: int
    list: str
    state: str
    text: str
    captured: str
    created_at: datetime


def _item(row: Any) -> GtdItem:
    return GtdItem(int(row[0]), row[1], row[2], row[3], row[4], row[5])


class GtdStore:
    """Owner-only gtd SQL over a shared existing Store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @contextmanager
    def _tx(self, owner: int) -> Iterator[psycopg.Connection]:
        try:
            with self._store.pool.connection() as conn, conn.transaction():
                conn.execute(
                    "SELECT set_config('precis.gtd_user_id', %s, true)",
                    (str(owner),),
                )
                yield conn
        except PrecisError:
            raise
        except psycopg.Error as exc:
            # SQL diagnostics can quote the private text; surface none of it.
            raise Upstream(
                "gtd storage is unavailable",
                next="retry the gtd operation when the database is available",
            ) from exc

    def capture(self, text: str) -> GtdItem:
        """Insert one inbox item; ``captured`` keeps the words as given."""
        owner = require_user(self._store)
        if not isinstance(text, str) or not text.strip():
            raise BadInput("capture needs nonblank text", next=_NEXT)
        if len(text.encode("utf-8")) > _MAX_CAPTURE_BYTES:
            raise BadInput(
                f"gtd capture is at most {_MAX_CAPTURE_BYTES} UTF-8 bytes",
                next="shorten the words; detail belongs in a note",
            )
        if any(unicodedata.category(ch) == "Cc" and ch not in "\n\t" for ch in text):
            raise BadInput(
                "capture text must not contain control characters "
                "other than newline and tab",
                next=_NEXT,
            )
        with self._tx(owner) as conn:
            row = conn.execute(
                "INSERT INTO gtd_items (user_id, text, captured) "
                f"VALUES (%s, %s, %s) RETURNING {_COLS}",
                (owner, text, text),
            ).fetchone()
        assert row is not None
        return _item(row)

    def get_item(self, item_id: int) -> GtdItem:
        """The owner's item; another user's id and a missing one look alike."""
        owner = require_user(self._store)
        if type(item_id) is not int or item_id <= 0:
            raise BadInput(
                "item_id must be a positive integer",
                next="use the gt<N> handle returned by capture",
            )
        with self._tx(owner) as conn:
            row = conn.execute(
                f"SELECT {_COLS} FROM gtd_items WHERE user_id = %s AND item_id = %s",
                (owner, item_id),
            ).fetchone()
        if row is None:
            raise NotFound("gtd item not found", next="get(kind='gtd', id='gt<N>')")
        return _item(row)

"""Per-login ``/eye`` walk records — the owner's private navigation trail.

Every authenticated focus render appends one row: the web user, the focused
handle and its kind, the same owner's previous handle (so the walk reads as
a linked trail), and a UTC timestamp. No query text and no rendered content
ever lands here — the record says *where* the owner walked, never *what the
page said* (gr743231).

Rows belong to one login and are unreachable from another: every operation
revalidates the trusted request principal (:func:`precis.user_context.
require_user`) and predicates every statement on that owner, the same
boundary :mod:`precis.learning` keeps for its private ledger. No RLS policy
is forced on the table: unlike the learning ledger, no MCP dispatch path
reaches it — only trusted in-process web routes do, so explicit owner
predicates are the whole boundary (the ``anki_known_undo`` table keeps the
same shape).

Reads and links are limited to a 90-day sliding window. Each append prunes
the owner's expired rows in the same transaction. The owner can also clear
the whole walk on /learning. The daily ``walk_cards`` worker pass
(:mod:`precis.workers.walk_cards`, gr743232) re-reads this log to queue
walked concepts for the card forge.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg

from precis.errors import BadInput, PrecisError, Upstream
from precis.user_context import require_user

if TYPE_CHECKING:
    from precis.store import Store

__all__ = ["RETENTION_DAYS", "WALK_LIMIT", "EyeVisitStore", "WalkStep"]

#: Rows older than this are pruned on append (gr743231's proposed window).
RETENTION_DAYS = 90

#: How many steps the /learning "your recent walk" panel renders.
WALK_LIMIT = 20


@dataclass(frozen=True, slots=True)
class WalkStep:
    """One immutable focus render: which handle, from which previous one."""

    visit_id: int
    handle: str
    kind: str
    previous_handle: str | None
    visited_at: datetime


def _step(row: Any) -> WalkStep:
    return WalkStep(
        visit_id=int(row[0]),
        handle=row[1],
        kind=row[2],
        previous_handle=row[3],
        visited_at=row[4],
    )


def _label(value: str, *, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\x00" in value:
        raise BadInput(
            f"{name} must contain 1 to {maximum} characters without NUL",
            next=f"supply a {name} within that limit",
        )
    return value


class EyeVisitStore:
    """Owner-only walk SQL over a shared existing Store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @contextmanager
    def _tx(self, owner: int) -> Iterator[psycopg.Connection]:
        try:
            with self._store.pool.connection() as conn, conn.transaction():
                yield conn
        except PrecisError:
            raise
        except psycopg.Error as exc:
            raise Upstream(
                "walk storage is unavailable",
                next="retry the walk operation when the database is available",
            ) from exc

    def record(self, *, handle: str, kind: str) -> WalkStep:
        """Append this owner's step, chain the previous one, prune the window.

        *previous_handle* is the handle on this owner's latest row before
        this one — read and inserted in the same transaction, so concurrent
        focus renders may interleave but never observe a torn trail."""
        owner = require_user(self._store)
        _label(handle, name="handle", maximum=200)
        _label(kind, name="kind", maximum=40)
        with self._tx(owner) as conn:
            conn.execute(
                "DELETE FROM eye_visits WHERE user_id = %s "
                "AND visited_at < now() - make_interval(days => %s)",
                (owner, RETENTION_DAYS),
            )
            previous = conn.execute(
                "SELECT handle FROM eye_visits WHERE user_id = %s "
                "ORDER BY visit_id DESC LIMIT 1",
                (owner,),
            ).fetchone()
            row = conn.execute(
                "INSERT INTO eye_visits (user_id, handle, kind, previous_handle) "
                "VALUES (%s, %s, %s, %s) "
                "RETURNING visit_id, handle, kind, previous_handle, visited_at",
                (owner, handle, kind, previous[0] if previous else None),
            ).fetchone()
            assert row is not None
            return _step(row)

    def recent(self, *, limit: int = WALK_LIMIT) -> list[WalkStep]:
        """This owner's most recent steps, newest first."""
        owner = require_user(self._store)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BadInput(
                "limit must be an integer from 1 to 100",
                next=f"request limit={WALK_LIMIT} for the standard walk panel",
            )
        with self._tx(owner) as conn:
            rows = conn.execute(
                "SELECT visit_id, handle, kind, previous_handle, visited_at "
                "FROM eye_visits WHERE user_id = %s "
                "AND visited_at >= now() - make_interval(days => %s) "
                "ORDER BY visit_id DESC LIMIT %s",
                (owner, RETENTION_DAYS, limit),
            ).fetchall()
        return [_step(row) for row in rows]

    def clear(self) -> int:
        """Delete every one of this owner's steps; returns the count."""
        owner = require_user(self._store)
        with self._tx(owner) as conn:
            result = conn.execute("DELETE FROM eye_visits WHERE user_id = %s", (owner,))
            return int(result.rowcount or 0)

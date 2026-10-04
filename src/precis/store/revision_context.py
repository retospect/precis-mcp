"""Who changed a ref or link, with which model, and why — for the revisions log.

Migration 0185's triggers write one ``revisions`` row per transaction that
changes a covered part of a ref or link (docs/backlog/local-mesh-upkeep.md
§2b). The trigger cannot see Python, so the reason, actor and model reach
it as transaction-local settings: ``precis.reason``, ``precis.actor``,
``precis.model`` and ``precis.event`` (``'merged-into'`` is the only
honoured override).

A caller wraps its writes in :func:`revision_context`; the store's pool
(:class:`precis.store.pool.PrecisPool`) calls :func:`apply_revision_context`
on every connection it hands out inside one. That runs
``set_config(name, value, true)`` — transaction-scoped, so it is safe under
pgbouncer transaction pooling. Never a session ``SET``: on the prod DSN a
session setting leaks to whichever client gets the server connection next.

Outside any context nothing is sent; a covered write then logs
``reason='(unrecorded)'`` with the DB role as actor, and the nightly count
of those rows names the write paths still to wrap. Nested contexts merge:
an inner field that is ``None`` inherits the outer one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from psycopg import Connection

#: The single event a caller may name; the trigger derives the rest
#: (edited / retired / restored / deleted) from the row change.
MERGED_INTO = "merged-into"


@dataclass(frozen=True, slots=True)
class RevisionContext:
    """The settings the revisions trigger reads for this transaction."""

    reason: str | None = None
    actor: str | None = None
    model: str | None = None
    event: str | None = None


_current: ContextVar[RevisionContext | None] = ContextVar(
    "precis_revision_context", default=None
)


def current_revision_context() -> RevisionContext | None:
    """The active context, or ``None`` outside any :func:`revision_context`."""
    return _current.get()


@contextmanager
def revision_context(
    reason: str | None = None,
    *,
    actor: str | None = None,
    model: str | None = None,
    event: str | None = None,
) -> Iterator[RevisionContext]:
    """Attach ``reason`` / ``actor`` / ``model`` to every write in the block.

    ``event`` may only be :data:`MERGED_INTO` (the merge path retiring the
    absorbed side); anything else is a programming error.
    """
    if event is not None and event != MERGED_INTO:
        raise ValueError(
            f"revision event override must be {MERGED_INTO!r}, got {event!r}"
        )
    outer = _current.get() or RevisionContext()
    ctx = RevisionContext(
        reason=reason if reason is not None else outer.reason,
        actor=actor if actor is not None else outer.actor,
        model=model if model is not None else outer.model,
        event=event if event is not None else outer.event,
    )
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def apply_revision_context(conn: Connection) -> None:
    """Send the active context to ``conn``'s current transaction.

    No-op outside a context, and on an autocommit connection (there is no
    transaction for a local setting to live in). On a regular pooled
    connection this opens the transaction the caller's statements then
    run in, which the pool commits on exit as before.
    """
    ctx = _current.get()
    if ctx is None or conn.autocommit:
        return
    conn.execute(
        "SELECT set_config('precis.reason', %s, true), "
        "set_config('precis.actor', %s, true), "
        "set_config('precis.model', %s, true), "
        "set_config('precis.event', %s, true)",
        (ctx.reason or "", ctx.actor or "", ctx.model or "", ctx.event or ""),
    )

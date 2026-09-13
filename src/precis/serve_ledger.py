"""Serve ledger — session-keyed dedup state for repeat skill serves.

docs/backlog/skill-graph.md slice 1, "Serve ledger (tier-2 state)".
Tracks, per MCP session, which skill slugs have been served in full and
at what ``file_sha256``, so a repeat ``get(kind='skill', id=<slug>)`` in
the same session can degrade to a cheap stub instead of resending an
unchanged multi-KB body. Deliberately narrow to skills — the design
permits generalizing to other kinds, but that's explicitly not built
now (see the backlog item's "Explicitly NOT in scope").

Keyed off the actual MCP session object (``ctx.session`` from FastMCP's
per-request ``Context`` — see ``tools/core.py``'s ``get``/``search``),
never a module global: two concurrent agent sessions must never see
each other's ledger, and the HTTP transport serves many sessions from
one process. A :class:`weakref.WeakKeyDictionary` keyed on the session
object itself means a closed/GC'd session's entry disappears on its
own — no explicit teardown hook needed. A process restart drops
everything (no persistence, by design): every skill looks unseen after
a restart and gets a full serve — "loss degrades to full serves."

The binding is a plain ``contextvars.ContextVar`` set for the duration
of one top-level tool call (:func:`bind`/:func:`unbind`, or the
:func:`session_scope` context manager) rather than an explicit
parameter threaded through ``runtime.dispatch_with_status`` down into
the handler's ``**kwargs`` — the strict per-verb kwarg whitelist there
(``runtime.dispatch.DispatchMixin._invoke_handler``) would otherwise
have to special-case a ``_session=`` kwarg on every kind except
``skill``. ``anyio.to_thread.run_sync`` (``server._offload_sync``)
copies the calling context into the worker thread, so the var would
still be visible if a caller ever bound it before offloading — but
every current caller (``tools.core.get`` / ``search``) binds it in the
same thread the handler actually runs on, so that propagation is a
documented safety margin, not something this module depends on.

Hard rules from the backlog spec:

- Never gates a write or correctness decision — this is a read-only
  annotation layer.
- A stub always carries its own ``full=true`` re-fetch escape hatch;
  the ledger's presence is a hint, never a hard suppression ("hint
  always, suppress never").
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from weakref import WeakKeyDictionary

#: One (file_sha256, when-served) pair for a served slug.
LedgerEntry = tuple[str, datetime]

#: session object -> {slug: (file_sha256, when-served)}. Weak on the
#: session key so a closed session's ledger is reclaimed with it. Note:
#: a bare ``object()`` isn't weakly referenceable — real session objects
#: (``mcp.server.session.ServerSession``, or any plain class instance a
#: test stands in for one) are.
_SESSION_LEDGERS: WeakKeyDictionary[object, dict[str, LedgerEntry]] = (
    WeakKeyDictionary()
)

#: Guards per-session ledger creation only (see ``record``).
_LEDGER_INIT_LOCK = threading.Lock()

#: The session bound for the currently-executing tool call, or ``None``
#: outside one (direct handler unit tests, the CLI, a stateless build).
_current_session: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "precis_serve_ledger_session", default=None
)


def bind(session: object | None) -> contextvars.Token[object | None]:
    """Bind ``session`` as the ledger's current session; returns a reset token."""
    return _current_session.set(session)


def unbind(token: contextvars.Token[object | None]) -> None:
    """Undo a :func:`bind` call. Always pair with a ``try/finally``."""
    _current_session.reset(token)


@contextmanager
def session_scope(session: object | None) -> Iterator[None]:
    """Bind ``session`` for the duration of the ``with`` block. Test/CLI convenience."""
    token = bind(session)
    try:
        yield
    finally:
        unbind(token)


def record(slug: str, file_sha256: str) -> None:
    """Record that ``slug`` was just served in full at ``file_sha256``.

    No-op when no session is bound (direct handler calls, the CLI, a
    stateless build) — there's nothing to key the ledger on.
    """
    session = _current_session.get()
    if session is None:
        return
    # WeakKeyDictionary.setdefault is a Python-level check-then-act:
    # two concurrent tool calls in one session (tool concurrency > 1)
    # could each mint a fresh dict and drop the other's entry. The lock
    # makes first-write win; lookups stay lock-free (a lost hint only
    # costs an extra full serve).
    with _LEDGER_INIT_LOCK:
        ledger = _SESSION_LEDGERS.setdefault(session, {})
    ledger[slug] = (file_sha256, datetime.now(UTC))


def lookup(slug: str) -> LedgerEntry | None:
    """Return ``(file_sha256, when-served)`` for ``slug`` in the bound
    session, or ``None`` if unrecorded or no session is bound."""
    session = _current_session.get()
    if session is None:
        return None
    ledger = _SESSION_LEDGERS.get(session)
    if ledger is None:
        return None
    return ledger.get(slug)


def was_served(slug: str) -> bool:
    """True if ``slug`` has been served (any sha) in the bound session.

    Used for the footer / search "(read this session)" annotation —
    unlike :func:`lookup`, callers here don't care about a sha match,
    only "has this been served before this turn."
    """
    return lookup(slug) is not None

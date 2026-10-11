"""MCP session binding for the duration of one tool call.

Originally the skill serve ledger (per-session dedup of repeat skill
serves); that dedup was replaced by the client-held ``skill rev`` tag
(gr477798) because subagents share the parent's session. What remains is
the reusable part: the binding of the live FastMCP session object
(``ctx.session``, see ``tools/core.py``'s ``get``/``search``) to a
``contextvars.ContextVar`` for one tool call, read by
``install_watchdog``'s per-session exit breadcrumb.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

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


def current_session() -> object | None:
    """The MCP session bound for this tool call, or ``None`` outside one.

    Exported because the binding — not the ledger — is the reusable part:
    ``install_watchdog``'s per-session exit breadcrumb keys off the same
    session this module already binds, rather than standing up a second
    contextvar that could disagree about which session a call belongs to.
    """
    return _current_session.get()


@contextmanager
def session_scope(session: object | None) -> Iterator[None]:
    """Bind ``session`` for the duration of the ``with`` block. Test/CLI convenience."""
    token = bind(session)
    try:
        yield
    finally:
        unbind(token)

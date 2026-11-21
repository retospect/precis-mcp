"""Per-MCP-session word-handle switch (gr674040).

The user says "use word handles" once and every list/search row in that
session gains a ``word`` column with the ref's two-word spoken handle, so
a voice agent never has to look ids up one by one before it can speak
them. Off by default — the 12 Nov ruling is opt-in only, and normal agent
traffic pays no extra tokens for a column it never reads.

Stateful transports use the live MCP session object. Stateless HTTP has
no cross-request session identity, so it uses the authenticated connector
credential (an OAuth grant survives access-token refresh). The bounded
process-local cache keeps flags off other credentials and avoids sharing a
preference through the installation-wide bearer.

An explicit switch wins over auto-enable. The built-in Claude connector
registration identifies Claude across stateless requests, where initialize
clientInfo is unavailable. It covers both voice and text because the
connector does not declare a conversation's modality. Deployments may
also match clientInfo, OAuth client id or HTTP User-Agent substrings via
``PRECIS_WORD_HANDLE_CLIENTS``. Those labels affect presentation only.
"""

from __future__ import annotations

import os
import weakref
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from typing import Any, Literal

from precis.principal import current_owner_key
from precis.serve_ledger import current_session
from precis.tool_ledger import _stateless_http, current_client

#: Client-label substrings that auto-enable word handles.
CLIENTS_ENV = "PRECIS_WORD_HANDLE_CLIENTS"

#: Fallback-dict bound for un-weak-referenceable sessions.
_MAX_FALLBACK_SESSIONS = 256

_flags: weakref.WeakKeyDictionary[Any, bool] = weakref.WeakKeyDictionary()
#: Un-weak-referenceable sessions: ``id()`` -> ``(session, on)``. The
#: strong session ref keeps a live entry's ``id()`` from recycling.
_flags_fallback: OrderedDict[int, tuple[Any, bool]] = OrderedDict()


_connector: ContextVar[tuple[str, str] | None] = ContextVar(
    "word_handle_connector", default=None
)
_connector_flags: OrderedDict[str, bool] = OrderedDict()
_connector_lock = Lock()


@contextmanager
def http_scope(client_id: str, user_agent: str) -> Iterator[None]:
    """Bind presentation metadata from the authenticated HTTP request."""
    token = _connector.set((client_id, user_agent))
    try:
        yield
    finally:
        _connector.reset(token)


def _switch_target() -> tuple[object | None, str | None]:
    """Prefer a live session except when the HTTP transport discards it."""
    session = current_session()
    if _connector.get() is None or (session is not None and not _stateless_http()):
        return session, None
    owner = current_owner_key()
    return None, owner if owner != "install" else None


def _auto_enabled() -> bool:
    client_id, user_agent = _connector.get() or ("", "")
    from precis.oauth.config import CLAUDE_CLIENT_ID

    if client_id == CLAUDE_CLIENT_ID:
        return True
    tokens = os.environ.get(CLIENTS_ENV, "").casefold().split(",")
    labels = tuple(
        label.casefold() for label in (current_client() or "", client_id, user_agent)
    )
    return any(
        token in label for part in tokens if (token := part.strip()) for label in labels
    )


def _flag_for(session: Any) -> bool | None:
    """The session's recorded switch, or ``None`` when it never said."""
    try:
        return _flags.get(session)
    except TypeError:
        entry = _flags_fallback.get(id(session))
        if entry is None:
            return None
        _flags_fallback.move_to_end(id(session))
        return entry[1]


def enabled() -> bool:
    """Resolve an explicit session or connector switch before auto-enable."""
    session, owner = _switch_target()
    if session is not None:
        flag = _flag_for(session)
        if flag is not None:
            return flag
    if owner is not None:
        with _connector_lock:
            if owner in _connector_flags:
                _connector_flags.move_to_end(owner)
                return _connector_flags[owner]
    return _auto_enabled()


def set_enabled(on: bool) -> Literal["connector credential", "session"] | None:
    """Set the switch and return the scope that received it, if any."""
    session, owner = _switch_target()
    if owner is not None:
        with _connector_lock:
            _connector_flags[owner] = on
            _connector_flags.move_to_end(owner)
            if len(_connector_flags) > _MAX_FALLBACK_SESSIONS:
                _connector_flags.popitem(last=False)
        return "connector credential"
    if session is None:
        return None
    try:
        _flags[session] = on
    except TypeError:
        key = id(session)
        _flags_fallback[key] = (session, on)
        _flags_fallback.move_to_end(key)
        while len(_flags_fallback) > _MAX_FALLBACK_SESSIONS:
            _flags_fallback.popitem(last=False)
    return "session"


def _clear_for_tests() -> None:
    """Drop all session switch state (test isolation)."""
    _flags.clear()
    _flags_fallback.clear()
    _connector_flags.clear()

"""Per-MCP-session word-handle switch (gr674040).

The user says "use word handles" once and every list/search row in that
session gains a ``word`` column with the ref's two-word spoken handle, so
a voice agent never has to look ids up one by one before it can speak
them. Off by default — the 12 Nov ruling is opt-in only, and normal agent
traffic pays no extra tokens for a column it never reads.

Two ways on:

- ``put(kind='handle', text='on'|'off')`` — the session switch. State
  keys on the session object ``serve_ledger`` binds for the call, stored
  in a :class:`~weakref.WeakKeyDictionary` so a closed session's flag is
  reclaimed with it (same argument ``hint_ledger`` makes for its seen-set;
  an un-weak-referenceable session falls back to an ``id()``-keyed dict
  capped at :data:`_MAX_FALLBACK_SESSIONS`). An explicit ``off`` beats
  the env rule below. Nothing is persisted across sessions by design.
- ``PRECIS_WORD_HANDLE_CLIENTS`` — comma-separated case-insensitive
  substrings matched against the bound client's ``name/version`` label
  (``tool_ledger.current_client``); a declaring voice client gets word
  handles without asking. Empty/unset matches nothing.

A sessionless caller (CLI, inproc tick, direct handler call, tests) can
only reach the env rule — there is no session to pin a flag to, and
``set_enabled`` reports that rather than pretending the switch took.

Under ``PRECIS_MCP_STATELESS_HTTP`` every request binds a fresh session
object, so a flag set on one request is invisible to the next — the
switch is per-session by contract, not per-process; the env rule is the
door for transports that can't hold session state.
"""

from __future__ import annotations

import os
import weakref
from collections import OrderedDict
from typing import Any

from precis.serve_ledger import current_session

#: Env var naming clientInfo substrings that auto-enable word handles.
CLIENTS_ENV = "PRECIS_WORD_HANDLE_CLIENTS"

#: Fallback-dict bound for un-weak-referenceable sessions.
_MAX_FALLBACK_SESSIONS = 256

_flags: weakref.WeakKeyDictionary[Any, bool] = weakref.WeakKeyDictionary()
#: Un-weak-referenceable sessions: ``id()`` -> ``(session, on)``. The
#: strong session ref keeps a live entry's ``id()`` from recycling.
_flags_fallback: OrderedDict[int, tuple[Any, bool]] = OrderedDict()


def _env_auto() -> bool:
    """True when the bound client's label matches ``PRECIS_WORD_HANDLE_CLIENTS``."""
    raw = os.environ.get(CLIENTS_ENV, "")
    if not raw.strip():
        return False
    from precis.tool_ledger import current_client

    client = current_client()
    if not client:
        return False
    label = client.casefold()
    return any(
        token in label
        for token in (part.strip().casefold() for part in raw.split(","))
        if token
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
    """True while the bound session wants word handles.

    An explicit ``put`` flag wins either way (``off`` suppresses even an
    env-matched client); with no flag the env rule decides. Sessionless
    callers only see the env rule — there is no flag to read.
    """
    session = current_session()
    if session is not None:
        flag = _flag_for(session)
        if flag is not None:
            return flag
    return _env_auto()


def set_enabled(on: bool) -> bool:
    """Pin the switch on the bound session.

    Returns ``False`` when no session is bound — a CLI or direct-dispatch
    ``put`` has nothing durable to key the flag to, so it reports the
    switch did not take rather than leaking a flag onto every later
    sessionless call.
    """
    session = current_session()
    if session is None:
        return False
    try:
        _flags[session] = on
    except TypeError:
        key = id(session)
        _flags_fallback[key] = (session, on)
        _flags_fallback.move_to_end(key)
        while len(_flags_fallback) > _MAX_FALLBACK_SESSIONS:
            _flags_fallback.popitem(last=False)
    return True


def _clear_for_tests() -> None:
    """Drop all session switch state (test isolation)."""
    _flags.clear()
    _flags_fallback.clear()

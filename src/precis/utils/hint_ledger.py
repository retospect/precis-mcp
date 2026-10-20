"""Per-MCP-session suppression of repeated hint text (gr499041).

The response boilerplate — ``Next:`` blocks, the ``trust:`` footer, the
skill ``misled by this skill?`` promo — is identical every time the same
call shape recurs, and d-mcpeff's 7-day analysis attributes ~200M tok/day
of cache-read carry to wrappers like it. Reto's ruling on the gripe: key
the shown hints per MCP session — suppress a hint the session already
received within a TTL, and fall back to always showing it when there is
no session (stdio, the CLI, direct handler calls, tests).

The session object is the one ``serve_ledger`` already binds for the
call; nothing here invents a second notion of session. State lives in a
:class:`~weakref.WeakKeyDictionary`, so a closed session's entry is
reclaimed with it (same argument ``install_watchdog`` makes for its
breadcrumb). A session that cannot be weak-referenced falls back to an
``id()``-keyed dict capped at :data:`_MAX_FALLBACK_SESSIONS` — bounded,
if not collectable.
"""

from __future__ import annotations

import time
import weakref
from collections import OrderedDict
from hashlib import sha1
from typing import Any

from precis.serve_ledger import current_session

#: Re-show a hint after this long even when the session already saw it —
#: a hint suppressed hours ago may be relevant to a different subtask.
_TTL_S = 1800.0

#: Per-session bound on remembered hints; oldest drop out LRU-style.
_MAX_HINTS_PER_SESSION = 512

#: Fallback-dict bound for un-weak-referenceable sessions.
_MAX_FALLBACK_SESSIONS = 256

_seen: weakref.WeakKeyDictionary[Any, OrderedDict[str, float]] = (
    weakref.WeakKeyDictionary()
)
#: Un-weak-referenceable sessions: ``id()`` -> ``(session, hints)``. The
#: strong reference is what makes the ``id()`` key safe — a live entry's
#: session can't be collected, so its id can't be recycled onto a new
#: session while it still holds hints.
_seen_fallback: OrderedDict[int, tuple[Any, OrderedDict[str, float]]] = OrderedDict()


def _bucket() -> OrderedDict[str, float] | None:
    """The session's ``{digest -> shown-at}`` map, or None when sessionless."""
    session = current_session()
    if session is None:
        return None
    try:
        return _seen.setdefault(session, OrderedDict())
    except TypeError:
        key = id(session)
        entry = _seen_fallback.get(key)
        if entry is None:
            entry = _seen_fallback[key] = (session, OrderedDict())
        _seen_fallback.move_to_end(key)
        while len(_seen_fallback) > _MAX_FALLBACK_SESSIONS:
            _seen_fallback.popitem(last=False)
        return entry[1]


def was_shown(text: str) -> bool:
    """True when this session was already served ``text`` within the TTL.

    Records the show on first sight. A sessionless caller always reads
    ``False`` — hints never suppress where there is no session to
    remember them against (the ruling's explicit fallback).
    """
    bucket = _bucket()
    if bucket is None:
        return False
    now = time.monotonic()
    digest = sha1(text.encode("utf-8")).hexdigest()[:16]
    seen_at = bucket.get(digest)
    if seen_at is not None and now - seen_at < _TTL_S:
        bucket.move_to_end(digest)
        return True
    bucket[digest] = now
    bucket.move_to_end(digest)
    while len(bucket) > _MAX_HINTS_PER_SESSION:
        bucket.popitem(last=False)
    return False


def _clear_for_tests() -> None:
    """Drop all session hint state (test isolation)."""
    _seen.clear()
    _seen_fallback.clear()

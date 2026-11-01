"""Bounded, process-local search attribution; ordinary reads do no trace SQL.

Only traces inserted by this process can receive feedback. Losing attribution
on restart is preferable to querying recent telemetry on every get. Cache
locks protect only in-memory work; database failures never escape the hook.
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from precis.utils import handle_registry
from precis.utils.mentions import _universal_handle_tokens

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)
WINDOW = timedelta(minutes=30)
MAX_TRACES = 32
MAX_SESSIONS = 128


@dataclass(frozen=True)
class RecentTrace:
    id: int
    created_at: datetime
    handles: frozenset[str]
    followed: set[str] = field(default_factory=set)
    used: set[str] = field(default_factory=set)


_RECENT: WeakKeyDictionary[Store, OrderedDict[int, deque[RecentTrace]]] = (
    WeakKeyDictionary()
)
_LOCK = Lock()


def remember(
    store: Store, ident: int, created_at: datetime, meta: dict[str, Any]
) -> None:
    session = meta.get("session")
    if not isinstance(session, int):
        return
    entry = RecentTrace(
        ident,
        created_at,
        frozenset(c["handle"] for c in meta["candidates"]),
    )
    with _LOCK:
        sessions = _RECENT.setdefault(store, OrderedDict())
        sessions.setdefault(session, deque(maxlen=MAX_TRACES)).append(entry)
        sessions.move_to_end(session)
        while len(sessions) > MAX_SESSIONS:
            sessions.popitem(last=False)


def _address(value: Any, kind: str | None = None) -> set[str]:
    """Recognize canonical handles/numeric ids without a resolution query."""
    if isinstance(value, (list, tuple)):
        return set().union(*(_address(v, kind) for v in value))
    if not isinstance(value, (str, int)):
        return set()
    raw = str(value).strip()
    if ":" in raw:
        kind, _, raw = raw.partition(":")
    if raw.isdigit() and kind:
        handle = handle_registry.try_format(kind, int(raw))
        return {handle} if handle else set()
    normalized = handle_registry.normalize(raw)
    parsed = handle_registry.parse(normalized)
    if parsed is None:
        return set()
    found_kind, chunk, ident = parsed
    return {handle_registry.format_handle(found_kind, ident, chunk=chunk)}


def _signals(verb: str, args: dict[str, Any]) -> tuple[str, set[str]]:
    payload = {**(args.get("__extras__") or {}), **args}
    if verb == "get":
        return "followed", _address(payload.get("id"), payload.get("kind"))
    if verb == "link" and (payload.get("mode") or "add") == "add":
        return "used", _address(payload.get("id"), payload.get("kind")) | _address(
            payload.get("target")
        )
    if verb in {"put", "edit"} and not payload.get("dry_run"):
        texts = _body_texts(payload)
        return "used", set().union(
            *(
                _address(token)
                for text in texts
                for token in _universal_handle_tokens(text)
            )
        )
    return "used", set()


def _body_texts(payload: dict[str, Any]) -> list[str]:
    texts = [
        value for key in ("text", "body") if isinstance(value := payload.get(key), str)
    ]
    for operation in payload.get("ops") or []:
        if isinstance(operation, dict):
            texts.extend(_body_texts(operation))
    return texts


def record(store: Store, session: int | None, verb: str, args: dict[str, Any]) -> None:
    """Attribute a successful call, with no reads and at most one UPDATE."""
    try:
        if (
            session is None
            or verb not in {"get", "link", "put", "edit"}
            or os.environ.get("PRECIS_SEARCH_TRACES", "1").strip().lower()
            in {"0", "off", "false", "no"}
        ):
            return
        cutoff = datetime.now(UTC) - WINDOW
        with _LOCK:
            sessions = _RECENT.get(store)
            entries = sessions.get(session) if sessions is not None else None
            if entries is None:
                return
            while entries and entries[0].created_at < cutoff:
                entries.popleft()
            recent = list(entries)
        if not recent:
            return
        field, handles = _signals(verb, args)
        with _LOCK:
            matches = {
                entry.id: sorted(new)
                for entry in recent
                if (new := (entry.handles & handles) - getattr(entry, field))
            }
        if not matches:
            return
        with store.pool.connection(timeout=0.005) as conn, conn.transaction():
            conn.execute("SET LOCAL statement_timeout = '20ms'")
            conn.execute("SET LOCAL lock_timeout = '5ms'")
            conn.execute(
                "UPDATE refs r SET meta = jsonb_set(r.meta, ARRAY[%s], "
                "(SELECT jsonb_agg(DISTINCT h ORDER BY h) "
                "FROM jsonb_array_elements("
                "COALESCE(r.meta->%s, '[]'::jsonb) || feedback.value) h)) "
                "FROM jsonb_each(%s::jsonb) feedback "
                "WHERE r.ref_id = feedback.key::bigint AND r.kind = 'agentlog' "
                "AND r.meta->>'trace' = 'search' AND r.retired_at IS NULL "
                "AND r.meta->>'session' = %s "
                "AND r.created_at >= now() - interval '30 minutes'",
                (field, field, json.dumps(matches), str(session)),
            )
        with _LOCK:
            for entry in recent:
                getattr(entry, field).update(matches.get(entry.id, []))
    except Exception:
        log.debug("search trace feedback skipped", exc_info=True)

"""Creation-time origin for numeric refs, independent of their coarse set_by.

Only the call's existing identity fields are copied. Client models are
declarations, never guesses from a client name; agentlog models keep their
recorded value. Reads are independent and best-effort, with a savepoint for
the optional agentlog lookup so a failed query cannot poison the filing.
Caller metadata cannot supply this stamp. The read-only gripe definer cannot
persist it without a migration, so that path retains its existing tags.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

from precis import agentlog, tool_ledger
from precis.store.revision_context import current_revision_context
from precis.utils.secret_scan import mask_secrets

log = logging.getLogger(__name__)

# Omit oversized identities rather than truncating them into different ones.
_MAX_VALUE_LENGTH = 512
_MAX_INTEGER = 10**_MAX_VALUE_LENGTH


def _actor() -> str | None:
    context = current_revision_context()
    return context.actor if context else None


def _read(read: Callable[[], Any]) -> str | int | None:
    """Keep bounded, JSONB-safe scalars verbatim; omit credential-shaped values."""
    try:
        value = read()
        if isinstance(value, str) and value:
            if len(value) > _MAX_VALUE_LENGTH or "\x00" in value:
                return None
            # JSON escaping alone accepts lone surrogates; PostgreSQL does not.
            value.encode("utf-8")
            return value if mask_secrets(value, warn=False) == value else None
        # Bound before decimal serialization, which can itself reject huge ints.
        if type(value) is int and 0 < value < _MAX_INTEGER:
            return value
    except Exception:
        log.debug("filing origin field unavailable", exc_info=True)
    return None


def agentlog_model(
    pool: ConnectionPool, agentlog_id: int, *, conn: Connection | None = None
) -> str | None:
    """Recorded actual model, else requested model, from this run only."""
    try:
        with nullcontext(conn) if conn is not None else pool.connection() as c:
            with c.transaction():
                row = c.execute(
                    "SELECT meta->>'model_actual', meta->>'model' FROM refs "
                    "WHERE ref_id = %s AND kind = 'agentlog'",
                    (agentlog_id,),
                ).fetchone()
        if row:
            value = _read(lambda: row[0] or row[1])
            return value if isinstance(value, str) else None
    except Exception:
        log.debug("filing origin agentlog model unavailable", exc_info=True)
    return None


def current_filed_by(
    pool: ConnectionPool, *, conn: Connection | None = None
) -> dict[str, Any]:
    """Capture known fields only; no session means no invented client or model."""
    readers: dict[str, Callable[[], Any]] = {
        "actor": _actor,
        "client": tool_ledger.current_client,
        "session_id": tool_ledger._session_id,
        "source": agentlog.current_source_from_env,
        "agentlog_id": agentlog.current_from_env,
    }
    origin: dict[str, Any] = {
        key: value for key, read in readers.items() if (value := _read(read))
    }
    model = _read(tool_ledger.current_client_model)
    if model:
        origin.update(model=model, model_source="declared")
    run_id = origin.get("agentlog_id")
    if isinstance(run_id, int):
        recorded = agentlog_model(pool, run_id, conn=conn)
        if recorded:
            origin.update(model=recorded, model_source="agentlog")
    return origin


def stamp_meta(
    meta: dict[str, Any] | None,
    pool: ConnectionPool,
    *,
    conn: Connection | None = None,
) -> dict[str, Any]:
    """Replace any carried stamp with this creation's independently read origin."""
    result: dict[str, Any] = dict(meta or {})
    result.pop("filed_by", None)
    origin = current_filed_by(pool, conn=conn)
    if origin:
        result["filed_by"] = origin
    return result

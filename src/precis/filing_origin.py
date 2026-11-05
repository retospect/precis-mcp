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
    pool: ConnectionPool,
    *,
    conn: Connection | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Capture known fields only; no session means no invented client or model.

    The active context's ``writer`` stamp wins, so a creation records the same
    attribution as its revision. ``actor`` names the writer before its context
    is entered."""
    readers: dict[str, Callable[[], Any]] = {
        "actor": (lambda: actor) if actor else _actor,
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
    context = current_revision_context()
    if context is not None and context.writer:
        origin.update(context.writer)
    return origin


def writer_stamp(
    pool: ConnectionPool,
    *,
    actor: str | None,
    payload: dict[str, str] | None = None,
    direct_human: str | None = None,
) -> dict[str, Any]:
    """The ``revisions.writer`` stamp for one verb write.

    A model the caller states is a claim (``model_source='self'``), used only
    when nothing better is known. ``direct_human`` is the authenticated person
    at a human ingress; a ``human`` in the payload is an agent saying the
    content is that person's direct input."""
    origin = current_filed_by(pool, actor=actor)
    claim = payload or {}
    if claim.get("model") and "model" not in origin:
        origin.update(model=claim["model"], model_source="self")
    if direct_human:
        origin.update(human=direct_human, human_via="direct")
    elif claim.get("human"):
        origin.update(human=claim["human"], human_via="mediated")
    return origin


def writer_label(writer: dict[str, Any]) -> str:
    """One stamp as a reader sees it: ``claude-opus-5-5 (self), gardener`` or
    ``human reto via claude-opus-5-5 (self)``."""
    who = str(writer.get("model") or writer.get("actor") or "unknown")
    if writer.get("model") and writer.get("model_source") == "self":
        who += " (self)"
    human = writer.get("human")
    if human:
        if writer.get("human_via") == "direct":
            return f"human {human}"
        who = f"human {human} via {who}"
    process = writer.get("source") or str(writer.get("client") or "").split("/")[0]
    return f"{who}, {process}" if process else who


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

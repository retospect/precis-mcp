"""Full LLM interaction log — Slice 1 of the adaptive-router plan.

Records every ``router.dispatch`` call's full logical request + final response +
outcome metadata to postgres (``llm_call_log`` + content-addressed ``llm_blob``,
migration 0061), so later slices can replay requests on other models and score
the difference. Operational, **not corpus** — never embedded (peer to
:mod:`precis.agentlog` / alerts).

**Dark by construction.** The writer is best-effort over a process-bound store
(bound at worker / runtime boot, mirroring :mod:`precis.secrets`); with no store
bound (DB-free callers, tests) it is a no-op, and any write failure is swallowed
so a logging problem can never break an LLM call.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Process-bound store the best-effort writer uses. ``bind_store`` sets it at
#: worker / runtime boot; unbound → every :func:`record_call` is a no-op.
_STORE: Store | None = None

#: Retention floor for the GC valve (days). Generous — the log is the training
#: asset — but bounded so it can't grow forever. Env-overridable.
RETENTION_DAYS_ENV = "PRECIS_LLM_LOG_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 90


def bind_store(store: Store | None) -> None:
    """Bind (or clear) the process store the logger writes through."""
    global _STORE
    _STORE = store


def enabled() -> bool:
    """True when a store is bound — so a caller can skip building the record
    (serializing the full request) when logging would no-op anyway."""
    return _STORE is not None


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """One dispatch call to log: full request/response text + metadata. The
    caller (``router.dispatch``) fills this; the writer dedups the text blobs."""

    source: str | None
    tier: str | None
    transport: str | None
    model: str | None
    tools_needed: bool | None
    request_text: str
    response_text: str
    cost_usd: float | None
    turns_used: int | None
    duration_ms: int | None
    errored: bool
    error: str | None
    data_parsed: bool | None
    features: dict[str, Any] = field(default_factory=dict)
    ref_id: int | None = None


def _blob_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_call(rec: LlmCallRecord, *, store: Store | None = None) -> None:
    """Write one call record — best-effort, never raises.

    No-op when no store is bound (and none passed). Dedups the request/response
    text into ``llm_blob`` (content-addressed), then inserts the metadata row.
    A store may be passed explicitly (tests); otherwise the bound one is used.
    """
    st = store if store is not None else _STORE
    if st is None:
        return
    try:
        _write(st, rec)
    except Exception:
        log.debug("route_log: record_call failed", exc_info=True)


def _write(store: Store, rec: LlmCallRecord) -> None:
    req_hash = _blob_hash(rec.request_text)
    resp_hash = _blob_hash(rec.response_text)
    import json

    with store.pool.connection() as conn:
        for h, text in ((req_hash, rec.request_text), (resp_hash, rec.response_text)):
            conn.execute(
                "INSERT INTO llm_blob (hash, text, bytes) VALUES (%s, %s, %s) "
                "ON CONFLICT (hash) DO NOTHING",
                (h, text, len(text.encode("utf-8"))),
            )
        conn.execute(
            """
            INSERT INTO llm_call_log (
                source, tier, transport, model, tools_needed,
                request_hash, response_hash, request_chars, response_chars,
                cost_usd, turns_used, duration_ms, errored, error, data_parsed,
                ref_id, features
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s
            )
            """,
            (
                rec.source,
                rec.tier,
                rec.transport,
                rec.model,
                rec.tools_needed,
                req_hash,
                resp_hash,
                len(rec.request_text),
                len(rec.response_text),
                rec.cost_usd,
                rec.turns_used,
                rec.duration_ms,
                rec.errored,
                rec.error,
                rec.data_parsed,
                rec.ref_id,
                json.dumps(rec.features),
            ),
        )
        conn.commit()


def gc(store: Store, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete log rows past the retention window; GC orphaned blobs. Returns the
    number of call rows deleted. Wire into the sweeper when the log grows."""
    with store.pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM llm_call_log WHERE ts < now() - (%s || ' days')::interval",
            (retention_days,),
        )
        deleted = cur.rowcount
        conn.execute(
            "DELETE FROM llm_blob b WHERE NOT EXISTS ("
            "  SELECT 1 FROM llm_call_log l"
            "   WHERE l.request_hash = b.hash OR l.response_hash = b.hash)"
        )
        conn.commit()
    return deleted


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "RETENTION_DAYS_ENV",
    "LlmCallRecord",
    "bind_store",
    "enabled",
    "gc",
    "record_call",
]

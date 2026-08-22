"""Tool-call ledger — one row per ``runtime.dispatch()`` call.

Records verb / kind / input-key-set / outcome / latency to postgres
(``tool_calls``, migration 0133), written from the single chokepoint every
verb call passes through regardless of caller —
:meth:`precis.runtime.dispatch.DispatchMixin.dispatch_with_status` — so it
covers the MCP server, the CLI, and in-process agent ticks alike (unlike
``precis.tools.core._log_tool_call``'s plain-text server log, which only
sees the MCP/CLI surface).

**No payload content, ever.** ``input_keys`` carries the top-level input
kwarg *names* the caller passed — never their values, and never a nested
body. Those names come from each verb wrapper's fixed Python signature
(``kind=``, ``id=``, ``q=``, ...), not from agent-supplied strings, so
there is no channel for a value to masquerade as a key name. The one
partial exception — the ``args={...}`` extras passthrough some handlers
accept (e.g. ``random.get``'s ``len=``/``alphabet=``) — is itself a single
top-level key (``"__extras__"``) in the captured set; its *nested* keys
(themselves declared param names, validated against the handler's
signature before the call) are deliberately not flattened in, keeping the
"top-level kwargs only" contract exact.

Operational, **not corpus** — never embedded (peer to
:mod:`precis.route_log` / :mod:`precis.agentlog` / alerts).

**Dark by construction.** The writer is best-effort over the caller's
:class:`~precis.store.Store` (whatever the runtime already has bound —
this module carries no process-global store of its own); any write
failure is swallowed so a ledger problem can never break the tool call it
is measuring.

**Mining example** — "which verb/kind/arg-shape confuses agents", the
motivating query, replacing a forensic transcript-mining pass::

    SELECT verb, kind, error_type, count(*) AS n
    FROM tool_calls
    WHERE ts > now() - interval '7 days'
      AND outcome = 'error'
    GROUP BY verb, kind, error_type
    ORDER BY n DESC;

Per-profile usage / error rate (keeps ``PRECIS_MCP_PROFILE`` honest)::

    SELECT profile, verb,
           count(*) AS calls,
           count(*) FILTER (WHERE outcome = 'error') AS errors
    FROM tool_calls
    WHERE ts > now() - interval '7 days'
    GROUP BY profile, verb
    ORDER BY calls DESC;

A ``/whatneedsdoing`` step-6 confusion-mining pass should query this table
directly instead of regex-scraping ``plan_tick`` transcripts wherever the
window is covered (this table only has rows from the moment migration
0133 shipped forward — older windows still need the transcript path).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

# Known cost, accepted by design: the write is a SYNCHRONOUS single INSERT
# on the dispatch path (one pooled round-trip per verb call, even for verbs
# that otherwise touch no DB). Fail-open, never load-bearing — but if this
# ever shows up in latency profiles, the levers are sampling or a queued
# writer, not dropping the ledger.

#: Retention floor for the GC valve (days). ``tool_calls`` gets a row per
#: verb call — much higher write volume than ``llm_call_log`` — so this
#: defaults tighter than that table's 90-day window. Env-overridable.
RETENTION_DAYS_ENV = "PRECIS_TOOL_CALLS_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 30

#: Rows pruned per sweep — bounded so the first drain of a long-unpruned
#: table stays a short DELETE, mirroring ``sweeper._gc_worker_logs``'s
#: batch pattern (this table has the same shape: one insert-only row per
#: high-frequency event).
_GC_BATCH = 50_000

#: Fleet-wide advisory-lock key for :func:`gc` (ascii ``"toolgc"``). Every
#: host runs a sweeper pass; without single-flight, concurrent hosts would
#: pile up overlapping batched DELETEs on the same table — the exact
#: failure mode ``route_log.gc`` and ``sweeper._gc_worker_logs`` were both
#: fixed for. Xact-scoped: released at commit/rollback.
_GC_LOCK = 0x746F6F6C6763


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One dispatch call to log. The dispatcher fills this from what it
    already computed for the response — no extra lookups on the hot path."""

    verb: str
    #: Raw caller-supplied ``kind=`` (pre-resolution) — ``None`` when the
    #: caller omitted it (itself a useful confusion signal: "missing
    #: kind=" calls are exactly the friction this ledger exists to find).
    kind: str | None
    #: Top-level input kwarg NAMES only — never values. Sorted by the
    #: caller for a stable, dedupable JSON representation.
    input_keys: list[str]
    #: ``'ok'`` | ``'error'``.
    outcome: str
    error_type: str | None = None
    result_count: int | None = None
    latency_ms: int | None = None
    agentlog_id: int | None = None
    source: str | None = None
    profile: str | None = None


def record_call(store: Store | None, rec: ToolCallRecord) -> None:
    """Write one ledger row — best-effort, never raises.

    No-op when ``store`` is ``None`` (stateless runtime, DB-free tests).
    Any write failure (missing table on a pre-0133 DB, connection hiccup)
    is caught and logged at debug — the ledger is diagnostic, never
    load-bearing for the call it is measuring.
    """
    if store is None:
        return
    try:
        _write(store, rec)
    except Exception:
        log.debug("tool_ledger: record_call failed", exc_info=True)


def _write(store: Store, rec: ToolCallRecord) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO tool_calls (
                agentlog_id, source, profile, verb, kind, input_keys,
                outcome, error_type, result_count, latency_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                rec.agentlog_id,
                rec.source,
                rec.profile,
                rec.verb,
                rec.kind,
                json.dumps(sorted(rec.input_keys)),
                rec.outcome,
                rec.error_type,
                rec.result_count,
                rec.latency_ms,
            ),
        )
        conn.commit()


def gc(store: Store, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete ``tool_calls`` rows past the retention window, one bounded
    batch per pass. Returns rows deleted (0 when another host holds the
    single-flight lock, or nothing was aged).

    Mirrors ``sweeper._gc_worker_logs``: single-flight advisory lock +
    ``LIMIT``-bounded batch, because this table shares its shape (insert-
    only, one row per high-frequency event, fleet-wide sweeper callers).
    """
    with store.pool.connection() as conn:
        got = conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", (_GC_LOCK,)
        ).fetchone()
        if not got or not got[0]:
            return 0  # another host is already pruning — don't pile on
        cur = conn.execute(
            "DELETE FROM tool_calls WHERE call_id IN ("
            "  SELECT call_id FROM tool_calls"
            "  WHERE ts < now() - (%s || ' days')::interval"
            "  LIMIT %s"
            ")",
            (retention_days, _GC_BATCH),
        )
        deleted = cur.rowcount or 0
        conn.commit()
    return deleted


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "RETENTION_DAYS_ENV",
    "ToolCallRecord",
    "gc",
    "record_call",
]

"""Tool-call ledger — one row per ``runtime.dispatch()`` call.

Records verb / kind / input-key-set / outcome / latency to postgres
(``tool_calls``, migration 0133), written from the single chokepoint every
verb call passes through regardless of caller —
:meth:`precis.runtime.dispatch.DispatchMixin.dispatch_with_status` — so it
covers the MCP server, the CLI, and in-process agent ticks alike (unlike
``precis.tools.core._log_tool_call``'s plain-text server log, which only
sees the MCP/CLI surface).

**Bounded content only.** ``input_keys`` carries the top-level input
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

Migration 0209 relaxes "no payload content" *boundedly* (Reto ruling
2026-10-14: the ledger must include ``arg_sample``): the sampled arg
values are masked + truncated scalars, payload keys appear as lengths,
credential-shaped keys are ``<redacted>``, and ``error_head`` is one
masked first line — so a session-mining pass can see *why* a call
failed and *what shape* was sent without the ledger ever holding a raw
payload.

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
import os
import re
import socket
import threading
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

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
    #: Opaque per-MCP-session key (0209) — ``None`` for CLI / inproc /
    #: direct-dispatch calls (those correlate by ``agentlog_id``).
    session_id: str | None = None
    #: Per-``session_id`` call ordinal; ``None`` when ``session_id`` is.
    seq: int | None = None
    #: Byte length of the rendered response body (0209).
    result_bytes: int | None = None
    #: First line of the rendered error body — masked, bounded (0209).
    error_head: str | None = None
    #: Bounded JSON of scalar argument values — masked, truncated,
    #: credential-shaped keys dropped (0209).
    arg_sample: str | None = None


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
                outcome, error_type, result_count, latency_ms,
                session_id, seq, result_bytes, error_head, arg_sample
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s)
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
                rec.session_id,
                rec.seq,
                rec.result_bytes,
                rec.error_head,
                rec.arg_sample,
            ),
        )
        conn.commit()


# ── session_id / seq (migration 0209) ─────────────────────────────────────
#
# ``session_id`` is minted once per bound MCP-session object — the same
# object ``serve_ledger`` already keys its per-call binding on, so the two
# can't disagree about which session a call belongs to. A WeakKeyDictionary
# means the entry dies with the session; ``id(session)`` is deliberately
# NOT the fallback because CPython reuses ids after GC (two sessions, one
# id = corrupted adjacency). ``seq`` is the per-session call ordinal —
# gaps are fine (a failed ledger write skips a number; ordering is all
# mining needs).

_SESSION_IDS: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
_SEQ_COUNTERS: dict[str, int] = {}
_SESSION_LOCK = threading.Lock()


def current_session_key() -> tuple[str | None, int | None]:
    """``(session_id, seq)`` for the currently-bound MCP session, or
    ``(None, None)`` outside one (CLI, inproc ticks, tests)."""
    try:
        from precis import serve_ledger

        session = serve_ledger.current_session()
    except Exception:
        return None, None
    if session is None:
        return None, None
    with _SESSION_LOCK:
        try:
            sid = _SESSION_IDS.get(session)
        except TypeError:
            sid = None
        if sid is None:
            sid = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:10]}"
            try:
                _SESSION_IDS[session] = sid
            except TypeError:
                # Unweakrefable session object — fall back to identity,
                # namespaced by pid so a recycled id can't alias a dead
                # session of another process.
                sid = f"{socket.gethostname()}-{os.getpid()}-{id(session):x}"
        n = _SEQ_COUNTERS.get(sid, 0) + 1
        _SEQ_COUNTERS[sid] = n
        return sid, n


# ── arg_sample / error_head / result_bytes (migration 0209) ──────────────

#: Per string value in the sample — enough to see a q= shape, never a body.
_SAMPLE_STR_CHARS = 80
#: Whole-arg_sample JSON budget — a call's arg shape, not its payload.
_SAMPLE_TOTAL_CHARS = 2000
#: Max scalar entries inside one list / dict in the sample.
_SAMPLE_ITEMS = 5
#: Keys whose values are agent-authored free text — recorded as
#: ``"<N chars>"`` so the sample still names the arg without carrying
#: its body. ``title=`` is payload the same way ``text=`` is (the
#: no-leak ledger test treats it as such).
_SAMPLE_PAYLOAD_KEYS = frozenset(
    {
        "text",
        "body",
        "content",
        "prompt",
        "command",
        "title",
        "detail",
        "message",
        "note",
        "reason",
    }
)
#: Keys that name credentials by shape — replaced with ``<redacted>``
#: before the value is even looked at (mask_secrets on the value is the
#: second line of defence, not the first).
_SECRETISH_KEY_RE = re.compile(
    r"(?i)(secret|token|password|passwd|api[_-]?key|authorization|credential)"
)


def _sample_value(v: Any, depth: int) -> Any:
    """One JSON-safe, bounded, masked rendering of an argument value."""
    from precis.utils.secret_scan import mask_secrets

    if v is None or isinstance(v, bool | int | float):
        return v
    if isinstance(v, str):
        s = " ".join(v.split())  # single-line, TSV-safe
        s = mask_secrets(s, warn=False)
        return s[:_SAMPLE_STR_CHARS] + ("…" if len(s) > _SAMPLE_STR_CHARS else "")
    if isinstance(v, (list, tuple)):
        if depth >= 2 or not v:
            return f"<{len(v)} items>" if v else []
        return [_sample_value(e, depth + 1) for e in v[:_SAMPLE_ITEMS]] + (
            ["…"] if len(v) > _SAMPLE_ITEMS else []
        )
    if isinstance(v, dict):
        # Dict args other than the extras passthrough are payload-shaped
        # (meta=, options=) — their key NAMES are the shape signal, not
        # their values.
        return f"<dict, {len(v)} keys>"
    return f"<{type(v).__name__}>"


def arg_sample(args: dict[str, Any]) -> str | None:
    """Bounded JSON of a call's argument values — the "what shape did the
    agent actually send" payload the ledger lacked (0209).

    Scalars pass through masked + truncated; ``text=``/``body=`` payloads
    are summarized by length; credential-shaped keys are ``<redacted>``
    outright. Keys are added in sorted order until the ~2KB budget runs
    out so the JSON is always parseable — never truncated mid-value.
    Returns ``None`` when ``args`` is empty.
    """
    if not args:
        return None
    out: dict[str, Any] = {}
    used = 2  # the braces
    for k in sorted(str(k) for k in args):
        v = args.get(k)
        if v is None:
            continue  # wrapper defaults aren't caller input (input_keys rule)
        if _SECRETISH_KEY_RE.search(k):
            sv: Any = "<redacted>"
        elif k in _SAMPLE_PAYLOAD_KEYS:
            sv = f"<{len(v)} chars>" if isinstance(v, str) else f"<{type(v).__name__}>"
        elif k == "meta" and isinstance(v, dict):
            # meta key names are the shape (meta.job_type is a group_by
            # target); meta VALUES are payload — never sampled.
            keys = sorted(str(x) for x in v)[:8]
            sv = "<dict: " + ",".join(keys) + ("…" if len(v) > 8 else "") + ">"
        elif k in ("__extras__", "args") and isinstance(v, dict):
            # The extras passthrough holds declared scalar params
            # (random.get's len=/alphabet=) — sample one level down,
            # secretish keys redacted there too.
            sv = {}
            for i, (ek, ev) in enumerate(v.items()):
                if i >= _SAMPLE_ITEMS:
                    sv["…"] = f"{len(v) - _SAMPLE_ITEMS} more keys"
                    break
                ek_s = str(ek)
                sv[ek_s] = (
                    "<redacted>"
                    if _SECRETISH_KEY_RE.search(ek_s)
                    else _sample_value(ev, 1)
                )
        else:
            sv = _sample_value(v, 0)
        piece = len(json.dumps(k)) + len(json.dumps(sv, ensure_ascii=False)) + 2
        if used + piece > _SAMPLE_TOTAL_CHARS:
            break
        out[k] = sv
        used += piece
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def error_head(body: str) -> str | None:
    """First line of a rendered error body — whitespace-collapsed,
    masked, bounded. The failure-family signature for mining."""
    from precis.utils.secret_scan import mask_secrets

    line = " ".join(body.split("\n", 1)[0].split())
    if not line:
        return None
    return mask_secrets(line, warn=False)[:200]


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
    "arg_sample",
    "current_session_key",
    "error_head",
    "gc",
    "record_call",
]

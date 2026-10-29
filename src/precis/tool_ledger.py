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

Migration 0210 adds caller attribution (gr484070): ``client`` records
the MCP client's self-declared ``clientInfo`` (``name/version``, plus a
``_meta`` model id when sent) — verbatim, ``NULL`` when there is no
bound session or no declaration.

``result_count`` — 0133's reserved column — is populated from
:attr:`precis.response.Response.result_count`, which collection-rendering
code paths set (search/list/count views, batch gets, links/log views on
the numeric-ref family, ``utils.search_merge.merge_and_render``);
``None`` where the response doesn't enumerate a result set. Correlation:
``session_id``/``seq`` bind MCP-session calls; ``agentlog_id``/``source``
cover the in-process agent loop via ``LlmRequest.env_overlay`` →
:func:`precis.utils.llm.router._overlay_tick_context` (the spawned-claude
path's env back-doors have no subprocess on that transport). ``source`` is
the PRODUCING JOB (``LlmRequest.source``: ``dream``, ``plan_tick``, …) from
``PRECIS_CURRENT_SOURCE``, never the model tier; a containerized
``claude -p`` gets both vars via ``agent_container.RUN_CONTEXT_ENV_KEYS``.

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

from precis import worktree_server

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
    #: Items the response enumerated (:attr:`Response.result_count`) —
    #: 0133's "reserved" column, populated since the handlers carry it.
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
    #: The MCP client's own declaration — ``clientInfo`` ``name/version``
    #: from the initialize handshake, plus `` (model:<id>)`` when the
    #: client sent a model id in the initialize ``_meta`` (0210,
    #: gr484070). ``None`` for CLI / inproc / direct-dispatch calls and
    #: for clients that send no clientInfo. Stored verbatim, never
    #: inferred — "which client keeps tripping on this call" is the
    #: mining question this answers.
    client: str | None = None


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
                session_id, seq, result_bytes, error_head, arg_sample,
                client
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s)
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
                rec.client,
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


def _session_id() -> str | None:
    """The currently-bound session's ledger id, minted on first use —
    ``None`` outside a session (CLI, inproc ticks, tests)."""
    try:
        from precis import serve_ledger

        session = serve_ledger.current_session()
    except Exception:
        return None
    if session is None:
        return None
    with _SESSION_LOCK:
        try:
            sid = _SESSION_IDS.get(session)
        except TypeError:
            sid = None
        if sid is None:
            # ``wt-<tree>-`` leads on a per-worktree server so its rows
            # sort apart from the shared server's (precis.worktree_server).
            head = f"{worktree_server.session_prefix()}{socket.gethostname()}"
            sid = f"{head}-{os.getpid()}-{uuid4().hex[:10]}"
            try:
                _SESSION_IDS[session] = sid
            except TypeError:
                # Unweakrefable session object — fall back to identity,
                # namespaced by pid so a recycled id can't alias a dead
                # session of another process.
                sid = f"{head}-{os.getpid()}-{id(session):x}"
        return sid


def current_session_key() -> tuple[str | None, int | None]:
    """PEEK at the ``(session_id, seq)`` the NEXT ledger row for this
    session will take — reads without consuming. Code that must name
    the in-flight call's ledger row (e.g. ``precis_se``'s op-trace
    frames) uses this; the ledger's own write consumes the same value
    via :func:`_mint_session_key`, so both agree on the seq.
    ``(None, None)`` outside a session."""
    sid = _session_id()
    if sid is None:
        return None, None
    with _SESSION_LOCK:
        return sid, _SEQ_COUNTERS.get(sid, 0) + 1


def _mint_session_key() -> tuple[str | None, int | None]:
    """CONSUME the next ``(session_id, seq)`` — the tool ledger's one
    writer; everything else peeks via :func:`current_session_key`."""
    sid = _session_id()
    if sid is None:
        return None, None
    with _SESSION_LOCK:
        n = _SEQ_COUNTERS.get(sid, 0) + 1
        _SEQ_COUNTERS[sid] = n
        return sid, n


# ── client attribution (migration 0210, gr484070) ────────────────────────
#
# The client's self-declared identity, read off the bound MCP session's
# initialize params. ``session.client_params`` is fixed for the life of
# the session, so this is a plain attribute read — no caching needed.

#: ``_meta`` keys a client may use to declare the model it runs
#: (``InitializeRequestParams.Meta`` is ``extra="allow"`` — anything
#: here is client-supplied, recorded verbatim).
_CLIENT_MODEL_KEYS = ("model", "model_id", "modelId", "llm")
#: Bound on the rendered label — clientInfo is agent-supplied text.
_CLIENT_CHARS = 200


def current_client() -> str | None:
    """``name/version`` (+ `` (model:<id>)``) for the currently-bound MCP
    session's clientInfo, or ``None`` outside a session / when the client
    declared none."""
    try:
        from precis import serve_ledger

        session = serve_ledger.current_session()
    except Exception:
        return None
    if session is None:
        return None
    try:
        # ServerSession.client_params — InitializeRequestParams | None.
        # ``session`` is typed ``object`` (the binding is transport-
        # agnostic), so the attribute is reached by getattr, not annotation.
        params = getattr(session, "client_params", None)
    except Exception:
        return None
    if params is None:
        return None
    info = getattr(params, "clientInfo", None)
    name = getattr(info, "name", None)
    if not name:
        return None
    version = getattr(info, "version", None)
    label = f"{name}/{version}" if version else str(name)
    model = _client_model(getattr(params, "meta", None))
    if model:
        label = f"{label} (model:{model})"
    return label[:_CLIENT_CHARS]


def _client_model(meta: Any) -> str | None:
    """The model id the client declared in initialize ``_meta``, if any."""
    if meta is None:
        return None
    for key in _CLIENT_MODEL_KEYS:
        v = getattr(meta, key, None)
        if isinstance(v, str) and v:
            return " ".join(v.split())[:80]
    return None


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
        if not s:
            # Explicitly-sent empty strings get the same marker payload
            # keys produce (gr503460 forensics): without it, "" and an
            # omitted key are indistinguishable in the ledger.
            return "<0 chars>"
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


# ── agent-facing reads (gr520238 ask 3) ────────────────────────────────
#
# Until these the ledger was write-only: "which views did that run call"
# needed a prod-psql regex over job transcripts. The agentlog surface
# exposes three shapes — ``get(kind='agentlog', id=N, view='calls')`` reads
# one run's own calls, ``get(kind='agentlog', id='/calls?…')`` is the
# cross-run mining view (GROUP BY + the newest matching rows), and
# ``get(kind='agentlog', id='/sessions?…')`` is the per-session roll-up
# (:func:`sessions_rollup`, block-navigation slice 2). arg_sample
# / error_head are already masked + bounded at write time, so rendering
# them here carries no payload.

#: Default / cap rows a calls read returns — same posture as job /logs.
CALLS_DEFAULT_LIMIT = 100
CALLS_MAX_LIMIT = 200
#: Default look-back window for the mining view (hours); the cap matches
#: the ledger's retention so a wider ask can't seq-scan dead history.
CALLS_DEFAULT_SINCE_HOURS = 168
CALLS_MAX_SINCE_HOURS = DEFAULT_RETENTION_DAYS * 24

_CALL_COLS = (
    "call_id, ts, agentlog_id, source, profile, verb, kind, input_keys, "
    "outcome, error_type, result_count, latency_ms, session_id, seq, "
    "result_bytes, error_head, arg_sample, client"
)


def calls_for_agentlog(
    store: Store, agentlog_id: int, *, limit: int = CALLS_DEFAULT_LIMIT
) -> tuple[int, list[dict[str, Any]]]:
    """``(total, rows)`` — one run's own calls, oldest-first, capped to
    the newest ``limit``. The ``view='calls'`` read on a concrete
    agentlog."""
    from psycopg.rows import dict_row

    with store.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        total_row = cur.execute(
            "SELECT count(*) FROM tool_calls WHERE agentlog_id = %s",
            (int(agentlog_id),),
        ).fetchone()
        rows = cur.execute(
            f"SELECT {_CALL_COLS} FROM tool_calls WHERE agentlog_id = %s "
            "ORDER BY ts DESC, call_id DESC LIMIT %s",
            (int(agentlog_id), int(limit)),
        ).fetchall()
    rows.reverse()
    return (int(total_row["count"]) if total_row else 0), rows


def calls_mining(
    store: Store,
    *,
    verb: str | None = None,
    kind: str | None = None,
    view: str | None = None,
    source: str | None = None,
    agentlog: int | None = None,
    errors: bool = False,
    since_hours: int = CALLS_DEFAULT_SINCE_HOURS,
    limit: int = CALLS_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """The cross-run ``/calls`` read: matching-row count, the
    ``GROUP BY (verb, kind, arg_sample->>'view')`` confusion-mining
    summary, and the newest ``limit`` matching rows (oldest-first for
    reading). Every filter binds as a parameter — never formatted."""
    from datetime import UTC, datetime, timedelta

    from psycopg.rows import dict_row

    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
    where = ["ts >= %(cutoff)s"]
    sql_params: dict[str, Any] = {"cutoff": cutoff}
    if verb:
        where.append("verb = %(verb)s")
        sql_params["verb"] = verb
    if kind:
        where.append("kind = %(kind)s")
        sql_params["kind"] = kind
    if view:
        where.append("arg_sample->>'view' = %(view)s")
        sql_params["view"] = view
    if source:
        where.append("source = %(source)s")
        sql_params["source"] = source
    if agentlog is not None:
        where.append("agentlog_id = %(agentlog)s")
        sql_params["agentlog"] = int(agentlog)
    if errors:
        where.append("outcome = 'error'")
    where_sql = " AND ".join(where)
    with store.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        total_row = cur.execute(
            f"SELECT count(*) AS n FROM tool_calls WHERE {where_sql}",
            sql_params,
        ).fetchone()
        groups = cur.execute(
            "SELECT verb, kind, arg_sample->>'view' AS view, count(*) AS n, "
            "count(*) FILTER (WHERE outcome = 'error') AS errors "
            f"FROM tool_calls WHERE {where_sql} "
            "GROUP BY verb, kind, arg_sample->>'view' "
            "ORDER BY n DESC, verb, kind LIMIT 25",
            sql_params,
        ).fetchall()
        rows = cur.execute(
            f"SELECT {_CALL_COLS} FROM tool_calls WHERE {where_sql} "
            "ORDER BY ts DESC, call_id DESC LIMIT %(limit)s",
            {**sql_params, "limit": int(limit)},
        ).fetchall()
    rows.reverse()
    return {
        "total": int(total_row["n"]) if total_row else 0,
        "groups": groups,
        "rows": rows,
    }


def sessions_rollup(
    store: Store,
    *,
    verb: str | None = None,
    session: str | None = None,
    since_hours: int = CALLS_DEFAULT_SINCE_HOURS,
    limit: int = CALLS_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Per-session roll-up of the ledger — the block-navigation slice-2
    meter ("hops, bytes and ``more`` calls per session",
    ``docs/backlog/block-navigation.md``).

    One row per ``session_id`` (0209's key; NULL-session rows — CLI,
    inproc — correlate by ``agentlog_id`` instead and stay out): call
    count (``hops``), summed ``result_bytes`` (``resp_bytes``), error
    count, calls by verb, the count of *pagination continuations* —
    a ``page`` arg sampled > 1, the only continuation signal the
    ledger can see — and the wall span ``first_ts``→``last_ts``.
    Ordered most-recently-active first, capped at ``limit`` sessions.
    ``session`` is an exact prefix match (session ids are long);
    ``verb`` narrows both which sessions appear and what is counted.
    """
    from datetime import UTC, datetime, timedelta

    from psycopg.rows import dict_row

    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
    where = ["session_id IS NOT NULL", "ts >= %(cutoff)s"]
    sql_params: dict[str, Any] = {"cutoff": cutoff, "limit": int(limit)}
    if verb:
        where.append("verb = %(verb)s")
        sql_params["verb"] = verb
    if session:
        # ``starts_with`` — a literal prefix, so a session-id fragment
        # can't smuggle LIKE wildcards.
        where.append("starts_with(session_id, %(session)s)")
        sql_params["session"] = session
    where_sql = " AND ".join(where)
    with store.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        total_row = cur.execute(
            "SELECT count(*) AS n FROM ("
            f"SELECT 1 FROM tool_calls WHERE {where_sql} GROUP BY session_id) s",
            sql_params,
        ).fetchone()
        rows = cur.execute(
            "SELECT session_id, count(*) AS hops, "
            "sum(result_bytes) AS resp_bytes, "
            "count(*) FILTER (WHERE outcome = 'error') AS errors, "
            "count(*) FILTER (WHERE CASE WHEN arg_sample->>'page' "
            "~ '^[0-9]{1,9}$' THEN (arg_sample->>'page')::int "
            "END > 1) AS continuations, "
            "min(ts) AS first_ts, max(ts) AS last_ts, "
            "EXTRACT(EPOCH FROM max(ts) - min(ts))::int AS span_seconds "
            f"FROM tool_calls WHERE {where_sql} "
            "GROUP BY session_id ORDER BY max(ts) DESC LIMIT %(limit)s",
            sql_params,
        ).fetchall()
        sids = [r["session_id"] for r in rows]
        verb_counts: dict[str, dict[str, int]] = {sid: {} for sid in sids}
        if sids:
            verb_where = ["session_id = ANY(%(sids)s)", "ts >= %(cutoff)s"]
            if verb:
                verb_where.append("verb = %(verb)s")
            for vr in cur.execute(
                "SELECT session_id, verb, count(*) AS n FROM tool_calls "
                f"WHERE {' AND '.join(verb_where)} GROUP BY session_id, verb",
                {**sql_params, "sids": sids},
            ).fetchall():
                verb_counts[vr["session_id"]][vr["verb"]] = int(vr["n"])
        for r in rows:
            r["calls_by_verb"] = verb_counts[r["session_id"]]
            r["resp_bytes"] = int(r["resp_bytes"] or 0)
            r["continuations"] = int(r["continuations"])
    return {
        "total": int(total_row["n"]) if total_row else 0,
        "rows": rows,
    }


def call_line(row: dict[str, Any]) -> str:
    """One ledger row as the agentlog surface renders it::

        2026-10-27T08:52:56Z agentlog 512345 get kind='paper' id='pa1' view='toc' → ok 137ms
        … search kind='job' → ERROR BadInput ([error:BadInput] kind …)

    ``arg_sample`` was masked + bounded at write time, so it renders
    verbatim; ``kind=``/``id=``/``view=`` lead, the rest sort after."""
    from precis.utils.timeutil import as_utc

    ts = as_utc(row.get("ts"))
    stamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts is not None else "?"
    head = [stamp]
    if row.get("agentlog_id") is not None:
        head.append(f"agentlog {row['agentlog_id']}")
    head.append(str(row.get("verb") or "?"))

    items: list[tuple[str, Any]] = []
    sample = row.get("arg_sample")
    if isinstance(sample, dict):
        seen = {k for k in ("kind", "id", "view") if k in sample}
        items = [(k, sample[k]) for k in ("kind", "id", "view") if k in sample]
        items += sorted((k, v) for k, v in sample.items() if k not in seen)
    if row.get("kind") and not any(k == "kind" for k, _ in items):
        items.insert(0, ("kind", row["kind"]))
    args = " ".join(
        f"{k}={v!r}" if isinstance(v, str) else f"{k}={json.dumps(v)}" for k, v in items
    )

    if row.get("outcome") == "error":
        tail = f"→ ERROR {row.get('error_type') or 'error'}"
        if row.get("error_head"):
            tail += f" ({row['error_head']})"
    else:
        lat = row.get("latency_ms")
        tail = "→ ok" + (f" {int(lat)}ms" if isinstance(lat, int | float) else "")
    args_part = f" {args}" if args else ""
    return f"{' '.join(head)}{args_part} {tail}"


__all__ = [
    "CALLS_DEFAULT_LIMIT",
    "CALLS_DEFAULT_SINCE_HOURS",
    "CALLS_MAX_LIMIT",
    "CALLS_MAX_SINCE_HOURS",
    "DEFAULT_RETENTION_DAYS",
    "RETENTION_DAYS_ENV",
    "ToolCallRecord",
    "arg_sample",
    "call_line",
    "calls_for_agentlog",
    "calls_mining",
    "current_client",
    "current_session_key",
    "error_head",
    "gc",
    "record_call",
    "sessions_rollup",
]

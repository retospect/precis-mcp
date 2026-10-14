"""Devin CLI session adapter: the sessions.db chat forest as a corpus.

Devin sessions live in ``~/.local/share/devin/cli/sessions.db`` — a real
local corpus, and the one that was missing. ``sessions`` gives the thread
id (a slug like ``circular-tarragon``), cwd and timestamps;
``message_nodes`` is the ordered chat forest: ``assistant`` messages carry
``tool_calls`` blocks (``id``/``name``/``arguments``), ``tool`` messages
carry the result text joined back by ``tool_call_id``, and
``tool_call_state.tool_call_update_json.status`` ('completed' | 'failed')
is the authoritative outcome flag — the tool message itself has no error
field.

Precis MCP calls appear as ``name='mcp_call_tool'`` with
``arguments={'server_name': 'precis', 'tool_name': <verb>, 'arguments':
{...payload...}}`` — verb/kind/arg_keys come from the *nested* arguments,
which is also what makes a non-precis MCP server visible (server_name !=
'precis' stays a plain tool call).

``is_error`` prefers the ``failed`` status; the result text's
``[error:<Type>]`` marker fills ``err_type``. Error classification here is
structured (status + marker), never prose-sniffing — same contract as the
codex adapter's "do not infer tool errors from quoted strings".

The DB is opened ``mode=ro``: the CLI is usually writing the WAL live
(there is almost always a session in flight — the one running the miner),
and a mining pass must never lock it. Everything the adapter writes goes
through ``redact.scrub`` on the write side, per README's contract.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import redact
import schema
from schema import Event

_ERR_TYPE_RE = re.compile(r"\[error:(\w+)\]")


def _iso(ts: int | float | None) -> str | None:
    if not ts:
        return None
    try:
        return (
            datetime.fromtimestamp(float(ts), UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _tool_identity(name: str, args: dict) -> tuple[str, str | None, str | None, dict]:
    """Map a Devin tool call to (tool, verb, kind, payload-args).

    ``mcp_call_tool`` is the MCP gateway: the real call is one level down
    (``tool_name`` + nested ``arguments``). Only ``server_name='precis'``
    resolves a verb — other servers' calls stay opaque tools, the same
    shape the codex adapter gives non-precis calls.
    """
    if name == "mcp_call_tool" and args.get("server_name") == "precis":
        inner = args.get("arguments")
        payload = inner if isinstance(inner, dict) else {}
        tool_name = args.get("tool_name")
        verb = tool_name if tool_name in schema.VERBS else None
        kind = payload.get("kind")
        return (
            name,
            verb,
            kind if isinstance(kind, str) else None,
            payload,
        )
    return name, None, None, args


def _iter_session_rows(
    conn: sqlite3.Connection, session_id: str
) -> Iterable[tuple[str, int | None]]:
    yield from conn.execute(
        "SELECT chat_message, created_at FROM message_nodes "
        "WHERE session_id = ? ORDER BY row_id",
        (session_id,),
    )


def _update_statuses(
    conn: sqlite3.Connection, session_id: str, coverage: Counter
) -> dict[str, str]:
    """tool_call_id -> 'completed'|'failed' from tool_call_state."""
    out: dict[str, str] = {}
    for tool_call_id, update_json in conn.execute(
        "SELECT tool_call_id, tool_call_update_json FROM tool_call_state "
        "WHERE session_id = ? AND tool_call_update_json IS NOT NULL",
        (session_id,),
    ):
        try:
            status = json.loads(update_json).get("status")
        except (TypeError, ValueError):
            coverage["malformed_update_json"] += 1
            continue
        if isinstance(status, str):
            out[tool_call_id] = status
    return out


def _parse_session(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    cwd: str | None,
    coverage: Counter,
) -> list[Event]:
    events: list[Event] = []
    pending: dict[str, Event] = {}
    statuses = _update_statuses(conn, session_id, coverage)
    seq = 0

    def emit(**kw) -> Event:
        nonlocal seq
        seq += 1
        ev = Event(
            corpus="devin", session=f"devin:{session_id}", seq=seq, cwd=cwd, **kw
        )
        events.append(ev)
        return ev

    for chat_json, created_at in _iter_session_rows(conn, session_id):
        try:
            msg = json.loads(chat_json)
        except (TypeError, ValueError):
            coverage["malformed_rows"] += 1
            continue
        if not isinstance(msg, dict):
            coverage["malformed_rows"] += 1
            continue
        role = msg.get("role")
        ts = _iso(created_at)

        if role == "assistant":
            text = _content_text(msg.get("content"))
            emit(ts=ts, result_head=redact.scrub(text)[: schema.HEAD_CHARS])
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                name = tc.get("name")
                args = tc.get("arguments")
                if not isinstance(tc_id, str) or not isinstance(name, str):
                    continue
                args = args if isinstance(args, dict) else {}
                tool, verb, kind, payload = _tool_identity(name, args)
                ev = emit(
                    ts=ts,
                    tool=tool,
                    verb=verb,
                    kind=kind,
                    arg_keys=tuple(sorted(payload)),
                    arg_digest=redact.scrub(
                        json.dumps(payload, sort_keys=True, ensure_ascii=False)
                    )[: schema.HEAD_CHARS],
                )
                pending[tc_id] = ev
        elif role == "tool":
            tc_id = msg.get("tool_call_id")
            call = pending.pop(tc_id, None) if isinstance(tc_id, str) else None
            if call is None:
                coverage["orphan_results"] += 1
                continue
            text = _content_text(msg.get("content"))
            call.result_bytes = len(text.encode("utf-8"))
            call.result_head = redact.scrub(text)[: schema.HEAD_CHARS]
            status = statuses.get(tc_id) if isinstance(tc_id, str) else None
            if status == "failed" or (
                text.startswith("MCP tool '") and "returned an error" in text[:80]
            ):
                call.is_error = True
                call.err_head = call.result_head
                m = _ERR_TYPE_RE.search(text)
                if m:
                    call.err_type = m.group(1)
            coverage["joined_results"] += 1
        elif role == "user":
            text = _content_text(msg.get("content"))
            if text:
                emit(
                    ts=ts,
                    tool="__user__",
                    result_bytes=len(text.encode("utf-8")),
                    result_head=redact.scrub(text)[: schema.HEAD_CHARS],
                )
        # 'system' and unknown roles carry nothing the schema models.

    coverage["calls_without_result"] += len(pending)
    return events


def extract(
    db_path: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> tuple[list[Event], Counter]:
    """Every session touched inside [since, until), events in chat order."""
    coverage: Counter[str] = Counter()
    events: list[Event] = []
    if not db_path.exists():
        coverage["db_missing"] += 1
        return events, coverage
    # Read-only URI: the CLI is typically mid-session and the WAL must not
    # be locked by a mining pass.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, working_directory, created_at, last_activity_at "
            "FROM sessions ORDER BY last_activity_at DESC"
        ).fetchall()
        since_ts = since.timestamp() if since else None
        until_ts = until.timestamp() if until else None
        picked: list[tuple[str, str | None]] = []
        for sid, cwd, created_at, last_at in rows:
            # A session overlaps the window iff it was alive at any point
            # inside it — last_activity_at >= since and created_at < until.
            if since_ts is not None and (last_at or 0) < since_ts:
                continue
            if until_ts is not None and (created_at or 0) >= until_ts:
                continue
            picked.append((sid, cwd))
        coverage["sessions_in_window"] = len(picked)
        if limit is not None:
            picked = picked[:limit]
        for sid, cwd in picked:
            session_events = _parse_session(conn, sid, cwd=cwd, coverage=coverage)
            in_window = [
                ev for ev in session_events if _in_window(ev.ts, since_ts, until_ts)
            ]
            coverage["events_in_window"] += len(in_window)
            coverage["events_out_of_window"] += len(session_events) - len(in_window)
            events.extend(in_window)
    finally:
        conn.close()
    return events, coverage


def _in_window(ts: str | None, since_ts: float | None, until_ts: float | None) -> bool:
    """Same stance as ``extract._filter_since``: an event whose ts won't
    parse is kept — we cannot prove it is out of window."""
    if not ts:
        return True
    try:
        value = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return True
    return (since_ts is None or value >= since_ts) and (
        until_ts is None or value < until_ts
    )

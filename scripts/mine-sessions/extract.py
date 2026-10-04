#!/usr/bin/env python3
"""Normalize agent transcripts + telemetry into one ``Event`` JSONL stream.

Four corpora, one shape (:mod:`schema`): local Claude Code session
transcripts (the rich one — full tool stream, exact per-turn tokens, user
corrections), and three read-only prod hops through ``scripts/prod-psql``
(the ``tool_calls`` ledger, ``llm_call_log``, and ``kind='job'`` transcripts).
Every downstream stage (``stats.py``/``detect.py``/``cards.py``) reads
``out/events.jsonl`` and never learns where an event came from beyond its
``corpus`` field — see ``README.md``.

Why extraction is its own committed stage rather than re-derived per pass:
every prior session-mining pass wrote its own scratch parser, so pass N's
numbers were never comparable with pass N-1's, and the *sidechain* half of
the local corpus (91% of files — see :func:`iter_local_files`) was missed
entirely by at least one of them.

Usage::

    uv run scripts/mine-sessions/extract.py --since 7d
    uv run scripts/mine-sessions/extract.py --since 7d --ledger --llmlog --jobs
    uv run scripts/mine-sessions/extract.py --since 2d --limit 20 --out /tmp/events.jsonl

No source flag given -> ``--local`` only (the prod three need a network hop
through ``scripts/prod-psql``; a missing/failed hop warns and the run
continues with whatever sources it does have).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_source
import outdir
import redact
import schema
from schema import Event

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = outdir.out_path("events.jsonl")

# ---------------------------------------------------------------------------
# --since parsing
# ---------------------------------------------------------------------------

_SINCE_RELATIVE_RE = re.compile(r"^(\d+)([dh])$")


def parse_since(value: str) -> datetime:
    """``5d`` / ``72h`` (relative to now, UTC) or an ISO date/datetime. A
    naive ISO value is treated as UTC (never local time — see
    ``docs/conventions/time.md``)."""
    text = value.strip()
    m = _SINCE_RELATIVE_RE.match(text)
    if m:
        n = int(m.group(1))
        delta = timedelta(days=n) if m.group(2) == "d" else timedelta(hours=n)
        return datetime.now(UTC) - delta
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"--since: expected '<N>d', '<N>h', or an ISO date, got {value!r}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _filter_since(events: list[Event], since: datetime | None) -> list[Event]:
    if since is None:
        return events
    kept = []
    for ev in events:
        ts = _parse_ts(ev.ts)
        # An event with no/unparseable ts is kept rather than dropped — we
        # can't tell it's out of window, and silently discarding it would
        # hide real data (e.g. the odd row shape) behind the time filter.
        if ts is not None and ts < since:
            continue
        kept.append(ev)
    return kept


def _day_bucket(ts: str | None, prefix: str) -> str:
    if isinstance(ts, str) and len(ts) >= 10:
        return f"{prefix}:{ts[:10]}"
    return f"{prefix}:unknown"


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class Stats:
    local_files: int = 0
    job_transcripts: int = 0
    parse_failures: int = 0
    events_by_corpus: dict[str, int] = field(default_factory=dict)

    def bump(self, corpus: str, n: int) -> None:
        self.events_by_corpus[corpus] = self.events_by_corpus.get(corpus, 0) + n


# ---------------------------------------------------------------------------
# Source 1: --local — Claude Code session transcripts
# ---------------------------------------------------------------------------

#: The precis command-profile ``command="verb(kind='x', ...)"`` string. A
#: regex is enough — this never tries to fully parse the call, just lift the
#: kind= literal out of it (same contract as detect.py's D9).
_KIND_RE = re.compile(r"kind\s*=\s*['\"]([^'\"]+)['\"]")


def iter_local_files(
    projects_root: str | None, since: datetime | None = None
) -> list[Path]:
    """Both real shapes under ``~/.claude/projects`` (or ``projects_root``,
    for tests): main-session files directly under a project dir, and
    subagent sidechains one level deeper under ``<session>/subagents/``.
    These are 91% of the corpus by file count — an earlier audit's glob
    (``*/*.jsonl`` only) missed them entirely.

    Ordered newest-mtime first, and filtered to files touched since *since*.
    Both matter for the same reason: the corpus is ~1350 files / 1.5 GB, and
    a file last written before the window cannot hold an event inside it. In
    alphabetical order a ``--limit`` smoke run silently picks arbitrary old
    files and reports zero events, which reads exactly like a broken
    extractor — that is a real thing this ordering prevents, not a
    hypothetical.
    """
    root = (
        Path(projects_root) if projects_root else (Path.home() / ".claude" / "projects")
    )
    paths = [*root.glob("*/*.jsonl"), *root.glob("*/*/subagents/*.jsonl")]
    cutoff = since.timestamp() if since else None
    dated: list[tuple[float, Path]] = []
    for p in paths:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if cutoff is not None and mtime < cutoff:
            continue
        dated.append((mtime, p))
    # Newest first, path as a deterministic tie-break (same-second writes).
    dated.sort(key=lambda t: (-t[0], str(t[1])))
    return [p for _, p in dated]


def _extract_kind(tool_name: str | None, args: dict[str, Any]) -> str | None:
    if not schema.is_precis_tool(tool_name):
        return None
    tail = (tool_name or "")[len(schema.PRECIS_TOOL_PREFIX) :]
    if tail in schema.VERBS:
        kind = args.get("kind")
        return kind if isinstance(kind, str) else None
    if tail == "precis":
        cmd = args.get("command")
        if isinstance(cmd, str):
            m = _KIND_RE.search(cmd)
            if m:
                return m.group(1)
    return None


def _split_user_content(content: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Pull ``(text strings, tool_result blocks)`` out of a ``user`` event's
    ``message.content``. In the real corpus a genuine human turn (and slash
    -command chrome) is a bare *string*, not a list-of-blocks — only
    tool_result carriers (and the rare injected block-list message) use the
    list shape, so both are handled here."""
    if isinstance(content, str):
        return ([content] if content else [], [])
    if isinstance(content, list):
        texts: list[str] = []
        tool_results: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(str(block.get("text", "")))
            elif btype == "tool_result":
                tool_results.append(block)
        return texts, tool_results
    return [], []


def _render_result_content(content: Any) -> str:
    """A tool_result's ``content`` is a plain string OR a list of
    ``{"type": "text", "text": ...}``-shaped blocks — handle both."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


@dataclass
class _PendingCall:
    """A ``tool_use`` block seen on an assistant turn, waiting for its
    ``tool_result`` (which arrives on a LATER user event, joined by
    ``tool_use_id``)."""

    session: str
    tool: str | None
    verb: str | None
    kind: str | None
    arg_keys: tuple[str, ...]
    arg_digest: str
    model: str | None
    usage: dict[str, Any]
    sidechain: bool
    cwd: str | None
    branch: str | None
    ts: str | None


def _extract_from_lines(
    lines: Iterable[str],
    *,
    corpus: str,
    session_fallback: str,
    is_sidechain_default: bool,
    stats: Stats,
) -> list[Event]:
    """The shared block-walker: one JSON-per-line stream in the
    ``{"type": "assistant"|"user"|..., "message": {"content": [...]}}``
    shape, used both for local transcripts and (reused verbatim, per the
    task) for ``kind='job'`` stream-json transcripts pulled from prod.

    Unparseable lines are skipped and counted in ``stats.parse_failures``,
    never raised — one bad line must not sink a whole file's events.
    """
    events: list[Event] = []
    pending: dict[str, _PendingCall] = {}
    seq_counters: dict[str, int] = {}

    def next_seq(session: str) -> int:
        seq_counters[session] = seq_counters.get(session, 0) + 1
        return seq_counters[session]

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            stats.parse_failures += 1
            continue
        if not isinstance(evt, dict):
            stats.parse_failures += 1
            continue

        etype = evt.get("type")
        sid = evt.get("sessionId")
        # A subagent transcript carries its PARENT's `sessionId`, not one of
        # its own — so a bare `sessionId` is shared by a main-session file and
        # every sidechain file under it, while each file's `seq` restarts at 1.
        # That would collide `(session, seq)`, which is the key every detector
        # uses for "within N turns" and which `cards.py` resolves back to text:
        # a parent's seq=1 and a sidechain's seq=1 would look like the same
        # event. Scoping by the file stem keeps the key unique while leaving
        # the parent id as a prefix, so grouping a session with its subagents
        # stays a `split("/", 1)[0]`.
        if isinstance(sid, str) and sid:
            session = sid if sid == session_fallback else f"{sid}/{session_fallback}"
        else:
            session = session_fallback
        sidechain = bool(evt.get("isSidechain", is_sidechain_default))
        cwd = evt.get("cwd")
        branch = evt.get("gitBranch")
        ts = evt.get("timestamp")

        if etype == "assistant":
            msg = evt.get("message")
            if not isinstance(msg, dict):
                continue
            model = msg.get("model")
            usage_raw = msg.get("usage")
            usage = usage_raw if isinstance(usage_raw, dict) else {}
            # One turn-level event per assistant turn — tool_use blocks carry
            # no token counts, only the enclosing turn does.
            #
            # It also carries the turn's prose in `result_head`, mirroring the
            # `__user__` events. `detect.read_then_unused` (D7) asks whether a
            # large result was ever referenced in "the next 2 assistant text
            # blocks"; with an empty field that detector cannot fire at all,
            # and a detector that is silently always-quiet is indistinguishable
            # from a clean corpus — the worst failure mode a mining pass has.
            turn_text = " ".join(
                str(b.get("text", ""))
                for b in (msg.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            events.append(
                Event(
                    corpus=corpus,
                    session=session,
                    seq=next_seq(session),
                    ts=ts,
                    model=model,
                    usage=usage,
                    result_head=redact.scrub(turn_text)[: schema.HEAD_CHARS],
                    sidechain=sidechain,
                    cwd=cwd,
                    branch=branch,
                )
            )
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_use_id = block.get("id")
                    if not isinstance(tool_use_id, str):
                        continue
                    name = block.get("name")
                    tool_name = name if isinstance(name, str) else None
                    raw_args = block.get("input")
                    args = raw_args if isinstance(raw_args, dict) else {}
                    verb = schema.verb_of(tool_name, args) if tool_name else None
                    kind = _extract_kind(tool_name, args)
                    arg_keys = tuple(sorted(args.keys()))
                    digest_raw = json.dumps(
                        args, sort_keys=True, ensure_ascii=False, default=str
                    )
                    arg_digest = redact.scrub(digest_raw)[: schema.HEAD_CHARS]
                    pending[tool_use_id] = _PendingCall(
                        session=session,
                        tool=tool_name,
                        verb=verb,
                        kind=kind,
                        arg_keys=arg_keys,
                        arg_digest=arg_digest,
                        model=model,
                        usage=usage,
                        sidechain=sidechain,
                        cwd=cwd,
                        branch=branch,
                        ts=ts,
                    )

        elif etype == "user":
            msg = evt.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            texts, tool_results = _split_user_content(content)

            for block in tool_results:
                tool_use_id = block.get("tool_use_id")
                call = (
                    pending.pop(tool_use_id, None)
                    if isinstance(tool_use_id, str)
                    else None
                )
                if call is None:
                    continue
                is_error = bool(block.get("is_error"))
                result_text = _render_result_content(block.get("content"))
                result_head = redact.scrub(result_text)[: schema.HEAD_CHARS]
                events.append(
                    Event(
                        corpus=corpus,
                        session=call.session,
                        seq=next_seq(call.session),
                        ts=call.ts,
                        tool=call.tool,
                        verb=call.verb,
                        kind=call.kind,
                        arg_keys=call.arg_keys,
                        arg_digest=call.arg_digest,
                        is_error=is_error,
                        err_head=result_head if is_error else "",
                        result_bytes=len(result_text),
                        result_head=result_head,
                        model=call.model,
                        usage=call.usage,
                        sidechain=call.sidechain,
                        cwd=call.cwd,
                        branch=call.branch,
                    )
                )

            # A real human turn (or slash-command chrome) — never emitted
            # for an event that only carries tool_result blocks.
            if texts and not tool_results:
                text = "\n".join(t for t in texts if t)
                if text:
                    events.append(
                        Event(
                            corpus=corpus,
                            session=session,
                            seq=next_seq(session),
                            ts=ts,
                            tool="__user__",
                            result_bytes=len(text),
                            result_head=redact.scrub(text)[: schema.HEAD_CHARS],
                            sidechain=sidechain,
                            cwd=cwd,
                            branch=branch,
                        )
                    )
        # Other event types (mode, last-prompt, permission-mode, system,
        # attachment, file-history-*, queue-operation, ai-title, ...) carry
        # nothing this schema models — parsed fine, just skipped.

    # Flush any tool_use whose result never arrived in this file (truncated
    # transcript, or an async agent whose result lands elsewhere) — the call
    # itself is still signal, just with an empty outcome.
    for call in pending.values():
        events.append(
            Event(
                corpus=corpus,
                session=call.session,
                seq=next_seq(call.session),
                ts=call.ts,
                tool=call.tool,
                verb=call.verb,
                kind=call.kind,
                arg_keys=call.arg_keys,
                arg_digest=call.arg_digest,
                model=call.model,
                usage=call.usage,
                sidechain=call.sidechain,
                cwd=call.cwd,
                branch=call.branch,
            )
        )
    return events


# ---------------------------------------------------------------------------
# Sources 2-4: prod, via scripts/prod-psql
# ---------------------------------------------------------------------------

_LEDGER_COLUMNS = (
    "ts",
    "source",
    "profile",
    "verb",
    "kind",
    "input_keys",
    "outcome",
    "error_type",
    "latency_ms",
)

_LLMLOG_COLUMNS = (
    "ts",
    "source",
    "tier",
    "model",
    "request_chars",
    "response_chars",
    "cost_usd",
    "duration_ms",
    "errored",
    "error",
    "data_parsed",
)


def _run_prod_psql(sql: str) -> str | None:
    """Run one read-only statement through ``scripts/prod-psql``. Returns
    stdout, or ``None`` (after printing a one-line warning) if the script is
    missing or the hop fails — a dead network must not kill a local-only
    run."""
    script = _REPO_ROOT / "scripts" / "prod-psql"
    env = dict(os.environ)
    env["PRECIS_PROD_PSQL_OPTS"] = '-F "\t" -tA'
    try:
        proc = subprocess.run(
            [str(script), sql],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"warning: scripts/prod-psql unavailable ({exc}) -- skipping this source",
            file=sys.stderr,
        )
        return None
    if proc.returncode != 0:
        print(
            f"warning: scripts/prod-psql failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:300]} -- skipping this source",
            file=sys.stderr,
        )
        return None
    return proc.stdout


def _parse_tsv_rows(stdout: str, columns: tuple[str, ...]) -> list[dict[str, str]]:
    rows = []
    for line in stdout.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != len(columns):
            continue  # malformed row (psqlrc leakage, a warning line, ...) — skip
        rows.append(dict(zip(columns, fields)))
    return rows


def _sql_ts_literal(since: datetime) -> str:
    return since.astimezone(UTC).isoformat(sep=" ")


def _ledger_sql(since: datetime | None) -> str:
    where = f" WHERE ts >= '{_sql_ts_literal(since)}'" if since else ""
    return f"SELECT {', '.join(_LEDGER_COLUMNS)} FROM tool_calls{where} ORDER BY ts;"


def fetch_ledger_rows(since: datetime | None) -> list[dict[str, Any]]:
    stdout = _run_prod_psql(_ledger_sql(since))
    if stdout is None:
        return []
    rows = []
    for r in _parse_tsv_rows(stdout, _LEDGER_COLUMNS):
        input_keys_raw = r["input_keys"]
        try:
            input_keys = json.loads(input_keys_raw) if input_keys_raw else []
        except json.JSONDecodeError:
            input_keys = []
        rows.append(
            {
                "ts": r["ts"] or None,
                "source": r["source"] or None,
                "profile": r["profile"] or None,
                "verb": r["verb"] or None,
                "kind": r["kind"] or None,
                "input_keys": input_keys,
                "outcome": r["outcome"] or None,
                "error_type": r["error_type"] or None,
                "latency_ms": _to_int(r["latency_ms"]),
            }
        )
    return rows


def ledger_row_to_event(row: dict[str, Any], *, seq: int) -> Event:
    """No payload fields — ``tool_calls`` carries none, by design (migration
    0133)."""
    ts = row.get("ts")
    input_keys = row.get("input_keys") or []
    if isinstance(input_keys, str):
        try:
            input_keys = json.loads(input_keys)
        except json.JSONDecodeError:
            input_keys = []
    arg_keys = tuple(sorted(input_keys)) if isinstance(input_keys, list) else ()
    return Event(
        corpus="ledger",
        session=_day_bucket(ts if isinstance(ts, str) else None, "ledger"),
        seq=seq,
        ts=ts,
        verb=row.get("verb"),
        kind=row.get("kind"),
        arg_keys=arg_keys,
        is_error=row.get("outcome") == "error",
        err_type=row.get("error_type"),
        latency_ms=_to_int(row.get("latency_ms")),
        source=row.get("source"),
        profile=row.get("profile"),
    )


def _emit_ledger_events(rows: list[dict[str, Any]]) -> list[Event]:
    counters: dict[str, int] = {}
    events: list[Event] = []
    for row in rows:
        ts = row.get("ts")
        session = _day_bucket(ts if isinstance(ts, str) else None, "ledger")
        counters[session] = counters.get(session, 0) + 1
        events.append(ledger_row_to_event(row, seq=counters[session]))
    return events


def _llmlog_sql(since: datetime | None) -> str:
    where = f" WHERE ts >= '{_sql_ts_literal(since)}'" if since else ""
    return f"SELECT {', '.join(_LLMLOG_COLUMNS)} FROM llm_call_log{where} ORDER BY ts;"


def fetch_llmlog_rows(since: datetime | None) -> list[dict[str, Any]]:
    # NOTE: deliberately not joining llm_blob (the full request/response
    # text) — that is large and a separate future flag; this pulls only the
    # queryable metadata columns.
    stdout = _run_prod_psql(_llmlog_sql(since))
    if stdout is None:
        return []
    rows = []
    for r in _parse_tsv_rows(stdout, _LLMLOG_COLUMNS):
        rows.append(
            {
                "ts": r["ts"] or None,
                "source": r["source"] or None,
                "tier": r["tier"] or None,
                "model": r["model"] or None,
                "request_chars": _to_int(r["request_chars"]),
                "response_chars": _to_int(r["response_chars"]),
                "cost_usd": float(r["cost_usd"]) if r["cost_usd"] else None,
                "duration_ms": _to_int(r["duration_ms"]),
                "errored": r["errored"] == "t",
                "error": r["error"] or None,
                "data_parsed": (r["data_parsed"] == "t") if r["data_parsed"] else None,
            }
        )
    return rows


def llmlog_row_to_event(row: dict[str, Any], *, seq: int) -> Event:
    ts = row.get("ts")
    err = row.get("error")
    err_text = str(err) if err else ""
    return Event(
        corpus="llmlog",
        session=_day_bucket(ts if isinstance(ts, str) else None, "llmlog"),
        seq=seq,
        ts=ts,
        tool="__llm__",
        is_error=bool(row.get("errored")),
        err_head=redact.scrub(err_text)[: schema.HEAD_CHARS] if err_text else "",
        result_bytes=_to_int(row.get("response_chars")) or 0,
        latency_ms=_to_int(row.get("duration_ms")),
        model=row.get("model"),
        source=row.get("source"),
        profile=row.get("tier"),
        usage={
            "request_chars": row.get("request_chars"),
            "response_chars": row.get("response_chars"),
            "cost_usd": row.get("cost_usd"),
            "data_parsed": row.get("data_parsed"),
        },
    )


def _emit_llmlog_events(rows: list[dict[str, Any]]) -> list[Event]:
    counters: dict[str, int] = {}
    events: list[Event] = []
    for row in rows:
        ts = row.get("ts")
        session = _day_bucket(ts if isinstance(ts, str) else None, "llmlog")
        counters[session] = counters.get(session, 0) + 1
        events.append(llmlog_row_to_event(row, seq=counters[session]))
    return events


_JOBS_SQL_BASE = (
    "SELECT ref_id, meta->>'transcript' AS transcript FROM refs "
    "WHERE kind = 'job' AND retired_at IS NULL AND meta ? 'transcript'"
)


def _jobs_sql(since: datetime | None, limit: int | None) -> str:
    where = _JOBS_SQL_BASE
    if since:
        where += f" AND updated_at >= '{_sql_ts_literal(since)}'"
    inner = f"{where} ORDER BY updated_at"
    if limit:
        inner += f" LIMIT {int(limit)}"
    # Plain `TO STDOUT` COPY-escapes \n/\t/\\ inside the transcript text, so
    # the lines would not re-parse as JSON — \copy ... FORMAT csv is the one
    # export shape that round-trips it.
    return f"\\copy ({inner}) TO STDOUT WITH (FORMAT csv)"


#: A job transcript is capped at 1 MiB server-side, but csv's default field
#: limit is 128 KiB — so a real `doctor_tick` row (~400 KiB) raises
#: `_csv.Error: field larger than field limit` and takes the whole extraction
#: with it. Sized well above the server cap so the reader is never the
#: binding constraint; not `sys.maxsize`, which overflows the C long the csv
#: module stores this in on some platforms.
_CSV_FIELD_LIMIT = 64 * 1024 * 1024


def fetch_jobs_rows(since: datetime | None, limit: int | None) -> list[tuple[str, str]]:
    stdout = _run_prod_psql(_jobs_sql(since, limit))
    if stdout is None:
        return []
    csv.field_size_limit(_CSV_FIELD_LIMIT)
    rows: list[tuple[str, str]] = []
    for fields in csv.reader(io.StringIO(stdout)):
        if len(fields) != 2:
            continue
        rows.append((fields[0], fields[1]))
    return rows


def jobs_transcript_to_events(
    ref_id: str, transcript: str, *, stats: Stats
) -> list[Event]:
    """A job's ``meta.transcript`` is a stream-json blob in the SAME
    tool_use/tool_result shape as the local corpus — reuse the local
    block-walker rather than a second parser."""
    return _extract_from_lines(
        transcript.splitlines(),
        corpus="jobs",
        session_fallback=f"job:{ref_id}",
        is_sidechain_default=False,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--since",
        default=None,
        help="'<N>d' / '<N>h' / an ISO date (default: no cutoff)",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output JSONL path")
    parser.add_argument("--until", help="exclusive ISO end of the Codex interval")
    parser.add_argument(
        "--codex", action="store_true", help="scoped local Codex rollouts"
    )
    parser.add_argument(
        "--codex-root", type=Path, default=Path.home() / ".codex/sessions"
    )
    parser.add_argument(
        "--cwd-root", action="append", default=[], help="Codex project root; repeatable"
    )
    parser.add_argument(
        "--thread-id",
        action="append",
        default=[],
        help="Codex thread and children; repeatable",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="cap local files / job transcripts scanned (smoke runs)",
    )
    parser.add_argument(
        "--local", action="store_true", help="local Claude Code transcripts"
    )
    parser.add_argument(
        "--local-only", action="store_true", dest="local_only", help="alias for --local"
    )
    parser.add_argument("--ledger", action="store_true", help="prod tool_calls ledger")
    parser.add_argument("--llmlog", action="store_true", help="prod llm_call_log")
    parser.add_argument(
        "--jobs", action="store_true", help="prod kind='job' transcripts"
    )
    parser.add_argument(
        "--projects-glob",
        default=None,
        metavar="DIR",
        help="override ~/.claude/projects (tests point this at a fixture dir)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    t0 = time.monotonic()
    args = _build_arg_parser().parse_args(argv)

    since = parse_since(args.since) if args.since else None
    until = parse_since(args.until) if args.until else None
    if args.codex and not (args.cwd_root or args.thread_id):
        raise SystemExit("--codex requires --cwd-root or --thread-id")
    if until is not None and since is not None and until <= since:
        raise SystemExit("--until must be later than --since")

    use_local = bool(args.local or args.local_only)
    use_ledger = bool(args.ledger)
    use_llmlog = bool(args.llmlog)
    use_jobs = bool(args.jobs)
    if not (use_local or use_ledger or use_llmlog or use_jobs or args.codex):
        use_local = True

    out_path = Path(args.out)
    if args.codex:
        outdir.require_external(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    stats = Stats()

    with out_path.open("w", encoding="utf-8") as out_fh:
        if args.codex:
            events, coverage = codex_source.extract(
                args.codex_root,
                since=since,
                until=until,
                cwds=args.cwd_root,
                threads=args.thread_id,
                limit=args.limit,
            )
            for ev in events:
                out_fh.write(ev.to_json() + "\n")
            stats.bump("codex", len(events))
            coverage_path = out_path.with_name("coverage.json")
            coverage_path.write_text(
                json.dumps(
                    {
                        "corpus": "codex",
                        "since": args.since,
                        "until": args.until,
                        "coverage": coverage,
                        "limitations": [
                            "Nested JavaScript tool calls are opaque orchestration; MCP rates are incomplete.",
                            "Tool error flags require structured outcomes; unstructured errors are not classified.",
                            "Coverage counters describe scoped files; events_in_window describes the interval.",
                            "Missing/reset counters and forks without history boundaries are undercounts, not zero work.",
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        if use_local:
            files = iter_local_files(args.projects_glob, since)
            if args.limit is not None:
                files = files[: args.limit]
            for path in files:
                stats.local_files += 1
                is_sidechain_default = path.parent.name == "subagents"
                try:
                    with path.open(encoding="utf-8") as fh:
                        events = _extract_from_lines(
                            fh,
                            corpus="local",
                            session_fallback=path.stem,
                            is_sidechain_default=is_sidechain_default,
                            stats=stats,
                        )
                except OSError as exc:
                    print(f"warning: could not read {path}: {exc}", file=sys.stderr)
                    continue
                events = _filter_since(events, since)
                for ev in events:
                    out_fh.write(ev.to_json() + "\n")
                stats.bump("local", len(events))

        # Each prod corpus is isolated. The local corpus is the expensive one
        # to produce and the only one with payloads; a cluster hop that fails,
        # times out, or returns something unparseable must not discard it.
        # (`_run_prod_psql` already handles an unreachable hop; this catches
        # what comes after a *successful* fetch — e.g. a csv row wider than
        # the reader's field limit, which is how this guard was earned.)
        if use_ledger:
            try:
                events = _emit_ledger_events(fetch_ledger_rows(since))
            except Exception as exc:
                print(f"warning: ledger corpus skipped: {exc}", file=sys.stderr)
            else:
                for ev in events:
                    out_fh.write(ev.to_json() + "\n")
                stats.bump("ledger", len(events))

        if use_llmlog:
            try:
                events = _emit_llmlog_events(fetch_llmlog_rows(since))
            except Exception as exc:
                print(f"warning: llmlog corpus skipped: {exc}", file=sys.stderr)
            else:
                for ev in events:
                    out_fh.write(ev.to_json() + "\n")
                stats.bump("llmlog", len(events))

        if use_jobs:
            try:
                job_rows = fetch_jobs_rows(since, args.limit)
            except Exception as exc:
                print(f"warning: jobs corpus skipped: {exc}", file=sys.stderr)
                job_rows = []
            for ref_id, transcript in job_rows:
                stats.job_transcripts += 1
                job_events = jobs_transcript_to_events(ref_id, transcript, stats=stats)
                job_events = _filter_since(job_events, since)
                for ev in job_events:
                    out_fh.write(ev.to_json() + "\n")
                stats.bump("jobs", len(job_events))

    elapsed = time.monotonic() - t0
    print(
        "mine-sessions/extract: "
        f"local_files={stats.local_files} job_transcripts={stats.job_transcripts} "
        f"parse_failures={stats.parse_failures} events={dict(stats.events_by_corpus)} "
        f"elapsed={elapsed:.1f}s -> {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

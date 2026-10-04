"""Codex rollout adapter: scoped payloads, joined outcomes, nonduplicated usage.

The first session_meta owns the file; later metadata may be inherited history.
Recent child rollouts declare subagent_history_start_ordinal, so inherited
parent turns must be skipped. Usage records carry per-response usage alongside
cumulative totals: prefer those records, otherwise difference token_count's
total_token_usage. Never sum last_token_usage, which can be repeated on polls.
Opaque orchestration code remains one call; we do not pretend to have observed
individual MCP calls nested inside JavaScript.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import redact
import schema
from schema import Event

_KIND_RE = re.compile(r"kind\s*=\s*['\"]([^'\"]+)['\"]")


def read_rows(path: Path, coverage: Counter):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                coverage["malformed_rows"] += 1
                continue
            if isinstance(row, dict) and isinstance(row.get("payload"), dict):
                yield row
            else:
                coverage["malformed_rows"] += 1


def metadata(path: Path, coverage: Counter) -> dict:
    for row in read_rows(path, coverage):
        if row.get("type") == "session_meta":
            return row["payload"]
    coverage["files_without_metadata"] += 1
    return {}


def under(cwd: str | None, roots: list[Path]) -> bool:
    if not cwd:
        return False
    return any(Path(cwd).resolve().is_relative_to(root) for root in roots)


def in_window(ts, since, until) -> bool:
    try:
        value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
    except (AttributeError, TypeError, ValueError):
        return False
    return (since is None or value >= since) and (until is None or value < until)


def usage(raw: dict) -> dict:
    keys = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cached_input_tokens": "cache_read_input_tokens",
        "cache_write_input_tokens": "cache_creation_input_tokens",
        "reasoning_output_tokens": "reasoning_output_tokens",
    }
    return {
        target: raw[key]
        for key, target in keys.items()
        if isinstance(raw.get(key), int)
    }


def parse(path: Path, meta: dict, coverage: Counter) -> list[Event]:
    sid = meta.get("id") or meta.get("session_id") or path.stem
    child = bool(meta.get("parent_thread_id") or isinstance(meta.get("source"), dict))
    boundary = meta.get("subagent_history_start_ordinal")
    if meta.get("forked_from_id") and boundary is None:
        coverage["forks_missing_history_boundary"] += 1
        return []
    events: list[Event] = []
    fallback: list[Event] = []
    pending: dict[str, Event] = {}
    response_ids: set[str] = set()
    totals: dict = {}
    model = None
    cwd = meta.get("cwd")
    branch = (meta.get("git") or {}).get("branch")

    def event(row: dict, **kwargs) -> Event:
        return Event(
            corpus="codex",
            session=f"codex:{sid}",
            seq=0,
            ts=row.get("timestamp"),
            model=model,
            cwd=cwd,
            branch=branch,
            sidechain=child,
            **kwargs,
        )

    for row in read_rows(path, coverage):
        payload = row["payload"]
        typ = row.get("type")
        if (
            typ == "session_meta"
            and child
            and boundary is None
            and payload.get("id", sid) != sid
        ):
            coverage["forks_missing_history_boundary"] += 1
            return []
        if boundary is not None and typ != "session_meta":
            if not isinstance(row.get("ordinal"), int) or row["ordinal"] < boundary:
                if typ == "event_msg" and payload.get("type") == "token_count":
                    raw = (payload.get("info") or {}).get("total_token_usage") or {}
                    totals = {
                        key: value
                        for key, value in raw.items()
                        if isinstance(value, int)
                    }
                coverage["inherited_rows_skipped"] += 1
                continue
        if typ == "turn_context":
            model = payload.get("model", model)
            cwd = payload.get("cwd", cwd)
        elif typ == "token_usage_record":
            if payload.get("thread_id", sid) != sid:
                coverage["foreign_usage_records_skipped"] += 1
                continue
            response_id = payload.get("response_id")
            if not response_id or response_id in response_ids:
                coverage["missing_or_duplicate_response_ids"] += 1
                continue
            response_ids.add(response_id)
            events.append(event(row, usage=usage(payload.get("usage") or {})))
            coverage["per_response_usage_records"] += 1
        elif typ == "event_msg" and payload.get("type") == "token_count":
            raw = (payload.get("info") or {}).get("total_token_usage") or {}
            current = {
                key: value for key, value in raw.items() if isinstance(value, int)
            }
            if not current:
                coverage["token_counts_without_totals"] += 1
                continue
            if child and not totals:
                # An older child counter can include inherited parent usage;
                # without a baseline its first delta is unknowable.
                coverage["child_usage_baseline_missing"] += 1
                totals = current
                continue
            if any(current.get(key, 0) < value for key, value in totals.items()):
                coverage["token_counter_resets"] += 1
                totals = current
                continue
            delta = {key: value - totals.get(key, 0) for key, value in current.items()}
            totals = current
            if any(delta.values()):
                fallback.append(event(row, usage=usage(delta)))
        elif typ == "response_item":
            kind = payload.get("type")
            if kind in {"function_call", "custom_tool_call"}:
                call_id = payload.get("call_id")
                if not isinstance(call_id, str):
                    coverage["calls_without_id"] += 1
                    continue
                name = payload.get("name", "unknown")
                namespace = payload.get("namespace")
                separator = "__" if namespace and namespace.startswith("mcp__") else "."
                tool = f"{namespace}{separator}{name}" if namespace else name
                args = payload.get("arguments", payload.get("input", {}))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"input": args}
                if not isinstance(args, dict):
                    args = {"input": args}
                verb = schema.verb_of(tool, args)
                item_kind = args.get("kind") if verb else None
                if (
                    verb
                    and tool.endswith("__precis")
                    and isinstance(args.get("command"), str)
                ):
                    match = _KIND_RE.search(args["command"])
                    item_kind = match.group(1) if match else None
                call = event(
                    row,
                    tool=tool,
                    verb=verb,
                    kind=item_kind,
                    arg_keys=tuple(sorted(args)),
                    arg_digest=redact.scrub(json.dumps(args, ensure_ascii=False))[
                        : schema.HEAD_CHARS
                    ],
                )
                if tool in {"exec", "functions.exec"}:
                    coverage["opaque_orchestrator_calls"] += 1
                pending[call_id] = call
                events.append(call)
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                outcome_call = pending.pop(payload.get("call_id"), None)
                if outcome_call is None:
                    coverage["orphan_outputs"] += 1
                    continue
                output = payload.get("output", "")
                text = (
                    output
                    if isinstance(output, str)
                    else json.dumps(output, ensure_ascii=False)
                )
                outcome_call.result_bytes = len(text.encode("utf-8"))
                outcome_call.result_head = redact.scrub(text)[: schema.HEAD_CHARS]
                structured = output
                if isinstance(output, str):
                    try:
                        structured = json.loads(output)
                    except ValueError:
                        structured = {}
                # Do not infer tool errors from quoted/narrated error strings.
                if isinstance(structured, dict):
                    outcome_call.is_error = bool(
                        structured.get("isError") or structured.get("is_error")
                    )
                    code = structured.get("exit_code")
                    outcome_call.is_error |= isinstance(code, int) and code != 0
                if outcome_call.is_error:
                    outcome_call.err_head = outcome_call.result_head
                coverage["joined_results"] += 1
            elif kind == "message" and payload.get("role") in {"user", "assistant"}:
                content = payload.get("content") or []
                text = "\n".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                    and part.get("type") in {"input_text", "output_text", "text"}
                )
                events.append(
                    event(
                        row,
                        tool="__user__" if payload["role"] == "user" else None,
                        result_bytes=len(text.encode("utf-8")),
                        result_head=redact.scrub(text)[: schema.HEAD_CHARS],
                    )
                )
    coverage["calls_without_result"] += len(pending)
    if not response_ids:
        events.extend(fallback)
        coverage["cumulative_usage_deltas"] += len(fallback)
    if not response_ids and not fallback:
        coverage["files_without_usage"] += 1
    events.sort(key=lambda item: item.ts or "")
    for seq, item in enumerate(events, 1):
        item.seq = seq
    return events


def extract(root: Path, *, since=None, until=None, cwds=(), threads=(), limit=None):
    if not cwds and not threads:
        raise ValueError("Codex mining requires --cwd-root or --thread-id scope")
    coverage: Counter[str] = Counter()
    roots = [Path(path).expanduser().resolve() for path in cwds]
    files = {}
    for path in root.expanduser().rglob("*.jsonl"):
        coverage["discovered_files"] += 1
        try:
            meta = metadata(path, coverage)
        except OSError:
            coverage["unreadable_files"] += 1
            continue
        sid = meta.get("id") or meta.get("session_id")
        if sid:
            files[sid] = (path, meta)
    selected = set(threads)
    while True:
        before = len(selected)
        for sid, (_, meta) in files.items():
            if (
                under(meta.get("cwd"), roots)
                or meta.get("parent_thread_id") in selected
            ):
                selected.add(sid)
        if len(selected) == before:
            break
    coverage["requested_threads_missing"] = len(set(threads) - files.keys())
    paths = [files[sid] for sid in selected if sid in files]
    paths.sort(key=lambda pair: str(pair[0]))
    coverage["scope_files"] = len(paths)
    if limit is not None:
        coverage["files_omitted_by_limit"] = max(0, len(paths) - limit)
        paths = paths[:limit]
    events = []
    for path, meta in paths:
        try:
            parsed = parse(path, meta, coverage)
        except OSError:
            coverage["unreadable_files"] += 1
            continue
        coverage["scanned_files"] += 1
        for item in parsed:
            if not item.ts:
                coverage["events_missing_timestamp"] += 1
            if in_window(item.ts, since, until):
                events.append(item)
    coverage["events_in_window"] = len(events)
    return events, dict(coverage)

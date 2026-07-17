#!/usr/bin/env python3
"""Claude Code SubagentStop hook — capture a specialist's reply to precis.

When Asa dispatches via Agent(subagent_type='researcher', ...) and
the subagent finishes, claude fires SubagentStop. This hook writes
the subagent's final text to the SAME conv ref Asa's working in,
with author = 'asa:agent:<subagent_type>' so next turn Asa sees
"I already asked researcher for X, they said Y."

Shares the HTTP shim path with capture_assistant_turn.py. Reads
the conv slug from ASA_CONV_SLUG (set by asa_bot before invoking
claude). If unset → silent no-op.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime


def main() -> int:
    try:
        with open("/tmp/asa-subagent-fired.log", "a") as fh:
            slug = os.environ.get("ASA_CONV_SLUG", "UNSET")
            fh.write(f"{datetime.now(tz=UTC).isoformat()} fired conv_slug={slug}\n")
    except Exception:
        pass

    conv_slug = os.environ.get("ASA_CONV_SLUG")
    if not conv_slug:
        return 0

    raw_stdin = sys.stdin.read()
    try:
        with open("/tmp/asa-subagent-payload.json", "w") as fh:
            fh.write(raw_stdin)
    except Exception:
        pass

    try:
        payload = json.loads(raw_stdin)
    except json.JSONDecodeError:
        return 0

    text = _extract_text(payload)
    if not text:
        return 0

    # Subagent identity. Claude Code's SubagentStop payload should
    # carry the subagent_type; falls back to 'unknown' if absent.
    subagent = (
        payload.get("subagent_type")
        or payload.get("agent")
        or "unknown"
    )

    body = {
        "conv_slug": conv_slug,
        "text": text,
        "msg_id": _msg_id_from(payload, subagent),
        "author": f"asa:agent:{subagent}",
        "ts": datetime.now(tz=UTC).isoformat(),
        "stop_reason": payload.get("stop_reason"),
        "input_tokens": _usage_field(payload, "input_tokens"),
        "output_tokens": _usage_field(payload, "output_tokens"),
    }
    url = os.environ.get("ASA_CAPTURE_URL", "http://127.0.0.1:9876/capture")
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=2)
    except (urllib.error.URLError, TimeoutError):
        _write_fallback(body)
    return 0


def _extract_text(payload: dict) -> str:
    for key in ("last_assistant_message", "response", "assistant_response", "text"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v
    msg = payload.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            chunks = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    chunks.append(c.get("text", ""))
            joined = "\n".join(chunks).strip()
            if joined:
                return joined
        if isinstance(content, str):
            return content
    return ""


def _msg_id_from(payload: dict, subagent: str) -> str:
    session_id = payload.get("session_id") or "unknown-session"
    ts = int(datetime.now(tz=UTC).timestamp() * 1000)
    return f"claude:{session_id}:{subagent}:{ts}"


def _usage_field(payload: dict, name: str) -> int | None:
    usage = payload.get("usage") or {}
    v = usage.get(name)
    if v is None:
        msg = payload.get("message")
        if isinstance(msg, dict):
            v = (msg.get("usage") or {}).get(name)
    return int(v) if v is not None else None


def _write_fallback(body: dict) -> None:
    fallback_path = os.environ.get(
        "ASA_CAPTURE_FALLBACK",
        "/Users/hermes/claudebot/capture-fallback.jsonl",
    )
    try:
        record = dict(body)
        record["fallback_ts"] = datetime.now(tz=UTC).isoformat()
        with open(fallback_path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())

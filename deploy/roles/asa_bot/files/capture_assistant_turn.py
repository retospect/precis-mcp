#!/usr/bin/env python3
"""Claude Code Stop hook — capture the assistant turn to precis via asa_bot.

Reads JSON from stdin (the hook payload) and POSTs to asa_bot's
HTTP capture shim at 127.0.0.1:9876. asa_bot relays through its
long-lived precis MCP client and writes to ``kind='conv'``.

Auth / conv-slug context comes from environment:

- ``ASA_CONV_SLUG``: set by asa_bot before invoking claude. Without
  it the hook exits 0 silently (this isn't an Asa turn — could be
  reto running ``claude`` interactively).
- ``ASA_CAPTURE_URL`` (optional): override the default
  ``http://127.0.0.1:9876/capture``.

Best-effort: hook never blocks claude. Timeout 2s, exit 0 always.
If asa_bot is mid-restart the HTTP call falls back to a JSONL file
which asa_bot replays on startup.

Hook payload shape (Claude Code Stop hook):
{
  "session_id": "...",
  "transcript_path": "...",
  "hook_event_name": "Stop",
  "stop_hook_active": false,
  ...plus the final assistant message and usage stats...
}

We extract what's useful and forward.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime


def main() -> int:
    # DIAGNOSTIC: log invocation timestamp + conv_slug to /tmp/asa-hook-fired.log
    try:
        with open("/tmp/asa-hook-fired.log", "a") as fh:
            slug = os.environ.get("ASA_CONV_SLUG", "UNSET")
            fh.write(f"{datetime.now(tz=UTC).isoformat()} fired conv_slug={slug}\n")
    except Exception:
        pass

    conv_slug = os.environ.get("ASA_CONV_SLUG")
    if not conv_slug:
        return 0

    # DIAGNOSTIC: dump the raw stdin payload so we can inspect what
    # Claude Code's Stop hook is actually sending. Format probably
    # doesn't match the legacy keys we look for.
    raw_stdin = sys.stdin.read()
    try:
        with open("/tmp/asa-hook-payload.json", "w") as fh:
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

    body = {
        "conv_slug": conv_slug,
        "text": text,
        "msg_id": _msg_id_from(payload),
        "author": "asa",
        "ts": datetime.now(tz=UTC).isoformat(),
        "stop_reason": payload.get("stop_reason"),
        "input_tokens": _usage_field(payload, "input_tokens"),
        "output_tokens": _usage_field(payload, "output_tokens"),
        "cache_read_tokens": _usage_field(payload, "cache_read_input_tokens"),
        "cache_creation_tokens": _usage_field(payload, "cache_creation_input_tokens"),
        "duration_ms": payload.get("duration_ms"),
    }
    url = os.environ.get("ASA_CAPTURE_URL", "http://127.0.0.1:9876/capture")
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=2)
    except (urllib.error.URLError, TimeoutError):
        # asa_bot is down or restarting. Fall back to JSONL so we
        # don't lose the capture; asa_bot replays on startup.
        _write_fallback(body)
    return 0


def _extract_text(payload: dict) -> str:
    # Claude Code's Stop hook ships the final assistant text under
    # ``last_assistant_message`` (confirmed via payload dump
    # 2026-06-12). Legacy keys kept as fallbacks for older versions.
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


def _msg_id_from(payload: dict) -> str:
    session_id = payload.get("session_id") or "unknown-session"
    # Use the full session id + millisecond timestamp; the conv
    # handler's compact trailer truncates to last 6 chars at display
    # time, so the full id stays the canonical idempotency key.
    ts = int(datetime.now(tz=UTC).timestamp() * 1000)
    return f"claude:{session_id}:{ts}"


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
        # Truly best-effort — never crash claude.
        pass


if __name__ == "__main__":
    sys.exit(main())

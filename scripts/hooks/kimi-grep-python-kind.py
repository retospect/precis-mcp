#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse nudge: answer ripgrep calls with the precis python kind.

Registered in ~/.kimi-code/config.toml (matcher ``^(Grep|Bash)$``) by
scripts/kimi-setup. When the agent reaches for the Grep tool or a bare
``rg``/``grep`` shell command inside a precis-mcp checkout, this hook runs
the SAME pattern through ``search(kind='python', mode='pattern', ...)`` on
the local precis MCP and prints the hits plus the repo convention
("structure first via precis, grep second — grep is truth for code you
changed"). Exit 0 always: the stdout rides along as context and the call
still executes — grep remains available, the python kind just gets the
first word. A python-kind result that misses something is itself signal:
file a gripe for the missing feature rather than working around it.

Fails open on every seam: no token, MCP unreachable, unparseable command,
non-ripgrep Bash — the hook prints nothing and allows.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

MAX_HITS = 12
TIMEOUT_S = 20

CONVENTION = (
    "precis python kind first: search(kind='python', mode='pattern', q=...) "
    "answers over the indexed tree (signatures, callers, callees) — grep "
    "second, and grep is truth for code you changed. If the python kind "
    "missed what you needed, file a gripe for the missing feature."
)


def _mcp_url() -> str:
    return os.environ.get("PRECIS_MCP_URL", "http://127.0.0.1:8765/mcp")


def _token() -> str:
    if os.environ.get("PRECIS_MCP_TOKEN"):
        return os.environ["PRECIS_MCP_TOKEN"]
    cfg = os.path.expanduser("~/.kimi-code/mcp.json")
    try:
        with open(cfg, encoding="utf-8") as fh:
            servers = json.load(fh).get("mcpServers", {})
        headers = servers.get("precis", {}).get("headers", {})
        return (headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def _post(payload: dict, token: str, session: str | None) -> dict | None:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(
        _mcp_url(),
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read().decode()
            new_session = resp.headers.get("Mcp-Session-Id")
    except OSError:
        return None
    if not raw.strip():
        return {"__session__": new_session}
    if raw.startswith("event:") or raw.startswith(":"):
        data = [ln[5:] for ln in raw.splitlines() if ln.startswith("data:")]
        raw = data[-1] if data else "{}"
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if isinstance(doc, dict) and new_session:
        doc["__session__"] = new_session
    return doc


def _search_python_kind(pattern: str) -> list[str]:
    """Run the pattern through search(kind='python', mode='pattern').

    Returns rendered hit lines; [] on any failure (fail-open).
    """
    token = _token()
    if not token or not pattern:
        return []
    init = _post(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "kimi-grep-python-kind", "version": "0"},
            },
        },
        token,
        None,
    )
    if init is None:
        return []
    session = init.get("__session__")
    _post(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        token,
        session,
    )
    doc = _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {
                    "kind": "python",
                    "mode": "pattern",
                    "q": pattern,
                    "page_size": MAX_HITS,
                },
            },
        },
        token,
        session,
    )
    if not doc or "error" in doc:
        return []
    lines: list[str] = []
    for c in doc.get("result", {}).get("content", []):
        if c.get("type") == "text":
            lines.extend(c["text"].splitlines())
    return lines


_RG_BASH = re.compile(r"^\s*(?:rtk\s+)?(?:rg|grep)\s+(.+)$")


def _bash_pattern(command: str) -> str:
    """The search term of a BARE rg/grep invocation, else ''.

    Anything with a pipe, redirect, substitution or multiple ``;``-joined
    commands is not a lookup we can mirror — allow silently.
    """
    if re.search(r"[|>`$()]", command) or ";" in command:
        return ""
    m = _RG_BASH.match(command.strip())
    if not m:
        return ""
    tokens = m.group(1).split()
    pattern = ""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--":
            i += 1
            if i < len(tokens):
                pattern = tokens[i]
            break
        if t.startswith("-"):
            i += 1
            continue
        pattern = t
        break
    return pattern.strip("'\"")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input", {}) or {}
    pattern = ""
    if tool == "Grep":
        pattern = str(ti.get("pattern") or "")
    elif tool == "Bash":
        pattern = _bash_pattern(str(ti.get("command") or ""))
    if not pattern:
        return
    hits = _search_python_kind(pattern)
    out = [f"[python-kind mirror of {tool} pattern {pattern!r}]"]
    if hits:
        out.extend(hits[: MAX_HITS + 6])
    else:
        out.append("(no python-kind hits, or the kind is unavailable)")
    out.append(CONVENTION)
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

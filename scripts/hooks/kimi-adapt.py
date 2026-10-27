#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Kimi Code hook adapter: run this repo's Claude hooks under Kimi.

Kimi Code (`kimi`) reads hooks only from the user-level
`~/.kimi-code/config.toml`; `scripts/kimi-setup` installs one `[[hooks]]`
entry per event that calls this file with the event name. The adapter then
dispatches to the hooks `.claude/settings.json` lists for that event, so
the Claude config stays the one list of guards — no second copy to drift.

Kimi's payload is Claude-shaped (`hook_event_name`, `tool_name`,
`tool_input`, `cwd`) and its tool names match Claude's (Bash, Edit, Write,
Read, Grep, Agent, `mcp__precis__*`). Differences bridged here:

- File tools key the path as `path`; the hooks read `file_path`, so the
  adapter adds `file_path` (and maps it back when a hook rewrites input).
- No `$CLAUDE_PROJECT_DIR`: set from the payload `cwd` (Kimi runs hooks in
  the session's project dir).
- Kimi blocks on exit 2 + stderr. A hook's `permissionDecision: deny` or
  `ask` becomes exit 2 with its reason (an `ask` passes once the command
  carries ASK_ACK); an `updatedInput` rewrite (Kimi has no input
  rewriting) becomes a block naming the corrected arguments.
- Hooks bound to Claude's own harness state (its transcript, subagents,
  process tree) are skipped: SKIP below. kimi-tree holds the worktree lock
  that session-start-lock.sh takes for Claude.

Fail-open like Claude: a hook that crashes or times out is ignored.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP = (
    "session-start-lock.sh",
    "session-end-reap.sh",
    "read-dedup.py",
    "session-size-nudge.py",
    "subagent-task-state.py",
    "edit-delegation-nudge.py",
    "agent-tier-nudge.py",
    "precompact-persist.sh",
)
FILE_TOOLS = ("Edit", "Write", "Read")
TIMEOUT_S = 60
# Kimi hooks cannot prompt. A hook's `ask` blocks; after the user approves,
# the agent re-issues the command with this prefix (the same escape hatch
# guard-prod-psql.py reads from its environment).
ASK_ACK = "ALLOW_PROD_WRITE=1"


def _matches(matcher: str, target: str) -> bool:
    if matcher in ("", "*"):
        return True
    return re.fullmatch(matcher, target) is not None


def _commands(settings: Path, event: str, target: str) -> list[str]:
    try:
        hooks = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    except (OSError, ValueError):
        return []
    out = []
    for group in hooks.get(event, []):
        if not _matches(group.get("matcher", ""), target):
            continue
        for h in group.get("hooks", []):
            cmd = h.get("command", "")
            if h.get("type") == "command" and not any(s in cmd for s in SKIP):
                out.append(cmd)
    return out


def _block(reason: str) -> None:
    sys.stderr.write(reason.strip() + "\n")
    sys.exit(2)


def _handle(stdout: str, tool_input: dict, context: list[str]) -> None:
    """Fold one hook's stdout into a block (exits) or extra context."""
    text = stdout.strip()
    if not text:
        return
    try:
        data = json.loads(text)
    except ValueError:
        context.append(text)
        return
    if not isinstance(data, dict):
        context.append(text)
        return
    spec = data.get("hookSpecificOutput") or {}
    decision = spec.get("permissionDecision") or data.get("decision")
    reason = spec.get("permissionDecisionReason") or data.get("reason") or ""
    if decision == "deny" or decision == "block":
        _block(reason or "blocked by a repo hook")
    if decision == "ask" and ASK_ACK not in str(tool_input.get("command", "")):
        _block(
            f"{reason}\nThis needs the user's confirmation. Ask them; once they "
            f"approve, re-issue the same command prefixed with `{ASK_ACK} `."
        )
    updated = spec.get("updatedInput")
    if isinstance(updated, dict) and updated != tool_input:
        if "file_path" in updated and "path" in tool_input:
            updated = {**updated, "path": updated["file_path"]}
            updated.pop("file_path")
        _block(
            f"{reason}\nRe-issue the call with these arguments: "
            + json.dumps(updated, ensure_ascii=False)
        )
    extra = spec.get("additionalContext") or data.get("systemMessage")
    if extra:
        context.append(str(extra))


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    event = event or payload.get("hook_event_name", "")
    root = Path(payload.get("cwd") or os.getcwd())
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool in FILE_TOOLS and "path" in tool_input and "file_path" not in tool_input:
        p = tool_input["path"]
        abs_path = p if os.path.isabs(p) else str(root / p)
        payload["tool_input"] = {**tool_input, "file_path": abs_path}
    target = tool if event.startswith(("PreToolUse", "PostToolUse")) else ""
    if event == "SessionStart":
        target = payload.get("source", "startup")
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root), "PRECIS_AGENT": "kimi"}
    if ASK_ACK in str(tool_input.get("command", "")):
        env["ALLOW_PROD_WRITE"] = "1"
    stdin = json.dumps(payload)
    context: list[str] = []
    for cmd in _commands(root / ".claude" / "settings.json", event, target):
        try:
            r = subprocess.run(
                ["bash", "-c", cmd],
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=root,
                env=env,
                timeout=TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 2:
            _block(r.stderr or r.stdout or "blocked by a repo hook")
        _handle(r.stdout, payload["tool_input"] if tool_input else {}, context)
    if context:
        print("\n".join(context))


if __name__ == "__main__":
    main()

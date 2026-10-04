"""Small Codex context/RTK trial, scoped to precis-mcp worktrees.

Why a separate adapter: Claude's lifecycle JSON and worktree RTK shim are
not interchangeable with Codex. Rewrite only simple read-only commands;
complete code searches and test exit codes remain untouched. Store counters
only so observing efficiency never creates another transcript/secret store.
Hook failures are advisory; sandbox and platform approvals still apply.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

CONTEXT = (
    "Token-efficiency trial: use RTK for noisy summaries/logs; keep full logs "
    "in your worktree and inspect targeted failure slices. For Python source "
    "discovery, MUST first try search(kind='python', q=..., scope='precis'), "
    "then symbol/outline get (skill precis-python-help). Do not dump whole "
    "files to orient. Verify the MCP root matches your worktree; never edit "
    "another checkout. Use raw rg for exhaustive searches. "
    "Read compact handoffs and owning docs once; "
    "fetch leaf context on demand. Record accepted results, not repeated status "
    "polls. Compression is not a completeness or release check."
)
LOG_HINT = (
    "RTK reminder: use rtk log for saved logs or bounded failure slices; "
    "preserve the full log and command exit code. Avoid repeated whole-file reads."
)
SOURCE_HINT = (
    "Fleet rule: Python source discovery MUST first try "
    "search(kind='python', q=..., scope='precis'); then symbol/outline get. "
    "Read precis-python-help once as needed. Do not dump entire files to "
    "orient. Verify the served root "
    "matches your task worktree before edits. Keep rg for exhaustive text "
    "checks, unindexed files or unavailable/mismatched roots."
)


def scoped(cwd: str) -> bool:
    """Match the owning checkout and its worktrees, not unrelated projects."""
    root = Path(__file__).resolve().parents[2]
    marker = "/.claude/worktrees/"
    primary = os.environ.get("PRECIS_EFFICIENCY_ROOT", str(root).split(marker, 1)[0])
    return cwd == primary or cwd.startswith(primary + "/")


def rewrite(command: str, tool_input: dict[str, Any]) -> str | None:
    """Ask RTK only about a narrow safe set; never evaluate shell text."""
    if tool_input.get("sandbox_permissions") == "require_escalated":
        return None
    if any(char in command for char in "\n\r$`<>|;&"):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    executable = shutil.which("rtk")
    if not executable or not words:
        return None
    if words[0] == "git" and len(words) >= 2 and words[1] in {"status", "log"}:
        # External diff/pager/config overrides aren't eligible for rewriting.
        if any(w.startswith(("--exec", "--ext-diff", "--config")) for w in words):
            return None
        result = subprocess.run(
            [executable, "rewrite", command],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        candidate = result.stdout.strip()
        if result.returncode == 0 and candidate.startswith("rtk git "):
            return candidate
    if len(words) == 2 and words[0] == "cat" and words[1].endswith(".log"):
        if not words[1].startswith("-"):
            return "rtk log " + shlex.quote(words[1])
    return None


def tick(payload: dict[str, Any], event: str, size: int = 0) -> int:
    """Atomic, per-session local counters; no commands, credentials or text."""
    import fcntl

    directory = Path(payload["cwd"]) / ".scratch" / "codex-efficiency"
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(
        str(payload.get("session_id", "unknown")).encode()
    ).hexdigest()[:20]
    path = directory / f"{key}.json"
    with (directory / f"{key}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = (
                json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            )
        except (ValueError, OSError):
            state = {}
        state[event] = int(state.get(event, 0)) + 1
        if size:
            state["tool_response_bytes"] = (
                int(state.get("tool_response_bytes", 0)) + size
            )
        temporary = path.with_suffix(f".{os.getpid()}.new")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, path)
        return int(state[event])


def respond(payload: dict[str, Any]) -> dict[str, Any]:
    cwd = payload.get("cwd", "")
    if not isinstance(cwd, str) or not scoped(cwd):
        return {}
    event = payload.get("hook_event_name", "")
    output: dict[str, Any] = {"hookEventName": event}
    if event in {"SessionStart", "PostCompact"}:
        output["additionalContext"] = CONTEXT
    elif event == "PreCompact":
        output["additionalContext"] = (
            "Before compaction: save a compact handoff with objective, decisions, "
            "holds, exact commits/tests, blockers, next action and docs already "
            "read. Keep pointers, not copied logs. Update your fleet note."
        )
    elif event == "PreToolUse":
        tool_input = payload.get("tool_input", {})
        if not isinstance(tool_input, dict):
            return {}
        command = tool_input.get("command", tool_input.get("cmd", ""))
        if not isinstance(command, str):
            return {}
        candidate = rewrite(command, tool_input)
        if candidate:
            tick(payload, "rewrites")
            updated = dict(tool_input)
            updated.pop("cmd", None)
            updated["command"] = candidate
            output.update(permissionDecision="allow", updatedInput=updated)
        elif any(word in command for word in ("cat ", "sed -n", "rg ", "grep ")):
            if tick(payload, "source_reminder_candidates") % 30 == 1:
                output["additionalContext"] = SOURCE_HINT
        elif any(
            word in command for word in ("tail ", "cat ", "sed -n", "scripts/test")
        ):
            if tick(payload, "reminder_candidates") % 30 == 1:
                output["additionalContext"] = LOG_HINT
    elif event == "PostToolUse":
        size = len(json.dumps(payload.get("tool_response", "")).encode())
        tick(payload, "results", size)
        if size > 20000 and tick(payload, "large_results") % 20 == 1:
            output["additionalContext"] = (
                "Large tool result: next time request a smaller slice or RTK "
                "summary; retain full output locally for completeness checks."
            )
    if len(output) == 1:
        return {}
    return {"hookSpecificOutput": output}


def main() -> None:
    try:
        result = respond(json.load(sys.stdin))
        if result:
            print(json.dumps(result))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        # An efficiency hint must never interfere with the original operation.
        return


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""PreToolUse hook (matcher: Edit|Write|MultiEdit|NotebookEdit|Agent|Task) —
nudge the MAIN loop to delegate a long un-delegated build to ``coder``.

Token-review (docs/backlog/token-review-hook-gaps.md, Rule F) found opus-tier
main loops doing 130-340 Edit/Write calls of mechanical multi-file feature work
with zero ``coder`` dispatches. This hook counts Edit/Write-family calls per
session; at ``THRESHOLD`` (50) with no ``coder`` dispatch since the last reset
it emits one additional-context nudge, then re-arms and nudges again every
further ``THRESHOLD`` edits.

- A dispatch to a build agent (``coder``, ``scaffold``, ``test-author``)
  resets the counter to zero — delegation happened, start over.
- Subagent calls are ignored: Claude Code stamps hook payloads from inside a
  subagent with ``agent_id``; only the main loop is counted.
- State: a small JSON file under the temp dir keyed by ``session_id``.

Advisory only (``additionalContext``, exit 0, never blocks); any parse or
state-IO problem stays silent. Wired in .claude/settings.json (PreToolUse).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

THRESHOLD = 50
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_AGENT_TOOLS = {"Agent", "Task"}
_BUILD_AGENTS = {"coder", "scaffold", "test-author"}


def _state_path(session: str) -> str:
    safe = "".join(c for c in session if c.isalnum() or c in "-_")
    return os.path.join(tempfile.gettempdir(), f"precis-editnudge-{safe}.json")


def _load(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            return int(json.load(f).get("edits", 0))
    except (OSError, ValueError, AttributeError):
        return 0


def _save(path: str, edits: int) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"edits": edits}, f)
    except OSError:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return 0  # subagent call (or junk): only the main loop is counted
    session = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    if not session:
        return 0
    tool = payload.get("tool_name")
    path = _state_path(str(session))

    if tool in _AGENT_TOOLS:
        ti = payload.get("tool_input") or {}
        if ti.get("subagent_type") in _BUILD_AGENTS:
            _save(path, 0)
        return 0
    if tool not in _EDIT_TOOLS:
        return 0

    edits = _load(path) + 1
    if edits < THRESHOLD:
        _save(path, edits)
        return 0
    _save(path, 0)  # re-arm: next nudge after another THRESHOLD edits
    note = (
        f"[delegate] {THRESHOLD} Edit/Write calls on the main loop with no "
        "`coder` dispatch. Mechanical multi-file implementation is what the "
        "`coder` agent (cheaper tier) is for: write a spec (files, intended "
        "behavior, how success is checked) and dispatch it, then review the "
        "diff — keep the main loop for design calls. Ignore if this is a "
        "genuinely design-heavy edit run."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": note,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

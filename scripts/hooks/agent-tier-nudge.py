#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook (matcher: Agent|Task) — nudge bare top-tier agent spawns
toward the cheap tier.

Token-review (gr509449) found fleet sessions dispatching ~68 bare
``general-purpose``/``claude`` spawns on fable/opus/default for work the
haiku defs (navigator, extract, tidy, gripe-filer, cmd-runner, cluster-ops)
cover. This hook fires when an Agent/Task call has a general-purpose/claude
(or missing) ``subagent_type`` AND a top-tier or unset ``model``, and emits
an advisory listing the cheap defs with their frontmatter one-liners.

- ``model`` unset or ``default`` bills the session model — top-tier in the
  fleet. An explicit ``sonnet``/``haiku`` stays silent.
- Names and descriptions are read live from ``.claude/agents/*.md``
  frontmatter so the message stays true as defs change; nothing is
  hard-coded.

Advisory only (``additionalContext``, exit 0, never blocks); any parse or
read problem stays silent — fail open.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_BARE_TYPES = {"general-purpose", "claude"}
_TOP_MODELS = {"fable", "opus", "default"}
_CHEAP_DEFS = (
    "navigator",
    "extract",
    "tidy",
    "gripe-filer",
    "cmd-runner",
    "cluster-ops",
)


def _agents_dir() -> Path:
    root = os.environ.get("CLAUDE_PROJECT_DIR")
    base = Path(root) if root else Path(__file__).resolve().parents[2]
    return base / ".claude" / "agents"


def _description(path: Path) -> str:
    """Pull the ``description:`` line out of a def's frontmatter block."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            return ""
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def _def_line() -> str:
    parts = []
    for name in _CHEAP_DEFS:
        desc = _description(_agents_dir() / f"{name}.md")
        parts.append(f"`{name}` ({desc})" if desc else f"`{name}`")
    return ", ".join(parts)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return 0

    stype = str(ti.get("subagent_type") or "").lower()
    if stype and stype not in _BARE_TYPES:
        return 0  # a named def was dispatched — cheap or deliberate
    model = str(ti.get("model") or "").lower()
    if model and model not in _TOP_MODELS:
        return 0  # explicit cheaper tier

    note = (
        "[agent-tier] bare agent spawn on a top-tier/unset model. Cheap haiku "
        f"defs cover most dispatch work — {_def_line()}. Name one via "
        "`subagent_type`, or pass `model: haiku`/`sonnet` explicitly. Ignore "
        "if this run genuinely needs top-tier judgment."
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

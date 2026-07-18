#!/usr/bin/env python3
"""PostToolUse hook: nudge to keep the orientation maps true.

Fires after a Write. If the new file is a handler / migration / ADR / skill —
the cases where a change usually needs a map or index updated — it feeds a
one-line reminder back to the session. For a new migration it also runs
`migration-check` so a duplicate number surfaces at *write* time, not just at
ship. Silent otherwise; never blocks (the tool already ran).

Wired in .claude/settings.json (PostToolUse, matcher "Write").
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# path fragment -> what to keep true
TRIGGERS = {
    "src/precis/handlers/": (
        "New handler — if this adds/renames a kind, update the kinds table "
        "in the precis-overview skill + the affordance index in CLAUDE.md."
    ),
    "src/precis/migrations/": (
        "New migration — forward-only (never edit a sealed file); regen the "
        "baseline snapshot at release time, not per-feature."
    ),
    "docs/decisions/": (
        "New/edited ADR — update docs/decisions/README.md (topic table + "
        "supersession graph); the older ADR names its successor and vice-versa."
    ),
    "src/precis/data/skills/": (
        "New/edited skill — if it adds a kind, update the precis-overview kinds "
        "table + the precis-toolpath call-sequences."
    ),
}


def _migration_collision(path: str) -> str:
    """If a new migration was written, return a collision warning (else '')."""
    if "src/precis/migrations/" not in path.replace("\\", "/"):
        return ""
    root = os.environ.get("CLAUDE_PROJECT_DIR", "")
    check = os.path.join(root, "scripts", "migration-check") if root else "scripts/migration-check"
    try:
        r = subprocess.run([check, "--quiet"], capture_output=True, text=True, timeout=15)
        if r.returncode != 0 and r.stdout.strip():
            return " ⚠ " + " ".join(r.stdout.split())
    except Exception:
        pass
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    path = (payload.get("tool_input") or {}).get("file_path", "")
    if not isinstance(path, str):
        return 0

    for fragment, note in TRIGGERS.items():
        if fragment in path.replace("\\", "/"):
            note += _migration_collision(path)
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": f"[map-staleness] {note}",
                        }
                    }
                )
            )
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

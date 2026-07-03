#!/usr/bin/env python3
"""PostToolUse hook: nudge to keep the orientation maps true.

Fires after a Write. If the new file is a handler or a migration — the two
cases where a new kind / schema change usually needs the maps updated — it
feeds a one-line reminder back to the session. Silent otherwise, so it stays
low-noise. Never blocks: the tool already ran.

Wired in .claude/settings.json (PostToolUse, matcher "Write").
"""

from __future__ import annotations

import json
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
}


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

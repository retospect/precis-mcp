#!/usr/bin/env -S uv run --no-project --python >=3.12 python
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
    "docs/backlog/": (
        "Backlog touched — if this ship completes an item, DELETE its file in "
        "the same commit and fold surviving truth into the owning package "
        "docstring (docs/README.md)."
    ),
    "src/precis/data/skills/": (
        "New/edited skill — if it adds a kind, update the precis-overview kinds "
        "table + the precis-toolpath call-sequences."
    ),
}

#: Triggers that fire only on an EDIT to an existing item — a freshly
#: written (still git-untracked) file gets no reminder (gr514353).
_CREATE_SILENT = {"docs/backlog/"}


def _git_tracked(path: str) -> bool | None:
    """True: tracked; False: untracked (a new file); None: can't tell."""
    try:
        r = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", os.path.basename(path)],
            cwd=os.path.dirname(path) or None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return None


# Bound for the migration-number advisory subprocess. A fleet-sized scan
# used to run ~90 s, so the timeout swallowed exactly the warning this hook
# exists to print — surface it as a run-it-by-hand nudge instead. Override
# with PRECIS_MIGRATION_CHECK_TIMEOUT (float seconds; tests use <1).
MIGRATION_CHECK_TIMEOUT = float(os.environ.get("PRECIS_MIGRATION_CHECK_TIMEOUT", "15"))


def _migration_collision(path: str) -> str:
    """If a new migration was written, return a collision warning (else '')."""
    if "src/precis/migrations/" not in path.replace("\\", "/"):
        return ""
    root = os.environ.get("CLAUDE_PROJECT_DIR", "")
    check = (
        os.path.join(root, "scripts", "migration-check")
        if root
        else "scripts/migration-check"
    )
    try:
        r = subprocess.run(
            [check, "--quiet"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=MIGRATION_CHECK_TIMEOUT,
        )
        if r.returncode != 0 and r.stdout.strip():
            return " ⚠ " + " ".join(r.stdout.split())
    except subprocess.TimeoutExpired:
        return (
            f" ⚠ migration-check timed out (>{MIGRATION_CHECK_TIMEOUT:g}s)"
            " — run `scripts/migration-check` by hand."
        )
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
            if fragment in _CREATE_SILENT and _git_tracked(path) is False:
                return 0  # a brand-new item: the delete-on-ship note doesn't apply yet
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

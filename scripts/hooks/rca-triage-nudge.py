#!/usr/bin/env python3
"""PreToolUse hook (matcher: Edit|Write|MultiEdit|NotebookEdit) — nudge toward
bug triage before a source edit.

A patch that fixes a symptom can mask a deeper defect, leaving it live and
harder to find — see the `bug` skill's three-bucket triage model. This hook
fires a single, one-line reminder the first time this session edits under
``src/`` (where a bug fix actually lands): if this is a bug fix, triage first
— could the obvious fix be masking a deeper defect? If so, dispatch the
`root-cause` agent (read-only, traces symptom→true defect) *before* patching.

Deliberately low-frequency: a nudge on every edit gets tuned out, so this
fires **once per session** (a marker file under the temp dir, keyed by
``CLAUDE_SESSION_ID`` when present, else a per-worktree marker) and stays
silent after. Scoped to ``src/`` — tests, docs, skills, and migrations aren't
where a masked-symptom patch would land. Never blocks (the tool still runs);
silent unless it's the first src/ edit this session.

Wired in .claude/settings.json (PreToolUse, matcher
"Edit|Write|MultiEdit|NotebookEdit").
"""

from __future__ import annotations

import json
import os
import sys
import tempfile


def _marker_path(payload: dict, project_dir: str | None) -> str:
    """Per-session marker path — CLAUDE_SESSION_ID when available (payload or
    env), else a stable per-worktree fallback under the project dir."""
    session = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    if session:
        return os.path.join(tempfile.gettempdir(), f"rca-nudge-{session}")
    if project_dir:
        return os.path.join(project_dir, ".claude", ".rca-nudge-fired")
    return os.path.join(tempfile.gettempdir(), "rca-nudge-fallback")


def _file_path(ti: dict) -> str:
    fp = ti.get("file_path") or ti.get("notebook_path")
    return fp if isinstance(fp, str) else ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    ti = payload.get("tool_input") or {}
    fp = _file_path(ti)
    if not fp or "src/" not in fp.replace(os.sep, "/"):
        return 0

    marker = _marker_path(payload, os.environ.get("CLAUDE_PROJECT_DIR"))
    if os.path.exists(marker):
        return 0
    try:
        with open(marker, "w") as f:
            f.write("fired\n")
    except OSError:
        return 0  # can't write the marker — stay silent rather than repeat

    note = (
        "[bug] editing under src/ — if this is a bug fix, triage first: could "
        "the obvious fix be masking a deeper defect? If so, dispatch the "
        "`root-cause` agent (read-only, traces symptom→true defect) before "
        "patching — see the `bug` skill."
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

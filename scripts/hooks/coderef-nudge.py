#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook (matcher: Grep) — nudge a symbol grep toward the python kind.

When the loop greps for a bare Python IDENTIFIER (a symbol or string-value
lookup, not a text/regex search), ``search(kind='python', ...)`` is the
better first call — lexical/semantic finds where the symbol lives,
``mode='literal'`` finds every use of a string VALUE, and
``scripts/coderef callers <file.py::Sym>`` is the exact who-calls once the
symbol is named. This injects a one-line, NON-BLOCKING reminder naming the
right tool per intent (gr520146: a nudge that only points at coderef's
who-calls loses to grep on where/concept questions).

Grep stays correct for text — strings, comments, config, non-Python, or a
symbol you can't yet name — and for anything outside this repo
(installed packages, venvs, other checkouts the index can't see), so the
trigger is deliberately narrow (bare identifier + Python target INSIDE the
session tree) to stay credible: a nudge that fires on every grep gets tuned
out. Never blocks (the tool still runs); silent unless it matches.

Wired in .claude/settings.json (PreToolUse, matcher "Grep").
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _symbol_heuristic import (
    is_symbol_candidate,
    path_arg_is_pythonish,
    path_leaves_project,
)


def _python_target(ti: dict, cwd: str = "") -> bool:
    """True unless the search is clearly scoped away from Python code."""
    glob = str(ti.get("glob") or "").lower()
    typ = str(ti.get("type") or "").lower()
    if glob:
        return "py" in glob
    if typ:
        return typ in ("py", "python")
    path = str(ti.get("path") or "")
    if path:
        if path_leaves_project(path, cwd):
            return False
        verdict = path_arg_is_pythonish(path, cwd)
        if verdict is not None:
            return verdict
    return True  # unscoped → treat as code-ish


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    ti = payload.get("tool_input") or {}
    pattern = ti.get("pattern")
    if not isinstance(pattern, str):
        return 0
    tok = pattern.strip()
    if not is_symbol_candidate(tok):
        return 0
    if not _python_target(ti, str(payload.get("cwd") or "")):
        return 0

    note = (
        f"[pykind] grepping for the symbol `{tok}` — the python kind "
        "usually answers in one call: "
        f"search(kind='python', mode='lexical'|'semantic', q='{tok}') "
        f"finds where it lives, mode='literal' finds every use of "
        f"'{tok}' as a string VALUE, and `scripts/coderef callers "
        f"<file.py::{tok}>` is the exact who-calls once it's named. "
        "Grep stays right for text, non-Python files and targets "
        "outside this repo."
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

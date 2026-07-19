#!/usr/bin/env python3
"""PreToolUse hook (matcher: Grep) — nudge a symbol grep toward coderef.

When the loop greps for a bare Python IDENTIFIER (a symbol lookup, not a
text/regex search), a call-graph query is exact where grep is fuzzy:
``scripts/coderef callers <file.py::Sym>`` finds real references with no
same-named false positives, and ``deps <file.py::Sym>`` pulls the connected
definitions. This injects a one-line, NON-BLOCKING reminder in that case only.

Grep stays correct for text — strings, comments, config, non-Python, or a
symbol you can't yet name — so the trigger is deliberately narrow (bare
identifier + Python target) to stay credible: a nudge that fires on every grep
gets tuned out. Never blocks (the tool still runs); silent unless it matches.

Wired in .claude/settings.json (PreToolUse, matcher "Grep").
"""

from __future__ import annotations

import json
import re
import sys

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Bare words that are almost never a symbol lookup worth a call-graph query —
# keeps the nudge from firing on common English / keyword greps.
_STOP = {
    "test", "todo", "fixme", "note", "true", "false", "none", "null",
    "self", "cls", "def", "class", "import", "return", "async", "await",
    "the", "and", "for", "not", "with", "type", "data", "value", "error",
    "name", "path", "file", "line", "text", "main", "init",
}


def _python_target(ti: dict) -> bool:
    """True unless the search is clearly scoped away from Python code."""
    glob = str(ti.get("glob") or "").lower()
    typ = str(ti.get("type") or "").lower()
    if glob:
        return "py" in glob
    if typ:
        return typ in ("py", "python")
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
    if len(tok) < 3 or not _IDENT.match(tok) or tok.lower() in _STOP:
        return 0
    if not _python_target(ti):
        return 0

    note = (
        f"[coderef] grepping for the symbol `{tok}` — for who-calls / "
        f"what-depends-on over Python, `scripts/coderef callers <file.py::{tok}>` "
        "(or `deps`) is exact: no same-named false positives, and it returns the "
        "connected code, not every text hit. Grep stays right for "
        "text/strings/non-Python or a symbol you can't yet name."
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

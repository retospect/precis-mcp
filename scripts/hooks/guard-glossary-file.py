#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook: refuse to recreate or edit ``docs/glossary.md``.

The repo-dev glossary was retired on 2026-11-03 (Reto, gripe gr559340): the
coined and overloaded vocabulary lives in the prod taxonomy as one ``taxon``
per term under the root ``tn559460`` ("repo-dev vocabulary"), one hub node per
memory subject. A session working from an old brief, a stale CLAUDE.md copy or
a spec that still says "add it to the glossary" would otherwise recreate the
file; this hook turns that into the lookup instead.

Denied: Edit/Write/MultiEdit/NotebookEdit whose target path ends in
``docs/glossary.md`` (any worktree). Everything else is allowed.

Escape hatch: ``ALLOW_GLOSSARY_FILE=1`` (reading an old branch's copy back is
``git show <sha>:docs/glossary.md``, which needs no hatch).

Wired in ``.claude/settings.json`` (PreToolUse, matcher
``Edit|Write|MultiEdit|NotebookEdit``). Mirrors ``guard-sealed-migration.py``.
"""

from __future__ import annotations

import json
import os
import sys

_TAIL = "docs/glossary.md"
_LOOKUP = "search(kind='taxon', q='<term>', args={'under': 'tn559460'})"


def evaluate(path: str) -> str | None:
    """Return a deny reason, or ``None`` to allow."""
    if not isinstance(path, str):
        return None
    p = path.replace("\\", "/")
    if p == _TAIL or p.endswith("/" + _TAIL):
        return (
            "Refusing to write docs/glossary.md: the glossary was retired on "
            "2026-11-03 (gr559340). Coined and overloaded terms are taxa under "
            "tn559460 (repo-dev vocabulary): look one up with " + _LOOKUP + "; "
            "add one with put(kind='taxon', text='<term> — <gloss> → <pointer>', "
            "meta={'pointer': '<pointer>'}, link='taxon:<hub tn>', "
            "rel='specialises'). Set ALLOW_GLOSSARY_FILE=1 to override."
        )
    return None


def main() -> int:
    if os.environ.get("ALLOW_GLOSSARY_FILE"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    path = (payload.get("tool_input") or {}).get("file_path", "")
    reason = evaluate(path)
    if reason is None:
        return 0
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

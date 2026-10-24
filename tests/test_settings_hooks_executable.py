"""Every hook command in ``.claude/settings.json`` runs a file that exists
and carries the exec bit (gr514921).

A hook committed without the exec bit dies with exit 126 and, once
landed, is silently dead — nothing else checks. ``settings.json`` is
read, never written, by this test; only ``$CLAUDE_PROJECT_DIR``-relative
paths are asserted (env-var paths outside the tree are skipped).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SETTINGS = _ROOT / ".claude" / "settings.json"

#: A ``$CLAUDE_PROJECT_DIR/<path>`` reference inside a hook command —
#: quoted or bare. The variable is always followed by a ``/``.
_PROJECT_PATH = re.compile(r"\$CLAUDE_PROJECT_DIR/([A-Za-z0-9_./-]+)")


def _hook_commands(node: object) -> list[str]:
    """Every ``command`` string anywhere under the ``hooks`` object."""
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "command" and isinstance(value, str):
                out.append(value)
            else:
                out.extend(_hook_commands(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_hook_commands(item))
    return out


def _referenced_paths() -> list[tuple[str, str]]:
    """(command, repo-relative path) pairs for every project-dir reference."""
    settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    pairs = [
        (cmd, m.group(1))
        for cmd in _hook_commands(settings.get("hooks", {}))
        for m in _PROJECT_PATH.finditer(cmd)
    ]
    assert pairs, "no $CLAUDE_PROJECT_DIR hook paths found — settings moved?"
    return pairs


def test_every_referenced_hook_file_exists_and_is_executable() -> None:
    missing: list[str] = []
    not_exec: list[str] = []
    for cmd, rel in _referenced_paths():
        path = _ROOT / rel
        if not path.is_file():
            missing.append(f"{rel}  (in: {cmd})")
        elif not os.access(path, os.X_OK):
            not_exec.append(f"{rel}  (in: {cmd})")
    problems = missing + not_exec
    assert not problems, (
        "hook files referenced by .claude/settings.json that are missing "
        "or lack the exec bit (a non-executable hook dies with exit 126):\n  "
        + "\n  ".join(problems)
    )

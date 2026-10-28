#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook: block bare ``git stash`` / ``git stash pop`` in a worktree.

The footgun this closes: the git stash stack is **shared** across the primary
checkout and every worktree, and sibling Claude sessions push/pop concurrently.
A bare ``git stash`` buries your changes onto that shared stack; a ``git stash
pop`` can grab **another session's** entry (wrong changes applied, then dropped).

Allowed (untouched): ``git stash push -u -m "<tag>"`` (tagged, findable),
``git stash apply <sha>`` (the safe restore — apply, never pop), and the
read/manage verbs ``list`` / ``show`` / ``drop`` / ``branch`` / ``clear``.
Denied: bare ``git stash`` (or ``push`` without ``-m``) and any ``git stash pop``.

Why pop stays refused (gr525771): pop is apply+drop, and the drop half is
the irreversible one — a wrong-entry pop destroys a sibling session's stash.
There is no safe spelling to admit: pop takes only a positional
``stash@{n}`` (git rejects a sha with "not a stash reference"), and a bare
pop grabs ``stash@{0}``, the newest entry on the SHARED stack. So the
refusal prints the recovery instead: list with ``%gd %H %gs`` (index + sha
+ tag), ``apply <sha>``, then re-list and ``drop stash@{n}`` matched by sha
— the index is positional and shifts under any concurrent push/pop.

Escape hatch: ``ALLOW_GIT_STASH=1``. Prefer a throwaway WIP commit to set work
aside. See the stash note in ``docs/conventions/shipping.md`` and the
worktree discipline.

Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``).
"""

from __future__ import annotations

import json
import os
import re
import sys

# ``git [ -C x | -c k=v ]* stash <rest-of-segment>`` — captures the args.
_STASH_RE = re.compile(r"\bgit\b(?:\s+-[cC]\s+\S+)*\s+stash\b([^\n;&|]*)")
_SAFE_SUBCMDS = {"list", "show", "drop", "clear", "branch", "apply", "store", "create"}


def evaluate(command: str) -> str | None:
    """Return a deny reason, or ``None`` to allow. Pure & testable."""
    if not isinstance(command, str) or "stash" not in command:
        return None
    for m in _STASH_RE.finditer(command):
        args = m.group(1).split()
        sub = args[0] if args else ""
        if sub in _SAFE_SUBCMDS:
            continue
        if sub == "pop":
            return (
                "Refusing `git stash pop` — pop is apply+drop on a stash stack "
                "SHARED across all worktrees and sibling sessions; it takes only "
                "a positional stash@{n} (git rejects a sha), so a bare pop can "
                "apply AND DESTROY another session's entry. Recover by sha:\n"
                "  1. git stash list --format='%gd %H %gs'  # index, sha, tag\n"
                "  2. git stash apply <sha>                 # restores, entry kept\n"
                "  3. git stash drop stash@{n}              # re-run step 1 right "
                "before dropping: the index is POSITIONAL and shifts on any "
                "push/pop — match the sha, not the line number\n"
                "Escape: ALLOW_GIT_STASH=1."
            )
        # bare `git stash` or `git stash push` — require an -m tag to stay findable
        if sub in ("", "push", "save") and not re.search(r"(^|\s)-m(\s|=)", m.group(1)):
            return (
                "Refusing bare `git stash` — the stash stack is SHARED across "
                "worktrees/sessions, so an untagged entry is unfindable and pop-able "
                "by another session. Prefer a throwaway WIP commit; if you must "
                'stash, use `git stash push -u -m "<unique-tag>"`, then restore '
                "with `git stash apply <sha>`. Or set ALLOW_GIT_STASH=1."
            )
    return None


def main() -> int:
    if os.environ.get("ALLOW_GIT_STASH"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    command = (payload.get("tool_input") or {}).get("command", "")
    reason = evaluate(command)
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

#!/usr/bin/env python3
"""PreToolUse hook: block ``git commit`` when the target branch is ``main``.

The footgun this closes: a worktree session's cwd drifts into the PRIMARY
checkout (or the command ``cd``s there), a ``git commit`` runs, and the work
lands **directly on ``main``** — bypassing the worktree AND the ``scripts/ship``
integration gate (ruff/mypy/pytest). ``scripts/ship`` refuses to run on
``main``, but only at *ship* time; by then the commits already exist and have
to be surgically moved back onto a feature branch. This denies the commit
up-front instead, mirroring ``guard-worktree-path.py`` (remove the *ability* to
do the wrong thing).

Scope, deliberately narrow to stay false-positive-free:
- Only ``git commit`` (incl. ``--amend``) — NOT ``git merge`` (a
  ``git merge --ff-only origin/main`` to sync the primary is legitimate), NOT
  ``commit-tree`` (``scripts/ship``'s plumbing), NOT cherry-pick/revert.
- Only when the resolved branch is ``main``/``master``. Commits on any feature
  / ``worktree-*`` / ``docs-*`` branch pass untouched.
- Follows a leading ``cd <path>`` and ``git -C <path>`` so the branch is read
  at the dir the commit actually targets, not just the session cwd.

Escape hatch: set ``ALLOW_COMMIT_ON_MAIN=1`` in the environment for the rare
legitimate direct-to-main commit.

Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``). See the
``worktree_edit_path_trap`` memory and CLAUDE.md's ship workflow.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

PROTECTED = {"main", "master"}

# A ``git … commit`` invocation, excluding the ``commit-tree`` plumbing verb.
_COMMIT_RE = re.compile(r"\bgit\b[^\n;&|]*?\bcommit\b(?!-tree)")
# ``git -C <path>`` inside a single segment.
_GIT_C_RE = re.compile(r"\bgit\b\s+(?:-c\s+\S+\s+)*-C\s+(['\"]?)([^'\"\s]+)\1")
# A segment that is *only* a ``cd <path>``.
_CD_RE = re.compile(r"""^\s*cd\s+(['\"]?)([^'\"]+)\1\s*$""")


def _branch(cwd: str) -> str:
    """Current branch at ``cwd``; empty string on any failure / detached."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _resolve(base: str, path: str) -> str:
    path = os.path.expanduser(path.strip())  # strip: a `cd /x && …` split leaves a trailing space
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def evaluate(command: str, cwd: str) -> str | None:
    """Return a deny reason, or ``None`` to allow. Pure & testable.

    Walks the command's ``&&`` / ``;`` / newline segments left-to-right,
    tracking the effective directory (updated by ``cd``), and denies the first
    ``git commit`` whose resolved branch is protected.
    """
    if not isinstance(command, str) or "commit" not in command:
        return None

    cur = cwd
    for seg in re.split(r"&&|\|\||;|\n", command):
        cd = _CD_RE.match(seg)
        if cd:
            cur = _resolve(cur, cd.group(2))
            continue
        if not _COMMIT_RE.search(seg):
            continue
        # This segment commits. Read the branch at its target dir.
        gc = _GIT_C_RE.search(seg)
        target = _resolve(cur, gc.group(2)) if gc else cur
        branch = _branch(target)
        if branch in PROTECTED:
            return (
                f"Refusing `git commit` on `{branch}` (at {target}). That lands "
                "work directly on the primary branch, bypassing the worktree "
                "and the scripts/ship gate. Commit inside a `claude -w` "
                "worktree on a feature branch and let /endsession (scripts/ship) "
                "merge it. If this really is intended, set ALLOW_COMMIT_ON_MAIN=1."
            )
    return None


def main() -> int:
    if os.environ.get("ALLOW_COMMIT_ON_MAIN"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # unparseable → never block
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command", "")
    cwd = payload.get("cwd") or os.getcwd()

    reason = evaluate(command, cwd)
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

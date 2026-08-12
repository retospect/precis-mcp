#!/usr/bin/env python3
"""PreToolUse hook: in a WORKTREE session, block a Bash command that ``cd``\\s
into the PRIMARY checkout (or any tree outside this worktree) before running
something there.

The footgun this closes (observed live 2026-07-25, cost a near-derailed
``/go``): a worktree session prefixes shell commands with ``cd <main-repo> &&
…``. The Bash tool already resets cwd to the worktree every call, so the ``cd``
is never needed — but the primary-repo absolute path is the most *available*
string in session context (CLAUDE.md header, the code-search SessionStart hint's
``path="…"`` arg, memory), so it's the one that gets grabbed. The result: the
Edit/Read tools (correct worktree abs paths) mutate the *worktree* while the
cd-prefixed shell reads/writes the *primary* checkout. The two diverge silently
and it reads as "my edits don't persist" — hours of churn chasing a phantom
"filesystem desync". CLAUDE.md and the ``worktree_edit_path_trap`` memory both
warn against it in prose; prose didn't prevent it. This denies the *command* up
front.

Scope, deliberately narrow to stay false-positive-free:
- Only fires in a **worktree** session (cwd resolves into a linked
  ``.claude/worktrees/*`` tree, not the primary). A primary session's ``cd`` is
  a no-op for this trap and is left alone (the checkout/commit guards cover the
  primary's real footguns).
- Only denies a **real command segment** whose effective cwd (after following
  leading ``cd`` segments) lands *inside the primary tree but outside this
  worktree*. A lone ``cd <primary>`` with nothing chained after it is harmless
  (cwd resets) and is allowed; ``cd`` within the worktree, ``cd /tmp/…`` /
  scratchpad, and ``git -C <path>`` (the recommended way to reach another
  checkout) are all untouched.

Escape hatch: set ``ALLOW_CD_TO_PRIMARY=1`` for the rare legitimate case.

Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``). Mirrors
``scripts/hooks/guard-checkout-in-primary.py`` structurally (segment walk, cwd
tracking, ``_is_primary``). See the ``worktree_edit_path_trap`` memory.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# A segment that is *only* a ``cd <path>`` (the caller has already split the
# command on ``&&`` / ``;`` / ``||`` / newline, so a segment is one command).
# A trailing redirection (``2>/dev/null``, ``>/x``, ``2>&1`` …) is tolerated and
# dropped: without this the greedy path group swallowed the redirect (e.g.
# ``cd <primary> 2>/dev/null``), yielding a path that ``_under`` no longer saw as
# inside the primary — so the segment slipped past the deny. An audit caught that
# exact evasion running live in a worktree session.
_CD_RE = re.compile(
    r"""^\s*cd\s+(['\"]?)(?P<path>[^'\"]+?)\1\s*(?:\d*[<>&]+\s*\S+\s*)*$"""
)


def _git(cwd: str, *args: str) -> str:
    """Run ``git <args>`` at ``cwd``; empty string on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _resolve(base: str, path: str) -> str:
    path = os.path.expanduser(
        path.strip()
    )  # strip: a `cd /x && …` split leaves a trailing space
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def _under(path: str, root: str) -> bool:
    """True when ``path`` is ``root`` itself or nested inside it (normalised,
    boundary-safe: ``/a/b`` is not under ``/a/bc``)."""
    p, r = os.path.normpath(path), os.path.normpath(root)
    return p == r or p.startswith(r + os.sep)


def _tops(cwd: str) -> tuple[str, str] | None:
    """``(primary_top, worktree_top)`` for ``cwd``, or ``None`` when the layout
    can't be read or ``cwd`` isn't a linked worktree.

    The primary and every linked worktree share one ``--git-common-dir``; the
    primary's toplevel equals that common dir's parent, a linked worktree's does
    not. So ``primary_top = dirname(common)`` and, when the current toplevel
    differs from it, this is a worktree session.
    """
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not common or not top:
        return None
    primary_top = os.path.normpath(os.path.dirname(common))
    worktree_top = os.path.normpath(top)
    if worktree_top == primary_top:
        return None  # primary session — not our concern
    return primary_top, worktree_top


def evaluate(command: str, cwd: str) -> str | None:
    """Return a deny reason, or ``None`` to allow. Pure & testable.

    Walks the command's ``&&`` / ``;`` / ``||`` / newline segments left-to-
    right, tracking the effective directory (updated by ``cd``), and denies the
    first *command* segment whose effective cwd is inside the primary tree but
    outside this worktree.
    """
    if not isinstance(command, str) or "cd" not in command:
        return None
    tops = _tops(cwd)
    if tops is None:
        return None
    primary_top, worktree_top = tops

    cur = cwd
    for seg in re.split(r"&&|\|\||;|\n", command):
        cd = _CD_RE.match(seg)
        if cd:
            cur = _resolve(cur, cd.group(2))
            continue
        if not seg.strip():
            continue  # empty segment (e.g. trailing `&&`) — not a command
        if _under(cur, primary_top) and not _under(cur, worktree_top):
            return (
                f"Refusing to run a command after `cd` into {cur} — that's the "
                f"PRIMARY checkout ({primary_top}), not this worktree "
                f"({worktree_top}). The Edit/Read tools write the worktree while "
                "this shell would touch the primary; they diverge silently (the "
                "'my edits don't persist' trap). Run Bash bare — cwd is already "
                "the worktree — or use `git -C <path>` to reach another checkout. "
                "If this is really intended, set ALLOW_CD_TO_PRIMARY=1."
            )
    return None


def main() -> int:
    if os.environ.get("ALLOW_CD_TO_PRIMARY"):
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

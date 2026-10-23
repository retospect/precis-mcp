#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook: auto-correct Edit/Write into the MAIN checkout from a worktree.

The worktree edit-path trap: when a session runs inside a linked git worktree
(``.claude/worktrees/<name>/``), an *absolute* ``file_path`` that points at the
**main** checkout root (``…/precis-mcp/src/…``) instead of the worktree is a
perfectly valid path — it just edits the wrong tree. The Edit/Write "succeeds"
but the change never reaches the worktree that the gate / ``scripts/ship``
actually test, so the work silently lands nowhere useful (Bash escapes this
because its cwd *is* the worktree, so relative paths there are fine — only the
file-tool absolute paths mis-target). This hook turns that silent mis-write
into an immediate, transparent **auto-correct**: it rewrites the tool call's
path to the worktree twin and lets it proceed (``permissionDecision: "allow"``
+ ``updatedInput``), instead of denying and forcing a retry.

Only fires inside a worktree (``git rev-parse --show-toplevel`` differs from the
main root) and only corrects a main-root path whose *worktree twin exists* (or
whose parent dir exists) — i.e. a genuine mis-target of a repo file, never an
external root (a sibling repo, the scratchpad, ``~/.claude``) or a
deliberate main-only path. Anything outside that unambiguous case is left
alone (return ``None`` → allow, untouched).

Wired in ``.claude/settings.json`` (PreToolUse, matcher
``Edit|Write|MultiEdit|NotebookEdit``). Edit/Write/MultiEdit all key the path
under ``file_path``; ``NotebookEdit`` uses ``notebook_path`` instead, which
this hook also inspects. See the ``worktree_edit_path_trap`` memory (the
original backlog write-up lives in git history).

Castor-lane strict mode (gr506125): when ``PRECIS_CASTOR_LANE`` is set
(``scripts/claude-local`` exports it), the lane's model can't be trusted to
stay inside its task tree — the gpt-oss re-pilot aimed every Edit at the
main checkout's absolute path and stalled. On top of the twin redirect
(which still runs first — a fix beats a refusal), any remaining path that
resolves outside the session's worktree root — a main-only path, a sibling
worktree, an external root, or a relative path that escapes cwd — is DENIED
with a reason naming the tree root, so the next attempt lands inside it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable


def _git(cwd: str, *args: str) -> str:
    """Run a read-only git command; empty string on any failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def evaluate(
    file_path: str,
    wt_root: str,
    main_root: str,
    *,
    exists: Callable[[str], bool] = os.path.exists,
    isdir: Callable[[str], bool] = os.path.isdir,
) -> str | None:
    """Return the corrected worktree-twin path, or ``None`` if no correction is
    needed. Pure & testable.

    ``wt_root`` is the current worktree root; ``main_root`` is the main
    checkout root (parent of the shared ``.git``). Existence checks are
    injectable so the path logic can be tested without a real filesystem.
    """
    if not file_path or not os.path.isabs(file_path):
        return None  # relative / missing paths resolve against the worktree cwd
    wt_root = os.path.normpath(wt_root)
    main_root = os.path.normpath(main_root)
    # Not a worktree session (main root == worktree root): nothing to guard.
    if wt_root == main_root:
        return None
    fp = os.path.normpath(file_path)
    # A worktree path is correct — allow, untouched. (The worktree lives UNDER
    # main_root, so this check must come first, before the main-root
    # containment test.)
    if fp == wt_root or fp.startswith(wt_root + os.sep):
        return None
    # Only guard paths inside the MAIN checkout root. External roots
    # (a sibling repo, scratchpad, ~/.claude) are legitimately elsewhere.
    if not (fp == main_root or fp.startswith(main_root + os.sep)):
        return None
    rel = os.path.relpath(fp, main_root)
    twin = os.path.join(wt_root, rel)
    # Correct only when the worktree actually has this file (or its parent
    # dir) — a real mis-target of a repo file, not a deliberate main-only
    # write. Anything else: leave it alone (None → allow as-is).
    if exists(twin) or isdir(os.path.dirname(twin)):
        return twin
    return None


def _castor_check(file_path: str, wt_root: str, cwd: str) -> dict | None:
    """Castor-lane containment: the JSON to emit when ``file_path`` escapes
    the session's tree, else ``None``. Runs after :func:`evaluate`, so only
    paths the twin-redirect could not fix reach here."""
    root = os.path.realpath(wt_root)
    fp = (
        os.path.realpath(file_path)
        if os.path.isabs(file_path)
        else os.path.realpath(os.path.join(cwd, file_path))
    )
    if fp == root or fp.startswith(root + os.sep):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "castor lane (PRECIS_CASTOR_LANE): file tools only write "
                f"inside this session's task tree {root}; "
                f"{fp} is outside it — retry with a path under the tree "
                "(a relative path resolves against it)"
            ),
        }
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # unparseable input → never block

    tool_input = payload.get("tool_input") or {}
    # Edit/Write/MultiEdit all key the path under file_path; NotebookEdit uses
    # notebook_path instead. Try both so NotebookEdit isn't a silent no-op.
    key = "file_path"
    file_path = tool_input.get(key, "")
    if not isinstance(file_path, str) or not file_path:
        key = "notebook_path"
        file_path = tool_input.get(key, "")
    if not isinstance(file_path, str) or not file_path:
        return 0
    cwd = payload.get("cwd") or os.getcwd()

    wt_root = _git(cwd, "rev-parse", "--show-toplevel")
    common = _git(cwd, "rev-parse", "--git-common-dir")
    if not wt_root or not common:
        return 0  # not a git repo / git unavailable → don't interfere
    if not os.path.isabs(common):
        common = os.path.join(wt_root, common)
    main_root = os.path.dirname(os.path.realpath(common))

    twin = evaluate(
        os.path.realpath(file_path),
        os.path.realpath(wt_root),
        main_root,
    )
    if twin is None:
        if os.environ.get("PRECIS_CASTOR_LANE"):
            denied = _castor_check(file_path, wt_root, cwd)
            if denied is not None:
                print(json.dumps(denied))
        return 0
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": (
                        "Worktree path trap: redirected main-checkout path to "
                        f"the worktree twin:\n  {twin}"
                    ),
                    "updatedInput": {**tool_input, key: twin},
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

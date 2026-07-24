#!/usr/bin/env python3
"""PreToolUse hook: block ``git checkout <branch>`` / ``git switch <branch>``
when run inside the PRIMARY checkout and the target isn't ``main``/``master``.

The footgun this closes: sessions/agents sometimes work *directly in the
primary* (no ``claude -w``) and check out a feature branch there. Nothing
else stops it — ``guard-commit-on-main.py`` blocks the *commit* that would
land on ``main``, but by the time a branch is checked out the primary has
already drifted; ``scripts/ship`` doesn't touch the primary's HEAD either (it
only fast-forwards ``main`` there). The result: the primary checkout ends up
parked on a stale feature branch (``devin-port``, ``chore-drop-dead-cluster-
dir``, …) instead of ``main``. This denies the *checkout* up front, so the
primary only ever moves via ``scripts/hooks/heal-primary-branch.sh`` (back to
``main``) or a deliberate override.

Scope, deliberately narrow to stay false-positive-free:
- Only ``git checkout <branch>`` / ``git checkout -b <branch>`` (or ``-B``)
  and ``git switch <branch>`` / ``git switch -c <branch>`` (or ``-C``).
- NOT ``git switch -`` / ``git checkout -`` (return to the previous branch —
  a common, safe toggle, not a fresh drift).
- NOT ``git checkout -- <file>`` or ``git checkout <file>`` — a bare
  ``checkout <name>`` is only treated as a branch checkout when ``<name>``
  resolves to an existing local branch (``refs/heads/<name>``); otherwise
  it's assumed to be a pathspec and left alone.
- Only denied when the *resolved* cwd (following a leading ``cd <path>`` /
  ``git -C <path>``, exactly like the sibling guard) is the PRIMARY checkout
  — i.e. NOT one of the linked ``.claude/worktrees/*`` trees — and the target
  branch isn't ``main``/``master``.

Escape hatch: set ``ALLOW_CHECKOUT_IN_PRIMARY=1`` in the environment for the
rare legitimate direct checkout in the primary.

Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``). See
``scripts/hooks/guard-commit-on-main.py`` (the sibling this mirrors) and the
``docs/decisions`` note on worktree lifecycle.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

PROTECTED = {"main", "master"}

# A ``git … checkout|switch`` invocation, capturing the verb and everything
# after it (the segment is already isolated to one command by the caller's
# ``&&``/``;``/``\n`` split, so "everything after" is just this command's args).
_CHECKOUT_RE = re.compile(r"\bgit\b[^\n;&|]*?\b(checkout|switch)\b(.*)")
# ``git -C <path>`` inside a single segment.
_GIT_C_RE = re.compile(r"\bgit\b\s+(?:-c\s+\S+\s+)*-C\s+(['\"]?)([^'\"\s]+)\1")
# A segment that is *only* a ``cd <path>``.
_CD_RE = re.compile(r"""^\s*cd\s+(['\"]?)([^'\"]+)\1\s*$""")

_CREATE_FLAGS = {"-b", "-B", "-c", "-C", "--create", "--force-create"}


def _git(cwd: str, *args: str) -> str:
    """Run ``git <args>`` at ``cwd``; empty string on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _git_ok(cwd: str, *args: str) -> bool:
    """True iff ``git <args>`` exits 0 at ``cwd``.

    Distinct from ``_git``: some checks (``show-ref --verify --quiet``) are
    silent on success — reading their exit code, not their (empty) stdout, is
    the only reliable signal.
    """
    try:
        subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, check=True
        )
        return True
    except Exception:
        return False


def _resolve(base: str, path: str) -> str:
    path = os.path.expanduser(
        path.strip()
    )  # strip: a `cd /x && …` split leaves a trailing space
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def _is_primary(cwd: str) -> bool:
    """True when ``cwd`` resolves into the PRIMARY checkout, not a linked worktree.

    The primary and every linked worktree share one ``--git-common-dir``, but
    only the primary's own toplevel *equals* that common dir's parent — a
    linked worktree's toplevel is its own (different) directory.
    """
    common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if not common or not top:
        return False
    return os.path.normpath(top) == os.path.normpath(os.path.dirname(common))


def _positional_tokens(args: str) -> list[str]:
    """Non-flag tokens from a checkout/switch argument string.

    A lone ``-`` (the "previous branch" sentinel) is kept as a token, not
    treated as a flag. ``--`` ends option parsing (everything after is a
    pathspec) — represented by a sentinel so callers can detect it.
    """
    out: list[str] = []
    for tok in args.split():
        if tok == "--":
            out.append("--PATHSPEC--")
            break
        if tok == "-" or not tok.startswith("-"):
            out.append(tok)
    return out


def _target_branch(verb: str, args: str, cwd: str) -> str | None:
    """The branch name a ``checkout``/``switch`` invocation targets, or ``None``.

    ``None`` covers: no target, ``-`` (previous branch), an explicit ``--``
    pathspec, and — for bare ``checkout`` only — a name that doesn't resolve
    to an existing local branch (assumed to be a file).
    """
    positional = _positional_tokens(args)
    if not positional or positional[0] in ("-", "--PATHSPEC--"):
        return None
    target = positional[0]
    tokens = args.split()
    creates = any(t in _CREATE_FLAGS for t in tokens)
    if verb == "switch":
        return target  # switch is unambiguous: always a branch (new or existing)
    # verb == "checkout"
    if creates:
        return target  # `-b`/`-B <name>`: about to create it, unconditionally a branch
    if _git_ok(cwd, "show-ref", "--verify", "--quiet", f"refs/heads/{target}"):
        return target
    return None  # doesn't resolve as a local branch — treat as a file checkout


def evaluate(command: str, cwd: str) -> str | None:
    """Return a deny reason, or ``None`` to allow. Pure & testable.

    Walks the command's ``&&`` / ``;`` / ``||`` / newline segments left-to-
    right, tracking the effective directory (updated by ``cd``), and denies
    the first checkout/switch whose resolved cwd is the primary and whose
    target isn't ``main``/``master``.
    """
    if not isinstance(command, str) or not re.search(r"\b(checkout|switch)\b", command):
        return None

    cur = cwd
    for seg in re.split(r"&&|\|\||;|\n", command):
        cd = _CD_RE.match(seg)
        if cd:
            cur = _resolve(cur, cd.group(2))
            continue
        m = _CHECKOUT_RE.search(seg)
        if not m:
            continue
        verb, rest = m.group(1), m.group(2)
        gc = _GIT_C_RE.search(seg)
        target_dir = _resolve(cur, gc.group(2)) if gc else cur
        branch = _target_branch(verb, rest, target_dir)
        if branch is None or branch in PROTECTED:
            continue
        if not _is_primary(target_dir):
            continue
        return (
            f"Refusing `git {verb} {branch}` in the PRIMARY checkout (at "
            f"{target_dir}). That drifts the primary off `main` — the "
            "footgun scripts/hooks/heal-primary-branch.sh exists to fix. "
            "Do the branch work in a `claude -w` worktree instead. If this "
            "really is intended, set ALLOW_CHECKOUT_IN_PRIMARY=1."
        )
    return None


def main() -> int:
    if os.environ.get("ALLOW_CHECKOUT_IN_PRIMARY"):
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

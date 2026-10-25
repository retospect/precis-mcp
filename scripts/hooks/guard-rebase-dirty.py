#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook: deny ``git rebase`` on a dirty tree with no autostash.

The footgun this closes (gr514625/gr514631): mid-batch the builder ran
``git rebase origin/main`` on a dirty worktree, git refused, the retry
added ``--autostash`` — and the resulting history rewrite silently
changed every recorded sha. Git DOES refuse a bare rebase on a dirty
tree, but only after the command runs and the operator then reaches for
``--autostash``, which has its own trap (gr514868: the autostash pop
restores staged changes UNSTAGED). Denying up front turns both steps
into one message: commit first.

Scope, deliberately narrow:
- Only ``git rebase`` segments (``pull --rebase`` and ``merge`` are the
  sibling guard's territory), and only when autostash is NOT in play:
  an explicit ``--autostash`` (a later ``--no-autostash`` re-disables), a
  ``-c rebase.autostash=<bool>`` inline config, or the
  ``rebase.autoStash`` config all pass — with a clean index that
  round-trips safely, and a dirty index is guard-autostash-staged.py's
  denial, not this one's.
- Only when the tree is actually dirty: ``diff --quiet`` or
  ``diff --cached --quiet`` exits 1. Untracked files don't count (git
  rebases fine over them); anything unverifiable passes.
- Rebase resume verbs (``--continue`` / ``--abort`` / ``--skip`` /
  ``--quit`` / ``--edit-todo`` / ``--show-current-patch``) pass
  untouched: they take no autostash flag and refusing ``--continue``
  mid-rebase would strand the user.
- Follows a leading ``cd <path>`` and ``git -C <path>`` so the tree is
  read where the verb runs.

Escape hatch: ``ALLOW_REBASE_DIRTY=1``.

Wiring lives in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``),
next to ``guard-autostash-staged.py`` — REPORT-ONLY from the tier that
shipped this; the settings file is permission-gated.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# A ``git [-c k=v | -C path]* rebase …`` invocation inside one segment:
# group 1 is the pre-verb option block, group 2 the args tail.
_GIT_RE = re.compile(
    r"\bgit\b((?:\s+-[cC]\s+\S+)*)\s+rebase(?=[\s;&|'\"]|$)([^\n;&|]*)"
)
_GIT_C_RE = re.compile(r"\bgit\b\s+(?:-c\s+\S+\s+)*-C\s+(['\"]?)([^'\"\s]+)\1")
_CD_RE = re.compile(r"""^\s*cd\s+(['\"]?)([^'\"]+)\1\s*$""")
_DASH_C_RE = re.compile(r"-c\s+([^=\s]+?)(?:=(\S*))?(?=\s|$)")
_REBASE_NAV_RE = re.compile(
    r"--(continue|abort|skip|quit|edit-todo|show-current-patch)\b"
)
_ON_RE = re.compile(r"--autostash(?:=(\S*))?")
_OFF_RE = re.compile(r"--no-autostash(?:=(\S*))?")


def _git(cwd: str, *args: str) -> str:
    """Run ``git <args>`` at ``cwd``; empty string on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=10,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _git_rc(cwd: str, *args: str) -> int:
    """Exit code of ``git <args>`` at ``cwd``; -1 when git can't even start."""
    try:
        return subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            check=False,
            timeout=10,
        ).returncode
    except Exception:
        return -1


def _truthy(value: str | None) -> bool:
    """Git-bool parse: ``None`` (bare ``-c key`` or bare ``--flag``) is true."""
    if value is None:
        return True
    return value.strip().lower() not in ("", "false", "no", "off", "0")


def _config_bool(cwd: str, key: str, cli_cfg: dict[str, str | None]) -> bool:
    """Effective bool config at ``cwd``: a ``-c key=value`` override (git
    lower-cases ``-c`` keys) beats the config file."""
    if key in cli_cfg:
        return _truthy(cli_cfg[key])
    return _git(cwd, "config", "--get", "--bool", key).lower() == "true"


def _is_dirty(cwd: str) -> bool:
    """Unstaged tracked edits OR a staged index — the two states ``git
    rebase`` refuses on. ``>1`` (not a repo) is not this guard's to deny."""
    return (
        _git_rc(cwd, "diff", "--quiet") == 1
        or _git_rc(cwd, "diff", "--cached", "--quiet") == 1
    )


def _resolve(base: str, path: str) -> str:
    path = os.path.expanduser(path.strip())
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def _flag_state(tail: str) -> bool | None:
    """The last ``--(no-)autostash`` in the args: True/False, or None."""
    states: list[tuple[int, bool]] = []
    for m in _ON_RE.finditer(tail):
        states.append((m.start(), _truthy(m.group(1))))
    for m in _OFF_RE.finditer(tail):
        states.append((m.start(), not _truthy(m.group(1))))
    if not states:
        return None
    return max(states)[1]


def evaluate(command: str, cwd: str) -> str | None:
    """Return a deny reason, or ``None`` to allow. Pure & testable.

    Walks the command's ``&&`` / ``;`` / ``||`` / newline segments
    left-to-right, tracking the effective directory, and denies the first
    dirty-tree ``git rebase`` that carries no autostash.
    """
    if not isinstance(command, str):
        return None
    cur = cwd
    for seg in re.split(r"&&|\|\||;|\n", command):
        cd = _CD_RE.match(seg)
        if cd:
            cur = _resolve(cur, cd.group(2))
            continue
        m = _GIT_RE.search(seg)
        if not m:
            continue
        opts, tail = m.groups()
        if _REBASE_NAV_RE.search(tail):
            continue
        gc = _GIT_C_RE.search(seg)
        target = _resolve(cur, gc.group(2)) if gc else cur
        cli_cfg = {m.group(1).lower(): m.group(2) for m in _DASH_C_RE.finditer(opts)}
        flag = _flag_state(tail)
        if flag:
            continue  # autostash requested — its own guard gates the index
        if flag is None and _config_bool(target, "rebase.autostash", cli_cfg):
            continue  # configured autostash — same story
        if not _is_dirty(target):
            continue
        return (
            f"Refusing `git rebase` without autostash on a dirty tree at "
            f"{target}: git refuses mid-command anyway, and the usual retry "
            "then rewrites every recorded sha (gr514625). Commit the "
            "changes first (preferred); with a clean INDEX, `--autostash` "
            "is allowed — but not over a staged index, which its pop "
            "restores UNSTAGED (gr514868). Escape: ALLOW_REBASE_DIRTY=1."
        )
    return None


def main() -> int:
    if os.environ.get("ALLOW_REBASE_DIRTY"):
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

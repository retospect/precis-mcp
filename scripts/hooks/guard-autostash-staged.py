#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook: block an autostash git verb while the index is non-empty.

The footgun this closes (gr514868): ``git rebase --autostash`` — and the
``git pull`` / ``git merge`` forms — stashes staged changes before the
operation and pops them back UNSTAGED: the stash apply does not restore the
index. The next ``git commit`` then silently takes only part of the diff; a
builder lost half a commit this way twice in one day and nearly shipped it.
Deny the command up front — commit first, then rebase — instead of trusting
the operator to notice the flattened staging afterwards.

Scope, deliberately narrow to stay false-positive-free:
- Only ``git rebase`` / ``git pull`` / ``git merge`` segments, and only when
  autostash is actually in play: an explicit ``--autostash`` (a later
  ``--no-autostash`` re-allows), a ``-c <key>=true`` inline config, or the
  matching ``rebase.autoStash`` / ``merge.autoStash`` config — for ``pull``
  the key follows the resolved mode (``--rebase[=…]`` flag, then
  ``branch.<name>.rebase``, then ``pull.rebase``), since a merge pull reads
  ``merge.autoStash`` and a rebase pull reads ``rebase.autoStash``.
- Only when ``git -C <dir> diff --cached --quiet`` exits 1 — staged changes
  exist to flatten. A clean index round-trips the stash untouched, so
  autostash of unstaged edits is harmless and passes.
- Rebase resume verbs (``--continue`` / ``--abort`` / ``--skip`` /
  ``--quit`` / ``--edit-todo`` / ``--show-current-patch``) pass untouched:
  they take no autostash flag, and refusing ``--continue`` mid-rebase would
  strand the user.
- Follows a leading ``cd <path>`` and ``git -C <path>`` so the index is read
  at the dir the verb actually targets. Not gated on ``scripts/ship``: the
  staged-state loss is plain git behaviour, not a ship-gate bypass.

Escape hatch: ``ALLOW_AUTOSTASH_STAGED=1``.

Wires in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``), next to
``guard-git-stash.py``. That file is permission-gated; the wiring diff ships
alongside this hook.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

# A ``git [-c k=v | -C path]* (rebase|pull|merge) …`` invocation inside one
# segment: group 1 is the pre-verb option block, group 2 the verb, group 3
# the args tail (ends at a shell metachar inside the segment).
_GIT_RE = re.compile(
    r"\bgit\b((?:\s+-[cC]\s+\S+)*)\s+(rebase|pull|merge)(?=[\s;&|'\"]|$)([^\n;&|]*)"
)
# ``git -C <path>`` inside a single segment.
_GIT_C_RE = re.compile(r"\bgit\b\s+(?:-c\s+\S+\s+)*-C\s+(['\"]?)([^'\"\s]+)\1")
# A segment that is *only* a ``cd <path>``.
_CD_RE = re.compile(r"""^\s*cd\s+(['\"]?)([^'\"]+)\1\s*$""")
# ``-c <k=v>`` pairs inside the pre-verb option block; ``-c k`` (no ``=``) is
# git's boolean-true shorthand, so group 2 may be missing.
_DASH_C_RE = re.compile(r"-c\s+([^=\s]+?)(?:=(\S*))?(?=\s|$)")
# Rebase navigation/resume verbs: they take no autostash flag, and refusing
# ``--continue`` mid-rebase would strand the user.
_REBASE_NAV_RE = re.compile(
    r"--(continue|abort|skip|quit|edit-todo|show-current-patch)\b"
)
# ``--rebase`` / ``--rebase=<mode>`` on pull; ``--rebase=false`` means merge.
_PULL_REBASE_RE = re.compile(r"--rebase(?:=(\S*))?")
_ON_RE = re.compile(r"--autostash(?:=(\S*))?")
_OFF_RE = re.compile(r"--no-autostash(?:=(\S*))?")


def _git(cwd: str, *args: str) -> str:
    """Run ``git <args>`` at ``cwd``; empty string on any failure."""
    try:
        out = subprocess.run(
            ["git", "--no-optional-locks", "-C", cwd, *args],
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
            ["git", "--no-optional-locks", "-C", cwd, *args],
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


def _has_staged(cwd: str) -> bool:
    """``diff --cached --quiet``: 0 clean, 1 staged changes, >1 not a repo or
    unreadable — only the 1 is this guard's to deny."""
    return _git_rc(cwd, "diff", "--cached", "--quiet") == 1


def _resolve(base: str, path: str) -> str:
    path = os.path.expanduser(path.strip())
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def _flag_state(tail: str) -> bool | None:
    """The last ``--(no-)autostash`` in the verb's args: True/False, or None
    when neither appears. ``--autostash=<bool>`` and the double negation
    ``--no-autostash=false`` parse as their values."""
    states: list[tuple[int, bool]] = []
    for m in _ON_RE.finditer(tail):
        states.append((m.start(), _truthy(m.group(1))))
    for m in _OFF_RE.finditer(tail):
        states.append((m.start(), not _truthy(m.group(1))))
    if not states:
        return None
    return max(states)[1]


def _pull_rebases(cwd: str, tail: str, cli_cfg: dict[str, str | None]) -> bool:
    """Whether this pull is a rebase: the flag first (last of --rebase /
    --no-rebase wins), then ``branch.<name>.rebase``, then ``pull.rebase``."""
    matches = list(_PULL_REBASE_RE.finditer(tail))
    no = tail.rfind("--no-rebase")
    last = matches[-1] if matches else None
    if last is not None and last.start() > no:
        return _truthy(last.group(1))
    if no != -1:
        return False
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch and _config_bool(cwd, f"branch.{branch}.rebase", cli_cfg):
        return True
    return _config_bool(cwd, "pull.rebase", cli_cfg)


def _autostash_key(
    cwd: str, verb: str, tail: str, cli_cfg: dict[str, str | None]
) -> str:
    """The config key that decides autostash for this verb."""
    if verb == "rebase":
        return "rebase.autostash"
    if verb == "merge":
        return "merge.autostash"
    return (
        "rebase.autostash" if _pull_rebases(cwd, tail, cli_cfg) else "merge.autostash"
    )


def evaluate(command: str, cwd: str) -> str | None:
    """Return a deny reason, or ``None`` to allow. Pure & testable.

    Walks the command's ``&&`` / ``;`` / ``||`` / newline segments left-to-
    right, tracking the effective directory (updated by ``cd``), and denies
    the first autostash verb whose index has staged changes.
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
        opts, verb, tail = m.groups()
        if verb == "rebase" and _REBASE_NAV_RE.search(tail):
            continue
        gc = _GIT_C_RE.search(seg)
        target = _resolve(cur, gc.group(2)) if gc else cur
        cli_cfg = {m.group(1).lower(): m.group(2) for m in _DASH_C_RE.finditer(opts)}
        flag = _flag_state(tail)
        if flag is False:
            continue  # explicit --no-autostash wins over any config
        if not _has_staged(target):
            continue  # nothing the autostash pop can flatten
        if flag:
            why = "--autostash"
        else:
            key = _autostash_key(target, verb, tail, cli_cfg)
            if not _config_bool(target, key, cli_cfg):
                continue
            why = f"config {key}=true"
        return (
            f"Refusing `git {verb}` with autostash ({why}) at {target}: the "
            "index has staged changes, and the autostash pop restores them "
            "UNSTAGED — the next `git commit` silently takes only part of "
            "the diff (gr514868). Commit first, then re-run; verify each "
            "commit afterwards with `git show --stat`. Escape: "
            "ALLOW_AUTOSTASH_STAGED=1."
        )
    return None


def main() -> int:
    if os.environ.get("ALLOW_AUTOSTASH_STAGED"):
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

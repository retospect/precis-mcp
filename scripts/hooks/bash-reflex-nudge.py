#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash) — nudge two reflex habits toward the
purpose-built tool.

``coderef-nudge.py`` only ever sees the native ``Grep`` tool, but a symbol
search run via ``Bash`` (``rg``/``grep``/``egrep``) is invisible to it — and
in practice that's most of the traffic. This hook fills that gap plus a
second, unrelated reflex: cluster ops (``ssh melchior|caspar|balthazar``,
``scripts/prod-psql``) run inline on the main loop instead of delegated to
the cheaper ``cluster-ops``/``cluster-admin`` agents.

Both rules are NON-BLOCKING (``additionalContext`` only, exit 0) and
deliberately narrow — a nudge that fires too often gets tuned out, same
rationale as ``coderef-nudge.py``'s own docstring. When extraction is
ambiguous, both rules stay silent rather than guess.

- **Rule A (coderef).** An ``rg``/``grep``/``egrep`` invocation (optionally
  after a single leading ``cd <path> &&``) whose search pattern is a bare
  Python identifier — same ``is_symbol_candidate`` heuristic as
  ``coderef-nudge.py`` (shared via ``_symbol_heuristic.py``), plus a light
  Python-target check (an explicit non-Python ``--include``/``--type``/``-g``
  glob silences it; unscoped defaults to code-ish, mirroring the Grep hook).
- **Rule B (cluster-ops).** The command starts with (or contains, after a
  leading ``cd``) ``ssh melchior|caspar|balthazar`` or invokes
  ``scripts/prod-psql`` — nudges toward the read-only ``cluster-ops`` agent
  (or ``cluster-admin`` for a documented write/runbook step) so raw log/psql
  dumps land in the sub-agent's cheap context, not the caller's.

Wired in .claude/settings.json (PreToolUse, matcher "Bash").
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _symbol_heuristic import is_symbol_candidate

# A single leading `cd <path> && ` prefix (the one case the spec calls out;
# deeper segment-walking is deliberately out of scope — stay silent instead).
_CD_PREFIX = re.compile(r"^\s*cd\s+\S+\s*&&\s*")

# The grep-family invocation itself, optionally through a full path and/or
# preceded by env-var assignments (`FOO=bar rg ...`).
_GREP_CMD = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:\S*/)?(?:rg|grep|egrep)\b(?P<rest>.*)$"
)

# Explicit non-Python scoping flags: `--include=GLOB`, `-g GLOB` (rg),
# `--type=TYPE` / `-t TYPE` (rg).
_INCLUDE_RE = re.compile(r"(?:--include=|-g\s+)(\S+)")
_TYPE_RE = re.compile(r"(?:--type=|-t\s+)(\S+)")

_SHELL_BREAK = {"|", "||", "&&", ";"}

_SSH_RE = re.compile(r"\bssh\s+(?:-\S+\s+)*(melchior|caspar|balthazar)\b")

# A leading ``cd <path>`` segment for the redundant-own-tree check. Tolerates a
# trailing redirect (``2>/dev/null``) and either ``;`` or ``&&`` separator — the
# exact shapes the audit found the stricter deny-guard regex missing.
_CD_LEAD = re.compile(
    r"""^\s*cd\s+(['\"]?)(?P<path>[^'\"]+?)\1\s*(?:\d*[<>&]+\s*\S+\s*)*(?:;|&&)"""
)


def _grep_target_ok(rest: str) -> bool:
    """True unless the search is explicitly scoped away from Python."""
    m = _INCLUDE_RE.search(rest)
    if m:
        return "py" in m.group(1).lower()
    m = _TYPE_RE.search(rest)
    if m:
        return m.group(1).lower() in ("py", "python")
    return True  # unscoped -> treat as code-ish, mirrors coderef-nudge.py


def _grep_pattern(rest: str) -> str | None:
    """First non-flag token after the grep/rg verb, or None if unclear."""
    try:
        tokens = shlex.split(rest)
    except ValueError:
        return None
    for tok in tokens:
        if tok in _SHELL_BREAK:
            return None  # pattern would live in a later segment - bail
        if tok.startswith("-"):
            continue  # a flag (e.g. -e, -i, -n, --include=...)
        return tok
    return None


def _rule_a(command: str) -> str | None:
    body = _CD_PREFIX.sub("", command, count=1)
    m = _GREP_CMD.match(body)
    if not m:
        return None
    rest = m.group("rest")
    if not _grep_target_ok(rest):
        return None
    pattern = _grep_pattern(rest)
    if pattern is None:
        return None
    tok = pattern.strip("\"'")
    if not is_symbol_candidate(tok):
        return None
    return (
        f"[coderef] grepping (via Bash) for the symbol `{tok}` — for "
        f"who-calls / what-depends-on over Python, `scripts/coderef callers "
        f"<file.py::{tok}>` (or `deps`) is exact: no same-named false "
        "positives, and it returns the connected code, not every text hit. "
        "`search_code` is the fuzzy complement for where-is/how-does. Grep "
        "stays right for text/strings/non-Python or a symbol you can't yet name."
    )


def _rule_b(command: str) -> str | None:
    body = _CD_PREFIX.sub("", command, count=1)
    if _SSH_RE.search(body):
        host = _SSH_RE.search(body).group(1)  # type: ignore[union-attr]
        return (
            f"[cluster-ops] ssh to `{host}` inline burns main-loop context on "
            "raw log/journal output. For a read-only check, the `cluster-ops` "
            "agent (haiku) SSHes, reads, and returns a short digest instead; "
            "for a documented write/runbook step, `cluster-admin` (sonnet)."
        )
    if "scripts/prod-psql" in body:
        return (
            "[cluster-ops] `scripts/prod-psql` inline pulls raw prod rows into "
            "the main-loop context. The `cluster-ops` agent (haiku) runs the "
            "same read-only query and returns a short digest instead."
        )
    return None


def _rule_c(command: str, cwd: str) -> str | None:
    """Nudge a redundant ``cd <own-worktree>; …`` prefix.

    cwd is already this worktree and the harness re-anchors it there after every
    Bash call, so a leading ``cd`` into the current tree is pure token waste on
    every command. Non-blocking (the deny-guard only ever fires on *cross*-tree
    cd; this catches the far-more-common same-tree redundancy the prose misses).
    """
    if not cwd:
        return None
    m = _CD_LEAD.match(command)
    if not m:
        return None
    raw = m.group("path").strip()
    tgt = os.path.normpath(os.path.expanduser(raw))
    if not os.path.isabs(tgt):
        return None
    cwd_n = os.path.normpath(cwd)
    if tgt == cwd_n or tgt.startswith(cwd_n + os.sep):
        return (
            "[cwd] the shell is already in this worktree and the harness "
            "re-anchors cwd here after every Bash call — the leading "
            f"`cd {raw};` is redundant on every command. Run bare; to read "
            "another tree use `git -C <path> …`."
        )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return 0
    cwd = payload.get("cwd") or ""

    note = _rule_a(command) or _rule_b(command) or _rule_c(command, cwd)
    if note is None:
        return 0
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

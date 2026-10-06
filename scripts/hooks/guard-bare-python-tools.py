#!/usr/bin/env python3
"""PreToolUse hook: refuse bare ``pytest`` / ``pip`` / ``mypy`` in Bash.

The ``uv`` for everything convention (docs/conventions/invariants.md): bare
tools run in an unpinned host environment and report phantom failures. The
wired paths are ``scripts/test`` (tests), ``uv run mypy`` and ``uv add`` /
``uv sync`` (packages).

Refused: a simple command (split on ``; & | ( )`` and newlines) whose command
word is ``pytest``, ``pip``, ``pip3``, ``mypy``, or ``python[3] -m pytest|pip``,
after skipping env assignments and ``rtk`` / ``sudo`` / ``env`` style wrappers.
Allowed: ``uv run ...``, ``uvx ...``, ``scripts/test ...``, tool names that are
only arguments (``git log | grep pytest``), and anything after ``ssh`` (not
this machine's shell). Any parse doubt allows. Exit 2 + stderr is the refusal.

Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``).
"""

from __future__ import annotations

import json
import re
import shlex
import sys

_BARE = {"pytest", "pip", "pip3", "mypy"}
_WRAPPERS = {"rtk", "proxy", "sudo", "env", "command", "time", "nohup", "exec"}
_PY = re.compile(r"python3?(\.\d+)?$")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")

_MSG = {
    "pytest": "run tests via `scripts/test <paths>` (or `scripts/test --impacted`)",
    "mypy": "run `uv run mypy ...` (or `scripts/test --typecheck`)",
    "pip": "use `uv add` / `uv sync` (or `uv run --with ...`)",
}


def _segments(command: str) -> list[list[str]] | None:
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return None
    segs: list[list[str]] = [[]]
    for tok in tokens:
        if tok and set(tok) <= set(";&|()"):
            segs.append([])
        else:
            segs[-1].append(tok)
    return [s for s in segs if s]


def evaluate(command: str) -> str | None:
    """Return a refusal message, or ``None`` to allow. Pure & testable."""
    if not isinstance(command, str) or "<<" in command:
        return None
    if not re.search(r"pytest|pip|mypy", command):
        return None
    segs = _segments(command)
    if segs is None:
        return None
    for seg in segs:
        i = 0
        while i < len(seg) and (_ASSIGN.match(seg[i]) or seg[i] in _WRAPPERS):
            i += 1
        rest = seg[i:]
        if not rest or rest[0] == "ssh":
            continue
        head = rest[0]
        if head in _BARE:
            kind = "pip" if head.startswith("pip") else head
            return f"Refusing bare `{head}`: {_MSG[kind]}."
        if _PY.match(head) and len(rest) >= 3 and rest[1] == "-m":
            if rest[2] == "pytest":
                return f"Refusing `{head} -m pytest`: {_MSG['pytest']}."
            if rest[2] == "pip":
                return f"Refusing `{head} -m pip`: {_MSG['pip']}."
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        command = (payload.get("tool_input") or {}).get("command", "")
        reason = evaluate(command)
    except Exception:  # parse doubt -> allow
        return 0
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

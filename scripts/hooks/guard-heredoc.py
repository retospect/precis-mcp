#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook: refuse heredocs in a shell command (gr514124, gr514330).

CLAUDE.md's one-plain-command rule bans heredocs (``cat <<EOF … EOF``) —
a multi-line body smuggles every other forbidden shape past a line-based
review and past the guards that scan the command string. Under a
``claude -w`` session the built-in worktree-isolation guard refuses them;
under Devin there is no built-in guard, so the same command ran. This
hook is the repo-side refuse so the documented rule binds identically in
both runtimes once it is wired into ``.claude/settings.json`` (matcher
``Bash``) — the generated Devin mirror (``.devin/hooks.v1.json``, matcher
``exec``) then picks it up on the next ``scripts/gen-devin-hooks render``.

Detected: ``<<`` (also ``<<-``, ``<<~``, a quoted/escaped delimiter, an
explicit fd like ``3<<EOF``) after quoted spans and ``$((…))`` arithmetic
are scrubbed. Allowed: ``<<<`` herestrings (single-line, not a compound
shape) and ``<<`` inside quotes/arithmetic.

KNOWN LIMIT: a regex scan, not a shell parser — ``<<`` surviving the
scrub as DATA (``git commit -m 'a << b'`` is scrubbed, but exotic
nesting can defeat it) still trips the deny. ``ALLOW_HEREDOC=1`` is the
escape hatch — rare and obvious, same rationale as
``guard-piped-exit-code.py``.

Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``).
"""

from __future__ import annotations

import json
import os
import re
import sys

#: Quoted spans removed before matching — ``'EOF'``/``"EOF"`` delimiters
#: leave the bare ``<<`` behind, so scrubbing them costs no recall while
#: killing the ``git commit -m "a << b"`` false positive.
_QUOTED_RE = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
#: ``$(( … ))`` / ``(( … ))`` arithmetic, where ``<<`` is a bit-shift.
_ARITH_RE = re.compile(r"\$\(\(.*?\)\)|\(\(.*?\)\)", re.S)
#: A heredoc operator after the scrubs: every remaining ``<<`` is one —
#: ``<<-``/``<<~``/quoted delimiters all start with it; a ``<<<``
#: herestring (single-line, allowed) is the only ``<<`` excluded.
_HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)")


def evaluate(command: str) -> str | None:
    """Return a deny reason, or ``None`` to allow. Pure & testable."""
    if not isinstance(command, str) or "<<" not in command:
        return None
    body = _ARITH_RE.sub("", _QUOTED_RE.sub("", command))
    if not _HEREDOC_RE.search(body):
        return None
    return (
        "Refusing a heredoc (`<<…`) in Bash — the one-plain-command rule "
        "(gr514124: the built-in worktree guard refuses this under Claude "
        "but has no Devin equivalent, so the hook denies it in both).\n"
        "Instead: write the file with the Write tool, or write a script "
        "with Write and run it as one plain command. A single-line "
        "`<<<`herestring is not refused.\n"
        "Or set ALLOW_HEREDOC=1 if the shape is genuinely needed."
    )


def main() -> int:
    if os.environ.get("ALLOW_HEREDOC"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or tool_input.get("cmd") or ""
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

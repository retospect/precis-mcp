#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook: block piping a status-critical script into a filter.

The footgun this closes: in a pipeline, the shell reports the **last**
command's exit status. So ``scripts/ship … | tail -30`` exits 0 whenever
``tail`` succeeds — which is always. A RED gate then looks exactly like a green
one, and the caller's next move ("shipped, now deploy") is built on a lie. This
is worse for a backgrounded run, where the harness reports that masked 0 as the
task's result and nothing else contradicts it.

It bit for real on 2026-09-07: ``scripts/ship --impacted … | tail -30`` reported
exit 0 while the script had printed ``✖ gate is RED — not shipping``.

Guarded commands are the ones whose *exit code is the decision* — ship, deploy,
bump. Reading their output is fine; losing their status is not. ``scripts/test``
is deliberately NOT guarded: it prints a ``N failed`` summary line that a tail
still shows, so piping it costs you nothing.

Allowed (untouched):
  * ``set -o pipefail; scripts/ship … | tail``  — pipefail returns the failure
  * a ``|`` inside quotes (``rg "scripts/ship|scripts/round" docs``) — data,
    not a pipe; only a ``bash -c "…"`` / ``sh -c '…'`` argument is still read
  * anything referencing ``PIPESTATUS`` — the caller is handling it explicitly
  * ``scripts/ship … > log 2>&1`` then reading the log separately
  * a bare ``scripts/ship …`` with no pipe

Escape hatch: ``ALLOW_PIPED_EXIT=1``.

Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``).
"""

from __future__ import annotations

import json
import os
import re
import sys

#: Scripts whose exit code is the decision, not a detail.
_GUARDED = ("ship", "deploy", "bump")

#: ``scripts/<name>`` in COMMAND POSITION — start of the command, or right
#: after a separator (``;`` ``&&`` ``||`` ``|`` ``(`` newline), allowing leading
#: ``VAR=val`` assignments — then anything that is not a pipe, then a literal
#: ``|`` that is not ``||``.
#:
#: Command position is the load-bearing part. The first version accepted the
#: script after ANY whitespace, so it fired on ``grep -n foo scripts/ship``
#: piped to ``head`` — reading the file, not running it. A guard that blocks
#: inspecting the very script it guards is worse than no guard: it trains you
#: to reach for the escape hatch, which then covers real invocations too.
#:
#: Quoted strings are blanked first (``_blank_quoted``), so ``rg "a|scripts/ship"``
#: is not a pipe.
#:
#: KNOWN LIMIT: unquoted data shapes (a heredoc writing these very tests, say)
#: still trip it. Regex cannot tell code from a string that looks like code
#: without a shell parser. Use ``ALLOW_PIPED_EXIT=1`` for that case — it is
#: rare and obvious.
_PIPED_RE = re.compile(
    r"(?:^|[;&|(\n])\s*(?:[A-Za-z_]\w*=\S*\s+)*"
    r"(?:\./|[\w/.-]*/)?scripts/(" + "|".join(_GUARDED) + r")\b"
    r"[^\n;&|]*\|(?!\|)"
)


#: A single- or double-quoted string (backslash escapes honoured in double).
_QUOTED_RE = re.compile(r'''\'[^\']*\'|"(?:\\.|[^"\\])*"''')

#: What precedes the argument of ``bash -c`` / ``sh -c``: that string is code,
#: so it must stay visible to ``_PIPED_RE``.
_SHELL_C_RE = re.compile(
    r"(?:^|[\s;&|(/])(?:ba|z|da)?sh\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*c\s+$"
)


def _blank_quoted(command: str) -> str:
    """Replace quoted-string contents with spaces (same length).

    A quoted ``|`` (``rg "a|scripts/ship|b"``) is data, not a pipe. The one
    exception is the argument of ``bash -c`` / ``sh -c``, which is executed as
    a command line and so is kept (its quotes turned into ``;``).
    """

    def _sub(m: re.Match[str]) -> str:
        if _SHELL_C_RE.search(command[: m.start()]):
            # Quotes become separators so the inner text is in command position.
            return ";" + m.group(0)[1:-1] + ";"
        return m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1]

    return _QUOTED_RE.sub(_sub, command)


def evaluate(command: str) -> str | None:
    """Return a deny reason, or ``None`` to allow. Pure & testable."""
    if not isinstance(command, str) or "scripts/" not in command or "|" not in command:
        return None
    # The caller is already handling pipeline status explicitly.
    if "pipefail" in command or "PIPESTATUS" in command:
        return None
    m = _PIPED_RE.search(_blank_quoted(command))
    if m is None:
        return None
    name = m.group(1)
    return (
        f"Refusing to pipe `scripts/{name}` — a pipeline reports the LAST "
        "command's status, so the filter's exit 0 masks a red gate and the run "
        "looks successful when it failed.\n"
        "Run it unpiped, one command per call:\n"
        f"  scripts/{name} … > .scratch/{name}.log 2>&1\n"
        "then read the result as the NEXT call:\n"
        f"  tail -30 .scratch/{name}.log\n"
        "Or set ALLOW_PIPED_EXIT=1 if you genuinely do not need the status."
    )


def main() -> int:
    if os.environ.get("ALLOW_PIPED_EXIT"):
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

#!/usr/bin/env python3
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
#: KNOWN LIMIT: this matches the raw command string, so a guarded shape quoted
#: as DATA (a heredoc writing these very tests, say) still trips it. Regex
#: cannot tell code from a string that looks like code without a shell parser.
#: Use ``ALLOW_PIPED_EXIT=1`` for that case — it is rare and obvious.
_PIPED_RE = re.compile(
    r"(?:^|[;&|(\n])\s*(?:[A-Za-z_]\w*=\S*\s+)*"
    r"(?:\./|[\w/.-]*/)?scripts/(" + "|".join(_GUARDED) + r")\b"
    r"[^\n;&|]*\|(?!\|)"
)


def evaluate(command: str) -> str | None:
    """Return a deny reason, or ``None`` to allow. Pure & testable."""
    if not isinstance(command, str) or "scripts/" not in command or "|" not in command:
        return None
    # The caller is already handling pipeline status explicitly.
    if "pipefail" in command or "PIPESTATUS" in command:
        return None
    m = _PIPED_RE.search(command)
    if m is None:
        return None
    name = m.group(1)
    return (
        f"Refusing to pipe `scripts/{name}` — a pipeline reports the LAST "
        "command's status, so the filter's exit 0 masks a red gate and the run "
        "looks successful when it failed.\n"
        "Use one of:\n"
        f"  set -o pipefail; scripts/{name} … | tail -30\n"
        f"  scripts/{name} … > /tmp/{name}.log 2>&1; echo \"EXIT=$?\"; "
        f"tail -30 /tmp/{name}.log\n"
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

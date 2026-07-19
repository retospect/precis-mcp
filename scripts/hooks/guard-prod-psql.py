#!/usr/bin/env python3
"""PreToolUse hook: auto-approve read-only ``scripts/prod-psql`` probes, ask on writes.

The friction this removes: routine prod polling (`scripts/prod-psql "SELECT …"`)
is the read-only way to peek at production, but `prod-psql` connects as
**write-capable `agent_rw`** and has no allow-list entry, so every SELECT
prompts. Blanket-allowing `Bash(scripts/prod-psql:*)` would fix the friction
but silently auto-approve `UPDATE`/`DELETE` on PROD too — the opposite of safe.

So this hook is the single decision point, keyed off the SQL:
- A **lone** `scripts/prod-psql "<read-only SQL>"` (SELECT / EXPLAIN / WITH /
  SHOW / TABLE / VALUES / a `\\d`-style backslash meta-command), with no shell
  chaining and no write keyword anywhere → **allow** (silent).
- `scripts/prod-psql` carrying a write keyword (INSERT/UPDATE/DELETE/DROP/…,
  `FOR UPDATE`, `nextval`, …) → **ask**, naming the danger.
- Anything else through `prod-psql` — piped stdin, interactive shell, compound
  command (`&& … ; … | …`), env-interpolated SQL we can't statically read →
  **no opinion** (return nothing) → normal permission flow still prompts. We
  never auto-approve what we can't prove is a single read-only statement.

Deliberately conservative: when unsure we defer to a prompt, never to allow. A
compound command is never auto-allowed, so a read-only prefix can't smuggle a
trailing `rm -rf` past the prompt.

Escape hatch: ``ALLOW_PROD_WRITE=1`` disables the write **ask** (mirrors
guard-prod-write.py) — it does not widen the read-only allow, which is always on.

Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``).
"""

from __future__ import annotations

import json
import os
import re
import sys

#: Shell metacharacters that could chain a second command onto the prod-psql
#: call. If any appear we refuse to auto-allow (a read-only SQL prefix must not
#: be able to smuggle e.g. ``&& rm -rf`` past the prompt).
_SHELL_CHAINING = re.compile(r"(&&|\|\||[;`]|\$\(|>>|>|<\(|\|)")

#: A lone prod-psql invocation with a single quoted SQL argument. Optional
#: ``FOO=bar`` env prefixes (the documented PRECIS_PROD_* overrides) are allowed.
_LONE_PROD_PSQL = re.compile(
    r"""^\s*
    (?:[A-Z_]+=\S+\s+)*            # optional env-var prefixes
    scripts/prod-psql\s+
    (?P<q>"[^"]*"|'[^']*')         # exactly one quoted SQL argument
    \s*$
    """,
    re.VERBOSE,
)

#: Read-only openers. A statement must start with one of these to be allowed.
_READ_OPENER = re.compile(
    r"^\s*(SELECT|EXPLAIN|WITH|SHOW|TABLE|VALUES)\b"
    r"|^\s*\\[a-z]",  # psql backslash meta-commands: \d \l \dt \x \timing …
    re.IGNORECASE,
)

#: Write / side-effecting keywords. Presence of any (whole-word) disqualifies an
#: auto-allow and, when prod-psql is clearly the target, triggers an ``ask``.
_WRITE_KEYWORDS = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|MERGE|"
    r"REINDEX|VACUUM|CLUSTER|LOCK|CALL|REFRESH|COMMENT|SECURITY|"
    r"nextval|setval|pg_terminate_backend|pg_cancel_backend"
    r")\b"
    r"|\bFOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE)\b"
    r"|\bSET\s+ROLE\b",
    re.IGNORECASE,
)


def _is_readonly_sql(sql: str) -> bool:
    """True only if every part of ``sql`` is provably read-only."""
    body = sql.strip().strip("\"'").strip()
    if not body:
        return False
    if _WRITE_KEYWORDS.search(body):
        return False
    return bool(_READ_OPENER.search(body))


def evaluate(command: str) -> dict[str, str] | None:
    """Return a ``{decision, reason}`` dict, or ``None`` for no opinion.

    Pure & testable — no I/O, no env reads (the ALLOW_PROD_WRITE escape hatch is
    applied by ``main`` so the read-only allow can't be switched off).
    """
    if "scripts/prod-psql" not in command:
        return None

    m = _LONE_PROD_PSQL.match(command)
    if m and not _SHELL_CHAINING.search(command):
        sql = m.group("q")
        if _is_readonly_sql(sql):
            return {
                "decision": "allow",
                "reason": "read-only prod-psql probe (SELECT/EXPLAIN/backslash) — auto-approved",
            }

    # Not a provable read-only lone call. If a write keyword is in play, surface
    # it as an explicit ask; otherwise stay silent and let the normal prompt run.
    if _WRITE_KEYWORDS.search(command):
        return {
            "decision": "ask",
            "reason": (
                "`scripts/prod-psql` with a write/side-effecting statement targets "
                "**PRODUCTION** (`precis_prod`, agent_rw). Prefer read-only SELECTs; "
                "proceed only for a deliberate prod write."
            ),
        }
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str):
        return 0
    result = evaluate(command)
    if result is None:
        return 0
    if result["decision"] == "ask" and os.environ.get("ALLOW_PROD_WRITE"):
        return 0  # escape hatch silences the write-ask, not the read allow
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": result["decision"],
                    "permissionDecisionReason": result["reason"],
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
